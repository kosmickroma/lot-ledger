# scripts/build_dcad_property_city.py
#
# One-shot ingest of DCAD's PROPERTY_CITY column from ACCOUNT_INFO.CSV into
# the parcels table. Idempotent ALTER on parcels to add property_city, then
# bulk UPDATE by account_num from the CSV.
#
# DCAD's source ACCOUNT_INFO.CSV has had PROPERTY_CITY all along; the original
# build_db.py ingest just never pulled it. This script closes that gap for
# the existing 759k DCAD rows without requiring a full re-ingest, AND
# build_db.py is updated alongside this so future re-ingests stay populated.
#
# Source:  data/ACCOUNT_INFO.CSV  (column "PROPERTY_CITY", index 21)
# Target:  lotledger.parcels.property_city  (added if missing, then backfilled)
#
# Connects to:
#   api/config.py    - shared DB connection helpers (main DB, not sessions)
#   PostgreSQL       - writes to lotledger DB
#
# Run:
#   .venv/bin/python3 scripts/build_dcad_property_city.py
#   (default source path is data/ACCOUNT_INFO.CSV — pass --source to override)
#
# Notes on the `(DALLAS CO)` suffix:
#   Cities that span multiple counties have the Dallas-portion labeled like
#   "GARLAND (DALLAS CO)" or "MESQUITE (DALLAS CO)" — strip that suffix at
#   ingest time so the DB stores clean city names. Same canonical pattern
#   as Collin/Denton/TAD.

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
import re

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn


DEFAULT_SOURCE = ROOT_DIR / "data/ACCOUNT_INFO.CSV"
BATCH_SIZE = 5000

# Strip trailing county-disambiguation suffix DCAD uses for multi-county
# cities: "GARLAND (DALLAS CO)" → "GARLAND". Case-insensitive on the suffix.
_DALLAS_CO_SUFFIX = re.compile(r"\s*\(DALLAS\s+CO\)\s*$", re.IGNORECASE)


def _normalize_city(raw: str) -> str:
    """Strip whitespace + (DALLAS CO) suffix. Return uppercase clean name."""
    text = (raw or "").strip()
    if not text:
        return ""
    text = _DALLAS_CO_SUFFIX.sub("", text).strip()
    return text.upper()


def _ensure_property_city_column(cur) -> None:
    """Idempotent ALTER to add property_city to parcels.

    parcels table is created by scripts/build_db.py at ingest time. This
    ALTER lives here (not in api/main.py:_ensure_session_schema) because
    parcels is in the main DB, not the sessions DB.
    """
    cur.execute(
        "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS property_city TEXT"
    )


def _read_csv_rows(source_path: Path) -> list[tuple[str, str]]:
    """Stream ACCOUNT_INFO.CSV and return (account_num, normalized_city) pairs.

    Skips rows where city is empty after normalization. Account numbers are
    kept exactly as the source CSV emits them (no padding/normalization here;
    parcels.account_num was ingested from the same CSV so they match).
    """
    pairs: list[tuple[str, str]] = []
    with source_path.open("r", encoding="latin-1", newline="") as fh:
        reader = csv.DictReader(fh)
        if "ACCOUNT_NUM" not in reader.fieldnames or "PROPERTY_CITY" not in reader.fieldnames:
            raise ValueError(
                f"ACCOUNT_INFO.CSV missing expected columns. Got: {reader.fieldnames!r}"
            )
        for row in reader:
            account_num = (row.get("ACCOUNT_NUM") or "").strip()
            city = _normalize_city(row.get("PROPERTY_CITY") or "")
            if account_num and city:
                pairs.append((account_num, city))
    return pairs


def _backfill_property_city(cur, pairs: list[tuple[str, str]]) -> int:
    """Bulk UPDATE parcels.property_city using a temp table for speed.

    A single multi-million-row UPDATE via CASE WHEN account_num would crawl;
    a temp table + JOIN is the standard idiom.
    """
    cur.execute(
        """
        CREATE TEMP TABLE _dcad_city_load (
            account_num TEXT PRIMARY KEY,
            property_city TEXT NOT NULL
        ) ON COMMIT DROP
        """
    )

    # Batch insert into temp table
    total = 0
    for i in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[i:i + BATCH_SIZE]
        args_str = ",".join(
            cur.mogrify("(%s,%s)", (acct, city)).decode("utf-8")
            for acct, city in batch
        )
        cur.execute(
            f"INSERT INTO _dcad_city_load (account_num, property_city) VALUES {args_str} "
            "ON CONFLICT (account_num) DO UPDATE SET property_city = EXCLUDED.property_city"
        )
        total += len(batch)

    print(f"Loaded {total:,} (account_num, city) pairs into temp table.")

    cur.execute(
        """
        UPDATE parcels p
        SET property_city = load.property_city
        FROM _dcad_city_load load
        WHERE p.account_num = load.account_num
          AND (p.property_city IS NULL OR p.property_city = '' OR p.property_city != load.property_city)
        """
    )
    return cur.rowcount or 0


def _report_verification(cur) -> None:
    """Print verification stats for KK to eyeball."""
    cur.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE property_city IS NOT NULL AND property_city != '') AS populated,
          COUNT(*) FILTER (WHERE property_city IS NULL OR property_city = '') AS still_missing,
          COUNT(*) AS total
        FROM parcels
        WHERE division_cd IN ('RES', 'COM')
        """
    )
    populated, still_missing, total = cur.fetchone()
    print(
        f"parcels.property_city (RES/COM only): populated={populated:,}  "
        f"still_missing={still_missing:,}  total={total:,}  "
        f"({100.0 * populated / total:.1f}% populated)"
    )

    cur.execute(
        """
        SELECT property_city, COUNT(*) AS n
        FROM parcels
        WHERE property_city IS NOT NULL AND property_city != ''
        GROUP BY property_city
        ORDER BY n DESC
        LIMIT 12
        """
    )
    print("Top 12 cities by parcel count:")
    for row in cur.fetchall():
        print(f"  {row[0]:<32} {row[1]:>10,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate DCAD parcels.property_city from ACCOUNT_INFO.CSV."
    )
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help="Path to ACCOUNT_INFO.CSV. Default: data/ACCOUNT_INFO.CSV",
    )
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.exists():
        raise SystemExit(f"Source not found: {source_path}")

    print(f"Reading PROPERTY_CITY from: {source_path}")
    pairs = _read_csv_rows(source_path)
    print(f"Found {len(pairs):,} parcels with non-empty PROPERTY_CITY.")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            print("Ensuring parcels.property_city column exists...")
            _ensure_property_city_column(cur)

            print("Backfilling parcels.property_city via temp-table JOIN...")
            updated = _backfill_property_city(cur, pairs)
            print(f"Backfill updated {updated:,} rows.")

            conn.commit()

            print()
            print("=== Verification ===")
            _report_verification(cur)
    finally:
        release_conn(conn)


if __name__ == "__main__":
    main()
