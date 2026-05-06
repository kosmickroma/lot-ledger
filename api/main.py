# api/main.py
#
# FastAPI application entry point. Defines all HTTP routes and mounts the
# frontend as static files. Validates credentials at startup so the app
# fails loudly if misconfigured rather than on first user request.
#
# Connects to:
#   api/config.py  — startup validation and database connection helpers
#   api/dcad.py    — parcel queries, classification logic, feature builders
#   api/redfin.py  — async Redfin active listing pull
#   api/geo.py     — polygon bbox helper for Redfin query bounds
#   frontend/      — served as static files at root /

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg2.extras import Json, execute_values
from pydantic import BaseModel

from api.auth import (
    AuthError,
    clear_auth_cookies,
    clear_login_failures,
    ensure_auth_settings,
    ensure_required_roles_exist,
    generate_csrf_token,
    get_client_ip,
    get_current_user,
    get_user_by_email,
    get_user_by_id,
    hash_password,
    login_allowed,
    record_login_failure,
    refresh_session_cookie,
    require_csrf,
    require_role,
    seed_bootstrap_users,
    set_auth_cookies,
    verify_password,
    write_auth_audit_log,
)
from api.config import get_conn, get_session_conn, get_settings, release_conn, release_session_conn
from api.counties.collin import _classify_collin, _normalize_collin_row, query_collin_parcels
from api.counties.dcad import SPTD_LABELS, _estimate_front_depth, build_feature, classify_parcel, query_parcels
from api.counties.denton import _classify_denton, _normalize_denton_row, query_denton_parcels
from api.counties.tad import _normalize_tad_row, _classify_tad, query_tad_parcels
from api.geo import polygon_bbox
from api.redfin import normalize_addr_key
from api.sold import log_redfin_sold_row_count, query_active_listings, query_sold_parcels


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
_job_store: dict[str, dict[str, Any]] = {}
_JOB_TTL_SECONDS = 7200    # 2-hour sliding-window TTL per session
_JOB_MAX = 50              # max jobs held in memory at once
_SESSION_RETENTION_DAYS = 30
logger = logging.getLogger(__name__)

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ADDRESS_SUGGEST_CACHE_TTL_SECONDS = 45
_ADDRESS_SUGGEST_CACHE_MAX = 512
_address_suggest_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}


def _normalize_suggest_query(raw: str) -> str:
    return " ".join(str(raw or "").strip().upper().split())


def _suggest_cache_get(cache_key: tuple[str, int]) -> list[dict[str, Any]] | None:
    payload = _address_suggest_cache.get(cache_key)
    if payload is None:
        return None
    ts, items = payload
    if time.monotonic() - ts > _ADDRESS_SUGGEST_CACHE_TTL_SECONDS:
        _address_suggest_cache.pop(cache_key, None)
        return None
    return [dict(item) for item in items]


def _suggest_cache_put(cache_key: tuple[str, int], items: list[dict[str, Any]]) -> None:
    if len(_address_suggest_cache) >= _ADDRESS_SUGGEST_CACHE_MAX:
        oldest_key = min(_address_suggest_cache, key=lambda key: _address_suggest_cache[key][0])
        _address_suggest_cache.pop(oldest_key, None)
    _address_suggest_cache[cache_key] = (time.monotonic(), [dict(item) for item in items])


def _evict_stale_jobs() -> None:
    """Remove expired jobs then trim to _JOB_MAX (evict oldest first)."""
    now = time.monotonic()
    expired = [
        jid for jid, job in _job_store.items()
        # Use last_accessed for sliding-window TTL; fall back to created_at
        if now - job.get("last_accessed", job.get("created_at", 0)) > _JOB_TTL_SECONDS
    ]
    for jid in expired:
        _job_store.pop(jid, None)
    while len(_job_store) >= _JOB_MAX:
        oldest = min(_job_store, key=lambda jid: _job_store[jid].get("created_at", 0))
        _job_store.pop(oldest, None)


def _ensure_session_schema() -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('developer', 'owner', 'member')),
                    is_active BOOLEAN NOT NULL DEFAULT true,
                    force_password_change BOOLEAN NOT NULL DEFAULT false,
                    session_version INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_by TEXT
                )
                """
            )
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_uq ON users (LOWER(username))")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uq ON users (LOWER(email))")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_audit_log (
                    id SERIAL PRIMARY KEY,
                    actor TEXT NOT NULL,
                    actor_user_id INTEGER,
                    action TEXT NOT NULL,
                    target_user TEXT,
                    target_user_id INTEGER,
                    detail TEXT,
                    ip TEXT,
                    user_agent TEXT,
                    ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_areas (
                    area_id     TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    name        TEXT NOT NULL,
                    polygon     JSONB NOT NULL,
                    user_id     INTEGER REFERENCES users(id),
                    created_at  TIMESTAMPTZ DEFAULT now(),
                    updated_at  TIMESTAMPTZ DEFAULT now()
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS analysis_sessions (
                    session_id      TEXT PRIMARY KEY,
                    polygon         JSONB NOT NULL,
                    parcel_count    INTEGER,
                    county_coverage TEXT[],
                    saved_area_id   TEXT REFERENCES saved_areas(area_id) ON DELETE SET NULL,
                    user_id         INTEGER REFERENCES users(id),
                    created_at      TIMESTAMPTZ DEFAULT now(),
                    last_accessed   TIMESTAMPTZ DEFAULT now(),
                    expires_at      TIMESTAMPTZ DEFAULT (now() + interval '{_SESSION_RETENTION_DAYS} days')
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS session_tags (
                    session_id  TEXT REFERENCES analysis_sessions(session_id) ON DELETE CASCADE,
                    account_num TEXT NOT NULL,
                    county      TEXT NOT NULL,
                    tag_type    TEXT NOT NULL,
                    tag_value   TEXT NOT NULL,
                    user_id     INTEGER REFERENCES users(id),
                    updated_at  TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (session_id, account_num, county, tag_type)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_parcels (
                    id BIGSERIAL PRIMARY KEY,
                    account_num TEXT NOT NULL,
                    county TEXT NOT NULL DEFAULT 'dcad',
                    payload JSONB,
                    user_id INTEGER REFERENCES users(id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS cached_jobs (
                    job_id       TEXT PRIMARY KEY,
                    user_id      INTEGER REFERENCES users(id),
                    created_at   TIMESTAMPTZ DEFAULT now(),
                    expires_at   TIMESTAMPTZ DEFAULT (now() + interval '{_JOB_TTL_SECONDS} seconds'),
                    rows         JSONB NOT NULL,
                    sold_points  JSONB NOT NULL,
                    polygon      JSONB NOT NULL
                )
                """
            )
            cur.execute("ALTER TABLE cached_jobs ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_session_tags_session ON session_tags (session_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_session_tags_user ON session_tags (user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON analysis_sessions (expires_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_saved_area ON analysis_sessions (saved_area_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON analysis_sessions (user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_saved_areas_user ON saved_areas (user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_saved_parcels_user ON saved_parcels (user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cached_jobs_expires ON cached_jobs (expires_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cached_jobs_user ON cached_jobs (user_id)")
        conn.commit()
    finally:
        release_session_conn(conn)


def _finalize_user_scoping() -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id ASC LIMIT 1")
            row = cur.fetchone()
            if row is None:
                return
            owner_id = int(row[0])

            cur.execute("UPDATE saved_areas SET user_id = %s WHERE user_id IS NULL", (owner_id,))
            cur.execute("UPDATE analysis_sessions SET user_id = %s WHERE user_id IS NULL", (owner_id,))
            cur.execute("UPDATE session_tags SET user_id = %s WHERE user_id IS NULL", (owner_id,))
            cur.execute("UPDATE saved_parcels SET user_id = %s WHERE user_id IS NULL", (owner_id,))

            cur.execute("ALTER TABLE saved_areas ALTER COLUMN user_id SET NOT NULL")
            cur.execute("ALTER TABLE analysis_sessions ALTER COLUMN user_id SET NOT NULL")
            cur.execute("ALTER TABLE session_tags ALTER COLUMN user_id SET NOT NULL")
            cur.execute("ALTER TABLE saved_parcels ALTER COLUMN user_id SET NOT NULL")
        conn.commit()
    finally:
        release_session_conn(conn)


def _persist_cached_job_sync(
    job_id: str,
    user_id: int,
    rows: list[dict[str, Any]],
    sold_points: list[dict[str, Any]],
    polygon: list[list[float]],
) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO cached_jobs (job_id, user_id, rows, sold_points, polygon, expires_at)
                VALUES (%s, %s, %s, %s, %s, now() + interval '{_JOB_TTL_SECONDS} seconds')
                ON CONFLICT (job_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    rows = EXCLUDED.rows,
                    sold_points = EXCLUDED.sold_points,
                    polygon = EXCLUDED.polygon,
                    expires_at = now() + interval '{_JOB_TTL_SECONDS} seconds'
                """,
                (job_id, int(user_id), Json(rows), Json(sold_points), Json(polygon)),
            )
        conn.commit()
    finally:
        release_session_conn(conn)


def _load_cached_job(job_id: str) -> dict[str, Any] | None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                                SELECT rows, sold_points, polygon, user_id
                FROM cached_jobs
                WHERE job_id = %s
                  AND expires_at > now()
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                f"""
                UPDATE cached_jobs
                SET expires_at = now() + interval '{_JOB_TTL_SECONDS} seconds'
                WHERE job_id = %s
                """,
                (job_id,),
            )
        conn.commit()
        rows, sold_points, polygon, user_id = row
        return {
            "rows": rows if isinstance(rows, list) else [],
            "redfin_data": {},
            "sold_points": sold_points if isinstance(sold_points, list) else [],
            "polygon": polygon if isinstance(polygon, list) else [],
            "user_id": int(user_id or 0),
            "created_at": time.monotonic(),
            "last_accessed": time.monotonic(),
        }
    finally:
        release_session_conn(conn)


def _counties_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    for row in rows:
        division = str(row.get("division_cd", "") or "").upper()
        if division == "TAD":
            seen.add("tad")
        elif division == "COLLIN":
            seen.add("collin")
        elif division == "DENTON":
            seen.add("denton")
        else:
            seen.add("dcad")
    return sorted(seen)


def _persist_session_sync(
    session_id: str,
    polygon: list[list[float]],
    parcel_count: int,
    county_coverage: list[str],
    user_id: int,
    saved_area_id: str | None = None,
) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO analysis_sessions (
                    session_id, polygon, parcel_count, county_coverage, saved_area_id,
                    user_id, last_accessed, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now(), now() + interval '{_SESSION_RETENTION_DAYS} days')
                ON CONFLICT (session_id) DO UPDATE SET
                    polygon = EXCLUDED.polygon,
                    parcel_count = EXCLUDED.parcel_count,
                    county_coverage = EXCLUDED.county_coverage,
                    saved_area_id = COALESCE(EXCLUDED.saved_area_id, analysis_sessions.saved_area_id),
                    user_id = EXCLUDED.user_id,
                    last_accessed = now(),
                    expires_at = now() + interval '{_SESSION_RETENTION_DAYS} days'
                """,
                (
                    session_id,
                    Json(polygon),
                    int(parcel_count),
                    county_coverage,
                    saved_area_id,
                    int(user_id),
                ),
            )
        conn.commit()
    finally:
        release_session_conn(conn)


