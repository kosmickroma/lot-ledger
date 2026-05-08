# scripts/neighborhoods/build.py
# Role: one-shot TIGER 2024 block-group pipeline for the neighborhood overlay POC.
# Connects to:
#   ingest/neighborhoods/raw/         - cached TIGER zip and extraction dir
#   ingest/neighborhoods/processed/   - placeholder for intermediate files
#   frontend/tx_block_groups.geojson   - static output consumed by the toggle

from __future__ import annotations

import os
from pathlib import Path
import urllib.request
import zipfile

import geopandas as gpd
import psycopg2


REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "ingest" / "neighborhoods" / "raw"
PROCESSED_DIR = REPO_ROOT / "ingest" / "neighborhoods" / "processed"
OUTPUT_PATH = REPO_ROOT / "frontend" / "tx_block_groups.geojson"
ZIP_PATH = RAW_DIR / "tl_2024_48_bg.zip"
EXTRACT_DIR = RAW_DIR / "tl_2024_48_bg"

TIGER_URL = "https://www2.census.gov/geo/tiger/TIGER2024/BG/tl_2024_48_bg.zip"
DFW_COUNTY_FIPS = ("48113", "48439", "48085", "48121")
SIMPLIFY_TOLERANCE = 0.00005


def download_tiger_zip() -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists():
        print(f"Using cached TIGER zip: {ZIP_PATH}")
        return ZIP_PATH
    print(f"Downloading {TIGER_URL}")
    urllib.request.urlretrieve(TIGER_URL, ZIP_PATH)
    return ZIP_PATH


def extract_tiger_zip(zip_path: Path) -> Path:
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    shp_candidates = list(EXTRACT_DIR.glob("*.shp"))
    if shp_candidates:
        return shp_candidates[0]

    print(f"Extracting {zip_path}")
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(EXTRACT_DIR)

    shp_candidates = list(EXTRACT_DIR.glob("*.shp"))
    if not shp_candidates:
        raise FileNotFoundError(f"No shapefile found after extracting {zip_path}")
    return shp_candidates[0]


def load_block_groups(shp_path: Path) -> gpd.GeoDataFrame:
    print(f"Loading {shp_path}")
    gdf = gpd.read_file(shp_path, engine="pyogrio")

    if "GEOID" not in gdf.columns:
        raise ValueError("TIGER block-group file is missing GEOID")

    gdf = gdf[gdf["GEOID"].astype(str).str.startswith(DFW_COUNTY_FIPS)].copy()
    print(f"Block groups after DFW clip: {len(gdf)}")

    if gdf.empty:
        raise ValueError("DFW clip returned no block groups")

    gdf = gdf.to_crs(epsg=4326)
    gdf["geometry"] = gdf["geometry"].simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)

    keep_columns = [column for column in ("GEOID", "NAMELSAD", "geometry") if column in gdf.columns]
    gdf = gdf[keep_columns]
    return gdf


def enrich_with_stats(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Enrich each block-group polygon with median appraised value and parcel count.

    Notes on county schema differences:
    - DCAD stores appraised value in appraisal.tot_val and joins on account_num.
    - TAD/Collin/Denton store appraised value in county table total_value.
    - All 4 county parcel tables expose centroid geometry in EPSG:4326.
    """
    print("Enriching block groups with median appraised value...")
    settings = {
        "host": os.environ.get("DB_HOST", "").strip(),
        "port": (os.environ.get("DB_PORT", "5432") or "5432").strip(),
        "user": os.environ.get("DB_USER", "").strip(),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_NAME", "").strip(),
    }
    missing = [name for name in ("host", "user", "password", "dbname") if not settings[name]]
    if missing:
        raise RuntimeError(
            "Stat enrichment requires DB_HOST/DB_USER/DB_PASSWORD/DB_NAME env vars "
            "(same config as api/config.py)."
        )

    if "GEOID" not in gdf.columns:
        raise ValueError("Cannot enrich stats: GEOID column missing")

    conn = psycopg2.connect(**settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TEMP TABLE bg_polys (
                    geoid TEXT PRIMARY KEY,
                    geom GEOMETRY(MULTIPOLYGON, 4326)
                ) ON COMMIT DROP
                """
            )
            for row in gdf.itertuples(index=False):
                geom_wkb = row.geometry.wkb_hex
                geoid = str(getattr(row, "GEOID"))
                cur.execute(
                    """
                    INSERT INTO bg_polys (geoid, geom)
                    VALUES (%s, ST_Multi(ST_SetSRID(ST_GeomFromWKB(decode(%s, 'hex')), 4326)))
                    """,
                    (geoid, geom_wkb),
                )

            cur.execute(
                """
                WITH all_parcels AS (
                    SELECT p.centroid AS pt, a.tot_val::numeric AS tot_val
                    FROM parcels p
                    JOIN appraisal a ON a.account_num = p.account_num
                    WHERE p.centroid IS NOT NULL
                      AND a.tot_val IS NOT NULL

                    UNION ALL

                    SELECT t.centroid AS pt, t.total_value::numeric AS tot_val
                    FROM tad_parcels t
                    WHERE t.centroid IS NOT NULL
                      AND t.total_value IS NOT NULL

                    UNION ALL

                    SELECT c.centroid AS pt, c.total_value::numeric AS tot_val
                    FROM collin_parcels c
                    WHERE c.centroid IS NOT NULL
                      AND c.total_value IS NOT NULL

                    UNION ALL

                    SELECT d.centroid AS pt, d.total_value::numeric AS tot_val
                    FROM denton_parcels d
                    WHERE d.centroid IS NOT NULL
                      AND d.total_value IS NOT NULL
                ),
                joined AS (
                    SELECT bg.geoid, ap.tot_val
                    FROM bg_polys bg
                    JOIN all_parcels ap ON ST_Within(ap.pt, bg.geom)
                )
                SELECT geoid,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY tot_val) AS median_val,
                       COUNT(*)::int AS parcel_count
                FROM joined
                GROUP BY geoid
                """
            )
            stats = {
                str(row[0]): {
                    "median": float(row[1]) if row[1] is not None else None,
                    "count": int(row[2] or 0),
                }
                for row in cur.fetchall()
            }
    finally:
        conn.close()

    print(f"Stats computed for {len(stats)} of {len(gdf)} block groups")
    gdf["median_appr_val"] = gdf["GEOID"].map(lambda geoid: stats.get(str(geoid), {}).get("median"))
    gdf["parcel_count"] = gdf["GEOID"].map(lambda geoid: stats.get(str(geoid), {}).get("count", 0))
    return gdf


def write_geojson(gdf: gpd.GeoDataFrame) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(OUTPUT_PATH, driver="GeoJSON", engine="pyogrio")
    size_mb = OUTPUT_PATH.stat().st_size / (1024 * 1024)
    print(f"Wrote {OUTPUT_PATH} ({size_mb:.1f} MB, {len(gdf)} features)")


def main() -> None:
    if OUTPUT_PATH.exists():
        size_mb = OUTPUT_PATH.stat().st_size / (1024 * 1024)
        print(f"Output already exists: {OUTPUT_PATH} ({size_mb:.1f} MB). Delete it to rebuild.")
        return

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = download_tiger_zip()
    shp_path = extract_tiger_zip(zip_path)
    gdf = load_block_groups(shp_path)
    gdf = enrich_with_stats(gdf)
    write_geojson(gdf)


if __name__ == "__main__":
    main()
