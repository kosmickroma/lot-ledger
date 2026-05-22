# scripts/build_dcad_res_detail_backfill.py
#
# One-shot backfill of the 27 new res_detail columns added in v3 of the
# CAD residential detail expansion. Reads data/RES_DETAIL.CSV, applies the
# same v3 normalizations (flag canonicalization, text cleanup, integer
# coercion), and bulk-UPDATEs the existing 682k res_detail rows via a
# temp-table-JOIN pattern — same shape as build_dcad_property_city.py.
#
# Source:  data/RES_DETAIL.CSV (column index 11 = NUM_STORIES_DESC, 23+ = NUM_*, etc.)
# Target:  lotledger.res_detail.{27 new columns}
#
# Connects to:
#   api/config.py       - shared DB connection helpers (main DB, not sessions)
#   PostgreSQL          - writes to lotledger DB
#
# Run:
#   .venv/bin/python3 scripts/build_dcad_res_detail_backfill.py
#   (default source path is data/RES_DETAIL.CSV — pass --source to override)

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn


DEFAULT_SOURCE = ROOT_DIR / "data" / "RES_DETAIL.CSV"
BATCH_SIZE = 5000

# Schema columns being backfilled. Order matters — used as the column list
# in the temp table CREATE + INSERT below. Keep in sync with the row dict
# produced in scripts/build_db.py:_build_res_detail_table.
_NEW_INT_COLS = [
    "num_bedrooms", "num_full_baths", "num_half_baths",
    "num_fireplaces", "num_kitchens", "num_wet_bars", "num_units",
    "eff_yr_built", "act_age",
]
_NEW_FLAG_COLS = [
    "pool_ind", "spa_ind", "sauna_ind", "sprinkler_sys_ind", "deck_ind",
]
_NEW_TEXT_COLS = [
    "num_stories_desc", "bldg_class_desc", "cdu_rating_desc",
    "constr_fram_typ_desc", "foundation_typ_desc",
    "heating_typ_desc", "ac_typ_desc", "fence_typ_desc",
    "ext_wall_desc", "basement_desc",
    "roof_typ_desc", "roof_mat_desc",
    "pct_complete",
]
_ALL_NEW_COLS = _NEW_INT_COLS + _NEW_FLAG_COLS + _NEW_TEXT_COLS

# CSV → DB column-name mapping. CSV uses UPPER_SNAKE; DB uses lower_snake.
_CSV_TO_DB = {col.upper(): col for col in _ALL_NEW_COLS}


def _normalize_flag(value: object) -> str | None:
    """Canonical 'T' / 'F' / NULL. Mirrors build_db.py + dcad.py helpers."""
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if text in {"Y", "T", "TRUE", "YES", "1"}:
        return "T"
    if text in {"N", "F", "FALSE", "NO", "0"}:
        return "F"
    return None


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (ValueError, TypeError):
        try:
            return int(float(text))
        except (ValueError, TypeError):
            return None


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ensure_columns(cur) -> None:
    """Idempotent ALTERs to add the 27 new columns to res_detail. NULL-able
    so existing rows preserve their state until backfilled."""
    int_alters = [f"ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS {c} INTEGER" for c in _NEW_INT_COLS]
    flag_alters = [f"ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS {c} TEXT" for c in _NEW_FLAG_COLS]
    text_alters = [f"ALTER TABLE res_detail ADD COLUMN IF NOT EXISTS {c} TEXT" for c in _NEW_TEXT_COLS]
    for sql in int_alters + flag_alters + text_alters:
        cur.execute(sql)


def _read_csv_rows(source_path: Path) -> list[tuple]:
    """Stream RES_DETAIL.CSV and return (account_num, *new_col_values) tuples
    in the same column order as _ALL_NEW_COLS."""
    rows: list[tuple] = []
    with source_path.open("r", encoding="latin-1", newline="") as fh:
        reader = csv.DictReader(fh)
        required = {"ACCOUNT_NUM"} | set(_CSV_TO_DB.keys())
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"RES_DETAIL.CSV missing expected columns: {sorted(missing)}")

        seen_accounts: set[str] = set()
        for raw in reader:
            account_num = (raw.get("ACCOUNT_NUM") or "").strip()
            if not account_num or account_num in seen_accounts:
                continue
            seen_accounts.add(account_num)

            int_values = [_safe_int(raw.get(c.upper())) for c in _NEW_INT_COLS]
            flag_values = [_normalize_flag(raw.get(c.upper())) for c in _NEW_FLAG_COLS]
            text_values = [_clean_text(raw.get(c.upper())) for c in _NEW_TEXT_COLS]

            rows.append((account_num, *int_values, *flag_values, *text_values))
    return rows