async def _persist_session_async(
    session_id: str,
    polygon: list[list[float]],
    parcel_count: int,
    county_coverage: list[str],
    user_id: int,
) -> None:
    try:
        await asyncio.to_thread(
            _persist_session_sync,
            session_id,
            polygon,
            parcel_count,
            county_coverage,
            user_id,
            None,
        )
    except Exception as exc:
        print(f"[session] persist failed for {session_id}: {exc}")


def _load_session_tags(session_id: str, user_id: int) -> dict[tuple[str, str], dict[str, str]]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_num, county, tag_type, tag_value
                FROM session_tags
                WHERE session_id = %s
                                    AND user_id = %s
                """,
                                (session_id, int(user_id)),
            )
            out: dict[tuple[str, str], dict[str, str]] = {}
            for account_num, county, tag_type, tag_value in cur.fetchall():
                key = (str(account_num or ""), str(county or "").lower())
                out.setdefault(key, {})[str(tag_type or "")] = str(tag_value or "")
            return out
    finally:
        release_session_conn(conn)


def _row_county(row: dict[str, Any]) -> str:
    division = str(row.get("division_cd", "") or "").upper()
    if division == "TAD":
        return "tad"
    if division == "COLLIN":
        return "collin"
    if division == "DENTON":
        return "denton"
    return "dcad"


def _apply_session_tags(session_id: str, user_id: int, rows: list[dict[str, Any]]) -> None:
    tags = _load_session_tags(session_id, user_id)
    for row in rows:
        account_num = str(row.get("account_num", "") or "")
        county = _row_county(row)
        payload = tags.get((account_num, county), {})
        row["verified_vacant"] = payload.get("verification", "")
        row["potential_target"] = payload.get("target", "")


def _load_session_polygon(session_id: str, user_id: int) -> list[list[float]] | None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT polygon
                FROM analysis_sessions
                WHERE session_id = %s
                                    AND user_id = %s
                  AND expires_at > now()
                """,
                                (session_id, int(user_id)),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                f"""
                UPDATE analysis_sessions
                SET last_accessed = now(),
                    expires_at = now() + interval '{_SESSION_RETENTION_DAYS} days'
                WHERE session_id = %s
                """,
                (session_id,),
            )
        conn.commit()
        polygon = row[0]
        return polygon if isinstance(polygon, list) else None
    finally:
        release_session_conn(conn)


def _session_exists(session_id: str, user_id: int) -> bool:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM analysis_sessions WHERE session_id = %s AND user_id = %s LIMIT 1", (session_id, int(user_id)))
            return cur.fetchone() is not None
    finally:
        release_session_conn(conn)


def _restore_job_from_session(session_id: str, user_id: int) -> dict[str, Any] | None:
    polygon = _load_session_polygon(session_id, user_id)
    if not polygon or len(polygon) < 3:
        return None

    def _safe_query(fn):
        try:
            return fn(polygon)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        dcad_future = executor.submit(_safe_query, query_parcels)
        tad_future = executor.submit(_safe_query, query_tad_parcels)
        collin_future = executor.submit(_safe_query, query_collin_parcels)
        denton_future = executor.submit(_safe_query, query_denton_parcels)
        dcad_result = dcad_future.result()
        tad_result = tad_future.result()
        collin_result = collin_future.result()
        denton_result = denton_future.result()

    if dcad_result is None and tad_result is None and collin_result is None and denton_result is None:
        return None

    rows: list[dict[str, Any]] = []
    if dcad_result:
        rows.extend(dcad_result.parcels)
    if tad_result:
        rows.extend(tad_result.parcels)
    if collin_result:
        rows.extend(collin_result.parcels)
    if denton_result:
        rows.extend(denton_result.parcels)

    _apply_session_tags(session_id, user_id, rows)
    raw_tags = _load_session_tags(session_id, user_id)
    tags: dict[str, dict[str, Any]] = {}
    for (account_num, _county), payload in raw_tags.items():
        verified_raw = str(payload.get("verification", "") or "").strip().lower()
        target_raw = str(payload.get("target", "") or "").strip().lower()
        verified_vacant = verified_raw if verified_raw in {"yes", "no"} else None
        potential_target = "yes" if target_raw == "yes" else None
        if verified_vacant is None and potential_target is None:
            continue
        existing = tags.get(account_num, {"verified_vacant": None, "potential_target": None})
        if verified_vacant is not None:
            existing["verified_vacant"] = verified_vacant
        if potential_target is not None:
            existing["potential_target"] = potential_target
        tags[account_num] = existing
    return {
        "rows": rows,
        "redfin_data": {},
        "sold_points": [],
        "tags": tags,
        "polygon": polygon,
        "user_id": int(user_id),
        "created_at": time.monotonic(),
        "last_accessed": time.monotonic(),
    }


    def _get_job(job_id: str, user_id: int | None = None) -> dict[str, Any] | None:
    """Return job if it exists and has not expired; evicts on TTL miss. Touching last_accessed keeps the session alive as long as the user is active."""
    job = _job_store.get(job_id)
    if job is not None and user_id is not None and int(job.get("user_id", -1)) != int(user_id):
        return None
    if job is None:
        cached = _load_cached_job(job_id)
        if cached is not None and (user_id is None or int(cached.get("user_id", -1)) == int(user_id)):
            _evict_stale_jobs()
            _job_store[job_id] = cached
            return cached
        restored = _restore_job_from_session(job_id, int(user_id)) if user_id is not None else None
        if restored is None:
            return None
        _evict_stale_jobs()
        _job_store[job_id] = restored
        return restored
    now = time.monotonic()
    if now - job.get("last_accessed", job.get("created_at", 0)) > _JOB_TTL_SECONDS:
        _job_store.pop(job_id, None)
        cached = _load_cached_job(job_id)
        if cached is not None and (user_id is None or int(cached.get("user_id", -1)) == int(user_id)):
            _evict_stale_jobs()
            _job_store[job_id] = cached
            return cached
        restored = _restore_job_from_session(job_id, int(user_id)) if user_id is not None else None
        if restored is None:
            return None
        _evict_stale_jobs()
        _job_store[job_id] = restored
        return restored
    job["last_accessed"] = now
    return job


class AnalyzeRequest(BaseModel):
    polygon: list[list[float]]
    include_redfin: bool = False
    include_sold: bool = False


class MergeJobsRequest(BaseModel):
    job_ids: list[str]


class VerificationRequest(BaseModel):
    verifications: dict[str, str] = {}
    potential_targets: dict[str, str] = {}


class SavedAreaCreateRequest(BaseModel):
    name: str
    polygon: list[list[float]]


class SavedAreaRenameRequest(BaseModel):
    name: str


class LoginRequest(BaseModel):
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class AdminCreateUserRequest(BaseModel):
    username: str
    email: str
    temp_password: str
    role: str = "member"


class AdminResetPasswordRequest(BaseModel):
    temp_password: str


def _password_valid(password: str) -> bool:
    return len(str(password or "")) >= 10


def _serialize_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "email": str(user["email"]),
        "role": str(user["role"]),
        "is_active": bool(user.get("is_active")),
        "force_password_change": bool(user.get("force_password_change")),
    }


