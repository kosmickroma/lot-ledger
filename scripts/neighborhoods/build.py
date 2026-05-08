# scripts/neighborhoods/build.py
# Role: one-shot TIGER 2024 block-group pipeline for the neighborhood overlay POC.
# Connects to:
#   ingest/neighborhoods/raw/         - cached TIGER zip and extraction dir
#   ingest/neighborhoods/processed/   - placeholder for intermediate files
#   frontend/tx_block_groups.geojson   - static output consumed by the toggle

from __future__ import annotations

from pathlib import Path
import urllib.request
import zipfile

import geopandas as gpd


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
    write_geojson(gdf)


if __name__ == "__main__":
    main()
