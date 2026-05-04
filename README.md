# LotLedger

LotLedger is a Dallas–Fort Worth parcel intelligence tool for real estate acquisition teams. Draw a polygon on the map, get every parcel inside color-coded by type, compare against active Redfin listings, tag promising parcels, and export an analyst-ready CSV.

**Counties covered:** Dallas (DCAD), Tarrant (TAD), Collin, Denton

---

## Table of Contents

1. [What You Need](#what-you-need)
2. [Running on Windows](#running-on-windows)
3. [Running on Mac or Linux](#running-on-mac-or-linux)
4. [Environment Variables](#environment-variables)
5. [Using the App](#using-the-app)
6. [Team Access Setup](#team-access-setup)
7. [Full Dev Setup with WSL2](#full-dev-setup-with-wsl2-db-rebuilds--pmtiles)
8. [Annual Data Refresh](#annual-data-refresh)
9. [Transferring to a New Machine](#transferring-to-a-new-machine)
10. [Transferring to a New Google Cloud Account](#transferring-to-a-new-google-cloud-account)
11. [Developer Reference](#developer-reference)

---

## What You Need

Before starting you need these four things:

1. **The code** — access to the private GitHub repository
2. **Database credentials** — the `DATABASE_URL` and `SESSION_DATABASE_URL` connection strings (ask the developer)
3. **Google Cloud Storage credentials** — a `credentials.json` service account file (ask the developer)
4. **Python 3.11 or newer** — free download from python.org

The app connects to a database that already has all the parcel data loaded. You do not need to build or load anything to get started.

---

## Running on Windows

> Gets the app running on your Windows computer in about 15 minutes.
> You do not need WSL2, Docker, or any Linux tools for this section.

### Step 1 — Install Python

1. Go to **python.org/downloads** and download Python 3.11 or newer
2. Run the installer
3. **On the first screen, check the box that says "Add Python to PATH"** before clicking Install
4. When finished, open **Command Prompt** (Windows key → type `cmd` → Enter)
5. Type `python --version` and press Enter — you should see `Python 3.11.x`

### Step 2 — Install Git

1. Go to **git-scm.com/download/win** and download Git for Windows
2. Run the installer, click Next through all the defaults
3. Close and reopen Command Prompt when finished
4. Type `git --version` — you should see `git version 2.x.x`

### Step 3 — Get the Code

In Command Prompt:

```
git clone https://github.com/YOUR-ACCOUNT/lot-ledger.git
cd lot-ledger
```

### Step 4 — Create a Virtual Environment

```
python -m venv .venv
.venv\Scripts\activate
```

You should now see `(.venv)` at the start of the line.

### Step 5 — Install Dependencies

```
pip install -r requirements.txt
```

Takes 1–3 minutes.

### Step 6 — Create Your .env File

In the `lot-ledger` folder, create a file called exactly `.env` (no other name, no `.txt` extension).

**How to create it on Windows:** Open Notepad, paste the content below, then File → Save As → navigate to the lot-ledger folder → set "Save as type" to "All Files" → name it `.env`

```
DATABASE_URL=postgresql://username:password@host:5432/lotledger
SESSION_DATABASE_URL=postgresql://username:password@host:5432/lotledger_sessions
GOOGLE_APPLICATION_CREDENTIALS=credentials.json
GCS_BUCKET=your-bucket-name
```

Fill in the values you received from the developer. Also copy the `credentials.json` file into the `lot-ledger` folder.

### Step 7 — Run the App

```
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

You should see `Application startup complete.`

Open your browser to **http://localhost:8000**

### Stopping and Starting

- Stop: press `Ctrl+C` in the Command Prompt window
- Start again: open Command Prompt, `cd lot-ledger`, run `.venv\Scripts\activate`, then the uvicorn command
- The Command Prompt window must stay open while the app is running

---

## Running on Mac or Linux

### Step 1 — Install Prerequisites

**Mac:**
```bash
brew install python@3.11 git
```
No Homebrew? Go to brew.sh for the one-line install.

**Ubuntu/Debian Linux:**
```bash
sudo apt update && sudo apt install python3.11 python3.11-venv git -y
```

### Step 2 — Get the Code and Run

```bash
git clone https://github.com/YOUR-ACCOUNT/lot-ledger.git
cd lot-ledger
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file with the same contents shown in the Windows section above.
Copy your `credentials.json` into the lot-ledger folder.

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Open browser to **http://localhost:8000**

---

## Environment Variables

All configuration lives in the `.env` file:

| Variable | What it is |
|---|---|
| `DATABASE_URL` | Connection string for the parcel database (all county data) |
| `SESSION_DATABASE_URL` | Connection string for saved areas and tags |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to the GCS service account JSON file |
| `GCS_BUCKET` | Name of the Google Cloud Storage bucket holding the PMTiles file |

Never share your `.env` file or `credentials.json`. Do not commit them to GitHub — they are in `.gitignore`.

---

## Using the App

**Browse mode** — the map loads with all parcels color-coded:

| Color | Meaning |
|---|---|
| Green | Vacant land |
| Yellow | Single family residential |
| Purple | Multifamily (apartments, condos) |
| Orange | Commercial |
| Gray | Exempt (government, HOA common areas, churches) |

**Draw mode** — click the polygon tool, draw a shape around any area, click the checkmark. Results load in the sidebar with counts by type.

**Tagging** — click any parcel in the sidebar to expand it, use the tag buttons (Interested, Not Interested, etc.)

**Export** — click Download CSV in the sidebar to export all results with tags

**Saved areas** — click Save Area to name and save your polygon. Click it again later to fly back and restore the draw.

---

## Team Access Setup

To give team members browser access with the ability to revoke it:

### HTTP Basic Auth (Recommended — One Password Per Person)

This requires the server to be running on Linux or WSL2 with Nginx installed.

**Install Nginx:**
```bash
sudo apt install nginx apache2-utils -y
```

**Create a password for each person:**
```bash
# First person (creates the file):
sudo htpasswd -c /etc/nginx/.htpasswd firstname.lastname

# Additional people (no -c flag):
sudo htpasswd /etc/nginx/.htpasswd secondperson
```

**Create `/etc/nginx/sites-available/lotledger`:**
```nginx
server {
    listen 80;
    server_name _;

    auth_basic "LotLedger";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

**Enable:**
```bash
sudo ln -s /etc/nginx/sites-available/lotledger /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx
```

**To revoke someone's access:**
```bash
sudo htpasswd -D /etc/nginx/.htpasswd firstname.lastname
sudo systemctl reload nginx
```

Done — their credentials stop working immediately.

---

## Full Dev Setup with WSL2 (DB Rebuilds & PMTiles)

> Only needed for database rebuilds and PMTiles generation.
> Tippecanoe (the tile build tool) only runs on Linux/Mac, so WSL2 is required on Windows for this.

### Install WSL2

Open PowerShell as Administrator:
```powershell
wsl --install
```

Restart when prompted. Then open **Ubuntu** from the Start menu and create a username and password.

### Set Up Inside WSL2

```bash
sudo apt update && sudo apt install python3.11 python3.11-venv git tippecanoe -y

git clone https://github.com/YOUR-ACCOUNT/lot-ledger.git
cd lot-ledger
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` and copy `credentials.json` into the folder (same as Quick Start).

**Accessing Windows files from WSL2:** your Windows C drive is at `/mnt/c/` inside WSL2.
Example: `C:\Users\Mike\Downloads\` becomes `/mnt/c/Users/Mike/Downloads/`

---

## Annual Data Refresh

Each year the county appraisal districts release new data. Update process:

> Run all commands from the lot-ledger folder with `.venv` active.
> Use WSL2 or Linux for this entire section.

### 1. Download New Data

- **DCAD (Dallas):** dcad.org → downloads
- **TAD (Tarrant):** tad.org → public data
- **Collin:** collincad.org → LiteDatabase annual export
- **Denton:** dentoncad.com → GIS/data downloads

Place files in `ingest/counties/{county}/cad/current/unzipped/`

### 2. Rebuild Each County

```bash
# Always do a dry run first (no --write-db), then add --write-db to actually write

python scripts/build_denton.py --snapshot-date 2026-01-01           # dry run
python scripts/build_denton.py --write-db --snapshot-date 2026-01-01  # write

python scripts/build_collin.py --snapshot-date 2026-01-01
python scripts/build_collin.py --write-db --snapshot-date 2026-01-01
```

### 3. Rebuild PMTiles

```bash
# Export all counties (~10-15 min)
PYTHONPATH=. python scripts/export_pmtiles.py

# Copy and run the tippecanoe command it prints (~45-60 min)
tippecanoe -o parcels.pmtiles ...

# Upload to GCS
gsutil cp parcels.pmtiles gs://YOUR-BUCKET/parcels.pmtiles
```

---

## Transferring to a New Machine

1. Install Python and Git (Steps 1–2 from Quick Start)
2. `git clone` the repository
3. Copy your `.env` file to the new machine's lot-ledger folder
4. Copy your `credentials.json` to the new machine's lot-ledger folder
5. Run `pip install -r requirements.txt`
6. Start the app

The parcel data lives in the cloud database. Moving machines takes about 10 minutes.

---

## Transferring to a New Google Cloud Account

To move the databases to a new GCP project (ownership transfer):

### Step 1 — Export from Current Database

```bash
pg_dump -h CURRENT_HOST -U postgres -d lotledger > parcels_backup.sql
pg_dump -h CURRENT_HOST -U postgres -d lotledger_sessions > sessions_backup.sql
```

### Step 2 — Set Up New Cloud SQL

In the new GCP project: Cloud SQL → Create Instance → PostgreSQL. Enable PostGIS after creation.

### Step 3 — Restore

```bash
psql -h NEW_HOST -U postgres -c "CREATE DATABASE lotledger;"
psql -h NEW_HOST -U postgres -c "CREATE DATABASE lotledger_sessions;"
psql -h NEW_HOST -U postgres -d lotledger < parcels_backup.sql
psql -h NEW_HOST -U postgres -d lotledger_sessions < sessions_backup.sql
```

### Step 4 — Update Env Vars

Update `DATABASE_URL` and `SESSION_DATABASE_URL` in `.env` to point to the new host. If running on Cloud Run, update the environment variables in the Cloud Run service settings.

Zero code changes required.

---

## Developer Reference

### Project Structure

```
api/
  main.py              — FastAPI app, all endpoints, CSV export, session handling
  config.py            — DB connection pools (parcel DB + session DB)
  geo.py               — polygon_bbox(), point_in_polygon()
  redfin.py            — async Redfin listing fetch
  counties/
    dcad.py            — Dallas County query, classification, normalization
    tad.py             — Tarrant County query, classification, normalization
    collin.py          — Collin County query, classification, normalization
    denton.py          — Denton County query, classification, normalization

scripts/
  build_db.py          — Ingest DCAD data into DB
  build_collin.py      — Ingest Collin CAD LiteDatabase into DB
  build_denton.py      — Ingest Denton GeoJSON into DB (streams via ijson)
  export_pmtiles.py    — Export all counties to GeoJSON, print tippecanoe command
  check_denton_codes.py — Utility: print state_cd distribution for Denton

frontend/
  index.html           — App shell, sidebar HTML
  map.js               — Map, draw, browse layer, popups, tags, export
  style.css            — Dark sidebar, toolbar, popup styles

docs/
  COUNTY_EXPANSION_CHECKLIST.md   — Step-by-step for adding a new county (proven on Collin + Denton)
  DENTON_BUILD_BRIEF.md           — Denton-specific build notes
  lot-ledger/                     — Planning docs and client notes
```

### Running in Development

```bash
source .venv/bin/activate           # Mac/Linux
.venv\Scripts\activate              # Windows

uvicorn api.main:app --reload       # restarts on file changes
```

### Branch Strategy

- `main` — production, must be stable
- `develop` — integration testing, staging environment
- Always develop on `develop`, merge to `main` only after smoke tests pass

### Adding a New County

Follow `docs/COUNTY_EXPANSION_CHECKLIST.md` in order. Proven against Collin and Denton. Next in sequence: Rockwall.

### Health Endpoints

- `GET /health` — `{"status": "ok"}`
- `GET /health/db` — `{"status": "ok", "db": "connected"}`

### Handing Off to a New Developer

1. Add them as collaborator on the private GitHub repo
2. Share `.env` securely (password manager, not email)
3. Share `credentials.json` the same way
4. They follow the Quick Start for their OS above
5. They read `docs/COUNTY_EXPANSION_CHECKLIST.md` before doing any county work
