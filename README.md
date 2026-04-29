# LotLedger

LotLedger is a Dallas property analysis web app for acquisition teams.

Draw an area on the map and get every parcel in that area with:
- ownership and mailing info
- DCAD values and lot metrics
- property classification colors
- live active listing overlay (best effort)
- one-click CSV export for spreadsheet workflows

The product goal is speed: turn a manual multi-tab research process into one map action and one export.

## What It Is Not

- Not a consumer home search app
- Not a CRM
- Not an automated investment model

It is an internal team tool for fast parcel triage.

## How It Works

1. User draws a polygon on the Dallas map.
2. Backend queries DCAD parcel records from PostGIS in Google Cloud SQL.
3. Backend pulls Redfin active listing addresses concurrently.
4. Parcels render by type on the map with popup details.
5. Sidebar shows off-market SFR rows sorted by land percent.
6. User exports CSV for Excel analysis.

## Color Meaning

- Red: active listing
- Blue: off-market single family
- Purple: multifamily
- Green: vacant residential lot
- Orange: commercial
- Gray: exempt

## Stack

- Backend: FastAPI, Python 3.11+
- Database: Google Cloud SQL (PostgreSQL + PostGIS)
- DB driver: psycopg2-binary
- Frontend: vanilla JavaScript, Leaflet, Leaflet.draw
- Hosting: Render

## Project Layout

- api/main.py: API routes, analyze/download flow, in-memory job store
- api/dcad.py: parcel query, classification, counts, feature build
- api/redfin.py: async Redfin grid pull
- api/geo.py: geometry helpers
- api/config.py: environment + DB pool
- scripts/build_db.py: one-time and repeatable DCAD loader
- frontend/index.html: app shell
- frontend/map.js: map logic, draw flow, sidebar, export trigger
- frontend/style.css: app styling

## Local Setup

1. Create and activate virtual environment.
2. Install dependencies.

pip install -r requirements.txt

3. Create environment file from template.

cp .env.example .env

4. Fill DB values in .env:
- DB_HOST
- DB_PORT
- DB_NAME
- DB_USER
- DB_PASSWORD
- PORT

5. Start app.

uvicorn api.main:app --reload

6. Open:

http://localhost:8000

## Build Or Refresh DCAD Database

Run this when loading fresh county data or after build-script changes:

python -m scripts.build_db

The loader uses upserts, so reruns update existing rows instead of duplicating tables.

## Health Endpoints

- /health
- /health/db

## CSV Export

- Export is generated on demand from analyzed rows.
- File is downloaded through the browser to the user machine.
- User can name the file at export time.

## Deployment

Configured for Render via render.yaml.

Live URL:
https://lot-ledger.onrender.com

## Known Operational Notes

- Redfin overlay is best effort and can be slower than DCAD parcel response.
- First request after Render idle can be slower.
- County data quality has edge cases; map coverage depends on centroid availability.

## Quick Troubleshooting

If map coverage looks off:
- Confirm build completed successfully.
- Check centroid coverage in SQL.
- Verify PARCEL_GEOM source files are complete and current.

If app is up but map is empty:
- Check /health and /health/db.
- Verify DB env vars in Render and local .env.

If CSV looks wrong:
- Run a known polygon and compare row-level output with reference POC.
- Confirm MLS status and land percent values on sample rows.
