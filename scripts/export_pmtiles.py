# scripts/export_pmtiles.py
#
# Role:
#   Export DCAD, TAD, Collin, and Denton parcel features from Postgres as
#   newline-delimited GeoJSON
#   for PMTiles generation.
#
# Connections:
#   - api/config.py: uses get_conn() and release_conn() for DB access
#   - docs/PMTILES_PLAN.md: SQL source of truth for export shape and fields
#   - tippecanoe CLI: this script prints the next command to run (does not execute it)
#
# Classification codes mirror api/counties/dcad.py:classify_parcel(),
# api/counties/tad.py:_classify_tad(), api/counties/collin.py:_classify_collin(),
# and api/counties/denton.py:_classify_denton().

from __future__ import annotations

import argparse
from pathlib import Path

from api.config import get_conn, release_conn


# Codes mirror classify_parcel() in api/counties/dcad.py.
# Owner name (gov/HOA) check is omitted — acceptable approximation for tile coloring.
DCAD_EXPORT_SQL = """
-- Polygon parcels: one feature per physical footprint.
-- DISTINCT ON (geom_key) collapses units/sub-parcels that share identical
-- geometry even when IDs differ. This prevents stacked alpha fills turning into
-- solid blocks in browse mode.
WITH source_rows AS (
  SELECT
    md5(p.polygon_geojson::text) AS geom_key,
    p.account_num,
    p.property_address,
    p.owner_name,
    COALESCE(a.tot_val, 0) AS tot_val,
    COALESCE(l.area_size, 0) AS area_size,
    COALESCE(l.area_estimated, false) AS area_estimated,
    -- ⚠️ PHASE 2 PENDING (2026-06-30): the live classifier (api/counties/dcad.py) now
    -- treats B12 as 'duplexes' (label-first, audit confirmed). This SQL still says
    -- 'multifamily' for B12. Re-baking from this file WILL regress browse (all DCAD
    -- duplexes → dark multifamily). Do NOT re-bake until the Phase-2 single-source
    -- fix lands and this CASE is updated to match dcad.py classify_parcel().
    CASE
      WHEN e.account_num IS NOT NULL
           OR COALESCE(a.sptd_code, p.sptd_code) IN ('X11', 'D10')            THEN 'exempt'
      WHEN COALESCE(a.sptd_code, p.sptd_code) IN ('C11', 'C12')
           AND COALESCE(a.tot_val, 0) <= 500                                   THEN 'exempt'
      WHEN COALESCE(a.sptd_code, p.sptd_code) IN ('B11', 'B12', 'A14', 'A13') THEN 'multifamily'
      WHEN COALESCE(a.sptd_code, p.sptd_code) = 'C11'                          THEN 'vacant'
      WHEN COALESCE(a.sptd_code, p.sptd_code) IN ('C12', 'C13', 'F10', 'F20') THEN 'commercial'
      ELSE 'single_family'
    END AS prop_type,
    p.polygon_geojson::json AS geometry
  FROM parcels p
  LEFT JOIN appraisal a       ON p.account_num = a.account_num
  LEFT JOIN land_detail l     ON p.account_num = l.account_num
  LEFT JOIN exempt_accounts e ON p.account_num = e.account_num
  WHERE p.polygon_geojson IS NOT NULL
    AND p.polygon_geojson != '{"type": "Polygon", "coordinates": []}'
    AND (p.polygon_geojson::json)->>'type' IN ('Polygon', 'MultiPolygon')
),
polygon_parcels AS (
  SELECT DISTINCT ON (s.geom_key)
    json_build_object(
      'type', 'Feature',
      'geometry', s.geometry,
      'properties', json_build_object(
        'account_num',           s.account_num,
        'prop_type',             s.prop_type,
        'situs_addr',            s.property_address,
        'owner_name',            s.owner_name,
        'appraised_val_current', s.tot_val,
        'area_size',             s.area_size,
        'area_estimated',        s.area_estimated,
        'source_county',         'dcad'
      )
    ) AS feature
  FROM source_rows s
  ORDER BY s.geom_key, s.tot_val DESC, s.account_num
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
    -- ⚠️ PHASE 2 PENDING (2026-06-30): see polygon block comment above. Same B12/duplexes
    -- divergence applies to this centroid fallback. Do not re-bake until Phase 2 lands.
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
-- DISTINCT ON (geom_key) collapses duplicates that share identical geometry
-- even when taxpin/account/parcel keys differ.
WITH source_rows AS (
  SELECT
    md5(ST_AsGeoJSON(t.geom)) AS geom_key,
    t.account_num,
    t.situs_addr,
    t.owner_name,
    COALESCE(t.total_value, 0) AS total_value,
    COALESCE(t.land_sqft, t.land_acres * 43560, 0) AS area_size,
    -- ⚠️ PHASE 2 PENDING (2026-06-30): tad.py classify_parcel() already emits
    -- 'duplexes' for B2/B3/B4 (duplex codes). This SQL still maps them to
    -- 'multifamily'. Do NOT re-bake TAD tiles until Phase 2 updates this CASE.
    CASE
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
    END AS prop_type,
    ST_AsGeoJSON(t.geom)::json AS geometry
  FROM tad_parcels t
  WHERE t.geom IS NOT NULL
    AND ST_IsValid(t.geom)
),
polygon_parcels AS (
  SELECT DISTINCT ON (s.geom_key)
    json_build_object(
      'type', 'Feature',
      'geometry', s.geometry,
      'properties', json_build_object(
        'account_num',           s.account_num,
        'prop_type',             s.prop_type,
        'situs_addr',            s.situs_addr,
        'owner_name',            s.owner_name,
        'appraised_val_current', s.total_value,
        'area_size',             s.area_size,
        'source_county',         'tad'
      )
    )::text AS feature
  FROM source_rows s
  ORDER BY s.geom_key, s.total_value DESC, s.account_num
)
SELECT feature FROM polygon_parcels
"""


