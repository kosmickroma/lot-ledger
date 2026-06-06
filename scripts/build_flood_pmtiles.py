#!/usr/bin/env python3
# scripts/build_flood_pmtiles.py
#
# Export FEMA NFHL flood polygons from the flood_zones PostGIS table to
# newline-delimited GeoJSON, then build flood_zones.pmtiles via tippecanoe
# (the existing parcels pattern at scripts/export_pmtiles.py + tippecanoe).
#
# Why PMTiles for flood zones:
#   The original Phase 3 used a live GET /api/flood-zones?bbox=... endpoint
#   serving GeoJSON. At wide zoom (DFW-level), the bbox matched 5000+
#   polygons, response payload hit 10MB+, and Cloud Run hit OOM on the 1Gi
#   preview instance → 503 errors mid-load. PMTiles pre-renders into
#   per-zoom-level vector tiles served as a flat file from GCS — browser
#   only pulls ~10KB tiles for the current viewport, zero per-request DB
#   work, GPU-accelerated rendering via protomaps-leaflet.
#
# Connections:
#   - api/config.py: get_conn() / release_conn() for DB access
#   - scripts/export_pmtiles.py: source pattern this mirrors
#   - tippecanoe CLI: this script prints (and optionally runs) the tippecanoe
#     command. Tippecanoe is already installed on the dev box at /usr/bin/.
#   - gsutil: this script prints the GCS upload command. Operator runs that
#     manually so the upload is auditable.
#
# Quarterly refresh workflow:
#   1. Drop new FEMA ZIPs in ingest/fema/nfhl/<YYYY-MM-DD>/raw/
#   2. Unzip into ingest/fema/nfhl/<YYYY-MM-DD>/unzipped/<county>/
#   3. python -m scripts.ingest_flood_zones --snapshot <YYYY-MM-DD>
#   4. python -m scripts.build_flood_pmtiles --run-tippecanoe --upload
#   5. Hard-refresh prod — protomaps-leaflet picks up the new tiles

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn  # noqa: E402


# Export shape:
#   - severity: numeric code so the symbolizer can fast-switch by value
#               (FLOODWAY=5, AE/A/AH/AO/V=4, X-shaded=3, X-unshaded=2, other=1)
#   - fld_zone, zone_subty, static_bfe, source_county: for tooltip + popup
#               wording. PMTiles tooltips read directly from properties.
#   - geometry: must already be MultiPolygon in 4326 (loader guarantees both)
#
# Note: ST_AsGeoJSON returns Polygon for single-ring MultiPolygons; tippecanoe
# accepts both. No need to coerce to MultiPolygon for the tile build.
FLOOD_EXPORT_SQL = """
SELECT json_build_object(
  'type', 'Feature',
  'geometry', ST_AsGeoJSON(geom)::json,
  'properties', json_build_object(
    'severity',      CASE
                       WHEN zone_subty = 'FLOODWAY' THEN 5
                       WHEN fld_zone IN ('AE','A','AH','AO','V','VE') THEN 4
                       WHEN zone_subty = '0.2 PCT ANNUAL CHANCE FLOOD HAZARD' THEN 3
                       WHEN fld_zone = 'X' THEN 2
                       ELSE 1
                     END,
    'fld_zone',      fld_zone,
    'zone_subty',    COALESCE(zone_subty, ''),
    'sfha_tf',       COALESCE(sfha_tf, ''),
    'static_bfe',    static_bfe,
    'source_county', source_county
  )
)::text AS feature
FROM flood_zones
WHERE geom IS NOT NULL
  AND ST_IsValid(geom)
"""


def export_flood_to_geojsonl(output_path: Path) -> int:
    conn = get_conn()
    row_count = 0
    try:
        # Named server-side cursor streams rows in 2K batches — keeps memory
        # bounded even though flood_zones is only 41K rows.
        with conn.cursor("flood_zones_export") as cur:
            cur.itersize = 2000
            cur.execute(FLOOD_EXPORT_SQL)
            with output_path.open("w", encoding="utf-8") as out_file:
                for row in cur:
                    feature_json = row[0]
                    if not feature_json:
                        continue
                    out_file.write(feature_json)
                    out_file.write("\n")
                    row_count += 1
                    if row_count % 10_000 == 0:
                        print(f"  {row_count:,} rows written...")
    finally:
        release_conn(conn)
    return row_count


