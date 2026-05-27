# api/config.py
#
# Database connection setup using psycopg2 (Cloud SQL / PostgreSQL).
# Single source of truth for all credentials and runtime settings.
# Raises clearly at startup if anything is missing — never fails silently.
#
# Two connection modes (auto-detected via environment):
#   Cloud Run  — connects via Unix socket injected by Cloud SQL Auth Proxy
#                (INSTANCE_UNIX_SOCKET env var set automatically by Cloud Run)
#   Local dev  — connects via TCP to Cloud SQL public IP (DB_HOST env var)
#
# Connects to:
#   api/main.py           — imports get_settings() and get_conn()
#   api/counties/dcad.py  — imports get_conn() for all DCAD queries
#   api/counties/tad.py   — imports get_conn() for all TAD queries
#   scripts/build_db.py   — imports get_conn() to write DCAD data

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import psycopg2
import psycopg2.pool
from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    session_database_url: str
    port: int


@lru_cache
def get_settings() -> Settings:
    db_host = os.getenv("DB_HOST", "").strip()
    db_name = os.getenv("DB_NAME", "").strip()
    db_user = os.getenv("DB_USER", "").strip()
    db_password = os.getenv("DB_PASSWORD", "").strip()
    db_port_raw = os.getenv("DB_PORT", "5432").strip() or "5432"
    port_raw = os.getenv("PORT", "8000").strip() or "8000"
    session_database_url = os.getenv("SESSION_DATABASE_URL", "").strip()

    unix_socket = os.environ.get("INSTANCE_UNIX_SOCKET", "").strip()
    if unix_socket:
        # Cloud Run unix socket: DB_PASSWORD is optional (Cloud SQL Auth Proxy handles auth)
        required = [("DB_NAME", db_name), ("DB_USER", db_user)]
    else:
        required = [("DB_HOST", db_host), ("DB_NAME", db_name), ("DB_USER", db_user), ("DB_PASSWORD", db_password)]
    missing = [k for k, v in required if not v]

    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    if not session_database_url:
        # Default session DB target uses same host/user/pass on the same instance.
        # Keeping the database separate makes client data portable via pg_dump.
        session_db_name = os.getenv("SESSION_DB_NAME", "lotledger_sessions").strip() or "lotledger_sessions"
        encoded_password = quote(db_password, safe="")
        if unix_socket:
            session_database_url = (
                f"postgresql://{db_user}:{encoded_password}@/{session_db_name}"
                f"?host={unix_socket}&connect_timeout=10"
            )
        else:
            session_database_url = (
                f"postgresql://{db_user}:{encoded_password}@{db_host}:{db_port_raw}/{session_db_name}"
                f"?connect_timeout=10"
            )

    return Settings(
        db_host=db_host,
        db_port=int(db_port_raw),
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
        session_database_url=session_database_url,
        port=int(port_raw),
    )


_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_session_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        s = get_settings()
        unix_socket = os.environ.get("INSTANCE_UNIX_SOCKET", "").strip()
        if unix_socket:
            # Cloud Run: use Unix socket injected by embedded Cloud SQL Auth Proxy.
            # psycopg2 treats a path starting with "/" as a Unix socket host.
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=20,
                host=unix_socket,
                dbname=s.db_name,
                user=s.db_user,
                password=s.db_password,
                connect_timeout=10,
            )
        else:
            # Local dev: connect via TCP to Cloud SQL public IP.
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=20,
                host=s.db_host,
                port=s.db_port,
                dbname=s.db_name,
                user=s.db_user,
                password=s.db_password,
                connect_timeout=10,
            )
    return _pool


def get_conn() -> psycopg2.extensions.connection:
    return get_pool().getconn()


def release_conn(conn: psycopg2.extensions.connection) -> None:
    try:
        get_pool().putconn(conn)
    except psycopg2.pool.PoolError:
        # Defensive fallback for rare pool state races under threaded access.
        # Prefer dropping the connection over crashing request handling.
        try:
            conn.close()
        except Exception:
            pass


def _session_dsn_from_settings(settings: Settings) -> str:
    parsed = urlparse(settings.session_database_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("connect_timeout", "10")
    return urlunparse(parsed._replace(query=urlencode(query)))


def _create_session_database_if_missing(settings: Settings) -> None:
    parsed = urlparse(settings.session_database_url)
    session_db_name = parsed.path.lstrip("/")
    if not session_db_name:
        return

    unix_socket = os.environ.get("INSTANCE_UNIX_SOCKET", "").strip()
    conn = None
    try:
        if unix_socket:
            conn = psycopg2.connect(
                host=unix_socket,
                dbname="postgres",
                user=settings.db_user,
                password=settings.db_password,
                connect_timeout=10,
            )
        else:
            conn = psycopg2.connect(
                host=settings.db_host,
                port=settings.db_port,
                dbname="postgres",
                user=settings.db_user,
                password=settings.db_password,
                connect_timeout=10,
            )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (session_db_name,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{session_db_name}"')
    finally:
        if conn is not None:
            conn.close()


def get_session_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _session_pool
    if _session_pool is None:
        s = get_settings()
        dsn = _session_dsn_from_settings(s)
        try:
            _session_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=5,
                dsn=dsn,
            )
        except psycopg2.OperationalError as exc:
            if 'does not exist' not in str(exc):
                raise
            _create_session_database_if_missing(s)
            _session_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=5,
                dsn=dsn,
            )
    return _session_pool


def get_session_conn() -> psycopg2.extensions.connection:
    return get_session_pool().getconn()


def release_session_conn(conn: psycopg2.extensions.connection) -> None:
    try:
        get_session_pool().putconn(conn)
    except psycopg2.pool.PoolError:
        try:
            conn.close()
        except Exception:
            pass


# Locked at 2026 per docs/DCAD_OWNER_FIELDS_FIX_SPEC.md v4a.2 §2.6.
# Used by every multi_owner / appraisal_yr year-scoped query and the
# multi_owner sync script's DELETE NOT EXISTS gate. Do NOT switch to
# (SELECT MAX(appraisal_yr) FROM multi_owner) — dual-policy invites
# endpoint/job drift if a partial future-year load lands before all
# sources are migrated.
CURRENT_APPRAISAL_YEAR = 2026