# Codes mirror _classify_collin() in api/counties/collin.py.
# Owner name (gov/HOA) check and nominal-value check are omitted — acceptable
# approximation for tile coloring.
COLLIN_EXPORT_SQL = """
-- Polygon parcels: one feature per physical footprint.
-- DISTINCT ON (geom_key) collapses duplicates that share identical geometry
-- even when geo/account keys differ.
WITH source_rows AS (
  SELECT
    md5(ST_AsGeoJSON(c.geom)) AS geom_key,
    c.account_num,
    c.property_address,
    c.owner_name,
    COALESCE(c.total_value, 0) AS total_value,
    COALESCE(c.land_sqft, c.land_acres * 43560, 0) AS area_size,
    -- ⚠️ PHASE 2 PENDING (2026-06-30): collin.py classify_parcel() already emits
    -- 'duplexes' for B2/B3/B4 (duplex codes). This SQL still maps them to
    -- 'multifamily'. Do NOT re-bake Collin tiles until Phase 2 updates this CASE.
    CASE
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
    END AS prop_type,
    ST_AsGeoJSON(c.geom)::json AS geometry
  FROM collin_parcels c
  WHERE c.geom IS NOT NULL
    AND ST_IsValid(c.geom)
),
polygon_parcels AS (
  SELECT DISTINCT ON (s.geom_key)
    json_build_object(
      'type', 'Feature',
      'geometry', s.geometry,
      'properties', json_build_object(
        'account_num',           s.account_num,
        'prop_type',             s.prop_type,
        'situs_addr',            s.property_address,
        'owner_name',            s.owner_name,
        'appraised_val_current', s.total_value,
        'area_size',             s.area_size,
        'area_estimated',        false,
        'source_county',         'collin'
      )
    )::text AS feature
  FROM source_rows s
  ORDER BY s.geom_key, s.total_value DESC, s.account_num
)
SELECT feature FROM polygon_parcels
"""


