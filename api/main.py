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
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import parse_qs, quote_plus

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Path as FastAPIPath, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg2.errors import UniqueViolation
from psycopg2.extras import Json, RealDictCursor, execute_values
from pydantic import BaseModel, Field

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
from api.counties.dcad import SPTD_LABELS, _clean_text, _estimate_front_depth, _fetch_additional_owners, build_feature, classify_parcel, query_parcels
from api.counties.denton import _classify_denton, _normalize_denton_row, query_denton_parcels
from api.counties.tad import _normalize_tad_row, _classify_tad, query_tad_parcels
from api.geo import polygon_bbox
from api.propelio.csv_match import query_propelio_sold_for_parcels
from api.propelio.routes import router as propelio_router
from api.redfin import normalize_addr_key
from api.sold import log_redfin_sold_row_count, query_active_listings, query_sold_parcels


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

TILES_BASE_URL = os.environ.get(
    "TILES_BASE_URL",
    "https://storage.googleapis.com/lot-ledger-tiles/parcels.pmtiles",
).strip()
_INDEX_HTML_CACHE: str | None = None


def _resolve_build_id() -> str:
    env_val = os.getenv("BUILD_ID", "").strip()
    if env_val:
        return env_val
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            timeout=2,
            check=False,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        sha = result.stdout.decode().strip()
        if sha:
            return sha
    except Exception:
        pass
    return "dev"


BUILD_ID = _resolve_build_id()


def _resolve_app_version() -> str:
    env_val = os.getenv("APP_VERSION", "").strip()
    if env_val:
        return env_val
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "--merges", "--first-parent", "main"],
            capture_output=True,
            timeout=2,
            check=False,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        count = result.stdout.decode().strip()
        if count.isdigit():
            return f"0.{count}"
    except Exception:
        pass
    return "dev"


APP_VERSION = _resolve_app_version()

_STALE_LOG_COOLDOWN_S = 300
_stale_log_seen: dict[tuple[str, str], float] = {}
_stale_log_lock = threading.Lock()


def _should_log_stale(user_id: str, client_version: str) -> bool:
    key = (user_id, client_version)
    now = time.monotonic()
    with _stale_log_lock:
        last = _stale_log_seen.get(key, 0.0)
        if now - last < _STALE_LOG_COOLDOWN_S:
            return False
        _stale_log_seen[key] = now
        if len(_stale_log_seen) > 1000:
            _stale_log_seen.clear()
            _stale_log_seen[key] = now
        return True

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
            # parcel_ratings — workspace-scoped Good/Bad ratings on CAD parcels,
            # parallel to comp_ratings but keyed by (county, account_num) since
            # parcels exist independently of propelio_comps. Per
            # docs/PARCEL_RATINGS_SPEC.md v2.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS parcel_ratings (
                    rating_id BIGSERIAL PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
                    county TEXT NOT NULL,
                    account_num TEXT NOT NULL,
                    rating TEXT NOT NULL CHECK (rating IN ('good', 'bad')),
                    rated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    rated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (workspace_id, county, account_num)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_parcel_ratings_workspace
                    ON parcel_ratings (workspace_id)
                """
            )
            # comp_ratings_views — per-view (NBV/Export) Good/Bad comp ratings,
            # additive companion to comp_ratings (which stays the ARV store,
            # untouched). view CHECK excludes 'arv' so ARV is structurally
            # single-sourced in the old table (impossible to shadow ARV here).
            # Created UNCONDITIONALLY at startup (never flag-gated) so fork +
            # reads work under any config — per docs/superpowers/specs/
            # 2026-06-29-per-view-ratings-spec.md §2 + FMEA C1.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS comp_ratings_views (
                    rating_id BIGSERIAL PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
                    comp_id BIGINT NOT NULL REFERENCES propelio_comps(comp_id) ON DELETE CASCADE,
                    view TEXT NOT NULL CHECK (view IN ('nbv','export')),
                    rating TEXT NOT NULL CHECK (rating IN ('good','bad')),
                    rated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    rated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (workspace_id, comp_id, view)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_comp_ratings_views_ws
                    ON comp_ratings_views (workspace_id, comp_id)
                """
            )
            # parcel_ratings_views — per-view (NBV/Export) Good/Bad CAD parcel
            # ratings, additive companion to parcel_ratings (ARV store). Same
            # view exclusion + unconditional-create discipline as above.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS parcel_ratings_views (
                    rating_id BIGSERIAL PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
                    county TEXT NOT NULL,
                    account_num TEXT NOT NULL,
                    view TEXT NOT NULL CHECK (view IN ('nbv','export')),
                    rating TEXT NOT NULL CHECK (rating IN ('good','bad')),
                    rated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    rated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (workspace_id, county, account_num, view)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_parcel_ratings_views_ws
                    ON parcel_ratings_views (workspace_id, county, account_num)
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
                    # Sprint 1 multi-user collab: saved_area_members join table
                    # per docs/MULTIUSER_COLLAB_SPRINT1_SPEC.md §2.1 + §2.2.
                    (
                        "saved_area_members_table",
                        """
                        CREATE TABLE IF NOT EXISTS saved_area_members (
                            area_id     TEXT        NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
                            user_id     INTEGER     NOT NULL REFERENCES users(id)            ON DELETE CASCADE,
                            role        TEXT        NOT NULL CHECK (role IN ('owner', 'editor')),
                            added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                            added_via   TEXT        NOT NULL CHECK (added_via IN ('owner', 'share_link', 'developer_bypass', 'backfill')),
                            PRIMARY KEY (area_id, user_id)
                        )
                        """,
                    ),
                    (
                        "idx_saved_area_members_user_id",
                        "CREATE INDEX IF NOT EXISTS idx_saved_area_members_user_id ON saved_area_members(user_id)",
                    ),
                    (
                        "saved_area_members_backfill",
                        """
                        INSERT INTO saved_area_members (area_id, user_id, role, added_via)
                        SELECT area_id, user_id, 'owner', 'backfill'
                        FROM saved_areas
                        WHERE user_id IS NOT NULL
                        ON CONFLICT (area_id, user_id) DO NOTHING
                        """,
                    ),
                    # Sprint 2 multi-user collab: per-field filter state
                    # per docs/MULTIUSER_COLLAB_SPRINT2_SPEC.md §2.1 + §2.2.
                    (
                        "saved_area_filter_fields_table",
                        """
                        CREATE TABLE IF NOT EXISTS saved_area_filter_fields (
                            area_id              TEXT        NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
                            field_key            TEXT        NOT NULL,
                            value                JSONB,
                            client_seq           BIGINT      NOT NULL DEFAULT 0,
                            updated_by_user_id   INTEGER     REFERENCES users(id) ON DELETE SET NULL,
                            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
                            PRIMARY KEY (area_id, field_key)
                        )
                        """,
                    ),
                    (
                        "idx_saved_area_filter_fields_updated_at",
                        "CREATE INDEX IF NOT EXISTS idx_saved_area_filter_fields_updated_at ON saved_area_filter_fields (area_id, updated_at DESC)",
                    ),
                    (
                        "saved_area_filter_fields_backfill",
                        """
                        INSERT INTO saved_area_filter_fields
                          (area_id, field_key, value, client_seq, updated_by_user_id)
                        SELECT
                            sa.area_id,
                            sections.section || '.' || sections.key  AS field_key,
                            sections.val                              AS value,
                            0                                         AS client_seq,
                            sa.user_id                                AS updated_by_user_id
                        FROM saved_areas sa
                        CROSS JOIN LATERAL (
                            SELECT 'checkboxes' AS section, key, value AS val
                              FROM jsonb_each(sa.filter_state -> 'checkboxes')
                              WHERE jsonb_typeof(sa.filter_state -> 'checkboxes') = 'object'
                            UNION ALL
                            SELECT 'numeric', key, value FROM jsonb_each(sa.filter_state -> 'numeric')
                              WHERE jsonb_typeof(sa.filter_state -> 'numeric') = 'object'
                            UNION ALL
                            SELECT 'sold', key, value FROM jsonb_each(sa.filter_state -> 'sold')
                              WHERE jsonb_typeof(sa.filter_state -> 'sold') = 'object'
                            UNION ALL
                            SELECT 'comp', key, value FROM jsonb_each(sa.filter_state -> 'comp')
                              WHERE jsonb_typeof(sa.filter_state -> 'comp') = 'object'
                            UNION ALL
                            SELECT 'propelio', key, value FROM jsonb_each(sa.filter_state -> 'propelio')
                              WHERE jsonb_typeof(sa.filter_state -> 'propelio') = 'object'
                        ) sections
                        WHERE jsonb_typeof(sa.filter_state) = 'object'
                        ON CONFLICT (area_id, field_key) DO NOTHING
                        """,
                    ),
                    ("idx_saved_parcels_user", "CREATE INDEX IF NOT EXISTS idx_saved_parcels_user ON saved_parcels (user_id)"),
                    ("drop_uq_saved_parcels_user_account", "DROP INDEX IF EXISTS uq_saved_parcels_user_account"),
                    ("idx_cached_jobs_expires", "CREATE INDEX IF NOT EXISTS idx_cached_jobs_expires ON cached_jobs (expires_at)"),
                    ("idx_cached_jobs_user", "CREATE INDEX IF NOT EXISTS idx_cached_jobs_user ON cached_jobs (user_id)"),
                    (
                        "cached_jobs_saved_area_id",
                        "ALTER TABLE cached_jobs ADD COLUMN IF NOT EXISTS saved_area_id TEXT REFERENCES saved_areas(area_id) ON DELETE SET NULL",
                    ),
                    (
                        "cached_jobs_propelio_sold_points",
                        "ALTER TABLE cached_jobs ADD COLUMN IF NOT EXISTS propelio_sold_points JSONB",
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
                    (
                        "stored_value_entries_table",
                        """
                        CREATE TABLE IF NOT EXISTS stored_value_entries (
                          area_id       TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
                          field_key     TEXT NOT NULL CHECK (field_key IN ('arv','tdpp','rehab_needed','mao_arv','tdpp_minus_mao_arv')),
                          numeric_value NUMERIC(14,0),
                          comment_text  TEXT,
                          client_seq    BIGINT NOT NULL DEFAULT 0,
                          updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                          PRIMARY KEY (area_id, field_key)
                        )
                        """,
                    ),
                    # K4 (2026-05-27 roadmap): add 'nbv' (New Build Value) to
                    # the allowed field_keys. NBV is the new user-typed input
                    # that drives TDPP via the calc nbv * 0.2 = tdpp. Existing
                    # TDPP values are preserved in the table; the calc only
                    # overrides displayed TDPP when NBV is set. Both steps are
                    # idempotent: the DROP locates the old field_key CHECK
                    # constraint by inspecting pg_constraint (no reliance on
                    # PostgreSQL's auto-generated name), and the ADD uses an
                    # explicit v2 name so re-runs are no-ops.
                    (
                        "stored_value_entries_drop_old_field_key_check",
                        """
                        DO $$
                        DECLARE
                            con_name TEXT;
                        BEGIN
                            SELECT conname INTO con_name
                            FROM pg_constraint
                            WHERE conrelid = 'stored_value_entries'::regclass
                              AND contype = 'c'
                              AND pg_get_constraintdef(oid) ILIKE '%field_key%'
                              AND pg_get_constraintdef(oid) NOT ILIKE '%nbv%'
                            LIMIT 1;
                            IF con_name IS NOT NULL THEN
                                EXECUTE 'ALTER TABLE stored_value_entries DROP CONSTRAINT ' || quote_ident(con_name);
                            END IF;
                        END$$;
                        """,
                    ),
                    (
                        "stored_value_entries_add_field_key_check_v2",
                        """
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_constraint
                                WHERE conname = 'stored_value_entries_field_key_check_v2'
                            ) THEN
                                ALTER TABLE stored_value_entries
                                ADD CONSTRAINT stored_value_entries_field_key_check_v2
                                CHECK (field_key IN ('arv','nbv','tdpp','rehab_needed','mao_arv','tdpp_minus_mao_arv'));
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


def _json_default(obj: Any) -> Any:
    """JSON fallback for psycopg2 Json(): coerce date/datetime to ISO strings.

    Fixes a long-standing silent persistence bug where query_sold_parcels
    returns datetime.date values for `sold_date`, which the default JSON
    encoder cannot serialize. Json() failed inside _persist_cached_job_sync,
    the failure was swallowed by the non-fatal try/except around it, and
    cached_jobs.sold_points consequently stayed empty for every job. The
    CSV Comp_* columns were never populated as a result.
    """
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def _json_safe(value: Any) -> Json:
    """psycopg2 Json wrapper that uses our date-aware default encoder."""
    return Json(value, dumps=lambda v: json.dumps(v, default=_json_default))


def _persist_cached_job_sync(
    job_id: str,
    user_id: int,
    rows: list[dict[str, Any]],
    sold_points: list[dict[str, Any]],
    polygon: list[list[float]],
    saved_area_id: str | None = None,
    propelio_sold_points: list[dict[str, Any]] | None = None,
) -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO cached_jobs (job_id, user_id, saved_area_id, rows, sold_points, propelio_sold_points, polygon, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now() + interval '{_JOB_TTL_SECONDS} seconds')
                ON CONFLICT (job_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    saved_area_id = COALESCE(EXCLUDED.saved_area_id, cached_jobs.saved_area_id),
                    rows = EXCLUDED.rows,
                    sold_points = EXCLUDED.sold_points,
                    propelio_sold_points = EXCLUDED.propelio_sold_points,
                    polygon = EXCLUDED.polygon,
                    expires_at = CASE
                        WHEN cached_jobs.expires_at IS NULL THEN NULL
                        ELSE now() + interval '{_JOB_TTL_SECONDS} seconds'
                    END
                """,
                (
                    job_id,
                    int(user_id),
                    saved_area_id,
                    _json_safe(rows),
                    _json_safe(sold_points),
                    _json_safe(propelio_sold_points) if propelio_sold_points is not None else None,
                    _json_safe(polygon),
                ),
            )
        conn.commit()
    finally:
        release_session_conn(conn)


def _load_propelio_sold_points(job_id: str) -> list[dict[str, Any]] | None:
    conn = get_session_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT propelio_sold_points
                FROM cached_jobs
                WHERE job_id = %s
                  AND (expires_at IS NULL OR expires_at > now())
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            value = row.get("propelio_sold_points")
            return value if isinstance(value, list) else None
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


# ─── Workspace ownership + parcel rating helpers (spec v2 2026-05-24) ──

def _assert_user_owns_area(area_id: str, user_id: int) -> None:
    """Raise 403 if the calling user doesn't own the saved area. Used by
    both /api/parcels/rate and /api/propelio/comp/rate to prevent
    cross-workspace rating mutation by anyone who knows an area_id.
    Per PARCEL_RATINGS_SPEC.md v2 §2A."""
    if not area_id or not user_id:
        raise HTTPException(status_code=400, detail="Missing area_id or user")
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM saved_areas WHERE area_id = %s AND user_id = %s LIMIT 1",
                (area_id, int(user_id)),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=403, detail="You don't own this workspace")
    finally:
        release_session_conn(conn)


# ─── Sprint 1 multi-user collab helpers (spec §2.3) ─────────────────────

def _assert_user_is_area_member(area_id: str, user_id: int) -> None:
    """Raise 403 if calling user isn't a member of this area. Owner falls back
    to saved_areas.user_id with lazy backfill, surviving rolling-deploy windows
    where an area was created on a pre-Sprint-1 Cloud Run instance and lacks
    an explicit owner membership row.
    Per docs/MULTIUSER_COLLAB_SPRINT1_SPEC.md §2.3 (Copilot S-5 catch)."""
    if not area_id or not user_id:
        raise HTTPException(status_code=400, detail="Missing area_id or user")
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM saved_area_members WHERE area_id = %s AND user_id = %s LIMIT 1",
                (area_id, int(user_id)),
            )
            if cur.fetchone() is not None:
                return
            # Lazy-backfill fallback: caller is the area's owner per saved_areas.user_id.
            cur.execute(
                "SELECT user_id FROM saved_areas WHERE area_id = %s AND user_id = %s LIMIT 1",
                (area_id, int(user_id)),
            )
            if cur.fetchone() is not None:
                cur.execute(
                    """
                    INSERT INTO saved_area_members (area_id, user_id, role, added_via)
                    VALUES (%s, %s, 'owner', 'backfill')
                    ON CONFLICT (area_id, user_id) DO NOTHING
                    """,
                    (area_id, int(user_id)),
                )
                conn.commit()
                return
            raise HTTPException(status_code=403, detail="You don't have access to this workspace")
    finally:
        release_session_conn(conn)


def _user_area_role(area_id: str, user_id: int) -> str | None:
    """Return 'owner' / 'editor' / None for the calling user's role in this
    area. Includes the same lazy-backfill fallback as _assert_user_is_area_member.
    Per docs/MULTIUSER_COLLAB_SPRINT1_SPEC.md §2.3."""
    if not area_id or not user_id:
        return None
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM saved_area_members WHERE area_id = %s AND user_id = %s LIMIT 1",
                (area_id, int(user_id)),
            )
            row = cur.fetchone()
            if row is not None:
                return str(row[0])
            # Lazy-backfill fallback: caller is the area's owner per saved_areas.user_id.
            cur.execute(
                "SELECT 1 FROM saved_areas WHERE area_id = %s AND user_id = %s LIMIT 1",
                (area_id, int(user_id)),
            )
            if cur.fetchone() is not None:
                cur.execute(
                    """
                    INSERT INTO saved_area_members (area_id, user_id, role, added_via)
                    VALUES (%s, %s, 'owner', 'backfill')
                    ON CONFLICT (area_id, user_id) DO NOTHING
                    """,
                    (area_id, int(user_id)),
                )
                conn.commit()
                return 'owner'
            return None
    finally:
        release_session_conn(conn)


def _load_parcel_ratings_for_workspace(
    workspace_id: str | None,
    view: str = "arv",
) -> dict[tuple[str, str], str]:
    """Return {(county_lower, account_num): rating} for the workspace, or
    {} if no workspace. Single SQL query — apply to features in-memory to
    avoid per-row joins across county-specific parcel tables.
    Per PARCEL_RATINGS_SPEC.md v2 §2D.

    Per-view ratings spec §3.3 / FMEA H2: view='arv' (default) reads the
    EXISTING parcel_ratings table (unchanged query, today's behavior). view
    in ('nbv','export') reads the additive parcel_ratings_views table
    WHERE view=%s, same workspace scoping. Chunk B only makes the function
    CAPABLE of per-view; all callers default to 'arv' (CSV's true per-view
    wiring is Chunk C; analyze's active-view wiring is Chunk E), so this
    is byte-for-byte today's behavior unless a caller passes a non-arv view.
    """
    if not workspace_id:
        return {}
    # Normalize + validate view (defensive; invalid → arv fallback rather than
    # 500, since this is a read helper, not a request handler).
    norm_view = str(view or "").strip().lower() or "arv"
    if norm_view not in ("arv", "nbv", "export"):
        norm_view = "arv"
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            if norm_view == "arv":
                # ARV path — EXISTING parcel_ratings table, unchanged query.
                cur.execute(
                    "SELECT county, account_num, rating FROM parcel_ratings WHERE workspace_id = %s",
                    (workspace_id,),
                )
            else:
                # NBV/Export path — additive parcel_ratings_views table.
                cur.execute(
                    "SELECT county, account_num, rating FROM parcel_ratings_views WHERE workspace_id = %s AND view = %s",
                    (workspace_id, norm_view),
                )
            return {(str(c).lower(), str(a)): str(r) for c, a, r in cur.fetchall()}
    finally:
        release_session_conn(conn)


def _load_parcel_ratings_by_view_for_workspace(
    workspace_id: str | None,
) -> dict[tuple[str, str], dict[str, str]]:
    """Return {(county_lower, account_num): {view: rating}} for the workspace's
    NON-ARV (nbv/export) parcel ratings, or {} if no workspace. One SQL query
    over the additive parcel_ratings_views table — the parcel analogue of the
    comp ratings_by_view Alt-D hydrate (load_comps_by_polygon, Chunk B).

    Per-view ratings spec §4 (Chunk E parcel display): ARV stays in
    `user_rating` via _load_parcel_ratings_for_workspace(view='arv'); this
    layers the per-view ratings so the frontend can project the active view's
    parcel marks without a cross-view bleed. Additive + backward-compatible:
    flag-off clients ignore the extra `ratings_by_view` property. Parcels with
    no per-view rating simply have no key here → the caller defaults to {}.
    """
    if not workspace_id:
        return {}
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT county, account_num, view, rating
                FROM parcel_ratings_views
                WHERE workspace_id = %s AND view IN ('nbv', 'export')
                """,
                (workspace_id,),
            )
            out: dict[tuple[str, str], dict[str, str]] = {}
            for c, a, v, r in cur.fetchall():
                out.setdefault((str(c).lower(), str(a)), {})[str(v)] = str(r)
            return out
    finally:
        release_session_conn(conn)


class ParcelRateRequest(BaseModel):
    saved_area_id: str
    county: str
    account_num: str
    rating: str | None = None  # 'good', 'bad', or None to clear
    # Per-view ratings spec §3.1 (FMEA C2): absent/None → 'arv' (coexistence
    # with old flag-off clients). Present-but-invalid → 400 (never silent ARV).
    view: Optional[Literal["arv", "nbv", "export"]] = None  # 'arv' | 'nbv' | 'export' | None


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


def _county_from_division(division_cd: Any) -> str:
    """Single source of truth for division_cd → county string.
    Used by _row_county (dict input) and the save_verification fast-path
    (raw division string from a thin JSONB query). Keep helpers unified
    here so the two callers can't drift.
    """
    division = str(division_cd or "").strip().upper()
    if division == "TAD":
        return "tad"
    if division == "COLLIN":
        return "collin"
    if division == "DENTON":
        return "denton"
    return "dcad"


def _row_county(row: dict[str, Any]) -> str:
    """Returns the canonical county string for a row dict. Delegates to
    _county_from_division — kept as a thin wrapper for the many existing
    callers that pass row dicts."""
    return _county_from_division(row.get("division_cd"))


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


def _build_features_from_rows(
    rows: list[dict[str, Any]],
    area_id: str | None = None,
    user: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Rebuild GeoJSON features from cached raw parcel rows.

    Used when loading a pinned session from cached_jobs — avoids a fresh CAD
    query while still returning the same feature shape as /api/analyze.
    Redfin signal is not available from cache, so on_redfin=False for all rows.

    Per PARCEL_RATINGS_SPEC.md v2 §2E: when area_id is provided, hydrate
    user_rating on each feature from parcel_ratings. Backwards-compatible —
    callers that don't pass area_id get no rating hydrate (None default).

    Mailer + Phone Tracking (2026-06-03): when user is provided and their
    role is not power_user / owner / developer, strip outreach_* keys from
    each feature's properties before returning. user=None preserves outreach
    keys (callers that don't know about user must not be public-facing).
    """
    # Re-hydrate outreach data from live DB on every session restore /
    # share-link open. Cached jobs hold rows snapshotted at the initial
    # bbox query — stale by the time a second user opens the share link
    # or the same user re-loads after editing. See KK preview smoke
    # 2026-06-03 for the user-visible bug this fixes.
    _hydrate_outreach_for_rows(rows, user)
    # Flood enrichment shipped 2026-06-06 — older cached_jobs rows pre-date
    # it and don't carry flood fields. Re-hydrate from the live spatial
    # join here too so saved-area replays show the same data as fresh
    # /api/analyze calls.
    _hydrate_flood_for_rows(rows)

    exempt_set: set[str] = set()
    features: list[dict[str, Any]] = []
    # ARV ratings stay in user_rating (today's behavior, unchanged query).
    parcel_ratings_map: dict[tuple[str, str], str] = _load_parcel_ratings_for_workspace(area_id, view="arv")
    # Chunk E (per-view ratings spec §4): additionally layer the per-view
    # (nbv/export) parcel ratings so the frontend projects the active view's
    # marks without ARV bleeding across views. Additive + backward-compatible.
    parcel_ratings_by_view: dict[tuple[str, str], dict[str, str]] = _load_parcel_ratings_by_view_for_workspace(area_id)
    counts: dict[str, int] = {
        "active": 0, "off_market": 0, "multifamily": 0, "duplexes": 0,
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
        elif prop_type == "duplexes":
            counts["duplexes"] += 1
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
            _rating_key = (
                feature["properties"]["source_county"],
                str(feature["properties"].get("account_num", "") or "").strip(),
            )
            feature["properties"]["user_rating"] = parcel_ratings_map.get(_rating_key)
            # Per-view ratings (nbv/export); {} when none → frontend projects
            # null for non-ARV views (Chunk E). ARV is user_rating above.
            feature["properties"]["ratings_by_view"] = parcel_ratings_by_view.get(_rating_key, {})
            features.append(feature)
        except Exception:
            continue
    # Outreach role gate (Mailer + Phone Tracking, 2026-06-03). Strip
    # outreach_* keys for users below power_user. No-op when user is None
    # (caller doesn't know about user — internal-only path).
    _strip_outreach_from_features(features, user)
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


def _load_cached_job_metadata(job_id: str, user_id: int) -> tuple[Any, int] | None:
    """Thin point-lookup for (polygon, parcel_count) — no row hydration.

    Returns None if the cached job doesn't exist OR if the user_id doesn't
    match. Mirrors the user-id gate that _get_job applies. Used by the
    save_verification fast-path to skip the expensive jsonb deserialization
    of the full rows blob when we only need the polygon + count.
    """
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT polygon, COALESCE(jsonb_array_length(rows), 0) AS parcel_count
                FROM cached_jobs
                WHERE job_id = %s AND user_id = %s
                """,
                (job_id, int(user_id)),
            )
            row = cur.fetchone()
            if row is None:
                return None
            polygon_val = row[0]
            parcel_count = int(row[1] or 0)
            return polygon_val if polygon_val is not None else [], parcel_count
    finally:
        release_session_conn(conn)


def _load_cached_account_county_pairs(job_id: str, user_id: int) -> list[tuple[str, str]]:
    """Stream (account_num, county) tuples for every parcel in a cached job.

    Uses LATERAL jsonb_array_elements server-side so only the two narrow
    fields cross the wire — never the ~50-key per-parcel row dict. Generic
    naming: reusable by any future endpoint that needs account/county
    pairs from cached_jobs.rows without the full payload.

    Performance note: this trades client-side JSON decode of a multi-MB
    blob for ~N narrow tuple rows over the wire. For an 11k-parcel job,
    typically ~5-10x faster on cold Cloud Run instances. See
    docs/SAVE_VERIFICATION_FAST_PATH_SPEC.md.
    """
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    el->>'account_num'                AS account_num,
                    COALESCE(el->>'division_cd', '')  AS division_cd
                FROM cached_jobs c,
                LATERAL jsonb_array_elements(c.rows) AS el
                WHERE c.job_id = %s AND c.user_id = %s
                """,
                (job_id, int(user_id)),
            )
            pairs: list[tuple[str, str]] = []
            for account_num_raw, division_cd_raw in cur:
                account_num = str(account_num_raw or "").strip()
                if not account_num:
                    continue
                pairs.append((account_num, _county_from_division(division_cd_raw)))
            return pairs
    finally:
        release_session_conn(conn)


def _counties_from_pairs(pairs: list[tuple[str, str]]) -> list[str]:
    """Returns sorted unique county codes from a list of (account, county)
    tuples. Parallel to _counties_from_rows but operates on the thin-query
    output of _load_cached_account_county_pairs."""
    return sorted({county for _, county in pairs if county})


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


