# scripts/build_dcad_multi_owner.py
#
# v4a §2.3 + §2.4 — Creates multi_owner table (if missing) and runs a
# per-year full sync from data/MULTI_OWNER.CSV. NOT EXISTS anti-join
# is used to clear stale rows (more robust than NOT IN for NULL/tuple
# semantics + better query planner choice).

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn, CURRENT_APPRAISAL_YEAR


DEFAULT_SOURCE = ROOT_DIR / "data/MULTI_OWNER.CSV"
BATCH_SIZE = 5000


def _create_table_sql() -> str:
    return (
        "CREATE TABLE IF NOT EXISTS multi_owner (\n"
        "    appraisal_yr   INTEGER NOT NULL,\n"
        "    account_num    TEXT NOT NULL,\n"
        "    owner_seq_num  INTEGER NOT NULL,\n"
        "    owner_name     TEXT NOT NULL,\n"
        "    ownership_pct  NUMERIC(5,2),\n"
        "    PRIMARY KEY (appraisal_yr, account_num, owner_seq_num)\n"
        ")"
    )


def _create_index_sql() -> str:
    return (
        "CREATE INDEX IF NOT EXISTS idx_multi_owner_account "
        "ON multi_owner (account_num)"
    )


def _delete_stale_sql(current_year: int) -> str:
    # Per spec v4a.2 §2.4 — NOT EXISTS, NOT NOT IN.
    return (
        f"DELETE FROM multi_owner mo\n"
        f"WHERE mo.appraisal_yr = {int(current_year)}\n"
        f"  AND NOT EXISTS (\n"
        f"    SELECT 1 FROM _multi_owner_load l\n"
        f"    WHERE l.account_num = mo.account_num\n"
        f"      AND l.owner_seq_num = mo.owner_seq_num\n"
        f"  )"
    )


def _to_int_or_none(value: Any) -> int | None:
    text = (value or "").strip() if isinstance(value, str) else value
    if text in (None, ""):
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _to_float_or_none(value: Any) -> float | None:
    text = (value or "").strip() if isinstance(value, str) else value
    if text in (None, ""):
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _build_load_row(src: dict[str, Any]) -> dict[str, Any] | None:
    account_num = (src.get("ACCOUNT_NUM") or "").strip()
    seq_num = _to_int_or_none(src.get("OWNER_SEQ_NUM"))
    appraisal_yr = _to_int_or_none(src.get("APPRAISAL_YR"))
    if not account_num or seq_num is None or appraisal_yr is None:
        return None
    owner_name = (src.get("OWNER_NAME") or "").strip()
    if not owner_name:
        return None
    return {
        "appraisal_yr": appraisal_yr,
        "account_num": account_num,
        "owner_seq_num": seq_num,
        "owner_name": owner_name,
        "ownership_pct": _to_float_or_none(src.get("OWNERSHIP_PCT")),
    }


def _read_csv_rows(source_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with source_path.open("r", encoding="latin-1", newline="") as fh:
        reader = csv.DictReader(fh)
        expected = {
            "APPRAISAL_YR", "ACCOUNT_NUM", "OWNER_SEQ_NUM",
            "OWNER_NAME", "OWNERSHIP_PCT",
        }
        missing = expected - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"MULTI_OWNER.CSV missing expected columns: {sorted(missing)!r}"
            )
        for src in reader:
            loaded = _build_load_row(src)
            if loaded is not None:
                rows.append(loaded)
    return rows


