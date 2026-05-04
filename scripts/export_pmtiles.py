# scripts/export_pmtiles.py
#
# Role:
#   Export DCAD, TAD, and Collin parcel features from Postgres as
#   newline-delimited GeoJSON
#   for PMTiles generation.
#
# Connections:
#   - api/config.py: uses get_conn() and release_conn() for DB access
#   - docs/PMTILES_PLAN.md: SQL source of truth for export shape and fields
#   - tippecanoe CLI: this script prints the next command to run (does not execute it)
#
# Classification codes mirror api/counties/dcad.py:classify_parcel(),
# api/counties/tad.py:_classify_tad(), and api/counties/collin.py:_classify_collin().

from __future__ import annotations

import argparse
from pathlib import Path

from api.config import get_conn, release_conn


# Codes mirror classify_parcel() in api/counties/dcad.py.
# Owner name (gov/HOA) check is omitted — acceptable approximation for tile coloring.
DCAD_EXPORT_SQL = """
-- Polygon parcels: one feature per physical footprint.
-- DISTINCT ON (gis_parcel_id) collapses condo units and commercial sub-parcels
-- that share the same building polygon. Without this, tippecanoe encodes all N
-- duplicate features and protomaps stacks them on the canvas — N × alpha fills
-- add up to a fully-opaque solid block.
WITH polygon_parcels AS (
  SELECT DISTINCT ON (COALESCE(p.gis_parcel_id, p.account_num))
    json_build_object(
      'type', 'Feature',
      'geometry', p.polygon_geojson::json,
      'properties', json_build_object(
        'account_num',           p.account_num,
        'prop_type',             CASE
          WHEN e.account_num IS NOT NULL
               OR COALESCE(a.sptd_code, p.sptd_code) IN ('X11', 'D10')            THEN 'exempt'
          WHEN COALESCE(a.sptd_code, p.sptd_code) IN ('C11', 'C12')
               AND COALESCE(a.tot_val, 0) <= 500                                   THEN 'exempt'
          WHEN COALESCE(a.sptd_code, p.sptd_code) IN ('B11', 'B12', 'A14', 'A13') THEN 'multifamily'
          WHEN COALESCE(a.sptd_code, p.sptd_code) = 'C11'                          THEN 'vacant'
          WHEN COALESCE(a.sptd_code, p.sptd_code) IN ('C12', 'C13', 'F10', 'F20') THEN 'commercial'
          ELSE 'single_family'
        END,
        'situs_addr',            p.property_address,
        'owner_name',            p.owner_name,
        'appraised_val_current', COALESCE(a.tot_val, 0),
        'area_size',             COALESCE(l.area_size, 0),
        'area_estimated',        COALESCE(l.area_estimated, false),
        'source_county',         'dcad'
      )
    ) AS feature
  FROM parcels p
  LEFT JOIN appraisal a       ON p.account_num = a.account_num
  LEFT JOIN land_detail l     ON p.account_num = l.account_num
  LEFT JOIN exempt_accounts e ON p.account_num = e.account_num
  WHERE p.polygon_geojson IS NOT NULL
    AND p.polygon_geojson != '{"type": "Polygon", "coordinates": []}'
    AND (p.polygon_geojson::json)->>'type' IN ('Polygon', 'MultiPolygon')
  ORDER BY COALESCE(p.gis_parcel_id, p.account_num), p.account_num
)
SELECT feature::text FROM polygon_parcels

UNION ALL

-- Fallback: parcels with centroid only (no polygon). Points don't stack so no
-- deduplication needed here.
SELECT json_build_object(
  'type', 'Feature',
  'geometry', ST_AsGeoJSON(p.centroid)::json,
  'properties', json_build_object(
    'account_num',           p.account_num,
    'prop_type',             CASE
      WHEN e.account_num IS NOT NULL
           OR COALESCE(a.sptd_code, p.sptd_code) IN ('X11', 'D10')            THEN 'exempt'
      WHEN COALESCE(a.sptd_code, p.sptd_code) IN ('C11', 'C12')
           AND COALESCE(a.tot_val, 0) <= 500                                   THEN 'exempt'
      WHEN COALESCE(a.sptd_code, p.sptd_code) IN ('B11', 'B12', 'A14', 'A13') THEN 'multifamily'
      WHEN COALESCE(a.sptd_code, p.sptd_code) = 'C11'                          THEN 'vacant'
      WHEN COALESCE(a.sptd_code, p.sptd_code) IN ('C12', 'C13', 'F10', 'F20') THEN 'commercial'
      ELSE 'single_family'
    END,
    'situs_addr',            p.property_address,
    'owner_name',            p.owner_name,
    'appraised_val_current', COALESCE(a.tot_val, 0),
    'area_size',             COALESCE(l.area_size, 0),
    'area_estimated',        COALESCE(l.area_estimated, false),
    'source_county',         'dcad'
  )
)::text AS feature
FROM parcels p
LEFT JOIN appraisal a       ON p.account_num = a.account_num
LEFT JOIN land_detail l     ON p.account_num = l.account_num
LEFT JOIN exempt_accounts e ON p.account_num = e.account_num
WHERE (p.polygon_geojson IS NULL
    OR p.polygon_geojson = '{"type": "Polygon", "coordinates": []}')
  AND p.centroid IS NOT NULL
"""


