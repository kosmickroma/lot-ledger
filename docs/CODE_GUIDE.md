# LotLedger — Code Guide
# Plain English. Interview-ready. Updated after every chunk.
# Internal only — gitignored
# Last updated: 2026-05-01

---

## HOW TO USE THIS FILE

This file explains what every piece of the codebase does and WHY it was built that way.
If someone asks "walk me through the architecture" or "how does the tile engine work" or
"why PostGIS instead of a GeoJSON file" — the answers are here.

Every implementation chunk adds a new section. New code does not ship without an entry here.

---

## THE THIRTY-SECOND PITCH

LotLedger is a map-driven parcel intelligence tool. A real estate analyst opens a map,
sees every parcel in Dallas and Tarrant County color-coded by type (vacant, SFR, multifamily,
commercial, exempt), draws a polygon around an area they're interested in, and downloads
a CSV sorted by teardown potential. That is the entire product.

"Teardown potential" = land value as a percentage of total value. A parcel where the land
is worth 80% of the total value means the structure is nearly worthless — the play is to
buy the land and build new. The app surfaces those first.

---

## WHY THESE TECHNOLOGY CHOICES

**Why FastAPI (not Django, Flask, Express)?**
FastAPI is Python-native, fast, and supports async (needed for Redfin concurrent fetch).
It generates automatic API docs at /docs. The project started in Python because the
business logic (parcel classification) was already proven in a Python CLI tool.
Porting to a different language would have been pure risk with no benefit.

**Why PostgreSQL + PostGIS (not a flat file or SQLite)?**
550k Dallas parcels + 761k Tarrant parcels with polygon geometry. You need spatial
queries: "give me every parcel whose centroid is inside this polygon." PostGIS does this
with a GIST index in milliseconds. A flat file or SQLite would require loading everything
into memory and doing geometry math in Python — orders of magnitude slower.

**Why psycopg2 directly (not SQLAlchemy or an ORM)?**
ORMs abstract away SQL. For spatial queries with ST_Within, ST_Intersects, UNNEST, and
multi-table joins, the SQL is too specific and too important to hide behind an ORM.
Every query in this codebase is intentional and readable. psycopg2 is the standard
Python PostgreSQL driver — mature, fast, no surprises.

**Why Leaflet (not Google Maps, Mapbox, MapLibre)?**
Leaflet is open source with no per-request pricing. No API key tied to a credit card.
For a product that the client will eventually run on his own cloud, Leaflet means zero
vendor dependency for the map itself. Mapbox charges per tile load. Google Maps charges
per session. Leaflet is free forever.

**Why vanilla JS (not React)?**
One HTML page, one JS file, one CSS file. No build step, no node_modules, no webpack.
The app deploys by uploading three static files. Any developer can open map.js and
understand the entire frontend without knowing any framework. When you're the only
developer on a tool this focused, a build pipeline is overhead with no payoff.

**Why Cloud SQL + Cloud Run (not Render)?**
The database is on Google Cloud Platform (us-central1). The original app server was on
Render (AWS us-east-1). Every database query crossed AWS → GCP: 30-60ms overhead per
query, 5 queries per draw = 150-300ms of pure network waste. Cloud Run puts the app
server in the same GCP region as the database. Query latency drops to 1-3ms.

**Why PMTiles (not API-based viewport loading)?**
We researched building a "browse mode" that would load parcels from the API as the user
pans. The problems: (1) a zoom-13 viewport over Dallas would return ~80,000 parcels —
Leaflet's ceiling is ~10,000; (2) FastAPI/uvicorn does not cancel DB queries when the
browser cancels a request, so rapid panning would saturate the DB connection pool;
(3) Regrid, the company that literally sells county parcel data, uses vector tiles not
API polling. PMTiles is a single pre-built file. Zero API calls for rendering.

---

## THE DATA PIPELINE (How Parcels Get Into The Database)

### DCAD (Dallas County)

1. Dallas Central Appraisal District releases an annual data extract: CSV files
   (appraisal records, residential detail, land segments, exempt accounts) plus a
   shapefile (PARCEL_GEOM) containing polygon boundaries.

