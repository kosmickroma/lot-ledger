# scripts/build_dcad_appraisal_jurisdiction_backfill.py
#
# v4a §2.2.b — ALTER appraisal to add 5 jurisdiction _DESC + 6 _TAXABLE_VAL
# columns and backfill from data/ACCOUNT_APPRL_YEAR.CSV.
# Idempotent. Mirrors build_dcad_owner_fields_backfill.py.
#
# Source-header note: actual CSV uses *_JURIS_DESC (NOT *_jurisdiction_DESC
# as the bundled XLSX claims). Verified by head -1 data/ACCOUNT_APPRL_YEAR.CSV.

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


DEFAULT_SOURCE = ROOT_DIR / "data/ACCOUNT_APPRL_YEAR.CSV"
BATCH_SIZE = 5000

# Spec v4a.2 §2.2.b. ISD_JURIS_DESC excluded — already stored in
# appraisal.isd_desc from original ingest (Task 9 wires this through).
APPRAISAL_JURIS_COLUMN_DEFS: list[dict[str, str]] = [
    {"db": "city_jurisdiction_desc",         "csv": "CITY_JURIS_DESC",         "sql_type": "text"},
    {"db": "county_jurisdiction_desc",       "csv": "COUNTY_JURIS_DESC",       "sql_type": "text"},
    {"db": "hospital_jurisdiction_desc",     "csv": "HOSPITAL_JURIS_DESC",     "sql_type": "text"},
    {"db": "college_jurisdiction_desc",      "csv": "COLLEGE_JURIS_DESC",      "sql_type": "text"},
    {"db": "special_dist_jurisdiction_desc", "csv": "SPECIAL_DIST_JURIS_DESC", "sql_type": "text"},
    {"db": "city_taxable_val",               "csv": "CITY_TAXABLE_VAL",        "sql_type": "numeric"},
    {"db": "county_taxable_val",             "csv": "COUNTY_TAXABLE_VAL",      "sql_type": "numeric"},
    {"db": "isd_taxable_val",                "csv": "ISD_TAXABLE_VAL",         "sql_type": "numeric"},
    {"db": "hospital_taxable_val",           "csv": "HOSPITAL_TAXABLE_VAL",    "sql_type": "numeric"},
    {"db": "college_taxable_val",            "csv": "COLLEGE_TAXABLE_VAL",     "sql_type": "numeric"},
    {"db": "special_dist_taxable_val",       "csv": "SPECIAL_DIST_TAXABLE_VAL","sql_type": "numeric"},
]


def _clean_text(value: Any) -> str | None:
    text = (value or "").strip() if isinstance(value, str) else ""
    return text if text else None


def _to_numeric_or_none(value: Any) -> float | int | None:
    text = (value or "").strip() if isinstance(value, str) else value
    if text in (None, "", "   "):
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _build_load_row(src: dict[str, Any]) -> dict[str, Any] | None:
    account_num = (src.get("ACCOUNT_NUM") or "").strip()
    if not account_num:
        return None
    out: dict[str, Any] = {"account_num": account_num}
    for col in APPRAISAL_JURIS_COLUMN_DEFS:
        raw = src.get(col["csv"])
        out[col["db"]] = (
            _clean_text(raw) if col["sql_type"] == "text" else _to_numeric_or_none(raw)
        )
    return out


def _ensure_columns(cur) -> None:
    for col in APPRAISAL_JURIS_COLUMN_DEFS:
        cur.execute(
            f"ALTER TABLE appraisal ADD COLUMN IF NOT EXISTS {col['db']} {col['sql_type'].upper()}"
        )


def _read_csv_rows(source_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with source_path.open("r", encoding="latin-1", newline="") as fh:
        reader = csv.DictReader(fh)
        expected = {"ACCOUNT_NUM", *[c["csv"] for c in APPRAISAL_JURIS_COLUMN_DEFS]}
        missing = expected - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"ACCOUNT_APPRL_YEAR.CSV missing expected columns: {sorted(missing)!r}"
            )
        for src in reader:
            loaded = _build_load_row(src)
            if loaded is not None:
                rows.append(loaded)
    return rows


def _backfill(cur, rows: list[dict[str, Any]]) -> int:
    col_db_names = [c["db"] for c in APPRAISAL_JURIS_COLUMN_DEFS]
    temp_cols_sql = "account_num TEXT PRIMARY KEY, " + ", ".join(
        f"{c['db']} {c['sql_type'].upper()}" for c in APPRAISAL_JURIS_COLUMN_DEFS
    )
    cur.execute(
        f"CREATE TEMP TABLE _dcad_appr_load ({temp_cols_sql}) ON COMMIT DROP"
    )

    placeholders = "(" + ",".join(["%s"] * (1 + len(col_db_names))) + ")"
    insert_cols = "account_num, " + ", ".join(col_db_names)
    update_set = ", ".join(f"{n} = EXCLUDED.{n}" for n in col_db_names)
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
            f"INSERT INTO _dcad_appr_load ({insert_cols}) VALUES {args_str} "
            f"ON CONFLICT (account_num) DO UPDATE SET {update_set}"
        )
        total += len(batch)
    print(f"Loaded {total:,} rows into temp table.")

    # INSERT...ON CONFLICT handles both inserts (accounts in source not yet
    # in appraisal) and updates (existing rows). appraisal's PK is account_num.
    cur.execute(
        f"INSERT INTO appraisal (account_num, {', '.join(col_db_names)}) "
        f"SELECT account_num, {', '.join(col_db_names)} FROM _dcad_appr_load "
        f"ON CONFLICT (account_num) DO UPDATE SET {update_set}"
    )
    return cur.rowcount or 0


def _report_verification(cur) -> None:
    cur.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE city_jurisdiction_desc IS NOT NULL) AS city_pop,
          COUNT(*) FILTER (WHERE county_jurisdiction_desc IS NOT NULL) AS county_pop,
          COUNT(*) FILTER (WHERE city_taxable_val IS NOT NULL) AS city_tax_pop,
          COUNT(*) AS total
        FROM appraisal
        """
    )
    city_pop, county_pop, city_tax_pop, total = cur.fetchone()
    print(
        f"appraisal: total={total:,}  "
        f"city_juris_desc={city_pop:,} ({100.0*city_pop/total:.2f}%)  "
        f"county_juris_desc={county_pop:,} ({100.0*county_pop/total:.2f}%)  "
        f"city_taxable_val={city_tax_pop:,} ({100.0*city_tax_pop/total:.2f}%)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill DCAD appraisal jurisdiction columns."
    )
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help="Path to ACCOUNT_APPRL_YEAR.CSV. Default: data/ACCOUNT_APPRL_YEAR.CSV",
    )
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.exists():
        raise SystemExit(f"Source not found: {source_path}")

    print(f"Reading jurisdiction fields from: {source_path}")
    rows = _read_csv_rows(source_path)
    print(f"Found {len(rows):,} appraisal rows with non-blank ACCOUNT_NUM.")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            print("Ensuring 11 new appraisal columns exist...")
            _ensure_columns(cur)

            print("Backfill via temp-table UPSERT...")
            affected = _backfill(cur, rows)
            print(f"Backfill affected {affected:,} appraisal rows.")

            conn.commit()
            print()
            print("=== Verification ===")
            _report_verification(cur)
    finally:
        release_conn(conn)


if __name__ == "__main__":
    main()