# Codes mirror _classify_tad() in api/counties/tad.py.
# Owner name (gov/HOA) check and nominal-value check are omitted — acceptable
# approximation for tile coloring.
TAD_EXPORT_SQL = """
-- Polygon parcels: one feature per physical footprint.
-- DISTINCT ON (taxpin/account_num/parcel_key) collapses condo or sequence-level
-- duplicates that share the same geometry and would otherwise stack as solids.
WITH polygon_parcels AS (
  SELECT DISTINCT ON (COALESCE(t.taxpin, t.account_num, t.parcel_key))
    json_build_object(
      'type', 'Feature',
      'geometry', ST_AsGeoJSON(t.geom)::json,
      'properties', json_build_object(
        'account_num',           t.account_num,
        'prop_type',             CASE
          WHEN t.property_class IN (
                 'D1','D2','G1','G2','G3','G4',
                 'J1','J2','J3','J4','J5',
                 'ROC','AC','X'
               )                                             THEN 'exempt'
          WHEN t.property_class IN (
                 'B1','B2','B3','B4','M1','M2','A3'
               )                                             THEN 'multifamily'
          WHEN t.property_class IN ('C1','C1C','O1')        THEN 'vacant'
          WHEN t.property_class IN (
                 'C2','C2C','BC','F1','F2','L1','L2','S'
               )                                             THEN 'commercial'
          ELSE 'single_family'
        END,
        'situs_addr',            t.situs_addr,
        'owner_name',            t.owner_name,
        'appraised_val_current', COALESCE(t.total_value, 0),
        'area_size',             COALESCE(t.land_sqft, t.land_acres * 43560, 0),
        'source_county',         'tad'
      )
    )::text AS feature
  FROM tad_parcels t
  WHERE t.geom IS NOT NULL
    AND ST_IsValid(t.geom)
  ORDER BY COALESCE(t.taxpin, t.account_num, t.parcel_key), t.account_num
)
SELECT feature FROM polygon_parcels
"""


