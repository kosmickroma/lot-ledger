# Phase 2 — Global Comps DB (Lean MVP)

> **Status:** spec, not yet implemented. Builds on the validated deep-pull experiment (`docs/propelio/DEEP_PULL_EXPERIMENT_SPEC.md`).
>
> **Branch (when implementation starts):** `feat/phase-2-global-comps` off `develop`. This spec is checked in on the deep-pull experimental branch for reference; implementation lands on its own branch.
>
> **Goal:** Move comps from per-workspace JSONB archives to a single global table queryable by geography. Mike's team experiences sub-second workspace opens for areas with any prior coverage. Comps become shared facts; workspaces query them via PostGIS spatial query.
>
> **Out of scope for MVP:** delta refresh, deep-pull rewire to global table (deep-pull stays on experimental tables for now), batched-render UX, drawn-polygon trigger for deep pull, deprecation of experimental tables. All deferred to Phase 2.5.

---

## What changes architecturally

### The core shift

Today: every workspace has its own `propelio_comp_archive` rows. Same comp appearing in 5 workspaces = 5 duplicate JSONB blobs.

After Phase 2 MVP: ONE `propelio_comps` table holds each unique comp once. Workspaces don't "attach" to comps — they query them by geography (PostGIS `ST_DWithin` or `ST_Contains`). Ratings move to a thin `comp_ratings` table keyed by `(workspace_id, comp_id)`.

### The new tables

```sql
-- One row per unique comp (deduped by comp_address_key)
CREATE TABLE propelio_comps (
    comp_id BIGSERIAL PRIMARY KEY,
    comp_address_key TEXT UNIQUE NOT NULL,
    -- Extracted typed columns (parsed on insert via _parse_property logic)
    address TEXT NOT NULL,
    neighborhood TEXT,
    lat NUMERIC(10, 7),
    lng NUMERIC(10, 7),
    -- Geometry column for spatial queries (POINT in WGS84)
    geom GEOMETRY(POINT, 4326),
    status TEXT,                          -- 'sold' | 'pending' | 'for_sale' | NULL
    last_status TEXT,                     -- historical status if changed
    price NUMERIC,
    last_price NUMERIC,                   -- preserved when price changes on refresh
    sold_date DATE,
    close_date DATE,
    dom INTEGER,
    bed NUMERIC,
    bath NUMERIC,
    baths_full INTEGER,
    baths_half INTEGER,
    garage INTEGER,
    sqft NUMERIC,
    lot_size NUMERIC,                     -- sq ft
    year_built INTEGER,
    mls TEXT,
    property_type TEXT,
    property_category TEXT,
    list_price NUMERIC,
    remarks TEXT,
    listing_agent_name TEXT,
    listing_agent_phone TEXT,
    listing_agent_email TEXT,
    listing_office_name TEXT,
    listing_office_phone TEXT,
    buyer_agent_name TEXT,
    buyer_agent_phone TEXT,
    buyer_agent_email TEXT,
    buyer_office_name TEXT,
    buyer_office_phone TEXT,
    photo_count INTEGER,
    photos JSONB,                          -- list of {url, ...} dicts
    -- Parcel match (when we resolved a county parcel for this comp)
    parcel_account_num TEXT,
    parcel_county TEXT,
    parcel_geom JSONB,                     -- GeoJSON polygon
    -- Full raw payload — futureproofs against new Propelio fields
    raw_payload JSONB NOT NULL,
    -- Audit
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_seen_source TEXT,                -- 'by_polygon' | 'by_address' | 'deep_pull' | 'backfill'
    UNIQUE (comp_address_key)
);

-- Spatial index for "comps within polygon" and "comps near point" queries
CREATE INDEX idx_propelio_comps_geom ON propelio_comps USING GIST (geom);

-- Common filter indexes
CREATE INDEX idx_propelio_comps_status ON propelio_comps (status) WHERE status IS NOT NULL;
CREATE INDEX idx_propelio_comps_sold_date ON propelio_comps (sold_date) WHERE sold_date IS NOT NULL;
CREATE INDEX idx_propelio_comps_close_date ON propelio_comps (close_date) WHERE close_date IS NOT NULL;
CREATE INDEX idx_propelio_comps_last_seen ON propelio_comps (last_seen_at);
CREATE INDEX idx_propelio_comps_parcel ON propelio_comps (parcel_county, parcel_account_num);

-- Workspace ratings — one rating per comp per workspace (NOT per user)
CREATE TABLE comp_ratings (
    rating_id BIGSERIAL PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
    comp_id BIGINT NOT NULL REFERENCES propelio_comps(comp_id) ON DELETE CASCADE,
    rating TEXT NOT NULL CHECK (rating IN ('good', 'bad')),  -- NULL = no row; deleting the row = clearing rating
    rated_by_user_id INTEGER REFERENCES users(id),
    rated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, comp_id)
);

CREATE INDEX idx_comp_ratings_workspace ON comp_ratings (workspace_id);
```

