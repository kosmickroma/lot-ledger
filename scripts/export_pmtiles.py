# scripts/export_pmtiles.py
#
# Role:
#   Export DCAD and TAD parcel features from Postgres as newline-delimited GeoJSON
#   for PMTiles generation.
#
# Connections:
#   - api/config.py: uses get_conn() and release_conn() for DB access
#   - docs/PMTILES_PLAN.md: SQL source of truth for export shape and fields
#   - tippecanoe CLI: this script prints the next command to run (does not execute it)
#
# Classification codes mirror api/counties/dcad.py:classify_parcel()
# and api/counties/tad.py:_classify_tad() exactly.

from __future__ import annotations

import argparse
from pathlib import Path

from api.config import get_conn, release_conn


# Codes mirror classify_parcel() in api/counties/dcad.py.
# Owner name (gov/HOA) check is omitted — acceptable approximation for tile coloring.
DCAD_EXPORT_SQL = """
SELECT feature::text
FROM (
  -- Parcels with polygon geometry
  SELECT json_build_object(
    'type', 'Feature',
    'geometry', p.polygon_geojson::json,
    'properties', json_build_object(
      'account_num',           p.account_num,
      'prop_type',             CASE
        WHEN e.account_num IS NOT NULL
             OR a.sptd_code IN ('X11', 'D10')           THEN 'exempt'
        WHEN a.sptd_code IN ('C11', 'C12')
             AND COALESCE(a.tot_val, 0) <= 500           THEN 'exempt'
        WHEN a.sptd_code IN ('B11', 'B12', 'A14', 'A13') THEN 'multifamily'
        WHEN a.sptd_code = 'C11'                         THEN 'vacant'
        WHEN a.sptd_code IN ('C12', 'C13', 'F10', 'F20') THEN 'commercial'
        ELSE 'single_family'
      END,
      'situs_addr',            p.property_address,
      'owner_name',            p.owner_name,
      'appraised_val_current', COALESCE(a.tot_val, 0),
      'source_county',         'dcad'
    )
  ) AS feature
  FROM parcels p
  LEFT JOIN appraisal a       ON p.account_num = a.account_num
  LEFT JOIN exempt_accounts e ON p.account_num = e.account_num
  WHERE p.polygon_geojson IS NOT NULL
    AND p.polygon_geojson != '{"type": "Polygon", "coordinates": []}'
    AND (p.polygon_geojson::json)->>'type' IN ('Polygon', 'MultiPolygon')

  UNION ALL

  -- Fallback: parcels with centroid only (no polygon)
  SELECT json_build_object(
    'type', 'Feature',
    'geometry', ST_AsGeoJSON(p.centroid)::json,
    'properties', json_build_object(
      'account_num',           p.account_num,
      'prop_type',             CASE
        WHEN e.account_num IS NOT NULL
             OR a.sptd_code IN ('X11', 'D10')           THEN 'exempt'
        WHEN a.sptd_code IN ('C11', 'C12')
             AND COALESCE(a.tot_val, 0) <= 500           THEN 'exempt'
        WHEN a.sptd_code IN ('B11', 'B12', 'A14', 'A13') THEN 'multifamily'
        WHEN a.sptd_code = 'C11'                         THEN 'vacant'
        WHEN a.sptd_code IN ('C12', 'C13', 'F10', 'F20') THEN 'commercial'
        ELSE 'single_family'
      END,
      'situs_addr',            p.property_address,
      'owner_name',            p.owner_name,
      'appraised_val_current', COALESCE(a.tot_val, 0),
      'source_county',         'dcad'
    )
  ) AS feature
  FROM parcels p
  LEFT JOIN appraisal a       ON p.account_num = a.account_num
  LEFT JOIN exempt_accounts e ON p.account_num = e.account_num
  WHERE (p.polygon_geojson IS NULL
      OR p.polygon_geojson = '{"type": "Polygon", "coordinates": []}')
    AND p.centroid IS NOT NULL
) q
"""


# Codes mirror _classify_tad() in api/counties/tad.py.
# Owner name (gov/HOA) check and nominal-value check are omitted — acceptable
# approximation for tile coloring.
TAD_EXPORT_SQL = """
SELECT json_build_object(
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
    'source_county',         'tad'
  )
)::text AS feature
FROM tad_parcels t
WHERE t.geom IS NOT NULL
  AND ST_IsValid(t.geom)
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
        description="Export DCAD and TAD parcels to newline-delimited GeoJSON for PMTiles."
    )
    parser.add_argument("--dcad-out", default="dcad.geojsonl")
    parser.add_argument("--tad-out", default="tad.geojsonl")
    args = parser.parse_args()

    dcad_out = Path(args.dcad_out)
    tad_out = Path(args.tad_out)

    print("Exporting DCAD...")
    dcad_count = export_query_to_geojsonl(DCAD_EXPORT_SQL, dcad_out, "dcad_export")
    print(f"DCAD export complete: {dcad_out} ({dcad_count:,} rows)")

    print("Exporting TAD...")
    tad_count = export_query_to_geojsonl(TAD_EXPORT_SQL, tad_out, "tad_export")
    print(f"TAD export complete: {tad_out} ({tad_count:,} rows)")

    print("\nNext — run tippecanoe (NOT executed by this script):")
    print(
        f"tippecanoe "
        f"-o parcels.pmtiles "
        f"-Z12 -z16 "
        f"-pn "
        f"-y account_num -y prop_type -y situs_addr -y owner_name "
        f"-y appraised_val_current -y source_county "
        f"--coalesce-densest-as-needed "
        f"--extend-zooms-if-still-dropping "
        f"-f "
        f"-L '{{\"file\":\"{dcad_out}\",\"layer\":\"dcad\"}}' "
        f"-L '{{\"file\":\"{tad_out}\",\"layer\":\"tad\"}}'"
    )


if __name__ == "__main__":
    main()
