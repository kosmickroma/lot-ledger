#!/usr/bin/env python3
# scripts/setup_outreach_schema.py
#
# Idempotent schema migration for the Mailer + Phone Tracking feature.
# Creates parcel_outreach_notes (per-parcel outreach notes, global scope),
# outreach_import_log (CSV import audit ledger), and csv_export_log (PII
# access audit). Also ensures pgcrypto is installed on the data DB —
# verified 2026-06-03 that only postgis is present by default, so
# gen_random_uuid() requires explicit extension creation.
#
# Lives in scripts/ (not api/main.py:_ensure_session_schema) because
# parcel-adjacent tables go on the DATA DB, not the sessions DB. Pattern
# matches scripts/ingest_zcta_polygons.py shipped 2026-06-02.
#
# Run once before deploying the feature code:
#   .venv/bin/python3 scripts/setup_outreach_schema.py

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn  # noqa: E402


def _ensure_outreach_schema_txn(conn) -> None:
    """Idempotent transactional schema migration on the data DB."""
    with conn.cursor() as cur:
        # Bound any lock waits + statement runtime — safe defaults on shared Cloud SQL.
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '60s'")

        # pgcrypto is required for gen_random_uuid(). Data DB does NOT install
        # it by default (only postgis is present). Without this, the UUID
        # column DEFAULT clauses below fail with "function gen_random_uuid()
        # does not exist".
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

        # Per-parcel outreach data. Global scope (not per-user, not per-
        # saved-area). Same row visible to every power_user+ in the org.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS parcel_outreach_notes (
                county        TEXT NOT NULL CHECK (county IN ('dcad', 'tad', 'collin', 'denton')),
                parcel_id     TEXT NOT NULL,
                phone_number  TEXT,
                mailer_sent   BOOLEAN NOT NULL DEFAULT false,
                mailer_date   DATE,
                last_updated_by_user_id INTEGER,
                last_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (county, parcel_id)
            )
            """
        )

        # CSV import audit ledger. Summary-only per spec v2 §13 (rollback is
        # "corrective re-import" not deterministic undo).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS outreach_import_log (
                import_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id       INTEGER NOT NULL,
                started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                file_name     TEXT,
                file_size_bytes INTEGER,
                mode          TEXT NOT NULL CHECK (mode IN ('preview', 'commit')),
                rows_total    INTEGER NOT NULL DEFAULT 0,
                rows_matched  INTEGER NOT NULL DEFAULT 0,
                rows_unmatched INTEGER NOT NULL DEFAULT 0,
                rows_updated  INTEGER NOT NULL DEFAULT 0,
                notes         TEXT
            )
            """
        )

        # PII access audit. Lightweight insurance — records every CSV export
        # that includes outreach data (phone numbers).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS csv_export_log (
                export_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id            INTEGER NOT NULL,
                exported_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                row_count          INTEGER NOT NULL DEFAULT 0,
                included_outreach  BOOLEAN NOT NULL DEFAULT false,
                job_id             TEXT
            )
            """
        )
    conn.commit()


def _ensure_indexes_concurrent(conn) -> None:
    """Build supporting indexes CONCURRENTLY — non-blocking on shared Cloud SQL.

    CREATE INDEX CONCURRENTLY cannot run inside a transaction block, so flip
    the connection to autocommit for these statements only.
    """
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # Partial: only index rows where the predicate is true. Smaller
            # index, faster scans for queries that filter on phone presence
            # (e.g. "show me parcels with skip-traced phones in DCAD").
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_outreach_county_phone_nonempty "
                "ON parcel_outreach_notes (county) "
                "WHERE phone_number IS NOT NULL AND phone_number <> ''"
            )
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_outreach_mailer_sent "
                "ON parcel_outreach_notes (county) WHERE mailer_sent"
            )
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_outreach_import_log_user "
                "ON outreach_import_log (user_id, started_at DESC)"
            )
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_csv_export_log_user "
                "ON csv_export_log (user_id, exported_at DESC)"
            )
    finally:
        conn.autocommit = False


def _verify(conn) -> None:
    """Sanity-check the schema landed as expected."""
    expected_tables = {"parcel_outreach_notes", "outreach_import_log", "csv_export_log"}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename = ANY(%s)",
            (list(expected_tables),),
        )
        found = {r[0] for r in cur.fetchall()}
    missing = expected_tables - found
    if missing:
        raise RuntimeError(f"[setup_outreach] expected tables missing: {sorted(missing)}")
    print(f"[setup_outreach] tables present: {sorted(found)}")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
            "AND indexname IN ('idx_outreach_county_phone_nonempty', 'idx_outreach_mailer_sent', "
            "'idx_outreach_import_log_user', 'idx_csv_export_log_user')"
        )
        idx_found = {r[0] for r in cur.fetchall()}
    print(f"[setup_outreach] indexes present: {sorted(idx_found)}")


def main() -> int:
    conn = get_conn()
    try:
        print("[setup_outreach] running schema migrations ...")
        _ensure_outreach_schema_txn(conn)
        _ensure_indexes_concurrent(conn)
        print("[setup_outreach] verifying schema ...")
        _verify(conn)
        print("[setup_outreach] DONE.")
        return 0
    finally:
        release_conn(conn)


if __name__ == "__main__":
    sys.exit(main())