**Notes on the schema:**

- `geom` is a PostGIS `GEOMETRY(POINT, 4326)` column. SRID 4326 = WGS84 lat/lng. This is what we need for `ST_DWithin` / `ST_Contains` queries against drawn polygons (which are also in lat/lng).
- `raw_payload` keeps the full Propelio response as JSONB. Futureproofs against new fields and lets us re-extract if we later want a column we didn't think of today.
- `comp_address_key` is the dedup key (same `_comp_address_key` function we use in deep_pull and archive).
- `comp_ratings.rating` has no NULL — clearing a rating deletes the row. Cleaner than tri-state column.
- All FKs use `ON DELETE CASCADE` — deleting a workspace cleans up its ratings (but NOT the comp itself; comps are global facts).

### The data flow

```
Propelio scrape (by-polygon, by-address, deep-pull)
    │
    │  comps come back as raw dicts
    │
    ▼
_parse_property(raw_dict) — existing function in scraper.py:1546
    │
    │  produces a Property dataclass with clean typed fields
    │
    ▼
INSERT INTO propelio_comps (...) VALUES (...)
ON CONFLICT (comp_address_key) DO UPDATE SET
    last_seen_at = NOW(),
    last_status = propelio_comps.status,
    status = EXCLUDED.status,
    last_price = propelio_comps.price,
    price = EXCLUDED.price,
    raw_payload = EXCLUDED.raw_payload,
    -- (other fields updated on each refresh)
    -- comp_id, first_seen_at, first_seen_source preserved
    │
    │
    ▼
Comp is now in the global table. Any future query against this area will hit it.
```

Workspace open path:

```
User draws polygon (or restores saved area)
    │
    ▼
POST /api/propelio/by-polygon { polygon: [...], saved_area_id: ... }
    │
    ▼
Backend: query propelio_comps WHERE ST_Within(geom, ST_GeomFromGeoJSON(polygon))
    │
    │  if rows > 0:
    │    return cached_comps (annotated with ratings from comp_ratings)
    │  else:
    │    fall back to scraper (existing path) + write results to global table
    │
    ▼
Frontend renders via existing _renderPropelioComps(data) — no change
```

---

## Migration plan

### Backfill from `propelio_comp_archive`

Current `propelio_comp_archive` has weeks of Mike's team's comp ratings. Don't lose them.

```python
# pseudocode for one-time backfill (runs in _ensure_session_schema startup OR a dedicated script)

# For each row in propelio_comp_archive:
#   1. Parse comp_data via _parse_property
#   2. INSERT INTO propelio_comps ... ON CONFLICT (comp_address_key) DO UPDATE (preserves dedup)
#   3. If user_rating IS NOT NULL: INSERT INTO comp_ratings (workspace_id, comp_id, rating, rated_at)
#      ON CONFLICT (workspace_id, comp_id) DO UPDATE
```

Approach: dedicated `scripts/backfill_propelio_comps.py` script that we run ONCE. Better than baking into startup — keeps startup fast, and gives us a reproducible artifact for the migration.

After backfill completes successfully, `propelio_comp_archive` stays in place as historical (we don't drop it in MVP — defer cleanup to Phase 2.5 once we're confident migration is good).

---

## Chunk-by-chunk implementation

### Chunk 1 — Schema + backfill script

**Files to modify:**
- `api/main.py` — add the two new tables (and indexes) to `_ensure_session_schema()` at line 184ish. Wrap in a guard? No — these become permanent, no env-var gate.

**Files to create:**
- `scripts/backfill_propelio_comps.py` — one-shot migration. Reads from `propelio_comp_archive`, writes to `propelio_comps` and `comp_ratings`. Idempotent (`ON CONFLICT DO UPDATE`).