class StoredValueGetResponse(BaseModel):
    arv: dict[str, Any]
    # K4 NBV fix (2026-05-29): nbv was added to the schema, the field_key
    # whitelist, and the frontend list when K4 shipped, but NOT to this
    # response model. Pydantic silently drops unknown fields when
    # constructed via **payload, so the GET (and PUT) response returned
    # JSON with no 'nbv' key — frontend `data["nbv"]` was always undefined,
    # state.nbv stayed at its blank default on reload, and the UI showed
    # NBV as wiped after every refresh even though the DB row was correct.
    nbv: dict[str, Any]
    tdpp: dict[str, Any]
    rehab_needed: dict[str, Any]
    mao_arv: dict[str, Any]
    tdpp_minus_mao_arv: dict[str, Any]


class OutreachPutRequest(BaseModel):
    """Manual outreach edit from the popup (Mailer + Phone Tracking v2, 2026-06-03).

    Partial-update semantics: omitted keys are not touched. To explicitly
    clear a field, send the literal null/None.

    v2 model: phone_number removed (Mike's phones live in his CRM), mailer_sent
    boolean removed (mailer_date IS NOT NULL replaces its semantic). New
    contact_info_retrieved boolean indicates whether outreach prep is done.
    """
    county: Literal["dcad", "tad", "collin", "denton"]
    parcel_id: str
    contact_info_retrieved: bool | None = None
    mailer_date: str | None = None  # ISO 'YYYY-MM-DD' or None
    # _set fields tell the server WHICH keys were intentionally supplied
    # vs simply absent. Pydantic doesn't distinguish missing-from-payload
    # from sent-as-None natively in v1 — these booleans make it explicit.
    contact_info_retrieved_set: bool = False
    mailer_date_set: bool = False


class StoredValuePutRequest(BaseModel):
    # K4 (2026-05-27): added 'nbv'. 'tdpp' kept in the whitelist (frontend
    # still PUTs tdpp for comment-only edits even though numeric_value is
    # now calc-driven — see put_area_stored_value below).
    field_key: Literal["arv", "nbv", "tdpp", "rehab_needed", "mao_arv", "tdpp_minus_mao_arv"]
    numeric_value: int | None = Field(default=None, ge=0, le=999_999_999)
    comment_text: str | None = None
    client_seq: int


# Sprint 2 multi-user collab: per-field filter PATCH request/response.
# Per docs/MULTIUSER_COLLAB_SPRINT2_SPEC.md v2 §3.
class FilterFieldPatchRequest(BaseModel):
    value: Any  # JSON-serializable: bool, int, str, None, or JSON-compatible object
    client_seq: int


class FilterFieldPatchResponse(BaseModel):
    area_id: str
    field_key: str
    value: Any
    client_seq: int
    updated_at: str | None
    updated_by_user_id: int | None


class SavedAreaUpdateRequest(BaseModel):
    name: str | None = None
    filter_state: dict[str, Any] | None = None
    type: Literal["area", "location"] | None = None
    lat: float | None = None
    lng: float | None = None
    originator_parcel_county: str | None = None
    originator_parcel_account_num: str | None = None
    polygon: list[list[float]] | None = None


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


_STORED_VALUE_FIELD_KEYS: tuple[str, ...] = (
    "arv",
    "nbv",  # K4 (2026-05-27): new manual field; auto-fills tdpp via frontend × 0.2
    "tdpp",
    "rehab_needed",
    "mao_arv",
    "tdpp_minus_mao_arv",
)
# K4: 'nbv' added as a regular manual field. tdpp stays manual — NBV is
# an auto-fill helper on the frontend, not a calc-driver. This keeps
# Mike's existing TDPP values safe and lets him override TDPP after NBV
# is set (per Option B product call 2026-05-27).
_STORED_VALUE_MANUAL_FIELD_KEYS: tuple[str, ...] = ("arv", "nbv", "tdpp", "rehab_needed")
_STORED_VALUE_CALC_FIELD_KEYS: tuple[str, ...] = ("mao_arv", "tdpp_minus_mao_arv")


# Sprint 2 multi-user collab: allowlist of field keys accepted by
# PATCH /api/areas/{id}/filter-fields/{field_key}. Mirrors the structure
# of captureFilterState() in frontend/map.js. Enumerated in
# docs/MULTIUSER_COLLAB_SPRINT2_SPEC.md §2.1.A.
# Test: tests/test_multiuser_collab_sprint2_allowlist_drift.py guards
# against frontend additions that miss this allowlist.
_FILTER_FIELD_BASE_KEYS: frozenset[str] = frozenset({
    # checkboxes (9)
    "checkboxes.active", "checkboxes.sold", "checkboxes.off_market",
    "checkboxes.vacant", "checkboxes.multifamily", "checkboxes.duplexes",
    "checkboxes.commercial", "checkboxes.exempt",
    "checkboxes.contact_status",
    # numeric Property Filters (8)
    "numeric.lot_sqft_min", "numeric.lot_sqft_max",
    "numeric.appr_val_min", "numeric.appr_val_max",
    "numeric.yr_built_min", "numeric.yr_built_max",
    "numeric.sqft_min",     "numeric.sqft_max",
    # sold (5)
    "sold.maxDaysAgo",  "sold.minPrice", "sold.maxPrice",
    "sold.minYearBuilt", "sold.maxYearBuilt",
    # comp Comp Filters (8)
    "comp.lot_sqft_min", "comp.lot_sqft_max",
    "comp.appr_val_min", "comp.appr_val_max",
    "comp.yr_built_min", "comp.yr_built_max",
    "comp.sqft_min",     "comp.sqft_max",
    # propelio (23) — includes the 6 parcelType* mirrors of the parcel-side
    # checkboxes synthesized by readPropelioFiltersFromUI() at capture time
    # (frontend/map.js:7146-7151). They're derived/redundant with
    # checkboxes.* but persist into filter_state because the capture spread
    # writes them. Allowlisted so PATCH doesn't 400 on toggle. Smoke-test
    # 2026-06-02 caught this on Mike's prod DB drift inspection.
    "propelio.months",  "propelio.range",
    "propelio.statusSold", "propelio.statusActive", "propelio.statusPending",
    "propelio.showOutsideArea", "propelio.soldWithinDays",
    "propelio.lotMin",  "propelio.lotMax",
    "propelio.sqftMin", "propelio.sqftMax",
    "propelio.yearMin", "propelio.yearMax",
    "propelio.priceMin", "propelio.priceMax",
    "propelio.sortMode",
    "propelio.neighborhood",
    "propelio.parcelTypeMultifamily", "propelio.parcelTypeDuplexes",
    "propelio.parcelTypeCommercial",  "propelio.parcelTypeVacant",
    "propelio.parcelTypeExempt",      "propelio.parcelTypeOffMarket",
})

# Derive view-prefixed keys for the ARV · NBV · Export toggle (Feature #3).
# Each base key K also gets _views.nbv.K and _views.export.K, built
# programmatically so the two sets can never drift.
_FILTER_FIELD_KEYS: frozenset[str] = frozenset(
    _FILTER_FIELD_BASE_KEYS
    | {f"_views.{v}.{k}" for v in ("nbv", "export") for k in _FILTER_FIELD_BASE_KEYS}
)


def compute_calc_fields(manual_map: dict[str, int | None]) -> dict[str, int | None]:
    arv_raw = manual_map.get("arv")
    tdpp_raw = manual_map.get("tdpp")
    rehab_raw = manual_map.get("rehab_needed")

    arv = Decimal(arv_raw) if arv_raw is not None else None
    tdpp = Decimal(tdpp_raw) if tdpp_raw is not None else None
    rehab = Decimal(rehab_raw) if rehab_raw is not None else None

    mao = (arv * Decimal("0.75") - rehab) if arv is not None and rehab is not None else None
    tdpp_minus_mao = (tdpp - mao) if tdpp is not None and mao is not None else None

    def _to_int(value: Decimal | None) -> int | None:
        if value is None:
            return None
        return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    return {
        "mao_arv": _to_int(mao),
        "tdpp_minus_mao_arv": _to_int(tdpp_minus_mao),
    }


