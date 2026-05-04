# scripts/build_denton.py
#
# Denton (DCAD) parcel loader with dry-run validation and optional DB write.
# Streams a large GeoJSON FeatureCollection (ijson) and upserts rows into
# denton_parcels without loading the entire file into memory.
#
# Connects to:
#   api/config.py                                           - shared DB connection helpers
#   ingest/counties/denton/cad/current/unzipped/Parcels_FC.geojson - Denton parcel source
#   PostgreSQL/PostGIS                                      - writes denton_parcels table (Phase N)

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
from typing import Any, Iterable

from psycopg2.extras import execute_values

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn

try:
    import ijson  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise ImportError("ijson is required for streaming Denton GeoJSON. Install with: pip install ijson") from exc


DEFAULT_GEOJSON = Path("ingest/counties/denton/cad/current/unzipped/Parcels_FC.geojson")
BATCH_SIZE = 500


@dataclass
class Stats:
    total_seen: int = 0
    kept: int = 0
    skipped_missing_id: int = 0
    skipped_missing_geometry: int = 0
    deduped_within_batch: int = 0


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "none" else text


def _to_float(value: Any) -> float | None:
    text = _clean_text(value).replace(",", "").replace("$", "")
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


def _to_iso_date(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None

    # Handles common patterns we see in county exports.
    for sep in ("-", "/"):
        parts = text.split(sep)
        if len(parts) == 3:
            if len(parts[0]) == 4:
                y, m, d = parts[0], parts[1], parts[2]
            else:
                m, d, y = parts[0], parts[1], parts[2]
            try:
                return date(int(y), int(m), int(d)).isoformat()
            except ValueError:
                pass

    try:
        # Last-resort: trim timestamp payload if present.
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _pick(props: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in props and props[key] not in (None, ""):
            return props[key]
    return None


def _compose_address(props: dict[str, Any]) -> str:
    full = _clean_text(_pick(props, "situs_full_address", "siteAddress"))
    if full:
        return full
    street = " ".join(
        p
        for p in [
            _clean_text(_pick(props, "situsStreetNumb", "siteNumber")),
            _clean_text(_pick(props, "situsStreetPrefix", "sitePrefix")),
            _clean_text(_pick(props, "situsStreetName", "siteStreet")),
            _clean_text(_pick(props, "situsStreetSuff", "siteSuffix")),
        ]
        if p
    ).strip()
    if street:
        return street
    return _clean_text(_pick(props, "situs_street_address"))


def _stream_features(geojson_path: Path) -> Iterable[dict[str, Any]]:
    with geojson_path.open("rb") as handle:
        yield from ijson.items(handle, "features.item")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _derive_row(
    record_map: dict[str, Any],
    geometry_geojson: str,
    snapshot_date: date,
) -> tuple[Any, ...] | None:
    account_num = _clean_text(_pick(record_map, "propId", "prop_id", "pid"))
    if not account_num:
        return None

    parcel_key = account_num
    owner_name = _clean_text(_pick(record_map, "name", "ownerName"))
    owner_address = _clean_text(_pick(record_map, "addrDeliveryLine", "mailingAddress"))
    owner_city = _clean_text(_pick(record_map, "addrCity", "mailingCity"))
    owner_state = _clean_text(_pick(record_map, "addrState", "mailingState"))
    owner_zip = _clean_text(_pick(record_map, "addrZip", "mailingZip"))

    property_address = _compose_address(record_map)
    property_city = _clean_text(_pick(record_map, "situsCity", "siteCity"))
    property_zip = _clean_text(_pick(record_map, "situsZip", "siteZip"))

    state_cd = _clean_text(_pick(record_map, "stateCodes", "state_cd"))
    exemptions = _clean_text(_pick(record_map, "exemptions"))

    land_value = _to_float(_pick(record_map, "landValue", "landHSValue"))
    improvement_value = _to_float(_pick(record_map, "improvementValue"))
    total_value = _to_float(_pick(record_map, "ownerAppraisedValue", "totalValue"))

    land_sqft = _to_float(_pick(record_map, "land_sqft", "landTotalSqft"))
    land_total_sqft = _to_float(_pick(record_map, "landTotalSqft"))
    land_acres = _to_float(_pick(record_map, "landAcres", "effectiveSizeAcres", "legalAcreage"))

    living_area = _to_float(_pick(record_map, "imprvMainArea", "livingArea"))
    year_built = _to_int(_pick(record_map, "imprvActualYearBuilt", "yearBuilt"))

    isd_desc = _clean_text(_pick(record_map, "schoolTaxingUnitName"))
    entity_codes = _clean_text(_pick(record_map, "taxingUnits"))

    deed_number = _clean_text(_pick(record_map, "deedID", "instrumentNum"))
    deed_date = _to_iso_date(_pick(record_map, "deedDt", "deedDate"))
    legal_descr = _clean_text(_pick(record_map, "legalDescription"))
    subdivision = _clean_text(_pick(record_map, "abstractSubdivisionDescription"))
    zoning = _clean_text(_pick(record_map, "cad_zoning", "zoning"))
    geo_id = _clean_text(_pick(record_map, "geoID", "geo_id"))

    # area_estimated is ONLY for area derived from geometry (ST_Area) fallback.
    # We mark true when neither explicit land_sqft nor land_acres is present.
    area_estimated = not (
        (land_sqft is not None and land_sqft > 0)
        or (land_acres is not None and land_acres > 0)
    )

    return (
        parcel_key,
        account_num,
        geo_id or None,
        owner_name or None,
        owner_address or None,
        owner_city or None,
        owner_state or None,
        owner_zip or None,
        property_address or None,
        property_city or None,
        property_zip or None,
        state_cd or None,
        exemptions or None,
        land_value,
        improvement_value,
        total_value,
        land_sqft,
        land_total_sqft,
        land_acres,
        living_area,
        year_built,
        isd_desc or None,
        entity_codes or None,
        deed_number or None,
        deed_date,
        legal_descr or None,
        subdivision or None,
        zoning or None,
        area_estimated,
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
                CREATE TABLE IF NOT EXISTS denton_parcels (
                    parcel_key VARCHAR(64) PRIMARY KEY,
                    account_num TEXT,
                    geo_id TEXT,
                    owner_name TEXT,
                    owner_address TEXT,
                    owner_city TEXT,
                    owner_state TEXT,
                    owner_zip TEXT,
                    property_address TEXT,
                    property_city TEXT,
                    property_zip TEXT,
                    state_cd TEXT,
                    exemptions TEXT,
                    land_value DOUBLE PRECISION,
                    improvement_value DOUBLE PRECISION,
                    total_value DOUBLE PRECISION,
                    land_sqft DOUBLE PRECISION,
                    land_total_sqft DOUBLE PRECISION,
                    land_acres DOUBLE PRECISION,
                    living_area DOUBLE PRECISION,
                    year_built INTEGER,
                    isd_desc TEXT,
                    entity_codes TEXT,
                    deed_number TEXT,
                    deed_date DATE,
                    legal_descr TEXT,
                    subdivision TEXT,
                    zoning TEXT,
                    area_estimated BOOLEAN NOT NULL DEFAULT FALSE,
                    geom geometry(Geometry, 4326),
                    centroid geometry(Point, 4326),
                    source_snapshot DATE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_denton_parcels_account_num ON denton_parcels (account_num)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_denton_parcels_state_cd ON denton_parcels (state_cd)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_denton_parcels_geom ON denton_parcels USING GIST (geom)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_denton_parcels_centroid ON denton_parcels USING GIST (centroid)")
        conn.commit()
    finally:
        release_conn(conn)


def _upsert_batch(batch: list[tuple[Any, ...]]) -> None:
    if not batch:
        return

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO denton_parcels (
                    parcel_key, account_num, geo_id,
                    owner_name, owner_address, owner_city, owner_state, owner_zip,
                    property_address, property_city, property_zip,
                    state_cd, exemptions,
                    land_value, improvement_value, total_value,
                    land_sqft, land_total_sqft, land_acres,
                    living_area, year_built,
                    isd_desc, entity_codes,
                    deed_number, deed_date,
                    legal_descr, subdivision, zoning,
                    area_estimated,
                    geom, centroid, source_snapshot
                ) VALUES %s
                ON CONFLICT (parcel_key) DO UPDATE SET
                    account_num = EXCLUDED.account_num,
                    geo_id = EXCLUDED.geo_id,
                    owner_name = EXCLUDED.owner_name,
                    owner_address = EXCLUDED.owner_address,
                    owner_city = EXCLUDED.owner_city,
                    owner_state = EXCLUDED.owner_state,
                    owner_zip = EXCLUDED.owner_zip,
                    property_address = EXCLUDED.property_address,
                    property_city = EXCLUDED.property_city,
                    property_zip = EXCLUDED.property_zip,
                    state_cd = EXCLUDED.state_cd,
                    exemptions = EXCLUDED.exemptions,
                    land_value = EXCLUDED.land_value,
                    improvement_value = EXCLUDED.improvement_value,
                    total_value = EXCLUDED.total_value,
                    land_sqft = EXCLUDED.land_sqft,
                    land_total_sqft = EXCLUDED.land_total_sqft,
                    land_acres = EXCLUDED.land_acres,
                    living_area = EXCLUDED.living_area,
                    year_built = EXCLUDED.year_built,
                    isd_desc = EXCLUDED.isd_desc,
                    entity_codes = EXCLUDED.entity_codes,
                    deed_number = EXCLUDED.deed_number,
                    deed_date = EXCLUDED.deed_date,
                    legal_descr = EXCLUDED.legal_descr,
                    subdivision = EXCLUDED.subdivision,
                    zoning = EXCLUDED.zoning,
                    area_estimated = EXCLUDED.area_estimated,
                    geom = EXCLUDED.geom,
                    centroid = EXCLUDED.centroid,
                    source_snapshot = EXCLUDED.source_snapshot,
                    updated_at = NOW()
                """,
                batch,
                template=(
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)),"
                    "ST_PointOnSurface(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))),"
                    "%s)"
                ),
                page_size=500,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


def _dedupe_batch(batch: list[tuple[Any, ...]], stats: Stats) -> list[tuple[Any, ...]]:
    if not batch:
        return batch
    keyed: dict[str, tuple[Any, ...]] = {}
    for row in batch:
        keyed[str(row[0])] = row
    removed = len(batch) - len(keyed)
    if removed > 0:
        stats.deduped_within_batch += removed
    return list(keyed.values())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build denton_parcels from streamed Denton GeoJSON.")
    parser.add_argument("--geojson-path", default=str(DEFAULT_GEOJSON), help="Path to Parcels_FC.geojson")
    parser.add_argument("--snapshot-date", default="2026-05-04", help="Snapshot date label (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=0, help="Optional max features to process")
    parser.add_argument("--write-db", action="store_true", help="Write to Postgres table denton_parcels")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    geojson_path = Path(args.geojson_path)
    if not geojson_path.exists():
        raise FileNotFoundError(f"GeoJSON not found: {geojson_path}")

    snapshot = date.fromisoformat(args.snapshot_date)
    stats = Stats()
    batch: list[tuple[Any, ...]] = []

    print("Denton build: starting")
    print(f"Source: {geojson_path}")
    print(f"Mode: {'WRITE_DB' if args.write_db else 'DRY_RUN'}")
    if args.limit > 0:
        print(f"Limit: {args.limit:,}")

    if args.write_db:
        _ensure_table()

    for feature in _stream_features(geojson_path):
        if args.limit > 0 and stats.total_seen >= args.limit:
            break

        stats.total_seen += 1
        props = feature.get("properties") if isinstance(feature, dict) else None
        geom = feature.get("geometry") if isinstance(feature, dict) else None

        if not isinstance(props, dict):
            stats.skipped_missing_id += 1
            continue

        if not isinstance(geom, dict) or not geom.get("type"):
            stats.skipped_missing_geometry += 1
            continue

        row = _derive_row(props, json.dumps(geom, default=_json_default), snapshot)
        if row is None:
            stats.skipped_missing_id += 1
            continue

        stats.kept += 1

        if args.write_db:
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                _upsert_batch(_dedupe_batch(batch, stats))
                batch.clear()
                if stats.kept % (BATCH_SIZE * 10) == 0:
                    print(f"Upserted {stats.kept:,} rows...")
        elif stats.kept % 50000 == 0:
            print(f"Scanned {stats.kept:,} valid rows...")

    if args.write_db and batch:
        _upsert_batch(_dedupe_batch(batch, stats))

    print("---")
    print("Denton build complete")
    print(f"Rows seen:            {stats.total_seen:,}")
    print(f"Rows kept:            {stats.kept:,}")
    print(f"Skipped missing id:   {stats.skipped_missing_id:,}")
    print(f"Skipped empty geom:   {stats.skipped_missing_geometry:,}")
    print(f"Deduped in-batch:     {stats.deduped_within_batch:,}")


if __name__ == "__main__":
    main()
