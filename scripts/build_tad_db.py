# scripts/build_tad_db.py
#
# Tarrant (TAD) parcel loader with local dry-run validation and optional DB write.
# Reads normalized WGS84 ParcelView shapefile and upserts into a dedicated table.
#
# Connects to:
#   api/config.py                                            - shared DB connection helpers
#   ingest/counties/tarrant/tad/<snapshot>/normalized/...   - ParcelView_4326 source shapefile
#   PostgreSQL/PostGIS                                       - writes tad_parcels table (Phase N)

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import sys
from typing import Any

import shapefile
from psycopg2.extras import execute_values

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn


DEFAULT_SOURCE = Path("ingest/counties/tarrant/tad/2026-05-01/normalized/ParcelView_4326/ParcelView.shp")
BATCH_SIZE = 5000
DEFAULT_CHECKPOINT = Path(".build_checkpoint.json")


@dataclass
class Stats:
    total_seen: int = 0
    kept: int = 0
    skipped_empty_geom: int = 0
    account_with_leading_zero: int = 0
    deduped_within_batch: int = 0


def _checkpoint_key(source_shp: Path, snapshot_date: str) -> str:
    return f"build_tad_db::{source_shp.resolve()}::{snapshot_date}"


def _load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "none" else text


def _to_float(value: Any) -> float | None:
    text = _clean_text(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def _shape_geojson(shape_obj: shapefile.Shape) -> str | None:
    try:
        geom = shape_obj.__geo_interface__
    except Exception:
        return None

    if not isinstance(geom, dict) or not geom.get("type") or not geom.get("coordinates"):
        return None

    return json.dumps(geom)


def _extract_row(fields: list[str], shape_record: shapefile.ShapeRecord, snapshot_date: date, stats: Stats) -> tuple | None:
    stats.total_seen += 1

    record_map = {fields[i]: shape_record.record[i] for i in range(len(fields))}

    account_num = _clean_text(record_map.get("Account_Nu"))
    sequence_num = _clean_text(record_map.get("Sequence_N"))
    taxpin = _clean_text(record_map.get("TAXPIN"))
    object_id = _clean_text(record_map.get("OBJECTID_1"))

    if account_num.startswith("0"):
        stats.account_with_leading_zero += 1

    if account_num:
        parcel_key = f"{account_num}:{sequence_num or '000'}"
    elif taxpin:
        parcel_key = f"TAXPIN:{taxpin}"
    else:
        parcel_key = f"OBJECTID:{object_id or stats.total_seen}"
    geometry_geojson = _shape_geojson(shape_record.shape)
    if not geometry_geojson:
        stats.skipped_empty_geom += 1
        return None

    stats.kept += 1

    return (
        parcel_key,
        account_num or None,
        sequence_num or None,
        taxpin or None,
        _clean_text(record_map.get("PIDN")) or None,
        _clean_text(record_map.get("Owner_Name")) or None,
        _clean_text(record_map.get("Owner_Addr")) or None,
        _clean_text(record_map.get("Owner_City")) or None,
        _clean_text(record_map.get("Situs_Addr")) or None,
        _clean_text(record_map.get("Property_C")) or None,
        _clean_text(record_map.get("State_Use_")) or None,
        _clean_text(record_map.get("LegalDescr")) or None,
        _clean_text(record_map.get("County")) or None,
        _clean_text(record_map.get("City")) or None,
        _clean_text(record_map.get("School")) or None,
        _to_float(record_map.get("ACRES")),
        _to_float(record_map.get("Land_Acres")),
        _to_float(record_map.get("Land_SqFt")),
        _to_float(record_map.get("Ag_Acres")),
        _to_float(record_map.get("Ag_Value")),
        _to_int(record_map.get("Year_Built")),
        _to_float(record_map.get("Living_Are")),
        _to_float(record_map.get("Land_Value")),
        _to_float(record_map.get("Improvemen")),
        _to_float(record_map.get("Total_Valu")),
        _to_float(record_map.get("Appraised_")),
        geometry_geojson,
        geometry_geojson,
        snapshot_date,
    )


def _ensure_table() -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tad_parcels (
                    parcel_key VARCHAR(64) PRIMARY KEY,
                    account_num VARCHAR(32),
                    sequence_num VARCHAR(8),
                    taxpin VARCHAR(64),
                    pidn VARCHAR(64),
                    owner_name TEXT,
                    owner_addr TEXT,
                    owner_city TEXT,
                    situs_addr TEXT,
                    property_class VARCHAR(16),
                    state_use_code VARCHAR(16),
                    legal_descr TEXT,
                    county_code VARCHAR(8),
                    city_code VARCHAR(8),
                    school_code VARCHAR(8),
                    acres DOUBLE PRECISION,
                    land_acres DOUBLE PRECISION,
                    land_sqft DOUBLE PRECISION,
                    ag_acres DOUBLE PRECISION,
                    ag_value DOUBLE PRECISION,
                    year_built INTEGER,
                    living_area DOUBLE PRECISION,
                    land_value DOUBLE PRECISION,
                    improvement_value DOUBLE PRECISION,
                    total_value DOUBLE PRECISION,
                    appraised_value DOUBLE PRECISION,
                    geom geometry(Geometry, 4326),
                    centroid geometry(Point, 4326),
                    source_snapshot DATE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tad_parcels_account_num ON tad_parcels (account_num)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tad_parcels_geom ON tad_parcels USING GIST (geom)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tad_parcels_centroid ON tad_parcels USING GIST (centroid)")
        conn.commit()
    finally:
        release_conn(conn)


def _upsert_batch(batch: list[tuple]) -> None:
    if not batch:
        return

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO tad_parcels (
                    parcel_key, account_num, sequence_num, taxpin, pidn,
                    owner_name, owner_addr, owner_city, situs_addr,
                    property_class, state_use_code, legal_descr,
                    county_code, city_code, school_code,
                    acres, land_acres, land_sqft, ag_acres, ag_value,
                    year_built, living_area, land_value, improvement_value,
                    total_value, appraised_value,
                    geom, centroid, source_snapshot
                ) VALUES %s
                ON CONFLICT (parcel_key) DO UPDATE SET
                    account_num = EXCLUDED.account_num,
                    sequence_num = EXCLUDED.sequence_num,
                    taxpin = EXCLUDED.taxpin,
                    pidn = EXCLUDED.pidn,
                    owner_name = EXCLUDED.owner_name,
                    owner_addr = EXCLUDED.owner_addr,
                    owner_city = EXCLUDED.owner_city,
                    situs_addr = EXCLUDED.situs_addr,
                    property_class = EXCLUDED.property_class,
                    state_use_code = EXCLUDED.state_use_code,
                    legal_descr = EXCLUDED.legal_descr,
                    county_code = EXCLUDED.county_code,
                    city_code = EXCLUDED.city_code,
                    school_code = EXCLUDED.school_code,
                    acres = EXCLUDED.acres,
                    land_acres = EXCLUDED.land_acres,
                    land_sqft = EXCLUDED.land_sqft,
                    ag_acres = EXCLUDED.ag_acres,
                    ag_value = EXCLUDED.ag_value,
                    year_built = EXCLUDED.year_built,
                    living_area = EXCLUDED.living_area,
                    land_value = EXCLUDED.land_value,
                    improvement_value = EXCLUDED.improvement_value,
                    total_value = EXCLUDED.total_value,
                    appraised_value = EXCLUDED.appraised_value,
                    geom = EXCLUDED.geom,
                    centroid = EXCLUDED.centroid,
                    source_snapshot = EXCLUDED.source_snapshot,
                    updated_at = NOW()
                """,
                batch,
                template=(
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),"
                    "ST_PointOnSurface(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)),"
                    "%s)"
                ),
                page_size=1000,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


def _dedupe_batch(batch: list[tuple], stats: Stats) -> list[tuple]:
    if not batch:
        return batch

    # Last row wins when the same parcel_key appears multiple times in one SQL statement.
    keyed: dict[str, tuple] = {}
    for row in batch:
        keyed[row[0]] = row

    removed = len(batch) - len(keyed)
    if removed > 0:
        stats.deduped_within_batch += removed

    return list(keyed.values())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build TAD parcel table from normalized ParcelView shapefile.")
    parser.add_argument("--source-shp", default=str(DEFAULT_SOURCE), help="Path to normalized ParcelView.shp")
    parser.add_argument("--snapshot-date", default="2026-05-01", help="Snapshot date label (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=0, help="Optional max features to process for local tests")
    parser.add_argument(
        "--write-db",
        action="store_true",
        help="Write to Postgres table tad_parcels. Default mode is dry-run only.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last committed checkpoint for this source/snapshot pair.",
    )
    parser.add_argument(
        "--fresh-start",
        action="store_true",
        help="Ignore existing checkpoint and start from record 1.",
    )
    parser.add_argument(
        "--checkpoint-file",
        default=str(DEFAULT_CHECKPOINT),
        help="Path to checkpoint json file.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_shp = Path(args.source_shp)
    if not source_shp.exists():
        raise FileNotFoundError(f"Source shapefile not found: {source_shp}")

    snapshot = date.fromisoformat(args.snapshot_date)
    checkpoint_file = Path(args.checkpoint_file)
    checkpoint_key = _checkpoint_key(source_shp, args.snapshot_date)

    print("TAD build: starting")
    print(f"Source: {source_shp}")
    print(f"Mode: {'WRITE_DB' if args.write_db else 'DRY_RUN'}")
    if args.limit > 0:
        print(f"Limit: {args.limit:,} features")

    reader = shapefile.Reader(str(source_shp), encoding="latin-1")
    total_records = len(reader)
    fields = [field[0] for field in reader.fields[1:]]

    required_fields = ["Account_Nu", "Sequence_N", "TAXPIN", "Owner_Name", "Situs_Addr", "Total_Valu"]
    missing = [f for f in required_fields if f not in fields]
    if missing:
        raise ValueError(f"Missing expected ParcelView fields: {', '.join(missing)}")

    if args.write_db:
        _ensure_table()

    checkpoint_doc = _load_checkpoint(checkpoint_file)
    start_index = 1
    if args.write_db and args.fresh_start:
        checkpoint_doc.pop(checkpoint_key, None)
        _save_checkpoint(checkpoint_file, checkpoint_doc)

    if args.write_db and args.resume:
        start_index = int(checkpoint_doc.get(checkpoint_key, {}).get("next_record_index", 1))
        if start_index < 1:
            start_index = 1
        if start_index > total_records:
            start_index = total_records + 1
        print(f"Resume: starting at record {start_index:,} of {total_records:,}")

    stats = Stats()
    batch: list[tuple] = []
    last_idx = 0
    processed_this_run = 0

    for idx in range(start_index, total_records + 1):
        if args.limit > 0 and processed_this_run >= args.limit:
            break
        shape_record = reader.shapeRecord(idx - 1)
        last_idx = idx
        processed_this_run += 1

        row = _extract_row(fields, shape_record, snapshot, stats)
        if row is None:
            continue

        if args.write_db:
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                write_batch = _dedupe_batch(batch, stats)
                _upsert_batch(write_batch)
                print(f"Upserted {stats.kept:,} rows...")
                checkpoint_doc[checkpoint_key] = {
                    "next_record_index": idx + 1,
                    "source_shp": str(source_shp),
                    "snapshot_date": args.snapshot_date,
                    "updated_by": "build_tad_db.py",
                }
                _save_checkpoint(checkpoint_file, checkpoint_doc)
                batch.clear()

        if not args.write_db and stats.kept % 100000 == 0:
            print(f"Scanned {stats.kept:,} valid rows...")

    if args.write_db and batch:
        write_batch = _dedupe_batch(batch, stats)
        _upsert_batch(write_batch)
        checkpoint_doc[checkpoint_key] = {
            "next_record_index": last_idx + 1,
            "source_shp": str(source_shp),
            "snapshot_date": args.snapshot_date,
            "updated_by": "build_tad_db.py",
        }
        _save_checkpoint(checkpoint_file, checkpoint_doc)

    if args.write_db and last_idx > 0:
        state = checkpoint_doc.get(checkpoint_key, {})
        state["completed"] = last_idx >= total_records
        state["last_record_index"] = last_idx
        checkpoint_doc[checkpoint_key] = state
        _save_checkpoint(checkpoint_file, checkpoint_doc)

    print("---")
    print("TAD build complete")
    print(f"Rows seen: {stats.total_seen:,}")
    print(f"Rows kept: {stats.kept:,}")
    print(f"Rows skipped (empty geometry): {stats.skipped_empty_geom:,}")
    print(f"Rows with leading-zero account ids: {stats.account_with_leading_zero:,}")
    print(f"Rows deduped within write batches: {stats.deduped_within_batch:,}")


if __name__ == "__main__":
    main()