**Dependencies:**
- PostGIS extension must be enabled on the session DB. Verify it's already installed (the parcels DB has it; need to confirm session DB does too).

**Smoke test:**
- Run script against staging DB.
- `SELECT COUNT(*) FROM propelio_comps` should be < total rows in `propelio_comp_archive` (dedup happened).
- `SELECT COUNT(*) FROM comp_ratings` should equal `SELECT COUNT(*) FROM propelio_comp_archive WHERE user_rating IS NOT NULL`.
- Spot check 10 random comps: address, price, status look right.

### Chunk 2 — Scraper persistence rewires to global table

**Files to modify:**
- `api/propelio/archive.py` — `merge_comps_into_archive(saved_area_id, comps)` keeps working for backward compat but ALSO writes the same comps to `propelio_comps`. Eventually we remove the archive write (Phase 2.5). For MVP, write to BOTH so we can verify before deprecating.

Or cleaner: introduce a new function `merge_comps_into_global(comps, source)` that handles `propelio_comps` upserts. Call it from `_run_by_polygon` AFTER the existing archive merge. This way nothing breaks if the global write fails — the existing archive flow still works.

**Files to create:**
- (none — modify `archive.py`)

**Smoke test:**
- Hit `POST /api/propelio/by-polygon` on a dev workspace with some test polygon.
- Confirm comps still flow back to the frontend (no UX regression).
- `SELECT COUNT(*) FROM propelio_comps` increases by the new comp count (minus dedupes).
- `SELECT * FROM propelio_comp_archive WHERE saved_area_id = ...` still has the same rows it had before.

### Chunk 3 — Read path: workspace open hits cache first

**Files to modify:**
- `api/propelio/routes.py` — `_run_by_polygon()` and the routes that call it:
  - At top of `_run_by_polygon`, query `propelio_comps` for comps inside the polygon (using `ST_Within` or `ST_Contains` on the `geom` column).
  - If cached_comps is non-empty: enrich with ratings via LEFT JOIN to `comp_ratings WHERE workspace_id = saved_area_id`. Return the response payload in the SAME shape the existing route returns (`{cached: bool, polygon_meta: ..., comps: [...]}`).
  - If cached_comps is empty: fall through to the existing scrape path. The scrape's results write to `propelio_comps` via Chunk 2's plumbing.

- `api/propelio/archive.py` — add a `load_comps_by_polygon(polygon_geojson, saved_area_id)` function that does the spatial query + ratings JOIN. This becomes the canonical "load cached comps for a workspace" function.

**Critical detail:** the response shape must match exactly what the frontend `_renderPropelioComps()` expects. Test by saving an area, opening it, and confirming the map renders the same as before.

**Smoke test:**
- Open a workspace that was JUST scraped (cache populated by Chunk 2).
- Verify response includes `cached: true` (new field) or similar signal that we hit cache.
- Verify map renders identically to a fresh scrape.
- Open a workspace in an uncovered area. Verify it falls back to scrape, response works, cache populates.
- Re-open the same workspace. Verify second open hits cache and is faster.

---

## What MVP does NOT include (Phase 2.5 work)

| Feature | Why deferred |
|---|---|
| Delta refresh (auto-scrape for new closings on workspace open) | MVP works without it — first time you open a stale area, you just see the cached snapshot. Adding delta requires careful UX so it doesn't surprise users with new comps after they've started working. |
| Deep-pull writes to global table | Deep-pull stays on its experimental tables for now. After MVP is validated, we deprecate the experimental tables in a Phase 2.5 chunk. Keeps blast radius small. |
| Batched render during deep-pull | Visual polish. Deep-pull stays SQL-inspectable for now. |
| Drawn-polygon trigger for deep-pull | UX nicety. Address-typeahead trigger works fine. |
| Stale freshness — `last_seen_at` driven re-fetch | Cache is "first write wins" for now. Phase 2.5 adds "comps older than X days re-fetched." |
| Cleanup of `propelio_comp_archive` | Stays in place as historical until we're confident the migration is solid (weeks of observation). |

---

## Risks and rollback plan

### Schema migration risks