# Codes mirror _classify_collin() in api/counties/collin.py.
# Owner name (gov/HOA) check and nominal-value check are omitted — acceptable
# approximation for tile coloring.
COLLIN_EXPORT_SQL = """
-- Polygon parcels: one feature per physical footprint.
-- DISTINCT ON (geo_id/account_num) collapses condo/account-level duplicates
-- that share the same polygon geometry and otherwise render as opaque solids.
WITH polygon_parcels AS (
  SELECT DISTINCT ON (COALESCE(c.geo_id, c.account_num, c.parcel_key))
    json_build_object(
      'type', 'Feature',
      'geometry', ST_AsGeoJSON(c.geom)::json,
      'properties', json_build_object(
        'account_num',           c.account_num,
        'prop_type',             CASE
          WHEN c.state_cd LIKE 'EX%%'
               OR c.state_cd IN ('D1', 'D2', 'D6', 'J1A', 'J2A', 'J3A', 'J4A', 'J5', 'J6A')
                                                         THEN 'exempt'
          WHEN c.state_cd IN ('A3', 'B1', 'B2', 'B3', 'B4', 'B6', 'B9')
                                                         THEN 'multifamily'
          WHEN c.state_cd IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'O')
                                                         THEN 'vacant'
          WHEN c.state_cd IN ('F1', 'F2', 'F3', 'F4', 'F6', 'F7', 'F9', 'M1', 'M2', 'M4', 'M5')
                                                         THEN 'commercial'
          ELSE 'single_family'
        END,
        'situs_addr',            c.property_address,
        'owner_name',            c.owner_name,
        'appraised_val_current', COALESCE(c.total_value, 0),
        'area_size',             COALESCE(c.land_sqft, c.land_acres * 43560, 0),
        'area_estimated',        false,
        'source_county',         'collin'
      )
    )::text AS feature
  FROM collin_parcels c
  WHERE c.geom IS NOT NULL
    AND ST_IsValid(c.geom)
  ORDER BY COALESCE(c.geo_id, c.account_num, c.parcel_key), c.account_num
)
SELECT feature FROM polygon_parcels
"""


def export_query_to_geojsonl(sql: str, output_path: Path, cursor_name: str) -> int:
    conn = get_conn()
    row_count = 0
    try:
        # Named (server-side) cursor streams rows in batches — avoids loading
        # 1M+ rows into memory at once.
        with conn.cursor(cursor_name) as cur:
            cur.itersize = 2000
            cur.execute(sql)
            with output_path.open("w", encoding="utf-8") as out_file:
                for row in cur:
                    feature_json = row[0]
                    if not feature_json:
                        continue
                    out_file.write(feature_json)
                    out_file.write("\n")
                    row_count += 1
                    if row_count % 100_000 == 0:
                        print(f"  {row_count:,} rows written...")
    finally:
        release_conn(conn)
    return row_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export DCAD, TAD, and Collin parcels to newline-delimited GeoJSON for PMTiles."
    )
    parser.add_argument("--dcad-out", default="dcad.geojsonl")
    parser.add_argument("--tad-out", default="tad.geojsonl")
    parser.add_argument("--collin-out", default="collin.geojsonl")
    args = parser.parse_args()

    dcad_out = Path(args.dcad_out)
    tad_out = Path(args.tad_out)
    collin_out = Path(args.collin_out)

    print("Exporting DCAD...")
    dcad_count = export_query_to_geojsonl(DCAD_EXPORT_SQL, dcad_out, "dcad_export")
    print(f"DCAD export complete: {dcad_out} ({dcad_count:,} rows)")

    print("Exporting TAD...")
    tad_count = export_query_to_geojsonl(TAD_EXPORT_SQL, tad_out, "tad_export")
    print(f"TAD export complete: {tad_out} ({tad_count:,} rows)")

    print("Exporting Collin...")
    collin_count = export_query_to_geojsonl(COLLIN_EXPORT_SQL, collin_out, "collin_export")
    print(f"Collin export complete: {collin_out} ({collin_count:,} rows)")

    print("\nNext — run tippecanoe (NOT executed by this script):")
    print(
        f"tippecanoe "
        f"-o parcels.pmtiles "
        f"-Z12 -z16 "
        f"-pn "
        f"-y account_num -y prop_type -y situs_addr -y owner_name "
        f"-y appraised_val_current -y area_size -y area_estimated -y source_county "
        f"--coalesce-densest-as-needed "
        f"--extend-zooms-if-still-dropping "
        f"-f "
        f"-L '{{\"file\":\"{dcad_out}\",\"layer\":\"dcad\"}}' "
        f"-L '{{\"file\":\"{tad_out}\",\"layer\":\"tad\"}}' "
        f"-L '{{\"file\":\"{collin_out}\",\"layer\":\"collin\"}}'"
    )


if __name__ == "__main__":
    main()
