#!/usr/bin/env python3
# scripts/ingest_zcta_polygons.py
#
# One-shot ingest of US Census TIGER 2025 ZCTA520 polygons (filtered to TX
# bbox overlap) into the data DB. Also runs idempotent schema migrations on
# the data DB: zcta_polygons table, tad_parcels.property_zip column,
# backfill_audit_rows ledger, and CONCURRENTLY-built indexes.
#
# Migrations live here (not in api/main.py:_ensure_session_schema) because
# parcel tables — tad_parcels, parcels, collin_parcels, denton_parcels —
# live in the data DB, not the sessions DB. Pattern matches
# scripts/build_tad_city_lookup.py.
#
# Companion to scripts/backfill_property_zip_from_zcta.py, which populates
# <county>_parcels.property_zip by spatial-joining against zcta_polygons.
#
# Connects to:
#   api/config.py       — shared data-DB connection helpers
#   PostgreSQL/PostGIS  — writes zcta_polygons + ALTERs tad_parcels
#
# Run:
#   .venv/bin/python3 scripts/ingest_zcta_polygons.py
#   (downloads tl_2025_us_zcta520.zip if not already present)

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

import shapefile

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn  # noqa: E402


TIGER_ZCTA_URL = "https://www2.census.gov/geo/tiger/TIGER2025/ZCTA520/tl_2025_us_zcta520.zip"
TIGER_ZCTA_DIR = ROOT_DIR / "ingest" / "tiger" / "zcta" / "tl_2025_us_zcta520"
TIGER_ZCTA_SHP = TIGER_ZCTA_DIR / "tl_2025_us_zcta520.shp"
SOURCE_LABEL = "tl_2025_us_zcta520"

# TX bbox (wide margin; the 4-county DFW footprint fits well inside).
TX_BBOX_MIN_LNG = -106.65
TX_BBOX_MIN_LAT = 25.84
TX_BBOX_MAX_LNG = -93.51
TX_BBOX_MAX_LAT = 36.50

BATCH_SIZE = 500


def _ensure_data_db_schema_txn(conn) -> None:
    """Idempotent transactional schema migration on the data DB."""
    with conn.cursor() as cur:
        # Bound any lock waits + statement runtime to avoid stalling shared Cloud SQL.
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '60s'")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS zcta_polygons (
                zcta5         VARCHAR(5) PRIMARY KEY CHECK (zcta5 ~ '^[0-9]{5}$'),
                state_cd      VARCHAR(2) NOT NULL DEFAULT 'TX',
                geom          GEOMETRY(MultiPolygon, 4326) NOT NULL,
                source        TEXT NOT NULL,
                ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

        # Additive nullable column on tad_parcels (metadata-only, sub-second).
        cur.execute(
            "ALTER TABLE tad_parcels ADD COLUMN IF NOT EXISTS property_zip VARCHAR(16)"
        )

        # Audit ledger for run-id-scoped rollback. Populated atomically by
        # the backfill script via UPDATE...RETURNING CTE pattern.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS backfill_audit_rows (
                run_id      UUID NOT NULL,
                county      TEXT NOT NULL,
                pk_col      TEXT NOT NULL,
                pk_value    TEXT NOT NULL,
                ran_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (run_id, county, pk_value)
            )
            """
        )
    conn.commit()


def _ensure_indexes_concurrent(conn) -> None:
    """Build new indexes CONCURRENTLY (non-blocking on shared Cloud SQL).

    CREATE INDEX CONCURRENTLY cannot run inside a transaction block, so flip
    the connection to autocommit for these statements only.
    """
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_zcta_polygons_geom "
                "ON zcta_polygons USING GIST (geom)"
            )
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tad_parcels_property_zip "
                "ON tad_parcels (property_zip)"
            )
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_backfill_audit_run_id "
                "ON backfill_audit_rows (run_id)"
            )
    finally:
        conn.autocommit = False


def _download_zcta_zip(target_dir: Path) -> None:
    """Download + extract the TIGER ZCTA shapefile if not already present."""
    target_dir.mkdir(parents=True, exist_ok=True)
    zip_path = target_dir.parent / "tl_2025_us_zcta520.zip"

    if TIGER_ZCTA_SHP.exists():
        print(f"[ingest_zcta] shapefile already present at {TIGER_ZCTA_SHP}")
        return

    print(f"[ingest_zcta] downloading {TIGER_ZCTA_URL} ...")
    urllib.request.urlretrieve(TIGER_ZCTA_URL, zip_path)
    print(f"[ingest_zcta] extracting to {target_dir} ...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target_dir)
    print("[ingest_zcta] download complete")


def _bbox_overlaps_tx(shape) -> bool:
    """Return True if the shape's bounding box overlaps the TX bbox."""
    # shapefile.Shape.bbox is (min_lng, min_lat, max_lng, max_lat)
    min_lng, min_lat, max_lng, max_lat = shape.bbox
    if max_lng < TX_BBOX_MIN_LNG or min_lng > TX_BBOX_MAX_LNG:
        return False
    if max_lat < TX_BBOX_MIN_LAT or min_lat > TX_BBOX_MAX_LAT:
        return False
    return True