- **PostGIS extension may not be enabled on the session DB.** Check before Chunk 1 lands. If not enabled, `CREATE EXTENSION postgis` is the fix (requires Cloud SQL config change, not just SQL).
- **Backfill script could lose data on edge cases.** Address-key collisions where two `propelio_comp_archive` rows for different workspaces had identical `comp_address_key` but slightly different `comp_data` — we keep most recent `last_seen_at`. Log discards.

### Read-path risks

- **Performance under high cache hit:** PostGIS `ST_Within` against thousands of comps should be sub-100ms with the GIST index, but worth load-testing once we have realistic data volume.
- **Response shape drift:** if the cache returns comps in a slightly different shape than the scrape, frontend may render weirdly. Mitigation: pass cache results through `_parse_property` again so they go through the same normalization the scrape does. Or write `propelio_comps` rows that ARE in the right shape from the start.

### Rollback plan

- **Chunk 1 (schema):** new tables can sit empty without affecting any existing code. Even if the backfill script has bugs, original `propelio_comp_archive` is untouched. Worst case: drop the new tables and re-run later.
- **Chunk 2 (persistence):** if global writes fail or look wrong, comment out the new write call. Existing archive flow continues.
- **Chunk 3 (read path):** if cache reads return weird data, add a feature flag (`PHASE_2_CACHE_READ=true`) that gates the cache-first behavior. Off by default initially, flip on once validated.

---

## After MVP — strategic pre-population (Phase 2B-ish)

After Chunks 1-3 ship to dev, BEFORE Mike's team hits the new behavior:

1. KK runs the deep-pull button on 4-6 strategic addresses in DFW:
   - Deep East Dallas
   - Uptown
   - North Plano
   - Frisco
   - North Tarrant (Bedford / Euless area)
   - Denton edge (rural test)
2. Each pulls 300-400 unique comps.
3. With Chunk 2 in place, deep-pull writes to BOTH experimental tables AND global table (after we add the global write to the deep-pull persistence path — small ~30min addition).
4. Total: ~2000 comps live in `propelio_comps` covering most common analyst test areas.
5. Mike's team's first polygon draw in those areas → sub-second cache hit. Looks magic.

Add this to the spec only if we want the deep-pull to write to global table during MVP. Alternative: skip it and let Mike's team's organic activity populate the cache over time. Each first-time draw is slow, subsequent are fast.

KK's call which path to take. Recommended: add deep-pull → global write as a tiny chunk 2.5 so we can pre-seed before Mike's team tests.

---

## Decisions locked (KK confirmed 2026-05-11)

1. **Deep-pull DOES write to global table during MVP.** ~30 min addition to Chunk 2: `api/propelio/deep_pull.py:_insert_pass_comps` ALSO upserts into `propelio_comps`. Enables the pre-seeding strategy — KK can run the seed campaign immediately after Chunks 1+2 ship.
2. **Conservative feature-flag rollout for cache reads.** Ship Chunks 1+2 first (write-only). Cache populates organically + via deep-pull seed runs over ~3-5 days. Then a separate deploy enables cache-first reads via the gate. The gate: env var `PHASE_2_CACHE_READ=true` set on Cloud Run, checked at request time in the read path.
3. **Cache-or-scrape decision lives inside `_run_by_polygon`.** Minimal disruption to existing call sites. Phase 2.5 could refactor into a dedicated orchestrator function later.
4. **PostGIS extension verification — task for Chunk 1.** Copilot must verify PostGIS is enabled on the session DB BEFORE writing the geom column DDL. If not enabled, the chunk pauses and we add `CREATE EXTENSION postgis` to `_ensure_session_schema` (requires confirming Cloud SQL flag allows it).
5. **Implementation branch:** continue on `feat/propelio-deep-pull-experiment`. Adds Phase 2 commits on top of the experimental work. Less clean than branching off develop fresh, but simpler workflow.

---

## Estimated effort

- Chunk 1 (Schema + backfill script): **3-4 hours**
- Chunk 2 (Scraper persistence): **3-4 hours**
- Chunk 3 (Read path): **3-4 hours**
- Pre-seeding deep-pull addition: **30 min** (if decided yes)
- **Total: ~10-13 hours of Copilot work, plus KK's review + smoke testing.**

Realistically 1 week of incremental ship-and-test, not all at once. Land Chunk 1, verify backfill on dev, then Chunk 2, then Chunk 3.
