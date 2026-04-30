# LotLedger

LotLedger is a Dallas parcel analysis web app for acquisition teams.

Core flow:
1. Draw an area on the map.
2. Review parcel intelligence and overlays.
3. Export clean CSV for spreadsheet workflows.

## What It Is

- Internal analyst tool for fast parcel triage.
- County-data-first platform (DCAD-backed).
- Map + export workflow, not a consumer home search app.

## What It Is Not

- Not a CRM.
- Not a consumer listing portal.
- Not an automated valuation model.

## Live Feature Set

- Polygon-based parcel analysis.
- Exact drawn-boundary filtering.
- Parcel type color classification.
- Best-effort active listing overlay.
- HOA boundaries overlay with HOA URL context.
- Street/Satellite basemap switcher (with labels on satellite).
- Verification brushes:
  - Verify Vacant
  - Verify Not Vacant
- CSV export including Verified Vacant, HOA, and HOA URL columns.

## Color Meaning

- Red: active listing
- Blue: off-market single family
- Green: vacant lot
- Purple: multifamily
- Orange: commercial
- Gray: exempt

## Stack

### Backend
- Python 3.11+
- FastAPI
- asyncio
- psycopg2-binary

### Database
- Google Cloud SQL (PostgreSQL + PostGIS)
- Spatial functions: ST_Intersects, ST_Within, ST_MakeEnvelope, ST_AsGeoJSON
- GIST indexes for geometry performance

### Frontend
- Vanilla JavaScript
- Leaflet.js
- Leaflet.draw
- Custom Leaflet controls for toolbox and basemap switching
- Custom badge rendering for verification marks

### Hosting
- Render (service + auto deploy)

## Data Sources

- DCAD appraisal/account/land/residential/exemption exports
- DCAD parcel geometry shapefile
- City of Dallas HOA boundaries GeoJSON
- Redfin listing grid endpoint (best-effort)

## Project Layout

- api/main.py: API routes, analyze/download, job-scoped data
- api/dcad.py: parcel query, joins, classification, feature build
- api/redfin.py: Redfin pull logic
- api/geo.py: geometry helpers
- api/config.py: environment and database pool
- scripts/build_db.py: DCAD loader/build pipeline
- scripts/load_hoa.py: HOA boundary loader
- frontend/index.html: app shell
- frontend/map.js: map behavior, tools, verification brushes, export trigger
- frontend/style.css: styling
- docs/BRIEFING.md: current-state stakeholder briefing
- docs/ROADMAP.md: prioritized roadmap

## Local Setup

1. Create and activate virtual environment.
2. Install dependencies.

```bash
pip install -r requirements.txt
```

3. Create environment file.

```bash
cp .env.example .env
```

4. Fill required env vars:
- DB_HOST
- DB_PORT
- DB_NAME
- DB_USER
- DB_PASSWORD
- PORT

5. Start API.

```bash
uvicorn api.main:app --reload
```

6. Open:
- http://localhost:8000

## Build or Refresh DCAD Database

```bash
python -m scripts.build_db
```

The loader uses upserts so reruns update existing records.

## Refresh HOA Boundaries

```bash
python -m scripts.load_hoa
```

## Health Endpoints

- /health
- /health/db

## CSV Export Notes

- Generated on demand from the latest analysis job.
- Downloaded in-browser.
- Filename is user-editable at export time.

## Operational Notes

- Redfin overlay can be unavailable; core parcel analysis still works.
- First request after Render idle can be slower.
- County data can have lag/parity edge cases (for example parcel split behavior).

## Troubleshooting

If map is empty:
1. Check /health
2. Check /health/db
3. Verify env vars

If parcel geometry looks wrong:
1. Confirm latest build completed
2. Verify source PARCEL_GEOM files
3. Rebuild database

If export looks wrong:
1. Re-run a known test polygon
2. Spot-check rows against source records
