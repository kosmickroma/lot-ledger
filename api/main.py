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
import os
import re
import secrets
import string
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

from fastapi import Body, Depends, FastAPI, HTTPException, Path as FastAPIPath, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg2.errors import UniqueViolation
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
    get_user_by_username_or_email,
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
from api.propelio.routes import router as propelio_router
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
_SHARE_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


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


def _is_idempotent_schema_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "already exists" in msg or "duplicate" in msg


def _run_schema_steps(cur: Any, steps: list[tuple[str, str]]) -> None:
    # Each step runs inside its own SAVEPOINT so that an idempotent-skip
    # (already exists / duplicate) does not poison the outer transaction.
    # Without this, the next step after a skipped one fails with
    # "current transaction is aborted, commands ignored until end of
    # transaction block" — same pattern already used in _finalize_user_scoping.
    for i, (step_id, sql) in enumerate(steps):
        sp_name = f"step_{i}"
        cur.execute(f"SAVEPOINT {sp_name}")
        try:
            cur.execute(sql)
        except Exception as exc:
            cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
            if _is_idempotent_schema_error(exc):
                print(f"[session-schema] step skipped (already applied): {step_id}")
                continue
            print(f"[session-schema] step failed: {step_id} ({exc})")
            raise
        cur.execute(f"RELEASE SAVEPOINT {sp_name}")


def _generate_share_id() -> str:
    return "area_" + "".join(secrets.choice(_SHARE_ID_ALPHABET) for _ in range(10))


def _backfill_saved_area_share_ids(cur: Any) -> None:
    cur.execute("SELECT area_id FROM saved_areas WHERE share_id IS NULL ORDER BY created_at ASC")
    rows = cur.fetchall() or []
    if not rows:
        return

    updated = 0
    for (area_id,) in rows:
        assigned = False
        for _ in range(100):
            candidate = _generate_share_id()
            cur.execute("SELECT 1 FROM saved_areas WHERE share_id = %s LIMIT 1", (candidate,))
            if cur.fetchone() is not None:
                continue
            cur.execute(
                "UPDATE saved_areas SET share_id = %s WHERE area_id = %s AND share_id IS NULL",
                (candidate, area_id),
            )
            if cur.rowcount:
                updated += 1
            assigned = True
            break
        if not assigned:
            raise RuntimeError(f"Unable to backfill share_id for saved area {area_id}")

    print(f"[session-schema] backfilled share_id for {updated} saved areas")


