# LotLedger

LotLedger is a Dallas–Fort Worth parcel intelligence tool for real estate acquisition teams.

Draw a polygon on the map, get every parcel inside color-coded by type, compare against active Redfin listings, tag promising parcels, and download a 32-column analyst-ready CSV.

---

## What It Does

**Browse layer** — On page load, every parcel in Dallas and Tarrant counties is visible on the map, color-coded and clickable. No draw needed. Rendered from a pre-built PMTiles vector tile file.

**Draw & analyze** — Draw any polygon shape. Results come back instantly: color-coded parcel outlines, sidebar counts by type, and a shortlist of off-market SFR sorted by land % of total value. Large areas tile automatically — no size cap.

**Saved areas** — Name and save drawn areas. They persist across sessions. Click to fly back and restore the polygon outline.

**Redfin overlay** — Active listings pulled async for the drawn area. Red circle markers, popup shows list price vs DCAD appraised value with dollar/percent delta.

**HOA boundaries** — 177 Dallas HOA polygons on demand. Hover for name and website URL.

**Verification & target tagging** — Vacant / Not Vacant / Target badges per parcel, all exported in the CSV.

**CSV export** — 32 columns, on-demand, user-editable filename. Includes Google Maps deep link per parcel.

**Multi-county** — Dallas (DCAD) and Tarrant (TAD) counties supported, routed automatically by polygon location.

---

## Color Reference

| Color | Meaning |
|---|---|
| Red | Active Redfin listing |
| Blue | Off-market single family |
| Green | Vacant lot |
| Purple | Multifamily (apartments, condos, duplexes) |
| Orange | Commercial |
| Gray | Exempt (church / school / government) |

---

## Stack

### Backend
- Python 3.11
- FastAPI + uvicorn
- psycopg2-binary (connection pool, maxconn=20)
- httpx (async Redfin fetch)

### Database
- Google Cloud SQL — PostgreSQL 18 + PostGIS
- Region: us-central1
- Spatial queries: `ST_Intersects`, `ST_Within`, `ST_MakeEnvelope`, `ST_AsGeoJSON`
- GIST indexes on all geometry columns

### Frontend
- Vanilla JavaScript (no build step)
- Leaflet 1.9.4 + Leaflet.draw 1.0.4
- protomaps-leaflet 3.1.2 (PMTiles canvas rendering)

### Hosting & Infrastructure
- Google Cloud Run (us-central1) — serverless container, scales to zero
- Google Cloud Storage — PMTiles tile file, public CORS
- Cloud Build — CI/CD on push to main (build → Artifact Registry → Cloud Run deploy)

---

## Data Sources

- DCAD appraisal/account/land/residential/exemption CSV exports (~850K parcels)
- DCAD parcel geometry shapefile
- TAD ParcelView shapefile (~700K parcels, normalized to EPSG:4326)
- City of Dallas HOA boundaries GeoJSON (177 boundaries)
- Redfin listing grid endpoint (live, best-effort)

---

## Project Layout

```
api/
  main.py              — FastAPI routes, in-memory job store, CSV builder
  config.py            — DB connection pool, Cloud Run Unix socket + local TCP modes
  geo.py               — polygon_bbox(), point_in_polygon() ray-cast
  redfin.py            — async Redfin grid fetch, USPS address normalization
  counties/
    dcad.py            — DCAD parcel query, classification, feature builder
    tad.py             — TAD parcel query, classification, row normalization

frontend/
  index.html           — app shell, CDN script tags, sidebar HTML
  map.js               — all client logic: map, draw, browse, popups, tags, export
  style.css            — dark sidebar, toolbar, popup and badge styles

scripts/
  build_db.py          — DCAD CSV → Postgres (upsert-safe)
  build_tad_db.py      — TAD ParcelView → Postgres (checkpoint-resumable)
  export_pmtiles.py    — all parcels → newline-delimited GeoJSON for tippecanoe
  load_hoa.py          — HOA shapefile → PostGIS
  reproject_tad_parcelview.py  — reproject TAD to EPSG:4326
  validate_tad_extract.py      — validate TAD folder before ingest

docs/                  — gitignored internal notes and specs
Dockerfile             — Python 3.11 container
cloudbuild.yaml        — Cloud Build CI/CD pipeline
requirements.txt       — Python dependencies
```

---

## Local Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in DB_HOST, DB_NAME, DB_USER, DB_PASSWORD
uvicorn api.main:app --reload
# → http://localhost:8000
```

---

## Rebuild DCAD Database

```bash
python -m scripts.build_db
```

Upsert-safe — re-run at any time to refresh from new DCAD export files.

---

## Build TAD (Tarrant County)

```bash
python3 scripts/validate_tad_extract.py
python3 scripts/reproject_tad_parcelview.py --overwrite
python3 scripts/build_tad_db.py --write-db --limit 100000 --fresh-start
python3 scripts/build_tad_db.py --write-db --resume
```

---

## Rebuild PMTiles Browse Layer

Run after any data ingest or classification change:

```bash
# 1. Export GeoJSON
python -m scripts.export_pmtiles --dcad-out dcad.geojsonl --tad-out tad.geojsonl

# 2. Build tiles (tippecanoe must be installed)
tippecanoe -o parcels.pmtiles -Z12 -z16 -pn \
  -y account_num -y prop_type -y situs_addr -y owner_name \
  -y appraised_val_current -y source_county \
  --coalesce-densest-as-needed --extend-zooms-if-still-dropping -f \
  -L '{"file":"dcad.geojsonl","layer":"dcad"}' \
  -L '{"file":"tad.geojsonl","layer":"tad"}'

# 3. Upload to GCS
gsutil cp parcels.pmtiles gs://lot-ledger-tiles/parcels.pmtiles
```

---

## Health Endpoints

- `GET /health` — `{"status": "ok"}`
- `GET /health/db` — `{"status": "ok", "db": "connected"}`

---

## Tarrant County Notes

TAD uses a dedicated `tad_parcels` table (PostGIS geometry, denormalized). Classification uses `property_class` codes (A1, B2, C1, etc.), not DCAD's `sptd_code`. Output normalized to the same color palette. Known field gaps vs DCAD: no frontage/depth, no zoning, school district shown as raw code.

## Operational Notes

- First request after Cloud Run scale-to-zero is slower (cold start ~2s).
- In-memory job store survives 30 min per session; restarts clear all jobs.
- Redfin overlay is unofficial and degrades gracefully on failure.
- PMTiles browse layer requires a full pipeline re-run (~45 min) when data changes.
