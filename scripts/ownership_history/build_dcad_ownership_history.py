#!/usr/bin/env python3
"""Ingest DCAD multi-year certified rolls into ownership_snapshots.

Long-format owner-history loader (one row per account x year). Idempotent —
re-run safely when new years arrive (drop folders into the Historical dir and
re-run). Loads OWNER_NAME1 + DEED_TXFR_DATE per ACCOUNT_NUM, county='dcad'.

Run (points at the MAIN worktree's gitignored data; uses the same DB env as
the v4a backfills, i.e. Mike's prod Cloud SQL):

    python scripts/ownership_history/build_dcad_ownership_history.py \
        --historical-dir /home/kk/projects/clients/lot-ledger/ingest/counties/dallas/dcad/Historical
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from collections.abc import Iterator
from pathlib import Path

# Make the repo root importable when run as a plain script from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from psycopg2.extras import execute_values  # noqa: E402

from api.config import get_conn, release_conn  # noqa: E402

COUNTY = "dcad"
BATCH_SIZE = 50_000
DEFAULT_HISTORICAL_DIR = str(
    Path(__file__).resolve().parents[2]
    / "ingest" / "counties" / "dallas" / "dcad" / "Historical"
)


def _find_year_files(historical_dir: str) -> dict[int, str]:
    """{year: account_info.csv path} for each 4-digit year folder present.

    Real layout: <year>/DCAD<year>_CERTIFIED_<date>/account_info.csv (mixed case,
    nested). Match account_info.csv case-insensitively, recursively.
    """
    found: dict[int, str] = {}
    if not os.path.isdir(historical_dir):
        return found
    for entry in sorted(os.listdir(historical_dir)):
        if not (entry.isdigit() and len(entry) == 4):
            continue
        year_path = os.path.join(historical_dir, entry)
        if not os.path.isdir(year_path):
            continue
        for root, _dirs, files in os.walk(year_path):
            hit = next((f for f in files if f.lower() == "account_info.csv"), None)
            if hit:
                found[int(entry)] = os.path.join(root, hit)
                break
    return dict(sorted(found.items()))


def _parse_deed_date(raw: str | None) -> dt.date | None:
    """Parse DCAD 'MM/DD/YYYY' → date; None on blank/zero/unparseable."""
    if not raw:
        return None
    text = raw.strip()
    if not text or text.startswith("00/00"):
        return None
    try:
        return dt.datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError:
        return None


def _row_to_record(
    row: dict[str, str], year: int, source_file: str
) -> tuple | None:
    """Map one CSV row → upsert tuple. Tolerant of missing optional columns.
    Returns None when ACCOUNT_NUM is blank (caller skips)."""
    acct = (row.get("ACCOUNT_NUM") or "").strip()
    if not acct:
        return None
    owner = (row.get("OWNER_NAME1") or "").strip() or None
    deed = _parse_deed_date(row.get("DEED_TXFR_DATE"))
    return (COUNTY, acct, year, owner, deed, source_file)


def _stream_record_batches(
    path: str, year: int, source_file: str, batch_size: int = BATCH_SIZE
) -> Iterator[list[tuple]]:
    """Yield bounded batches of upsert tuples, streaming the CSV row-by-row.

    Peak memory is one batch (never the whole file or year), so ingesting an
    ~800k-row roll stays at a few MB instead of ~250-450 MB.
    """
    batch: list[tuple] = []
    with open(path, "r", encoding="latin-1", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rec = _row_to_record(row, year, source_file)
            if rec is None:
                continue
            batch.append(rec)
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch


def _create_table() -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS ownership_snapshots ("
                "  county TEXT NOT NULL,"
                "  account_num TEXT NOT NULL,"
                "  snapshot_year INTEGER NOT NULL,"
                "  owner_name TEXT,"
                "  deed_txfr_date DATE,"
                "  source_file TEXT NOT NULL,"
                "  ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
                "  PRIMARY KEY (county, account_num, snapshot_year))"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_ownership_snap_acct "
                "ON ownership_snapshots (county, account_num)"
            )
        conn.commit()
    finally:
        release_conn(conn)


def _upsert(records: list[tuple]) -> None:
    if not records:
        return
    conn = get_conn()
    try:
        sql = (
            "INSERT INTO ownership_snapshots "
            "(county, account_num, snapshot_year, owner_name, deed_txfr_date, source_file) "
            "VALUES %s "
            "ON CONFLICT (county, account_num, snapshot_year) DO UPDATE SET "
            "  owner_name = EXCLUDED.owner_name,"
            "  deed_txfr_date = EXCLUDED.deed_txfr_date,"
            "  source_file = EXCLUDED.source_file,"
            "  ingested_at = NOW()"
        )
        with conn.cursor() as cur:
            for i in range(0, len(records), 10000):
                execute_values(cur, sql, records[i : i + 10000], page_size=1000)
                print(f"  upserted {min(i + 10000, len(records)):,} / {len(records):,}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


def _ingest_year(year: int, path: str, batch_size: int = BATCH_SIZE) -> int:
    source_file = os.path.basename(path)
    total = 0
    for batch in _stream_record_batches(path, year, source_file, batch_size):
        _upsert(batch)
        total += len(batch)
    print(f"{year}: upserted {total:,} records from {source_file}")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest DCAD ownership history.")
    ap.add_argument("--historical-dir", default=DEFAULT_HISTORICAL_DIR)
    args = ap.parse_args()

    year_files = _find_year_files(args.historical_dir)
    if not year_files:
        raise SystemExit(
            f"No account_info.csv found under {args.historical_dir}/<year>/. "
            "Download + unzip the DCAD Certified Data Files first."
        )
    print(f"Years found: {sorted(year_files)}")
    _create_table()
    total = 0
    for year, path in year_files.items():
        total += _ingest_year(year, path)
    print(f"\nDone — {total:,} rows upserted across {len(year_files)} years.")


if __name__ == "__main__":
    main()