def _backfill(cur, rows: list[tuple]) -> int:
    """Bulk-UPDATE res_detail via temp-table JOIN. Returns updated row count."""
    col_defs = (
        ["account_num TEXT PRIMARY KEY"]
        + [f"{c} INTEGER" for c in _NEW_INT_COLS]
        + [f"{c} TEXT" for c in _NEW_FLAG_COLS]
        + [f"{c} TEXT" for c in _NEW_TEXT_COLS]
    )
    cur.execute(
        f"CREATE TEMP TABLE _dcad_res_load ({', '.join(col_defs)}) ON COMMIT DROP"
    )

    placeholders = "(" + ",".join(["%s"] * (1 + len(_ALL_NEW_COLS))) + ")"
    insert_cols = ", ".join(["account_num"] + _ALL_NEW_COLS)
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        args_str = ",".join(cur.mogrify(placeholders, r).decode("utf-8") for r in batch)
        cur.execute(
            f"INSERT INTO _dcad_res_load ({insert_cols}) VALUES {args_str} "
            f"ON CONFLICT (account_num) DO UPDATE SET "
            + ", ".join(f"{c} = EXCLUDED.{c}" for c in _ALL_NEW_COLS)
        )
        total += len(batch)

    print(f"Loaded {total:,} rows into temp table.")

    updates = ", ".join(f"{c} = load.{c}" for c in _ALL_NEW_COLS)
    cur.execute(
        f"UPDATE res_detail r SET {updates} "
        f"FROM _dcad_res_load load "
        f"WHERE r.account_num = load.account_num"
    )
    return cur.rowcount or 0


def _report_verification(cur) -> None:
    """Print verification stats — coverage per representative column."""
    cur.execute(
        """
        SELECT
          COUNT(*) AS total_rows,
          COUNT(*) FILTER (WHERE num_bedrooms IS NOT NULL) AS has_beds,
          COUNT(*) FILTER (WHERE num_full_baths IS NOT NULL) AS has_full_baths,
          COUNT(*) FILTER (WHERE num_half_baths IS NOT NULL) AS has_half_baths,
          COUNT(*) FILTER (WHERE pool_ind = 'T') AS pool_t,
          COUNT(*) FILTER (WHERE pool_ind = 'F') AS pool_f,
          COUNT(*) FILTER (WHERE pool_ind IS NULL) AS pool_null,
          COUNT(*) FILTER (WHERE foundation_typ_desc IS NOT NULL AND foundation_typ_desc != '') AS has_foundation,
          COUNT(*) FILTER (WHERE roof_typ_desc IS NOT NULL AND roof_typ_desc != '') AS has_roof
        FROM res_detail
        """
    )
    row = cur.fetchone()
    cols = ["total_rows", "has_beds", "has_full_baths", "has_half_baths",
            "pool_t", "pool_f", "pool_null", "has_foundation", "has_roof"]
    for col, val in zip(cols, row):
        pct = (100.0 * val / row[0]) if row[0] else 0
        print(f"  {col:<25} {val:>10,}  ({pct:.1f}%)")

    print()
    cur.execute(
        "SELECT foundation_typ_desc, COUNT(*) FROM res_detail "
        "WHERE foundation_typ_desc IS NOT NULL AND foundation_typ_desc != '' "
        "GROUP BY foundation_typ_desc ORDER BY COUNT(*) DESC LIMIT 5"
    )
    print("Top foundation types (sanity check):")
    for r in cur.fetchall():
        print(f"  {r[0]:<30} {r[1]:>10,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill DCAD res_detail with 27 new columns from RES_DETAIL.CSV."
    )
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help="Path to RES_DETAIL.CSV. Default: data/RES_DETAIL.CSV",
    )
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.exists():
        raise SystemExit(f"Source not found: {source_path}")

    print(f"Reading RES_DETAIL.CSV from: {source_path}")
    rows = _read_csv_rows(source_path)
    print(f"Parsed {len(rows):,} distinct ACCOUNT_NUM rows.")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            print("Ensuring 27 new columns exist on res_detail...")
            _ensure_columns(cur)

            print("Backfilling via temp-table JOIN...")
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
