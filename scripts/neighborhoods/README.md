# Neighborhood overlay pipeline (TIGER Block Groups)

One-shot build script. Produces `frontend/tx_block_groups.geojson` from US Census TIGER 2024 Block Groups, clipped to the 4 DFW counties.

## Run
    pip install -r requirements-dev.txt
    python scripts/neighborhoods/build.py

## Output
    frontend/tx_block_groups.geojson  (~3-8 MB after simplification)

## Plug out
Delete `scripts/neighborhoods/`, `ingest/neighborhoods/`, `frontend/tx_block_groups.geojson`, and `api/neighborhoods.py`.
Remove the import/include lines in `api/main.py` and the delimited `// === Neighborhood overlay ===` block in `frontend/map.js`.