def _ensure_session_schema() -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL,
                    email TEXT,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('developer', 'owner', 'power_user', 'user', 'member')),
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
                    filter_state JSONB,
                    type        TEXT NOT NULL DEFAULT 'area' CHECK (type IN ('area', 'location')),
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
                    name            TEXT,
                    filter_state    JSONB,
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
                    saved_area_id TEXT REFERENCES saved_areas(area_id) ON DELETE SET NULL,
                    created_at   TIMESTAMPTZ DEFAULT now(),
                    expires_at   TIMESTAMPTZ DEFAULT (now() + interval '{_JOB_TTL_SECONDS} seconds'),
                    rows         JSONB NOT NULL,
                    sold_points  JSONB NOT NULL,
                    polygon      JSONB NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS propelio_comps (
                    comp_id BIGSERIAL PRIMARY KEY,
                    comp_address_key TEXT UNIQUE NOT NULL,
                    address TEXT NOT NULL,
                    neighborhood TEXT,
                    lat NUMERIC(10, 7),
                    lng NUMERIC(10, 7),
                    geom GEOMETRY(POINT, 4326),
                    status TEXT,
                    last_status TEXT,
                    price NUMERIC,
                    last_price NUMERIC,
                    sold_date DATE,
                    close_date DATE,
                    dom INTEGER,
                    beds NUMERIC,
                    baths NUMERIC,
                    baths_full INTEGER,
                    baths_half INTEGER,
                    garage INTEGER,
                    sqft NUMERIC,
                    lot_size NUMERIC,
                    year_built INTEGER,
                    mls TEXT,
                    property_type TEXT,
                    property_category TEXT,
                    list_price NUMERIC,
                    remarks TEXT,
                    listing_agent_name TEXT,
                    listing_agent_phone TEXT,
                    listing_agent_email TEXT,
                    listing_office_name TEXT,
                    listing_office_phone TEXT,
                    buyer_agent_name TEXT,
                    buyer_agent_phone TEXT,
                    buyer_agent_email TEXT,
                    buyer_office_name TEXT,
                    buyer_office_phone TEXT,
                    photo_count INTEGER,
                    photos JSONB,
                    parcel_account_num TEXT,
                    parcel_county TEXT,
                    parcel_geom JSONB,
                    parsed_payload JSONB NOT NULL,
                    raw_payload JSONB,
                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    first_seen_source TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_propelio_comps_geom
                    ON propelio_comps USING GIST (geom)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_propelio_comps_status
                    ON propelio_comps (status)
                    WHERE status IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_propelio_comps_sold_date
                    ON propelio_comps (sold_date)
                    WHERE sold_date IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_propelio_comps_close_date
                    ON propelio_comps (close_date)
                    WHERE close_date IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_propelio_comps_last_seen
                    ON propelio_comps (last_seen_at)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_propelio_comps_parcel
                    ON propelio_comps (parcel_county, parcel_account_num)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS comp_ratings (
                    rating_id BIGSERIAL PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
                    comp_id BIGINT NOT NULL REFERENCES propelio_comps(comp_id) ON DELETE CASCADE,
                    rating TEXT NOT NULL CHECK (rating IN ('good', 'bad')),
                    rated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    rated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (workspace_id, comp_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_comp_ratings_workspace
                    ON comp_ratings (workspace_id)
                """
            )
            # One-shot migration: copy legacy propelio_comp_archive.user_rating into
            # comp_ratings so existing ratings remain visible after the canonical store
            # cutover. Idempotent — re-runs are safe and a no-op for rows already
            # migrated by the original Phase 2 backfill script.
            cur.execute(
                """
                INSERT INTO comp_ratings (workspace_id, comp_id, rating, rated_at)
                SELECT pa.saved_area_id,
                       pc.comp_id,
                       pa.user_rating,
                       COALESCE(pa.rating_at, NOW())
                FROM propelio_comp_archive pa
                JOIN propelio_comps pc ON pc.comp_address_key = pa.comp_address_key
                WHERE pa.user_rating IS NOT NULL
                ON CONFLICT (workspace_id, comp_id) DO NOTHING
                """
            )
            if os.environ.get("DEEP_PULL_EXPERIMENT") == "true":
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS propelio_deep_pull_jobs (
                        job_id TEXT PRIMARY KEY,
                        saved_area_id TEXT,
                        started_by_user_id INTEGER REFERENCES users(id),
                        target_address TEXT NOT NULL,
                        target_lat NUMERIC,
                        target_lng NUMERIC,
                        lead_id TEXT,
                        cma_id TEXT,
                        status TEXT NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued', 'running', 'completed', 'stopped',
                                              'error', 'saturated', 'blocked')),
                        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_pass_at TIMESTAMPTZ,
                        next_pass_at TIMESTAMPTZ,
                        passes_completed INTEGER NOT NULL DEFAULT 0,
                        total_unique_comps INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        stop_requested BOOLEAN NOT NULL DEFAULT FALSE
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_deep_pull_jobs_status
                        ON propelio_deep_pull_jobs (status, next_pass_at)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS propelio_deep_pull_experiment (
                        id BIGSERIAL PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES propelio_deep_pull_jobs(job_id) ON DELETE CASCADE,
                        pass_num INTEGER NOT NULL,
                        months INTEGER NOT NULL,
                        range_mi NUMERIC NOT NULL,
                        pass_label TEXT,
                        comp_address_key TEXT NOT NULL,
                        comp_data JSONB NOT NULL,
                        is_first_seen_in_job BOOLEAN NOT NULL,
                        fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (job_id, pass_num, comp_address_key)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_deep_pull_exp_job
                        ON propelio_deep_pull_experiment (job_id, pass_num)
                    """
                )
            _run_schema_steps(
                cur,
                [
                    ("saved_areas_user_id", "ALTER TABLE saved_areas ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)"),
                    ("users_email_drop_not_null", "ALTER TABLE users ALTER COLUMN email DROP NOT NULL"),
                    ("users_role_check_drop", "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check"),
                    (
                        "users_role_check_add",
                        "ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('developer', 'owner', 'power_user', 'user', 'member'))",
                    ),
                    ("users_role_member_alias", "UPDATE users SET role = 'user' WHERE role = 'member'"),
                    ("saved_areas_filter_state", "ALTER TABLE saved_areas ADD COLUMN IF NOT EXISTS filter_state JSONB"),
                    ("saved_areas_type", "ALTER TABLE saved_areas ADD COLUMN IF NOT EXISTS type TEXT"),
                    (
                        "saved_areas_originator_county",
                        "ALTER TABLE saved_areas ADD COLUMN IF NOT EXISTS originator_parcel_county TEXT",
                    ),
                    (
                        "saved_areas_originator_account",
                        "ALTER TABLE saved_areas ADD COLUMN IF NOT EXISTS originator_parcel_account_num TEXT",
                    ),
                    ("saved_areas_share_id", "ALTER TABLE saved_areas ADD COLUMN IF NOT EXISTS share_id VARCHAR(20)"),
                    (
                        "saved_areas_public_toggle",
                        "ALTER TABLE saved_areas ADD COLUMN IF NOT EXISTS is_publicly_viewable BOOLEAN NOT NULL DEFAULT FALSE",
                    ),
                    ("saved_areas_type_backfill", "UPDATE saved_areas SET type = 'area' WHERE type IS NULL OR type = ''"),
                    ("saved_areas_type_default", "ALTER TABLE saved_areas ALTER COLUMN type SET DEFAULT 'area'"),
                    ("saved_areas_type_not_null", "ALTER TABLE saved_areas ALTER COLUMN type SET NOT NULL"),
                    (
                        "saved_areas_type_check",
                        """
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1
                                FROM pg_constraint
                                WHERE conname = 'saved_areas_type_check'
                            ) THEN
                                ALTER TABLE saved_areas
                                ADD CONSTRAINT saved_areas_type_check CHECK (type IN ('area', 'location'));
                            END IF;
                        END$$;
                        """,
                    ),
                    ("analysis_sessions_user_id", "ALTER TABLE analysis_sessions ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)"),
                    ("analysis_sessions_name", "ALTER TABLE analysis_sessions ADD COLUMN IF NOT EXISTS name TEXT"),
                    ("analysis_sessions_filter_state", "ALTER TABLE analysis_sessions ADD COLUMN IF NOT EXISTS filter_state JSONB"),
                    ("analysis_sessions_expires_nullable", "ALTER TABLE analysis_sessions ALTER COLUMN expires_at DROP NOT NULL"),
                    ("session_tags_user_id", "ALTER TABLE session_tags ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)"),
                    ("saved_parcels_user_id", "ALTER TABLE saved_parcels ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)"),
                    ("saved_parcels_area_id", "ALTER TABLE saved_parcels ADD COLUMN IF NOT EXISTS area_id TEXT REFERENCES saved_areas(area_id) ON DELETE CASCADE"),
                    ("idx_saved_parcels_area_id", "CREATE INDEX IF NOT EXISTS idx_saved_parcels_area_id ON saved_parcels (area_id)"),
                    ("uq_saved_parcels_standalone", "CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_parcels_standalone ON saved_parcels (user_id, account_num, county) WHERE area_id IS NULL"),
                    ("uq_saved_parcels_bonded", "CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_parcels_bonded ON saved_parcels (user_id, account_num, county, area_id) WHERE area_id IS NOT NULL"),
                    ("cached_jobs_user_id", "ALTER TABLE cached_jobs ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)"),
                    ("idx_session_tags_session", "CREATE INDEX IF NOT EXISTS idx_session_tags_session ON session_tags (session_id)"),
                    ("idx_session_tags_user", "CREATE INDEX IF NOT EXISTS idx_session_tags_user ON session_tags (user_id)"),
                    ("idx_sessions_expires", "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON analysis_sessions (expires_at)"),
                    ("idx_sessions_saved_area", "CREATE INDEX IF NOT EXISTS idx_sessions_saved_area ON analysis_sessions (saved_area_id)"),
                    ("idx_sessions_user", "CREATE INDEX IF NOT EXISTS idx_sessions_user ON analysis_sessions (user_id)"),
                    (
                        "idx_sessions_named",
                        """
                        CREATE INDEX IF NOT EXISTS idx_sessions_named
                        ON analysis_sessions (user_id, name)
                        WHERE name IS NOT NULL
                        """,
                    ),
                    ("idx_saved_areas_user", "CREATE INDEX IF NOT EXISTS idx_saved_areas_user ON saved_areas (user_id)"),
                    (
                        "uq_saved_areas_share_id",
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_areas_share_id ON saved_areas (share_id) WHERE share_id IS NOT NULL",
                    ),
                    ("idx_saved_parcels_user", "CREATE INDEX IF NOT EXISTS idx_saved_parcels_user ON saved_parcels (user_id)"),
                    ("drop_uq_saved_parcels_user_account", "DROP INDEX IF EXISTS uq_saved_parcels_user_account"),
                    ("idx_cached_jobs_expires", "CREATE INDEX IF NOT EXISTS idx_cached_jobs_expires ON cached_jobs (expires_at)"),
                    ("idx_cached_jobs_user", "CREATE INDEX IF NOT EXISTS idx_cached_jobs_user ON cached_jobs (user_id)"),
                    (
                        "cached_jobs_saved_area_id",
                        "ALTER TABLE cached_jobs ADD COLUMN IF NOT EXISTS saved_area_id TEXT REFERENCES saved_areas(area_id) ON DELETE SET NULL",
                    ),
                    ("idx_cached_jobs_saved_area", "CREATE INDEX IF NOT EXISTS idx_cached_jobs_saved_area ON cached_jobs (saved_area_id)"),
                    (
                        "deep_pull_jobs_net_new_comps",
                        """
                        DO $$
                        BEGIN
                            IF to_regclass('public.propelio_deep_pull_jobs') IS NOT NULL THEN
                                ALTER TABLE propelio_deep_pull_jobs
                                ADD COLUMN IF NOT EXISTS net_new_comps INTEGER NOT NULL DEFAULT 0;
                            END IF;
                        END$$;
                        """,
                    ),
                ],
            )
            _backfill_saved_area_share_ids(cur)
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

            for tbl in ("saved_areas", "analysis_sessions", "session_tags", "saved_parcels"):
                try:
                    cur.execute("SAVEPOINT sp_notnull")
                    cur.execute(f"ALTER TABLE {tbl} ALTER COLUMN user_id SET NOT NULL")
                    cur.execute("RELEASE SAVEPOINT sp_notnull")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_notnull")
        conn.commit()
    finally:
        release_session_conn(conn)


def _persist_cached_job_sync(
    job_id: str,
    user_id: int,
    rows: list[dict[str, Any]],
    sold_points: list[dict[str, Any]],
    polygon: list[list[float]],
    saved_area_id: str | None = None,
) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO cached_jobs (job_id, user_id, saved_area_id, rows, sold_points, polygon, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, now() + interval '{_JOB_TTL_SECONDS} seconds')
                ON CONFLICT (job_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    saved_area_id = COALESCE(EXCLUDED.saved_area_id, cached_jobs.saved_area_id),
                    rows = EXCLUDED.rows,
                    sold_points = EXCLUDED.sold_points,
                    polygon = EXCLUDED.polygon,
                    expires_at = CASE
                        WHEN cached_jobs.expires_at IS NULL THEN NULL
                        ELSE now() + interval '{_JOB_TTL_SECONDS} seconds'
                    END
                """,
                (job_id, int(user_id), saved_area_id, Json(rows), Json(sold_points), Json(polygon)),
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
                                SELECT rows, sold_points, polygon, user_id, saved_area_id
                FROM cached_jobs
                WHERE job_id = %s
                                    AND (expires_at IS NULL OR expires_at > now())
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                f"""
                UPDATE cached_jobs
                SET expires_at = CASE
                    WHEN expires_at IS NULL THEN NULL
                    ELSE now() + interval '{_JOB_TTL_SECONDS} seconds'
                END
                WHERE job_id = %s
                """,
                (job_id,),
            )
        conn.commit()
        rows, sold_points, polygon, user_id, saved_area_id = row
        return {
            "rows": rows if isinstance(rows, list) else [],
            "redfin_data": {},
            "sold_points": sold_points if isinstance(sold_points, list) else [],
            "polygon": polygon if isinstance(polygon, list) else [],
            "user_id": int(user_id or 0),
            "saved_area_id": str(saved_area_id or "").strip() or None,
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


def _saved_area_exists(area_id: str) -> bool:
    normalized = str(area_id or "").strip()
    if not normalized:
        return False
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM saved_areas WHERE area_id = %s LIMIT 1", (normalized,))
            return cur.fetchone() is not None
    finally:
        release_session_conn(conn)


def _job_share_id(job_id: str, fallback_saved_area_id: str | None = None) -> str:
    saved_area_id = str(fallback_saved_area_id or "").strip()
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            if not saved_area_id:
                cur.execute(
                    "SELECT saved_area_id FROM cached_jobs WHERE job_id = %s LIMIT 1",
                    (job_id,),
                )
                row = cur.fetchone()
                saved_area_id = str(row[0] or "").strip() if row else ""
            if not saved_area_id:
                cur.execute(
                    "SELECT saved_area_id FROM analysis_sessions WHERE session_id = %s LIMIT 1",
                    (job_id,),
                )
                row = cur.fetchone()
                saved_area_id = str(row[0] or "").strip() if row else ""
            if not saved_area_id:
                return ""
            cur.execute(
                "SELECT share_id FROM saved_areas WHERE area_id = %s LIMIT 1",
                (saved_area_id,),
            )
            row = cur.fetchone()
            return str(row[0] or "").strip() if row else ""
    finally:
        release_session_conn(conn)


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
                    expires_at = CASE
                        WHEN analysis_sessions.expires_at IS NULL THEN NULL
                        ELSE now() + interval '{_SESSION_RETENTION_DAYS} days'
                    END
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
    saved_area_id: str | None = None,
) -> None:
    try:
        await asyncio.to_thread(
            _persist_session_sync,
            session_id,
            polygon,
            parcel_count,
            county_coverage,
            user_id,
            saved_area_id,
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
                                    AND (expires_at IS NULL OR expires_at > now())
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
                    expires_at = CASE
                        WHEN expires_at IS NULL THEN NULL
                        ELSE now() + interval '{_SESSION_RETENTION_DAYS} days'
                    END
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


def _build_features_from_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Rebuild GeoJSON features from cached raw parcel rows.

    Used when loading a pinned session from cached_jobs — avoids a fresh CAD
    query while still returning the same feature shape as /api/analyze.
    Redfin signal is not available from cache, so on_redfin=False for all rows.
    """
    exempt_set: set[str] = set()
    features: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "active": 0, "off_market": 0, "multifamily": 0,
        "vacant": 0, "commercial": 0, "exempt": 0, "total": len(rows),
    }
    for row in rows:
        division_cd = str(row.get("division_cd", "") or "").upper()
        if division_cd == "TAD":
            prop_type = _classify_tad(row)
        elif division_cd == "COLLIN":
            prop_type = _classify_collin(row)
        elif division_cd == "DENTON":
            prop_type = _classify_denton(row)
        else:
            prop_type = classify_parcel(row, exempt_set)
        if prop_type == "multifamily":
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
            feature = build_feature(row, prop_type, False, None)
            feature["properties"]["source_county"] = (
                "tad" if division_cd == "TAD"
                else "collin" if division_cd == "COLLIN"
                else "denton" if division_cd == "DENTON"
                else "dcad"
            )
            features.append(feature)
        except Exception:
            continue
    return features, counts


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
    area_id: str | None = None


class MergeJobsRequest(BaseModel):
    job_ids: list[str]
    area_id: str | None = None


class VerificationRequest(BaseModel):
    verifications: dict[str, str] = {}
    potential_targets: dict[str, str] = {}


class SavedAreaCreateRequest(BaseModel):
    name: str
    polygon: list[list[float]] | None = None
    filter_state: dict[str, Any] | None = None
    type: Literal["area", "location"] = "area"
    lat: float | None = None
    lng: float | None = None
    # Optional: the job_id the user was analyzing when they clicked Save Area.
    # Backfills cached_jobs.saved_area_id and analysis_sessions.saved_area_id so
    # the next CSV download from this job carries the new area's share_id.
    job_id: str | None = None
    originator_parcel_county: str | None = None
    originator_parcel_account_num: str | None = None


class SavedAreaUpdateRequest(BaseModel):
    name: str | None = None
    filter_state: dict[str, Any] | None = None
    type: Literal["area", "location"] | None = None
    lat: float | None = None
    lng: float | None = None
    originator_parcel_county: str | None = None
    originator_parcel_account_num: str | None = None


class SavedParcelCreateRequest(BaseModel):
    account_num: str
    county: str = "dcad"
    payload: dict[str, Any] | None = None
    # Optional: when set, also creates a bonded copy of this target into the
    # specified saved area (in addition to ensuring the standalone exists).
    # Unknown / non-owned area_ids are ignored silently.
    area_id: str | None = None


class SaveSessionRequest(BaseModel):
    name: str
    filter_state: dict[str, Any] | None = None


class UpdateSessionRequest(BaseModel):
    name: str | None = None
    filter_state: dict[str, Any] | None = None


class LoginRequest(BaseModel):
    # `identifier` is the canonical field (username OR email). `email` is kept
    # as a backward-compat alias for any client still posting the old shape.
    identifier: str | None = None
    email: str | None = None
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class AdminCreateUserRequest(BaseModel):
    username: str
    email: str | None = None
    temp_password: str
    role: str = "user"


class AdminResetPasswordRequest(BaseModel):
    temp_password: str


def _password_valid(password: str) -> bool:
    return len(str(password or "")) >= 10


def _serialize_user(user: dict[str, Any]) -> dict[str, Any]:
    raw_email = user.get("email")
    return {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "email": None if raw_email is None else str(raw_email),
        "role": str(user["role"]),
        "is_active": bool(user.get("is_active")),
        "force_password_change": bool(user.get("force_password_change")),
    }


def _to_geojson_polygon(latlngs: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for pair in latlngs:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        lat = float(pair[0])
        lng = float(pair[1])
        out.append([lng, lat])
    return out


def _to_leaflet_polygon(geojson_pairs: Any) -> list[list[float]]:
    if not isinstance(geojson_pairs, list):
        return []
    out: list[list[float]] = []
    for pair in geojson_pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        lng = float(pair[0])
        lat = float(pair[1])
        out.append([lat, lng])
    return out


def _normalize_saved_area_payload(request: SavedAreaCreateRequest | SavedAreaUpdateRequest) -> tuple[str | None, list[list[float]] | None, float | None, float | None]:
    area_type = str(request.type or "area") if getattr(request, "type", None) else None
    if area_type is not None and area_type not in {"area", "location"}:
        raise HTTPException(status_code=400, detail="Invalid area type")

    raw_polygon = request.polygon if isinstance(getattr(request, "polygon", None), list) else None
    polygon_geojson: list[list[float]] | None = None
    lat: float | None = getattr(request, "lat", None)
    lng: float | None = getattr(request, "lng", None)

    if area_type == "location":
        if lat is not None and lng is not None:
            polygon_geojson = [[float(lng), float(lat)]]
        elif raw_polygon and len(raw_polygon) >= 1:
            polygon_geojson = _to_geojson_polygon(raw_polygon)
            if polygon_geojson:
                lng = float(polygon_geojson[0][0])
                lat = float(polygon_geojson[0][1])
        else:
            raise HTTPException(status_code=400, detail="Location requires lat/lng")
    elif area_type == "area":
        if raw_polygon is None or len(raw_polygon) < 3:
            raise HTTPException(status_code=400, detail="Polygon must have at least 3 points")
        polygon_geojson = _to_geojson_polygon(raw_polygon)
        lat = None
        lng = None

    return area_type, polygon_geojson, lat, lng


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
        "email": row[2] if row[2] is None else str(row[2]),
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
    if actor_role == "owner" and target_role not in {"member", "user", "power_user"}:
        raise HTTPException(status_code=403, detail="Owner can manage user and power_user accounts only")
    if action in {"disable", "delete"} and target_role == "owner":
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


def _parcel_addr_match_key(row: dict[str, Any]) -> str:
    """Return a normalized parcel address key suitable for Redfin joins.

    County ingest sources are inconsistent. Some rows store full display strings
    like "123 MAIN ST\nCITY, TX 75000" while redfin_active.addr_key is street-only.
    Use only the first line and first comma-delimited segment before normalization
    so matching is resilient across counties.
    """
    raw = str(row.get("property_address", "") or "").strip()
    if not raw:
        return ""
    first_line = raw.splitlines()[0].strip()
    street_only = first_line.split(",", 1)[0].strip() if first_line else ""
    candidate = street_only or first_line or raw
    return normalize_addr_key(candidate)


# Validate required runtime settings at startup.
get_settings()

app = FastAPI(title="LotLedger")
app.include_router(propelio_router)


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

    def _unauth_response() -> JSONResponse:
        response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        clear_auth_cookies(response)
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

    if is_protected or path.startswith("/api/") or path.startswith("/auth/") or path.startswith("/admin/"):
        try:
            user = get_current_user(request)
            request.state.current_user = user
        except AuthError:
            user = None

    if path.startswith("/api/") and user is None:
        return _unauth_response()

    if path.startswith("/admin/") and user is None:
        return _unauth_response()

    if path.startswith("/auth/") and path not in {"/auth/login"} and user is None:
        return _unauth_response()

    if user and bool(user.get("force_password_change")):
        if path.startswith("/api/") or path.startswith("/admin/") or path.startswith("/auth/"):
            if path not in _FORCE_PASSWORD_CHANGE_ALLOWED_PATHS and path != "/auth/login":
                return JSONResponse(
                    {"detail": "Password change required", "code": "FORCE_PASSWORD_CHANGE_REQUIRED"},
                    status_code=403,
                )

    response = await call_next(request)

    # Rolling refresh for authenticated requests, except auth endpoints that
    # explicitly manage cookies themselves.
    if user and path not in {"/auth/logout", "/auth/change-password", "/auth/login"}:
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
    # No CSRF check on login: there's no authenticated session to protect yet.
    # Requiring CSRF here only creates a chicken-and-egg cookie-timing bug
    # (the legitimate case where users hit "Sign In" on a cold page load and
    # the browser hasn't yet processed the csrf cookie from the prior 401).
    # CSRF still protects every other endpoint (logout, change-password, all /api/*).
    ip = get_client_ip(request)
    allowed, retry_after = login_allowed(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Too many failed attempts. Retry in {retry_after}s")

    identifier = str(payload.identifier or payload.email or "").strip()
    password = str(payload.password or "")
    if not identifier:
        raise HTTPException(status_code=422, detail="Username or email is required")
    user = get_user_by_username_or_email(identifier)
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
        "email": row[2] if row[2] is None else str(row[2]),
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
                    "email": row[2] if row[2] is None else str(row[2]),
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
    email_raw = str(payload.email or "").strip().lower()
    email_or_null: str | None = email_raw if email_raw else None
    temp_password = str(payload.temp_password or "")
    role = str(payload.role or "user").strip().lower()

    if not username:
        raise HTTPException(status_code=422, detail="Username is required")
    if not _password_valid(temp_password):
        raise HTTPException(status_code=422, detail="Password must be at least 10 characters")

    if role == "member":
        role = "user"

    allowed_roles_by_owner = {"power_user", "user", "member"}
    allowed_roles_by_developer = {"owner", "power_user", "user", "member"}

    if actor["role"] == "owner":
        if role not in allowed_roles_by_owner:
            raise HTTPException(status_code=403, detail=f"Owners cannot create role '{role}'")
    elif actor["role"] == "developer":
        if role not in allowed_roles_by_developer:
            raise HTTPException(status_code=403, detail=f"Developers cannot create role '{role}' via this path")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, email, password_hash, role, is_active, force_password_change, created_by)
                VALUES (%s, %s, %s, %s, true, true, %s)
                RETURNING id, username, email, role, is_active, force_password_change
                """,
                (username, email_or_null, hash_password(temp_password), role, actor["username"]),
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
            "email": row[2] if row[2] is None else str(row[2]),
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
    actor: dict[str, Any] = Depends(require_role("owner", "developer")),
) -> dict[str, Any]:
    require_csrf(request)
    alphabet = string.ascii_letters + string.digits
    temp_password = "".join(secrets.choice(alphabet) for _ in range(14))

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
    return {"ok": True, "temp_password": temp_password}


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int,
    request: Request,
    actor: dict[str, Any] = Depends(require_role("owner", "developer")),
) -> dict[str, Any]:
    require_csrf(request)

    if int(user_id) == int(actor["id"]):
        raise HTTPException(status_code=403, detail="Cannot delete yourself")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            target = _require_target_user(cur, user_id)
            _enforce_admin_target_rules(actor, target, "delete")
            write_auth_audit_log(
                actor=actor["username"],
                actor_user_id=int(actor["id"]),
                action="delete_user",
                target_user=target["username"],
                target_user_id=int(target["id"]),
                detail=f"role={target['role']}",
                ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
            cur.execute("DELETE FROM session_tags WHERE user_id = %s", (int(user_id),))
            cur.execute("DELETE FROM saved_parcels WHERE user_id = %s", (int(user_id),))
            cur.execute("DELETE FROM cached_jobs WHERE user_id = %s", (int(user_id),))
            cur.execute("DELETE FROM analysis_sessions WHERE user_id = %s", (int(user_id),))
            cur.execute("DELETE FROM saved_areas WHERE user_id = %s", (int(user_id),))
            cur.execute("DELETE FROM users WHERE id = %s", (int(user_id),))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
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
async def analyze(request: AnalyzeRequest, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)
    polygon = request.polygon
    include_redfin = bool(request.include_redfin)
    include_sold = bool(request.include_sold)
    area_id = str(request.area_id or "").strip() or None
    user_id = int(user["id"])
    if area_id and not _saved_area_exists(area_id):
        raise HTTPException(status_code=404, detail="Saved area not found")
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
            "saved_area_id": area_id,
            "created_at": time.monotonic(),
            "last_accessed": time.monotonic(),
        }
        try:
            await asyncio.to_thread(_persist_cached_job_sync, empty_job_id, user_id, [], sold_points, polygon, area_id)
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
        addr_key = _parcel_addr_match_key(row)
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
        "saved_area_id": area_id,
        "created_at": time.monotonic(),
        "last_accessed": time.monotonic(),
    }
    try:
        await asyncio.to_thread(_persist_cached_job_sync, job_id, user_id, rows, sold_points, polygon, area_id)
    except Exception as exc:
        logger.warning("Failed to persist job to cache (non-fatal): %s", exc)
    asyncio.create_task(
        _persist_session_async(
            job_id,
            polygon,
            len(rows),
            county_coverage,
            user_id,
            area_id,
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
async def merge_jobs(request: MergeJobsRequest, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Merge rows from multiple tile job_ids into a single exportable job."""
    require_csrf(req)
    merged_rows: list[dict[str, Any]] = []
    merged_redfin: dict[str, Any] = {}
    merged_sold_points: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_sold_keys: set[str] = set()
    user_id = int(user["id"])
    area_id = str(request.area_id or "").strip() or None
    if area_id and not _saved_area_exists(area_id):
        raise HTTPException(status_code=404, detail="Saved area not found")
    for job_id in request.job_ids:
        job = _get_job(job_id, user_id)
        if job is None:
            continue
        if area_id is None:
            inferred_area = str(job.get("saved_area_id") or "").strip()
            if inferred_area:
                area_id = inferred_area
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
    if area_id and not _saved_area_exists(area_id):
        area_id = None

    _evict_stale_jobs()
    new_job_id = str(uuid.uuid4())
    _job_store[new_job_id] = {
        "rows": merged_rows,
        "redfin_data": merged_redfin,
        "sold_points": merged_sold_points,
        "polygon": [],
        "user_id": user_id,
        "saved_area_id": area_id,
        "created_at": time.monotonic(),
        "last_accessed": time.monotonic(),
    }
    try:
        await asyncio.to_thread(_persist_cached_job_sync, new_job_id, user_id, merged_rows, merged_sold_points, [], area_id)
    except Exception as exc:
        logger.warning("Failed to persist job to cache (non-fatal): %s", exc)
    return {"job_id": new_job_id}


@app.post("/api/job/{job_id}/verification")
async def save_verification(job_id: str, request: VerificationRequest, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)
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
async def set_tag(req: Request, payload: dict[str, Any] = Body(...), user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)
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
    if str(user.get("role") or "").strip().lower() == "user":
        raise HTTPException(status_code=403, detail="CSV export not available for this role")

    job = _get_job(job_id, int(user["id"]))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    rows = job.get("rows", [])
    redfin_data: dict[str, dict] = job.get("redfin_data", {})
    sold_points: list[dict[str, Any]] = job.get("sold_points", []) or []
    job_saved_area_id = str(job.get("saved_area_id") or "").strip() or None
    csv_share_id = _job_share_id(job_id, job_saved_area_id)
    logger.info("Download job %s: %d parcel rows, %d sold points", job_id, len(rows), len(sold_points))

    # Look up bonded seed-target account_nums for this area (if any), so the
    # CSV can mark each row with whether it was a seed of this workspace.
    # Empty set when the job has no saved_area_id (analysis-only, not yet saved).
    seed_account_nums: set[str] = set()
    if job_saved_area_id:
        _conn = get_session_conn()
        try:
            with _conn.cursor() as _cur:
                _cur.execute(
                    "SELECT account_num FROM saved_parcels WHERE area_id = %s",
                    (job_saved_area_id,),
                )
                seed_account_nums = {str(r[0]) for r in _cur.fetchall() if r and r[0]}
        finally:
            release_session_conn(_conn)

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
                "Seed Target",
                "share_id",
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
            addr_key = _parcel_addr_match_key(row)
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
                    "yes" if str(row.get("account_num", "") or "") in seed_account_nums else "",
                    csv_share_id,
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


@app.get("/api/sessions")
async def list_sessions(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """List the current user's named (permanent) sessions, ordered newest first."""
    uid = int(user["id"])
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT session_id, name, parcel_count, county_coverage, filter_state, polygon, created_at
                FROM analysis_sessions
                WHERE user_id = %s AND name IS NOT NULL
                ORDER BY created_at DESC
                """,
                (uid,),
            )
            sessions = []
            for row in cur.fetchall():
                polygon_latlngs = _to_leaflet_polygon(row[5]) if row[5] else []
                sessions.append({
                    "session_id": row[0],
                    "name": row[1],
                    "parcel_count": int(row[2] or 0),
                    "county_coverage": list(row[3] or []),
                    "filter_state": row[4] if isinstance(row[4], dict) else None,
                    "latlngs": polygon_latlngs,
                    "created_at": row[6].isoformat() if row[6] else None,
                })
    finally:
        release_session_conn(conn)
    return {"sessions": sessions}


@app.get("/api/sessions/{session_id}/data")
async def get_session_data(session_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Return a named session's full parcel data from cached_jobs (same shape as /api/analyze)."""
    uid = int(user["id"])
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM analysis_sessions WHERE session_id = %s AND user_id = %s AND name IS NOT NULL",
                (session_id, uid),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Named session not found")
    finally:
        release_session_conn(conn)

    cached = _load_cached_job(session_id)
    if cached is None or int(cached.get("user_id", -1)) != uid:
        raise HTTPException(status_code=404, detail="Session data not found in cache")

    rows = list(cached.get("rows", []))
    sold_points = list(cached.get("sold_points", []))
    _apply_session_tags(session_id, uid, rows)
    features, counts = _build_features_from_rows(rows)
    return {
        "type": "FeatureCollection",
        "features": features,
        "counts": counts,
        "sold_points": sold_points,
        "job_id": session_id,
        "redfin_requested": False,
        "redfin_ok": False,
        "redfin_skipped": True,
        "source_status": {"dcad_ok": True, "tad_ok": True, "collin_ok": True, "denton_ok": True},
    }


@app.post("/api/sessions/{session_id}/save")
async def save_session(
    session_id: str,
    request: SaveSessionRequest,
    req: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Name an ephemeral session, making it permanent. Pins BOTH analysis_sessions and cached_jobs in one transaction."""
    require_csrf(req)
    name = str(request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Session name is required")
    uid = int(user["id"])
    is_developer = str(user.get("role") or "").strip().lower() == "developer"
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            select_where = "WHERE session_id = %s" if is_developer else "WHERE session_id = %s AND user_id = %s"
            select_params = (session_id,) if is_developer else (session_id, uid)
            cur.execute(
                f"SELECT 1 FROM analysis_sessions {select_where}",
                select_params,
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Session not found")
            update_where = "WHERE session_id = %s" if is_developer else "WHERE session_id = %s AND user_id = %s"
            update_params: tuple[Any, ...] = (
                name,
                Json(request.filter_state) if isinstance(request.filter_state, dict) else None,
                session_id,
            ) if is_developer else (
                name,
                Json(request.filter_state) if isinstance(request.filter_state, dict) else None,
                session_id,
                uid,
            )
            cur.execute(
                f"""
                UPDATE analysis_sessions
                SET name = %s, filter_state = %s, expires_at = NULL, last_accessed = now()
                {update_where}
                RETURNING session_id, name, parcel_count, county_coverage, filter_state, polygon, created_at
                """,
                update_params,
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Session not found")
            # Pin cached_jobs in the same transaction (gap #1: both rows must be NULL or session becomes unloadable)
            cur.execute("UPDATE cached_jobs SET expires_at = NULL WHERE job_id = %s", (session_id,))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    polygon_latlngs = _to_leaflet_polygon(row[5]) if row[5] else []
    return {
        "session_id": row[0],
        "name": row[1],
        "parcel_count": int(row[2] or 0),
        "county_coverage": list(row[3] or []),
        "filter_state": row[4] if isinstance(row[4], dict) else None,
        "latlngs": polygon_latlngs,
        "created_at": row[6].isoformat() if row[6] else None,
    }


@app.put("/api/sessions/{session_id}")
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    req: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Rename a session and/or update its filter_state."""
    require_csrf(req)
    if request.name is None and request.filter_state is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    uid = int(user["id"])
    is_developer = str(user.get("role") or "").strip().lower() == "developer"

    update_cols: list[str] = []
    params: list[Any] = []
    if request.name is not None:
        name = str(request.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Session name is required")
        update_cols.append("name = %s")
        params.append(name)
    if request.filter_state is not None:
        update_cols.append("filter_state = %s")
        params.append(Json(request.filter_state) if isinstance(request.filter_state, dict) else None)

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            where_clause = "WHERE session_id = %s" if is_developer else "WHERE session_id = %s AND user_id = %s"
            query_params: list[Any] = [*params, session_id]
            if not is_developer:
                query_params.append(uid)
            cur.execute(
                f"""
                UPDATE analysis_sessions
                SET {', '.join(update_cols)}
                {where_clause}
                RETURNING session_id, name, parcel_count, county_coverage, filter_state, polygon, created_at
                """,
                tuple(query_params),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    polygon_latlngs = _to_leaflet_polygon(row[5]) if row[5] else []
    return {
        "session_id": row[0],
        "name": row[1],
        "parcel_count": int(row[2] or 0),
        "county_coverage": list(row[3] or []),
        "filter_state": row[4] if isinstance(row[4], dict) else None,
        "latlngs": polygon_latlngs,
        "created_at": row[6].isoformat() if row[6] else None,
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: str,
    req: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a named session and its cached_jobs row. Cascades to session_tags via FK."""
    require_csrf(req)
    uid = int(user["id"])
    is_developer = str(user.get("role") or "").strip().lower() == "developer"
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            where_clause = "WHERE session_id = %s" if is_developer else "WHERE session_id = %s AND user_id = %s"
            query_params = (session_id,) if is_developer else (session_id, uid)
            cur.execute(f"DELETE FROM analysis_sessions {where_clause}", query_params)
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Session not found")
            # Explicit delete — cached_jobs has no FK to analysis_sessions
            cur.execute("DELETE FROM cached_jobs WHERE job_id = %s", (session_id,))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)
    return {"ok": True}


@app.get("/api/areas")
async def list_saved_areas(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT area_id, name, polygon, filter_state, type, share_id,
                       originator_parcel_county, originator_parcel_account_num,
                       created_at, updated_at
                FROM saved_areas
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (int(user["id"]),)
            )
            areas = [
                {
                    "area_id": row[0],
                    "name": row[1],
                    "polygon": _to_leaflet_polygon(row[2]),
                    "filter_state": row[3] if isinstance(row[3], dict) else None,
                    "type": str(row[4] or "area"),
                    "share_id": str(row[5] or ""),
                    "originator_parcel_county": str(row[6] or "").strip().lower() or None,
                    "originator_parcel_account_num": str(row[7] or "").strip() or None,
                    "lat": (float(row[2][0][1]) if isinstance(row[2], list) and row[2] and len(row[2][0]) >= 2 else None) if str(row[4] or "area") == "location" else None,
                    "lng": (float(row[2][0][0]) if isinstance(row[2], list) and row[2] and len(row[2][0]) >= 2 else None) if str(row[4] or "area") == "location" else None,
                    "created_at": row[8].isoformat() if row[8] else None,
                    "updated_at": row[9].isoformat() if row[9] else None,
                }
                for row in cur.fetchall()
            ]
    finally:
        release_session_conn(conn)
    return {"areas": areas}


def _point_in_polygon(lat: float, lng: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test. Polygon is a list of [lng, lat] pairs
    (GeoJSON convention, NOT Leaflet [lat, lng]). Returns True if the point
    falls inside or on the polygon boundary."""
    if not isinstance(polygon, list) or len(polygon) < 3:
        return False
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = float(polygon[i][0]), float(polygon[i][1])
        xj, yj = float(polygon[j][0]), float(polygon[j][1])
        if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _bond_standalone_targets_to_area(cur: Any, *, user_id: int, area_id: str, polygon_geojson: list[list[float]]) -> int:
    """For all of the user's standalone targets (area_id IS NULL) whose
    payload['lat'], payload['lng'] falls inside the given GeoJSON polygon,
    INSERT a bonded copy with area_id set. Returns the number of bonds created.

    Standalones are NOT modified - they remain as the user's master library.
    Bonded copies inherit account_num, county, and payload from the standalone.
    Conflicts (a bond already exists) are silently ignored.
    """
    if not isinstance(polygon_geojson, list) or len(polygon_geojson) < 3:
        return 0
    cur.execute(
        """
        SELECT account_num, county, payload
        FROM saved_parcels
        WHERE user_id = %s AND area_id IS NULL
        """,
        (int(user_id),),
    )
    standalones = cur.fetchall()
    bonded_count = 0
    for account_num, county, payload in standalones:
        if not isinstance(payload, dict):
            continue
        lat_raw = payload.get("lat")
        lng_raw = payload.get("lng")
        if lat_raw is None or lng_raw is None:
            continue
        try:
            lat = float(lat_raw)
            lng = float(lng_raw)
        except (TypeError, ValueError):
            continue
        if not _point_in_polygon(lat, lng, polygon_geojson):
            continue
        cur.execute(
            """
            INSERT INTO saved_parcels (account_num, county, payload, user_id, area_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, account_num, county, area_id) WHERE area_id IS NOT NULL
            DO NOTHING
            """,
            (account_num, county, Json(payload), int(user_id), area_id),
        )
        if cur.rowcount > 0:
            bonded_count += 1
    return bonded_count


@app.post("/api/areas")
async def create_saved_area(request: SavedAreaCreateRequest, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)
    name = str(request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Area name is required")
    area_type, polygon_geojson, lat, lng = _normalize_saved_area_payload(request)
    originator_county = str(request.originator_parcel_county or "").strip().lower() or None
    originator_account = str(request.originator_parcel_account_num or "").strip() or None
    if bool(originator_county) != bool(originator_account):
        raise HTTPException(
            status_code=400,
            detail="originator_parcel_county and originator_parcel_account_num must be provided together",
        )

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            row = None
            share_id = ""
            for _ in range(10):
                share_id = _generate_share_id()
                try:
                    cur.execute("SAVEPOINT sp_create_saved_area")
                    cur.execute(
                        """
                        INSERT INTO saved_areas (
                            name, polygon, filter_state, type, share_id, user_id,
                            originator_parcel_county, originator_parcel_account_num
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING area_id, originator_parcel_county, originator_parcel_account_num, created_at, updated_at
                        """,
                        (
                            name,
                            Json(polygon_geojson),
                            Json(request.filter_state) if isinstance(request.filter_state, dict) else None,
                            area_type,
                            share_id,
                            int(user["id"]),
                            originator_county,
                            originator_account,
                        ),
                    )
                    row = cur.fetchone()
                    cur.execute("RELEASE SAVEPOINT sp_create_saved_area")
                    break
                except UniqueViolation:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_create_saved_area")
                    cur.execute("RELEASE SAVEPOINT sp_create_saved_area")
                    continue
            if row is None:
                raise HTTPException(status_code=503, detail="Failed to allocate unique share_id")
            # Backfill linkage: if the user was analyzing a job when they hit Save,
            # link that job (and its session) to the new area so CSV exports from
            # this point on carry the correct share_id.
            inflight_job_id = str(request.job_id or "").strip()
            if inflight_job_id:
                new_area_id = row[0]
                cur.execute(
                    "UPDATE cached_jobs SET saved_area_id = %s WHERE job_id = %s AND saved_area_id IS NULL AND user_id = %s",
                    (new_area_id, inflight_job_id, int(user["id"])),
                )
                cur.execute(
                    "UPDATE analysis_sessions SET saved_area_id = %s WHERE session_id = %s AND saved_area_id IS NULL AND user_id = %s",
                    (new_area_id, inflight_job_id, int(user["id"])),
                )

            # Bond standalone targets inside the polygon to the new area.
            # Only meaningful for type='area' (location pins are single points).
            if area_type == "area" and isinstance(polygon_geojson, list) and len(polygon_geojson) >= 3:
                _bond_standalone_targets_to_area(
                    cur,
                    user_id=int(user["id"]),
                    area_id=row[0],
                    polygon_geojson=polygon_geojson,
                )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    return {
        "area_id": row[0],
        "name": name,
        "polygon": _to_leaflet_polygon(polygon_geojson),
        "filter_state": request.filter_state if isinstance(request.filter_state, dict) else None,
        "type": area_type,
        "share_id": share_id,
        "originator_parcel_county": str(row[1] or "").strip().lower() or None,
        "originator_parcel_account_num": str(row[2] or "").strip() or None,
        "lat": lat,
        "lng": lng,
        "created_at": row[3].isoformat() if row[3] else None,
        "updated_at": row[4].isoformat() if row[4] else None,
    }


@app.get("/api/areas/{area_id}")
async def get_saved_area(area_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT area_id, name, polygon, filter_state, type, share_id,
                       originator_parcel_county, originator_parcel_account_num,
                       user_id, created_at, updated_at
                FROM saved_areas
                WHERE area_id = %s
                LIMIT 1
                """,
                (area_id,),
            )
            row = cur.fetchone()

            seed_parcels: list[dict[str, Any]] = []
            if row is not None:
                cur.execute(
                    """
                    SELECT account_num, county, payload
                    FROM saved_parcels
                    WHERE area_id = %s
                    ORDER BY created_at ASC
                    """,
                    (row[0],),
                )
                for sp_row in cur.fetchall():
                    seed_parcels.append(
                        {
                            "account_num": sp_row[0],
                            "county": sp_row[1],
                            "payload": sp_row[2] if isinstance(sp_row[2], dict) else {},
                        }
                    )
    finally:
        release_session_conn(conn)

    if row is None:
        raise HTTPException(status_code=404, detail="Saved area not found")
    if int(row[8] or 0) != int(user["id"]):
        raise HTTPException(status_code=403, detail="Forbidden")

    return {
        "area_id": row[0],
        "name": row[1],
        "polygon": _to_leaflet_polygon(row[2]),
        "filter_state": row[3] if isinstance(row[3], dict) else None,
        "type": str(row[4] or "area"),
        "share_id": str(row[5] or ""),
        "originator_parcel_county": str(row[6] or "").strip().lower() or None,
        "originator_parcel_account_num": str(row[7] or "").strip() or None,
        "lat": (float(row[2][0][1]) if isinstance(row[2], list) and row[2] and len(row[2][0]) >= 2 else None) if str(row[4] or "area") == "location" else None,
        "lng": (float(row[2][0][0]) if isinstance(row[2], list) and row[2] and len(row[2][0]) >= 2 else None) if str(row[4] or "area") == "location" else None,
        "created_at": row[9].isoformat() if row[9] else None,
        "updated_at": row[10].isoformat() if row[10] else None,
        "seed_parcels": seed_parcels,
    }


@app.get("/api/area/by-share-id/{share_id}")
async def get_saved_area_by_share_id(
    share_id: str = FastAPIPath(..., regex=r"^area_[A-Za-z0-9]{10}$"),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                  SELECT area_id, name, polygon, filter_state, type, share_id,
                      originator_parcel_county, originator_parcel_account_num,
                      created_at, updated_at,
                       COUNT(*) OVER() AS match_count
                FROM saved_areas
                WHERE share_id = %s
                LIMIT 1
                """,
                (share_id,),
            )
            row = cur.fetchone()

            seed_parcels: list[dict[str, Any]] = []
            if row is not None:
                cur.execute(
                    """
                    SELECT account_num, county, payload
                    FROM saved_parcels
                    WHERE area_id = %s
                    ORDER BY created_at ASC
                    """,
                    (row[0],),
                )
                for sp_row in cur.fetchall():
                    seed_parcels.append(
                        {
                            "account_num": sp_row[0],
                            "county": sp_row[1],
                            "payload": sp_row[2] if isinstance(sp_row[2], dict) else {},
                        }
                    )
    finally:
        release_session_conn(conn)

    if row is None:
        raise HTTPException(status_code=404, detail="Saved area not found")

    if int(row[10] or 0) > 1:
        logger.warning("Multiple saved_areas rows found for share_id=%s", share_id)

    return {
        "area_id": row[0],
        "name": row[1],
        "polygon": _to_leaflet_polygon(row[2]),
        "filter_state": row[3] if isinstance(row[3], dict) else None,
        "type": str(row[4] or "area"),
        "share_id": str(row[5] or ""),
        "originator_parcel_county": str(row[6] or "").strip().lower() or None,
        "originator_parcel_account_num": str(row[7] or "").strip() or None,
        "lat": (float(row[2][0][1]) if isinstance(row[2], list) and row[2] and len(row[2][0]) >= 2 else None) if str(row[4] or "area") == "location" else None,
        "lng": (float(row[2][0][0]) if isinstance(row[2], list) and row[2] and len(row[2][0]) >= 2 else None) if str(row[4] or "area") == "location" else None,
        "created_at": row[8].isoformat() if row[8] else None,
        "updated_at": row[9].isoformat() if row[9] else None,
        "seed_parcels": seed_parcels,
    }


@app.post("/api/areas/from-share-id/{share_id}")
async def fork_saved_area(
    req: Request,
    share_id: str = FastAPIPath(..., regex=r"^area_[A-Za-z0-9]{10}$"),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_csrf(req)

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT area_id, name, polygon, filter_state, type,
                       originator_parcel_county, originator_parcel_account_num
                FROM saved_areas
                WHERE share_id = %s
                LIMIT 1
                """,
                (share_id,),
            )
            source = cur.fetchone()
            if source is None:
                raise HTTPException(status_code=404, detail="Saved area not found")

            source_area_id = source[0]
            source_name = str(source[1] or "Untitled")
            source_polygon = source[2] if isinstance(source[2], list) else []
            source_filter_state = source[3] if isinstance(source[3], dict) else None
            source_type = str(source[4] or "area")
            source_originator_county = str(source[5] or "").strip().lower() or None
            source_originator_account = str(source[6] or "").strip() or None

            cur.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (int(user["id"]),))
            if cur.fetchone() is None:
                raise HTTPException(status_code=403, detail="User not found")

            cur.execute(
                """
                SELECT name
                FROM saved_areas
                WHERE user_id = %s
                  AND (name = %s OR name LIKE %s)
                """,
                (int(user["id"]), source_name, f"{source_name} (%)"),
            )
            taken_names = {str(row[0] or "") for row in cur.fetchall()}
            if source_name not in taken_names:
                fork_name = source_name
            else:
                next_n = 2
                while f"{source_name} ({next_n})" in taken_names:
                    next_n += 1
                fork_name = f"{source_name} ({next_n})"

            row = None
            new_share_id = ""
            for _ in range(10):
                new_share_id = _generate_share_id()
                try:
                    cur.execute("SAVEPOINT sp_fork_saved_area")
                    cur.execute(
                        """
                        INSERT INTO saved_areas (
                            name, polygon, filter_state, type, share_id, user_id,
                            originator_parcel_county, originator_parcel_account_num
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING area_id, originator_parcel_county, originator_parcel_account_num, created_at, updated_at
                        """,
                        (
                            fork_name,
                            Json(source_polygon),
                            Json(source_filter_state) if isinstance(source_filter_state, dict) else None,
                            source_type,
                            new_share_id,
                            int(user["id"]),
                            source_originator_county,
                            source_originator_account,
                        ),
                    )
                    row = cur.fetchone()
                    cur.execute("RELEASE SAVEPOINT sp_fork_saved_area")
                    break
                except UniqueViolation:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_fork_saved_area")
                    cur.execute("RELEASE SAVEPOINT sp_fork_saved_area")
                    continue
            if row is None:
                raise HTTPException(status_code=503, detail="Failed to allocate unique share_id")

            # Note: user_rating + rating_at intentionally NOT copied here. Ratings
            # are now in comp_ratings (canonical Phase 2 store, copied separately
            # below). The archive is comp-data only.
            cur.execute(
                """
                INSERT INTO propelio_comp_archive (
                    saved_area_id, comp_address_key, comp_mls, comp_data,
                    parcel_geom, parcel_account_num, status, last_status, last_price
                )
                SELECT %s, comp_address_key, comp_mls, comp_data,
                       parcel_geom, parcel_account_num, status, last_status, last_price
                FROM propelio_comp_archive
                WHERE saved_area_id = %s
                """,
                (row[0], source_area_id),
            )

            cur.execute(
                """
                INSERT INTO comp_ratings (workspace_id, comp_id, rating, rated_by_user_id, rated_at)
                SELECT %s, comp_id, rating, rated_by_user_id, rated_at
                FROM comp_ratings
                WHERE workspace_id = %s
                """,
                (row[0], source_area_id),
            )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    return {
        "area_id": row[0],
        "name": fork_name,
        "polygon": _to_leaflet_polygon(source_polygon),
        "filter_state": source_filter_state if isinstance(source_filter_state, dict) else None,
        "type": source_type,
        "share_id": new_share_id,
        "originator_parcel_county": str(row[1] or "").strip().lower() or None,
        "originator_parcel_account_num": str(row[2] or "").strip() or None,
        "lat": (float(source_polygon[0][1]) if isinstance(source_polygon, list) and source_polygon and len(source_polygon[0]) >= 2 else None) if source_type == "location" else None,
        "lng": (float(source_polygon[0][0]) if isinstance(source_polygon, list) and source_polygon and len(source_polygon[0]) >= 2 else None) if source_type == "location" else None,
        "created_at": row[3].isoformat() if row[3] else None,
        "updated_at": row[4].isoformat() if row[4] else None,
    }


@app.put("/api/areas/{area_id}")
async def update_saved_area(area_id: str, request: SavedAreaUpdateRequest, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)
    if request.name is None and request.filter_state is None and request.type is None and request.lat is None and request.lng is None and request.originator_parcel_county is None and request.originator_parcel_account_num is None:
        raise HTTPException(status_code=400, detail="Nothing to update")

    update_cols: list[str] = ["updated_at = now()"]
    params: list[Any] = []

    if request.name is not None:
        name = str(request.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Area name is required")
        update_cols.append("name = %s")
        params.append(name)

    if request.filter_state is not None:
        update_cols.append("filter_state = %s")
        params.append(Json(request.filter_state) if isinstance(request.filter_state, dict) else None)

    if request.type is not None or request.lat is not None or request.lng is not None:
        area_type, polygon_geojson, _, _ = _normalize_saved_area_payload(request)
        if area_type is not None:
            update_cols.append("type = %s")
            params.append(area_type)
        if polygon_geojson is not None:
            update_cols.append("polygon = %s")
            params.append(Json(polygon_geojson))

    if request.originator_parcel_county is not None or request.originator_parcel_account_num is not None:
        if request.originator_parcel_county is None or request.originator_parcel_account_num is None:
            raise HTTPException(
                status_code=400,
                detail="originator_parcel_county and originator_parcel_account_num must be provided together",
            )
        county = str(request.originator_parcel_county or "").strip().lower() or None
        account = str(request.originator_parcel_account_num or "").strip() or None
        update_cols.append("originator_parcel_county = %s")
        params.append(county)
        update_cols.append("originator_parcel_account_num = %s")
        params.append(account)

    is_developer = str(user.get("role") or "").strip().lower() == "developer"
    where_clause = "WHERE area_id = %s" if is_developer else "WHERE area_id = %s AND user_id = %s"
    query_params: list[Any] = [*params, area_id]
    if not is_developer:
        query_params.append(int(user["id"]))

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE saved_areas
                SET {', '.join(update_cols)}
                {where_clause}
                RETURNING area_id, name, polygon, filter_state, type,
                          originator_parcel_county, originator_parcel_account_num,
                          updated_at
                """,
                tuple(query_params),
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
        "polygon": _to_leaflet_polygon(row[2]),
        "filter_state": row[3] if isinstance(row[3], dict) else None,
        "type": str(row[4] or "area"),
        "originator_parcel_county": str(row[5] or "").strip().lower() or None,
        "originator_parcel_account_num": str(row[6] or "").strip() or None,
        "lat": (float(row[2][0][1]) if isinstance(row[2], list) and row[2] and len(row[2][0]) >= 2 else None) if str(row[4] or "area") == "location" else None,
        "lng": (float(row[2][0][0]) if isinstance(row[2], list) and row[2] and len(row[2][0]) >= 2 else None) if str(row[4] or "area") == "location" else None,
        "updated_at": row[7].isoformat() if row[7] else None,
    }


@app.delete("/api/areas/{area_id}")
async def delete_saved_area(area_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    is_developer = str(user.get("role") or "").strip().lower() == "developer"
    where_clause = "WHERE area_id = %s" if is_developer else "WHERE area_id = %s AND user_id = %s"
    params = (area_id,) if is_developer else (area_id, int(user["id"]))

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM saved_areas {where_clause}", params)
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


@app.get("/api/parcels")
async def list_saved_parcels(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_num, county, payload, created_at
                FROM saved_parcels
                WHERE user_id = %s AND area_id IS NULL
                ORDER BY created_at DESC
                """,
                (int(user["id"]),),
            )
            parcels = [
                {
                    "account_num": row[0],
                    "county": row[1],
                    "payload": row[2] if isinstance(row[2], dict) else {},
                    "created_at": row[3].isoformat() if row[3] else None,
                }
                for row in cur.fetchall()
            ]
    finally:
        release_session_conn(conn)
    return {"parcels": parcels}


@app.post("/api/parcels")
async def create_saved_parcel(request: SavedParcelCreateRequest, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)
    account_num = str(request.account_num or "").strip()
    county = str(request.county or "dcad").strip().lower() or "dcad"
    if not account_num:
        raise HTTPException(status_code=400, detail="account_num is required")

    payload_data = request.payload if isinstance(request.payload, dict) else {}
    target_area_id = str(request.area_id or "").strip() or None

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            # Always ensure a standalone target row exists for this user/parcel.
            # If one already exists, refresh its payload (matches prior behavior).
            cur.execute(
                """
                INSERT INTO saved_parcels (account_num, county, payload, user_id, area_id)
                VALUES (%s, %s, %s, %s, NULL)
                ON CONFLICT (user_id, account_num, county) WHERE area_id IS NULL
                DO UPDATE SET payload = EXCLUDED.payload
                RETURNING account_num, county, payload, created_at, area_id
                """,
                (account_num, county, Json(payload_data), int(user["id"])),
            )
            standalone_row = cur.fetchone()

            # If a workspace area_id was supplied, also create a bonded copy.
            # Validate the area exists AND belongs to this user (or skip silently
            # - we don't want to leak whether an area exists).
            bonded_row = None
            if target_area_id:
                cur.execute(
                    "SELECT 1 FROM saved_areas WHERE area_id = %s AND user_id = %s LIMIT 1",
                    (target_area_id, int(user["id"])),
                )
                area_owned = cur.fetchone() is not None
                if area_owned:
                    cur.execute(
                        """
                        INSERT INTO saved_parcels (account_num, county, payload, user_id, area_id)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (user_id, account_num, county, area_id) WHERE area_id IS NOT NULL
                        DO UPDATE SET payload = EXCLUDED.payload
                        RETURNING account_num, county, payload, created_at, area_id
                        """,
                        (account_num, county, Json(payload_data), int(user["id"]), target_area_id),
                    )
                    bonded_row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    # Return the standalone row by default (matches prior shape). If a bonded
    # copy was also created, surface its area_id so the frontend can confirm
    # the bond happened.
    response: dict[str, Any] = {
        "account_num": standalone_row[0],
        "county": standalone_row[1],
        "payload": standalone_row[2] if isinstance(standalone_row[2], dict) else {},
        "created_at": standalone_row[3].isoformat() if standalone_row[3] else None,
        "area_id": None,
    }
    if bonded_row is not None:
        response["bonded_area_id"] = bonded_row[4]
    return response


@app.delete("/api/parcels/{county}/{account_num}")
async def delete_saved_parcel(county: str, account_num: str, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM saved_parcels WHERE county = %s AND account_num = %s AND user_id = %s",
                (str(county or "dcad").strip().lower() or "dcad", str(account_num or "").strip(), int(user["id"])),
            )
            deleted = cur.rowcount or 0
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if not deleted:
        raise HTTPException(status_code=404, detail="Saved parcel not found")
    return {"ok": True, "deleted": int(deleted)}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")