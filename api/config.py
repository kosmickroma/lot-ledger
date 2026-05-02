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
    port: int


@lru_cache
def get_settings() -> Settings:
    db_host = os.getenv("DB_HOST", "").strip()
    db_name = os.getenv("DB_NAME", "").strip()
    db_user = os.getenv("DB_USER", "").strip()
    db_password = os.getenv("DB_PASSWORD", "").strip()
    db_port_raw = os.getenv("DB_PORT", "5432").strip() or "5432"
    port_raw = os.getenv("PORT", "8000").strip() or "8000"

    missing = [k for k, v in [
        ("DB_HOST", db_host), ("DB_NAME", db_name),
        ("DB_USER", db_user), ("DB_PASSWORD", db_password),
    ] if not v]

    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return Settings(
        db_host=db_host,
        db_port=int(db_port_raw),
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
        port=int(port_raw),
    )


_pool: psycopg2.pool.ThreadedConnectionPool | None = None


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