def _stored_value_serialize_payload(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {
        key: {
            "numeric_value": None,
            "comment_text": "",
            "value_source": "manual" if key in _STORED_VALUE_MANUAL_FIELD_KEYS else "calculated",
            "client_seq": 0,
        }
        for key in _STORED_VALUE_FIELD_KEYS
    }

    for row in rows:
        field_key = str(row.get("field_key") or "").strip().lower()
        if field_key not in payload:
            continue
        numeric_raw = row.get("numeric_value")
        numeric_value = int(numeric_raw) if numeric_raw is not None else None
        comment_text = str(row.get("comment_text") or "")
        client_seq = int(row.get("client_seq") or 0)
        payload[field_key] = {
            "numeric_value": numeric_value,
            "comment_text": comment_text,
            "value_source": "manual" if field_key in _STORED_VALUE_MANUAL_FIELD_KEYS else "calculated",
            "client_seq": client_seq,
        }

    manual_map = {
        "arv": payload["arv"]["numeric_value"],
        "tdpp": payload["tdpp"]["numeric_value"],
        "rehab_needed": payload["rehab_needed"]["numeric_value"],
        # K4 (2026-05-27): nbv is in the manual-field set but not used by
        # any backend calc — it's purely a stored helper. The frontend
        # auto-fills tdpp from nbv on the input side.
    }
    calc_fields = compute_calc_fields(manual_map)
    for calc_key in _STORED_VALUE_CALC_FIELD_KEYS:
        payload[calc_key]["numeric_value"] = calc_fields.get(calc_key)
        payload[calc_key]["value_source"] = "calculated"

    return payload


def _stored_value_get_rows(conn: Any, area_id: str) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT field_key, numeric_value, comment_text, client_seq
            FROM stored_value_entries
            WHERE area_id = %s
            """,
            (area_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def _stored_value_build_response(conn: Any, area_id: str) -> StoredValueGetResponse:
    rows = _stored_value_get_rows(conn, area_id)
    payload = _stored_value_serialize_payload(rows)
    return StoredValueGetResponse(**payload)


def _stored_value_csv_cells(payload: dict[str, dict[str, Any]]) -> list[str]:
    def _num_str(field_key: str) -> str:
        value = payload.get(field_key, {}).get("numeric_value")
        return "" if value is None else str(int(value))

    def _comment_str(field_key: str) -> str:
        return str(payload.get(field_key, {}).get("comment_text") or "")

    return [
        _num_str("arv"),
        _comment_str("arv"),
        _num_str("tdpp"),
        _comment_str("tdpp"),
        _num_str("rehab_needed"),
        _comment_str("rehab_needed"),
        _num_str("mao_arv"),
        _comment_str("mao_arv"),
        _num_str("tdpp_minus_mao_arv"),
        _comment_str("tdpp_minus_mao_arv"),
        # K4 (2026-05-27): NBV cells APPENDED at end of stored-values block
        # so existing columns 1-10 within the block are unchanged. CSV column
        # count grows by 2 overall.
        _num_str("nbv"),
        _comment_str("nbv"),
    ]


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


def _additional_owner_cells(additional_owners: list[dict] | None) -> tuple[str, str, str, str]:
    """Return (owner2_name, owner2_pct, owner3_name, owner3_pct) cells.

    Per spec v4a.2 §2.8 — only seqs 2 and 3 surface in CSV. multi_owner
    can have more, but DCAD caps at seq=3 in practice. Empty strings
    fill missing seqs so cell count stays fixed.
    """
    seqs: dict[int, dict] = {}
    for entry in (additional_owners or []):
        seq = entry.get("seq")
        if isinstance(seq, int):
            seqs[seq] = entry

    def _name(seq: int) -> str:
        return (seqs.get(seq, {}).get("name") or "").strip()

    def _pct(seq: int) -> str:
        v = seqs.get(seq, {}).get("pct")
        return f"{v:.2f}" if isinstance(v, (int, float)) else ""

    return _name(2), _pct(2), _name(3), _pct(3)


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


def _is_truly_empty_parcel(row: dict[str, Any]) -> bool:
    """A parcel with no usable descriptive data — exclude from /api/analyze.

    These are GIS-pipeline parcels: CAD staff drew the polygon but the
    appraisal team hasn't entered the tax record yet. No owner to
    contact, no value to assess, no legal description. Mike's workflow
    can't act on them. Once the CAD completes data entry and we
    re-ingest, they'll auto-populate via UPSERT and reappear.
    """
    addr = _clean_text(row.get("property_address")) or _clean_text(row.get("addr"))
    owner = _clean_text(row.get("owner_name"))
    legal = _clean_text(row.get("legal1")) or _clean_text(row.get("legal_descr"))
    tot_val = _safe_float(row.get("tot_val")) or _safe_float(row.get("total_value"))
    cert_val = _safe_float(row.get("cert_total_value"))
    return (
        not addr
        and not owner
        and not legal
        and (tot_val is None or tot_val <= 0)
        and (cert_val is None or cert_val <= 0)
    )


def _run_propelio_query_with_conn(parcel_input: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """Run propelio sold-by-parcels query with session connection management."""
    conn = get_session_conn()
    try:
        return query_propelio_sold_for_parcels(conn, parcel_input)
    finally:
        release_session_conn(conn)


_CSV_COUNTY_SOURCE_MAP = {
    "dcad": "DCAD",
    "tad": "TAD",
    "collin": "Collin",
    "denton": "Denton",
    "dallas": "DCAD",
    "tarrant": "TAD",
    # divison_cd variants written by per-county ingests:
    "RES": "DCAD",
    "COM": "DCAD",
    "TAD": "TAD",
    "COLLIN": "Collin",
    "DENTON": "Denton",
    "COLLIN_CAD": "Collin",
}


def _csv_county_source(row: dict[str, Any]) -> str:
    """Return the PascalCase County Source enum value for a CSV row.
    Tries source_county first (the canonical feature.properties key set by
    /api/analyze), then division_cd (set by per-county ingests), then
    falls back to "". Phase 2 CSV column 2 — drives the 'County Source'
    cell so analysts know which CAD a row's data came from.
    """
    for key in ("source_county", "division_cd"):
        raw = str(row.get(key) or "").strip()
        if not raw:
            continue
        # Try direct map, then lowercase.
        mapped = _CSV_COUNTY_SOURCE_MAP.get(raw) or _CSV_COUNTY_SOURCE_MAP.get(raw.lower())
        if mapped:
            return mapped
    return ""


def _fetch_ownership_history(
    account_keys: set[tuple[str, str]],
) -> dict[tuple[str, str], dict]:
    """Batched lookup of multi-county owner history for the given keys.

    `account_keys` is a set of (county_lower, account_num) pairs — the
    pair, not the account_num alone, is the unique key. Two counties can
    legitimately share an account_num string for unrelated parcels, so
    keying by account_num alone would silently merge ownership timelines
    across counties. Callers always know the parcel's county at gather
    time (via _csv_county_source or parcel_county), so passing the pair
    is cheap.

    Returns {(county_lower, account_num): {
        "owners":     {year: owner_name},
        "deed_dates": {year: deed_txfr_date|None},
        "acquired":   date|None,
    }}.

    `owners` and `deed_dates` are per-year maps for the snapshot_years
    present in `ownership_snapshots` for that account. `acquired` is the
    deed_txfr_date of the latest snapshot_year present (kept for v1 cell-
    builder back-compat). v2 callers pair the Current Owner's displayed
    deed_txfr_date by reading `deed_dates[anchor_year]` after
    `_distill_distinct_owners()` resolves which year anchors the current
    owner — which may not be the latest year if the latest year's
    owner_name is blank.

    Returns {} on any DB error (e.g. table not yet created) so the CSV
    export never breaks on missing history.
    """
    if not account_keys:
        return {}
    try:
        conn = get_conn()
    except Exception as exc:
        logger.warning("ownership history: get_conn failed (%s); emitting blanks", exc)
        return {}
    try:
        out: dict[tuple[str, str], dict] = {}
        # Group by county so each query hits the composite PK index
        # (county, account_num, snapshot_year) cleanly with one ANY() per
        # county rather than mixing counties in a tuple-IN clause.
        by_county: dict[str, list[str]] = {}
        for county, acct in account_keys:
            by_county.setdefault(county, []).append(acct)

        with conn.cursor() as cur:
            for county, accts in by_county.items():
                cur.execute(
                    "SELECT account_num, snapshot_year, owner_name, deed_txfr_date "
                    "FROM ownership_snapshots "
                    "WHERE county = %s AND account_num = ANY(%s)",
                    (county, accts),
                )
                for acct, year, owner, deed in cur.fetchall():
                    key = (county, str(acct))
                    rec = out.setdefault(
                        key,
                        {"owners": {}, "deed_dates": {}, "acquired": None, "_max": -1},
                    )
                    rec["owners"][int(year)] = owner or ""
                    rec["deed_dates"][int(year)] = deed
                    if int(year) > rec["_max"]:
                        rec["_max"] = int(year)
                        rec["acquired"] = deed
        return out
    except Exception as exc:
        # A missing table (before the ingest runs) is benign and expected during
        # the deploy-before-ingest window — warn (no full traceback), don't spam.
        # pgcode 42P01 = undefined_table. Real errors still surface as warnings.
        if getattr(exc, "pgcode", None) == "42P01":
            logger.warning("ownership_snapshots table not present yet; emitting blanks")
        else:
            logger.warning("ownership history lookup failed (%s); emitting blanks", exc)
        try:
            conn.rollback()  # don't return a poisoned connection to the pool
        except Exception:
            pass
        return {}
    finally:
        release_conn(conn)


# v2 (2026-06-01): distinct-owners-most-recent-first helpers for the
# ownership-history CSV columns. See
# docs/superpowers/specs/2026-06-01-dcad-ownership-history-v2-design.md.

_OWNER_NAME_PUNCT_RE = re.compile(r"[,.'()]")
_OWNER_NAME_WS_RE = re.compile(r"\s+")


def _normalize_owner_name(name: str | None) -> str:
    """Normalize an owner_name for distinct-owner grouping.

    Trim + uppercase + collapse internal whitespace + strip punctuation
    (commas, periods, apostrophes, parens), and replace '&' with 'AND'.
    Suffixes like LLC/INC/TR/TRUST/EST are deliberately NOT stripped —
    those represent real ownership flips (estate planning, probate) per
    the 2026-05-28 spike validation.

    Returns "" for None / blank input.
    """
    if not name:
        return ""
    s = str(name).strip()
    if not s:
        return ""
    s = s.upper().replace("&", " AND ")
    s = _OWNER_NAME_PUNCT_RE.sub("", s)
    s = _OWNER_NAME_WS_RE.sub(" ", s).strip()
    return s


def _distill_distinct_owners(per_year_owners: dict[int, str]) -> list[tuple[str, int]]:
    """Distill {year: owner_name} into distinct ownership periods, most-recent-first.

    Each entry is (display_name, earliest_year_in_run). `display_name` is
    the actual stored owner_name from the LATEST year in the run (latest
    spelling wins, trimmed of leading/trailing whitespace but otherwise
    preserved as stored). `earliest_year_in_run` is the earliest
    CALENDAR-CONSECUTIVE year whose normalized name matches.

    Blank/whitespace-only owner_name years are SKIPPED — they don't
    anchor a run, don't contribute to grouping, and are invisible to
    the output.

    Grouping uses _normalize_owner_name() for equality. Calendar-
    consecutive years are required. If per_year_owners = {2024: 'A',
    2022: 'A'} with no 2023 entry, the result is TWO entries for A — the
    gap breaks the run.

    Returns [] for empty input or all-blank input.
    """
    if not per_year_owners:
        return []
    cleaned: list[tuple[int, str, str]] = []
    for year, raw_name in per_year_owners.items():
        norm = _normalize_owner_name(raw_name)
        if not norm:
            continue
        display = str(raw_name).strip()
        cleaned.append((int(year), display, norm))
    if not cleaned:
        return []
    cleaned.sort(key=lambda r: r[0], reverse=True)
    result: list[tuple[str, int]] = []
    cur_norm: str | None = None
    cur_display: str = ""
    cur_earliest: int = 0
    for year, display, norm in cleaned:
        if cur_norm is None:
            cur_norm = norm
            cur_display = display
            cur_earliest = year
        elif norm == cur_norm and year == cur_earliest - 1:
            cur_earliest = year
        else:
            result.append((cur_display, cur_earliest))
            cur_norm = norm
            cur_display = display
            cur_earliest = year
    if cur_norm is not None:
        result.append((cur_display, cur_earliest))
    return result


def _is_owner_history_supported(county_or_source: str | None) -> bool:
    """True if this county has ingested rows in `ownership_snapshots`.

    The v2 distinct-owners CSV columns + popup display read from
    `ownership_snapshots`. A county only appears here once we've ingested
    its certified rolls; this gate keeps the CSV emitting blank cells
    rather than misleading "Current Owner = (None)" for counties whose
    data is not yet loaded. Extend the set when a new county is ingested.

    Supported (data ingested 2026-06-01):
      * dcad / dallas    — DCAD Certified Data (2021-2025)
      * collin           — Collin CAD via data.austintexas.gov (2020-2025)
      * denton           — Denton CAD PACS Appraisal Export (2020-2025)
      * tad / tarrant    — TAD Standard Data PropertyData(Certified) (2021-2025)
    """
    return str(county_or_source or "").strip().lower() in {
        "dcad", "dallas", "collin", "denton", "tad", "tarrant",
    }


def _ownership_history_cells(hist: dict | None) -> list:
    """Render the 6 v2 ownership-history cells:
        [Current Owner, Current Owner Acquired, Prior Owner 1..4]

    `hist` is the per-account record from `_fetch_ownership_history`
    (with v2 `owners`, `deed_dates`, `acquired` keys), or None for
    non-DCAD / missing accounts. Always returns exactly 6 cells so
    header/parcel/orphan row widths stay aligned.

    Layout C, per docs/superpowers/specs/2026-06-01-dcad-ownership-history-v2-design.md:
      - Slot 0 (Current Owner): display_name of the first run from
        `_distill_distinct_owners` (the LATEST non-blank year's owner_name).
      - Slot 1 (Current Owner Acquired): the deed_txfr_date for the
        anchor year (the latest non-blank year), formatted MM/DD/YYYY;
        blank when null. The anchor year — which may not be the max
        snapshot year if the max is blank — is recovered from
        `hist["deed_dates"]` (added by the v2 lookup helper) so the date
        always pairs with the actually-displayed Current Owner.
      - Slots 2-5 (Prior Owner 1-4): up to 4 prior distilled runs as
        'NAME (~YYYY)' where YYYY = earliest_year_in_run. Older priors
        (5th+) are silently dropped (overflow handling deferred until
        1999-2020 ownership data lands).
    """
    if not hist:
        return ["", "", "", "", "", ""]
    per_year_owners = hist.get("owners") or {}
    distilled = _distill_distinct_owners(per_year_owners)
    if not distilled:
        return ["", "", "", "", "", ""]
    deed_dates = hist.get("deed_dates") or {}
    anchor_year = max(
        (y for y, n in per_year_owners.items() if str(n or "").strip()),
        default=None,
    )
    anchor_deed = deed_dates.get(anchor_year) if anchor_year is not None else None
    current_name = distilled[0][0]
    current_acquired = anchor_deed.strftime("%m/%d/%Y") if anchor_deed else ""
    prior_cells: list[str] = [
        f"{display} (~{earliest})" for display, earliest in distilled[1:5]
    ]
    while len(prior_cells) < 4:
        prior_cells.append("")
    return [current_name, current_acquired, *prior_cells]


def _google_maps_link(row: dict[str, Any]) -> str:
    property_address = str(row.get("property_address", "") or "").strip()
    street_num = str(row.get("street_num", "") or "").strip()
    full_street_name = str(row.get("full_street_name", "") or "").strip()
    # Never fall back to owner_* — owner mailing address is often different
    # from the property (absentee owners). Better to omit than fabricate.
    # State defaults to TX since the entire footprint is TX-only.
    city = str(row.get("property_city", "") or "").strip()
    state = str(row.get("property_state", "") or "TX").strip()
    zip_code = str(row.get("property_zip", "") or "").strip()[:5]

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
    if path in {"/", "/health", "/health/db", "/version", "/auth/login"}:
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
            # Tier 2 (2026-06-16): offload the per-request auth DB read off the
            # event loop. get_current_user does a blocking psycopg2 lookup; on a
            # single-worker async server, calling it inline here serialized EVERY
            # request behind that query. to_thread runs it in the threadpool so
            # concurrent users don't block each other. AuthError still propagates.
            user = await asyncio.to_thread(get_current_user, request)
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

    path_only = request.url.path
    query_str = request.url.query or ""
    if path_only == "/" or path_only.endswith(".html"):
        response.headers["Cache-Control"] = "no-store"
    elif path_only.endswith((".js", ".css")):
        parsed_v = parse_qs(query_str).get("v", [""])[0].strip()
        if parsed_v:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
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
async def version_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Version"] = BUILD_ID

    client_version = request.headers.get("X-Client-Version", "").strip()
    if client_version and client_version != BUILD_ID:
        user_id = "anonymous"
        try:
            user = getattr(request.state, "current_user", None)
            if user:
                user_id = str(user.get("id") or user.get("username") or "unknown")
        except Exception:
            pass
        if _should_log_stale(user_id, client_version):
            print(json.dumps({
                "severity": "WARNING",
                "message": "stale_client",
                "clientVersion": client_version,
                "serverVersion": BUILD_ID,
                "path": request.url.path,
                "userId": user_id,
            }), flush=True)

    return response


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


@app.get("/version", include_in_schema=False)
async def version_endpoint() -> dict[str, str]:
    return {"build_id": BUILD_ID, "app_version": APP_VERSION}


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
                        p.property_city::text AS city,
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
                        t.property_city::text AS city,
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
                       p.owner_name2, p.biz_name,
                       p.owner_address_line1, p.owner_address_line2,
                       p.owner_address_line3, p.owner_address_line4,
                       p.owner_country,
                       p.street_half_num, p.bldg_id, p.unit_id, p.mapsco,
                       p.property_address, p.property_city, p.property_zip, p.division_cd,
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
                       a.city_jurisdiction_desc, a.county_jurisdiction_desc,
                       a.hospital_jurisdiction_desc, a.college_jurisdiction_desc,
                       a.special_dist_jurisdiction_desc,
                       a.city_taxable_val, a.county_taxable_val, a.isd_taxable_val,
                       a.hospital_taxable_val, a.college_taxable_val,
                       a.special_dist_taxable_val,
                       r.yr_built, r.tot_living_area, r.tot_main_sf,
                       -- v3 residential detail expansion (canonical key aliases
                       -- per CAD_RESIDENTIAL_DETAIL_EXPANSION_SPEC.md).
                       r.num_bedrooms AS beds,
                       r.num_full_baths AS full_baths,
                       r.num_half_baths AS half_baths,
                       r.num_fireplaces AS fireplaces,
                       r.num_kitchens AS kitchens,
                       r.num_wet_bars AS wet_bars,
                       r.num_units AS units,
                       r.eff_yr_built AS eff_yr_built,
                       r.act_age AS act_age,
                       r.pool_ind AS pool_flag,
                       r.spa_ind AS spa_flag,
                       r.sauna_ind AS sauna_flag,
                       r.sprinkler_sys_ind AS sprinkler_flag,
                       r.deck_ind AS deck_flag,
                       r.num_stories_desc AS stories_desc,
                       r.bldg_class_desc AS bldg_class,
                       r.cdu_rating_desc AS cdu_rating,
                       r.constr_fram_typ_desc AS construction_frame_type,
                       r.foundation_typ_desc AS foundation_type,
                       r.heating_typ_desc AS heating_type,
                       r.ac_typ_desc AS ac_type,
                       r.fence_typ_desc AS fence_type,
                       r.ext_wall_desc AS ext_wall,
                       r.basement_desc AS basement,
                       r.roof_typ_desc AS roof_type,
                       r.roof_mat_desc AS roof_material,
                       r.pct_complete AS pct_complete,
                      l.zoning, l.front_dim, l.depth_dim, l.area_size, l.area_uom, l.area_estimated,
                       (e.account_num IS NOT NULL) AS is_exempt_account,
                       pon.contact_info_retrieved AS outreach_contact_info_retrieved,
                       pon.mailer_date            AS outreach_mailer_date
                FROM parcels p
                LEFT JOIN parcel_outreach_notes pon
                       ON pon.county = 'dcad' AND pon.parcel_id = p.account_num
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

            parcel["additional_owners"] = _fetch_additional_owners(
                [account_num]
            ).get(account_num, [])

            # v4b §3 Rule A — compose canonical owner mailing address
            # from the last populated LINE1-4. Single-parcel mirror of
            # the same composition applied in query_parcels merged_rows.
            from api.counties.dcad import _compose_dcad_owner_address
            parcel["owner_address"] = _compose_dcad_owner_address(parcel)

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
                SELECT tad_parcels.parcel_key, tad_parcels.account_num, taxpin,
                       owner_name, owner_addr, owner_city, owner_citystate,
                       owner_zip, situs_addr, property_city, property_zip,
                       property_class, state_use_code,
                       legal_descr, school_code,
                       acres, land_acres, land_sqft,
                       year_built, living_area,
                       land_value, improvement_value, total_value,
                       pon.contact_info_retrieved AS outreach_contact_info_retrieved,
                       pon.mailer_date            AS outreach_mailer_date,
                       ST_Area(ST_OrientedEnvelope(geom)::geography) / NULLIF(ST_Area(geom::geography), 0) AS envelope_ratio,
                       ST_Perimeter(ST_OrientedEnvelope(geom)::geography) * 3.28084 AS envelope_perim_ft,
                       ST_Area(ST_OrientedEnvelope(geom)::geography) * 10.763910416709722 AS envelope_area_sqft,
                       ST_AsGeoJSON(geom)::json AS polygon_geojson,
                       ST_Y(centroid) AS _lat,
                       ST_X(centroid) AS _lng
                FROM tad_parcels
                LEFT JOIN parcel_outreach_notes pon
                       ON pon.county = 'tad' AND pon.parcel_id = tad_parcels.parcel_key
                WHERE tad_parcels.account_num = %s
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
                    pon.contact_info_retrieved AS outreach_contact_info_retrieved,
                    pon.mailer_date            AS outreach_mailer_date,
                    ST_Area(ST_OrientedEnvelope(geom)::geography) / NULLIF(ST_Area(geom::geography), 0) AS envelope_ratio,
                    ST_Perimeter(ST_OrientedEnvelope(geom)::geography) * 3.28084 AS envelope_perim_ft,
                    ST_Area(ST_OrientedEnvelope(geom)::geography) * 10.763910416709722 AS envelope_area_sqft,
                    ST_Area(geom::geography) * 10.763910416709722 AS geom_sqft,
                    ST_AsGeoJSON(geom)::json AS polygon_geojson,
                    ST_Y(centroid) AS _lat,
                    ST_X(centroid) AS _lng
                FROM collin_parcels
                LEFT JOIN parcel_outreach_notes pon
                       ON pon.county = 'collin' AND pon.parcel_id = collin_parcels.parcel_key
                WHERE collin_parcels.account_num = %s
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
                    ST_Area(ST_Transform(ST_OrientedEnvelope(p.geom), 2276)) / NULLIF(ST_Area(ST_Transform(p.geom, 2276)), 0) AS envelope_ratio,
                    -- Phase 3 (2026-05-21): residential detail aliased from
                    -- denton_improvement_detail via LATERAL one-row pick.
                    -- Mirrors the bbox-filter SELECT in
                    -- api/counties/denton.py. Without this, popup clicks
                    -- on Denton parcels would still show N/A even after
                    -- the cached job refreshes.
                    d.foundation_type,
                    d.roof_material,
                    d.roof_type,
                    d.ext_wall,
                    d.heating_type,
                    d.ac_type,
                    d.beds,
                    d.fireplaces,
                    d.cdu_rating,
                    d.bldg_class,
                    d.sprinkler_flag,
                    d.plumbing_count,
                    d.interior_finish,
                    d.flooring,
                    d.eff_yr_built,
                    d.dropped_imprv_count,
                    d.raw_heating_cooling_code,
                    -- Feature-derived columns (Phase 3 patch 2026-05-21).
                    d.pool_flag,
                    d.deck_flag,
                    d.garage_capacity,
                    d.stories,
                    -- Phase 3 patch v3 (bath + room + outdoor + end_unit).
                    d.baths,
                    d.full_baths,
                    d.half_baths,
                    d.total_rooms,
                    d.outdoor_fireplaces,
                    d.end_unit,
                    pon.contact_info_retrieved AS outreach_contact_info_retrieved,
                    pon.mailer_date            AS outreach_mailer_date
                FROM denton_parcels p
                LEFT JOIN LATERAL (
                    SELECT *
                    FROM denton_improvement_detail
                    WHERE prop_id = p.account_num
                    LIMIT 1
                ) d ON TRUE
                LEFT JOIN parcel_outreach_notes pon
                       ON pon.county = 'denton' AND pon.parcel_id = p.parcel_key
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
async def get_parcel_near(
    lat: float,
    lng: float,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Nearest-parcel lookup by lat/lng coordinate.
    Used by address search to reliably find the parcel footprint at a geocoded point.
    Tries DCAD, TAD, Collin, then Denton using polygon containment + centroid proximity.
    Returns a GeoJSON Feature in the same shape as /api/parcel/{county}/{account_num}.

    Outreach gate: strips outreach_* keys before returning when the
    requesting user is below power_user (Mailer + Phone Tracking, 2026-06-03).
    """
    dcad_account = _find_dcad_near(lat, lng)
    if dcad_account:
        row, exempt_set = _fetch_dcad_parcel_by_account(dcad_account)
        if row is not None:
            prop_type = classify_parcel(row, exempt_set)
            feature = build_feature(row, prop_type, False, None)
            feature["properties"]["source_county"] = "dcad"
            _strip_outreach_from_feature(feature, user)
            return feature

    tad_account = _find_tad_near(lat, lng)
    if tad_account:
        row = _fetch_tad_parcel_by_account(tad_account)
        if row is not None:
            prop_type = _classify_tad(row)
            feature = build_feature(row, prop_type, False, None)
            feature["properties"]["source_county"] = "tad"
            _strip_outreach_from_feature(feature, user)
            return feature

    collin_account = _find_collin_near(lat, lng)
    if collin_account:
        row = _fetch_collin_parcel_by_account(collin_account)
        if row is not None:
            prop_type = _classify_collin(row)
            feature = build_feature(row, prop_type, False, None)
            feature["properties"]["source_county"] = "collin"
            _strip_outreach_from_feature(feature, user)
            return feature

    denton_account = _find_denton_near(lat, lng)
    if denton_account:
        row = _fetch_denton_parcel_by_account(denton_account)
        if row is not None:
            prop_type = _classify_denton(row)
            feature = build_feature(row, prop_type, False, None)
            feature["properties"]["source_county"] = "denton"
            _strip_outreach_from_feature(feature, user)
            return feature

    raise HTTPException(status_code=404, detail="No parcel found near this point")


_OWNER_HISTORY_POPUP_ROLES = {"developer", "owner", "power_user"}

# Mailer + Phone Tracking (2026-06-03). Same role set as owner-history —
# both are PII / outreach-tracking data restricted to power_user+. Regular
# users + members do NOT see these fields anywhere (popup, CSV, API).
_OUTREACH_ALLOWED_ROLES = {"developer", "owner", "power_user"}
# v2 (2026-06-03): drop outreach_phone (LotLedger doesn't store phones — they
# live in Mike's CRM), drop outreach_mailer_sent boolean (mailer_date IS NOT
# NULL replaces its semantic), add outreach_contact_info_retrieved.
_OUTREACH_FEATURE_KEYS = ("outreach_contact_info_retrieved", "outreach_mailer_date")


def _user_can_see_outreach(user: dict[str, Any] | None) -> bool:
    """Single source of truth for outreach visibility. Mirror of
    frontend `_isPowerUserOrAbove()` at map.js:12120. NOT the same as
    `_canDownloadCsv()` (which lets member through) — outreach is stricter.
    """
    role = str((user or {}).get("role") or "").strip().lower()
    return role in _OUTREACH_ALLOWED_ROLES


def _strip_outreach_from_features(features: list[dict[str, Any]], user: dict[str, Any] | None) -> None:
    """Remove outreach keys from feature properties when the requesting user
    is not allowed to see them. Mutates the features list in place. Defense-
    in-depth: even if frontend wouldn't render the data, we don't ship it
    over the wire to ineligible roles.
    """
    if _user_can_see_outreach(user):
        return
    for feature in features or []:
        props = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(props, dict):
            continue
        for key in _OUTREACH_FEATURE_KEYS:
            props.pop(key, None)


def _strip_outreach_from_feature(feature: dict[str, Any] | None, user: dict[str, Any] | None) -> None:
    """Single-feature variant of _strip_outreach_from_features. Same role
    semantics. No-op when feature is None or already stripped.
    """
    if feature is None:
        return
    _strip_outreach_from_features([feature], user)


def _row_outreach_key(row: dict[str, Any]) -> tuple[str, str] | None:
    """Return (county_lower, parcel_id) for a row, or None if not addressable.
    DCAD uses account_num; others use parcel_key. Mirror of frontend matchKey
    logic in _buildPanelOutreachHtml.

    KK reported bug 2026-06-03 PM: cached_jobs rows cache DCAD's raw
    division_cd as "RES" (Residential) or "COM" (Commercial) — NOT "DCAD".
    Raw string comparison against the {dcad,tad,collin,denton} set failed
    for cached rows → hydrate skipped → CSV outreach cells came back blank
    even though the DB had data. Fix: route through _csv_county_source
    which already knows the RES/COM → DCAD mapping (see
    _CSV_COUNTY_SOURCE_MAP), then lowercase its output.
    """
    pascal_county = _csv_county_source(row)  # handles "RES"/"COM" → "DCAD"
    county = pascal_county.lower()
    if county not in _OUTREACH_PARCEL_TABLES:
        return None
    if county == "dcad":
        parcel_id = str(row.get("account_num") or "").strip()
    else:
        parcel_id = str(row.get("parcel_key") or row.get("account_num") or "").strip()
    if not parcel_id:
        return None
    return (county, parcel_id)


def _hydrate_flood_for_rows(rows: list[dict[str, Any]]) -> None:
    """Mutate rows in place, populating flood_zone / flood_zone_subtype /
    flood_bfe via one batched centroid-based spatial join.

    Same architectural pattern as _hydrate_outreach_for_rows: cached parcel
    rows in cached_jobs were snapshotted BEFORE flood enrichment shipped
    (2026-06-06), so loading a saved area replays rows with no flood
    fields. Without this helper, every popup row reads N/A even though the
    DB has the data.

    Centroid is grabbed from row['lat'] / row['lng'] which every parcel
    row carries — same fallback used by _resolveParcelAnchor on the
    frontend. Single SQL query regardless of row count via unnest()
    arrays. Rows whose centroid lands in no FEMA polygon get empty
    flood_zone (frontend then displays "N/A").
    """
    if not rows:
        return

    # Collect (idx, lng, lat) for rows that have a centroid.
    candidates: list[tuple[int, float, float]] = []
    for idx, row in enumerate(rows):
        lat = row.get("lat")
        lng = row.get("lng")
        try:
            lat_f = float(lat) if lat is not None else None
            lng_f = float(lng) if lng is not None else None
        except (TypeError, ValueError):
            lat_f = lng_f = None
        if lat_f is None or lng_f is None:
            row["flood_zone"] = ""
            row["flood_zone_subtype"] = ""
            row["flood_bfe"] = None
            continue
        candidates.append((idx, lng_f, lat_f))

    if not candidates:
        return

    indices = [c[0] for c in candidates]
    lngs = [c[1] for c in candidates]
    lats = [c[2] for c in candidates]

    # Default everyone to empty; the SELECT below overrides matches.
    for idx in indices:
        rows[idx]["flood_zone"] = ""
        rows[idx]["flood_zone_subtype"] = ""
        rows[idx]["flood_bfe"] = None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (p.idx)
                       p.idx, f.fld_zone, f.zone_subty, f.static_bfe
                FROM (
                    SELECT unnest(%s::int[])     AS idx,
                           unnest(%s::float8[])  AS lng,
                           unnest(%s::float8[])  AS lat
                ) p
                JOIN flood_zones f
                  ON ST_Contains(
                       f.geom,
                       ST_SetSRID(ST_MakePoint(p.lng, p.lat), 4326)
                     )
                ORDER BY p.idx, ST_Area(f.geom) DESC
                """,
                (indices, lngs, lats),
            )
            for idx, fld_zone, zone_subty, static_bfe in cur.fetchall():
                row = rows[idx]
                row["flood_zone"] = fld_zone or ""
                row["flood_zone_subtype"] = zone_subty or ""
                row["flood_bfe"] = float(static_bfe) if static_bfe is not None else None
    except Exception as exc:
        logger.warning("Flood hydration failed (table missing?): %s", exc)
    finally:
        release_conn(conn)


def _hydrate_outreach_for_rows(rows: list[dict[str, Any]], user: dict[str, Any] | None) -> None:
    """Mutate rows in place, refreshing outreach_* fields from the live
    parcel_outreach_notes table.

    Mailer + Phone Tracking (2026-06-03 polish): cached jobs (CSV export,
    session restore, share link) hold rows snapshotted at the initial bbox
    query. If a user edits outreach via the popup PUT, the DB has new data
    but the cache has stale data. KK reported this in preview smoke — checked
    Mailer Sent in popup, exported CSV, cell was blank.

    Resolution: every cache-consumption site calls this helper to refresh
    outreach fields. Behavior:
      - user below power_user → clear outreach_* on every row (also serves
        as defense-in-depth strip when CSV writer or session restore feeds
        cached data)
      - user is power_user+ → query parcel_outreach_notes for all rows in
        one batch (WHERE (county, parcel_id) IN (...)), stamp fresh values
        onto rows.

    Single query regardless of row count. Rows with no matching outreach
    record get cleared outreach_* fields (so an UNSET parcel doesn't display
    stale data from when it was first fetched and the popup user then
    cleared the phone).
    """
    if not rows:
        return

    if not _user_can_see_outreach(user):
        for row in rows:
            for key in _OUTREACH_FEATURE_KEYS:
                row.pop(key, None)
        return

    # Build the list of unique (county, parcel_id) keys.
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        k = _row_outreach_key(row)
        if k is None or k in seen:
            continue
        seen.add(k)
        keys.append(k)

    if not keys:
        # Nothing to hydrate; clear any stale values to be safe.
        for row in rows:
            for key in _OUTREACH_FEATURE_KEYS:
                row[key] = None
        return

    # Single batched fetch.
    conn = get_conn()
    fresh: dict[tuple[str, str], tuple[bool, "date_module.date | None"]] = {}
    try:
        from psycopg2.extras import execute_values
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                SELECT pon.county, pon.parcel_id, pon.contact_info_retrieved, pon.mailer_date
                FROM parcel_outreach_notes pon
                JOIN (VALUES %s) AS v(county, parcel_id)
                  ON v.county = pon.county AND v.parcel_id = pon.parcel_id
                """,
                keys,
                template="(%s, %s)",
                page_size=1000,
            )
            for c, pid, contact_retrieved, mailer_date in cur.fetchall():
                fresh[(c, pid)] = (bool(contact_retrieved), mailer_date)
    finally:
        release_conn(conn)

    # Stamp values onto rows. Rows whose (county, parcel_id) isn't in the
    # fresh map have no outreach record — explicitly clear the cached fields
    # so any stale value is overwritten.
    for row in rows:
        k = _row_outreach_key(row)
        if k is None:
            row["outreach_contact_info_retrieved"] = False
            row["outreach_mailer_date"] = None
            continue
        if k in fresh:
            contact_retrieved, mailer_date = fresh[k]
            row["outreach_contact_info_retrieved"] = contact_retrieved
            row["outreach_mailer_date"] = mailer_date.isoformat() if hasattr(mailer_date, "isoformat") and mailer_date else mailer_date
        else:
            row["outreach_contact_info_retrieved"] = False
            row["outreach_mailer_date"] = None


def _outreach_parcel_id_cell(row: dict[str, Any] | None) -> str:
    """Return the Parcel ID for one CSV row.

    v3 (2026-06-03 PM, Mike request): Parcel ID was pulled out of the
    right-edge outreach block and placed at column B (position 2). It's
    the FUB-match key, not outreach PII, so always emitted regardless of
    user role. row=None → empty string.

    Per-county PK lookup:
      DCAD     → row['account_num'] (17-char alphanumeric)
      TAD/Collin/Denton → row['parcel_key']
    All globally unique, no collisions across counties.
    """
    if not row:
        return ""
    county = _csv_county_source(row).lower()
    if county == "dcad":
        return str(row.get("account_num") or "").strip()
    return (
        str(row.get("parcel_key") or "").strip()
        or str(row.get("account_num") or "").strip()
    )


def _flood_zone_csv_cell(row: dict[str, Any] | None) -> str:
    """Format the Flood Zone CSV cell per spec popup-wording rules.

    AE / A / AH / AO with BFE: "AE (BFE 502.3 ft)"
    AE / A / AH / AO without BFE: "AE"
    FLOODWAY subtype: "AE — FLOODWAY (no build)"
    X (0.2 PCT subtype): "X — 500-yr floodplain"
    X (minimal-hazard subtype): "X — minimal risk"
    X bare: "X"
    Empty / no enrichment: ""
    """
    if not row:
        return ""
    zone = str(row.get("flood_zone") or "").strip()
    if not zone:
        return ""
    subty = str(row.get("flood_zone_subtype") or "").strip().upper()
    bfe = row.get("flood_bfe")
    if subty == "FLOODWAY":
        return f"{zone} — FLOODWAY (no build)"
    if zone in ("AE", "A", "AH", "AO", "V", "VE"):
        if bfe is not None:
            try:
                return f"{zone} (BFE {float(bfe):.1f} ft)"
            except (TypeError, ValueError):
                return zone
        return zone
    if zone == "X":
        if "0.2 PCT" in subty:
            return "X — 500-yr floodplain"
        if "MINIMAL" in subty:
            return "X — minimal risk"
        return "X"
    return zone


def _outreach_csv_cells(row: dict[str, Any] | None, user: dict[str, Any] | None) -> tuple[str, str]:
    """Return the 2 right-edge outreach CSV cells: (Contact Info Retrieved, Last Mailer Sent).

    v3 (2026-06-03 PM): Parcel ID moved out to column B via the separate
    _outreach_parcel_id_cell helper. This helper now only handles the role-
    gated outreach data.

    - Both cells blanked when the user is below power_user+ (Mike's outreach
      data is PII).
    - row=None (e.g. orphan comp with no matched CAD parcel) → 2 empty cells.
    """
    if not row:
        return ("", "")
    if not _user_can_see_outreach(user):
        return ("", "")

    contact_retrieved_raw = row.get("outreach_contact_info_retrieved")
    contact_retrieved_cell = "yes" if contact_retrieved_raw else ""
    mailer_date_raw = row.get("outreach_mailer_date")
    if hasattr(mailer_date_raw, "isoformat"):
        mailer_date_cell = mailer_date_raw.isoformat()
    elif mailer_date_raw:
        mailer_date_cell = str(mailer_date_raw)
    else:
        mailer_date_cell = ""
    return (contact_retrieved_cell, mailer_date_cell)


def _format_live_deed_for_popup(raw: Any) -> str:
    """Coerce a live deed_date (datetime.date | 'YYYY-MM-DD' str | '' | None)
    into the popup's MM/DD/YYYY display format. Returns "" when nothing
    usable. Tolerates Collin's string-format and Denton's date-object form."""
    import datetime as _dt
    if raw is None:
        return ""
    if isinstance(raw, _dt.date):
        return raw.strftime("%m/%d/%Y")
    text = str(raw).strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            parsed = _dt.datetime.strptime(text, fmt).date()
            return parsed.strftime("%m/%d/%Y")
        except ValueError:
            continue
    return text  # fall back to raw string if it doesn't match a known format


def _owner_history_for_popup(
    county_lower: str,
    account_num: str,
    user: dict[str, Any] | None,
    parcel_props: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the popup owner-history block, or None when the viewer isn't
    authorized / county isn't supported / no data exists.

    Gated to {developer, owner, power_user} per Mike's 2026-06-01 ask.

    Output shape (chain-oriented, reconciled against the live CAD owner):
        {
            "current_owner":    "...",                   # str
            "current_acquired": "MM/DD/YYYY" | "",       # str
            "previous":         ["NAME (~YYYY)", ...],   # up to 4 strings
        }

    Reconcile rule (Mike's preview-selected behavior 2026-06-01): if the
    live CAD `owner_name` (from parcel_props) differs from the snapshot's
    most-recent owner_name (after normalization), promote the live owner
    to `current_owner`, use the live `deed_date` for `current_acquired`,
    and shift the snapshot's "current" into the FIRST entry of
    `previous`. Resolves the "Prior Owner N" off-by-one that surfaces
    when a sale happened after the last certified roll.

    If they match (or parcel_props lacks an owner_name to compare), the
    block reads straight from the snapshot — same data as the CSV's
    Current Owner / Current Owner Acquired / Prior Owner 1..4 columns.
    """
    role = (user or {}).get("role")
    if role not in _OWNER_HISTORY_POPUP_ROLES:
        return None
    if not _is_owner_history_supported(county_lower):
        return None
    acct = (account_num or "").strip()
    if not acct:
        return None
    history = _fetch_ownership_history({(county_lower, acct)})
    hist = history.get((county_lower, acct))
    if not hist:
        return None
    per_year_owners = hist.get("owners") or {}
    distilled = _distill_distinct_owners(per_year_owners)
    # Snapshot distilled to nothing — every year was blank/whitespace.
    if not distilled:
        return None

    deed_dates = hist.get("deed_dates") or {}
    anchor_year = max(
        (y for y, n in per_year_owners.items() if str(n or "").strip()),
        default=None,
    )
    anchor_deed = deed_dates.get(anchor_year) if anchor_year is not None else None

    snap_current_name = distilled[0][0]
    snap_current_earliest = distilled[0][1]  # first year of the snapshot's "current" run
    snap_acquired_str = anchor_deed.strftime("%m/%d/%Y") if anchor_deed else ""
    snap_prior_runs = distilled[1:]  # [(name, earliest_year), ...] older runs

    def _fmt_prior(name: str, year: int) -> str:
        return f"{name} (~{year})"

    props = parcel_props or {}
    live_owner_raw = str(props.get("owner_name") or "").strip()
    snap_norm = _normalize_owner_name(snap_current_name) if snap_current_name else ""
    live_norm = _normalize_owner_name(live_owner_raw)

    # MATCH (or no live-owner info to compare): snapshot drives the block.
    if not live_norm or snap_norm == live_norm:
        return {
            "current_owner":    snap_current_name,
            "current_acquired": snap_acquired_str,
            "previous":         [_fmt_prior(n, y) for n, y in snap_prior_runs[:4]],
        }

    # MISMATCH: live owner is fresher than the latest certified roll.
    # Promote the live owner to the top of the chain and demote the
    # snapshot's "current" to the first "Previously" entry. The demoted
    # entry uses the snapshot's recorded deed-year when on file (more
    # accurate than the "first year we saw them" approximation older
    # priors fall back to), and the earliest-year-in-run otherwise.
    demoted_year = anchor_deed.year if anchor_deed is not None else snap_current_earliest
    demoted = _fmt_prior(snap_current_name, demoted_year)
    # Cap total previous entries at 4 — drop the oldest snapshot prior to
    # make room for the demoted snapshot current.
    return {
        "current_owner":    live_owner_raw,
        "current_acquired": _format_live_deed_for_popup(props.get("deed_date")),
        "previous":         [demoted, *[_fmt_prior(n, y) for n, y in snap_prior_runs[:3]]],
    }


@app.get("/api/parcel/{county}/{account_num}")
async def get_parcel_detail(
    county: str,
    account_num: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Single-parcel detail endpoint used by PMTiles click popups.

    Called by: frontend tile-layer click flow after queryTileFeaturesDebug returns
    account_num + source_county for the clicked parcel.

    Why it exists: PMTiles stores only minimal properties for fast rendering. This
    endpoint fetches the full parcel row from the live database and returns one
    GeoJSON feature in the same shape produced by /api/analyze.

    Owner-history cells (since 2026-06-01): when the requesting user is a
    superuser (developer/owner/power_user) AND the parcel's county has
    ingested owner-history rows in ownership_snapshots, attach the 6
    distinct-owners cells as `properties.owner_history_cells`. Frontend
    renders a popup section above Remarks; absence of the key hides it
    entirely.
    """
    county_key = county.strip().lower()
    if county_key not in {"dcad", "tad", "collin", "denton"}:
        raise HTTPException(status_code=400, detail="county must be 'dcad', 'tad', 'collin', or 'denton'")

    if county_key == "dcad":
        row, exempt_set = _fetch_dcad_parcel_by_account(account_num)
        if row is None:
            raise HTTPException(status_code=404, detail="Parcel not found")
        # KK debug 2026-06-06: per-parcel fetch helpers don't run the flood
        # spatial join the way query_parcels does (Phase 2 enrichment lives
        # in the per-county adapter's batch flow, not the single-parcel
        # helper). Every popup click was showing "Flood Zone: N/A" because
        # this endpoint is what powers fresh popup loads from PMTiles
        # browse mode. Hydrate one row, same helper used by saved-area
        # replays.
        _hydrate_flood_for_rows([row])
        prop_type = classify_parcel(row, exempt_set)
        feature = build_feature(row, prop_type, False, None)
        feature["properties"]["source_county"] = "dcad"
    elif county_key == "tad":
        row = _fetch_tad_parcel_by_account(account_num)
        if row is None:
            raise HTTPException(status_code=404, detail="Parcel not found")
        _hydrate_flood_for_rows([row])
        prop_type = _classify_tad(row)
        feature = build_feature(row, prop_type, False, None)
        feature["properties"]["source_county"] = "tad"
    elif county_key == "collin":
        row = _fetch_collin_parcel_by_account(account_num)
        if row is None:
            raise HTTPException(status_code=404, detail="Parcel not found")
        _hydrate_flood_for_rows([row])
        prop_type = _classify_collin(row)
        feature = build_feature(row, prop_type, False, None)
        feature["properties"]["source_county"] = "collin"
    else:
        row = _fetch_denton_parcel_by_account(account_num)
        if row is None:
            raise HTTPException(status_code=404, detail="Parcel not found")
        _hydrate_flood_for_rows([row])
        prop_type = _classify_denton(row)
        feature = build_feature(row, prop_type, False, None)
        feature["properties"]["source_county"] = "denton"

    block = _owner_history_for_popup(
        county_key, account_num, user, feature["properties"]
    )
    if block is not None:
        feature["properties"]["owner_history"] = block
    # Outreach role gate — strip if user is not power_user+.
    _strip_outreach_from_feature(feature, user)
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

    # Merge rows from all counties and enforce lowercase shortname county tags
    # for stable Propelio direct-join keying across all four counties.
    all_rows: list[dict[str, Any]] = []
    exempt_set: set[str] = set()
    if dcad_result:
        for row in dcad_result.parcels:
            row["county"] = "dcad"
        all_rows.extend(dcad_result.parcels)
        exempt_set.update(dcad_result.exempt_accounts)
    if tad_result:
        for row in tad_result.parcels:
            row["county"] = "tad"
        all_rows.extend(tad_result.parcels)
    if collin_result:
        for row in collin_result.parcels:
            row["county"] = "collin"
        all_rows.extend(collin_result.parcels)
    if denton_result:
        for row in denton_result.parcels:
            row["county"] = "denton"
        all_rows.extend(denton_result.parcels)

    # Fetch Propelio sold records for these parcels (direct-join by (account_num, county))
    propelio_sold_points: list[dict[str, Any]] | None = None
    if include_sold and all_rows:
        parcel_input = [
            (row["account_num"], row["county"])
            for row in all_rows
            if row.get("account_num") and row.get("county")
        ]
        if parcel_input:
            try:
                propelio_match_dict = await asyncio.to_thread(
                    _run_propelio_query_with_conn, parcel_input
                )
                propelio_sold_points = [
                    {"account_num": account_num, "county": county, **comp_dict}
                    for (county, account_num), comp_dict in propelio_match_dict.items()
                ]
            except Exception as exc:
                logger.warning(
                    "propelio_sold: analyze-time query failed: %s: %s (parcel_count=%d)",
                    type(exc).__name__,
                    exc,
                    len(parcel_input),
                )
                propelio_sold_points = None

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
            await asyncio.to_thread(_persist_cached_job_sync, empty_job_id, user_id, [], sold_points, polygon, area_id, propelio_sold_points=None)
        except Exception as exc:
            logger.warning("Failed to persist job to cache (non-fatal): %s", exc)
        return {
            "type": "FeatureCollection",
            "features": [],
            "counts": {"active": 0, "off_market": 0, "multifamily": 0, "duplexes": 0, "vacant": 0, "commercial": 0, "exempt": 0, "total": 0},
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
    # Phase 3: apply truly-empty filter (gate on no addr, no owner, no legal, no value/cert_value)
    # to exclude brand-new pipeline parcels that clutter Mike's workflow. DCAD will no-op.
    rows = [r for r in rows if not _is_truly_empty_parcel(r)]
    features: list[dict[str, Any]] = []
    # Per PARCEL_RATINGS_SPEC.md v2 §2E: hydrate user_rating on each
    # parcel feature when the analyze request is for a saved area. Single
    # SQL query, applied in-memory below to avoid per-row joins across
    # county-specific parcel tables. Public/unauth endpoints intentionally
    # skip this hydrate (security boundary per KK product call).
    # ARV ratings stay in user_rating (today's behavior, unchanged query).
    parcel_ratings_map: dict[tuple[str, str], str] = _load_parcel_ratings_for_workspace(area_id, view="arv")
    # Chunk E (per-view ratings spec §4): additionally layer the per-view
    # (nbv/export) parcel ratings so the frontend projects the active view's
    # marks without ARV bleeding across views. Additive + backward-compatible.
    parcel_ratings_by_view: dict[tuple[str, str], dict[str, str]] = _load_parcel_ratings_by_view_for_workspace(area_id)
    counts = {
        "active": 0,
        "off_market": 0,
        "multifamily": 0,
        "duplexes": 0,
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
        elif prop_type == "duplexes":
            counts["duplexes"] += 1
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
            # Hydrate user_rating from parcel_ratings (workspace-scoped).
            # None for parcels with no rating; "good"/"bad" otherwise.
            _rating_key = (
                feature["properties"]["source_county"],
                str(feature["properties"].get("account_num", "") or "").strip(),
            )
            feature["properties"]["user_rating"] = parcel_ratings_map.get(_rating_key)
            # Per-view ratings (nbv/export); {} when none → frontend projects
            # null for non-ARV views (Chunk E). ARV is user_rating above.
            feature["properties"]["ratings_by_view"] = parcel_ratings_by_view.get(_rating_key, {})
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
        await asyncio.to_thread(_persist_cached_job_sync, job_id, user_id, rows, sold_points, polygon, area_id, propelio_sold_points=propelio_sold_points)
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
    # Outreach role gate (Mailer + Phone Tracking, 2026-06-03). Strip
    # outreach_* keys for non-power_user roles. Mutates features in place.
    _strip_outreach_from_features(features, user)
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
        await asyncio.to_thread(_persist_cached_job_sync, new_job_id, user_id, merged_rows, merged_sold_points, [], area_id, propelio_sold_points=None)
    except Exception as exc:
        logger.warning("Failed to persist job to cache (non-fatal): %s", exc)
    return {"job_id": new_job_id}


@app.post("/api/job/{job_id}/verification")
async def save_verification(job_id: str, request: VerificationRequest, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Persist verify/target tags for a cached job.

    Performance: branches on whether the job is in the in-memory _job_store.
    - WARM (_job_store hit): use existing path that iterates the full row
      dicts. Lets us mutate in-place so subsequent reads see the new tag
      values without a DB roundtrip. Same behavior as before this refactor.
    - COLD (_job_store miss): use the fast-path. Two thin JSONB queries
      replace the multi-MB row blob hydration that was the bottleneck for
      large jobs after the residential-detail expansion. The in-memory
      mutation is moot in this branch (no _job_store entry to mutate) so
      we skip it cleanly.
    See docs/SAVE_VERIFICATION_FAST_PATH_SPEC.md for the design.
    """
    require_csrf(req)
    user_id = int(user["id"])

    # Branch on warm vs cold cache state.
    warm_job = _job_store.get(job_id)
    if warm_job is not None and int(warm_job.get("user_id", -1)) != user_id:
        # User-id mismatch on the in-memory entry — treat as not found for
        # this user. Same behavior as _get_job's gate.
        warm_job = None

    rows: list[dict[str, Any]] = []
    polygon: Any = []
    parcel_count: int = 0
    account_county_pairs: list[tuple[str, str]] = []

    if warm_job is not None:
        # Warm path: existing behavior preserved.
        rows = warm_job.get("rows", [])
        polygon = warm_job.get("polygon", [])
        parcel_count = len(rows)
        # Iterate full row dicts for pairs (same logic as the original loop).
        for row in rows:
            account_num = str(row.get("account_num", "") or "").strip()
            if not account_num:
                continue
            account_county_pairs.append((account_num, _row_county(row)))
    else:
        # Cold path: thin JSONB queries — no full blob hydration.
        metadata = _load_cached_job_metadata(job_id, user_id)
        if metadata is None:
            raise HTTPException(status_code=404, detail="Job not found")
        polygon, parcel_count = metadata
        account_county_pairs = _load_cached_account_county_pairs(job_id, user_id)

    verifications = request.verifications or {}
    potential_targets = request.potential_targets or {}

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
        county_coverage = (
            _counties_from_rows(rows) if warm_job is not None else _counties_from_pairs(account_county_pairs)
        )
        _persist_session_sync(
            job_id,
            polygon,
            parcel_count,
            county_coverage,
            user_id,
            None,
        )

        upsert_rows: dict[tuple[str, str, str], tuple[str, str, str, str, str]] = {}
        delete_rows: set[tuple[str, str, str]] = set()

        for account_num, county in account_county_pairs:
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


class DownloadFilterRequest(BaseModel):
    filter_ids: dict[str, Any] | None = None
    filter_state: dict[str, Any] | None = None
    filename: str | None = None
    # 2026-05-24: passed by frontend to override the share_id source.
    # Without this, _job_share_id reads cached_jobs.saved_area_id which
    # stays pinned to the ORIGINAL area after a Copy Area As → the CSV's
    # share_id column would point back at the source area, not the active
    # copy. When this field is set, it wins over the cached_jobs fallback.
    loaded_area_id: str | None = None
    # Per-view ratings spec §3.4 (FMEA H1): the active filter view the CSV
    # is exported from. view='arv' (default for absent — coexistence with
    # old clients) → today's ARV Good Comp column byte-for-byte. view in
    # ('nbv','export') → Good Comp column sourced from that view's comp +
    # parcel ratings. Frontend sends this in Chunk E; Chunk C defaults to
    # 'arv' = today's behavior.
    view: str = "arv"


@app.get("/api/download/{job_id}")
async def download(
    job_id: str,
    filename: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    """Backward-compat GET path: full export, no visibility filter, no filter state cols."""
    return await _run_download_csv(job_id, filename, user)


@app.post("/api/download/{job_id}")
async def download_filtered(
    job_id: str,
    body: DownloadFilterRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    """Filtered export: frontend POSTs visible parcel keys + visible comp keys + filter state."""
    require_csrf(request)
    parcel_ids_filter: set[tuple[str, str]] | None = None
    comp_ids_filter: set[str] | None = None
    filter_ids = body.filter_ids or {}
    parcels_list = filter_ids.get("parcels") if isinstance(filter_ids, dict) else None
    if isinstance(parcels_list, list):
        parcel_ids_filter = {
            (
                str(p.get("source_county", "") or "").strip().lower(),
                str(p.get("account_num", "") or "").strip(),
            )
            for p in parcels_list
            if isinstance(p, dict) and p.get("source_county") and p.get("account_num")
        }
    comps_list = filter_ids.get("comps") if isinstance(filter_ids, dict) else None
    if isinstance(comps_list, list):
        comp_ids_filter = {str(k).strip() for k in comps_list if str(k or "").strip()}
    return await _run_download_csv(
        job_id,
        body.filename,
        user,
        parcel_ids_filter=parcel_ids_filter,
        comp_ids_filter=comp_ids_filter,
        # filter_state and bad_comp_ids params reserved for Phase C2.
        filter_state=body.filter_state,
        loaded_area_id=body.loaded_area_id,
        # Per-view ratings §3.4 (FMEA H1): thread the active view so the
        # Good Comp column reflects it. Defaults to 'arv' (coexistence).
        view=body.view,
    )


async def _run_download_csv(
    job_id: str,
    filename: str | None,
    user: dict[str, Any],
    parcel_ids_filter: set[tuple[str, str]] | None = None,
    comp_ids_filter: set[str] | None = None,
    filter_state: dict[str, Any] | None = None,
    bad_comp_ids: set[int] | None = None,
    loaded_area_id: str | None = None,
    view: str = "arv",
) -> StreamingResponse:
    # H9 (FMEA): role gate runs FIRST, before any view handling. The CSV
    # export role check is never weakened by per-view wiring.
    if str(user.get("role") or "").strip().lower() == "user":
        raise HTTPException(status_code=403, detail="CSV export not available for this role")

    # Per-view ratings §3.4 (FMEA H1): normalize the active view. absent/None
    # → 'arv' (coexistence with old clients that don't send the field).
    # Present-but-invalid → 400 (NEVER silently default an invalid present
    # view to ARV — same discipline as the write side, FMEA C2).
    norm_view = str(view or "").strip().lower() or "arv"
    if norm_view not in ("arv", "nbv", "export"):
        raise HTTPException(status_code=400, detail="view must be 'arv', 'nbv', 'export', or null")

    job = _get_job(job_id, int(user["id"]))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    rows = job.get("rows", [])
    # Re-hydrate outreach data from live DB so CSV export reflects edits
    # made AFTER the job was cached (popup PUT, bulk import). Without this,
    # KK's preview-smoke bug repeats: check Mailer Sent → export → blank cell.
    # Helper is a no-op (or clear) for non-power_user roles.
    _hydrate_outreach_for_rows(rows, user)
    # Flood enrichment shipped 2026-06-06 — older cached_jobs rows pre-date
    # it and don't carry flood fields. Re-hydrate same as above so CSV
    # export rights the column even for pre-flood snapshots.
    _hydrate_flood_for_rows(rows)
    redfin_data: dict[str, dict] = job.get("redfin_data", {})
    sold_points: list[dict[str, Any]] = job.get("sold_points", []) or []
    propelio_sold_points: list[dict[str, Any]] = _load_propelio_sold_points(job_id) or []
    # 2026-05-24 bug fix: prefer the frontend's currently-loaded area_id
    # over the job's cached saved_area_id. After Copy Area As, the job
    # stays pinned to the SOURCE area (the create-area UPDATE on
    # cached_jobs is guarded by `saved_area_id IS NULL`), so without this
    # override the CSV's share_id column would resolve to the original
    # area, not the active copy the user is exporting from.
    override_area_id = str(loaded_area_id or "").strip() or None
    job_saved_area_id = override_area_id or (str(job.get("saved_area_id") or "").strip() or None)
    csv_share_id = _job_share_id(job_id, job_saved_area_id)
    # csv_workspace_name populated below from the saved_areas lookup that
    # also resolves the originator parcel. Empty string when no area is
    # linked (analysis-only export, no saved workspace).
    csv_workspace_name = ""
    logger.info("Download job %s: %d parcel rows, %d sold points", job_id, len(rows), len(sold_points))

    # Look up bonded seed-target account_nums for this area (if any), so the
    # CSV can mark each row with whether it was a seed of this workspace.
    # Empty set when the job has no saved_area_id (analysis-only, not yet saved).
    seed_account_nums: set[str] = set()
    # Originator parcel (bonded gold-star target for the workspace) drives the
    # "Intended Target" column. None when there's no saved_area_id.
    originator_key: tuple[str, str] | None = None
    if job_saved_area_id:
        _conn = get_session_conn()
        try:
            with _conn.cursor() as _cur:
                _cur.execute(
                    "SELECT account_num FROM saved_parcels WHERE area_id = %s",
                    (job_saved_area_id,),
                )
                seed_account_nums = {str(r[0]) for r in _cur.fetchall() if r and r[0]}
                _cur.execute(
                    "SELECT originator_parcel_county, originator_parcel_account_num, name"
                    " FROM saved_areas WHERE area_id = %s",
                    (job_saved_area_id,),
                )
                _orow = _cur.fetchone()
                if _orow:
                    csv_workspace_name = str(_orow[2] or "")
                if _orow and _orow[0] and _orow[1]:
                    originator_key = (
                        str(_orow[0]).strip().lower(),
                        str(_orow[1]).strip(),
                    )
        finally:
            release_session_conn(_conn)

    stored_value_export_cells = [""] * 12  # K4 (2026-05-27): +2 for NBV num+comment
    if job_saved_area_id:
        _conn = get_session_conn()
        try:
            sv_rows = _stored_value_get_rows(_conn, job_saved_area_id)
            payload = _stored_value_serialize_payload(sv_rows)
            stored_value_export_cells = _stored_value_csv_cells(payload)
        finally:
            release_session_conn(_conn)

    # Comp ratings pre-fetch: snapshot the workspace's rating state once at
    # export start. Bad-rated rows are skipped entirely; good-rated rows get
    # a "yes" in the trailing Good Comp column. Unsaved-area exports
    # (job_saved_area_id is None) skip this entirely → no drops, no flags.
    #
    # Two dicts are built from a single query:
    # - rating_by_comp_id: comp_id → rating. Used by the orphan row path
    #   (each orphan row IS one comp, so per-comp rating is the right level).
    # - parcel_rating_by_key: (county, account_num) → effective rating with
    #   bad-wins conflict resolution. Used by the parcel row path so the
    #   logic is provenance-agnostic — ANY rated comp matched to a visible
    #   parcel (via propelio_comps.parcel_county + parcel_account_num)
    #   drives the parcel's effective rating, regardless of which writer
    #   (analyze sold-match, Quick Sweep, deep pull, strip runner, etc.)
    #   put the comp in propelio_comps. This bridges the user's mental
    #   model ("I rated the property") to the comp-keyed data model.
    # See docs/COMP_RATING_EXPORT_SPEC.md §3.3 File 3.
    rating_by_comp_id: dict[int, str] = {}
    parcel_rating_by_key: dict[tuple[str, str], str] = {}
    # parcel_rating_direct_by_key: DIRECT parcel ratings (new feature per
    # PARCEL_RATINGS_SPEC.md v2). Distinct from parcel_rating_by_key above
    # which is comp-derived. Used in COMP-WINS-ABSOLUTE exclusion: direct
    # rating only applies when there's no comp rating for that parcel.
    # Per-view ratings spec §3.4 (FMEA H1): source the ACTIVE view's parcel
    # ratings. view='arv' → parcel_ratings (today's behavior); view in
    # ('nbv','export') → parcel_ratings_views (Chunk B made the loader
    # view-aware). _load_parcel_ratings_for_workspace handles the routing.
    parcel_rating_direct_by_key: dict[tuple[str, str], str] = _load_parcel_ratings_for_workspace(job_saved_area_id, view=norm_view)
    if job_saved_area_id:
        _conn = get_session_conn()
        try:
            with _conn.cursor() as cur:
                # Per-view ratings spec §3.4 (FMEA H1): source the ACTIVE
                # view's comp ratings. view='arv' → comp_ratings (unchanged
                # query, today's ARV behavior byte-for-byte). view in
                # ('nbv','export') → comp_ratings_views WHERE view=%s
                # (workspace-scoped — C4 cross-tenant guard). The bad-wins
                # conflict resolution below runs ONCE on this view's data.
                if norm_view == "arv":
                    cur.execute(
                        """
                        SELECT
                            cr.comp_id,
                            cr.rating,
                            pc.parcel_county,
                            pc.parcel_account_num
                        FROM comp_ratings cr
                        JOIN propelio_comps pc ON cr.comp_id = pc.comp_id
                        WHERE cr.workspace_id = %s
                        """,
                        (job_saved_area_id,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                            cr.comp_id,
                            cr.rating,
                            pc.parcel_county,
                            pc.parcel_account_num
                        FROM comp_ratings_views cr
                        JOIN propelio_comps pc ON cr.comp_id = pc.comp_id
                        WHERE cr.workspace_id = %s AND cr.view = %s
                        """,
                        (job_saved_area_id, norm_view),
                    )
                for comp_id, rating, parcel_county, parcel_account_num in cur.fetchall():
                    rating_by_comp_id[int(comp_id)] = rating
                    if not parcel_county or not parcel_account_num:
                        continue
                    pkey = (
                        str(parcel_county).strip().lower(),
                        str(parcel_account_num).strip(),
                    )
                    if not pkey[0] or not pkey[1]:
                        continue
                    # Bad-wins conflict resolution: once a parcel has any bad
                    # rating, no other rating displaces it. Otherwise good
                    # upgrades from no-rating, but cannot overwrite an
                    # earlier bad. Runs ONCE on the active view's data (H1).
                    existing = parcel_rating_by_key.get(pkey)
                    if existing == "bad":
                        continue
                    if rating == "bad" or existing is None:
                        parcel_rating_by_key[pkey] = rating
        finally:
            release_session_conn(_conn)

    def _rating_for(comp_id_raw) -> str | None:
        """Resolve a comp_id (any type) to its rating, treating malformed
        values as unrated. Never raises — degrades silently to None
        (no drop, no flag) for ALL of the following inputs:

        - None (legacy cached_jobs propelio_sold_points entries with no comp_id).
        - bool (True/False are instances of int in Python; without this
          explicit reject they would resolve to comp_id=1 / comp_id=0
          and silently hit real or sentinel rating rows).
        - Non-numeric types (string, dict, list, NaN payloads).
        - OverflowError on absurdly large numeric values.
        - Non-positive ints (comp_id is BIGSERIAL, always >= 1; negative,
          zero, or sentinel-style values are not real comp_ids).

        See docs/COMP_RATING_EXPORT_SPEC.md §3.3 File 3 + §4 decision #10.
        """
        if comp_id_raw is None or isinstance(comp_id_raw, bool):
            return None
        try:
            cid = int(comp_id_raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if cid <= 0:
            return None
        return rating_by_comp_id.get(cid)

    # Visible parcel keys set — used both for parcel-row filtering AND for
    # deduping comp rows (a comp whose (parcel_county, parcel_account_num) is
    # already in this set is represented by its parent parcel row inline).
    visible_parcel_keys: set[tuple[str, str]] = parcel_ids_filter or set()

    # CAD-by-parcel-key lookup over ALL cached parcel rows (not just visible
    # ones). When a comp's parent parcel is in the polygon but filtered out
    # (e.g. multifamily parcel hidden), the comp row still gets full CAD data
    # from this lookup. Comps with no matching parcel in cached_jobs.rows
    # (true OAC / spillover) fall through to empty-dict — CAD cells blank.
    cad_by_parcel_key: dict[tuple[str, str], dict[str, Any]] = {}
    for _pr in rows:
        _key = (
            str(_pr.get("county", "") or "").strip().lower(),
            str(_pr.get("account_num", "") or "").strip(),
        )
        if _key[0] and _key[1]:
            cad_by_parcel_key[_key] = _pr

    # Pre-fetch orphan comps for the export. Only kicks in when the POST path
    # supplied a comps filter list. Each row that lacks a visible parent
    # parcel will emit its own comp row at the end of generate_csv.
    orphan_comps: list[dict[str, Any]] = []
    if comp_ids_filter:
        _conn = get_session_conn()
        try:
            with _conn.cursor(cursor_factory=RealDictCursor) as _cur:
                _cur.execute(
                    "SELECT comp_id, comp_address_key, address, lat, lng, status, last_status,"
                    " price, last_price, sold_date, close_date, dom, beds, baths,"
                    " sqft, lot_size, year_built, parcel_county, parcel_account_num"
                    " FROM propelio_comps WHERE comp_address_key = ANY(%s)",
                    (list(comp_ids_filter),),
                )
                # Build set of (county, account_num) keys that WILL be
                # inline-emitted via propelio_sold_points. Only those should
                # be skipped from orphan emission. Comps whose parcel is
                # visible but who are NOT in propelio_sold_points (e.g. pulled
                # via the frontend's per-address comp fetch) would otherwise
                # fall through both paths and be lost from the CSV entirely.
                _inlined_keys: set[tuple[str, str]] = set()
                for _sp in propelio_sold_points:
                    _ic = (
                        str(_sp.get("county", "") or "").strip().lower(),
                        str(_sp.get("account_num", "") or "").strip(),
                    )
                    if _ic[0] and _ic[1]:
                        _inlined_keys.add(_ic)
                for _crow in _cur.fetchall():
                    _ck = (
                        str(_crow.get("parcel_county") or "").strip().lower(),
                        str(_crow.get("parcel_account_num") or "").strip(),
                    )
                    # Skip ONLY if comp will be represented inline in its
                    # parcel row (parcel visible AND comp in propelio_sold_points).
                    if (
                        _ck[0]
                        and _ck[1]
                        and _ck in visible_parcel_keys
                        and _ck in _inlined_keys
                    ):
                        continue
                    orphan_comps.append(dict(_crow))
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

    propelio_comp_by_parcel_key: dict[tuple[str, str], dict] = {}
    for comp in propelio_sold_points:
        county = str(comp.get("county", "") or "").strip().lower()
        account_num = str(comp.get("account_num", "") or "").strip()
        if county and account_num:
            propelio_comp_by_parcel_key[(county, account_num)] = comp

    # 2026-05-27 K5: when this CSV belongs to a saved workspace, build the
    # download filename as `<workspace-name>_<YYYY-MM-DD>_<HHMMSS>.csv` so the
    # operator can tell at a glance which workspace each download came from.
    # Falls back to the frontend-provided filename (legacy `lotledger_…`) for
    # analysis-only exports that have no linked saved area.
    if csv_workspace_name:
        from datetime import datetime as _dt
        filename = f"{csv_workspace_name}_{_dt.now().strftime('%Y-%m-%d_%H%M%S')}.csv"
    download_name = _normalize_csv_filename(filename)

    # Ownership-history. Collect (county_lower, account_num) keys from every
    # supported-county parcel + orphan comp, then one batched lookup against
    # ownership_snapshots (one query per county, joined back here). Supported
    # set is owned by _is_owner_history_supported (DCAD + Collin + Denton
    # today; TAD pending). Keying by (county, account_num) — not account_num
    # alone — keeps two counties that share the same account_num string from
    # bleeding ownership timelines into each other.
    _hist_keys: set[tuple[str, str]] = set()
    for _r in rows:
        _src = _csv_county_source(_r)
        if _is_owner_history_supported(_src):
            _an = str(_r.get("account_num") or "").strip()
            if _an:
                _hist_keys.add((_src.lower(), _an))
    for _oc in orphan_comps:
        _orphan_county = str(_oc.get("parcel_county") or "").strip().lower()
        if _is_owner_history_supported(_orphan_county):
            _an = str(_oc.get("parcel_account_num") or "").strip()
            if _an:
                _hist_keys.add((_orphan_county, _an))
    ownership_history = _fetch_ownership_history(_hist_keys)

    def generate_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "Row Type",
                "Parcel ID",      # 2 (Mike request 2026-06-03 PM): convenient at column B
                "County Source",  # 3 (Phase 2 — 2026-05-21): "DCAD" / "TAD" / "Collin" / "Denton"
                "Intended Target",
                "MLS Status",
                "Property Address",
                "Property City",
                "Property State",
                "Property Zip",
                "Owner Name",
                "Owner Mailing Address",
                "Owner City",
                "Owner State",
                "Owner Zip",
                "Land Value",
                "Improvement Value",
                "Total Value",
                "Value Source",
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
                # === Phase 2 residential detail expansion (2026-05-21) ===
                # New cross-county canonical columns. Populated for any
                # county whose ingest exposes the field via build_feature
                # (DCAD: full coverage after Phase 1 backfill; Collin:
                # beds/baths/pool/stories already covered; TAD: pending
                # TAD-half PR for the structural fields; Denton: blank).
                # See docs/CAD_RESIDENTIAL_DETAIL_EXPANSION_SPEC.md
                # canonical-field contract.
                "Full Baths",
                "Half Baths",
                "Fireplaces",
                "Kitchens",
                "Wet Bars",
                "Garage Capacity",
                "Stories (raw)",
                "Structure Type",
                "Foundation Type",
                "Construction Frame Type",
                "Exterior Wall",
                "Heating Type",
                "AC Type",
                "Roof Type",
                "Roof Material",
                "Fence Type",
                "Basement",
                "Building Class",
                "CDU Rating",
                "Effective Year Built",
                "Actual Age",
                "% Complete",
                "Spa Flag",
                "Sauna Flag",
                "Sprinkler Flag",
                "Deck Flag",
                # === end Phase 2 residential detail expansion ===
                # === Phase 4 Denton-rich residential extras (2026-05-21) ===
                # 5 new columns surfacing fields Denton publishes that DCAD
                # doesn't (Interior Finish, Flooring, Total Rooms, Outdoor
                # Fireplaces, End Unit). DCAD/Collin/TAD parcels return blank
                # for these — no regression, just N/A from build_feature.
                "Interior Finish",
                "Flooring",
                "Total Rooms",
                "Outdoor Fireplaces",
                "End Unit",
                # === end Phase 4 residential extras ===
                "Current Market Value",
                "Current Assessed Value",
                "Current Ag Use Value",
                "Current Ag Market Value",
                "Current Ag Loss Value",
                "Certified Total Value",
                "Denton - Exemptions",
                "Denton - Homestead (HS)",
                "Deed Number (Detailed)",
                "Subdivision (Parsed)",
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
                "Comp Status",
                # COMPATIBILITY LOCK: Good Comp slots immediately after Comp
                # Status, grouped with the comp data columns. Layout history:
                # originally col 96; +1 to col 97 by Value Source (col 13,
                # 2026-05-20); +27 to col 124 by Phase 2 residential detail
                # expansion (County Source col 2 + 26 residential cols after
                # Units col 73, 2026-05-21 morning); +5 to col 129 by Phase 4
                # Denton-rich residential extras (Interior Finish, Flooring,
                # Total Rooms, Outdoor Fireplaces, End Unit — 2026-05-21
                # afternoon). Stored Values cluster now lives at 142-151.
                # Total CSV columns: 151. Mirror locks at parcel/orphan
                # writerow append sites must place the "yes"/blank cell at the
                # exact same offset. Do not move this column without
                # coordinated updates at all three sites.
                "Good Comp",
                "RF_Comp Sold Price",
                "RF_Comp Sold Date",
                "RF_Comp $/sqft",
                "RF_Comp Year Built",
                "RF_Comp Living Area (sqft)",
                "RF_Comp Lot Size (sqft)",
                "RF_Comp Beds",
                "RF_Comp Baths",
                "RF_Comp Days on Market",
                "RF_Comp Listing URL",
                "Seed Target",
                "share_id",
                "Workspace Name",
                "Stored ARV",
                "Stored ARV Comment",
                "Stored TDPP",
                "Stored TDPP Comment",
                "Stored Rehab Needed",
                "Stored Rehab Needed Comment",
                "Stored MAO (ARV)",
                "Stored MAO (ARV) Comment",
                "Stored TDPP-MAO (ARV)",
                "Stored TDPP-MAO (ARV) Comment",
                # K4 (2026-05-27): NBV columns APPENDED at the end of the
                # stored-values header block so existing positions hold.
                "Stored NBV",
                "Stored NBV Comment",
                # === v4a §2.8 — 28 new columns appended at the right edge ===
                # Existing columns (1..151) are byte-stable. Mike's
                # column-letter formulas keep working.
                # (Property City/State/Zip moved to positions 6-8 near Property Address)
                "Property Street Number",
                "Property Street Half Number",
                "Property Full Street Name",
                "Property Building ID",
                "Property Unit ID",
                "Property MAPSCO",
                "Owner Name 2",
                "Biz Name",
                "Owner Address Line 1 (raw)",
                "Owner Address Line 2 (raw)",
                "Owner Address Line 3 (raw)",
                "Owner Address Line 4 (raw)",
                "Owner Country",
                "City Jurisdiction",
                "County Jurisdiction",
                "Hospital Jurisdiction",
                "College Jurisdiction",
                "Special District Jurisdiction",
                "City Taxable Value",
                "County Taxable Value",
                "ISD Taxable Value",
                "Hospital Taxable Value",
                "College Taxable Value",
                "Special District Taxable Value",
                "Owner 2 Name",
                "Owner 2 %",
                "Owner 3 Name",
                "Owner 3 %",
                "Current Owner",
                "Current Owner Acquired",
                "Prior Owner 1",
                "Prior Owner 2",
                "Prior Owner 3",
                "Prior Owner 4",
                # Mailer + Phone Tracking v3 (2026-06-03 PM). Right-edge
                # block now holds only the role-gated outreach cells.
                # v3 (Mike request): Parcel ID moved out of this block to
                # column B (position 2) for convenience — column-letter
                # formulas for everything from column B onward shifted +1.
                "Contact Info Retrieved",
                "Last Mailer Sent",
                "Flood Zone",
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
            # Phase C1: skip parcels not in the visibility filter (empty filter set
            # is treated as no filter — frontend null-guards before sending).
            if parcel_ids_filter is not None:
                _row_key = (
                    str(row.get("county", "") or "").strip().lower(),
                    str(row.get("account_num", "") or "").strip(),
                )
                if not _row_key[0] or not _row_key[1] or _row_key not in parcel_ids_filter:
                    continue

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

            row_key = (
                str(row.get("county", "") or "").strip().lower(),
                str(row.get("account_num", "") or "").strip(),
            )
            propelio_comp = propelio_comp_by_parcel_key.get(row_key)
            # Comp-rating gate at parcel level: skip parcel row entirely if
            # ANY rated comp in this workspace matches the parcel by
            # (parcel_county, parcel_account_num). Provenance-agnostic — the
            # comp doesn't have to be in propelio_sold_points; any writer to
            # propelio_comps (analyze, Quick Sweep, deep pull, strip runner)
            # counts. bad-wins / good-otherwise conflict resolution applied
            # at pre-fetch time. See docs/COMP_RATING_EXPORT_SPEC.md §3.1.
            #
            # COMP-WINS-ABSOLUTE (per PARCEL_RATINGS_SPEC.md v2 §2F + KK
            # product call): when a comp rating exists for this parcel,
            # that determines row inclusion regardless of the direct parcel
            # rating. Direct parcel rating only applies when no comp rating
            # exists. Net effect:
            #   comp=bad → exclude (regardless of direct)
            #   comp=good → include (regardless of direct)
            #   no comp + direct=bad → exclude
            #   otherwise → include
            _parcel_rating = parcel_rating_by_key.get(row_key)
            if _parcel_rating == "bad":
                continue
            if _parcel_rating is None:
                _direct_rating = parcel_rating_direct_by_key.get(row_key)
                if _direct_rating == "bad":
                    continue
            rf_comp = comp_by_parcel_key.get(str(row.get("account_num", "") or ""))

            _is_intended_target = (
                originator_key is not None
                and (
                    str(row.get("county", "") or "").strip().lower(),
                    str(row.get("account_num", "") or "").strip(),
                ) == originator_key
            )
            _own = _ownership_history_cells(
                ownership_history.get(
                    (_csv_county_source(row).lower(),
                     str(row.get("account_num") or "").strip())
                )
                if _is_owner_history_supported(_csv_county_source(row)) else None
            )
            writer.writerow(
                [
                    "Parcel",
                    _outreach_parcel_id_cell(row),  # 2 (Mike request 2026-06-03 PM): Parcel ID at column B
                    _csv_county_source(row),         # 3 (Phase 2)
                    "yes" if _is_intended_target else "",
                    "Active" if on_redfin else "Off Market",
                    display_address,
                    row.get("property_city", "") or "",
                    "TX",
                    row.get("property_zip", "") or "",
                    row.get("owner_name", ""),
                    row.get("owner_address", ""),
                    row.get("owner_city", ""),
                    row.get("owner_state", ""),
                    row.get("owner_zip", ""),
                    round(_safe_float(land_val), 0) if _safe_float(land_val) is not None else "",
                    round(_safe_float(impr_val), 0) if _safe_float(impr_val) is not None else "",
                    round(_safe_float(tot_val), 0) if _safe_float(tot_val) is not None else "",
                    str(row.get("total_value_source") or ""),
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
                    # === Phase 2 residential detail cells (parcel writerow) ===
                    # Same canonical keys as the popup/card reads. row.get() returns
                    # None for counties that don't expose the field → blank cell.
                    round(_safe_float(row.get("full_baths")), 0) if _safe_float(row.get("full_baths")) is not None else "",
                    round(_safe_float(row.get("half_baths")), 0) if _safe_float(row.get("half_baths")) is not None else "",
                    round(_safe_float(row.get("fireplaces")), 0) if _safe_float(row.get("fireplaces")) is not None else "",
                    round(_safe_float(row.get("kitchens")), 0) if _safe_float(row.get("kitchens")) is not None else "",
                    round(_safe_float(row.get("wet_bars")), 0) if _safe_float(row.get("wet_bars")) is not None else "",
                    round(_safe_float(row.get("garage_capacity")), 0) if _safe_float(row.get("garage_capacity")) is not None else "",
                    row.get("stories_desc", "") or "",
                    row.get("structure_type", "") or "" if (row.get("structure_type") or "") != "N/A" else "",
                    row.get("foundation_type", "") or "" if (row.get("foundation_type") or "") != "N/A" else "",
                    row.get("construction_frame_type", "") or "" if (row.get("construction_frame_type") or "") != "N/A" else "",
                    row.get("ext_wall", "") or "" if (row.get("ext_wall") or "") != "N/A" else "",
                    row.get("heating_type", "") or "" if (row.get("heating_type") or "") != "N/A" else "",
                    row.get("ac_type", "") or "" if (row.get("ac_type") or "") != "N/A" else "",
                    row.get("roof_type", "") or "" if (row.get("roof_type") or "") != "N/A" else "",
                    row.get("roof_material", "") or "" if (row.get("roof_material") or "") != "N/A" else "",
                    row.get("fence_type", "") or "" if (row.get("fence_type") or "") != "N/A" else "",
                    row.get("basement", "") or "" if (row.get("basement") or "") != "N/A" else "",
                    row.get("bldg_class", "") or "" if (row.get("bldg_class") or "") != "N/A" else "",
                    row.get("cdu_rating", "") or "" if (row.get("cdu_rating") or "") != "N/A" else "",
                    row.get("eff_yr_built", "") or "" if (row.get("eff_yr_built") or "") != "N/A" else "",
                    row.get("act_age", "") or "" if (row.get("act_age") or "") != "N/A" else "",
                    row.get("pct_complete", "") or "" if (row.get("pct_complete") or "") != "N/A" else "",
                    row.get("spa_flag", "") or "",
                    row.get("sauna_flag", "") or "",
                    row.get("sprinkler_flag", "") or "",
                    row.get("deck_flag", "") or "",
                    # === end Phase 2 residential detail cells ===
                    # === Phase 4 Denton-rich residential extras (2026-05-21) ===
                    # Same canonical keys exposed by build_feature for all
                    # counties. DCAD/Collin/TAD parcels return "N/A" or 0
                    # which we coerce to blank cells for spreadsheet sanity.
                    row.get("interior_finish", "") or "" if (row.get("interior_finish") or "") != "N/A" else "",
                    row.get("flooring", "") or "" if (row.get("flooring") or "") != "N/A" else "",
                    int(_safe_float(row.get("total_rooms"))) if _safe_float(row.get("total_rooms")) is not None else "",
                    int(_safe_float(row.get("outdoor_fireplaces"))) if _safe_float(row.get("outdoor_fireplaces")) is not None else "",
                    row.get("end_unit", "") or "",
                    # === end Phase 4 residential extras ===
                    round(_safe_float(row.get("curr_market_value")), 0) if _safe_float(row.get("curr_market_value")) is not None else "",
                    round(_safe_float(row.get("curr_assessed_value")), 0) if _safe_float(row.get("curr_assessed_value")) is not None else "",
                    round(_safe_float(row.get("curr_ag_use_value")), 0) if _safe_float(row.get("curr_ag_use_value")) is not None else "",
                    round(_safe_float(row.get("curr_ag_market_value")), 0) if _safe_float(row.get("curr_ag_market_value")) is not None else "",
                    round(_safe_float(row.get("curr_ag_loss_value")), 0) if _safe_float(row.get("curr_ag_loss_value")) is not None else "",
                    round(_safe_float(row.get("cert_total_value")), 0) if _safe_float(row.get("cert_total_value")) is not None else "",
                    row.get("exemptions", "") or "",
                    row.get("exempt_homestead", "") or "",
                    row.get("deed_number", "") or "",
                    row.get("subdivision", "") or "",
                    round(_safe_float(propelio_comp.get("sold_price")), 0) if propelio_comp and _safe_float(propelio_comp.get("sold_price")) is not None else "",
                    (propelio_comp.get("sold_date", "") or "") if propelio_comp else "",
                    round(_safe_float(propelio_comp.get("price_per_sqft")), 0) if propelio_comp and _safe_float(propelio_comp.get("price_per_sqft")) is not None else "",
                    int(_safe_float(propelio_comp.get("yr_built"))) if propelio_comp and _safe_float(propelio_comp.get("yr_built")) not in (None, 0.0) else "",
                    int(_safe_float(propelio_comp.get("sqft"))) if propelio_comp and _safe_float(propelio_comp.get("sqft")) not in (None, 0.0) else "",
                    round(_safe_float(propelio_comp.get("lot_sqft")), 0) if propelio_comp and _safe_float(propelio_comp.get("lot_sqft")) is not None else "",
                    int(_safe_float(propelio_comp.get("beds"))) if propelio_comp and _safe_float(propelio_comp.get("beds")) not in (None, 0.0) else "",
                    int(_safe_float(propelio_comp.get("baths"))) if propelio_comp and _safe_float(propelio_comp.get("baths")) not in (None, 0.0) else "",
                    int(_safe_float(propelio_comp.get("dom"))) if propelio_comp and _safe_float(propelio_comp.get("dom")) not in (None, 0.0) else "",
                    (propelio_comp.get("listing_url", "") or "") if propelio_comp else "",
                    (propelio_comp.get("status", "") or "") if propelio_comp else "",
                    # COMPATIBILITY LOCK: Good Comp slots immediately after
                    # Comp Status. Shifted +1 by Value Source at column 13.
                    # Mirrored at header + orphan writerow.
                    # Do not move without coordinated updates at all three sites.
                    "yes" if _parcel_rating == "good" else "",
                    round(_safe_float(rf_comp.get("sold_price")), 0) if rf_comp and _safe_float(rf_comp.get("sold_price")) is not None else "",
                    (rf_comp.get("sold_date", "") or "") if rf_comp else "",
                    round(_safe_float(rf_comp.get("price_per_sqft")), 0) if rf_comp and _safe_float(rf_comp.get("price_per_sqft")) is not None else "",
                    int(_safe_float(rf_comp.get("yr_built"))) if rf_comp and _safe_float(rf_comp.get("yr_built")) not in (None, 0.0) else "",
                    int(_safe_float(rf_comp.get("sqft"))) if rf_comp and _safe_float(rf_comp.get("sqft")) not in (None, 0.0) else "",
                    round(_safe_float(rf_comp.get("lot_sqft")), 0) if rf_comp and _safe_float(rf_comp.get("lot_sqft")) is not None else "",
                    int(_safe_float(rf_comp.get("beds"))) if rf_comp and _safe_float(rf_comp.get("beds")) not in (None, 0.0) else "",
                    int(_safe_float(rf_comp.get("baths"))) if rf_comp and _safe_float(rf_comp.get("baths")) not in (None, 0.0) else "",
                    int(_safe_float(rf_comp.get("dom"))) if rf_comp and _safe_float(rf_comp.get("dom")) not in (None, 0.0) else "",
                    (rf_comp.get("listing_url", "") or "") if rf_comp else "",
                    "yes" if str(row.get("account_num", "") or "") in seed_account_nums else "",
                    csv_share_id,
                    csv_workspace_name,
                    *stored_value_export_cells,
                    # === v4a §2.8 — 28 new cells (mirror header order exactly) ===
                    # (property_city/TX/property_zip moved to positions 6-8 near display_address)
                    row.get("street_num", "") or "",
                    row.get("street_half_num", "") or "",
                    row.get("full_street_name", "") or "",
                    row.get("bldg_id", "") or "",
                    row.get("unit_id", "") or "",
                    row.get("mapsco", "") or "",
                    row.get("owner_name2", "") or "",
                    row.get("biz_name", "") or "",
                    row.get("owner_address_line1", "") or "",
                    row.get("owner_address_line2", "") or "",
                    row.get("owner_address_line3", "") or "",
                    row.get("owner_address_line4", "") or "",
                    row.get("owner_country", "") or "",
                    row.get("city_jurisdiction_desc", "") or "",
                    row.get("county_jurisdiction_desc", "") or "",
                    row.get("hospital_jurisdiction_desc", "") or "",
                    row.get("college_jurisdiction_desc", "") or "",
                    row.get("special_dist_jurisdiction_desc", "") or "",
                    round(_safe_float(row.get("city_taxable_val")), 0) if _safe_float(row.get("city_taxable_val")) is not None else "",
                    round(_safe_float(row.get("county_taxable_val")), 0) if _safe_float(row.get("county_taxable_val")) is not None else "",
                    round(_safe_float(row.get("isd_taxable_val")), 0) if _safe_float(row.get("isd_taxable_val")) is not None else "",
                    round(_safe_float(row.get("hospital_taxable_val")), 0) if _safe_float(row.get("hospital_taxable_val")) is not None else "",
                    round(_safe_float(row.get("college_taxable_val")), 0) if _safe_float(row.get("college_taxable_val")) is not None else "",
                    round(_safe_float(row.get("special_dist_taxable_val")), 0) if _safe_float(row.get("special_dist_taxable_val")) is not None else "",
                    *_additional_owner_cells(row.get("additional_owners")),
                    _own[0], _own[1], _own[2], _own[3], _own[4], _own[5],
                    # Mailer + Phone Tracking — 4 cells (Parcel ID always
                    # emitted; outreach 3 blanked for non-power_user roles).
                    *_outreach_csv_cells(row, user),
                    _flood_zone_csv_cell(row),
                ]
            )
            buffer.seek(0)
            yield buffer.getvalue()
            buffer.truncate(0)
            buffer.seek(0)

        # Emit a row per orphan visible comp (comps not already represented by
        # a visible parent parcel row). Same column shape as parcel rows; the
        # parcel-specific cells are left blank.
        for _c in orphan_comps:
            # Comp-rating gate: skip orphan row entirely if the comp is rated
            # 'bad' in this workspace. 'good' surfaces as "yes" in the
            # trailing Good Comp column; unrated stays blank. See
            # docs/COMP_RATING_EXPORT_SPEC.md §3.1.
            _orphan_rating = _rating_for(_c.get("comp_id"))
            if _orphan_rating == "bad":
                continue
            _comp_price = _safe_float(_c.get("price")) or _safe_float(_c.get("last_price"))
            _comp_sqft = _safe_float(_c.get("sqft"))
            _comp_sold_date = _c.get("sold_date") or _c.get("close_date") or ""
            _comp_status_raw = (_c.get("status") or _c.get("last_status") or "").strip()
            _comp_lat = _safe_float(_c.get("lat"))
            _comp_lng = _safe_float(_c.get("lng"))
            _comp_ppsf = (
                round(_comp_price / _comp_sqft, 2)
                if (_comp_price is not None and _comp_sqft is not None and _comp_sqft > 0)
                else ""
            )
            _comp_maps_link = (
                f"https://www.google.com/maps?q={_comp_lat},{_comp_lng}"
                if _comp_lat is not None and _comp_lng is not None
                else ""
            )
            # Row shape mirrors parcel rows: 2 prefix cols (Row Type, Intended
            # Target) + 80 parcel/comp cols + share_id. Most parcel-specific
            # cells are "" on a comp row; the inline "Comp *" columns hold the
            # comp's own data.
            _cad_key = (
                str(_c.get("parcel_county") or "").strip().lower(),
                str(_c.get("parcel_account_num") or "").strip(),
            )
            _cad: dict[str, Any] = cad_by_parcel_key.get(_cad_key, {}) if _cad_key[0] and _cad_key[1] else {}

            # Property-level values: prefer CAD when available, fall back to comp data.
            _yr_parcel = _safe_float(_cad.get("yr_built")) if _cad else None
            _yr_comp = _safe_float(_c.get("year_built"))
            _yr_pref = _yr_parcel if _yr_parcel not in (None, 0.0) else _yr_comp
            _yr_csv = int(_yr_pref) if _yr_pref not in (None, 0.0) else ""

            _living_parcel = _safe_float(_cad.get("living_area")) if _cad else None
            _living_pref = _living_parcel if _living_parcel not in (None, 0.0) else _comp_sqft
            _sqft_csv = int(_living_pref) if _living_pref not in (None, 0.0) else ""

            _main_area_csv = (
                int(_safe_float(_cad.get("main_area")))
                if _cad and _safe_float(_cad.get("main_area")) not in (None, 0.0)
                else ""
            )

            _area_size_cad = _safe_float(_cad.get("area_size")) if _cad else None
            _area_estimated_cad = bool(_cad.get("area_estimated")) if _cad else False
            if _area_size_cad is not None and _area_size_cad > 0:
                _area_sf = round(_area_size_cad, 0)
                _area_ac = round(_area_size_cad / 43560, 3)
            else:
                _lot_comp = _safe_float(_c.get("lot_size"))
                _area_sf = round(_lot_comp, 0) if _lot_comp is not None else ""
                _area_ac = round(_lot_comp / 43560, 3) if _lot_comp is not None else ""
            _lot_sf_csv = "" if _area_estimated_cad else _area_sf
            _lot_ac_csv = "" if _area_estimated_cad else _area_ac
            _est_lot_sf_csv = _area_sf if _area_estimated_cad else ""
            _est_lot_ac_csv = _area_ac if _area_estimated_cad else ""

            _dims_estimated_cad = bool(_cad.get("dims_estimated")) if _cad else False
            _front_dim_val = _safe_float(_cad.get("front_dim")) if _cad else None
            _depth_dim_val = _safe_float(_cad.get("depth_dim")) if _cad else None
            _frontage_csv = (
                int(_front_dim_val)
                if (not _dims_estimated_cad and _front_dim_val not in (None, 0.0))
                else ""
            )
            _depth_csv = (
                int(_depth_dim_val)
                if (not _dims_estimated_cad and _depth_dim_val not in (None, 0.0))
                else ""
            )
            _est_frontage_csv = (
                int(_front_dim_val)
                if (_dims_estimated_cad and _front_dim_val not in (None, 0.0))
                else ""
            )
            _est_depth_csv = (
                int(_depth_dim_val)
                if (_dims_estimated_cad and _depth_dim_val not in (None, 0.0))
                else ""
            )

            _beds_parcel = _safe_float(_cad.get("beds")) if _cad else None
            _beds_comp = _safe_float(_c.get("beds"))
            _beds_pref = _beds_parcel if _beds_parcel is not None else _beds_comp
            _beds_csv = round(_beds_pref, 1) if _beds_pref is not None else ""

            _baths_parcel = _safe_float(_cad.get("baths")) if _cad else None
            _baths_comp = _safe_float(_c.get("baths"))
            _baths_pref = _baths_parcel if _baths_parcel is not None else _baths_comp
            _baths_csv = round(_baths_pref, 1) if _baths_pref is not None else ""

            _dom_csv = (
                int(_safe_float(_c.get("dom")))
                if _safe_float(_c.get("dom")) not in (None, 0.0)
                else ""
            )
            _price_csv = round(_comp_price, 0) if _comp_price is not None else ""
            _comp_status_titled = _comp_status_raw.title() or ""

            # Prefer parcel address if CAD has one (more standardized), else comp's address.
            _row_address = (
                _cad.get("property_address") or _c.get("address") or ""
            )

            # Prefer parcel lat/lng if available — comp's coords get used only on OAC.
            _row_lat = _safe_float(_cad.get("lat")) if _cad else None
            if _row_lat is None:
                _row_lat = _comp_lat
            _row_lng = _safe_float(_cad.get("lng")) if _cad else None
            if _row_lng is None:
                _row_lng = _comp_lng
            _row_maps_link = _google_maps_link(_cad) if _cad else _comp_maps_link

            _hoa_text = (
                _cad.get("hoa_name", "")
                or ("N/A (Tarrant HOA not loaded)" if _cad.get("division_cd") == "TAD" else "")
            ) if _cad else ""

            # Legal Description is a join of legal1..legal5 in the parcel writerow.
            # Mirror that exactly so comp rows show the same concatenated value.
            _legal_desc = " ".join(
                [
                    str(_cad.get("legal1", "") or "").strip(),
                    str(_cad.get("legal2", "") or "").strip(),
                    str(_cad.get("legal3", "") or "").strip(),
                    str(_cad.get("legal4", "") or "").strip(),
                    str(_cad.get("legal5", "") or "").strip(),
                ]
            ).strip() if _cad else ""
            _own_orphan = _ownership_history_cells(
                ownership_history.get(_cad_key)  # (county_lower, account_num) — same shape as the fetch dict's keys
                if _is_owner_history_supported(_cad_key[0]) and _cad_key[1] else None
            )
            writer.writerow(
                [
                    "Comp",                                                                                     # 1
                    _outreach_parcel_id_cell(_cad),                                                              # 2 Parcel ID (Mike request 2026-06-03 PM)
                    _csv_county_source(_cad) if _cad else "",                                                   # 3 County Source (Phase 2)
                    "",                                                                                          # 4 Intended Target
                    _comp_status_titled,                                                                         # 4 MLS Status (comp's own status)
                    _row_address,                                                                                # 5 Property Address
                    _cad.get("property_city", "") or "",                                                         # 6 Property City
                    "TX" if _cad else "",                                                                        # 7 Property State
                    _cad.get("property_zip", "") or "",                                                          # 8 Property Zip
                    _cad.get("owner_name", "") or "",                                                            # 9
                    _cad.get("owner_address", "") or "",                                                         # 6
                    _cad.get("owner_city", "") or "",                                                            # 7
                    _cad.get("owner_state", "") or "",                                                           # 8
                    _cad.get("owner_zip", "") or "",                                                             # 9
                    round(_safe_float(_cad.get("land_val")), 0) if _safe_float(_cad.get("land_val")) is not None else "",      # 10
                    round(_safe_float(_cad.get("impr_val")), 0) if _safe_float(_cad.get("impr_val")) is not None else "",      # 11
                    round(_safe_float(_cad.get("tot_val")), 0) if _safe_float(_cad.get("tot_val")) is not None else "",        # 12
                    str(_cad.get("total_value_source") or "") if _cad else "",                                                 # 13 Value Source
                    "",                                                                                          # 14 Redfin List Price (parcel-only)
                    round(_safe_float(_cad.get("land_pct")), 1) if _safe_float(_cad.get("land_pct")) is not None else "",      # 15
                    _yr_csv,                                                                                     # 16 Year Built
                    _sqft_csv,                                                                                   # 17 Living Area
                    _main_area_csv,                                                                              # 18 Total Structure Area
                    _cad.get("state_code", "") or _cad.get("sptd_code", "") or "",                               # 19
                    _cad.get("zoning", "") or "",                                                                # 20
                    _lot_sf_csv,                                                                                 # 21 Lot Size (sf)
                    _lot_ac_csv,                                                                                 # 22 Lot Size (ac)
                    _frontage_csv,                                                                               # 23
                    _depth_csv,                                                                                  # 24
                    _est_frontage_csv,                                                                           # 25
                    _est_depth_csv,                                                                              # 26
                    _cad.get("isd_desc", "") or "",                                                              # 27 School District
                    _cad.get("nbhd_cd", "") or "",                                                               # 28 Neighborhood
                    _cad.get("legal1", "") or "",                                                                # 29 Subdivision
                    _legal_desc,                                                                                 # 30 Legal Description (legal1..legal5 joined)
                    _row_lat if _row_lat is not None else "",                                                    # 31 Latitude
                    _row_lng if _row_lng is not None else "",                                                    # 32 Longitude
                    _row_maps_link,                                                                              # 33 Maps Link
                    _cad.get("verified_vacant", "") or "",                                                       # 34
                    _cad.get("potential_target", "") or "",                                                      # 35
                    _hoa_text,                                                                                   # 36
                    _cad.get("hoa_url", "") or "",                                                               # 37
                    _est_lot_sf_csv,                                                                             # 38
                    _est_lot_ac_csv,                                                                             # 39
                    _cad.get("tax_agent_name", "") or "",                                                        # 40
                    _cad.get("tax_agent_id", "") or "",                                                          # 41
                    _cad.get("tax_agent_auth_protest", "") or "",                                                # 42
                    _cad.get("tax_agent_auth_resolve", "") or "",                                                # 43
                    _cad.get("tax_agent_mailings", "") or "",                                                    # 44
                    int(_safe_float(_cad.get("permit_count"))) if _safe_float(_cad.get("permit_count")) not in (None, 0.0) else "",  # 45
                    _cad.get("latest_permit_date", "") or "",                                                    # 46
                    _cad.get("latest_permit_type", "") or "",                                                    # 47
                    round(_safe_float(_cad.get("latest_permit_value")), 0) if _safe_float(_cad.get("latest_permit_value")) is not None else "",  # 48
                    int(_safe_float(_cad.get("protest_case_count"))) if _safe_float(_cad.get("protest_case_count")) not in (None, 0.0) else "",  # 49
                    int(_safe_float(_cad.get("latest_protest_year"))) if _safe_float(_cad.get("latest_protest_year")) not in (None, 0.0) else "",  # 50
                    _cad.get("latest_protest_status", "") or "",                                                 # 51
                    round(_safe_float(_cad.get("latest_protest_final_market")), 0) if _safe_float(_cad.get("latest_protest_final_market")) is not None else "",  # 52
                    _cad.get("protest_active", "") or "",                                                        # 53
                    _cad.get("ag_type", "") or "",                                                               # 54
                    round(_safe_float(_cad.get("ag_acres")), 4) if _safe_float(_cad.get("ag_acres")) is not None else "",  # 55
                    round(_safe_float(_cad.get("ag_value")), 0) if _safe_float(_cad.get("ag_value")) is not None else "",  # 56
                    round(_safe_float(_cad.get("ag_market_value")), 0) if _safe_float(_cad.get("ag_market_value")) is not None else "",  # 57
                    _cad.get("deed_num", "") or "",                                                              # 58
                    _cad.get("deed_type", "") or "",                                                             # 59
                    _cad.get("deed_date", "") or "",                                                             # 60
                    _cad.get("land_type_code", "") or "",                                                        # 61
                    _cad.get("land_type_name", "") or "",                                                        # 62
                    _cad.get("prop_use_code", "") or "",                                                         # 63
                    _cad.get("prop_use_name", "") or "",                                                         # 64
                    _cad.get("class_cd", "") or "",                                                              # 65
                    _cad.get("entity_codes", "") or "",                                                          # 66
                    _cad.get("commercial_flag", "") or "",                                                       # 67
                    _cad.get("pool_flag", "") or "",                                                             # 68
                    _beds_csv,                                                                                   # 69
                    _baths_csv,                                                                                  # 70
                    round(_safe_float(_cad.get("stories")), 1) if _safe_float(_cad.get("stories")) is not None else "",  # 71
                    round(_safe_float(_cad.get("units")), 0) if _safe_float(_cad.get("units")) is not None else "",      # 72
                    # === Phase 2 residential detail cells (comp writerow, matched CAD) ===
                    # Pulls from _cad via canonical keys — same fields as parcel writerow.
                    # Empty when _cad missing (orphan comps populated by the orphan writerow below).
                    round(_safe_float(_cad.get("full_baths")), 0) if _cad and _safe_float(_cad.get("full_baths")) is not None else "",
                    round(_safe_float(_cad.get("half_baths")), 0) if _cad and _safe_float(_cad.get("half_baths")) is not None else "",
                    round(_safe_float(_cad.get("fireplaces")), 0) if _cad and _safe_float(_cad.get("fireplaces")) is not None else "",
                    round(_safe_float(_cad.get("kitchens")), 0) if _cad and _safe_float(_cad.get("kitchens")) is not None else "",
                    round(_safe_float(_cad.get("wet_bars")), 0) if _cad and _safe_float(_cad.get("wet_bars")) is not None else "",
                    round(_safe_float(_cad.get("garage_capacity")), 0) if _cad and _safe_float(_cad.get("garage_capacity")) is not None else "",
                    (_cad.get("stories_desc") or "") if _cad else "",
                    (_cad.get("structure_type") or "") if _cad and (_cad.get("structure_type") or "") != "N/A" else "",
                    (_cad.get("foundation_type") or "") if _cad and (_cad.get("foundation_type") or "") != "N/A" else "",
                    (_cad.get("construction_frame_type") or "") if _cad and (_cad.get("construction_frame_type") or "") != "N/A" else "",
                    (_cad.get("ext_wall") or "") if _cad and (_cad.get("ext_wall") or "") != "N/A" else "",
                    (_cad.get("heating_type") or "") if _cad and (_cad.get("heating_type") or "") != "N/A" else "",
                    (_cad.get("ac_type") or "") if _cad and (_cad.get("ac_type") or "") != "N/A" else "",
                    (_cad.get("roof_type") or "") if _cad and (_cad.get("roof_type") or "") != "N/A" else "",
                    (_cad.get("roof_material") or "") if _cad and (_cad.get("roof_material") or "") != "N/A" else "",
                    (_cad.get("fence_type") or "") if _cad and (_cad.get("fence_type") or "") != "N/A" else "",
                    (_cad.get("basement") or "") if _cad and (_cad.get("basement") or "") != "N/A" else "",
                    (_cad.get("bldg_class") or "") if _cad and (_cad.get("bldg_class") or "") != "N/A" else "",
                    (_cad.get("cdu_rating") or "") if _cad and (_cad.get("cdu_rating") or "") != "N/A" else "",
                    (_cad.get("eff_yr_built") or "") if _cad and (_cad.get("eff_yr_built") or "") != "N/A" else "",
                    (_cad.get("act_age") or "") if _cad and (_cad.get("act_age") or "") != "N/A" else "",
                    (_cad.get("pct_complete") or "") if _cad and (_cad.get("pct_complete") or "") != "N/A" else "",
                    (_cad.get("spa_flag") or "") if _cad else "",
                    (_cad.get("sauna_flag") or "") if _cad else "",
                    (_cad.get("sprinkler_flag") or "") if _cad else "",
                    (_cad.get("deck_flag") or "") if _cad else "",
                    # === end Phase 2 residential detail cells (comp) ===
                    # === Phase 4 Denton-rich residential extras (comp) 2026-05-21 ===
                    (_cad.get("interior_finish") or "") if _cad and (_cad.get("interior_finish") or "") != "N/A" else "",
                    (_cad.get("flooring") or "") if _cad and (_cad.get("flooring") or "") != "N/A" else "",
                    int(_safe_float(_cad.get("total_rooms"))) if _cad and _safe_float(_cad.get("total_rooms")) is not None else "",
                    int(_safe_float(_cad.get("outdoor_fireplaces"))) if _cad and _safe_float(_cad.get("outdoor_fireplaces")) is not None else "",
                    (_cad.get("end_unit") or "") if _cad else "",
                    # === end Phase 4 residential extras (comp) ===
                    round(_safe_float(_cad.get("curr_market_value")), 0) if _safe_float(_cad.get("curr_market_value")) is not None else "",      # was 73
                    round(_safe_float(_cad.get("curr_assessed_value")), 0) if _safe_float(_cad.get("curr_assessed_value")) is not None else "",  # 74
                    round(_safe_float(_cad.get("curr_ag_use_value")), 0) if _safe_float(_cad.get("curr_ag_use_value")) is not None else "",      # 75
                    round(_safe_float(_cad.get("curr_ag_market_value")), 0) if _safe_float(_cad.get("curr_ag_market_value")) is not None else "",  # 76
                    round(_safe_float(_cad.get("curr_ag_loss_value")), 0) if _safe_float(_cad.get("curr_ag_loss_value")) is not None else "",    # 77
                    round(_safe_float(_cad.get("cert_total_value")), 0) if _safe_float(_cad.get("cert_total_value")) is not None else "",        # 78
                    _cad.get("exemptions", "") or "",                                                            # 79 Denton - Exemptions
                    _cad.get("exempt_homestead", "") or "",                                                      # 80 Denton - Homestead
                    _cad.get("deed_number", "") or "",                                                           # 81 Deed Number (Detailed)
                    _cad.get("subdivision", "") or "",                                                           # 82 Subdivision (Parsed)
                    _price_csv,                                                                                  # 86 Comp Sold Price
                    str(_comp_sold_date) if _comp_sold_date else "",                                             # 87 Comp Sold Date
                    _comp_ppsf,                                                                                  # 88 Comp $/sqft
                    int(_yr_comp) if _yr_comp not in (None, 0.0) else "",                                        # 89 Comp Year Built (MLS source)
                    int(_comp_sqft) if _comp_sqft not in (None, 0.0) else "",                                    # 90 Comp Living Area
                    int(_safe_float(_c.get("lot_size"))) if _safe_float(_c.get("lot_size")) not in (None, 0.0) else "",  # 91 Comp Lot Size
                    int(_beds_comp) if _beds_comp is not None else "",                                           # 92 Comp Beds (MLS source)
                    int(_baths_comp) if _baths_comp is not None else "",                                         # 93 Comp Baths (MLS source)
                    _dom_csv,                                                                                    # 94 Comp DOM
                    "",                                                                                          # 95 Comp Listing URL
                    _comp_status_titled,                                                                         # 96 Comp Status
                    # COMPATIBILITY LOCK: Good Comp slots immediately after
                    # Comp Status. Shifted +1 by Value Source at column 13.
                    # Mirrored at header + parcel writerow.
                    # Do not move without coordinated updates at all three sites.
                    "yes" if _orphan_rating == "good" else "",                                                    # 97 Good Comp
                    "",                                                                                          # 98 RF_Comp Sold Price
                    "",                                                                                          # 99 RF_Comp Sold Date
                    "",                                                                                          # 100 RF_Comp $/sqft
                    "",                                                                                          # 101 RF_Comp Year Built
                    "",                                                                                          # 102 RF_Comp Living Area
                    "",                                                                                          # 103 RF_Comp Lot Size
                    "",                                                                                          # 104 RF_Comp Beds
                    "",                                                                                          # 105 RF_Comp Baths
                    "",                                                                                          # 106 RF_Comp DOM
                    "",                                                                                          # 107 RF_Comp Listing URL
                    "",                                                                                          # 108 Seed Target
                    csv_share_id,                                                                                # 109 share_id
                    csv_workspace_name,                                                                          # 110 Workspace Name
                    *stored_value_export_cells,                                                                   # 111-120 stored values snapshot
                    # === v4a §2.8 — 28 new cells (mirror header order) ===
                    # (property_city/TX/property_zip moved to positions 6-8 near _row_address)
                    _cad.get("street_num", "") or "",
                    _cad.get("street_half_num", "") or "",
                    _cad.get("full_street_name", "") or "",
                    _cad.get("bldg_id", "") or "",
                    _cad.get("unit_id", "") or "",
                    _cad.get("mapsco", "") or "",
                    _cad.get("owner_name2", "") or "",
                    _cad.get("biz_name", "") or "",
                    _cad.get("owner_address_line1", "") or "",
                    _cad.get("owner_address_line2", "") or "",
                    _cad.get("owner_address_line3", "") or "",
                    _cad.get("owner_address_line4", "") or "",
                    _cad.get("owner_country", "") or "",
                    _cad.get("city_jurisdiction_desc", "") or "",
                    _cad.get("county_jurisdiction_desc", "") or "",
                    _cad.get("hospital_jurisdiction_desc", "") or "",
                    _cad.get("college_jurisdiction_desc", "") or "",
                    _cad.get("special_dist_jurisdiction_desc", "") or "",
                    round(_safe_float(_cad.get("city_taxable_val")), 0) if _cad and _safe_float(_cad.get("city_taxable_val")) is not None else "",
                    round(_safe_float(_cad.get("county_taxable_val")), 0) if _cad and _safe_float(_cad.get("county_taxable_val")) is not None else "",
                    round(_safe_float(_cad.get("isd_taxable_val")), 0) if _cad and _safe_float(_cad.get("isd_taxable_val")) is not None else "",
                    round(_safe_float(_cad.get("hospital_taxable_val")), 0) if _cad and _safe_float(_cad.get("hospital_taxable_val")) is not None else "",
                    round(_safe_float(_cad.get("college_taxable_val")), 0) if _cad and _safe_float(_cad.get("college_taxable_val")) is not None else "",
                    round(_safe_float(_cad.get("special_dist_taxable_val")), 0) if _cad and _safe_float(_cad.get("special_dist_taxable_val")) is not None else "",
                    *_additional_owner_cells(_cad.get("additional_owners") if _cad else []),
                    _own_orphan[0], _own_orphan[1], _own_orphan[2], _own_orphan[3], _own_orphan[4], _own_orphan[5],
                    # Mailer + Phone Tracking — 4 cells. _outreach_csv_cells
                    # handles _cad=None internally (returns 4 blanks).
                    *_outreach_csv_cells(_cad, user),
                    _flood_zone_csv_cell(_cad),
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


# Sprint 3 multi-user collab — SSE listener lifecycle.
# Per docs/MULTIUSER_COLLAB_SPRINT3_SPEC.md v1 §3.6.

def _build_session_dsn() -> str:
    """Build asyncpg-compatible DSN for the lotledger_sessions DB.
    Mirrors api/config.py:54-78 — when INSTANCE_UNIX_SOCKET is set
    (Cloud Run), uses the Unix socket; otherwise TCP for local dev.

    Sprint 3 hotfix: initial deploy defaulted to 127.0.0.1:5432 which
    is wrong on Cloud Run — there's no Postgres on localhost, the
    connection goes through the Cloud SQL Auth Proxy Unix socket
    at /cloudsql/<instance>. asyncpg supports the same URL shape
    as psycopg2 via the `host` query parameter for Unix sockets.
    """
    import os
    from urllib.parse import quote_plus as _q
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    db = os.getenv("SESSION_DB_NAME", "lotledger_sessions")
    unix_socket = os.environ.get("INSTANCE_UNIX_SOCKET", "").strip()
    if unix_socket:
        # Cloud Run: Postgres via Cloud SQL Auth Proxy Unix socket.
        return f"postgresql://{user}:{_q(password)}@/{db}?host={unix_socket}"
    # Local dev or non-Cloud-Run deploy: TCP host:port.
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = os.getenv("DB_PORT", "5432")
    return f"postgresql://{user}:{_q(password)}@{host}:{port}/{db}"


@app.on_event("startup")
async def _startup_sse_listener() -> None:
    """Start the background asyncpg LISTEN task. Mirrors the
    session-cleanup startup pattern above."""
    from api.sse import _listen_forever
    dsn = _build_session_dsn()
    app.state.sse_listen_task = asyncio.create_task(_listen_forever(dsn))
    logger.info("[sse-listen] startup task created")


@app.on_event("shutdown")
async def _shutdown_sse_listener() -> None:
    """Cancel + await the background LISTEN task on app shutdown."""
    task = getattr(app.state, "sse_listen_task", None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    logger.info("[sse-listen] shutdown complete")


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

    def _query() -> list[dict[str, Any]]:
        conn = get_session_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.session_id, s.name, s.parcel_count, s.county_coverage, s.filter_state, s.polygon, s.created_at,
                           sa.originator_parcel_county, sa.originator_parcel_account_num
                    FROM analysis_sessions s
                    LEFT JOIN saved_areas sa ON sa.area_id = s.saved_area_id
                    WHERE s.user_id = %s AND s.name IS NOT NULL
                    ORDER BY s.created_at DESC
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
                        "originator_parcel_county": str(row[7] or "").strip().lower() or None,
                        "originator_parcel_account_num": str(row[8] or "").strip() or None,
                    })
                return sessions
        finally:
            release_session_conn(conn)

    # Tier 2: run the blocking session-DB query off the event loop.
    sessions = await asyncio.to_thread(_query)
    return {"sessions": sessions}


@app.get("/api/sessions/{session_id}/data")
async def get_session_data(session_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Return a named session's full parcel data from cached_jobs (same shape as /api/analyze)."""
    uid = int(user["id"])
    conn = get_session_conn()
    session_name: str | None = None
    originator_county: str | None = None
    originator_account: str | None = None
    session_saved_area_id: str | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.name, sa.originator_parcel_county, sa.originator_parcel_account_num, s.saved_area_id
                FROM analysis_sessions s
                LEFT JOIN saved_areas sa ON sa.area_id = s.saved_area_id
                WHERE s.session_id = %s AND s.user_id = %s AND s.name IS NOT NULL
                """,
                (session_id, uid),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Named session not found")
            session_name = str(row[0] or "").strip() or None
            originator_county = str(row[1] or "").strip().lower() or None
            originator_account = str(row[2] or "").strip() or None
            session_saved_area_id = str(row[3] or "").strip() or None
    finally:
        release_session_conn(conn)

    cached = _load_cached_job(session_id)
    if cached is None or int(cached.get("user_id", -1)) != uid:
        raise HTTPException(status_code=404, detail="Session data not found in cache")

    rows = list(cached.get("rows", []))
    sold_points = list(cached.get("sold_points", []))
    _apply_session_tags(session_id, uid, rows)
    # Pass area_id so parcel ratings hydrate per PARCEL_RATINGS_SPEC.md v2.
    # Pass user so outreach_* keys are stripped for non-power_user roles.
    features, counts = _build_features_from_rows(rows, area_id=session_saved_area_id, user=user)
    return {
        "type": "FeatureCollection",
        "session_id": session_id,
        "name": session_name,
        "originator_parcel_county": originator_county,
        "originator_parcel_account_num": originator_account,
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


def _resolve_originator_coords_batch(
    originators: list[tuple[str, str]],
) -> dict[tuple[str, str], tuple[float, float]]:
    """Bulk-resolve parcel centroids for a list of (county, account_num) pairs.

    Opens its OWN data-DB connection via get_conn() — parcel tables live on
    that DB, NOT on the session DB that backs saved_areas. Callers MUST NOT
    pass a session cursor in.

    Groups by county and runs one query per county so the cost is
    O(counties) instead of O(originators). Returns a dict keyed by
    (lowercased_county, stripped_account_num) → (lat, lng) for each pair
    that resolved. Missing pairs are absent (caller treats absence as
    "originator unresolved").
    """
    if not originators:
        return {}

    by_county: dict[str, set[str]] = {}
    for county, account in originators:
        c = str(county or "").strip().lower()
        a = str(account or "").strip()
        if not c or not a:
            continue
        by_county.setdefault(c, set()).add(a)

    if not by_county:
        return {}

    table_for: dict[str, str] = {
        "dcad": "parcels",
        "tad": "tad_parcels",
        "collin": "collin_parcels",
        "denton": "denton_parcels",
    }

    out: dict[tuple[str, str], tuple[float, float]] = {}
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for county, accounts in by_county.items():
                table = table_for.get(county)
                if not table:
                    continue
                cur.execute(
                    f"""
                    SELECT account_num,
                           ST_Y(centroid) AS lat,
                           ST_X(centroid) AS lng
                    FROM {table}
                    WHERE account_num = ANY(%s) AND centroid IS NOT NULL
                    """,
                    (list(accounts),),
                )
                for r in cur.fetchall():
                    acct = str(r[0]).strip()
                    lat_raw = r[1]
                    lng_raw = r[2]
                    if lat_raw is None or lng_raw is None:
                        continue
                    try:
                        out[(county, acct)] = (float(lat_raw), float(lng_raw))
                    except (TypeError, ValueError):
                        continue
    finally:
        release_conn(conn)
    return out


@app.get("/api/areas")
async def list_saved_areas(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    # Sprint 1 multi-user collab: return areas the caller is a member of
    # (owner OR editor). The role column travels with each row so the
    # frontend can drive affordance gating (spec §3 row 1, §3.5).
    uid = int(user["id"])

    def _fetch_rows():
        conn = get_session_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT sa.area_id, sa.name, sa.polygon, sa.filter_state, sa.type, sa.share_id,
                           sa.originator_parcel_county, sa.originator_parcel_account_num,
                           sa.created_at, sa.updated_at,
                           sam.role
                    FROM saved_areas sa
                    JOIN saved_area_members sam
                      ON sam.area_id = sa.area_id
                     AND sam.user_id = %s
                    ORDER BY sa.created_at DESC
                    """,
                    (uid,)
                )
                return cur.fetchall()
        finally:
            release_session_conn(conn)

    # Tier 2: blocking session-DB query off the event loop.
    rows = await asyncio.to_thread(_fetch_rows)

    originators_to_resolve: list[tuple[str, str]] = []
    for row in rows:
        c = str(row[6] or "").strip().lower()
        a = str(row[7] or "").strip()
        if c and a:
            originators_to_resolve.append((c, a))
    # Centroid lookups hit the DATA DB (parcel tables live there), NOT the
    # session DB that backs saved_areas. The batch helper opens its own
    # get_conn() pool — must be called OUTSIDE the session-cursor scope.
    resolved_coords = await asyncio.to_thread(_resolve_originator_coords_batch, originators_to_resolve)

    areas: list[dict[str, Any]] = []
    subject_props_acc: dict[tuple[str, str], dict[str, Any]] = {}

    for row in rows:
        area_type = str(row[4] or "area")
        county = str(row[6] or "").strip().lower() or None
        account = str(row[7] or "").strip() or None

        originator_unresolved = bool(
            county and account and (county, account) not in resolved_coords
        )

        created_at_iso = row[8].isoformat() if row[8] else None
        updated_at_iso = row[9].isoformat() if row[9] else None

        areas.append({
            "area_id": row[0],
            "name": row[1],
            "polygon": _to_leaflet_polygon(row[2]),
            "filter_state": row[3] if isinstance(row[3], dict) else None,
            "type": area_type,
            "share_id": str(row[5] or ""),
            "originator_parcel_county": county,
            "originator_parcel_account_num": account,
            "originator_unresolved": originator_unresolved,
            "lat": (float(row[2][0][1]) if isinstance(row[2], list) and row[2] and len(row[2][0]) >= 2 else None) if area_type == "location" else None,
            "lng": (float(row[2][0][0]) if isinstance(row[2], list) and row[2] and len(row[2][0]) >= 2 else None) if area_type == "location" else None,
            "created_at": created_at_iso,
            "updated_at": updated_at_iso,
            "role": str(row[10] or "owner"),  # Sprint 1: 'owner' | 'editor'
        })

        if county and account and not originator_unresolved:
            coord = resolved_coords[(county, account)]
            key = (county, account)
            entry = subject_props_acc.get(key)
            if entry is None:
                entry = {
                    "county": county,
                    "account_num": account,
                    "lat": coord[0],
                    "lng": coord[1],
                    "areas": [],
                }
                subject_props_acc[key] = entry
            entry["areas"].append({
                "area_id": row[0],
                "name": row[1],
                "updated_at": updated_at_iso,
            })

    subject_properties: list[dict[str, Any]] = []
    for entry in subject_props_acc.values():
        entry["areas"].sort(key=lambda a: a["updated_at"] or "", reverse=True)
        subject_properties.append(entry)
    return {"areas": areas, "subject_properties": subject_properties}


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

            # Sprint 1 multi-user collab: record the creator as the area's
            # 'owner' member (spec §3 row 2). Placed AFTER the SAVEPOINT
            # loop completes so a UniqueViolation retry doesn't roll back
            # the membership INSERT; placed BEFORE the transaction commit
            # so both rows land atomically (Copilot S-4).
            cur.execute(
                """
                INSERT INTO saved_area_members (area_id, user_id, role, added_via)
                VALUES (%s, %s, 'owner', 'owner')
                ON CONFLICT (area_id, user_id) DO NOTHING
                """,
                (row[0], int(user["id"])),
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
    # Sprint 1: membership check (spec §3 row 3) replaces row[8] owner check.
    # 404 first (above), 403 below — preserves the "area doesn't exist vs.
    # area exists but you're not a member" distinction.
    _assert_user_is_area_member(area_id, int(user["id"]))
    caller_role = _user_area_role(area_id, int(user["id"]))

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
        "role": caller_role,  # Sprint 1: 'owner' | 'editor' for the calling user
    }


@app.get("/api/areas/{area_id}/stored-value")
async def get_area_stored_value(
    area_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> StoredValueGetResponse:
    # Sprint 1 multi-user collab: membership check (spec §3 row 4).
    # Editors can read stored values on shared areas. _assert_user_is_area_member
    # raises 403 if not a member; preserves 404 semantics via the existence
    # check below (area-doesn't-exist vs. area-exists-but-no-access).
    if not _saved_area_exists(area_id):
        raise HTTPException(status_code=404, detail="Saved area not found")
    _assert_user_is_area_member(area_id, int(user["id"]))
    conn = get_session_conn()
    try:
        return _stored_value_build_response(conn, area_id)
    finally:
        release_session_conn(conn)


@app.put("/api/areas/{area_id}/stored-value")
async def put_area_stored_value(
    area_id: str,
    body: StoredValuePutRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> StoredValueGetResponse:
    require_csrf(request)

    field_key = str(body.field_key or "").strip().lower()
    if field_key in _STORED_VALUE_CALC_FIELD_KEYS and body.numeric_value is not None:
        raise HTTPException(status_code=400, detail="calc fields are read-only for numeric_value")

    # Sprint 1 multi-user collab: membership check (spec §3 row 5). Editors
    # can write stored values on shared areas.
    if not _saved_area_exists(area_id):
        raise HTTPException(status_code=404, detail="Saved area not found")
    _assert_user_is_area_member(area_id, int(user["id"]))

    conn = get_session_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute(
                """
                INSERT INTO stored_value_entries
                  (area_id, field_key, numeric_value, comment_text, client_seq)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (area_id, field_key) DO UPDATE SET
                  numeric_value = EXCLUDED.numeric_value,
                  comment_text  = EXCLUDED.comment_text,
                  client_seq    = EXCLUDED.client_seq,
                  updated_at    = now()
                WHERE stored_value_entries.client_seq < EXCLUDED.client_seq
                RETURNING client_seq, numeric_value, comment_text
                """,
                (
                    area_id,
                    field_key,
                    body.numeric_value,
                    body.comment_text,
                    int(body.client_seq),
                ),
            )
            updated = cur.fetchone()
            if updated is None:
                cur.execute(
                    """
                    SELECT client_seq, numeric_value, comment_text
                    FROM stored_value_entries
                    WHERE area_id = %s AND field_key = %s
                    LIMIT 1
                    """,
                    (area_id, field_key),
                )
                current = cur.fetchone()
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "stale write rejected",
                        "current": {
                            "field_key": field_key,
                            "client_seq": int(current.get("client_seq") or 0) if current else 0,
                            "numeric_value": int(current.get("numeric_value")) if current and current.get("numeric_value") is not None else None,
                            "comment_text": str(current.get("comment_text") or "") if current else "",
                        },
                    },
                )

            # Sprint 3 hotfix (2026-06-02): broadcast stored value changes
            # on winning writes (updated is not None means UPSERT WHERE
            # client_seq < EXCLUDED passed). Mirror of filter PATCH §3.2.
            import json as _json_sv
            _sv_notify = _json_sv.dumps({
                "area_id": area_id,
                "type": "stored_value",
                "field_key": field_key,
                "numeric_value": int(updated.get("numeric_value")) if updated.get("numeric_value") is not None else None,
                "comment_text": str(updated.get("comment_text") or ""),
                "client_seq": int(updated.get("client_seq") or 0),
                "by_user_id": int(user["id"]),
                "by_session_id": request.headers.get("x-session-id", ""),
            })
            cur.execute(
                "SELECT pg_notify('saved_area_filter_changes', %s)",
                (_sv_notify,),
            )

        conn.commit()
        return _stored_value_build_response(conn, area_id)
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


# Sprint 2 multi-user collab: per-field filter PATCH endpoint
# per docs/MULTIUSER_COLLAB_SPRINT2_SPEC.md v2 §3.
@app.patch("/api/areas/{area_id}/filter-fields/{field_key}")
async def patch_area_filter_field(
    area_id: str,
    field_key: str,
    body: FilterFieldPatchRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> FilterFieldPatchResponse:
    """Sprint 2 per-field filter PATCH. Per spec §3.

    Editor or owner can write. UPSERT with `client_seq < EXCLUDED` guard
    enforces LWW per (area, field). Cache `saved_areas.filter_state`
    updated transactionally via two-step CASE-WHEN/jsonb_set (Copilot
    B-2 fix — bare jsonb_set with create_missing silently NO-OPs on
    null/missing parents, verified on PG 14.23).
    """
    require_csrf(request)

    # Membership check (Sprint 1 editor-tier).
    if not _saved_area_exists(area_id):
        raise HTTPException(status_code=404, detail="Saved area not found")
    _assert_user_is_area_member(area_id, int(user["id"]))

    # Allowlist validation (spec §2.1.A + drift regression test).
    if field_key not in _FILTER_FIELD_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown filter field_key: {field_key!r}")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            # Lazy backfill (rolling-deploy safety, spec §2.2): if this
            # area has no per-field rows yet, explode the cached blob
            # INSIDE the same transaction as the UPSERT. Same jsonb_typeof
            # guards as the eager backfill (Copilot B-1 fix).
            cur.execute(
                "SELECT 1 FROM saved_area_filter_fields WHERE area_id = %s LIMIT 1",
                (area_id,),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """
                    INSERT INTO saved_area_filter_fields
                      (area_id, field_key, value, client_seq, updated_by_user_id)
                    SELECT
                        sa.area_id,
                        sections.section || '.' || sections.key,
                        sections.val,
                        0,
                        sa.user_id
                    FROM saved_areas sa
                    CROSS JOIN LATERAL (
                        SELECT 'checkboxes' AS section, key, value AS val
                          FROM jsonb_each(sa.filter_state -> 'checkboxes')
                          WHERE jsonb_typeof(sa.filter_state -> 'checkboxes') = 'object'
                        UNION ALL
                        SELECT 'numeric', key, value FROM jsonb_each(sa.filter_state -> 'numeric')
                          WHERE jsonb_typeof(sa.filter_state -> 'numeric') = 'object'
                        UNION ALL
                        SELECT 'sold', key, value FROM jsonb_each(sa.filter_state -> 'sold')
                          WHERE jsonb_typeof(sa.filter_state -> 'sold') = 'object'
                        UNION ALL
                        SELECT 'comp', key, value FROM jsonb_each(sa.filter_state -> 'comp')
                          WHERE jsonb_typeof(sa.filter_state -> 'comp') = 'object'
                        UNION ALL
                        SELECT 'propelio', key, value FROM jsonb_each(sa.filter_state -> 'propelio')
                          WHERE jsonb_typeof(sa.filter_state -> 'propelio') = 'object'
                    ) sections
                    WHERE sa.area_id = %s
                      AND jsonb_typeof(sa.filter_state) = 'object'
                    ON CONFLICT (area_id, field_key) DO NOTHING
                    """,
                    (area_id,),
                )

            # UPSERT with stale-write rejection via client_seq guard.
            cur.execute(
                """
                INSERT INTO saved_area_filter_fields
                  (area_id, field_key, value, client_seq, updated_by_user_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (area_id, field_key) DO UPDATE
                SET value              = EXCLUDED.value,
                    client_seq         = EXCLUDED.client_seq,
                    updated_by_user_id = EXCLUDED.updated_by_user_id,
                    updated_at         = now()
                WHERE saved_area_filter_fields.client_seq < EXCLUDED.client_seq
                RETURNING value, client_seq, updated_at, updated_by_user_id
                """,
                (area_id, field_key, Json(body.value), int(body.client_seq), int(user["id"])),
            )
            row = cur.fetchone()
            # Sprint 3 §3.2 + Agent C C-4: track whether this PATCH WON the
            # UPSERT race. If RETURNING produced a row, our client_seq beat
            # the persisted one and we should broadcast. If it returned None,
            # we lost — re-fetch the winning value for client reconciliation
            # but DO NOT broadcast a phantom NOTIFY.
            patch_won = row is not None
            if row is None:
                # Stale write — re-fetch current persisted state for client
                # to reconcile against.
                cur.execute(
                    """
                    SELECT value, client_seq, updated_at, updated_by_user_id
                    FROM saved_area_filter_fields
                    WHERE area_id = %s AND field_key = %s
                    """,
                    (area_id, field_key),
                )
                row = cur.fetchone()

            # Update denormalized cache. Copilot B-2 fix: bare jsonb_set
            # with create_missing=true silently NO-OPs when the section
            # parent is null/missing/non-object. Two-step CASE-WHEN guard
            # coerces parent to {} first, then sets the leaf.
            #
            # _views branch: a 4-level path needs EVERY intermediate parent
            # built explicitly (PG 14.23 silently no-ops otherwise — same
            # hazard, deeper). Nested jsonb_set ensures _views → _views.<v>
            # → _views.<v>.<section> all exist before writing the leaf.
            # The flat (ARV) path is byte-for-byte unchanged.
            if field_key.startswith("_views."):
                _, view, section, key = field_key.split(".")
                cur.execute(
                    """
                    UPDATE saved_areas
                    SET filter_state = jsonb_set(
                        jsonb_set(
                            jsonb_set(
                                jsonb_set(COALESCE(filter_state,'{}'::jsonb),
                                          ARRAY['_views'],
                                          COALESCE(filter_state->'_views','{}'::jsonb),
                                          true),
                                ARRAY['_views',%s],
                                COALESCE(filter_state->'_views'->%s,'{}'::jsonb),
                                true),
                            ARRAY['_views',%s,%s],
                            COALESCE(filter_state->'_views'->%s->%s,'{}'::jsonb),
                            true),
                        ARRAY['_views',%s,%s,%s], %s::jsonb, true
                    ),
                    updated_at = now()
                    WHERE area_id = %s
                    """,
                    (view, view, view, section, view, section, view, section, key, Json(row[0]), area_id),
                )
            else:
                section, _, key = field_key.partition('.')
                cur.execute(
                    """
                    UPDATE saved_areas
                    SET filter_state = jsonb_set(
                        CASE
                            WHEN jsonb_typeof(COALESCE(filter_state, '{}'::jsonb) -> %s) = 'object'
                            THEN COALESCE(filter_state, '{}'::jsonb)
                            ELSE jsonb_set(
                                COALESCE(filter_state, '{}'::jsonb),
                                ARRAY[%s],
                                '{}'::jsonb,
                                true
                            )
                        END,
                        %s::text[],
                        %s::jsonb,
                        true
                    ),
                    updated_at = now()
                    WHERE area_id = %s
                    """,
                    (section, section, [section, key], Json(row[0]), area_id),
                )

            # Sprint 3 §3.2: broadcast on winning writes only. NOTIFY is
            # delivered atomically with the transaction commit per Postgres
            # semantics, so multi-listener instances all see this on commit.
            # Per Agent C catch C-4 — gate on patch_won to avoid phantom
            # broadcasts for stale-write reconciliation re-fetches.
            if patch_won and row is not None:
                import json as _json
                _notify_payload = _json.dumps({
                    "area_id": area_id,
                    "field_key": field_key,
                    "value": body.value,
                    "client_seq": int(body.client_seq),
                    "by_user_id": int(user["id"]),
                    "by_session_id": request.headers.get("x-session-id", ""),
                    "updated_at": row[2].isoformat() if row[2] else None,
                })
                cur.execute(
                    "SELECT pg_notify('saved_area_filter_changes', %s)",
                    (_notify_payload,),
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

    return FilterFieldPatchResponse(
        area_id=area_id,
        field_key=field_key,
        value=row[0],
        client_seq=int(row[1]),
        updated_at=row[2].isoformat() if row[2] else None,
        updated_by_user_id=row[3],
    )


# Sprint 3 multi-user collab: SSE live-push endpoint.
# Per docs/MULTIUSER_COLLAB_SPRINT3_SPEC.md v1 §3.5.
@app.get("/api/areas/{area_id}/events")
async def stream_area_events(
    area_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    """SSE stream for live filter-change push. Membership-gated.

    Subscribes the client to the area's broadcast queue. NOTIFY events
    from any FastAPI process arrive via the LISTEN connection and get
    fanned out via the in-process subscriber map (see api/sse.py).

    Returns sse-starlette EventSourceResponse with ping=30s heartbeat.
    Per Agent C: timeout-with-disconnect-check pattern prevents the
    generator from parking forever on dead clients.
    """
    from sse_starlette import EventSourceResponse
    import asyncio as _asyncio
    import json as _json
    from api.sse import register_subscriber, unregister_subscriber

    # Membership gating (Sprint 1).
    if not _saved_area_exists(area_id):
        raise HTTPException(status_code=404, detail="Saved area not found")
    _assert_user_is_area_member(area_id, int(user["id"]))

    # Origin check (Agent C minor-risk mitigation).
    origin = request.headers.get("origin", "")
    request_url = str(request.url)
    expected_origin = request_url.split("/api/")[0] if "/api/" in request_url else ""
    if origin and expected_origin and origin != expected_origin:
        raise HTTPException(status_code=403, detail="Cross-origin SSE rejected")

    queue: _asyncio.Queue = _asyncio.Queue(maxsize=100)
    await register_subscriber(area_id, queue)

    async def event_generator():
        try:
            # Initial 'connected' event signals stream is live.
            yield {
                "event": "connected",
                "data": _json.dumps({"area_id": area_id, "user_id": int(user["id"])}),
            }
            # KK debug 2026-06-06: synthetic resync immediately after
            # connected. PostgreSQL LISTEN/NOTIFY does NOT buffer for
            # disconnected subscribers — any pg_notify fired while a
            # client EventSource was zombie / reconnecting is LOST.
            # _broadcast_resync_to_all in api/sse.py only fires on the
            # server-side LISTEN connection reconnect (very rare). The
            # much more common client EventSource reconnect (Edge tab
            # throttling, network blips, watchdog force-close) had no
            # resync mechanism — leading to "tab B's star didn't update
            # because the area_meta_change event fired during a zombie
            # window." Frontend resync handler already calls
            # _reloadSavedResources via _sseRefetchArea; reusing it
            # avoids any new frontend code.
            #
            # Trade-off: every connect (initial page load AND every
            # reconnect) triggers one extra GET /api/areas + /api/parcels
            # + /api/sessions on the client. On page load this is
            # near-zero overhead since those endpoints get hit anyway by
            # other init code. The much bigger fix is "users don't lose
            # state silently."
            yield {
                "event": "resync",
                "data": _json.dumps({
                    "area_id": area_id,
                    "reason": "client_connect",
                }),
            }
            while True:
                # Agent C catch: timeout-with-disconnect-check pattern.
                # Without timeout, the generator parks forever on dead clients.
                try:
                    msg = await _asyncio.wait_for(queue.get(), timeout=30.0)
                except _asyncio.TimeoutError:
                    # Heartbeat tick. sse-starlette's built-in ping=30 also
                    # fires; this is belt-and-suspenders for disconnect detection.
                    if await request.is_disconnected():
                        break
                    yield {
                        "event": "heartbeat",
                        "data": "{}",
                    }
                    continue
                if await request.is_disconnected():
                    break
                # Determine event type.
                event_type = "message"
                if isinstance(msg, dict) and msg.get("type") in (
                    "resync", "blob_explode", "stored_value",
                    "saved_parcel_change", "area_meta_change",
                ):
                    event_type = msg["type"]
                yield {
                    "event": event_type,
                    "data": _json.dumps(msg),
                }
        finally:
            # Always clean up subscriber map — even on cancellation /
            # exception / disconnect.
            await unregister_subscriber(area_id, queue)

    return EventSourceResponse(
        event_generator(),
        ping=30,  # built-in heartbeat (Agent A: don't hand-roll)
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # tell intermediaries not to buffer
        },
    )


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

            # parcel_ratings — per PARCEL_RATINGS_SPEC.md v2 §2G. Fork
            # copies direct parcel ratings parallel to comp ratings so
            # the forked workspace inherits the source's judgment.
            cur.execute(
                """
                INSERT INTO parcel_ratings (workspace_id, county, account_num, rating, rated_by_user_id, rated_at)
                SELECT %s, county, account_num, rating, rated_by_user_id, rated_at
                FROM parcel_ratings
                WHERE workspace_id = %s
                """,
                (row[0], source_area_id),
            )

            # comp_ratings_views — per-view (nbv/export) comp ratings. Per
            # per-view ratings spec §3.3 / FMEA C5: fork copies the _views
            # tables parallel to the ARV comp_ratings copy above so the
            # forked workspace inherits the source's per-view judgments.
            # rating_id excluded (BIGSERIAL auto-generates); view copied
            # verbatim. Same cursor + transaction as the copies above.
            cur.execute(
                """
                INSERT INTO comp_ratings_views (workspace_id, comp_id, view, rating, rated_by_user_id, rated_at)
                SELECT %s, comp_id, view, rating, rated_by_user_id, rated_at
                FROM comp_ratings_views
                WHERE workspace_id = %s
                """,
                (row[0], source_area_id),
            )

            # parcel_ratings_views — per-view (nbv/export) direct parcel
            # ratings, parallel to the parcel_ratings copy above. Same
            # discipline: rating_id excluded, view copied verbatim, same
            # cursor + transaction. FMEA C5.
            cur.execute(
                """
                INSERT INTO parcel_ratings_views (workspace_id, county, account_num, view, rating, rated_by_user_id, rated_at)
                SELECT %s, county, account_num, view, rating, rated_by_user_id, rated_at
                FROM parcel_ratings_views
                WHERE workspace_id = %s
                """,
                (row[0], source_area_id),
            )

            # stored_value_entries — copy the source area's stored values
            # (ARV / TDPP / rehab + comments) into the fork, parallel to the
            # rating copies above, so a shared-then-opened (forked) area keeps
            # its stored values instead of loading blank.
            cur.execute(
                """
                INSERT INTO stored_value_entries
                    (area_id, field_key, numeric_value, comment_text, client_seq)
                SELECT %s, field_key, numeric_value, comment_text, client_seq
                FROM stored_value_entries
                WHERE area_id = %s
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


# ─── Sprint 1 multi-user collab: join-by-share-id (spec §4.1) ───────────


@app.post("/api/areas/by-share-id/{share_id}/join")
async def join_saved_area_via_share_id(
    req: Request,
    share_id: str = FastAPIPath(..., regex=r"^area_[A-Za-z0-9]{10}$"),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Grant the calling user 'editor' membership on the area pointed at by
    this share_id. Replaces the auto-fork pattern (spec §4.1). Idempotent —
    re-calling for an already-member returns their existing role.
    """
    require_csrf(req)
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT area_id, user_id FROM saved_areas WHERE share_id = %s LIMIT 1",
                (share_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Saved area not found")
            area_id, owner_id = str(row[0]), int(row[1] or 0)

            # No-op if the caller IS the owner. Lazy-backfill via the
            # helper covers the case where their membership row is
            # missing (rolling-deploy window).
            if owner_id == int(user["id"]):
                cur.execute(
                    "SELECT role FROM saved_area_members WHERE area_id = %s AND user_id = %s",
                    (area_id, int(user["id"])),
                )
                existing = cur.fetchone()
                role = str(existing[0]) if existing else "owner"
                conn.commit()
                return {"area_id": area_id, "role": role, "already_member": True}

            cur.execute(
                """
                INSERT INTO saved_area_members (area_id, user_id, role, added_via)
                VALUES (%s, %s, 'editor', 'share_link')
                ON CONFLICT (area_id, user_id) DO NOTHING
                RETURNING role
                """,
                (area_id, int(user["id"])),
            )
            inserted = cur.fetchone()
            # Copilot SQ-2: if INSERT was a no-op (user already has a
            # membership row, possibly with a non-editor role from a
            # future promotion path), return the ACTUAL role.
            if inserted is None:
                cur.execute(
                    "SELECT role FROM saved_area_members WHERE area_id = %s AND user_id = %s",
                    (area_id, int(user["id"])),
                )
                existing = cur.fetchone()
                role = str(existing[0]) if existing else "editor"
            else:
                role = "editor"
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
        "area_id": area_id,
        "role": role,
        "already_member": inserted is None,
    }


@app.put("/api/areas/{area_id}")
async def update_saved_area(area_id: str, request: SavedAreaUpdateRequest, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)
    if request.name is None and request.filter_state is None and request.type is None and request.lat is None and request.lng is None and request.originator_parcel_county is None and request.originator_parcel_account_num is None and request.polygon is None:
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
        # Merge instead of overwrite so a whole-blob PUT never silently wipes
        # _views data written by PATCH (ARV·NBV·Export feature flag).
        update_cols.append("filter_state = COALESCE(filter_state,'{}') || %s::jsonb")
        params.append(Json(request.filter_state) if isinstance(request.filter_state, dict) else Json({}))

    if request.polygon is not None:
        if not isinstance(request.polygon, list) or len(request.polygon) < 3:
            raise HTTPException(status_code=400, detail="polygon must have at least 3 coordinate pairs")
        update_cols.append("polygon = %s")
        params.append(Json(_to_geojson_polygon(request.polygon)))

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

    # Sprint 1 multi-user collab: tiered auth (spec §3.1).
    #   - Owner-only fields: name, type, lat/lng (polygon), originator_parcel_*.
    #   - Editor-allowed fields: filter_state only.
    # Developer bypass skips both tiers as today.
    owner_only_fields = (
        request.name is not None
        or request.type is not None
        or request.lat is not None
        or request.lng is not None
        or request.polygon is not None
        or request.originator_parcel_county is not None
        or request.originator_parcel_account_num is not None
    )
    editor_fields = request.filter_state is not None

    # Fix B (2026-07-12): app-role `owner` may rename areas SHARED TO THEM — not just
    # areas they created, and NOT arbitrary areas. This override does NOT skip
    # authorization: it SWAPS the creator-ownership assert for a membership assert
    # (see the gate below). `name` is the sole owner-only field it permits — polygon
    # reshape / type / originator stay creator-owner-only for the owner role.
    # `developer` keeps its existing full bypass, unchanged.
    is_owner_role = str(user.get("role") or "").strip().lower() == "owner"
    rename_only = (
        request.name is not None
        and request.type is None
        and request.lat is None
        and request.lng is None
        and request.polygon is None
        and request.originator_parcel_county is None
        and request.originator_parcel_account_num is None
    )
    owner_rename_override = is_owner_role and rename_only

    if not is_developer:
        if owner_rename_override:
            # App-role owner renaming: allowed on any area they can SEE (own or shared to
            # them), not on arbitrary areas. DECIDED with KK 2026-07-12 (scope option "b").
            _assert_user_is_area_member(area_id, int(user["id"]))
        elif owner_only_fields:
            _assert_user_owns_area(area_id, int(user["id"]))
        elif editor_fields:
            _assert_user_is_area_member(area_id, int(user["id"]))
        # else: the earlier 400 "Nothing to update" guard already triggered.

    # Copilot B-1: drop `AND user_id = %s` from WHERE — the pre-check above
    # is the gate. Without this, editors silently 404 because the row's
    # user_id != caller's user_id.
    where_clause = "WHERE area_id = %s"
    query_params: list[Any] = [*params, area_id]

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

            # Sprint 2 multi-user collab (spec §4): when filter_state was
            # in the PUT body, ALSO explode the blob into per-field rows
            # transactionally so the new per-field table stays consistent
            # with the cached blob. Uses client_seq=0 so any subsequent
            # per-field PATCH (using monotonic seq >= 1) wins on conflict —
            # this preserves the "per-field PATCH always wins over wholesale
            # blob PUT" semantic.
            # Same jsonb_typeof guards as eager backfill (Copilot B-1 fix).
            if request.filter_state is not None and isinstance(request.filter_state, dict):
                cur.execute(
                    """
                    INSERT INTO saved_area_filter_fields
                      (area_id, field_key, value, client_seq, updated_by_user_id)
                    SELECT
                        %s::text                                   AS area_id,
                        sections.section || '.' || sections.key    AS field_key,
                        sections.val                                AS value,
                        0                                           AS client_seq,
                        %s::integer                                 AS updated_by_user_id
                    FROM (
                        SELECT 'checkboxes' AS section, key, value AS val
                          FROM jsonb_each(%s::jsonb -> 'checkboxes')
                          WHERE jsonb_typeof(%s::jsonb -> 'checkboxes') = 'object'
                        UNION ALL
                        SELECT 'numeric', key, value FROM jsonb_each(%s::jsonb -> 'numeric')
                          WHERE jsonb_typeof(%s::jsonb -> 'numeric') = 'object'
                        UNION ALL
                        SELECT 'sold', key, value FROM jsonb_each(%s::jsonb -> 'sold')
                          WHERE jsonb_typeof(%s::jsonb -> 'sold') = 'object'
                        UNION ALL
                        SELECT 'comp', key, value FROM jsonb_each(%s::jsonb -> 'comp')
                          WHERE jsonb_typeof(%s::jsonb -> 'comp') = 'object'
                        UNION ALL
                        SELECT 'propelio', key, value FROM jsonb_each(%s::jsonb -> 'propelio')
                          WHERE jsonb_typeof(%s::jsonb -> 'propelio') = 'object'
                    ) sections
                    ON CONFLICT (area_id, field_key) DO UPDATE
                    SET value              = EXCLUDED.value,
                        updated_by_user_id = EXCLUDED.updated_by_user_id,
                        updated_at         = now()
                    WHERE saved_area_filter_fields.client_seq < 1
                    """,
                    (
                        area_id,
                        int(user["id"]),
                        Json(request.filter_state), Json(request.filter_state),
                        Json(request.filter_state), Json(request.filter_state),
                        Json(request.filter_state), Json(request.filter_state),
                        Json(request.filter_state), Json(request.filter_state),
                        Json(request.filter_state), Json(request.filter_state),
                    ),
                )
                # Sprint 3 §3.3: single batch NOTIFY for the blob-explode
                # path. Frontend treats this as a refetch hint rather than
                # N individual field-change events.
                import json as _json_blob_notify
                cur.execute(
                    "SELECT pg_notify('saved_area_filter_changes', %s)",
                    (_json_blob_notify.dumps({
                        "area_id": area_id,
                        "type": "blob_explode",
                        "by_user_id": int(user["id"]),
                        "by_session_id": req.headers.get("x-session-id", ""),
                    }),),
                )

            # KK debug 2026-06-06: name + originator changes on this PUT
            # had no SSE broadcast — User B only saw them on tab refocus
            # (via the visibilitychange handler at frontend/map.js:198).
            # Now fire an area_meta_change event so other connected
            # members of the area refetch /api/areas and see the new name
            # / originator within a second instead of "eventually."
            # Filter_state changes already broadcast via blob_explode
            # above; this handles the remaining mutation surface on the
            # generic PUT.
            name_or_originator_changed = (
                request.name is not None
                or request.originator_parcel_county is not None
                or request.originator_parcel_account_num is not None
            )
            if name_or_originator_changed:
                import json as _json_meta_notify
                cur.execute(
                    "SELECT pg_notify('saved_area_filter_changes', %s)",
                    (_json_meta_notify.dumps({
                        "area_id": area_id,
                        "type": "area_meta_change",
                        "name_changed": request.name is not None,
                        "originator_changed": (
                            request.originator_parcel_county is not None
                            or request.originator_parcel_account_num is not None
                        ),
                        "by_user_id": int(user["id"]),
                        "by_session_id": req.headers.get("x-session-id", ""),
                    }),),
                )

            if request.polygon is not None:
                _bond_standalone_targets_to_area(
                    cur,
                    user_id=int(user["id"]),
                    area_id=area_id,
                    polygon_geojson=_to_geojson_polygon(request.polygon),
                )
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

    # Sprint 1: explicit owner pre-check so editors get 403, not silent 404
    # (Copilot S-3). Developers bypass per existing convention.
    if not is_developer:
        role = _user_area_role(area_id, int(user["id"]))
        if role is None:
            raise HTTPException(status_code=404, detail="Saved area not found")
        if role != "owner":
            raise HTTPException(status_code=403, detail="Only the owner can delete this workspace")

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
    uid = int(user["id"])

    def _query() -> list[dict[str, Any]]:
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
                    (uid,),
                )
                return [
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

    # Tier 2: run the blocking session-DB query off the event loop.
    parcels = await asyncio.to_thread(_query)
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
            # Sprint 1 multi-user collab (Copilot B-2 blocking catch): the
            # gate is membership, not ownership — editors must be able to
            # save parcels into shared areas. Was a silent skip pre-Sprint-1.
            # Skip silently when the caller isn't a member (don't leak
            # whether the area exists).
            bonded_row = None
            if target_area_id:
                area_role = _user_area_role(target_area_id, int(user["id"]))
                if area_role is not None:  # 'owner' OR 'editor' both qualify
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

                    # Mike report 2026-06-06: "stars don't show up when my
                    # assistants are adding areas with saved parcels until I
                    # view the area with a saved parcel." Phase 1 SSE only
                    # broadcast filter / stored_value mutations — saved
                    # parcel adds never reached other connected users. Fire
                    # a saved_parcel_change event so subscribers of this
                    # area_id refetch their saved-parcel list (and the gold
                    # star renders without requiring an area open).
                    import json as _json_sp_add
                    cur.execute(
                        "SELECT pg_notify('saved_area_filter_changes', %s)",
                        (_json_sp_add.dumps({
                            "area_id": target_area_id,
                            "type": "saved_parcel_change",
                            "action": "add",
                            "account_num": account_num,
                            "county": county,
                            "by_user_id": int(user["id"]),
                            "by_session_id": req.headers.get("x-session-id", ""),
                        }),),
                    )
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
    deleted_area_ids: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM saved_parcels
                WHERE county = %s AND account_num = %s AND user_id = %s
                RETURNING area_id
                """,
                (str(county or "dcad").strip().lower() or "dcad", str(account_num or "").strip(), int(user["id"])),
            )
            rows = cur.fetchall()
            deleted = len(rows)
            # Same Mike report 2026-06-06: collect every distinct area_id we
            # just removed a bonded copy from so the SSE broadcast below can
            # fan out to all affected areas in one transaction. RETURNING is
            # how we know which areas were touched — the DELETE WHERE clause
            # doesn't filter by area_id (it nukes all copies for this user).
            for row in rows:
                aid = row[0]
                if aid:
                    deleted_area_ids.append(str(aid))

            if deleted_area_ids:
                import json as _json_sp_del
                target_account = str(account_num or "").strip()
                target_county = str(county or "dcad").strip().lower() or "dcad"
                user_id_int = int(user["id"])
                session_id_hdr = req.headers.get("x-session-id", "")
                for affected_area_id in set(deleted_area_ids):
                    cur.execute(
                        "SELECT pg_notify('saved_area_filter_changes', %s)",
                        (_json_sp_del.dumps({
                            "area_id": affected_area_id,
                            "type": "saved_parcel_change",
                            "action": "delete",
                            "account_num": target_account,
                            "county": target_county,
                            "by_user_id": user_id_int,
                            "by_session_id": session_id_hdr,
                        }),),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    if not deleted:
        raise HTTPException(status_code=404, detail="Saved parcel not found")
    return {"ok": True, "deleted": int(deleted)}


@app.post("/api/parcels/rate")
async def rate_parcel(request: ParcelRateRequest, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Workspace-scoped Good/Bad/Clear rating for a CAD parcel. Parallel
    to /api/propelio/comp/rate but keyed by (county, account_num) since
    parcels exist independently of comps. Per PARCEL_RATINGS_SPEC.md v2.

    view routing (per-view ratings spec §3.1, FMEA C2): view='arv' (the
    default for absent/None — coexistence with old clients) → the EXISTING
    parcel_ratings table, byte-for-byte unchanged; view in ('nbv','export')
    → the additive parcel_ratings_views table, UPSERT keyed with view;
    clear = DELETE that view's row only. view='arv' is never written to
    the _views table (CHECK excludes it)."""
    require_csrf(req)
    area_id = str(request.saved_area_id or "").strip()
    county = str(request.county or "").strip().lower()
    account_num = str(request.account_num or "").strip()
    rating = request.rating
    if not area_id or not county or not account_num:
        raise HTTPException(status_code=400, detail="saved_area_id, county, account_num all required")
    if rating not in (None, "good", "bad"):
        raise HTTPException(status_code=400, detail="rating must be 'good', 'bad', or null")
    # View normalization (FMEA C2): absent/None → 'arv' (old clients coexist;
    # flag-ON frontend always sends an explicit view, so missing = old client).
    # Present-but-invalid → 400 (NEVER silently default an invalid present
    # view to ARV).
    if request.view is None:
        norm_view = "arv"
    else:
        norm_view = str(request.view).strip().lower()
        if norm_view not in ("arv", "nbv", "export"):
            raise HTTPException(status_code=400, detail="view must be 'arv', 'nbv', 'export', or null")
    # Sprint 1: editors can rate parcels on shared areas (spec §3.2).
    _assert_user_is_area_member(area_id, int(user["id"]))

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            if norm_view == "arv":
                # ARV path — EXISTING parcel_ratings table, byte-for-byte
                # unchanged. This is the load-bearing coexistence invariant.
                if rating is None:
                    cur.execute(
                        "DELETE FROM parcel_ratings WHERE workspace_id = %s AND county = %s AND account_num = %s",
                        (area_id, county, account_num),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO parcel_ratings (workspace_id, county, account_num, rating, rated_by_user_id)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (workspace_id, county, account_num) DO UPDATE
                        SET rating = EXCLUDED.rating,
                            rated_by_user_id = EXCLUDED.rated_by_user_id,
                            rated_at = NOW()
                        """,
                        (area_id, county, account_num, rating, int(user["id"])),
                    )
            else:
                # NBV/Export path — additive parcel_ratings_views table. view
                # CHECK excludes 'arv' (structurally impossible to shadow ARV).
                if rating is None:
                    cur.execute(
                        """
                        DELETE FROM parcel_ratings_views
                        WHERE workspace_id = %s AND county = %s AND account_num = %s AND view = %s
                        """,
                        (area_id, county, account_num, norm_view),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO parcel_ratings_views (workspace_id, county, account_num, view, rating, rated_by_user_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (workspace_id, county, account_num, view) DO UPDATE
                        SET rating = EXCLUDED.rating,
                            rated_by_user_id = EXCLUDED.rated_by_user_id,
                            rated_at = NOW()
                        """,
                        (area_id, county, account_num, norm_view, rating, int(user["id"])),
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
    return {"ok": True, "rating": rating, "view": norm_view}


# =============================================================================
# Mailer + Phone Tracking endpoints (spec v2, 2026-06-03)
# =============================================================================
#
# Three endpoints:
#   PUT  /api/parcels/outreach           — single-field edit from popup
#   POST /api/parcels/outreach/import    — bulk CSV upload from CRM round-trip
#   GET  /api/parcels/outreach/imports   — recent import history
#
# All gated to power_user / owner / developer via _user_can_see_outreach.
# Data lives on the DATA DB (parcel_outreach_notes table).


_OUTREACH_PARCEL_TABLES = {
    "dcad":   ("parcels",         "account_num"),
    "tad":    ("tad_parcels",     "parcel_key"),
    "collin": ("collin_parcels",  "parcel_key"),
    "denton": ("denton_parcels",  "parcel_key"),
}


def _validate_outreach_parcel_id(county: str, parcel_id: str) -> bool:
    """Return True if the (county, parcel_id) actually exists in the
    corresponding parcel table. Cheap existence check before UPSERT so
    we don't accumulate ghost rows in parcel_outreach_notes."""
    if county not in _OUTREACH_PARCEL_TABLES:
        return False
    table, pk = _OUTREACH_PARCEL_TABLES[county]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Identifier interpolation is safe — `table` and `pk` come from
            # the hardcoded _OUTREACH_PARCEL_TABLES dict, not user input.
            cur.execute(
                f"SELECT 1 FROM {table} WHERE {pk} = %s LIMIT 1",
                (parcel_id,),
            )
            return cur.fetchone() is not None
    finally:
        release_conn(conn)


def _parse_iso_date(text: str | None) -> "date_module.date | None":
    """Parse 'YYYY-MM-DD' into a date. Returns None on empty/invalid."""
    import datetime as date_module
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return date_module.date.fromisoformat(text)
    except (ValueError, TypeError):
        return None


@app.put("/api/parcels/outreach")
async def put_parcel_outreach(
    request: OutreachPutRequest,
    req: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Manual edit of outreach data from the popup. UPSERTs into
    parcel_outreach_notes. Partial update — only sets fields where the
    corresponding _set flag is true. Empty phone_number string is treated
    as null (clear)."""
    require_csrf(req)
    if not _user_can_see_outreach(user):
        raise HTTPException(status_code=403, detail="Outreach editing requires power_user role or higher")

    county = str(request.county).strip().lower()
    parcel_id = str(request.parcel_id or "").strip()
    if not parcel_id:
        raise HTTPException(status_code=400, detail="parcel_id required")

    if not _validate_outreach_parcel_id(county, parcel_id):
        raise HTTPException(status_code=404, detail=f"Parcel not found: {county}/{parcel_id}")

    # Normalize values. mailer_date must parse as ISO date if supplied.
    contact_info_retrieved_value: bool | None = None
    if request.contact_info_retrieved_set:
        contact_info_retrieved_value = bool(request.contact_info_retrieved) if request.contact_info_retrieved is not None else False

    mailer_date_value = None
    if request.mailer_date_set:
        if request.mailer_date is None:
            mailer_date_value = None
        else:
            mailer_date_value = _parse_iso_date(str(request.mailer_date))
            if request.mailer_date and mailer_date_value is None:
                raise HTTPException(status_code=400, detail="mailer_date must be ISO YYYY-MM-DD")

    if not (request.contact_info_retrieved_set or request.mailer_date_set):
        raise HTTPException(status_code=400, detail="Nothing to update — set at least one of contact_info_retrieved / mailer_date")

    user_id = int(user["id"])
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Read existing row (so partial-update preserves untouched fields).
            cur.execute(
                "SELECT contact_info_retrieved, mailer_date "
                "FROM parcel_outreach_notes WHERE county = %s AND parcel_id = %s",
                (county, parcel_id),
            )
            existing = cur.fetchone()
            existing_contact_retrieved, existing_mailer_date = (existing or (False, None))

            new_contact_retrieved = contact_info_retrieved_value if request.contact_info_retrieved_set else (existing_contact_retrieved or False)
            new_mailer_date = mailer_date_value if request.mailer_date_set else existing_mailer_date

            cur.execute(
                """
                INSERT INTO parcel_outreach_notes
                    (county, parcel_id, contact_info_retrieved, mailer_date,
                     last_updated_by_user_id, last_updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (county, parcel_id) DO UPDATE SET
                    contact_info_retrieved = EXCLUDED.contact_info_retrieved,
                    mailer_date            = EXCLUDED.mailer_date,
                    last_updated_by_user_id = EXCLUDED.last_updated_by_user_id,
                    last_updated_at = EXCLUDED.last_updated_at
                RETURNING contact_info_retrieved, mailer_date, last_updated_at
                """,
                (county, parcel_id, new_contact_retrieved, new_mailer_date, user_id),
            )
            row = cur.fetchone()
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)

    return {
        "ok": True,
        "county": county,
        "parcel_id": parcel_id,
        "outreach_contact_info_retrieved": bool(row[0]),
        "outreach_mailer_date": row[1].isoformat() if row[1] else None,
        "last_updated_at": row[2].isoformat() if row[2] else None,
    }


# CSV import — header aliases (case-insensitive) accepted for each logical field.
# v2 2026-06-03: dropped phone + mailer_sent aliases (model removed those
# fields); added contact_info_retrieved aliases.
_OUTREACH_IMPORT_KEY_ALIASES = ("parcel id", "parcel_id", "cad id", "cad_id", "parcel_key", "account_num", "account #", "account")
_OUTREACH_IMPORT_CONTACT_RETRIEVED_ALIASES = (
    "contact info retrieved", "contact_info_retrieved", "contact retrieved",
    "skip traced", "skip_traced", "contacted",
)
_OUTREACH_IMPORT_MAILER_DATE_ALIASES = (
    "last mailer sent date", "last_mailer_sent_date", "last mailer sent",
    "mailer date", "mailer_date", "date mailed", "date_mailed",
)
_OUTREACH_IMPORT_TAGS_ALIASES = ("tags", "tag", "labels")

# FUB-fallback constants. When the LotLedger→FUB import put the parcel_key
# into the Tags column (because the operator didn't create a `parcel_key`
# custom field in FUB before importing), the FUB export comes back with an
# empty `parcel_key` column and the actual ID inside Tags like:
#     "00239925:000, Import"
# Our parser handles that case by parsing Tags when parcel_key is empty.
_OUTREACH_FUB_TAG_NOISE_LOWER = {"import", "imported", "lead", "client"}


def _extract_parcel_id_from_fub_tags(tags_value: str) -> str:
    """When FUB stores parcel_key inside the Tags column, the export comes
    back like "00239925:000, Import". Pick the first non-noise tag that
    looks like a parcel_id (alphanumeric with optional colon/dash, contains
    at least one digit).

    Returns "" when nothing usable found.
    """
    import re as re_module
    if not tags_value:
        return ""
    PARCEL_ID_SHAPE = re_module.compile(r"^[\w:.\-]+$")
    for raw_tag in tags_value.split(","):
        tag = raw_tag.strip()
        if not tag:
            continue
        if tag.lower() in _OUTREACH_FUB_TAG_NOISE_LOWER:
            continue
        if not PARCEL_ID_SHAPE.match(tag):
            continue
        if not any(c.isdigit() for c in tag):
            continue
        return tag
    return ""

_OUTREACH_IMPORT_MAX_BYTES = 10 * 1024 * 1024     # 10 MB
_OUTREACH_IMPORT_MAX_ROWS = 50_000


def _normalize_csv_header(h: str) -> str:
    return (h or "").strip().lower().replace("﻿", "")


def _find_csv_column(headers: list[str], aliases: tuple[str, ...]) -> str | None:
    """Case-insensitive header lookup. Returns the original header name (so
    DictReader can find it) or None when no alias matches."""
    norm_to_original: dict[str, str] = {_normalize_csv_header(h): h for h in headers}
    for alias in aliases:
        if alias in norm_to_original:
            return norm_to_original[alias]
    return None


def _parse_outreach_yes_no_cell(raw: str) -> bool | None:
    """Parse a yes/no-style CSV cell. Accept 'yes'/'y'/'true'/'1' → True,
    'no'/'n'/'false'/'0' → False, EMPTY or unrecognized → None.

    Mike bug 2026-06-05 (post-LPAD-fix): Mike's CSV is a saved-area export
    where the same parcel appears twice (once as a parcel row with the Yes
    cell filled in, once as a comp row with the Yes cell empty). The
    dedup picked the later (empty) row and the empty cell was being
    parsed as False, silently overwriting Mike's actual entries. Empty
    cells must mean "no change," not "clear to False." If Mike ever wants
    to explicitly clear, he types "no"/"false"/"0".
    """
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text in ("yes", "y", "true", "1", "contacted", "retrieved", "done"):
        return True
    if text in ("no", "n", "false", "0", "uncontacted", "not contacted"):
        return False
    return None


def _parse_outreach_csv_date(text: str | None) -> "date_module.date | None":
    """Parse a date cell from a re-imported outreach CSV. More permissive than
    _parse_iso_date because Excel auto-formats ISO dates into US locale
    (M/D/YYYY) the moment a user opens the file — silently mangling the export.

    Accepts:
      - YYYY-MM-DD   (LotLedger's own export format)
      - YYYY/MM/DD   (defensive)
      - M/D/YYYY     (Excel US auto-format after opening + saving)
      - MM/DD/YYYY   (same with zero-padding)

    Returns None on empty / unparseable so the import loop can decide whether
    to treat it as "no change" vs. "explicit clear" via the *_set flags.
    """
    import datetime as date_module
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    # ISO first — fast path + matches our own export
    try:
        return date_module.date.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    # ISO with slashes
    if "/" in s and len(s) == 10 and s[4] == "/" and s[7] == "/":
        try:
            return date_module.date.fromisoformat(s.replace("/", "-"))
        except (ValueError, TypeError):
            pass
    # US M/D/YYYY or MM/DD/YYYY (Excel default for en-US locale)
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            try:
                month, day, year = (int(p) for p in parts)
                if 1 <= month <= 12 and 1 <= day <= 31 and 1000 <= year <= 9999:
                    return date_module.date(year, month, day)
            except (ValueError, TypeError):
                pass
    return None


@app.post("/api/parcels/outreach/import")
async def import_parcel_outreach(
    req: Request,
    file: UploadFile = File(...),
    mode: str = Query("commit", regex=r"^(preview|commit)$"),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Bulk CSV import for outreach data (Mailer + Phone Tracking, spec v2 §5.3).

    Staging-table + batched set-based UPSERT pattern (NOT per-row UPSERT).
    Per Copilot critique 2026-06-03: per-row would have created N transactions
    on shared Cloud SQL; staging approach is one INSERT batch + 4 county
    resolution UPDATEs + N/1000 chunked UPSERTs.

    Modes:
      preview — SELECT-only diff computation; nothing written to
                parcel_outreach_notes. Returns counts + sample unmatched ids.
      commit  — Same diff + UPSERT into parcel_outreach_notes.
    """
    require_csrf(req)
    if not _user_can_see_outreach(user):
        raise HTTPException(status_code=403, detail="Outreach import requires power_user role or higher")

    raw_bytes = await file.read()
    file_size = len(raw_bytes)
    if file_size > _OUTREACH_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"CSV too large (>10MB): {file_size} bytes")

    # Parse CSV.
    import csv as csv_module
    import io as io_module
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("latin-1")
        except UnicodeDecodeError as e:
            raise HTTPException(status_code=400, detail=f"CSV encoding not readable: {e}")

    reader = csv_module.DictReader(io_module.StringIO(text))
    headers = reader.fieldnames or []
    if not headers:
        raise HTTPException(status_code=400, detail="CSV has no header row")

    key_col = _find_csv_column(headers, _OUTREACH_IMPORT_KEY_ALIASES)
    contact_retrieved_col = _find_csv_column(headers, _OUTREACH_IMPORT_CONTACT_RETRIEVED_ALIASES)
    mailer_date_col = _find_csv_column(headers, _OUTREACH_IMPORT_MAILER_DATE_ALIASES)
    # FUB fallback: when the LotLedger→FUB import didn't have a parcel_key
    # custom field set up, FUB stuffs the Parcel ID into the Tags column.
    # The Tags column comes back as "00239925:000, Import" — we parse it
    # below per-row when the dedicated key column is missing or empty.
    tags_col = _find_csv_column(headers, _OUTREACH_IMPORT_TAGS_ALIASES)

    if key_col is None and tags_col is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Required column not found. Add a 'Parcel ID' column (or 'parcel_key' / 'account_num') "
                f"to your CSV, OR include a 'Tags' column containing the parcel IDs (FUB fallback). "
                f"Headers seen: {headers}"
            ),
        )

    # Read rows into memory (already capped at MAX_BYTES; row cap separately).
    parsed_rows: list[tuple[int, str, bool | None, "date_module.date | None"]] = []
    import datetime as date_module
    rows_skipped_no_id = 0
    for idx, raw_row in enumerate(reader):
        if idx >= _OUTREACH_IMPORT_MAX_ROWS:
            raise HTTPException(status_code=413, detail=f"CSV too many rows (>{_OUTREACH_IMPORT_MAX_ROWS})")
        # Primary path: dedicated parcel_id column.
        parcel_id = str(raw_row.get(key_col) or "").strip() if key_col else ""
        # FUB fallback: pull from Tags when the dedicated column was missing
        # or this specific row's cell is empty.
        tags_value = str(raw_row.get(tags_col) or "") if tags_col else ""
        if not parcel_id and tags_value:
            parcel_id = _extract_parcel_id_from_fub_tags(tags_value)
        if not parcel_id:
            rows_skipped_no_id += 1
            continue
        contact_retrieved = (
            _parse_outreach_yes_no_cell(raw_row.get(contact_retrieved_col) or "")
            if contact_retrieved_col else None
        )
        mailer_date = _parse_outreach_csv_date(raw_row.get(mailer_date_col) or "") if mailer_date_col else None
        parsed_rows.append((idx, parcel_id, contact_retrieved, mailer_date))

    if not parsed_rows:
        raise HTTPException(status_code=400, detail="CSV had no data rows after the header")

    user_id = int(user["id"])
    conn = get_conn()
    matched = 0
    updated = 0
    unmatched_ids: list[str] = []
    sample_unmatched: list[str] = []
    try:
        with conn.cursor() as cur:
            # Staging table — temp, dropped at txn end.
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SET LOCAL statement_timeout = '120s'")
            cur.execute(
                """
                CREATE TEMP TABLE outreach_staging (
                    row_idx INTEGER PRIMARY KEY,
                    parcel_id TEXT NOT NULL,
                    contact_info_retrieved BOOLEAN,
                    mailer_date DATE,
                    matched_county TEXT,
                    contact_info_retrieved_set BOOLEAN NOT NULL DEFAULT false,
                    mailer_date_set BOOLEAN NOT NULL DEFAULT false
                ) ON COMMIT DROP
                """
            )

            # Batched INSERT into staging via execute_values.
            # Per-row *_set flags (Mike bug 2026-06-05): set is True only when
            # the cell had explicit content. Empty cells = "no change" not
            # "clear to False". This matters because Mike's saved-area CSV
            # has the same parcel in two rows (parcel section with data +
            # comp section with empty cells); a column-level set flag plus
            # row_idx-DESC dedup picked the empty row and wiped his entries.
            from psycopg2.extras import execute_values
            execute_values(
                cur,
                """
                INSERT INTO outreach_staging
                    (row_idx, parcel_id, contact_info_retrieved, mailer_date,
                     contact_info_retrieved_set, mailer_date_set)
                VALUES %s
                """,
                [
                    (
                        idx, pid, cir, md,
                        cir is not None,
                        md is not None,
                    )
                    for idx, pid, cir, md in parsed_rows
                ],
                page_size=1000,
            )

            # Resolve county via set-based join — one UPDATE per county table.
            for county_key, (table, pk) in _OUTREACH_PARCEL_TABLES.items():
                # Defensive multi-county-match warning: count rows already matched
                # in a different county before this UPDATE. Should be 0 (verified
                # globally unique across 2.3M parcels).
                cur.execute(
                    f"""
                    UPDATE outreach_staging s
                    SET matched_county = %s
                    FROM {table} p
                    WHERE p.{pk} = s.parcel_id
                      AND s.matched_county IS NULL
                    """,
                    (county_key,),
                )

            # DCAD-only second pass: Excel strips leading zeros when a user
            # opens an exported CSV and re-saves it. DCAD's account_num is
            # canonically 17 digits, zero-padded ('00000424978000000'); after
            # Excel mangling, Mike's CSV cell looks like '424978000000'. Pad
            # numeric-only ids back to 17 and retry. Mike bug 2026-06-05:
            # entire re-import wrote nothing because the row went silently to
            # the unmatched bucket. Only DCAD needs this: TAD parcel_keys
            # contain colons, Collin contains letters, Denton isn't zero-
            # padded — none of those get mangled by Excel.
            cur.execute(
                """
                UPDATE outreach_staging s
                SET matched_county = 'dcad'
                FROM parcels p
                WHERE p.account_num = LPAD(s.parcel_id, 17, '0')
                  AND s.matched_county IS NULL
                  AND s.parcel_id ~ '^[0-9]+$'
                """
            )

            cur.execute(
                """
                SELECT
                    COUNT(*),
                    COUNT(*) FILTER (WHERE matched_county IS NOT NULL),
                    COUNT(*) FILTER (WHERE matched_county IS NULL)
                FROM outreach_staging
                """
            )
            row_total, row_matched, row_unmatched = cur.fetchone()
            matched = int(row_matched or 0)

            if row_unmatched:
                cur.execute(
                    "SELECT parcel_id FROM outreach_staging WHERE matched_county IS NULL ORDER BY row_idx LIMIT 10"
                )
                sample_unmatched = [r[0] for r in cur.fetchall()]

            # Collected committed rows per chunk (commit mode only). Frontend
            # uses these to update lastAnalysisGeojson in place so popups
            # reflect the re-imported data without a full page refetch.
            # KK bug 2026-06-05: typing a date into the CSV and re-importing
            # didn't surface in the popup because the in-memory feature
            # properties stayed stale; only a fresh /api/analyze would
            # pick up DB changes.
            committed_rows: list[dict[str, Any]] = []
            if mode == "commit":
                # Chunked UPSERT — 1000-row chunks via WHERE row_idx ranges.
                # DISTINCT ON dedupes within the chunk's selected rows so we
                # never present the same (county, parcel_id) twice to ON
                # CONFLICT (which would raise CardinalityViolation). Last
                # row in the CSV wins (ORDER BY row_idx DESC). Cross-chunk
                # duplicates are handled naturally — second chunk's UPSERT
                # just updates whatever the first chunk inserted.
                # KK FUB round-trip 2026-06-03: this fires when Mike's
                # saved area has comp rows + parcel rows pointing at the
                # same underlying CAD parcel (intentional in CSV output;
                # duplicates here).
                chunk_size = 1000
                row_idx = 0
                while True:
                    cur.execute(
                        """
                        WITH deduped AS (
                            -- Pick the most data-bearing row per parcel. Mike's
                            -- saved-area CSV exports each parcel twice — once
                            -- in the parcel section with outreach cells filled
                            -- in, once in the comp section with empty cells.
                            -- Previous "row_idx DESC" dedup picked the empty
                            -- (later) row and silently wiped Mike's entries.
                            SELECT DISTINCT ON (s.matched_county, s.parcel_id)
                                s.matched_county,
                                s.parcel_id,
                                s.contact_info_retrieved,
                                s.contact_info_retrieved_set,
                                s.mailer_date,
                                s.mailer_date_set
                            FROM outreach_staging s
                            WHERE s.matched_county IS NOT NULL
                              AND s.row_idx >= %s AND s.row_idx < %s
                              -- Skip no-op rows (neither flag set) entirely so
                              -- a CSV row with all-empty outreach cells doesn't
                              -- create or touch a parcel_outreach_notes row.
                              AND (s.contact_info_retrieved_set OR s.mailer_date_set)
                            ORDER BY s.matched_county, s.parcel_id,
                                     (s.contact_info_retrieved_set::int + s.mailer_date_set::int) DESC,
                                     s.row_idx DESC
                        ),
                        existing AS (
                            -- Pull current values for parcels that already have
                            -- a row, so the SELECT below can preserve untouched
                            -- fields without needing CASE/EXCLUDED gymnastics in
                            -- the ON CONFLICT clause.
                            SELECT pon.county, pon.parcel_id,
                                   pon.contact_info_retrieved AS old_cir,
                                   pon.mailer_date AS old_md
                            FROM parcel_outreach_notes pon
                            JOIN deduped d ON d.matched_county = pon.county
                                          AND d.parcel_id      = pon.parcel_id
                        ),
                        upserted AS (
                            INSERT INTO parcel_outreach_notes
                                (county, parcel_id, contact_info_retrieved, mailer_date,
                                 last_updated_by_user_id, last_updated_at)
                            SELECT
                                d.matched_county,
                                d.parcel_id,
                                CASE WHEN d.contact_info_retrieved_set
                                     THEN COALESCE(d.contact_info_retrieved, false)
                                     ELSE COALESCE(e.old_cir, false) END,
                                CASE WHEN d.mailer_date_set
                                     THEN d.mailer_date
                                     ELSE e.old_md END,
                                %s,
                                now()
                            FROM deduped d
                            LEFT JOIN existing e ON e.county = d.matched_county
                                                AND e.parcel_id = d.parcel_id
                            ON CONFLICT (county, parcel_id) DO UPDATE SET
                                contact_info_retrieved = EXCLUDED.contact_info_retrieved,
                                mailer_date            = EXCLUDED.mailer_date,
                                last_updated_by_user_id = EXCLUDED.last_updated_by_user_id,
                                last_updated_at = EXCLUDED.last_updated_at
                            RETURNING county, parcel_id, contact_info_retrieved, mailer_date
                        )
                        SELECT county, parcel_id, contact_info_retrieved, mailer_date FROM upserted
                        """,
                        (row_idx, row_idx + chunk_size, user_id),
                    )
                    chunk_rows = cur.fetchall()
                    chunk_count = len(chunk_rows)
                    for (cnty, pid, cir, md) in chunk_rows:
                        committed_rows.append({
                            "county": cnty,
                            "parcel_id": pid,
                            "outreach_contact_info_retrieved": bool(cir),
                            "outreach_mailer_date": (
                                md.isoformat() if hasattr(md, "isoformat") and md else None
                            ),
                        })
                    updated += chunk_count
                    row_idx += chunk_size
                    if row_idx >= row_total:
                        break

            # Audit row.
            cur.execute(
                """
                INSERT INTO outreach_import_log
                    (user_id, file_name, file_size_bytes, mode,
                     rows_total, rows_matched, rows_unmatched, rows_updated)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING import_id
                """,
                (
                    user_id,
                    file.filename or "(unnamed)",
                    file_size,
                    mode,
                    int(row_total),
                    int(row_matched),
                    int(row_unmatched),
                    updated,
                ),
            )
            import_id = cur.fetchone()[0]
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)

    return {
        "import_id": str(import_id),
        "mode": mode,
        "total": int(row_total),
        "matched": int(row_matched),
        "unmatched": int(row_unmatched),
        "updated": updated,
        "sample_unmatched_ids": sample_unmatched,
        # Commit mode includes the actual upserted rows so the frontend
        # can refresh in-memory feature props without a full refetch.
        # KK bug 2026-06-05 — popup state stale after re-import.
        "committed_rows": committed_rows,
    }


@app.get("/api/parcels/outreach/imports")
async def list_parcel_outreach_imports(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return recent CSV imports (last 20) for diagnostics."""
    if not _user_can_see_outreach(user):
        raise HTTPException(status_code=403, detail="Outreach data not available for this role")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT import_id, user_id, started_at, file_name, file_size_bytes,
                       mode, rows_total, rows_matched, rows_unmatched, rows_updated, notes
                FROM outreach_import_log
                ORDER BY started_at DESC
                LIMIT 20
                """
            )
            rows = cur.fetchall()
    finally:
        release_conn(conn)

    return {
        "imports": [
            {
                "import_id": str(r[0]),
                "user_id": int(r[1]) if r[1] is not None else None,
                "started_at": r[2].isoformat() if r[2] else None,
                "file_name": r[3],
                "file_size_bytes": int(r[4]) if r[4] is not None else None,
                "mode": r[5],
                "rows_total": int(r[6] or 0),
                "rows_matched": int(r[7] or 0),
                "rows_unmatched": int(r[8] or 0),
                "rows_updated": int(r[9] or 0),
                "notes": r[10],
            }
            for r in rows
        ]
    }


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    global _INDEX_HTML_CACHE
    if _INDEX_HTML_CACHE is None:
        html = (FRONTEND_DIR / "index.html").read_text()
        html = html.replace("__TILES_URL__", TILES_BASE_URL)
        html = html.replace("__BUILD_ID__", BUILD_ID)
        html = html.replace("__APP_VERSION__", APP_VERSION)
        _INDEX_HTML_CACHE = html
    return HTMLResponse(_INDEX_HTML_CACHE)


# AI module seam (the ONLY backend touch point). The env var is read directly
# here — NOT via `from api.ai.config import AI_ENABLED` — so that when the flag
# is off, api/ai is never imported at all. That keeps the guarantee absolute:
# a broken, half-deployed, or deleted api/ai/ package cannot stop the app from
# starting. Must stay ABOVE the StaticFiles catch-all mount below, or every
# /api/ai/* route is shadowed and 404s.
if os.getenv("AI_ENABLED", "false").strip().lower() == "true":
    from api.ai.routes import router as ai_router

    app.include_router(ai_router)

app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")