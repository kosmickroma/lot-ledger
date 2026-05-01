# scripts/enrich_tad_standard_data.py
#
# Enriches existing tad_parcels rows using TAD Standard Data delimited text files.
# Reads Residential/Commercial/Personal PropertyData files (pipe-delimited),
# maps by Account_Num + Sequence_No to parcel_key, and updates owner/address/zip,
# legal, school, and valuation-related fields.
#
# Connects to:
#   ingest/counties/tarrant/tad/<snapshot>/unzipped/StandardData/**/PropertyData(Delimited)_*.txt
#   api/config.py  - shared DB connection helpers
#   PostgreSQL/PostGIS tad_parcels table

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Iterator

from psycopg2.extras import execute_values

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn

DEFAULT_STANDARD_DIR = Path(
    "ingest/counties/tarrant/tad/2026-05-01/unzipped/StandardData"
)
DEFAULT_FULL_SET_FILE = Path(
    "ingest/counties/tarrant/tad/2026-05-01/unzipped/PropertyData(Delimited)/PropertyData_2026.txt"
)
DEFAULT_FILES = [
    DEFAULT_STANDARD_DIR / "ResidentialPropertyData" / "PropertyData(Delimited)_R.txt",
    DEFAULT_STANDARD_DIR / "CommercialPropertyData" / "PropertyData(Delimited)_C.txt",
    DEFAULT_STANDARD_DIR / "PersonalPropertyData" / "PropertyData(Delimited)_P.txt",
]
BATCH_SIZE = 25000
PROGRESS_EVERY = 10  # print after every N batches


@dataclass
class Stats:
    files_seen: int = 0
    rows_seen: int = 0
    rows_with_key: int = 0
    rows_buffered: int = 0
    rows_applied: int = 0


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "none" else text


def _to_float(value: object) -> float | None:
    text = _clean_text(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: object) -> int | None:
    num = _to_float(value)
    if num is None:
        return None
    try:
        return int(num)
    except (TypeError, ValueError):
        return None


def _parcel_key(account_num: str, sequence_no: str) -> str:
    acct = _clean_text(account_num)
    seq = _clean_text(sequence_no)
    if not acct:
        return ""
    return f"{acct}:{seq or '000'}"


def _iter_rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", encoding="latin-1", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="|")
        for row in reader:
            yield row


def _ensure_enrichment_columns() -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS owner_zip VARCHAR(16)")
            cur.execute("ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS owner_zip4 VARCHAR(8)")
            cur.execute("ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS owner_citystate TEXT")
        conn.commit()
    finally:
        release_conn(conn)