2. `scripts/build_db.py` reads these files and upserts them into Cloud SQL:
   - `parcels` table: account_num, owner_name, address, centroid point, polygon geometry
   - `appraisal` table: land_val, impr_val, tot_val
   - `res_detail` table: yr_built, living area
   - `land_detail` table: lot size, frontage, depth
   - `exempt_accounts` table: just account numbers with exemption code 14

   **Important geometry detail**: `polygon_geojson` in the `parcels` table is a **TEXT
   column** — it stores a GeoJSON string like `'{"type":"Polygon","coordinates":[...]}'`.
   It is NOT a PostGIS geometry column. This means you query it with `polygon_geojson::json`
   (plain JSON cast) — not `ST_AsGeoJSON(polygon_geojson)`. The reason: the original build
   script serialized geometry from a shapefile directly to JSON strings. The centroid IS a
   real PostGIS geometry column (queried with `ST_AsGeoJSON(centroid)`).
   TAD is different: `tad_parcels.geom` IS a native PostGIS geometry column and IS queried
   with `ST_AsGeoJSON(geom)`. The two counties store geometry differently.

3. Why five tables instead of one? The DCAD extract is already normalized this way —
   appraisal records include commercial properties not in res_detail, land_detail has
   multiple segments per parcel. Flattening them would lose data or create row explosions.
   At query time, `query_parcels()` reassembles them with a single JOIN (not 5 sequential
   round trips) — so storage stays normalized but reads are fast.

### TAD (Tarrant County)

1. Tarrant Appraisal District releases a ParcelView shapefile — a single denormalized
   shapefile with geometry + all attributes in one file.

2. `scripts/build_tad_db.py` reads the shapefile and upserts into a single
   `tad_parcels` table. One table works because TAD's shapefile is already flattened.

3. An enrichment script (`scripts/enrich_tad_standard_data.py`) fills in additional
   fields from a separate TAD Standard Data file (PropertyData_2026.txt, 1.99M rows).
   This runs separately because it's slow and only needed once.

---

## THE API (What Each Endpoint Does and Why)

### POST /api/analyze — The core endpoint

Takes a polygon (list of [lng, lat] coordinates), returns a GeoJSON FeatureCollection
of every parcel inside it.

