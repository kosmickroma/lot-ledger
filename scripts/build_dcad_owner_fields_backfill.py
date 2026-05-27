# scripts/build_dcad_owner_fields_backfill.py
#
# v4a §2.2.a + §2.4 — Adds 11 new TEXT columns to the parcels table and
# backfills them from data/ACCOUNT_INFO.CSV. Idempotent (re-running is a
# no-op on unchanged source). Mirrors the proven temp-table + JOIN pattern
# from scripts/build_dcad_property_city.py.
#
# Connects to:
#   api/config.py            - shared DB connection helpers
#   data/ACCOUNT_INFO.CSV    - DCAD source
#   PostgreSQL table parcels - target

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn


DEFAULT_SOURCE = ROOT_DIR / "data/ACCOUNT_INFO.CSV"
BATCH_SIZE = 5000

# Single source of truth: maps DB column ↔ CSV header.
# Spec v4a.2 §2.2.a. Order is the canonical column order used everywhere.
OWNER_FIELDS_COLUMN_DEFS: list[dict[str, str]] = [
    {"db": "owner_name2",         "csv": "OWNER_NAME2"},
    {"db": "biz_name",            "csv": "BIZ_NAME"},
    {"db": "owner_address_line1", "csv": "OWNER_ADDRESS_LINE1"},
    {"db": "owner_address_line2", "csv": "OWNER_ADDRESS_LINE2"},
    {"db": "owner_address_line3", "csv": "OWNER_ADDRESS_LINE3"},
    {"db": "owner_address_line4", "csv": "OWNER_ADDRESS_LINE4"},
    {"db": "owner_country",       "csv": "OWNER_COUNTRY"},
    {"db": "street_half_num",     "csv": "STREET_HALF_NUM"},
    {"db": "bldg_id",             "csv": "BLDG_ID"},
    {"db": "unit_id",             "csv": "UNIT_ID"},
    {"db": "mapsco",              "csv": "MAPSCO"},
]


def _clean(value: Any) -> str | None:
    """Trim whitespace; return None for empty strings so SQL NULL is stored."""
    text = (value or "").strip() if isinstance(value, str) else ""
    return text if text else None


def _build_load_row(src: dict[str, Any]) -> dict[str, Any] | None:
    """Map one ACCOUNT_INFO.CSV row into the temp-table column shape.

    Returns None when ACCOUNT_NUM is blank (skip the row).
    """
    account_num = (src.get("ACCOUNT_NUM") or "").strip()
    if not account_num:
        return None
    out: dict[str, Any] = {"account_num": account_num}
    for col in OWNER_FIELDS_COLUMN_DEFS:
        out[col["db"]] = _clean(src.get(col["csv"]))
    return out


def _ensure_columns(cur) -> None:
    for col in OWNER_FIELDS_COLUMN_DEFS:
        cur.execute(
            f"ALTER TABLE parcels ADD COLUMN IF NOT EXISTS {col['db']} TEXT"
        )


def _read_csv_rows(source_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with source_path.open("r", encoding="latin-1", newline="") as fh:
        reader = csv.DictReader(fh)
        # Validate required headers up front.
        expected = {"ACCOUNT_NUM", *[c["csv"] for c in OWNER_FIELDS_COLUMN_DEFS]}
        missing = expected - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"ACCOUNT_INFO.CSV missing expected columns: {sorted(missing)!r}"
            )
        for src in reader:
            loaded = _build_load_row(src)
            if loaded is not None:
                rows.append(loaded)
    return rows


def _backfill(cur, rows: list[dict[str, Any]]) -> int:
    col_db_names = [c["db"] for c in OWNER_FIELDS_COLUMN_DEFS]
    temp_cols_sql = "account_num TEXT PRIMARY KEY, " + ", ".join(
        f"{n} TEXT" for n in col_db_names
    )
    cur.execute(
        f"CREATE TEMP TABLE _dcad_owner_load ({temp_cols_sql}) ON COMMIT DROP"
    )

    placeholders = "(" + ",".join(["%s"] * (1 + len(col_db_names))) + ")"
    insert_cols = "account_num, " + ", ".join(col_db_names)
    update_set = ", ".join(
        f"{n} = EXCLUDED.{n}" for n in col_db_names
    )
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        args_str = ",".join(
            cur.mogrify(
                placeholders,
                (r["account_num"], *(r[n] for n in col_db_names)),
            ).decode("utf-8")
            for r in batch
        )
        cur.execute(
            f"INSERT INTO _dcad_owner_load ({insert_cols}) VALUES {args_str} "
            f"ON CONFLICT (account_num) DO UPDATE SET {update_set}"
        )
        total += len(batch)
    print(f"Loaded {total:,} rows into temp table.")

    update_assignments = ", ".join(
        f"{n} = load.{n}" for n in col_db_names
    )
    cur.execute(
        f"UPDATE parcels p SET {update_assignments} "
        "FROM _dcad_owner_load load WHERE p.account_num = load.account_num"
    )
    return cur.rowcount or 0


def _report_verification(cur) -> None:
    cur.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE biz_name IS NOT NULL AND biz_name <> '') AS biz_populated,
          COUNT(*) FILTER (WHERE owner_address_line1 IS NOT NULL AND owner_address_line1 <> '') AS line1_populated,
          COUNT(*) FILTER (WHERE owner_country IS NOT NULL AND owner_country <> '') AS country_populated,
          COUNT(*) AS total
        FROM parcels
        WHERE division_cd IN ('RES', 'COM')
        """
    )
    biz, line1, country, total = cur.fetchone()
    print(
        f"parcels (RES/COM only): total={total:,}  "
        f"biz_name={biz:,} ({100.0*biz/total:.2f}%)  "
        f"owner_address_line1={line1:,} ({100.0*line1/total:.2f}%)  "
        f"owner_country={country:,} ({100.0*country/total:.2f}%)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill DCAD parcels with 11 owner+property raw fields."
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

    print(f"Reading owner+property raw fields from: {source_path}")
    rows = _read_csv_rows(source_path)
    print(f"Found {len(rows):,} parcels with non-blank ACCOUNT_NUM.")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            print("Ensuring 11 new parcels columns exist...")
            _ensure_columns(cur)

            print("Bulk UPDATE via temp-table JOIN...")
            updated = _backfill(cur, rows)
            print(f"Backfill updated {updated:,} rows.")

            conn.commit()
            print()
            print("=== Verification ===")
            _report_verification(cur)
    finally:
        release_conn(conn)


if __name__ == "__main__":
    main()