def _sync(cur, rows: list[dict[str, Any]], current_year: int) -> tuple[int, int]:
    cur.execute(
        "CREATE TEMP TABLE _multi_owner_load (\n"
        "    appraisal_yr   INTEGER NOT NULL,\n"
        "    account_num    TEXT NOT NULL,\n"
        "    owner_seq_num  INTEGER NOT NULL,\n"
        "    owner_name     TEXT NOT NULL,\n"
        "    ownership_pct  NUMERIC(5,2),\n"
        "    PRIMARY KEY (appraisal_yr, account_num, owner_seq_num)\n"
        ") ON COMMIT DROP"
    )

    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        args_str = ",".join(
            cur.mogrify(
                "(%s,%s,%s,%s,%s)",
                (
                    r["appraisal_yr"],
                    r["account_num"],
                    r["owner_seq_num"],
                    r["owner_name"],
                    r["ownership_pct"],
                ),
            ).decode("utf-8")
            for r in batch
        )
        cur.execute(
            "INSERT INTO _multi_owner_load "
            "(appraisal_yr, account_num, owner_seq_num, owner_name, ownership_pct) "
            f"VALUES {args_str} "
            "ON CONFLICT (appraisal_yr, account_num, owner_seq_num) DO UPDATE SET "
            "owner_name = EXCLUDED.owner_name, "
            "ownership_pct = EXCLUDED.ownership_pct"
        )
        total += len(batch)

    cur.execute(
        "INSERT INTO multi_owner "
        "(appraisal_yr, account_num, owner_seq_num, owner_name, ownership_pct) "
        "SELECT appraisal_yr, account_num, owner_seq_num, owner_name, ownership_pct "
        "FROM _multi_owner_load "
        "ON CONFLICT (appraisal_yr, account_num, owner_seq_num) DO UPDATE SET "
        "owner_name = EXCLUDED.owner_name, "
        "ownership_pct = EXCLUDED.ownership_pct"
    )
    upserted = cur.rowcount or 0

    cur.execute(_delete_stale_sql(current_year))
    deleted = cur.rowcount or 0
    return upserted, deleted


def _report_verification(cur, current_year: int) -> None:
    cur.execute(
        "SELECT COUNT(*), COUNT(DISTINCT account_num) "
        "FROM multi_owner WHERE appraisal_yr = %s",
        (current_year,),
    )
    total, distinct_accounts = cur.fetchone()
    print(
        f"multi_owner @ year={current_year}: rows={total:,} "
        f"distinct_accounts={distinct_accounts:,}"
    )

    cur.execute(
        "SELECT owner_seq_num, COUNT(*) "
        "FROM multi_owner WHERE appraisal_yr = %s "
        "GROUP BY owner_seq_num ORDER BY owner_seq_num",
        (current_year,),
    )
    print("Distribution by seq_num:")
    for seq_num, n in cur.fetchall():
        print(f"  seq={seq_num}: {n:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Per-year sync of DCAD multi_owner from MULTI_OWNER.CSV."
    )
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help="Path to MULTI_OWNER.CSV. Default: data/MULTI_OWNER.CSV",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=CURRENT_APPRAISAL_YEAR,
        help=f"Appraisal year to sync. Default: {CURRENT_APPRAISAL_YEAR} (from api.config).",
    )
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.exists():
        raise SystemExit(f"Source not found: {source_path}")

    print(f"Reading multi_owner rows from: {source_path}  (year={args.year})")
    rows = _read_csv_rows(source_path)
    # Year-scoped sync: only ingest rows matching the requested year so we
    # never accidentally clear other years.
    year_rows = [r for r in rows if r["appraisal_yr"] == args.year]
    print(
        f"Filtered to year={args.year}: {len(year_rows):,} rows "
        f"(out of {len(rows):,} total in source CSV)."
    )

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            print("Ensuring multi_owner table + index exist...")
            cur.execute(_create_table_sql())
            cur.execute(_create_index_sql())

            print("Year-scoped UPSERT + NOT EXISTS anti-join delete...")
            upserted, deleted = _sync(cur, year_rows, args.year)
            print(f"Upserted {upserted:,} rows.  Deleted {deleted:,} stale rows.")

            conn.commit()
            print()
            print("=== Verification ===")
            _report_verification(cur, args.year)
    finally:
        release_conn(conn)


if __name__ == "__main__":
    main()