What it does step by step:
1. Extracts the bounding box of the polygon (fast rectangle filter)
2. Queries DCAD and TAD in parallel (asyncio.gather) — each runs in its own thread
3. DCAD: single JOIN query across parcels + appraisal + res_detail + land_detail + exempt_accounts, then point-in-polygon filter in Python
4. TAD: single query against tad_parcels (already denormalized), then point-in-polygon filter
5. Redfin active listings fetched concurrently as an async task (doesn't block the DB queries)
6. Classifies every parcel (exempt → multifamily → vacant → commercial → SFR)
7. Merges Redfin matches by address normalization
8. Returns GeoJSON + counts + job_id

Why two-step bbox then point-in-polygon?
The GIST index makes the bbox query fast (~5ms for 550k parcels). The bbox over-selects
(returns parcels near the edges that might be outside the polygon). The point-in-polygon
step (ray-casting algorithm in geo.py) filters those out exactly. Doing exact polygon math
on all 550k parcels without the bbox pre-filter would be slow.

### GET /api/download/{job_id} — CSV export

Retrieves the job from `_job_store` (in-memory dict, 2-hour sliding-window TTL), streams a 31-column CSV.
"Streams" means it sends data as it generates rows rather than building the whole CSV in
memory first — important for large exports (10k+ parcels).

The TTL is sliding: every time the job is accessed (export, verification tag, target mark),
`last_accessed` is updated and the 2-hour window resets. An analyst actively working a
session will never hit the expiry. The clock only runs out if the browser is left idle for
two straight hours.

Why in-memory job store (not database)?
It was the simplest correct thing. The job data is temporary (one analyst session).
The risk: Cloud Run scales to zero when idle — restart wipes all jobs. Phase 3 replaces
this with a persistent Postgres table. For now the analyst must export before the app restarts.

### POST /api/merge-jobs — Tile engine support

When a large draw is split into tiles, each tile gets its own job_id. This endpoint
merges multiple tile job_ids into one canonical job_id for export. Without this, the
analyst would need to download 4 separate CSVs for a large draw.

### GET /api/hoa — HOA boundaries

Returns 177 Dallas HOA polygon boundaries as GeoJSON. Loaded from the
`hoa_boundaries` PostGIS table. Used for the HOA overlay toggle on the map.

### GET /health and GET /health/db — Monitoring

/health: confirms the app is running. /health/db: tests a real DB connection.
Cloud Run and monitoring tools hit /health to know if the service is up.

---

## THE TILE ENGINE (How Large Draws Work)

Defined in `frontend/map.js` starting around line 804.

**The problem**: A large polygon draw (say, all of East Dallas) hits a 30-second timeout
on the server because the DB query and Redfin fetch take too long for one request.

**The solution**: Split the bounding box into a 2×2 grid of tiles. Each tile is a smaller
polygon that gets its own independent request. Four smaller requests instead of one huge one.

**Threshold**: `TILE_AREA_THRESHOLD = 0.003` sq-degrees. If the bbox area exceeds this,
use the tile engine. Below it, single request.

**Adaptive refinement**: If a tile returns a 502 error (server overloaded), that tile
automatically splits into 4 sub-tiles. Max 2 levels of splitting, so worst case:
one failing quad becomes 16 micro-tiles. This is `fetchTileDataRecursive()`.

**Parallel fetching**: Tiles are fetched in parallel batches of up to 8 at a time.
Each tile request uses 2 DB connections (one DCAD thread + one TAD thread), so 8 tiles
= 16 connections, safely within the pool limit of 20. This was only safe to enable after
the DCAD query was collapsed to a single JOIN (previously each tile needed 5+ connections
for DCAD alone, which would have exhausted the pool). For the common 4-tile case, all 4
tiles fire simultaneously and the total time equals one tile's time, not four.

**Deduplication**: Tile boundaries overlap slightly. A parcel on the border of two tiles
would appear in both results. The engine deduplicates by `parcel_key` / `account_num`
before merging results.

---

## PARCEL CLASSIFICATION (The Business Logic — Do Not Change)

Defined in `api/dcad.py:classify_parcel()` and `api/tad.py:_classify_tad()`.

Priority order: **exempt → multifamily → vacant → commercial → single_family**

Why this order matters: a multifamily building that is also exempt (e.g., a nonprofit
apartment complex) should show as exempt. The first matching rule wins.

**Exempt checks (4 layers)**:
1. Account number is in the `exempt_accounts` table (DCAD exemption code 14 = fully exempt)
2. sptd_code = 'X11' (state exemption code)
3. Owner name contains a government keyword (CITY OF, ISD, COUNTY, etc.)
4. Owner name contains ' HOA' AND sptd_code is NOT a residential type

Why the HOA guard? The word "HOA" appears in the Vietnamese surname "HOANG." About 600
homeowners with that surname would be falsely marked exempt without the SPTD check.
The check says: only treat ' HOA' as an HOA if the parcel is not classified as
residential (A11, A12, A13, A20). A homeowner named Hoang with an A11 code stays SFR.

**Colors** (must match COLORS object in map.js exactly):
```
Blue   #2980b9  — single_family (off-market SFR)
Green  #27ae60  — vacant
Purple #8e44ad  — multifamily
Orange #e67e22  — commercial
Gray   #95a5a6  — exempt
Red    #e74c3c  — active Redfin listing (overrides above)
```

---

## THE RENDERING ENGINE (How Parcels Appear On The Map)

Defined in `frontend/map.js:renderFeatures()` at line 550.

**Two render paths**:
- Parcel HAS polygon geometry → filled `L.geoJSON` polygon. No dot. Polygon IS the click target.
- Parcel has NO polygon geometry → `L.circleMarker` dot (radius 5, or 7 for Redfin listings).

**Why no dot inside a polygon?**
Early versions put a dot at the centroid AND a polygon outline for every parcel. When
parcels are dense, dots and polygons from neighboring parcels would stack visually.
The rule "polygon fill = click target, no dot" was the fix. The centroid dot only appears
when there's no polygon data.

**Condo special case**:
Condos are individual units inside a building. Ten condo units share one building footprint.
If each unit rendered a filled polygon, you'd get 10 stacked filled shapes for one building.
The fix: condos render as outline-only (no fill), and only ONE outline per building
(deduped by geometry key). Click the outline → popup shows the first unit's data.

**Four deduplication guards** (all must be respected before adding any new render path):
1. `polygonGeometrySeen` — prevents stacked polygon fills for shared footprints
2. `condoOutlineSeen` — one condo outline per building, not one per unit
3. `duplicateNonCondoFootprint` — skip entire row if non-condo geometry already rendered
4. `accountRenderedAsPolygon` — if this account already has a polygon fill, skip its dot

Full rules documented in `docs/RENDERING_RULES.md`.

---

## ADDRESS MATCHING (How Redfin Listings Connect To Parcels)

Defined in `api/redfin.py:normalize_addr_key()`.

Redfin says "1234 MAIN ST". DCAD says "1234 MAIN STREET". They need to match.

The function:
1. Uppercases everything
2. Expands ~40 abbreviation pairs (ST→STREET, DR→DRIVE, BLVD→BOULEVARD, etc.)
3. Strips the last word if it's a directional or transport type token

Why strip the last word? DCAD sometimes includes "1234 MAIN STREET DALLAS" while Redfin
has "1234 MAIN STREET." Stripping "DALLAS" from the end makes them match.

**The ON_REDFIN guard**:
Address matching could falsely mark ALL units in a condo building as "active listing"
if one unit is for sale. The guard: only flag a parcel as on_redfin if its parcel_key
matches exactly (direct account_num match). No GIS fallback. One listed unit does not
infect the whole building.

---

## LAYER ARCHITECTURE (What Goes On What Layer)

```
drawLayer            — polygon outline drawn by the analyst
pmtilesLayer         — PMTiles browse layer (all parcels, always visible) [PHASE 2.5]
markerLayer          — Draw Mode results: polygon fills + dots + condo outlines
redfinLayer          — Redfin-linked parcels when toggle is ON
verificationBadgeLayer — ✓/✗ vacancy verification badges
targetBadgeLayer     — ★ potential target badges
```

Why separate layers? So they can be toggled independently. The Redfin layer can be
hidden without clearing parcel results. Badges can be cleared without losing parcel data.
PMTiles is separate so Draw Mode can clear its results without wiping the browse layer.

Layer order matters: layers added first render below layers added later. PMTiles (browse)
renders below markerLayer (draw results). If the same parcel appears in both, the draw
result renders on top.

---

## CHUNK 1 — Branch Setup + Code Organization (2026-05-01)

### County module structure: why api/counties/

The app started with two county modules sitting flat in the api/ directory:
`api/dcad.py` and `api/tad.py`. With Collin, Denton, and Rockwall counties coming,
that pattern would become a mess of flat files.

The fix: a `counties/` sub-package inside `api/`.

```
api/
  config.py          ← shared DB connection (not county-specific)
  geo.py             ← shared geometry math (not county-specific)
  redfin.py          ← separate data source (not a county)
  main.py            ← routes — ties everything together
  counties/
    __init__.py
    dcad.py          ← Dallas (was api/dcad.py)
    tad.py           ← Tarrant (was api/tad.py)
    collin.py        ← future
    denton.py        ← future
```

Adding a new county means: create `api/counties/newcounty.py`, wire it into
`api/main.py`. Nothing else changes. Each county module is self-contained.

Why not move geo.py and redfin.py into counties/?
- `geo.py` is pure math (point_in_polygon, polygon_bbox) — used by every county
- `redfin.py` is a separate data source, not a county appraisal district
- `config.py` is shared infrastructure
These belong at the api/ level, not inside a county-specific package.

### Dead code removed: summarize_counts()

`summarize_counts()` existed in `api/counties/dcad.py` but was never called from
anywhere in the codebase. `api/main.py` has its own inline count loop. The function
was a holdover from an earlier design where counting was going to be delegated to
the DCAD module. Since main.py took over that responsibility, the function became
unreachable. Removed.

### Connection pool: maxconn raised from 10 to 20

The original pool of 10 was sized for a single Render instance with moderate load.
Cloud Run can serve multiple concurrent requests (and may run multiple instances).
At maxconn=10 with multi-analyst Browse Mode use, the pool would exhaust under load.
Raised to 20. At Cloud Run max-instances=10, theoretical maximum = 200 connections.
Cloud SQL max_connections should be set to 200+ before launch.

### Cloud Run connection mode: Unix socket auto-detection

`api/config.py` now auto-detects its environment:
- If `INSTANCE_UNIX_SOCKET` env var is set → connect via Unix socket (Cloud Run)
- Otherwise → connect via TCP to `DB_HOST` (local development)

Cloud Run injects `INSTANCE_UNIX_SOCKET` automatically when `--add-cloudsql-instances`
is set on the service. No manual configuration needed. Local dev is unchanged.

### Branch strategy

- `main` → Render (frozen at the 2026-05-01 stability patch, do not push features)
- `develop` → Cloud Run (all new work goes here, PRs eventually merge to main)

Git history is preserved on the moved files (`git mv` not delete+create).

---

## WHAT GETS ADDED HERE WITH EACH CHUNK

| Chunk | What to document | Status |
|---|---|---|
| Chunk 1: Branch + cleanup | County module structure, why api/counties/, dead code removed | DONE |
| Chunk 2: Cloud Run | Cloud Run config, Cloud SQL Auth Proxy, why service account not public IP | pending |
| Chunk 3: Cloud Build | CD pipeline, branch → service mapping | pending |
| Chunk 4: PMTiles frontend | protomaps-leaflet, paint rules, layer order, click event pattern | pending |
| Chunk 5: Export + tiles | Export query design, tippecanoe flags and why, GCS CORS setup | pending |
| Chunk 6: Click popup API | /api/parcel endpoint design, why lazy fetch vs embedded fields | DONE |

---

## CHUNK 6 — CLICK POPUP API (2026-05-02)

### What was added

New endpoint in `api/main.py`:

- `GET /api/parcel/{county}/{account_num}`

Behavior:

1. Validates `county` is `dcad` or `tad` (otherwise 400)
2. Fetches one parcel by `account_num` from the correct county dataset
3. Reuses existing normalization/classification/feature-builder logic
4. Returns one GeoJSON Feature in the same shape as each feature in `/api/analyze`
5. Returns 404 if parcel is not found

### Why this exists

PMTiles intentionally carries a small property payload for speed. On click, the UI only
knows lightweight identifiers like `account_num` and county. The popup still needs full
parcel detail (owner, values, year built, school, etc.).

This endpoint provides that on demand:

1. Click parcel on PMTiles layer
2. Hit-test returns county + account
3. Frontend calls `/api/parcel/{county}/{account_num}`
4. Backend returns a full feature payload for popup rendering

### Why lazy fetch was chosen over embedding everything in tiles

- Embedding full popup fields in PMTiles would make tiles significantly larger.
- Larger tiles mean slower initial load and more bandwidth.
- Lazy fetch keeps the map fast while still giving full detail when the analyst asks for it.

### County-specific implementation details

- DCAD path queries `parcels` plus `appraisal` (and related detail tables), then uses
   existing `classify_parcel` + `build_feature` to keep behavior aligned with `/api/analyze`.
- TAD path queries `tad_parcels`, normalizes with existing `_normalize_tad_row`, classifies
   with existing `_classify_tad`, then builds the same feature shape via `build_feature`.

This avoids duplicate formatting code and keeps one source of truth for parcel output shape.
