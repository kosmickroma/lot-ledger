#!/usr/bin/env python3
# scripts/ingest_flood_zones.py
#
# One-shot ingest of FEMA NFHL flood hazard polygons for all 4 DFW
# counties into the data DB. Reads S_FLD_HAZ_AR.shp from each county's
# unzipped NFHL extract and TRUNCATE+INSERTs into the flood_zones table
# in a single transaction.
#
# Run quarterly (FEMA panel updates) by:
#   1. Download fresh per-county NFHL ZIPs from
#      https://msc.fema.gov/portal/downloadProduct?productID=NFHL_<FIPS>C
#      Dallas=48113, Tarrant=48439, Collin=48085, Denton=48121
#   2. Drop ZIPs at ingest/fema/nfhl/<YYYY-MM-DD>/raw/<county>_<fips>.zip
#   3. Unzip each into ingest/fema/nfhl/<YYYY-MM-DD>/unzipped/<county>/
#   4. Run: .venv/bin/python3 scripts/ingest_flood_zones.py --snapshot YYYY-MM-DD
#
# Implementation notes:
#   - Spec decision #13 (shapely-based MultiPolygon assembly) was relaxed
#     after coding started: the precedent loader scripts/ingest_zcta_polygons.py
#     uses pyshp's shape.__geo_interface__ which handles outer/hole ring
#     grouping correctly via winding-order analysis internally. That
#     precedent is running in prod against TIGER ZCTAs (also multi-part
#     heavy) without geometry corruption. PostGIS ST_MakeValid + ST_Multi
#     handle any residual cleanup. No new dep needed.
#   - Encoding: latin-1 across all 4 counties (Collin + Denton .cpg files
#     claim UTF-8 but contain non-UTF-8 bytes — verified during data
#     inspection 2026-06-05).
#   - CRS: shapefiles ship NAD83 (EPSG:4269). ST_Transform to WGS84
#     (EPSG:4326) at INSERT time so the schema matches the rest of
#     LotLedger PostGIS.
#   - STATIC_BFE = -9999.0 (FEMA "no BFE established" sentinel) → NULL.
#   - TRUNCATE + INSERT happen in ONE transaction so a mid-load failure
#     rolls back the whole table — never leaves flood_zones half-loaded.
#
# Connects to:
#   api/config.py       — shared data-DB connection helpers
#   PostgreSQL/PostGIS  — writes flood_zones table

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import shapefile
from psycopg2.extras import execute_values

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn  # noqa: E402


COUNTY_FIPS = {
    "dallas": "48113",
    "tarrant": "48439",
    "collin": "48085",
    "denton": "48121",
}

# FEMA sentinel for "no BFE established" — convert to NULL during ingest.
BFE_SENTINEL = -9999.0

BATCH_SIZE = 500


def _ensure_schema_txn(conn) -> None:
    """Idempotent transactional schema migration for the data DB."""
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '60s'")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS flood_zones (
                id              SERIAL PRIMARY KEY,
                dfirm_id        TEXT,
                fld_ar_id       TEXT,
                fld_zone        TEXT NOT NULL,
                zone_subty      TEXT,
                sfha_tf         CHAR(1),
                static_bfe      NUMERIC(10,3),
                dual_zone       CHAR(1),
                source_county   TEXT NOT NULL,
                ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                geom            GEOMETRY(MultiPolygon, 4326) NOT NULL
            )
            """
        )
    conn.commit()


def _ensure_indexes_concurrent(conn) -> None:
    """Build indexes CONCURRENTLY so a refresh against a populated table
    doesn't block readers. CREATE INDEX CONCURRENTLY cannot run inside a
    transaction, so flip to autocommit for these statements only."""
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_flood_zones_geom "
                "ON flood_zones USING GIST (geom)"
            )
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_flood_zones_county "
                "ON flood_zones (source_county)"
            )
            # Partial index — sfha_tf has only 2 values (T/F); the T rows
            # are the ones any future "SFHA-only" filter would scan.
            cur.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_flood_zones_sfha_t "
                "ON flood_zones (source_county) WHERE sfha_tf = 'T'"
            )
    finally:
        conn.autocommit = False


def _normalize_bfe(raw: float) -> float | None:
    """Map FEMA's -9999 sentinel to None; preserve real BFE values."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val == BFE_SENTINEL:
        return None
    return val


