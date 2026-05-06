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
11. [Deploying a Branch Preview](#deploying-a-branch-preview)
12. [Developer Reference](#developer-reference)

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
| `SESSION_DATABASE_URL` | Connection string for saved areas, tags, and auth |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to the GCS service account JSON file |
| `GCS_BUCKET` | Name of the Google Cloud Storage bucket holding the PMTiles file |
| `SESSION_SECRET` | Random secret for signing session cookies (generate with `python -c "import secrets; print(secrets.token_hex(32))"`) |
| `BOOTSTRAP_DEV_EMAIL` | Email for the first developer account (created on first startup if DB is empty) |
| `BOOTSTRAP_DEV_PASSWORD` | Password for the developer account |
| `BOOTSTRAP_OWNER_EMAIL` | Email for the owner account (optional — add when client email is known) |
| `BOOTSTRAP_OWNER_PASSWORD` | Password for the owner account (optional) |
| `AUTH_COOKIE_SECURE` | Set to `false` for local HTTP dev; defaults to `true` (HTTPS only) |
| `TRUST_PROXY` | Set to `true` on Cloud Run so X-Forwarded-For IP is used for rate limiting |

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

**Filters** — checkboxes in the sidebar let you show/hide each parcel type (Vacant, SFR, Off-Market, Multifamily, Commercial, Exempt) in both browse and draw modes.

**Sold comps** — after a draw, the sidebar shows a panel of recently sold properties inside the polygon (DeepFin data). Filter by sold date window, price tier, or year built. Purple outlines on the map indicate parcels with a matched sold comp nearby.

**Tagging** — click any parcel in the sidebar to expand it, use the tag buttons (Interested, Not Interested, etc.)

**Export** — click Download CSV in the sidebar to export all results with tags. Sold comp data (price, date, $/sqft, beds/baths, DOM, listing URL) appears as inline columns on the right side of each parcel row.

**Address search** — type any address in the search bar to jump to a parcel. Searches all four counties.

**Saved areas** — click Save Area to name and save your polygon. Click it again later to fly back and restore the draw.

---

## Team Access Setup

LotLedger has a built-in login system. No Nginx or HTTP basic auth needed.

### Adding a New User

Log in as owner or developer, click your name in the top-right of the sidebar, and open the **Admin Panel**. Click **Create User**, fill in their email and a temporary password, and set their role:

| Role | What they can do |
|---|---|
| `member` | Full map access — draw, tag, export, saved areas |
| `owner` | Member access + create/disable/reset users |
| `developer` | Full access to everything, immutable role |

The new user will be prompted to set their own password on first login.

### Disabling a User

Open Admin Panel → click **Disable** next to the user. Their session is invalidated immediately.

### Resetting a Password

Open Admin Panel → click **Reset Password** next to the user. You'll get a temporary password to share with them securely.

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

## Deploying a Branch Preview

> Use this when you want to test a feature branch on Cloud Run without merging it to `develop`.
> The preview lives at its own URL and runs alongside `lot-ledger-dev` (which keeps tracking `develop`).

> **Recommended terminal: [Google Cloud Shell](https://shell.cloud.google.com).** It's a free Linux VM in your browser, with `gcloud` pre-installed and pre-authenticated against your Google account. Every `gcloud` command in this section runs there with no setup. **Avoid pasting `gcloud` commands into PowerShell, CMD, or WSL** — terminal paste mangling (line wraps becoming literal newlines mid-command) is a recurring failure mode and Cloud Shell doesn't have it. WSL is still right for actual development work; Cloud Shell is right for one-off admin like this.

### Format rules for every command in this section

- **Every command is a single line.** No backslash continuations (`\`). Paste each command as-is.
- If your terminal still wraps long commands onto multiple lines visually, that's fine — just don't insert your own newlines or trailing whitespace.
- One command per code block. Run each, verify, then move to the next.

### How it works

| Service | Branch | URL pattern | Auto-deploys? |
|---|---|---|---|
| `lot-ledger-dev` | `develop` | `https://lot-ledger-dev-…run.app` | yes (Cloud Build trigger on push) |
| `lot-ledger-preview` | any feature branch | `https://lot-ledger-preview-…run.app` | no (manual `gcloud builds submit`) |

Both services share the same Cloud SQL database. Schema migrations run on startup via `_ensure_session_schema()`, so additive migrations on a preview branch will be visible to `lot-ledger-dev` after its next pod restart. **Do not preview a branch with destructive migrations against the shared DB** — spin up a separate Cloud SQL instance first or run the migration only against a throwaway DB.

### One-time setup — create the preview service

You only do this once per GCP project. After that, every preview deploy is a single command.

#### Step 1 — Authenticate and set the project

In Cloud Shell, you're already authed — skip the login command. In a local terminal:

```
gcloud auth login
```

Then in either:

```
gcloud config set project lot-ledger
```

#### Step 2 — Grant the build service account the permissions Cloud Build needs

This is the single most-likely-to-be-missed step. Post-2024 GCP projects use the **Compute Engine default service account** (`<projectNumber>-compute@developer.gserviceaccount.com`) for `gcloud builds submit`. By default it has NONE of the permissions Cloud Build needs — and the legacy Cloud Build SA that older projects auto-set-up isn't used here. **Three separate grants are required.** Missing any one produces a different cryptic error during the first deploy.

> If your project number isn't `505466930182`, find yours with `gcloud projects describe lot-ledger --format='value(projectNumber)'` and substitute throughout. The SA name pattern is always `<projectNumber>-compute@developer.gserviceaccount.com`.

**Grant 1 — Cloud Build builder role (storage, Artifact Registry, build pub/sub, logging):**

```
gcloud projects add-iam-policy-binding lot-ledger --member=serviceAccount:505466930182-compute@developer.gserviceaccount.com --role=roles/cloudbuild.builds.builder
```

Without this: `storage.objects.get access denied` when uploading the source tarball.

**Grant 2 — Cloud Run deploy permission:**

```
gcloud projects add-iam-policy-binding lot-ledger --member=serviceAccount:505466930182-compute@developer.gserviceaccount.com --role=roles/run.developer
```

Without this: `Permission 'run.services.get' denied on resource ...` when the build hits the deploy step. The `cloudbuild.builds.builder` role does NOT include Cloud Run permissions, despite the name.

**Grant 3 — Permission to act as the runtime service account:**

The Cloud Run service runs as a custom runtime SA (`lot-ledger-run@lot-ledger.iam.gserviceaccount.com` in this project) that holds Cloud SQL / Secret Manager / Artifact Registry reader bindings. The build SA needs explicit permission to deploy *as* the runtime SA — this is granted at the SA level, not the project level:

```
gcloud iam service-accounts add-iam-policy-binding lot-ledger-run@lot-ledger.iam.gserviceaccount.com --member=serviceAccount:505466930182-compute@developer.gserviceaccount.com --role=roles/iam.serviceAccountUser
```

Without this: `User does not have permission to act as service account ...` when the deploy step tries to start a revision.

After all three: each command should print `Updated IAM policy for ...` followed by a yaml dump of bindings.

> **Tip for paste-mangling terminals:** any of these failing with `command not found` or `--member: not found` means the line wrapped on paste. Write the command to `/tmp/grant.sh` first and `bash` it — see "Common errors" at the bottom.

#### Step 3 — Export the dev service config

This grabs all the env vars, secrets, and Cloud SQL connections from `lot-ledger-dev` so the preview service mirrors it exactly:

```
gcloud run services describe lot-ledger-dev --region=us-central1 --format=export > /tmp/preview-svc.yaml
```

> If the command prompts to enable `cloudresourcemanager.googleapis.com`, type `y` and wait — first-time projects need this API on. The prompt is normal and only appears once.

Sanity-check the file:

```
head -20 /tmp/preview-svc.yaml
```

You should see `name: lot-ledger-dev` near the top.

#### Step 4 — Rename the service in the exported file

Easiest one-line approach (replaces every occurrence of `lot-ledger-dev` with `lot-ledger-preview`, which is correct for the `name` field, the revision-name prefix if present, and the `urls:` annotation — Cloud Run regenerates the URLs annotation on creation regardless):

```
sed -i 's/lot-ledger-dev/lot-ledger-preview/g' /tmp/preview-svc.yaml
```

Verify the rename:

```
grep "name:" /tmp/preview-svc.yaml
```

The first match should be `name: lot-ledger-preview`. Other matches will be env-var names and container internals — leave those alone.

#### Step 5 — Create the preview service

```
gcloud run services replace /tmp/preview-svc.yaml --region=us-central1
```

You should see `Service [lot-ledger-preview] revision [...] has been deployed`. The new service starts with a placeholder image (whatever was on `lot-ledger-dev` at export time). The first real deploy in the next section replaces that image.

#### Step 6 — Make the preview URL publicly accessible

`gcloud run services replace` doesn't carry over IAM bindings, so the new service is locked down by default. Match `lot-ledger-dev`'s public reachability:

```
gcloud run services add-iam-policy-binding lot-ledger-preview --region=us-central1 --member=allUsers --role=roles/run.invoker
```

You should see `Updated IAM policy for service [lot-ledger-preview]`.

> If your `lot-ledger-dev` is locked down to specific IAM members instead of `allUsers`, copy that binding pattern here.

#### Step 7 — Get the preview URL and bookmark it

```
gcloud run services describe lot-ledger-preview --region=us-central1 --format='value(status.url)'
```

Bookmark the URL it prints (e.g. `https://lot-ledger-preview-505466930182.us-central1.run.app`). It stays the same across every preview deploy.

### Deploying a branch — the everyday workflow

When you have a feature branch you want to preview, run these from a **local clone of the repo** (not Cloud Shell — Cloud Shell is for the GCP setup commands; the build needs your local working tree). The actual `gcloud builds submit` command can be run from either, but a local clone makes the source-tree handling simpler.

#### Step 1 — Make sure your branch has the deploy infrastructure

`cloudbuild-preview.yaml` lives on `develop`. If your feature branch was created before that file existed, merge develop in:

```
git checkout your-feature-branch
```

```
git merge develop --no-edit
```

The `--no-edit` flag accepts the default merge commit message and skips the editor — without it, git opens nano or vim depending on your environment, which is easy to fumble (especially typing vim commands like `:wq` into nano).

```
git push origin your-feature-branch
```

#### Step 2 — Submit the build

```
gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger
```

Cloud Build packages your working tree, builds the container, tags it `:preview-<build-id>` and `:preview`, pushes to Artifact Registry, and deploys to `lot-ledger-preview`. Takes 3–5 minutes.

When it finishes you'll see a `URL: https://lot-ledger-preview-...` line. Refresh that URL to see the new build.

### Checking what's currently deployed

```
gcloud run services describe lot-ledger-preview --region=us-central1 --format='value(spec.template.spec.containers[0].image)'
```

The `:preview-<build-id>` suffix tells you which build is live. Cross-reference with `gcloud builds list --limit=10` to find when it was deployed.

### Rolling back

Easiest: redeploy a known-good branch.

```
git checkout main
```

```
gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger
```

Or pin directly to a previous image:

```
gcloud run services update lot-ledger-preview --region=us-central1 --image=us-central1-docker.pkg.dev/lot-ledger/lot-ledger-api/api:preview-<old-build-id>
```

### Tearing it down

When you're done with branch previews for a while:

```
gcloud run services delete lot-ledger-preview --region=us-central1
```

The service is gone, no ongoing cost. Re-do the one-time setup next time you need a preview environment.

### Common errors and fixes

These are every error we actually hit setting this up the first time, in roughly the order they appeared.

**`storage.objects.get access denied` on `gcloud builds submit`** →
Missing Grant 1. Run the `roles/cloudbuild.builds.builder` command from Step 2 of one-time setup.

**`Permission 'run.services.get' denied on resource 'namespaces/<project>/services/lot-ledger-preview'`** →
Missing Grant 2. Run the `roles/run.developer` command from Step 2. Note: this fails in ~4 seconds (an immediate API rejection, not a startup timeout). If your build step 3 fails at exit 1 in under 10s, this is almost certainly the cause.

**`User does not have permission to act as service account lot-ledger-run@...`** →
Missing Grant 3. Run the `roles/iam.serviceAccountUser` command from Step 2. This is the SA-level grant (not project-level) — the runtime SA name appears literally in the command.

**Build fails at the `gcloud run deploy` step with image tag like `:preview-` (empty after the dash)** →
The `cloudbuild-preview.yaml` is using a substitution variable that's empty for manual builds. `$SHORT_SHA` and `$COMMIT_SHA` are only populated for Git-trigger builds, not for `gcloud builds submit` from a working directory. Both image references in the file must use `$BUILD_ID` (always populated). If you cloned an older copy, search the file for `$SHORT_SHA` and replace with `$BUILD_ID`.

**Cloud Run rejects deploy with "service must be unique" or similar** →
You're trying to create `lot-ledger-preview` and it already exists. Skip Step 5 (services replace) and go straight to deploying with `gcloud builds submit`.

**`gcloud builds log <id>` only prints a deprecation warning, no log content** →
The streaming log subcommand is flaky. To pull just the failed step's output:

```
gcloud logging read "resource.type=build AND resource.labels.build_id=<id>" --limit=2000 --format="value(textPayload)" --project=lot-ledger --order=asc | grep "Step #3"
```

Or `gcloud builds describe <id>` for the structured build object (includes step exit codes and failureInfo). Or open `https://console.cloud.google.com/cloud-build/builds;region=global/<id>?project=lot-ledger` for the inline log view.

**Auth session from `lot-ledger-dev` doesn't work on `lot-ledger-preview`** →
Expected — different domains have different cookie jars. Log in fresh on the preview URL.

**`gcloud` command paste breaks with `unrecognized arguments` or `command not found` even though the command looks right** →
Your terminal is inserting literal newlines where the line wrapped visually. Reliable workaround: write the command to a temp script and run it. Example:

```
cat > /tmp/cmd.sh <<'EOF'
gcloud projects add-iam-policy-binding lot-ledger --member=serviceAccount:505466930182-compute@developer.gserviceaccount.com --role=roles/run.developer
EOF
bash /tmp/cmd.sh
```

The `<<'EOF'` heredoc preserves literal content with no shell interpolation. Paste-safe regardless of how mangled the multiline pastes get. This trick works for any long gcloud command.

Or use [Cloud Shell](https://shell.cloud.google.com) which doesn't have the paste-mangling problem at all.

**Editor opens on `git merge` and you don't know how to save** →
Use `git merge develop --no-edit` to skip the editor entirely (recommended). If you're already stuck in one:
- **nano** (the bottom of the screen shows `^O`, `^X` etc.): `Ctrl+O`, `Enter`, `Ctrl+X`
- **vim** (no help bar, lots of `~` symbols): `Esc`, type `:wq`, `Enter`
- The default merge commit message is fine — don't change it. If you accidentally typed something into the message body, delete it before saving.

### Gotchas to keep in mind

- **Shared DB**: see the warning above. Phase 1 of the saved-sessions feature is additive and safe. Phase 3's `session_tags` PK migration is destructive — do not preview Phase 3 against the shared DB.
- **Env vars drift**: if you add a new env var to `lot-ledger-dev` (via the Cloud Run console or `gcloud run services update`), the preview service won't pick it up automatically. Re-run Steps 3–5 of the one-time setup to refresh the preview's config.
- **Cost**: preview is a real Cloud Run service. Idle, it's near-free. Active, it bills like dev. If you forget to tear it down, it costs the same as keeping a second dev environment around — usually pennies a day at this scale, but worth knowing.

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