# Codes mirror _classify_denton() in api/counties/denton.py.
# Includes primary-code split from comma-delimited state_cd and owner keyword
# checks to keep PMTiles browse colors aligned with analyze route behavior.
DENTON_EXPORT_SQL = """
SELECT json_build_object(
  'type', 'Feature',
  'geometry', ST_AsGeoJSON(d.geom)::json,
  'properties', json_build_object(
    'account_num',           d.account_num,
    'prop_type',             CASE
      WHEN code IN ('D1', 'D2', 'E1', 'E4') THEN 'exempt'
      WHEN owner_up LIKE '%CITY OF %'
        OR owner_up LIKE '%COUNTY%'
        OR owner_up LIKE '%STATE OF TEXAS%'
        OR owner_up LIKE '%UNITED STATES%'
        OR owner_up LIKE '% ISD%'
        OR owner_up LIKE '%INDEPENDENT SCHOOL DISTRICT%'
        OR owner_up LIKE '%COLLIN COLLEGE%'
        OR (
          (
            owner_up LIKE '%HOMEOWNER%'
            OR owner_up LIKE '%OWNERS ASSOC%'
            OR owner_up LIKE '% HOA%'
            OR owner_up LIKE '%CIVIC ASSOC%'
            OR owner_up LIKE '%PROPERTY OWNERS%'
          )
          AND code NOT IN ('A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A9')
        ) THEN 'exempt'
      -- ⚠️ PHASE 2 PENDING (2026-06-30): denton.py classify_parcel() emits 'duplexes'
      -- for OB2 and B2 (duplex codes). This SQL maps them to 'multifamily'. Do NOT
      -- re-bake Denton tiles until Phase 2 updates this CASE.
      WHEN code IN ('B1', 'B2') THEN 'multifamily'
      WHEN code IN ('C1', 'C2', 'C3', 'C5') THEN 'vacant'
      WHEN code IN ('F1', 'F2', 'F3', 'J3', 'J5', 'OC1') THEN 'commercial'
      WHEN code IN ('A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'OA1', 'OA5') THEN 'single_family'
      ELSE 'single_family'
    END,
    'situs_addr',            d.property_address,
    'owner_name',            d.owner_name,
    'appraised_val_current', COALESCE(d.total_value, 0),
    'area_size',             COALESCE(d.land_sqft, d.land_acres * 43560, ST_Area(d.geom::geography) * 10.763910416709722, 0),
    'area_estimated',        CASE
                               WHEN COALESCE(d.land_sqft, 0) > 0 THEN false
                               WHEN COALESCE(d.land_acres, 0) > 0 THEN false
                               ELSE true
                             END,
    'source_county',         'denton'
  )
)::text AS feature
FROM (
  SELECT
    p.account_num,
    p.property_address,
    p.owner_name,
    p.total_value,
    p.land_sqft,
    p.land_acres,
    p.geom,
    UPPER(TRIM(SPLIT_PART(COALESCE(p.state_cd, ''), ',', 1))) AS code,
    UPPER(COALESCE(p.owner_name, '')) AS owner_up
  FROM denton_parcels p
  WHERE p.geom IS NOT NULL
    AND ST_IsValid(p.geom)
) d
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
    description="Export DCAD, TAD, Collin, and Denton parcels to newline-delimited GeoJSON for PMTiles."
    )
    parser.add_argument("--dcad-out", default="dcad.geojsonl")
    parser.add_argument("--tad-out", default="tad.geojsonl")
    parser.add_argument("--collin-out", default="collin.geojsonl")
    parser.add_argument("--denton-out", default="denton.geojsonl")
    args = parser.parse_args()

    dcad_out = Path(args.dcad_out)
    tad_out = Path(args.tad_out)
    collin_out = Path(args.collin_out)
    denton_out = Path(args.denton_out)

    print("Exporting DCAD...")
    dcad_count = export_query_to_geojsonl(DCAD_EXPORT_SQL, dcad_out, "dcad_export")
    print(f"DCAD export complete: {dcad_out} ({dcad_count:,} rows)")

    print("Exporting TAD...")
    tad_count = export_query_to_geojsonl(TAD_EXPORT_SQL, tad_out, "tad_export")
    print(f"TAD export complete: {tad_out} ({tad_count:,} rows)")

    print("Exporting Collin...")
    collin_count = export_query_to_geojsonl(COLLIN_EXPORT_SQL, collin_out, "collin_export")
    print(f"Collin export complete: {collin_out} ({collin_count:,} rows)")

    print("Exporting Denton...")
    denton_count = export_query_to_geojsonl(DENTON_EXPORT_SQL, denton_out, "denton_export")
    print(f"Denton export complete: {denton_out} ({denton_count:,} rows)")

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
        f"-L '{{\"file\":\"{collin_out}\",\"layer\":\"collin\"}}' "
        f"-L '{{\"file\":\"{denton_out}\",\"layer\":\"denton\"}}'"
    )

    print("\nThen — upload to GCS (NOT executed by this script).")
    print("Use a NEW versioned filename; never overwrite the existing file in place.")
    print("Include the Cache-Control flag to keep caching policy explicit and consistent:")
    print(
        'gsutil -h "Cache-Control:public, max-age=3600" '
        "cp parcels.pmtiles gs://YOUR-BUCKET/parcels-YYYYMMDD.pmtiles"
    )
    print("Then update TILES_BASE_URL on the Cloud Run service to the new file URL.")


if __name__ == "__main__":
    main()