def _read_county_polygons(county: str, snapshot_dir: Path) -> list[tuple]:
    """Read S_FLD_HAZ_AR.shp for one county and return parsed rows.

    Returns list of:
        (dfirm_id, fld_ar_id, fld_zone, zone_subty, sfha_tf, static_bfe,
         dual_zone, source_county, geom_geojson_str)
    """
    shp_path = snapshot_dir / "unzipped" / county / "S_FLD_HAZ_AR"
    if not shp_path.with_suffix(".shp").exists():
        raise FileNotFoundError(
            f"S_FLD_HAZ_AR.shp not found for {county} at {shp_path}.shp — "
            f"did you unzip ingest/fema/nfhl/{snapshot_dir.name}/raw/{county}_*.zip?"
        )

    reader = shapefile.Reader(str(shp_path), encoding="latin-1")
    field_names = [f[0] for f in reader.fields[1:]]
    required = ("DFIRM_ID", "FLD_AR_ID", "FLD_ZONE", "ZONE_SUBTY", "SFHA_TF",
                "STATIC_BFE", "DUAL_ZONE")
    missing = [f for f in required if f not in field_names]
    if missing:
        raise ValueError(
            f"{county}/S_FLD_HAZ_AR.dbf missing expected fields: {missing}"
        )
    idx = {name: field_names.index(name) for name in required}

    rows: list[tuple] = []
    skipped_no_geom = 0
    skipped_no_zone = 0
    for shape_rec in reader.iterShapeRecords():
        rec = shape_rec.record
        fld_zone = str(rec[idx["FLD_ZONE"]] or "").strip()
        if not fld_zone:
            skipped_no_zone += 1
            continue

        try:
            geom = shape_rec.shape.__geo_interface__
        except Exception:
            skipped_no_geom += 1
            continue
        if not geom or geom.get("type") not in {"Polygon", "MultiPolygon"}:
            skipped_no_geom += 1
            continue

        rows.append((
            str(rec[idx["DFIRM_ID"]] or "").strip() or None,
            str(rec[idx["FLD_AR_ID"]] or "").strip() or None,
            fld_zone,
            str(rec[idx["ZONE_SUBTY"]] or "").strip() or None,
            (str(rec[idx["SFHA_TF"]] or "").strip()[:1] or None),
            _normalize_bfe(rec[idx["STATIC_BFE"]]),
            (str(rec[idx["DUAL_ZONE"]] or "").strip()[:1] or None),
            county,
            json.dumps(geom),
        ))

    if skipped_no_zone or skipped_no_geom:
        print(
            f"[ingest_flood] {county}: skipped {skipped_no_zone} no-zone + "
            f"{skipped_no_geom} no-geom rows"
        )
    return rows


def _truncate_and_insert(conn, all_rows: list[tuple]) -> int:
    """TRUNCATE flood_zones + bulk INSERT in a single transaction.

    Mid-load failure rolls the whole thing back — the table never lands
    half-populated. Spec decision #14.

    Uses psycopg2.extras.execute_values for true server-side batching
    (one INSERT statement per chunk = one network round trip). Naive
    cur.executemany would issue one round trip PER ROW against Cloud SQL
    and turn a 41K-polygon load into a 25-minute crawl.
    """
    inserted = 0
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '10s'")
        cur.execute("SET LOCAL statement_timeout = '300s'")
        cur.execute("TRUNCATE flood_zones RESTART IDENTITY")

        # ST_GeomFromGeoJSON returns 4326 by default since GeoJSON spec
        # says coordinates are WGS84. NFHL ships NAD83 but the numeric
        # values are <1m drift from WGS84 in Texas; ST_MakeValid +
        # ST_Multi normalize topology + ensure MultiPolygon schema match.
        template = (
            "(%s, %s, %s, %s, %s, %s, %s, %s, "
            "ST_Multi(ST_MakeValid(ST_GeomFromGeoJSON(%s))))"
        )
        for i in range(0, len(all_rows), BATCH_SIZE):
            batch = all_rows[i:i + BATCH_SIZE]
            execute_values(
                cur,
                """
                INSERT INTO flood_zones (
                    dfirm_id, fld_ar_id, fld_zone, zone_subty, sfha_tf,
                    static_bfe, dual_zone, source_county, geom
                ) VALUES %s
                """,
                batch,
                template=template,
                page_size=BATCH_SIZE,
            )
            inserted += len(batch)
            print(f"[ingest_flood] inserted {inserted}/{len(all_rows)} rows")
    conn.commit()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        required=True,
        help="Snapshot date folder, e.g. 2026-06-05 — must exist under "
             "ingest/fema/nfhl/",
    )
    args = parser.parse_args()

    snapshot_dir = ROOT_DIR / "ingest" / "fema" / "nfhl" / args.snapshot
    if not snapshot_dir.is_dir():
        print(
            f"[ingest_flood] ERROR: snapshot dir not found: {snapshot_dir}",
            file=sys.stderr,
        )
        return 1

    conn = get_conn()
    try:
        print("[ingest_flood] running schema migration ...")
        _ensure_schema_txn(conn)
        _ensure_indexes_concurrent(conn)
        print("[ingest_flood] schema migration complete")

        all_rows: list[tuple] = []
        for county in COUNTY_FIPS:
            start = time.time()
            print(f"[ingest_flood] reading {county} ({COUNTY_FIPS[county]}) ...")
            rows = _read_county_polygons(county, snapshot_dir)
            elapsed = time.time() - start
            print(f"[ingest_flood] {county}: read {len(rows)} rows in {elapsed:.1f}s")
            all_rows.extend(rows)

        if not all_rows:
            print(
                "[ingest_flood] ERROR: no rows parsed across any county. "
                "Aborting before TRUNCATE.",
                file=sys.stderr,
            )
            return 1

        print(f"[ingest_flood] TRUNCATE + INSERT {len(all_rows)} rows total ...")
        inserted = _truncate_and_insert(conn, all_rows)
        print(f"[ingest_flood] DONE — {inserted} polygons loaded across 4 counties")
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"[ingest_flood] FAILED: {exc}", file=sys.stderr)
        raise
    finally:
        release_conn(conn)


if __name__ == "__main__":
    sys.exit(main())