def _read_tx_zctas(shp_path: Path) -> list[tuple[str, str]]:
    """Read TX-bbox-overlapping ZCTAs from the shapefile.

    Returns list of (zcta5, geom_geojson_str) tuples.
    """
    reader = shapefile.Reader(str(shp_path))
    fields = [f[0] for f in reader.fields[1:]]

    # TIGER 2025 ZCTA520: the 5-digit ZIP field is "ZCTA5CE20" (20 = 2020 census vintage).
    if "ZCTA5CE20" not in fields:
        raise ValueError(
            f"Expected ZCTA5CE20 field in TIGER shapefile. Got: {fields}"
        )
    zcta_idx = fields.index("ZCTA5CE20")

    rows: list[tuple[str, str]] = []
    total_scanned = 0
    for shape_rec in reader.iterShapeRecords():
        total_scanned += 1
        if not _bbox_overlaps_tx(shape_rec.shape):
            continue

        zcta5 = str(shape_rec.record[zcta_idx]).strip()
        if not zcta5 or not zcta5.isdigit() or len(zcta5) != 5:
            continue

        try:
            geom = shape_rec.shape.__geo_interface__
        except Exception:
            continue
        if not geom or geom.get("type") not in {"Polygon", "MultiPolygon"}:
            continue

        rows.append((zcta5, json.dumps(geom)))

        if total_scanned % 5000 == 0:
            print(f"[ingest_zcta] scanned {total_scanned} features, kept {len(rows)} TX-bbox rows so far")

    print(f"[ingest_zcta] total scanned: {total_scanned}; kept: {len(rows)}")
    return rows


def _upsert_zcta_rows(conn, rows: list[tuple[str, str]]) -> None:
    """Upsert ZCTA rows in batches of BATCH_SIZE."""
    upserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO zcta_polygons (zcta5, state_cd, geom, source, ingested_at)
                VALUES (%s, 'TX',
                        ST_Multi(ST_MakeValid(ST_GeomFromGeoJSON(%s))),
                        %s, now())
                ON CONFLICT (zcta5) DO UPDATE SET
                    geom = EXCLUDED.geom,
                    source = EXCLUDED.source,
                    ingested_at = EXCLUDED.ingested_at
                """,
                [(zcta5, geom_json, SOURCE_LABEL) for zcta5, geom_json in batch],
            )
        conn.commit()
        upserted += len(batch)
        print(f"[ingest_zcta] upserted batch {i // BATCH_SIZE + 1} (rows {upserted}/{len(rows)})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip the download step (assume shapefile already extracted).",
    )
    args = parser.parse_args()

    if not args.skip_download:
        _download_zcta_zip(TIGER_ZCTA_DIR)

    if not TIGER_ZCTA_SHP.exists():
        print(f"[ingest_zcta] ERROR: shapefile not found at {TIGER_ZCTA_SHP}", file=sys.stderr)
        return 1

    conn = get_conn()
    try:
        print("[ingest_zcta] running schema migrations ...")
        _ensure_data_db_schema_txn(conn)
        _ensure_indexes_concurrent(conn)
        print("[ingest_zcta] schema migrations complete")

        print(f"[ingest_zcta] reading TX ZCTAs from {TIGER_ZCTA_SHP} ...")
        rows = _read_tx_zctas(TIGER_ZCTA_SHP)
        if not rows:
            print("[ingest_zcta] WARNING: no rows kept. Aborting upsert.")
            return 1

        print(f"[ingest_zcta] upserting {len(rows)} rows ...")
        _upsert_zcta_rows(conn, rows)
        print(f"[ingest_zcta] DONE — {len(rows)} TX ZCTAs upserted.")
        return 0
    finally:
        release_conn(conn)


if __name__ == "__main__":
    sys.exit(main())
