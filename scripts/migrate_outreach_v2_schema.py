#!/usr/bin/env python3
# scripts/migrate_outreach_v2_schema.py
#
# Mailer + Phone Tracking v2 (2026-06-03): switch data model from
# (phone_number, mailer_sent boolean, mailer_date) to
# (contact_info_retrieved boolean, mailer_date).
#
# Mike's call after preview smoke: LotLedger doesn't store phone numbers —
# those live in his CRM (FUB). LotLedger just tracks "have I done the
# outreach prep for this parcel?" (contact_info_retrieved) and "when did
# I last mail them?" (mailer_date, relabeled "Last Mailer Sent Date" in
# UI/CSV but DB column name stays for simpler migration).
#
# Migration is idempotent and additive-safe-first:
#   1. Add contact_info_retrieved column (default false)
#   2. Drop phone_number column
#   3. Drop mailer_sent column (presence of mailer_date now = "was sent")
#   4. Drop the partial index on phone_number (no longer relevant)
#
# Test data on dev DB gets blown away when the columns drop (acceptable
# per KK 2026-06-03 — small set of test entries from preview smoke).
#
# Run before deploying code that uses the new shape:
#   .venv/bin/python3 scripts/migrate_outreach_v2_schema.py

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn  # noqa: E402


def _ensure_v2_columns(conn) -> None:
    """Add contact_info_retrieved + drop phone_number + drop mailer_sent."""
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '60s'")

        # 1. Add new column (additive — safe to run first; old code still works).
        cur.execute(
            "ALTER TABLE parcel_outreach_notes "
            "ADD COLUMN IF NOT EXISTS contact_info_retrieved BOOLEAN NOT NULL DEFAULT false"
        )

        # 2. Drop the phone_number partial index BEFORE dropping the column.
        cur.execute("DROP INDEX IF EXISTS idx_outreach_county_phone_nonempty")

        # 3. Drop phone_number column (Mike's data lives in FUB).
        cur.execute("ALTER TABLE parcel_outreach_notes DROP COLUMN IF EXISTS phone_number")

        # 4. Drop the mailer_sent boolean (mailer_date IS NOT NULL replaces
        #    its semantic — "was the mailer sent?" → "is there a date?").
        # The partial index on mailer_sent stays (it filters on the column);
        # drop it before the column.
        cur.execute("DROP INDEX IF EXISTS idx_outreach_mailer_sent")
        cur.execute("ALTER TABLE parcel_outreach_notes DROP COLUMN IF EXISTS mailer_sent")
    conn.commit()


def _ensure_v2_indexes_concurrent(conn) -> None:
    """Rebuild supporting indexes for the new column.

    CREATE INDEX CONCURRENTLY cannot run inside a transaction block, so flip
    the connection to autocommit for these statements only.
    """
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # Partial index: only rows where contact_info_retrieved=true.
            # Useful for queries like "show me all parcels where I've already
            # done skip-trace."
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_outreach_county_contact_retrieved "
                "ON parcel_outreach_notes (county) WHERE contact_info_retrieved"
            )
            # Partial index: rows that have a mailer_date — same semantic the
            # old mailer_sent index had, now keyed on the date column.
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_outreach_mailer_date_set "
                "ON parcel_outreach_notes (county) WHERE mailer_date IS NOT NULL"
            )
    finally:
        conn.autocommit = False


def _verify(conn) -> None:
    """Confirm schema is in the expected v2 shape."""
    expected_cols = {"county", "parcel_id", "contact_info_retrieved", "mailer_date",
                     "last_updated_by_user_id", "last_updated_at", "created_at"}
    dropped_cols = {"phone_number", "mailer_sent"}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'parcel_outreach_notes'"
        )
        found = {r[0] for r in cur.fetchall()}

    missing = expected_cols - found
    if missing:
        raise RuntimeError(f"[migrate_outreach_v2] expected columns missing: {sorted(missing)}")

    still_present = found & dropped_cols
    if still_present:
        raise RuntimeError(f"[migrate_outreach_v2] columns that should be dropped still exist: {sorted(still_present)}")

    print(f"[migrate_outreach_v2] schema OK. columns: {sorted(found)}")


def main() -> int:
    conn = get_conn()
    try:
        print("[migrate_outreach_v2] applying schema migration ...")
        _ensure_v2_columns(conn)
        print("[migrate_outreach_v2] rebuilding indexes (CONCURRENTLY) ...")
        _ensure_v2_indexes_concurrent(conn)
        print("[migrate_outreach_v2] verifying ...")
        _verify(conn)
        print("[migrate_outreach_v2] DONE.")
        return 0
    finally:
        release_conn(conn)


if __name__ == "__main__":
    sys.exit(main())