def _apply_batch(batch: list[tuple]) -> int:
    if not batch:
        return 0

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                UPDATE tad_parcels AS t
                SET
                    owner_name = COALESCE(NULLIF(v.owner_name, ''), t.owner_name),
                    owner_addr = COALESCE(NULLIF(v.owner_addr, ''), t.owner_addr),
                    owner_city = COALESCE(NULLIF(v.owner_city, ''), t.owner_city),
                    owner_citystate = COALESCE(NULLIF(v.owner_citystate, ''), t.owner_citystate),
                    owner_zip = COALESCE(NULLIF(v.owner_zip, ''), t.owner_zip),
                    owner_zip4 = COALESCE(NULLIF(v.owner_zip4, ''), t.owner_zip4),
                    situs_addr = COALESCE(NULLIF(v.situs_addr, ''), t.situs_addr),
                    property_class = COALESCE(NULLIF(v.property_class, ''), t.property_class),
                    state_use_code = COALESCE(NULLIF(v.state_use_code, ''), t.state_use_code),
                    legal_descr = COALESCE(NULLIF(v.legal_descr, ''), t.legal_descr),
                    school_code = COALESCE(NULLIF(v.school_code, ''), t.school_code),
                    land_acres = COALESCE(v.land_acres, t.land_acres),
                    land_sqft = COALESCE(v.land_sqft, t.land_sqft),
                    ag_acres = COALESCE(v.ag_acres, t.ag_acres),
                    ag_value = COALESCE(v.ag_value, t.ag_value),
                    year_built = COALESCE(v.year_built, t.year_built),
                    living_area = COALESCE(v.living_area, t.living_area),
                    land_value = COALESCE(v.land_value, t.land_value),
                    improvement_value = COALESCE(v.improvement_value, t.improvement_value),
                    total_value = COALESCE(v.total_value, t.total_value),
                    appraised_value = COALESCE(v.appraised_value, t.appraised_value),
                    updated_at = NOW()
                FROM (VALUES %s) AS v(
                    parcel_key,
                    owner_name,
                    owner_addr,
                    owner_city,
                    owner_citystate,
                    owner_zip,
                    owner_zip4,
                    situs_addr,
                    property_class,
                    state_use_code,
                    legal_descr,
                    school_code,
                    land_acres,
                    land_sqft,
                    ag_acres,
                    ag_value,
                    year_built,
                    living_area,
                    land_value,
                    improvement_value,
                    total_value,
                    appraised_value
                )
                WHERE t.parcel_key = v.parcel_key
                """,
                batch,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            )
            applied = cur.rowcount
        conn.commit()
        return max(applied, 0)
    finally:
        release_conn(conn)


def run(files: list[Path], write_db: bool) -> Stats:
    stats = Stats()
    buffer: list[tuple] = []
    header_only_files: list[Path] = []

    for path in files:
        if not path.exists():
            print(f"[skip] missing file: {path}")
            continue

        stats.files_seen += 1
        print(f"[read] {path}")
        rows_before = stats.rows_seen

        for row in _iter_rows(path):
            stats.rows_seen += 1
            key = _parcel_key(row.get("Account_Num", ""), row.get("Sequence_No", ""))
            if not key:
                continue
            stats.rows_with_key += 1

            citystate = _clean_text(row.get("Owner_CityState", ""))
            owner_city = citystate.split(",", 1)[0].strip() if citystate else ""

            buffer.append(
                (
                    key,
                    _clean_text(row.get("Owner_Name", "")),
                    _clean_text(row.get("Owner_Address", "")),
                    owner_city,
                    citystate,
                    _clean_text(row.get("Owner_Zip", "")),
                    _clean_text(row.get("Owner_Zip4", "")),
                    _clean_text(row.get("Situs_Address", "")),
                    _clean_text(row.get("Property_Class", "")),
                    _clean_text(row.get("State_Use_Code", "")),
                    _clean_text(row.get("LegalDescription", "")),
                    _clean_text(row.get("School", "")),
                    _to_float(row.get("Land_Acres", "")),
                    _to_float(row.get("Land_SqFt", "")),
                    _to_float(row.get("Ag_Acres", "")),
                    _to_float(row.get("Ag_Value", "")),
                    _to_int(row.get("Year_Built", "")),
                    _to_float(row.get("Living_Area", "")),
                    _to_float(row.get("Land_Value", "")),
                    _to_float(row.get("Improvement_Value", "")),
                    _to_float(row.get("Total_Value", "")),
                    _to_float(row.get("Appraised_Value", "")),
                )
            )
            stats.rows_buffered += 1

            if write_db and len(buffer) >= BATCH_SIZE:
                stats.rows_applied += _apply_batch(buffer)
                buffer.clear()
                batch_num = stats.rows_applied // BATCH_SIZE
                if batch_num % PROGRESS_EVERY == 0:
                    pct = 100 * stats.rows_seen / 1_998_461
                    print(f"  [progress] {stats.rows_applied:,} rows updated ({pct:.1f}% read)")

        if stats.rows_seen == rows_before:
            header_only_files.append(path)
            print(f"[warn] no data rows detected in {path} (header-only or wrong export)")

    if write_db and buffer:
        stats.rows_applied += _apply_batch(buffer)

    if header_only_files:
        print("\n[warn] One or more Standard Data files contained no records:")
        for f in header_only_files:
            print(f"  - {f}")
        print("[hint] Re-download the full data export. These appear to be schema/header-only files.")

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich tad_parcels from TAD Standard Data files")
    parser.add_argument(
        "--standard-dir",
        type=Path,
        default=DEFAULT_STANDARD_DIR,
        help="Directory containing StandardData subfolders",
    )
    parser.add_argument(
        "--write-db",
        action="store_true",
        help="Apply updates to DB (default is dry-run summary)",
    )
    parser.add_argument(
        "--full-set-file",
        type=Path,
        default=DEFAULT_FULL_SET_FILE,
        help="Single full-set delimited file path (preferred when present)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.full_set_file.exists():
        files = [args.full_set_file]
        print(f"[mode] using full-set file: {args.full_set_file}")
    else:
        files = [
            args.standard_dir / "ResidentialPropertyData" / "PropertyData(Delimited)_R.txt",
            args.standard_dir / "CommercialPropertyData" / "PropertyData(Delimited)_C.txt",
            args.standard_dir / "PersonalPropertyData" / "PropertyData(Delimited)_P.txt",
        ]
        print(f"[mode] using split files under: {args.standard_dir}")

    if args.write_db:
        _ensure_enrichment_columns()

    stats = run(files, write_db=args.write_db)

    print("\nTAD standard-data enrichment summary")
    print(f"  Files seen: {stats.files_seen}")
    print(f"  Rows read: {stats.rows_seen}")
    print(f"  Rows with parcel key: {stats.rows_with_key}")
    print(f"  Rows buffered: {stats.rows_buffered}")
    if args.write_db:
        print(f"  Rows applied (UPDATE matches): {stats.rows_applied}")
    else:
        print("  Dry run only. Re-run with --write-db to apply updates.")


if __name__ == "__main__":
    main()