def _require_target_user(cur, target_user_id: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT id, username, email, role, is_active, force_password_change, session_version, created_by
        FROM users
        WHERE id = %s
        LIMIT 1
        """,
        (int(target_user_id),),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": int(row[0]),
        "username": str(row[1]),
        "email": str(row[2]),
        "role": str(row[3]),
        "is_active": bool(row[4]),
        "force_password_change": bool(row[5]),
        "session_version": int(row[6] or 1),
        "created_by": str(row[7] or ""),
    }


def _enforce_admin_target_rules(actor: dict[str, Any], target: dict[str, Any], action: str) -> None:
    actor_role = str(actor.get("role") or "")
    target_role = str(target.get("role") or "")
    if target_role == "developer":
        raise HTTPException(status_code=403, detail="Developer accounts cannot be modified via admin endpoints")
    if actor_role == "owner" and target_role != "member":
        raise HTTPException(status_code=403, detail="Owner can manage member accounts only")
    if action == "disable" and target_role == "owner":
        # Keep at least one active owner account.
        conn = get_session_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users WHERE role = 'owner' AND is_active = true")
                active_owners = int(cur.fetchone()[0] or 0)
            conn.commit()
        finally:
            release_session_conn(conn)
        if active_owners <= 1 and bool(target.get("is_active")):
            raise HTTPException(status_code=403, detail="Cannot disable the last active owner account")


def _normalize_csv_filename(raw: str | None) -> str:
    if not raw:
        return "parcels.csv"

    base = raw.strip().replace("/", " ").replace("\\", " ")
    base = _FILENAME_SAFE_RE.sub("_", base).strip("._ ")
    if not base:
        return "parcels.csv"

    if base.lower().endswith(".csv"):
        stem = base[:-4].rstrip("._ ")
    else:
        stem = base

    if not stem:
        stem = "parcels"

    stem = stem[:96]
    return f"{stem}.csv"


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _google_maps_link(row: dict[str, Any]) -> str:
    property_address = str(row.get("property_address", "") or "").strip()
    street_num = str(row.get("street_num", "") or "").strip()
    full_street_name = str(row.get("full_street_name", "") or "").strip()
    city = str(row.get("property_city", "") or row.get("owner_city", "") or "").strip()
    state = str(row.get("property_state", "") or row.get("owner_state", "") or "TX").strip()
    zip_code = str(row.get("property_zip", "") or row.get("owner_zip", "") or "").strip()[:5]

    # Prefer full property address when available; otherwise fall back to
    # street parts. Keep this county-agnostic (no hardcoded city).
    primary_address = property_address or " ".join([street_num, full_street_name]).strip()
    if primary_address:
        parts = [primary_address, city, state, zip_code]
        query_text = ", ".join(part for part in parts if part)
        if query_text:
            return f"https://maps.google.com/?q={quote_plus(query_text)}"

    # For sparse county rows (for example personal-property records with no
    # situs address), use centroid coordinates so link is still usable.
    lat = _safe_float(row.get("lat"))
    lng = _safe_float(row.get("lng"))
    if lat is not None and lng is not None:
        return f"https://maps.google.com/?q={lat},{lng}"

    return "https://maps.google.com"


# Validate required runtime settings at startup.
get_settings()

app = FastAPI(title="LotLedger")


_FORCE_PASSWORD_CHANGE_ALLOWED_PATHS = {
    "/auth/me",
    "/auth/change-password",
    "/auth/logout",
}


def _is_public_path(path: str) -> bool:
    if path in {"/", "/health", "/health/db", "/auth/login"}:
        return True
    if path.startswith("/auth/"):
        return False
    if path.startswith("/admin/"):
        return False
    if path.startswith("/api/"):
        return False
    # Static frontend assets remain public; app is logically gated by auth UI.
    return True


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    is_protected = not _is_public_path(path)
    user: dict[str, Any] | None = None

    if is_protected or path.startswith("/api/") or path.startswith("/auth/") or path.startswith("/admin/"):
        try:
            user = get_current_user(request)
            request.state.current_user = user
        except AuthError:
            user = None

    if path.startswith("/api/") and user is None:
        response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        clear_auth_cookies(response)
        return response

    if path.startswith("/admin/") and user is None:
        response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        clear_auth_cookies(response)
        return response

    if path.startswith("/auth/") and path not in {"/auth/login"} and user is None:
        response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        clear_auth_cookies(response)
        return response

    if user and bool(user.get("force_password_change")):
        if path.startswith("/api/") or path.startswith("/admin/") or path.startswith("/auth/"):
            if path not in _FORCE_PASSWORD_CHANGE_ALLOWED_PATHS and path != "/auth/login":
                return JSONResponse(
                    {"detail": "Password change required", "code": "FORCE_PASSWORD_CHANGE_REQUIRED"},
                    status_code=403,
                )

    response = await call_next(request)

    # Rolling refresh for authenticated requests, except explicit logout.
    if user and path != "/auth/logout":
        refresh_session_cookie(response, user, request)

    if request.url.path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    if not request.cookies.get("ll_csrf"):
        response.set_cookie(
            "ll_csrf",
            generate_csrf_token(),
            max_age=8 * 60 * 60,
            httponly=False,
            secure=os.getenv("AUTH_COOKIE_SECURE", "true").strip().lower() not in {"0", "false", "no"},
            samesite="lax",
            path="/",
        )
    return response


@app.middleware("http")
async def no_cache_frontend(request: Request, call_next):
    return await call_next(request)


@app.post("/auth/login")
async def auth_login(request: Request, payload: LoginRequest, response: Response) -> dict[str, Any]:
    require_csrf(request)
    ip = get_client_ip(request)
    allowed, retry_after = login_allowed(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Too many failed attempts. Retry in {retry_after}s")

    email = str(payload.email or "").strip().lower()
    password = str(payload.password or "")
    user = get_user_by_email(email)
    if user is None or not verify_password(password, str(user.get("password_hash") or "")):
        record_login_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not bool(user.get("is_active")):
        raise HTTPException(status_code=403, detail="Account disabled")

    clear_login_failures(ip)
    set_auth_cookies(response, user)
    return {"user": _serialize_user(user)}


@app.post("/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, bool]:
    if request.cookies.get("ll_session"):
        require_csrf(request)
    clear_auth_cookies(response)
    return {"ok": True}


@app.get("/auth/me")
async def auth_me(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return {"user": _serialize_user(user)}


@app.post("/auth/change-password")
async def auth_change_password(
    request: Request,
    payload: ChangePasswordRequest,
    response: Response,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_csrf(request)
    current_password = str(payload.current_password or "")
    new_password = str(payload.new_password or "")
    confirm_password = str(payload.confirm_password or "")

    if not verify_password(current_password, str(user.get("password_hash") or "")):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if new_password != confirm_password:
        raise HTTPException(status_code=422, detail="Password confirmation does not match")
    if not _password_valid(new_password):
        raise HTTPException(status_code=422, detail="Password must be at least 10 characters")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET password_hash = %s,
                    force_password_change = false,
                    session_version = session_version + 1
                WHERE id = %s
                RETURNING id, username, email, role, is_active, force_password_change, session_version
                """,
                (hash_password(new_password), int(user["id"])),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        release_session_conn(conn)

    if row is None:
        raise HTTPException(status_code=404, detail="User not found")

    refreshed_user = {
        "id": int(row[0]),
        "username": str(row[1]),
        "email": str(row[2]),
        "role": str(row[3]),
        "is_active": bool(row[4]),
        "force_password_change": bool(row[5]),
        "session_version": int(row[6] or 1),
    }
    set_auth_cookies(response, refreshed_user)
    return {"ok": True, "user": _serialize_user(refreshed_user)}


@app.get("/admin/users")
async def admin_list_users(user: dict[str, Any] = Depends(require_role("owner", "developer"))) -> dict[str, Any]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, email, role, is_active, force_password_change, created_at, created_by
                FROM users
                ORDER BY created_at ASC
                """
            )
            users = [
                {
                    "id": int(row[0]),
                    "username": str(row[1]),
                    "email": str(row[2]),
                    "role": str(row[3]),
                    "is_active": bool(row[4]),
                    "force_password_change": bool(row[5]),
                    "created_at": row[6].isoformat() if row[6] else None,
                    "created_by": str(row[7] or ""),
                }
                for row in cur.fetchall()
            ]
    finally:
        release_session_conn(conn)
    return {"users": users}


@app.post("/admin/users")
async def admin_create_user(
    request: Request,
    payload: AdminCreateUserRequest,
    actor: dict[str, Any] = Depends(require_role("owner", "developer")),
) -> dict[str, Any]:
    require_csrf(request)
    username = str(payload.username or "").strip()
    email = str(payload.email or "").strip().lower()
    temp_password = str(payload.temp_password or "")
    role = str(payload.role or "member").strip().lower()

    if not username or not email:
        raise HTTPException(status_code=422, detail="Username and email are required")
    if not _password_valid(temp_password):
        raise HTTPException(status_code=422, detail="Password must be at least 10 characters")

    if actor["role"] == "owner":
        role = "member"
    elif role not in {"owner", "member"}:
        raise HTTPException(status_code=422, detail="Developer may create owner or member users only")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, email, password_hash, role, is_active, force_password_change, created_by)
                VALUES (%s, %s, %s, %s, true, true, %s)
                RETURNING id, username, email, role, is_active, force_password_change
                """,
                (username, email, hash_password(temp_password), role, actor["username"]),
            )
            row = cur.fetchone()
            write_auth_audit_log(
                actor=actor["username"],
                actor_user_id=int(actor["id"]),
                action="create_user",
                target_user=username,
                target_user_id=int(row[0]),
                detail=f"role={role}",
                ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if "users_username_lower_uq" in str(exc) or "users_email_lower_uq" in str(exc):
            raise HTTPException(status_code=409, detail="Username or email already exists")
        raise
    finally:
        release_session_conn(conn)

    return {
        "user": {
            "id": int(row[0]),
            "username": str(row[1]),
            "email": str(row[2]),
            "role": str(row[3]),
            "is_active": bool(row[4]),
            "force_password_change": bool(row[5]),
        }
    }


@app.post("/admin/users/{user_id}/disable")
async def admin_disable_user(
    user_id: int,
    request: Request,
    actor: dict[str, Any] = Depends(require_role("owner", "developer")),
) -> dict[str, Any]:
    require_csrf(request)
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            target = _require_target_user(cur, user_id)
            _enforce_admin_target_rules(actor, target, "disable")
            cur.execute("UPDATE users SET is_active = false WHERE id = %s", (int(user_id),))
            write_auth_audit_log(
                actor=actor["username"],
                actor_user_id=int(actor["id"]),
                action="disable_user",
                target_user=target["username"],
                target_user_id=int(target["id"]),
                detail="",
                ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        conn.commit()
    finally:
        release_session_conn(conn)
    return {"ok": True}


@app.post("/admin/users/{user_id}/enable")
async def admin_enable_user(
    user_id: int,
    request: Request,
    actor: dict[str, Any] = Depends(require_role("owner", "developer")),
) -> dict[str, Any]:
    require_csrf(request)
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            target = _require_target_user(cur, user_id)
            _enforce_admin_target_rules(actor, target, "enable")
            cur.execute("UPDATE users SET is_active = true WHERE id = %s", (int(user_id),))
            write_auth_audit_log(
                actor=actor["username"],
                actor_user_id=int(actor["id"]),
                action="enable_user",
                target_user=target["username"],
                target_user_id=int(target["id"]),
                detail="",
                ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        conn.commit()
    finally:
        release_session_conn(conn)
    return {"ok": True}


@app.post("/admin/users/{user_id}/reset-password")
async def admin_reset_password(
    user_id: int,
    request: Request,
    payload: AdminResetPasswordRequest,
    actor: dict[str, Any] = Depends(require_role("owner", "developer")),
) -> dict[str, Any]:
    require_csrf(request)
    temp_password = str(payload.temp_password or "")
    if not _password_valid(temp_password):
        raise HTTPException(status_code=422, detail="Password must be at least 10 characters")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            target = _require_target_user(cur, user_id)
            _enforce_admin_target_rules(actor, target, "reset")
            cur.execute(
                """
                UPDATE users
                SET password_hash = %s,
                    force_password_change = true,
                    session_version = session_version + 1
                WHERE id = %s
                """,
                (hash_password(temp_password), int(user_id)),
            )
            write_auth_audit_log(
                actor=actor["username"],
                actor_user_id=int(actor["id"]),
                action="reset_password",
                target_user=target["username"],
                target_user_id=int(target["id"]),
                detail="force_password_change=true",
                ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        conn.commit()
    finally:
        release_session_conn(conn)
    return {"ok": True}


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/hoa")
async def hoa_boundaries() -> dict:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT objectid, asso_name, asso_web, status,
                       ST_AsGeoJSON(geom)::json AS geometry
                FROM hoa_boundaries
                ORDER BY asso_name
                """
            )
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        release_conn(conn)

    features = []
    for row in rows:
        geom = row.pop("geometry", None)
        if geom is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "name": row.get("asso_name") or "",
                "url": row.get("asso_web") or "",
                "status": row.get("status") or "",
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/api/counties/boundaries")
async def counties_boundaries() -> dict:
    """
    DFW county boundaries (Dallas, Tarrant, Collin, Denton, Rockwall, Parker, Kaufman).
    Used by frontend for county overlay layer.
    """
    geojson_path = FRONTEND_DIR / "tx_counties_dfw.geojson"
    try:
        with open(geojson_path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="County boundaries file not found")


@app.get("/api/address/suggest")
async def address_suggest(q: str, limit: int = 8) -> dict[str, Any]:
    """
    Texas-only address suggestions from parcel tables.
    Used by frontend typeahead; does not call external geocoders.
    """
    query = _normalize_suggest_query(q)
    if len(query) < 3:
        return {"items": []}

    max_items = max(1, min(int(limit or 8), 10))
    cache_key = (query, max_items)
    cached = _suggest_cache_get(cache_key)
    if cached is not None:
        return {"items": cached}

    prefix = f"{query}%"
    per_county_limit = max(3, max_items)

    def _rows_to_items(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for county, account_num, address, city, lat, lng in rows:
            addr_text = str(address or "").strip()
            acct_text = str(account_num or "").strip()
            county_text = str(county or "").strip().lower()
            if not addr_text or not acct_text or not county_text or lat is None or lng is None:
                continue
            dedupe_key = (county_text, acct_text)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            city_text = str(city or "").strip()
            label = f"{addr_text}, {city_text}, TX" if city_text else f"{addr_text}, TX"
            items.append(
                {
                    "label": label,
                    "address": addr_text,
                    "city": city_text,
                    "county": county_text,
                    "account_num": acct_text,
                    "lat": float(lat),
                    "lng": float(lng),
                }
            )
        items.sort(
            key=lambda item: (
                0 if item["address"].upper().startswith(query) else 1,
                item["address"],
            )
        )
        return items[:max_items]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout TO '900ms'")
            cur.execute(
                """
                WITH dcad AS (
                    SELECT
                        'dcad'::text AS county,
                        p.account_num::text AS account_num,
                        p.property_address::text AS address,
                        p.owner_city::text AS city,
                        ST_Y(p.centroid) AS lat,
                        ST_X(p.centroid) AS lng
                    FROM parcels p
                    WHERE p.centroid IS NOT NULL
                      AND p.property_address IS NOT NULL
                      AND p.property_address <> ''
                      AND upper(p.property_address) LIKE %s
                    ORDER BY p.property_address
                    LIMIT %s
                ),

                tad AS (
                    SELECT
                        'tad'::text AS county,
                        t.account_num::text AS account_num,
                        t.situs_addr::text AS address,
                        t.owner_city::text AS city,
                        ST_Y(t.centroid) AS lat,
                        ST_X(t.centroid) AS lng
                    FROM tad_parcels t
                    WHERE t.centroid IS NOT NULL
                      AND t.situs_addr IS NOT NULL
                      AND t.situs_addr <> ''
                      AND upper(t.situs_addr) LIKE %s
                    ORDER BY t.situs_addr
                    LIMIT %s
                ),

                collin AS (
                    SELECT
                        'collin'::text AS county,
                        c.account_num::text AS account_num,
                        c.property_address::text AS address,
                        c.property_city::text AS city,
                        ST_Y(c.centroid) AS lat,
                        ST_X(c.centroid) AS lng
                    FROM collin_parcels c
                    WHERE c.centroid IS NOT NULL
                      AND c.property_address IS NOT NULL
                      AND c.property_address <> ''
                      AND upper(c.property_address) LIKE %s
                    ORDER BY c.property_address
                    LIMIT %s
                ),

                denton AS (
                    SELECT
                        'denton'::text AS county,
                        d.account_num::text AS account_num,
                        d.property_address::text AS address,
                        d.property_city::text AS city,
                        ST_Y(d.centroid) AS lat,
                        ST_X(d.centroid) AS lng
                    FROM denton_parcels d
                    WHERE d.centroid IS NOT NULL
                      AND d.property_address IS NOT NULL
                      AND d.property_address <> ''
                      AND upper(d.property_address) LIKE %s
                    ORDER BY d.property_address
                    LIMIT %s
                ),

                candidates AS (
                    SELECT * FROM dcad

                    UNION ALL

                    SELECT * FROM tad

                    UNION ALL

                    SELECT * FROM collin

                    UNION ALL

                    SELECT * FROM denton
                )
                SELECT county, account_num, address, city, lat, lng
                FROM candidates
                ORDER BY
                    CASE WHEN upper(address) LIKE %s THEN 0 ELSE 1 END,
                    address
                LIMIT %s
                """,
                (
                    prefix,
                    per_county_limit,
                    prefix,
                    per_county_limit,
                    prefix,
                    per_county_limit,
                    prefix,
                    per_county_limit,
                    prefix,
                    max_items,
                ),
            )

            rows = cur.fetchall()
            items = _rows_to_items(rows)
            _suggest_cache_put(cache_key, items)
            return {"items": items}
    except Exception as exc:
        logger.warning("address_suggest failed (non-fatal): %s", exc)
        return {"items": []}
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        release_conn(conn)


@app.get("/health/db")
async def health_db_check() -> dict[str, str]:
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if conn is not None:
            release_conn(conn)
    return {"status": "ok", "db": "ok"}


def _fetch_dcad_parcel_by_account(account_num: str) -> tuple[dict[str, Any] | None, set[str]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.account_num, p.parcel_key, p.gis_parcel_id,
                       p.owner_name, p.owner_address, p.owner_city, p.owner_state,
                       p.owner_zip, p.street_num, p.full_street_name,
                       p.property_address, p.property_zip, p.division_cd,
                       COALESCE(a.sptd_code, p.sptd_code) AS sptd_code,
                       p.nbhd_cd, p.legal1, p.legal2, p.legal3, p.legal4, p.legal5,
                       p.polygon_geojson,
                       ST_Y(p.centroid) AS lat,
                       ST_X(p.centroid) AS lng,
                       CASE
                        WHEN p.polygon_geojson IS NOT NULL
                            AND (p.polygon_geojson::json)->>'type' IN ('Polygon', 'MultiPolygon')
                        THEN ST_Area(ST_OrientedEnvelope(ST_SetSRID(ST_GeomFromGeoJSON(p.polygon_geojson::text), 4326))::geography)
                            / NULLIF(ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(p.polygon_geojson::text), 4326)::geography), 0)
                        ELSE NULL
                       END AS envelope_ratio,
                       CASE
                        WHEN p.polygon_geojson IS NOT NULL
                            AND (p.polygon_geojson::json)->>'type' IN ('Polygon', 'MultiPolygon')
                        THEN ST_Perimeter(ST_OrientedEnvelope(ST_SetSRID(ST_GeomFromGeoJSON(p.polygon_geojson::text), 4326))::geography) * 3.28084
                        ELSE NULL
                       END AS envelope_perim_ft,
                       CASE
                        WHEN p.polygon_geojson IS NOT NULL
                            AND (p.polygon_geojson::json)->>'type' IN ('Polygon', 'MultiPolygon')
                        THEN ST_Area(ST_OrientedEnvelope(ST_SetSRID(ST_GeomFromGeoJSON(p.polygon_geojson::text), 4326))::geography) * 10.763910416709722
                        ELSE NULL
                       END AS envelope_area_sqft,
                       a.land_val, a.impr_val, a.tot_val, a.isd_desc,
                       r.yr_built, r.tot_living_area, r.tot_main_sf,
                      l.zoning, l.front_dim, l.depth_dim, l.area_size, l.area_uom, l.area_estimated,
                       (e.account_num IS NOT NULL) AS is_exempt_account
                FROM parcels p
                LEFT JOIN appraisal a ON p.account_num = a.account_num
                LEFT JOIN res_detail r ON p.account_num = r.account_num
                LEFT JOIN LATERAL (
                    SELECT zoning, front_dim, depth_dim, area_size, area_uom, area_estimated
                    FROM land_detail
                    WHERE account_num = p.account_num
                    LIMIT 1
                ) l ON TRUE
                LEFT JOIN exempt_accounts e ON p.account_num = e.account_num
                WHERE p.account_num = %s
                LIMIT 1
                """,
                (account_num,),
            )
            row = cur.fetchone()
            if row is None:
                return None, set()
            cols = [desc[0] for desc in cur.description]
            parcel = dict(zip(cols, row))

            sptd_code = str(parcel.get("sptd_code") or "").strip()
            parcel["state_code"] = SPTD_LABELS.get(sptd_code, sptd_code)
            land_val = _safe_float(parcel.get("land_val"))
            tot_val = _safe_float(parcel.get("tot_val"))
            parcel["land_pct"] = (
                round((land_val / tot_val) * 100, 1)
                if land_val is not None and tot_val not in (None, 0)
                else None
            )
            area_size = _safe_float(parcel.get("area_size"))
            front_dim = _safe_float(parcel.get("front_dim"))
            depth_dim = _safe_float(parcel.get("depth_dim"))
            dims_estimated = False
            if front_dim in (None, 0.0) or depth_dim in (None, 0.0):
                est_front, est_depth = _estimate_front_depth(parcel)
                if est_front is not None and est_depth is not None:
                    front_dim = est_front
                    depth_dim = est_depth
                    parcel["front_dim"] = est_front
                    parcel["depth_dim"] = est_depth
                    dims_estimated = True
            parcel["dims_estimated"] = dims_estimated
            area_estimated = bool(parcel.get("area_estimated"))
            if (area_size is None or area_size <= 0) and front_dim and front_dim > 0 and depth_dim and depth_dim > 0:
                parcel["area_size"] = front_dim * depth_dim
                parcel["area_estimated"] = True
            else:
                parcel["area_estimated"] = area_estimated
            parcel["hoa_name"] = ""
            parcel["hoa_url"] = ""

            exempt_set = {account_num} if parcel.get("is_exempt_account") else set()
            return parcel, exempt_set
    finally:
        release_conn(conn)


def _fetch_tad_parcel_by_account(account_num: str) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT parcel_key, account_num, taxpin,
                       owner_name, owner_addr, owner_city, owner_citystate,
                       owner_zip, situs_addr, property_class, state_use_code,
                       legal_descr, school_code,
                       acres, land_acres, land_sqft,
                       year_built, living_area,
                       land_value, improvement_value, total_value,
                       ST_Area(ST_OrientedEnvelope(geom)::geography) / NULLIF(ST_Area(geom::geography), 0) AS envelope_ratio,
                       ST_Perimeter(ST_OrientedEnvelope(geom)::geography) * 3.28084 AS envelope_perim_ft,
                       ST_Area(ST_OrientedEnvelope(geom)::geography) * 10.763910416709722 AS envelope_area_sqft,
                       ST_AsGeoJSON(geom)::json AS polygon_geojson,
                       ST_Y(centroid) AS _lat,
                       ST_X(centroid) AS _lng
                FROM tad_parcels
                WHERE account_num = %s
                LIMIT 1
                """,
                (account_num,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc[0] for desc in cur.description]
            raw = dict(zip(cols, row))
            return _normalize_tad_row(raw)
    finally:
        release_conn(conn)


def _fetch_collin_parcel_by_account(account_num: str) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    parcel_key,
                    account_num,
                    geo_id,
                    owner_name,
                    owner_address,
                    owner_city,
                    owner_state,
                    owner_zip,
                    property_address,
                    property_city,
                    property_zip,
                    state_cd,
                    state_cd_name,
                    class_cd,
                    subdivision,
                    legal_descr,
                    school_code,
                    city_code,
                    zoning,
                    land_sqft,
                    land_acres,
                    living_area,
                    year_built,
                    land_value,
                    improvement_value,
                    total_value,
                    cert_total_value,
                    curr_market_value,
                    curr_assessed_value,
                    curr_ag_use_value,
                    curr_ag_market_value,
                    curr_ag_loss_value,
                    deed_num,
                    deed_type,
                    deed_date,
                    land_type_code,
                    land_type_name,
                    prop_use_code,
                    prop_use_name,
                    prop_type,
                    prop_sub_type,
                    commercial_flag,
                    pool_flag,
                    beds,
                    baths,
                    stories,
                    units,
                    protest_code,
                    entity_codes,
                    exemptions,
                    exempt_homestead,
                    tax_agent_id,
                    tax_agent_name,
                    tax_agent_auth_protest,
                    tax_agent_auth_resolve,
                    tax_agent_mailings,
                    permit_count,
                    latest_permit_date,
                    latest_permit_type,
                    latest_permit_value,
                    protest_case_count,
                    latest_protest_year,
                    latest_protest_status,
                    latest_protest_final_market,
                    protest_active,
                    ag_type,
                    ag_acres,
                    ag_value,
                    ag_market_value,
                    ST_Area(ST_OrientedEnvelope(geom)::geography) / NULLIF(ST_Area(geom::geography), 0) AS envelope_ratio,
                    ST_Perimeter(ST_OrientedEnvelope(geom)::geography) * 3.28084 AS envelope_perim_ft,
                    ST_Area(ST_OrientedEnvelope(geom)::geography) * 10.763910416709722 AS envelope_area_sqft,
                    ST_Area(geom::geography) * 10.763910416709722 AS geom_sqft,
                    ST_AsGeoJSON(geom)::json AS polygon_geojson,
                    ST_Y(centroid) AS _lat,
                    ST_X(centroid) AS _lng
                FROM collin_parcels
                WHERE account_num = %s
                LIMIT 1
                """,
                (account_num,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc[0] for desc in cur.description]
            raw = dict(zip(cols, row))
            return _normalize_collin_row(raw)
    finally:
        release_conn(conn)


def _fetch_denton_parcel_by_account(account_num: str) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.parcel_key,
                    p.account_num,
                    p.geo_id,
                    p.owner_name,
                    p.owner_address,
                    p.owner_city,
                    p.owner_state,
                    p.owner_zip,
                    p.property_address,
                    p.property_city,
                    p.property_zip,
                    p.state_cd,
                    p.exemptions,
                    p.land_value,
                    p.improvement_value,
                    p.total_value,
                    p.land_sqft,
                    p.land_total_sqft,
                    p.land_acres,
                    p.living_area,
                    p.year_built,
                    p.isd_desc,
                    p.entity_codes,
                    p.deed_number,
                    p.deed_date,
                    p.legal_descr,
                    p.subdivision,
                    p.zoning,
                    p.area_estimated,
                    ST_Y(p.centroid) AS _lat,
                    ST_X(p.centroid) AS _lng,
                    ST_AsGeoJSON(p.geom)::json AS polygon_geojson,
                    ST_Perimeter(ST_Transform(p.geom, 2276)) AS envelope_perimeter,
                    ST_Area(ST_Transform(p.geom, 2276)) AS geom_area_sqft,
                    ST_Area(ST_Transform(ST_OrientedEnvelope(p.geom), 2276)) AS envelope_area_sqft,
                    ST_Perimeter(ST_Transform(p.geom, 2276)) AS envelope_perim_ft,
                    ST_Area(ST_Transform(p.geom, 2276)) AS geom_sqft,
                    ST_Area(ST_Transform(ST_OrientedEnvelope(p.geom), 2276)) / NULLIF(ST_Area(ST_Transform(p.geom, 2276)), 0) AS envelope_ratio
                FROM denton_parcels p
                WHERE p.account_num = %s
                LIMIT 1
                """,
                (account_num,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc[0] for desc in cur.description]
            raw = dict(zip(cols, row))
            return _normalize_denton_row(raw)
    finally:
        release_conn(conn)


def _find_dcad_near(lat: float, lng: float) -> str | None:
    """Return the closest DCAD account_num to (lat, lng) within ~440m (0.004°)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.account_num
                FROM parcels p
                WHERE p.centroid IS NOT NULL
                  AND ST_DWithin(
                      p.centroid,
                      ST_SetSRID(ST_Point(%s, %s), 4326),
                      0.004
                  )
                ORDER BY ST_Distance(p.centroid, ST_SetSRID(ST_Point(%s, %s), 4326))
                LIMIT 1
                """,
                (lng, lat, lng, lat),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        release_conn(conn)


def _find_tad_near(lat: float, lng: float) -> str | None:
    """Return a TAD account_num whose polygon contains (lat, lng), or the nearest centroid."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_num FROM tad_parcels
                WHERE geom IS NOT NULL
                  AND ST_Contains(geom, ST_SetSRID(ST_Point(%s, %s), 4326))
                LIMIT 1
                """,
                (lng, lat),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                """
                SELECT account_num FROM tad_parcels
                WHERE centroid IS NOT NULL
                  AND ST_DWithin(
                      centroid,
                      ST_SetSRID(ST_Point(%s, %s), 4326),
                      0.004
                  )
                ORDER BY ST_Distance(centroid, ST_SetSRID(ST_Point(%s, %s), 4326))
                LIMIT 1
                """,
                (lng, lat, lng, lat),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        release_conn(conn)


def _find_collin_near(lat: float, lng: float) -> str | None:
    """Return a Collin account_num whose polygon contains (lat, lng), or nearest centroid."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_num FROM collin_parcels
                WHERE geom IS NOT NULL
                  AND ST_Contains(geom, ST_SetSRID(ST_Point(%s, %s), 4326))
                LIMIT 1
                """,
                (lng, lat),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                """
                SELECT account_num FROM collin_parcels
                WHERE centroid IS NOT NULL
                  AND ST_DWithin(
                      centroid,
                      ST_SetSRID(ST_Point(%s, %s), 4326),
                      0.004
                  )
                ORDER BY ST_Distance(centroid, ST_SetSRID(ST_Point(%s, %s), 4326))
                LIMIT 1
                """,
                (lng, lat, lng, lat),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        release_conn(conn)


def _find_denton_near(lat: float, lng: float) -> str | None:
    """Return a Denton account_num whose polygon contains (lat, lng), or nearest centroid."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_num FROM denton_parcels
                WHERE geom IS NOT NULL
                  AND ST_Contains(geom, ST_SetSRID(ST_Point(%s, %s), 4326))
                LIMIT 1
                """,
                (lng, lat),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                """
                SELECT account_num FROM denton_parcels
                WHERE centroid IS NOT NULL
                  AND ST_DWithin(
                      centroid,
                      ST_SetSRID(ST_Point(%s, %s), 4326),
                      0.004
                  )
                ORDER BY ST_Distance(centroid, ST_SetSRID(ST_Point(%s, %s), 4326))
                LIMIT 1
                """,
                (lng, lat, lng, lat),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        release_conn(conn)


@app.get("/api/parcel/near")
async def get_parcel_near(lat: float, lng: float) -> dict[str, Any]:
    """
    Nearest-parcel lookup by lat/lng coordinate.
    Used by address search to reliably find the parcel footprint at a geocoded point.
    Tries DCAD, TAD, Collin, then Denton using polygon containment + centroid proximity.
    Returns a GeoJSON Feature in the same shape as /api/parcel/{county}/{account_num}.
    """
    dcad_account = _find_dcad_near(lat, lng)
    if dcad_account:
        row, exempt_set = _fetch_dcad_parcel_by_account(dcad_account)
        if row is not None:
            prop_type = classify_parcel(row, exempt_set)
            feature = build_feature(row, prop_type, False, None)
            feature["properties"]["source_county"] = "dcad"
            return feature

    tad_account = _find_tad_near(lat, lng)
    if tad_account:
        row = _fetch_tad_parcel_by_account(tad_account)
        if row is not None:
            prop_type = _classify_tad(row)
            feature = build_feature(row, prop_type, False, None)
            feature["properties"]["source_county"] = "tad"
            return feature

    collin_account = _find_collin_near(lat, lng)
    if collin_account:
        row = _fetch_collin_parcel_by_account(collin_account)
        if row is not None:
            prop_type = _classify_collin(row)
            feature = build_feature(row, prop_type, False, None)
            feature["properties"]["source_county"] = "collin"
            return feature

    denton_account = _find_denton_near(lat, lng)
    if denton_account:
        row = _fetch_denton_parcel_by_account(denton_account)
        if row is not None:
            prop_type = _classify_denton(row)
            feature = build_feature(row, prop_type, False, None)
            feature["properties"]["source_county"] = "denton"
            return feature

    raise HTTPException(status_code=404, detail="No parcel found near this point")


@app.get("/api/parcel/{county}/{account_num}")
async def get_parcel_detail(county: str, account_num: str) -> dict[str, Any]:
    """
    Single-parcel detail endpoint used by PMTiles click popups.

    Called by: frontend tile-layer click flow after queryTileFeaturesDebug returns
    account_num + source_county for the clicked parcel.

    Why it exists: PMTiles stores only minimal properties for fast rendering. This
    endpoint fetches the full parcel row from the live database and returns one
    GeoJSON feature in the same shape produced by /api/analyze.
    """
    county_key = county.strip().lower()
    if county_key not in {"dcad", "tad", "collin", "denton"}:
        raise HTTPException(status_code=400, detail="county must be 'dcad', 'tad', 'collin', or 'denton'")

    if county_key == "dcad":
        row, exempt_set = _fetch_dcad_parcel_by_account(account_num)
        if row is None:
            raise HTTPException(status_code=404, detail="Parcel not found")
        prop_type = classify_parcel(row, exempt_set)
        feature = build_feature(row, prop_type, False, None)
        feature["properties"]["source_county"] = "dcad"
        return feature

    if county_key == "tad":
        row = _fetch_tad_parcel_by_account(account_num)
        if row is None:
            raise HTTPException(status_code=404, detail="Parcel not found")
        prop_type = _classify_tad(row)
        feature = build_feature(row, prop_type, False, None)
        feature["properties"]["source_county"] = "tad"
        return feature

    if county_key == "collin":
        row = _fetch_collin_parcel_by_account(account_num)
        if row is None:
            raise HTTPException(status_code=404, detail="Parcel not found")
        prop_type = _classify_collin(row)
        feature = build_feature(row, prop_type, False, None)
        feature["properties"]["source_county"] = "collin"
        return feature

    row = _fetch_denton_parcel_by_account(account_num)
    if row is None:
        raise HTTPException(status_code=404, detail="Parcel not found")
    prop_type = _classify_denton(row)
    feature = build_feature(row, prop_type, False, None)
    feature["properties"]["source_county"] = "denton"
    return feature


@app.post("/api/analyze")
async def analyze(request: AnalyzeRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    polygon = request.polygon
    include_redfin = bool(request.include_redfin)
    include_sold = bool(request.include_sold)
    user_id = int(user["id"])
    if len(polygon) < 3:
        raise HTTPException(status_code=400, detail="Polygon must have at least 3 points")

    min_lat, min_lng, max_lat, max_lng = polygon_bbox(polygon)

    redfin_data: dict[str, dict] = {}

    dcad_result = None
    tad_result = None
    collin_result = None
    denton_result = None
    redfin_fetch_ok = False
    sold_points: list[dict[str, Any]] = []
    failed_sources: list[str] = []

    tasks = [
        asyncio.to_thread(query_parcels, polygon),
        asyncio.to_thread(query_tad_parcels, polygon),
        asyncio.to_thread(query_collin_parcels, polygon),
        asyncio.to_thread(query_denton_parcels, polygon),
    ]

    sold_task_idx: int | None = None
    active_task_idx: int | None = None

    if include_sold:
        sold_task_idx = len(tasks)
        tasks.append(asyncio.to_thread(query_sold_parcels, polygon))

    if include_redfin:
        active_task_idx = len(tasks)
        tasks.append(asyncio.to_thread(query_active_listings, polygon))

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    if isinstance(raw_results[0], Exception):
        failed_sources.append("DCAD")
    else:
        dcad_result = raw_results[0]
    if isinstance(raw_results[1], Exception):
        failed_sources.append("TAD")
    else:
        tad_result = raw_results[1]
    if isinstance(raw_results[2], Exception):
        failed_sources.append("Collin")
    else:
        collin_result = raw_results[2]
    if isinstance(raw_results[3], Exception):
        failed_sources.append("Denton")
    else:
        denton_result = raw_results[3]

    if sold_task_idx is not None:
        sold_result = raw_results[sold_task_idx]
        if isinstance(sold_result, Exception):
            logger.warning("Sold points query failed; continuing without sold overlay: %s", sold_result)
            sold_points = []
        else:
            sold_points = sold_result or []

    if active_task_idx is not None:
        active_result = raw_results[active_task_idx]
        if isinstance(active_result, Exception):
            logger.warning("Active listings query failed; continuing without active overlay: %s", active_result)
            redfin_data = {}
        else:
            redfin_data = active_result or {}
            redfin_fetch_ok = bool(redfin_data)

    # Never return silent partial county coverage; fail loudly instead.
    if failed_sources:
        raise HTTPException(
            status_code=502,
            detail=f"County query failed for: {', '.join(failed_sources)}. No partial results returned.",
        )

    # Merge rows from all counties, deduplicating by (account_num, county).
    all_rows: list[dict[str, Any]] = []
    exempt_set: set[str] = set()
    if dcad_result:
        all_rows.extend(dcad_result.parcels)
        exempt_set.update(dcad_result.exempt_accounts)
    if tad_result:
        all_rows.extend(tad_result.parcels)
    if collin_result:
        all_rows.extend(collin_result.parcels)
    if denton_result:
        all_rows.extend(denton_result.parcels)

    if not all_rows:
        _evict_stale_jobs()
        empty_job_id = str(uuid.uuid4())
        _job_store[empty_job_id] = {
            "rows": [],
            "redfin_data": {},
            "sold_points": sold_points,
            "polygon": polygon,
            "user_id": user_id,
            "created_at": time.monotonic(),
            "last_accessed": time.monotonic(),
        }
        try:
            await asyncio.to_thread(_persist_cached_job_sync, empty_job_id, user_id, [], sold_points, polygon)
        except Exception as exc:
            logger.warning("Failed to persist job to cache (non-fatal): %s", exc)
        return {
            "type": "FeatureCollection",
            "features": [],
            "counts": {"active": 0, "off_market": 0, "multifamily": 0, "vacant": 0, "commercial": 0, "exempt": 0, "total": 0},
            "sold_points": sold_points,
            "job_id": empty_job_id,
            "redfin_requested": include_redfin,
            "redfin_ok": False,
            "redfin_skipped": False,
            "source_status": {
                "dcad_ok": dcad_result is not None,
                "tad_ok": tad_result is not None,
                "collin_ok": collin_result is not None,
                "denton_ok": denton_result is not None,
            },
        }

    redfin_skipped = False
    rows = all_rows
    features: list[dict[str, Any]] = []
    counts = {
        "active": 0,
        "off_market": 0,
        "multifamily": 0,
        "vacant": 0,
        "commercial": 0,
        "exempt": 0,
        "total": len(rows),
    }

    for row in rows:
        parcel_key = str(row.get("parcel_key", "") or "")
        account_num = str(row.get("account_num", "") or "")
        # TAD stores parcel_key as "account:000"; Collin as "account:R-XXXX-...".
        # Both start with account_num + ":" — treat those as a direct match too.
        direct_match = (not parcel_key) or (parcel_key == account_num) or parcel_key.startswith(account_num + ":")
        addr_key = normalize_addr_key(str(row.get("property_address", "") or ""))
        on_redfin = addr_key in redfin_data and direct_match
        redfin_listing = redfin_data.get(addr_key) if on_redfin else None
        # TAD rows carry division_cd="TAD" — use TAD classifier; DCAD rows use existing classifier.
        if row.get("division_cd") == "TAD":
            prop_type = _classify_tad(row)
        elif row.get("division_cd") == "COLLIN":
            prop_type = _classify_collin(row)
        elif row.get("division_cd") == "DENTON":
            prop_type = _classify_denton(row)
        else:
            prop_type = classify_parcel(row, exempt_set)

        if on_redfin:
            counts["active"] += 1
        elif prop_type == "multifamily":
            counts["multifamily"] += 1
        elif prop_type == "vacant":
            counts["vacant"] += 1
        elif prop_type == "commercial":
            counts["commercial"] += 1
        elif prop_type == "exempt":
            counts["exempt"] += 1
        else:
            counts["off_market"] += 1

        try:
            feature = build_feature(row, prop_type, on_redfin, redfin_listing)
            division_cd = str(row.get("division_cd", "") or "").upper()
            if division_cd == "TAD":
                feature["properties"]["source_county"] = "tad"
            elif division_cd == "COLLIN":
                feature["properties"]["source_county"] = "collin"
            elif division_cd == "DENTON":
                feature["properties"]["source_county"] = "denton"
            else:
                feature["properties"]["source_county"] = "dcad"
            features.append(feature)
        except ValueError:
            continue

    _evict_stale_jobs()
    job_id = str(uuid.uuid4())
    county_coverage = _counties_from_rows(rows)
    _job_store[job_id] = {
        "rows": rows,
        "redfin_data": redfin_data,
        "sold_points": sold_points,
        "polygon": polygon,
        "user_id": user_id,
        "created_at": time.monotonic(),
        "last_accessed": time.monotonic(),
    }
    try:
        await asyncio.to_thread(_persist_cached_job_sync, job_id, user_id, rows, sold_points, polygon)
    except Exception as exc:
        logger.warning("Failed to persist job to cache (non-fatal): %s", exc)
    asyncio.create_task(
        _persist_session_async(
            job_id,
            polygon,
            len(rows),
            county_coverage,
            user_id,
        )
    )
    return {
        "type": "FeatureCollection",
        "features": features,
        "counts": counts,
        "sold_points": sold_points,
        "job_id": job_id,
        "redfin_requested": include_redfin,
        "redfin_ok": redfin_fetch_ok,
        "redfin_skipped": redfin_skipped,
        "source_status": {
            "dcad_ok": dcad_result is not None,
            "tad_ok": tad_result is not None,
            "collin_ok": collin_result is not None,
            "denton_ok": denton_result is not None,
        },
    }


@app.post("/api/merge-jobs")
async def merge_jobs(request: MergeJobsRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Merge rows from multiple tile job_ids into a single exportable job."""
    merged_rows: list[dict[str, Any]] = []
    merged_redfin: dict[str, Any] = {}
    merged_sold_points: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_sold_keys: set[str] = set()
    user_id = int(user["id"])
    for job_id in request.job_ids:
        job = _get_job(job_id, user_id)
        if job is None:
            continue
        for row in job.get("rows", []):
            key = str(row.get("parcel_key") or row.get("account_num") or "")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            merged_rows.append(row)
        merged_redfin.update(job.get("redfin_data", {}))
        for point in job.get("sold_points", []) or []:
            sold_key = str(
                point.get("listing_url")
                or f"{point.get('lat')},{point.get('lng')},{point.get('sold_date') or ''}"
            )
            if sold_key in seen_sold_keys:
                continue
            seen_sold_keys.add(sold_key)
            merged_sold_points.append(point)

    if not merged_rows:
        raise HTTPException(status_code=404, detail="No valid tile jobs found to merge")

    _evict_stale_jobs()
    new_job_id = str(uuid.uuid4())
    _job_store[new_job_id] = {
        "rows": merged_rows,
        "redfin_data": merged_redfin,
        "sold_points": merged_sold_points,
        "polygon": [],
        "user_id": user_id,
        "created_at": time.monotonic(),
        "last_accessed": time.monotonic(),
    }
    try:
        await asyncio.to_thread(_persist_cached_job_sync, new_job_id, user_id, merged_rows, merged_sold_points, [])
    except Exception as exc:
        logger.warning("Failed to persist job to cache (non-fatal): %s", exc)
    return {"job_id": new_job_id}


@app.post("/api/job/{job_id}/verification")
async def save_verification(job_id: str, request: VerificationRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    job = _get_job(job_id, int(user["id"]))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    rows = job.get("rows", [])
    verifications = request.verifications or {}
    potential_targets = request.potential_targets or {}
    polygon = job.get("polygon", [])

    def _normalize_verification(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw == "yes":
            return "Yes"
        if raw == "no":
            return "No"
        return ""

    def _normalize_target(value: Any) -> str:
        raw = str(value or "").strip().lower()
        return "Yes" if raw in {"1", "true", "yes", "y"} else ""

    try:
        # Ensure parent session row exists before writing child tag rows.
        _persist_session_sync(
            job_id,
            polygon,
            len(rows),
            _counties_from_rows(rows),
            int(user["id"]),
            None,
        )

        upsert_rows: dict[tuple[str, str, str], tuple[str, str, str, str, str]] = {}
        delete_rows: set[tuple[str, str, str]] = set()

        for row in rows:
            account_num = str(row.get("account_num", "") or "").strip()
            if not account_num:
                continue
            county = _row_county(row)

            verification_value = _normalize_verification(verifications.get(account_num, ""))
            key_ver = (account_num, county, "verification")
            if verification_value:
                upsert_rows[key_ver] = (job_id, account_num, county, "verification", verification_value)
            else:
                delete_rows.add(key_ver)

            target_value = _normalize_target(potential_targets.get(account_num, ""))
            key_target = (account_num, county, "target")
            if target_value:
                upsert_rows[key_target] = (job_id, account_num, county, "target", target_value)
            else:
                delete_rows.add(key_target)

        conn = get_session_conn()
        try:
            with conn.cursor() as cur:
                if upsert_rows:
                    execute_values(
                        cur,
                        """
                        INSERT INTO session_tags (session_id, account_num, county, tag_type, tag_value, user_id)
                        VALUES %s
                        ON CONFLICT (session_id, account_num, county, tag_type)
                        DO UPDATE SET tag_value = EXCLUDED.tag_value, updated_at = now()
                        """,
                        [(*vals, int(user["id"])) for vals in upsert_rows.values()],
                        template="(%s,%s,%s,%s,%s,%s)",
                        page_size=500,
                    )
                if delete_rows:
                    cur.executemany(
                        """
                        DELETE FROM session_tags
                        WHERE session_id = %s
                          AND account_num = %s
                          AND county = %s
                          AND tag_type = %s
                                                    AND user_id = %s
                        """,
                                                [(job_id, account_num, county, tag_type, int(user["id"])) for account_num, county, tag_type in delete_rows],
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            release_session_conn(conn)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to persist verification tags: {exc}") from exc

    updates = 0
    for row in rows:
        account_num = str(row.get("account_num", "") or "").strip()
        normalized = _normalize_verification(verifications.get(account_num, ""))
        if row.get("verified_vacant") != normalized:
            row["verified_vacant"] = normalized
            updates += 1

        potential_value = _normalize_target(potential_targets.get(account_num, ""))
        if row.get("potential_target") != potential_value:
            row["potential_target"] = potential_value
            updates += 1

    return {"ok": True, "updated": updates}


@app.post("/api/tags/set")
async def set_tag(payload: dict[str, Any] = Body(...), user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or "").strip()
    account_num = str(payload.get("account_num") or "").strip()
    field = str(payload.get("field") or "").strip()
    value = payload.get("value")

    if not job_id or not account_num or field not in {"verified_vacant", "potential_target"}:
        raise HTTPException(status_code=400, detail="Invalid payload")

    job = _get_job(job_id, int(user["id"]))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    rows = job.get("rows", [])
    polygon = job.get("polygon", [])
    row_ref: dict[str, Any] | None = None
    county = ""
    for row in rows:
        if str(row.get("account_num", "") or "").strip() == account_num:
            row_ref = row
            county = _row_county(row)
            break

    if not county:
        raise HTTPException(status_code=400, detail="Account not found in job")

    raw = "" if value is None else str(value).strip().lower()
    if field == "verified_vacant":
        tag_type = "verification"
        if raw == "":
            tag_value = ""
        elif raw in {"yes", "no"}:
            tag_value = "Yes" if raw == "yes" else "No"
        else:
            raise HTTPException(status_code=400, detail="Invalid verification value")
    else:
        tag_type = "target"
        if raw in {"", "0", "false", "no", "n"}:
            tag_value = ""
        elif raw in {"1", "true", "yes", "y"}:
            tag_value = "Yes"
        else:
            raise HTTPException(status_code=400, detail="Invalid target value")

    try:
        _persist_session_sync(job_id, polygon, len(rows), _counties_from_rows(rows), int(user["id"]), None)
        conn = get_session_conn()
        try:
            with conn.cursor() as cur:
                if tag_value:
                    cur.execute(
                        """
                        INSERT INTO session_tags (session_id, account_num, county, tag_type, tag_value, user_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (session_id, account_num, county, tag_type)
                        DO UPDATE SET tag_value = EXCLUDED.tag_value, updated_at = now()
                        """,
                        (job_id, account_num, county, tag_type, tag_value, int(user["id"])),
                    )
                else:
                    cur.execute(
                        """
                        DELETE FROM session_tags
                        WHERE session_id = %s
                          AND account_num = %s
                          AND county = %s
                          AND tag_type = %s
                                                    AND user_id = %s
                        """,
                                                (job_id, account_num, county, tag_type, int(user["id"])),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            release_session_conn(conn)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to persist tag update: {exc}") from exc

    if row_ref is not None:
        if field == "verified_vacant":
            row_ref["verified_vacant"] = tag_value
        else:
            row_ref["potential_target"] = tag_value

    return {"ok": True}


@app.get("/api/download/{job_id}")
async def download(job_id: str, filename: str | None = None, user: dict[str, Any] = Depends(get_current_user)) -> StreamingResponse:
    job = _get_job(job_id, int(user["id"]))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    rows = job.get("rows", [])
    redfin_data: dict[str, dict] = job.get("redfin_data", {})
    sold_points: list[dict[str, Any]] = job.get("sold_points", []) or []
    logger.info("Download job %s: %d parcel rows, %d sold points", job_id, len(rows), len(sold_points))

    def _deg_dist(lat1, lng1, lat2, lng2):
        return ((lat1 - lat2) ** 2 + (lng1 - lng2) ** 2) ** 0.5

    _COMP_THRESHOLD = 0.00135  # ~150 m at Texas latitude
    comp_by_parcel_key: dict[str, dict] = {}
    for _pr in rows:
        p_lat = _safe_float(_pr.get("lat"))
        p_lng = _safe_float(_pr.get("lng"))
        if p_lat is None or p_lng is None:
            continue
        best_comp: dict[str, Any] | None = None
        best_dist = _COMP_THRESHOLD
        for _sp in sold_points:
            s_lat = _safe_float(_sp.get("lat"))
            s_lng = _safe_float(_sp.get("lng"))
            if s_lat is None or s_lng is None:
                continue
            dist = _deg_dist(p_lat, p_lng, s_lat, s_lng)
            if dist < best_dist:
                best_dist = dist
                best_comp = _sp
        if best_comp is not None:
            comp_by_parcel_key[str(_pr.get("account_num", "") or "")] = best_comp

    download_name = _normalize_csv_filename(filename)

    def generate_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "Property Address",
                "MLS Status",
                "Owner Name",
                "Owner Mailing Address",
                "Owner City",
                "Owner State",
                "Owner Zip",
                "Land Value",
                "Improvement Value",
                "Total Value",
                "Redfin List Price",
                "Land % of Total",
                "Year Built",
                "Living Area (sq ft)",
                "Total Structure Area (sq ft)",
                "State Code",
                "Zoning",
                "Lot Size (sq ft)",
                "Lot Size (acres)",
                "Frontage (ft)",
                "Depth (ft)",
                "Est Frontage (ft)",
                "Est Depth (ft)",
                "School District",
                "Neighborhood Code",
                "Subdivision",
                "Legal Description",
                "Latitude",
                "Longitude",
                "Google Maps Link",
                "Verified Vacant",
                "Potential Target",
                "HOA",
                "HOA URL",
                "Estimated Lot Size (sq ft)",
                "Estimated Lot Size (acres)",
                "Tax Agent Name",
                "Tax Agent ID",
                "Tax Agent Auth Protest",
                "Tax Agent Auth Resolve",
                "Tax Agent Mailings",
                "Permit Count",
                "Latest Permit Date",
                "Latest Permit Type",
                "Latest Permit Value",
                "Protest Case Count",
                "Latest Protest Year",
                "Latest Protest Status",
                "Latest Protest Final Market Value",
                "Protest Active",
                "Ag Type",
                "Ag Acres",
                "Ag Use Value",
                "Ag Market Value",
                "Deed Number",
                "Deed Type",
                "Deed Date",
                "Land Type Code",
                "Land Type Name",
                "Property Use Code",
                "Property Use Name",
                "Class Code",
                "Entity Codes",
                "Commercial Flag",
                "Pool Flag",
                "Beds",
                "Baths",
                "Stories",
                "Units",
                "Current Market Value",
                "Current Assessed Value",
                "Current Ag Use Value",
                "Current Ag Market Value",
                "Current Ag Loss Value",
                "Certified Total Value",
                "Denton - Exemptions",
                "Denton - Homestead (HS)",
                "Denton - School District",
                "Denton - Entity Codes",
                "Denton - Deed Number",
                "Denton - Deed Date",
                "Denton - Subdivision",
                "Comp Sold Price",
                "Comp Sold Date",
                "Comp $/sqft",
                "Comp Year Built",
                "Comp Living Area (sqft)",
                "Comp Lot Size (sqft)",
                "Comp Beds",
                "Comp Baths",
                "Comp Days on Market",
                "Comp Listing URL",
            ]
        )
        buffer.seek(0)
        yield buffer.getvalue()
        buffer.truncate(0)
        buffer.seek(0)

        # Put sparse rows (missing property_address) at the bottom so analysts
        # see usable address records first.
        sorted_rows = sorted(
            rows,
            key=lambda r: (
                str(r.get("property_address", "") or "").strip() == "",
                str(r.get("property_address", "") or "").strip(),
                str(r.get("owner_name", "") or "").strip(),
            ),
        )
        for row in sorted_rows:
            parcel_key = str(row.get("parcel_key", "") or "")
            account_num = str(row.get("account_num", "") or "")
            direct_match = (not parcel_key) or (parcel_key == account_num) or parcel_key.startswith(account_num + ":")
            addr_key = normalize_addr_key(str(row.get("property_address", "") or ""))
            on_redfin = addr_key in redfin_data and direct_match
            redfin_listing = redfin_data.get(addr_key) if on_redfin else None

            land_val = row.get("land_val")
            impr_val = row.get("impr_val")
            tot_val = row.get("tot_val")
            land_pct = row.get("land_pct")
            area_size = row.get("area_size")
            area_estimated = bool(row.get("area_estimated"))
            _area_sf = round(_safe_float(area_size), 0) if _safe_float(area_size) is not None else ""
            _area_ac = round(area_size / 43560, 3) if _safe_float(area_size) is not None else ""
            lot_sqft_csv = "" if area_estimated else _area_sf
            lot_acres_csv = "" if area_estimated else _area_ac
            est_lot_sqft_csv = _area_sf if area_estimated else ""
            est_lot_acres_csv = _area_ac if area_estimated else ""
            dims_estimated = bool(row.get("dims_estimated"))
            front_dim_val = _safe_float(row.get("front_dim"))
            depth_dim_val = _safe_float(row.get("depth_dim"))
            frontage_csv = int(front_dim_val) if (not dims_estimated and front_dim_val not in (None, 0.0)) else ""
            depth_csv = int(depth_dim_val) if (not dims_estimated and depth_dim_val not in (None, 0.0)) else ""
            est_frontage_csv = int(front_dim_val) if (dims_estimated and front_dim_val not in (None, 0.0)) else ""
            est_depth_csv = int(depth_dim_val) if (dims_estimated and depth_dim_val not in (None, 0.0)) else ""
            yr_built = row.get("yr_built")
            living_area = row.get("tot_living_area")
            main_area = row.get("tot_main_sf")
            legal_desc = " ".join(
                [
                    str(row.get("legal1", "") or "").strip(),
                    str(row.get("legal2", "") or "").strip(),
                    str(row.get("legal3", "") or "").strip(),
                    str(row.get("legal4", "") or "").strip(),
                    str(row.get("legal5", "") or "").strip(),
                ]
            ).strip()

            display_address = (
                str(row.get("property_address", "") or "").strip()
                or str(row.get("legal1", "") or "").strip()
                or str(row.get("parcel_key", "") or "").strip()
            )

            comp = comp_by_parcel_key.get(str(row.get("account_num", "") or ""))

            writer.writerow(
                [
                    display_address,
                    "Active" if on_redfin else "Off Market",
                    row.get("owner_name", ""),
                    row.get("owner_address", ""),
                    row.get("owner_city", ""),
                    row.get("owner_state", ""),
                    row.get("owner_zip", ""),
                    round(_safe_float(land_val), 0) if _safe_float(land_val) is not None else "",
                    round(_safe_float(impr_val), 0) if _safe_float(impr_val) is not None else "",
                    round(_safe_float(tot_val), 0) if _safe_float(tot_val) is not None else "",
                    redfin_listing["price"] if redfin_listing and redfin_listing.get("price") else "",
                    round(_safe_float(land_pct), 1) if _safe_float(land_pct) is not None else "",
                    int(yr_built) if _safe_float(yr_built) not in (None, 0.0) else "",
                    int(_safe_float(living_area)) if _safe_float(living_area) not in (None, 0.0) else "",
                    int(_safe_float(main_area)) if _safe_float(main_area) not in (None, 0.0) else "",
                    row.get("state_code", "") or row.get("sptd_code", ""),
                    row.get("zoning", "") or "",
                    lot_sqft_csv,
                    lot_acres_csv,
                    frontage_csv,
                    depth_csv,
                    est_frontage_csv,
                    est_depth_csv,
                    row.get("isd_desc", "") or "",
                    row.get("nbhd_cd", "") or "",
                    row.get("legal1", "") or "",
                    legal_desc,
                    row.get("lat", "") or "",
                    row.get("lng", "") or "",
                    _google_maps_link(row),
                    row.get("verified_vacant", "") or "",
                    row.get("potential_target", "") or "",
                    (
                        row.get("hoa_name", "")
                        or ("N/A (Tarrant HOA not loaded)" if row.get("division_cd") == "TAD" else "")
                    ),
                    row.get("hoa_url", "") or "",
                    est_lot_sqft_csv,
                    est_lot_acres_csv,
                    row.get("tax_agent_name", "") or "",
                    row.get("tax_agent_id", "") or "",
                    row.get("tax_agent_auth_protest", "") or "",
                    row.get("tax_agent_auth_resolve", "") or "",
                    row.get("tax_agent_mailings", "") or "",
                    int(_safe_float(row.get("permit_count"))) if _safe_float(row.get("permit_count")) not in (None, 0.0) else "",
                    row.get("latest_permit_date", "") or "",
                    row.get("latest_permit_type", "") or "",
                    round(_safe_float(row.get("latest_permit_value")), 0) if _safe_float(row.get("latest_permit_value")) is not None else "",
                    int(_safe_float(row.get("protest_case_count"))) if _safe_float(row.get("protest_case_count")) not in (None, 0.0) else "",
                    int(_safe_float(row.get("latest_protest_year"))) if _safe_float(row.get("latest_protest_year")) not in (None, 0.0) else "",
                    row.get("latest_protest_status", "") or "",
                    round(_safe_float(row.get("latest_protest_final_market")), 0) if _safe_float(row.get("latest_protest_final_market")) is not None else "",
                    row.get("protest_active", "") or "",
                    row.get("ag_type", "") or "",
                    round(_safe_float(row.get("ag_acres")), 4) if _safe_float(row.get("ag_acres")) is not None else "",
                    round(_safe_float(row.get("ag_value")), 0) if _safe_float(row.get("ag_value")) is not None else "",
                    round(_safe_float(row.get("ag_market_value")), 0) if _safe_float(row.get("ag_market_value")) is not None else "",
                    row.get("deed_num", "") or "",
                    row.get("deed_type", "") or "",
                    row.get("deed_date", "") or "",
                    row.get("land_type_code", "") or "",
                    row.get("land_type_name", "") or "",
                    row.get("prop_use_code", "") or "",
                    row.get("prop_use_name", "") or "",
                    row.get("class_cd", "") or "",
                    row.get("entity_codes", "") or "",
                    row.get("commercial_flag", "") or "",
                    row.get("pool_flag", "") or "",
                    round(_safe_float(row.get("beds")), 1) if _safe_float(row.get("beds")) is not None else "",
                    round(_safe_float(row.get("baths")), 1) if _safe_float(row.get("baths")) is not None else "",
                    round(_safe_float(row.get("stories")), 1) if _safe_float(row.get("stories")) is not None else "",
                    round(_safe_float(row.get("units")), 0) if _safe_float(row.get("units")) is not None else "",
                    round(_safe_float(row.get("curr_market_value")), 0) if _safe_float(row.get("curr_market_value")) is not None else "",
                    round(_safe_float(row.get("curr_assessed_value")), 0) if _safe_float(row.get("curr_assessed_value")) is not None else "",
                    round(_safe_float(row.get("curr_ag_use_value")), 0) if _safe_float(row.get("curr_ag_use_value")) is not None else "",
                    round(_safe_float(row.get("curr_ag_market_value")), 0) if _safe_float(row.get("curr_ag_market_value")) is not None else "",
                    round(_safe_float(row.get("curr_ag_loss_value")), 0) if _safe_float(row.get("curr_ag_loss_value")) is not None else "",
                    round(_safe_float(row.get("cert_total_value")), 0) if _safe_float(row.get("cert_total_value")) is not None else "",
                    row.get("exemptions", "") or "",
                    row.get("exempt_homestead", "") or "",
                    row.get("isd_desc", "") or "",
                    row.get("entity_codes", "") or "",
                    row.get("deed_number", "") or "",
                    row.get("deed_date", "") or "",
                    row.get("subdivision", "") or "",
                    round(_safe_float(comp.get("sold_price")), 0) if comp and _safe_float(comp.get("sold_price")) is not None else "",
                    (comp.get("sold_date", "") or "") if comp else "",
                    round(_safe_float(comp.get("price_per_sqft")), 0) if comp and _safe_float(comp.get("price_per_sqft")) is not None else "",
                    int(_safe_float(comp.get("yr_built"))) if comp and _safe_float(comp.get("yr_built")) not in (None, 0.0) else "",
                    int(_safe_float(comp.get("sqft"))) if comp and _safe_float(comp.get("sqft")) not in (None, 0.0) else "",
                    round(_safe_float(comp.get("lot_sqft")), 0) if comp and _safe_float(comp.get("lot_sqft")) is not None else "",
                    int(_safe_float(comp.get("beds"))) if comp and _safe_float(comp.get("beds")) not in (None, 0.0) else "",
                    int(_safe_float(comp.get("baths"))) if comp and _safe_float(comp.get("baths")) not in (None, 0.0) else "",
                    int(_safe_float(comp.get("dom"))) if comp and _safe_float(comp.get("dom")) not in (None, 0.0) else "",
                    (comp.get("listing_url", "") or "") if comp else "",
                ]
            )
            buffer.seek(0)
            yield buffer.getvalue()
            buffer.truncate(0)
            buffer.seek(0)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


def _cleanup_expired_sessions_sync() -> int:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM analysis_sessions WHERE expires_at < now()")
            deleted = cur.rowcount or 0
            cur.execute("DELETE FROM cached_jobs WHERE expires_at < now()")
            deleted += cur.rowcount or 0
        conn.commit()
        return int(deleted)
    finally:
        release_session_conn(conn)


async def _cleanup_expired_sessions_loop() -> None:
    while True:
        try:
            deleted = await asyncio.to_thread(_cleanup_expired_sessions_sync)
            if deleted:
                print(f"[session] cleaned expired sessions: {deleted}")
        except Exception as exc:
            print(f"[session] cleanup failed: {exc}")
        await asyncio.sleep(24 * 60 * 60)


@app.on_event("startup")
async def _startup_session_storage() -> None:
    ensure_auth_settings()
    await asyncio.to_thread(_ensure_session_schema)
    await asyncio.to_thread(seed_bootstrap_users)
    await asyncio.to_thread(_finalize_user_scoping)
    await asyncio.to_thread(ensure_required_roles_exist)
    await asyncio.to_thread(log_redfin_sold_row_count)
    app.state.session_cleanup_task = asyncio.create_task(_cleanup_expired_sessions_loop())


@app.on_event("shutdown")
async def _shutdown_session_storage() -> None:
    task = getattr(app.state, "session_cleanup_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(Exception):
            await task


@app.get("/api/session/{session_id}")
async def get_session(session_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    job = _get_job(session_id, int(user["id"]))
    if job is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": session_id,
        "job_id": session_id,
        "parcel_count": len(job.get("rows", [])),
        "restored": True,
    }


@app.get("/api/areas")
async def list_saved_areas(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT area_id, name, polygon, created_at, updated_at
                FROM saved_areas
                WHERE user_id = %s
                ORDER BY created_at DESC
                """
                ,
                (int(user["id"]),)
            )
            areas = [
                {
                    "area_id": row[0],
                    "name": row[1],
                    "polygon": row[2],
                    "created_at": row[3].isoformat() if row[3] else None,
                    "updated_at": row[4].isoformat() if row[4] else None,
                }
                for row in cur.fetchall()
            ]
    finally:
        release_session_conn(conn)
    return {"areas": areas}


@app.post("/api/areas")
async def create_saved_area(request: SavedAreaCreateRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    name = str(request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Area name is required")
    if len(request.polygon) < 3:
        raise HTTPException(status_code=400, detail="Polygon must have at least 3 points")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO saved_areas (name, polygon, user_id)
                VALUES (%s, %s, %s)
                RETURNING area_id, created_at, updated_at
                """,
                (name, Json(request.polygon), int(user["id"])),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    return {
        "area_id": row[0],
        "name": name,
        "polygon": request.polygon,
        "created_at": row[1].isoformat() if row[1] else None,
        "updated_at": row[2].isoformat() if row[2] else None,
    }


@app.put("/api/areas/{area_id}")
async def rename_saved_area(area_id: str, request: SavedAreaRenameRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    name = str(request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Area name is required")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE saved_areas
                SET name = %s, updated_at = now()
                WHERE area_id = %s AND user_id = %s
                RETURNING area_id, name, updated_at
                """,
                (name, area_id, int(user["id"])),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if row is None:
        raise HTTPException(status_code=404, detail="Saved area not found")
    return {
        "area_id": row[0],
        "name": row[1],
        "updated_at": row[2].isoformat() if row[2] else None,
    }


@app.delete("/api/areas/{area_id}")
async def delete_saved_area(area_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM saved_areas WHERE area_id = %s AND user_id = %s", (area_id, int(user["id"])))
            deleted = cur.rowcount or 0
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if not deleted:
        raise HTTPException(status_code=404, detail="Saved area not found")
    return {"ok": True, "deleted": int(deleted)}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")