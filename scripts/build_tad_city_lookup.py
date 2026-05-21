# scripts/build_tad_city_lookup.py
#
# One-shot ingest of TAD's Cities.shp DBF into a small lookup table, plus
# idempotent ALTER on tad_parcels to add property_city + backfill from the
# lookup on city_code = city_tdc.
#
# Source:  ingest/counties/tarrant/tad/<snapshot>/unzipped/Cities/Cities.shp
# Target:  lotledger.tad_city_lookup  (city_tdc PK, city_name, city_text)
#          lotledger.tad_parcels.property_city  (added if missing, then backfilled)
#
# Connects to:
#   api/config.py            - shared DB connection helpers (main DB, not sessions)
#   PostgreSQL/PostGIS       - writes to lotledger DB
#
# Run:
#   .venv/bin/python3 scripts/build_tad_city_lookup.py
#   (default source path is the 2026-05-01 snapshot — pass --source to override)

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import shapefile

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn


DEFAULT_SOURCE = ROOT_DIR / "ingest/counties/tarrant/tad/2026-05-01/unzipped/Cities/Cities"


def _ensure_lookup_table(cur) -> None:
    """Idempotent CREATE for tad_city_lookup."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tad_city_lookup (
            city_tdc TEXT PRIMARY KEY,
            city_name TEXT NOT NULL,
            city_text TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _ensure_parcels_property_city(cur) -> None:
    """Idempotent ALTER to add property_city to tad_parcels.

    The tad_parcels table is created by scripts/build_tad_db.py:_ensure_table.
    This ALTER lives here (not in api/main.py:_ensure_session_schema) because
    tad_parcels is in the main DB, not the sessions DB — _ensure_session_schema
    runs against the wrong connection.
    """
    cur.execute(
        "ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS property_city TEXT"
    )


def _read_cities_shp(source_path: Path) -> list[tuple[str, str, str]]:
    """Read the Cities.shp DBF and return (city_tdc, city_name_upper, city_text) tuples."""
    reader = shapefile.Reader(str(source_path), encoding="latin-1")
    fields = [f[0] for f in reader.fields[1:]]

    required = {"CITY_TDC", "CITY_NAME"}
    missing = required - set(fields)
    if missing:
        raise ValueError(
            f"Cities.shp DBF missing required fields: {sorted(missing)}. "
            f"Got: {sorted(fields)}"
        )

    rows: list[tuple[str, str, str]] = []
    for rec in reader.iterShapeRecords():
        record = dict(zip(fields, rec.record))
        city_tdc = str(record.get("CITY_TDC") or "").strip()
        # Normalize to upper to match Collin/Denton's existing property_city
        # casing convention.
        city_name = str(record.get("CITY_NAME") or "").strip().upper()
        city_text = str(record.get("CITY_TEXT") or "").strip()
        if city_tdc and city_name:
            rows.append((city_tdc, city_name, city_text))

    return rows


def _upsert_lookup_rows(cur, rows: list[tuple[str, str, str]]) -> None:
    cur.executemany(
        """
        INSERT INTO tad_city_lookup (city_tdc, city_name, city_text)
        VALUES (%s, %s, %s)
        ON CONFLICT (city_tdc) DO UPDATE SET
            city_name = EXCLUDED.city_name,
            city_text = EXCLUDED.city_text,
            updated_at = now()
        """,
        rows,
    )


def _backfill_property_city(cur) -> int:
    """JOIN-update tad_parcels.property_city from the lookup. Returns rows updated."""
    cur.execute(
        """
        UPDATE tad_parcels
        SET property_city = lookup.city_name
        FROM tad_city_lookup lookup
        WHERE tad_parcels.city_code = lookup.city_tdc
          AND (tad_parcels.property_city IS NULL OR tad_parcels.property_city = '')
        """
    )
    return cur.rowcount or 0


def _report_verification(cur) -> None:
    """Print the three verification queries from the spec for KK to eyeball."""
    cur.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE property_city IS NOT NULL AND property_city != '') AS populated,
          COUNT(*) FILTER (WHERE property_city IS NULL OR property_city = '') AS still_missing,
          COUNT(*) AS total
        FROM tad_parcels
        """
    )
    populated, still_missing, total = cur.fetchone()
    print(
        f"tad_parcels.property_city: populated={populated:,}  still_missing={still_missing:,}  "
        f"total={total:,}  ({100.0 * populated / total:.1f}% populated)"
    )

    cur.execute(
        """
        SELECT property_city, COUNT(*) AS n
        FROM tad_parcels
        WHERE property_city IS NOT NULL AND property_city != ''
        GROUP BY property_city
        ORDER BY n DESC
        LIMIT 10
        """
    )
    print("Top 10 cities by parcel count:")
    for row in cur.fetchall():
        print(f"  {row[0]:<32} {row[1]:>10,}")

    cur.execute(
        """
        SELECT t.city_code, COUNT(*) AS n
        FROM tad_parcels t
        LEFT JOIN tad_city_lookup l ON t.city_code = l.city_tdc
        WHERE t.city_code IS NOT NULL AND t.city_code != ''
          AND l.city_tdc IS NULL
        GROUP BY t.city_code
        ORDER BY n DESC
        LIMIT 20
        """
    )
    unmatched = cur.fetchall()
    if unmatched:
        print(f"Unmatched city_codes ({len(unmatched)} distinct):")
        for row in unmatched:
            print(f"  code={row[0]!r}  parcels={row[1]:,}")
    else:
        print("Unmatched city_codes: 0 (every TAD city_code has a lookup row)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build TAD city lookup from Cities.shp DBF and backfill tad_parcels.property_city."
    )
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help="Path to Cities.shp (no extension). Default: 2026-05-01 snapshot.",
    )
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.with_suffix(".shp").exists() and not source_path.exists():
        raise SystemExit(f"Source not found: {source_path}")

    print(f"Reading Cities.shp DBF from: {source_path}")
    rows = _read_cities_shp(source_path)
    print(f"Loaded {len(rows)} city rows.")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            print("Ensuring tad_city_lookup table exists...")
            _ensure_lookup_table(cur)

            print("Ensuring tad_parcels.property_city column exists...")
            _ensure_parcels_property_city(cur)

            print(f"Upserting {len(rows)} lookup rows...")
            _upsert_lookup_rows(cur, rows)

            print("Backfilling tad_parcels.property_city from lookup...")
            updated = _backfill_property_city(cur)
            print(f"Backfill updated {updated:,} rows.")

            conn.commit()

            print()
            print("=== Verification ===")
            _report_verification(cur)
    finally:
        release_conn(conn)


if __name__ == "__main__":
    main()
