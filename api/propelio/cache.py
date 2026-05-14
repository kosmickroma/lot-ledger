# api/propelio/cache.py
#
# Propelio cache and quota log helpers backed by Postgres.
# Provides 7-day address-level response caching and monthly quota-balance
# history tracking for Propelio API calls.
#
# Connects to:
#   api/config.py            - get_conn(), release_conn() helpers
#   api/propelio/routes.py   - cache reads/writes and quota logging

from __future__ import annotations

import re
from typing import Any

from psycopg2.extras import Json

from api.config import get_conn, release_conn


CACHE_TTL_DAYS = 7
_TRAILING_PUNCT_RE = re.compile(r"[\s\.,;:!?]+$")


def normalize_address_key(raw: str) -> str:
    """Normalize user-entered addresses for stable cache keying."""
    text = " ".join(str(raw or "").strip().lower().split())
    if not text:
        return ""
    return _TRAILING_PUNCT_RE.sub("", text)


def get_cached(address_key: str) -> dict[str, Any] | None:
    """Return cached payload if still fresh, else None."""
    key = normalize_address_key(address_key)
    if not key:
        return None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload
                FROM propelio_cache
                WHERE address_key = %s
                                    AND fetched_at >= now() - make_interval(days => %s)
                LIMIT 1
                                """,
                (key, CACHE_TTL_DAYS),
            )
            row = cur.fetchone()
            if row is None:
                return None
            payload = row[0]
            return payload if isinstance(payload, dict) else None
    finally:
        release_conn(conn)


def put_cached(address_key: str, payload: dict[str, Any]) -> None:
    """Upsert cached payload for a normalized address key."""
    key = normalize_address_key(address_key)
    if not key:
        return

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO propelio_cache (address_key, payload, fetched_at)
                VALUES (%s, %s, now())
                ON CONFLICT (address_key)
                DO UPDATE SET
                    payload = EXCLUDED.payload,
                    fetched_at = EXCLUDED.fetched_at
                """,
                (key, Json(payload)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


def log_quota(balance: int | None, address_key: str) -> None:
    """Insert one quota-balance sample row."""
    key = normalize_address_key(address_key)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO propelio_quota_log (balance, address_key)
                VALUES (%s, %s)
                """,
                (balance, key or None),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


def latest_quota_balance() -> int | None:
    """Return most recent non-null quota balance in the last 30 days."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT balance
                FROM propelio_quota_log
                WHERE fetched_at >= now() - interval '30 days'
                  AND balance IS NOT NULL
                ORDER BY fetched_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None or row[0] is None:
                return None
            return int(row[0])
    finally:
        release_conn(conn)


def ensure_tables() -> None:
    """Create Propelio cache/quota tables if they do not exist."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS propelio_cache (
                    address_key TEXT PRIMARY KEY,
                    payload     JSONB NOT NULL,
                    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS propelio_quota_log (
                    id          SERIAL PRIMARY KEY,
                    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    balance     INTEGER,
                    address_key TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS propelio_quota_log_fetched_idx
                    ON propelio_quota_log (fetched_at DESC)
                """
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


ensure_tables()