def build_tippecanoe_command(geojsonl_path: Path, pmtiles_path: Path) -> list[str]:
    return [
        "tippecanoe",
        "-o", str(pmtiles_path),
        # Zoom range: 8 (DFW-wide overview) to 16 (street-level detail).
        # Parcels build uses Z12-16; we extend down to Z8 because flood
        # polygons are large enough to be meaningful even zoomed way out.
        "-Z8", "-z16",
        # Layer name read by protomaps-leaflet on the frontend.
        "-l", "flood_zones",
        # Keep properties on every feature so per-feature symbolizer
        # can read severity / fld_zone / zone_subty.
        "-y", "severity",
        "-y", "fld_zone",
        "-y", "zone_subty",
        "-y", "sfha_tf",
        "-y", "static_bfe",
        "-y", "source_county",
        # Drop densest features as needed at low zooms so the tile stays
        # under the 500KB-per-tile soft cap — visual fidelity follows
        # severity ordering, so floodways stay visible the longest.
        "--coalesce-densest-as-needed",
        # If still over budget at the target zoom, extend higher.
        "--extend-zooms-if-still-dropping",
        # Allow overwrite + skip the "no features" check.
        "-f",
        str(geojsonl_path),
    ]


def build_gsutil_upload_command(pmtiles_path: Path) -> list[str]:
    # Same bucket as parcels.pmtiles — frontend will fetch from
    # https://storage.googleapis.com/lot-ledger-tiles/flood_zones.pmtiles
    return [
        "gsutil",
        "-h", "Cache-Control:public, max-age=3600",
        "cp",
        str(pmtiles_path),
        "gs://lot-ledger-tiles/flood_zones.pmtiles",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--geojsonl-out",
        default="flood_zones.geojsonl",
        help="Path for the intermediate GeoJSONL export (default: flood_zones.geojsonl)",
    )
    parser.add_argument(
        "--pmtiles-out",
        default="flood_zones.pmtiles",
        help="Path for the tippecanoe output (default: flood_zones.pmtiles)",
    )
    parser.add_argument(
        "--run-tippecanoe",
        action="store_true",
        help="Execute tippecanoe directly. Default is to print the command.",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Execute gsutil cp to GCS. Only effective if --run-tippecanoe also set.",
    )
    args = parser.parse_args()

    geojsonl_path = Path(args.geojsonl_out)
    pmtiles_path = Path(args.pmtiles_out)

    print(f"[build_flood_pmtiles] Exporting flood_zones → {geojsonl_path} ...")
    row_count = export_flood_to_geojsonl(geojsonl_path)
    print(f"[build_flood_pmtiles] Export complete: {row_count:,} features")

    if row_count == 0:
        print("[build_flood_pmtiles] ERROR: no rows exported — flood_zones empty?", file=sys.stderr)
        return 1

    tippecanoe_cmd = build_tippecanoe_command(geojsonl_path, pmtiles_path)
    print()
    print("[build_flood_pmtiles] Next — tippecanoe:")
    print("  " + " ".join(shlex.quote(arg) for arg in tippecanoe_cmd))

    if args.run_tippecanoe:
        print()
        print("[build_flood_pmtiles] Running tippecanoe ...")
        result = subprocess.run(tippecanoe_cmd, check=False)
        if result.returncode != 0:
            print("[build_flood_pmtiles] tippecanoe FAILED", file=sys.stderr)
            return result.returncode
        print("[build_flood_pmtiles] tippecanoe complete")

        if args.upload:
            upload_cmd = build_gsutil_upload_command(pmtiles_path)
            print()
            print("[build_flood_pmtiles] Uploading to GCS ...")
            print("  " + " ".join(shlex.quote(arg) for arg in upload_cmd))
            result = subprocess.run(upload_cmd, check=False)
            if result.returncode != 0:
                print("[build_flood_pmtiles] gsutil upload FAILED", file=sys.stderr)
                return result.returncode
            print("[build_flood_pmtiles] Upload complete — flood_zones.pmtiles live on GCS")
        else:
            upload_cmd = build_gsutil_upload_command(pmtiles_path)
            print()
            print("[build_flood_pmtiles] Next — upload to GCS:")
            print("  " + " ".join(shlex.quote(arg) for arg in upload_cmd))

    return 0


if __name__ == "__main__":
    sys.exit(main())
