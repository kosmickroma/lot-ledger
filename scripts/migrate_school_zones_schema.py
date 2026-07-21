#!/usr/bin/env python3
# scripts/migrate_school_zones_schema.py
#
# Idempotent, additive schema migration for the DB-backed school-zones
# feature: two new tables in the `lotledger` data DB, school_attendance_zones
# + school_campus_ratings. No data load -- scripts/ingest_school_zones.py
# does that, per-district, after this has been run at least once.
#
# docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §1, §2.
#
# Modeled on scripts/ingest_flood_zones.py's _ensure_schema_txn /
# _ensure_indexes_concurrent (SET LOCAL lock_timeout/statement_timeout,
# CREATE TABLE IF NOT EXISTS, CREATE INDEX CONCURRENTLY) -- but flood's
# index step has no INVALID-index detect/drop/retry (verified: flood's
# _ensure_indexes_concurrent has no pg_index/indisvalid query anywhere).
# A CREATE INDEX CONCURRENTLY that fails partway leaves an INVALID index
# behind; a later `IF NOT EXISTS` run then treats the broken index as
# already-built and never rebuilds it. This script checks pg_index.indisvalid
# and drops-then-retries before each CONCURRENTLY build, so a rerun after a
# failure actually repairs itself (§9.1's "twice" idempotence rehearsal).
#
# Run: .venv/bin/python3 scripts/migrate_school_zones_schema.py
# (also called at the top of scripts/ingest_school_zones.py's main(), same
# as flood's ingest script re-runs its own schema-ensure every invocation --
# CREATE TABLE/INDEX ... IF NOT EXISTS is safe to call every time.)
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn  # noqa: E402

_INDEXES = [
    (
        "idx_school_attendance_zones_geom",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_school_attendance_zones_geom "
        "ON school_attendance_zones USING GIST (geom)",
    ),
    (
        "idx_school_attendance_zones_district_level",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_school_attendance_zones_district_level "
        "ON school_attendance_zones (district_tea_id, level)",
    ),
]


def ensure_schema_txn(conn) -> None:
    """Idempotent transactional table creation -- brief catalog lock only,
    never touches any existing rows (CREATE TABLE IF NOT EXISTS)."""
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '60s'")
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS school_attendance_zones (
                id               SERIAL PRIMARY KEY,
                level            TEXT NOT NULL CHECK (level IN ('elementary', 'middle', 'high')),
                district_tea_id  TEXT NOT NULL,
                district_name    TEXT,
                campus_tea_id    TEXT,
                campus_name      TEXT NOT NULL,
                geom             GEOMETRY(MultiPolygon, 4326) NOT NULL,
                boundary_vintage TEXT,
                source_url       TEXT,
                source_kind      TEXT,
                retrieved_at     DATE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS school_campus_ratings (
                campus_tea_id TEXT NOT NULL,
                rating_year   INT NOT NULL,
                letter        TEXT,
                score         INT,
                achievement   JSONB,
                growth        JSONB,
                PRIMARY KEY (campus_tea_id, rating_year)
            )
            """
        )
    conn.commit()


def _index_is_invalid(conn, index_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = %s",
            (index_name,),
        )
        row = cur.fetchone()
    return row is not None and row[0] is False


def ensure_indexes_concurrent(conn) -> None:
    """CREATE INDEX CONCURRENTLY per index, with INVALID-index detect/
    drop/retry: an index name that exists but is invalid would otherwise
    make `IF NOT EXISTS` silently skip rebuilding it forever."""
    conn.autocommit = True
    try:
        for name, ddl in _INDEXES:
            if _index_is_invalid(conn, name):
                print(f"[school-zones-migrate] {name} is INVALID -- dropping before rebuild")
                with conn.cursor() as cur:
                    cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            with conn.cursor() as cur:
                cur.execute(ddl)
            if _index_is_invalid(conn, name):
                # One retry only -- a second consecutive failure is a real
                # problem (disk, lock contention), not transient.
                print(f"[school-zones-migrate] {name} still INVALID after rebuild -- retrying once")
                with conn.cursor() as cur:
                    cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                    cur.execute(ddl)
                if _index_is_invalid(conn, name):
                    raise RuntimeError(f"{name} is INVALID after retry -- aborting")
    finally:
        conn.autocommit = False


def ensure_schema_and_indexes(conn) -> None:
    """Order: create empty tables -> create indexes -> (caller loads data).
    Idempotent -- safe to call on every ingest run, same as flood's ingest
    script re-running its own schema-ensure every invocation."""
    ensure_schema_txn(conn)
    ensure_indexes_concurrent(conn)


def main() -> int:
    conn = get_conn()
    try:
        print("[school-zones-migrate] running schema migration ...")
        ensure_schema_and_indexes(conn)
        print("[school-zones-migrate] schema migration complete")
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"[school-zones-migrate] FAILED: {exc}", file=sys.stderr)
        raise
    finally:
        release_conn(conn)


if __name__ == "__main__":
    sys.exit(main())
