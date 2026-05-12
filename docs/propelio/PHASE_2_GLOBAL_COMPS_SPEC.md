# Phase 2 — Global Comps DB (Lean MVP)

> **Status:** spec, not yet implemented. Builds on the validated deep-pull experiment (`docs/propelio/DEEP_PULL_EXPERIMENT_SPEC.md`).
>
> **Revision:** v2 (2026-05-11). Addresses Copilot first-review findings: PostGIS extension prerequisite (`CREATE EXTENSION` added), backfill input shape clarified (archive `comp_data` IS `asdict(Property)`, NOT raw — used directly as `parsed_payload`), dual-payload column design (`parsed_payload` + `raw_payload`) added for response shape parity, deep-pull `_parse_property` normalization required before global write (cross-path key consistency), polygon GeoJSON wrapper + ring-closure required, `geom` population via `ST_SetSRID(ST_MakePoint(lng, lat), 4326)` made explicit, `bed`/`bath` columns renamed to `beds`/`baths` (plural — matches frontend's `extra` dict keys), duplicate UNIQUE constraint removed, `comp_ratings.rated_by_user_id` gains `ON DELETE SET NULL`, parallel-write try/except/swallow semantics explicit, backfill RETURNING comp_id pattern documented, rollback warning added re: pre-seeded ratings being unrecoverable.
>
> **Branch (when implementation starts):** continues on `feat/propelio-deep-pull-experiment` per KK's call (simpler than separate branch, accepts that experimental + permanent code coexist for the MVP window).
>
> **Goal:** Move comps from per-workspace JSONB archives to a single global table queryable by geography. Mike's team experiences sub-second workspace opens for areas with any prior coverage. Comps become shared facts; workspaces query them via PostGIS spatial query.
>
> **MVP scope:** Chunks 1–3 below. Deep-pull also writes to global table from Chunk 2 forward (enables pre-seed campaign).
>
> **Out of scope for MVP:** delta refresh, batched-render UX, drawn-polygon trigger for deep pull, freshness invalidation, deprecation of `propelio_comp_archive`. All deferred to Phase 2.5.

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
    -- Geometry column for spatial queries (POINT in WGS84).
    -- Populated via ST_SetSRID(ST_MakePoint(lng, lat), 4326) in BOTH the
    -- INSERT column list AND the ON CONFLICT DO UPDATE SET clause. If lat/lng
    -- are NULL (rare — Propelio returned no address coords), geom stays NULL
    -- and that comp is excluded from cache reads (known limitation).
    geom GEOMETRY(POINT, 4326),
    status TEXT,                          -- 'sold' | 'pending' | 'for_sale' | NULL
    last_status TEXT,                     -- historical status if changed
    price NUMERIC,
    last_price NUMERIC,                   -- preserved when price changes on refresh
    sold_date DATE,
    close_date DATE,
    dom INTEGER,
    -- NOTE: column names use plural ("beds", "baths") to match the frontend's
    -- comp.extra dict keys exactly. This avoids a column-to-key rename layer
    -- in the response reconstruction path.
    beds NUMERIC,
    baths NUMERIC,
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
    -- DUAL PAYLOAD STORAGE — see "Response shape reconstruction" section.
    -- parsed_payload: the asdict(Property) form, ALWAYS populated. Used as
    --   the source of truth for cache-read response reconstruction so the
    --   frontend sees an identical shape to a fresh scrape.
    -- raw_payload: the ORIGINAL Propelio raw dict if available, NULL for
    --   backfilled rows whose archive comp_data didn't have extra["raw"].
    --   Futureproofs against new Propelio fields we want to surface later.
    parsed_payload JSONB NOT NULL,
    raw_payload JSONB,                     -- nullable (older archive rows)
    -- Audit
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_seen_source TEXT                 -- 'by_polygon' | 'by_address' | 'deep_pull' | 'backfill'
);
-- (UNIQUE constraint defined inline on comp_address_key above —
--  removed the duplicate table-level UNIQUE that was here.)

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
    rated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    rated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, comp_id)
);

CREATE INDEX idx_comp_ratings_workspace ON comp_ratings (workspace_id);
```

**Notes on the schema:**

- `geom` is a PostGIS `GEOMETRY(POINT, 4326)` column. SRID 4326 = WGS84 lat/lng. The Chunk 3 spatial query uses `ST_Within(geom, polygon)` to find comps inside a drawn polygon. (`ST_DWithin` is distance-based for "near a point" — not used here.)
- **`parsed_payload` is the source of truth for response reconstruction.** It's always populated. `raw_payload` is the original Propelio dict when known (futureproofing); nullable.
- **Column names use plural form (`beds`, `baths`) to match the frontend's `comp.extra` dict keys exactly.** This avoids column-to-key renames in the response path.
- `comp_address_key` is the dedup key (same `_comp_address_key` function from archive.py:21). **CRITICAL:** all paths writing to `propelio_comps` MUST first normalize raw CMA dicts through `_parse_property` before computing the key. See "Cross-path key consistency" below.
- `comp_ratings.rating` has no NULL — clearing a rating deletes the row. Cleaner than tri-state column.
- `comp_ratings.rated_by_user_id` uses `ON DELETE SET NULL` so user deletion doesn't fail with FK violations. The rating itself is workspace-scoped and survives the user being gone.
- Workspace/comp deletion cascades clean up `comp_ratings` rows but never the underlying comp.

### PostGIS extension prerequisite

PostGIS must be enabled on the session DB before Chunk 1 DDL runs. Cloud SQL ships PostGIS as a pre-installed contrib module; `CREATE EXTENSION IF NOT EXISTS postgis` should be sufficient (no Cloud SQL instance flag required).

Chunk 1's first task: add this line to `_ensure_session_schema()` immediately after the existing `pgcrypto` line:

```python
cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
```

Verify with `SELECT PostGIS_Version()` after Chunk 1 ships. If the extension cannot be created (Cloud SQL refuses), STOP and reach out — that's a config change, not a code one.

### Response shape reconstruction (the architectural answer to Copilot #8)

The frontend's `_renderPropelioComps()` expects comps in this shape:

```
{
  address, price, status, sqft, lot_size, year_built, neighborhood, ...,
  extra: { lat, lon, beds, baths, baths_full, baths_half, garage, dom,
           list_price, close_date, mls, remarks, property_type, raw, ... },
  comp_address_key, user_rating, parcel_geom, parcel_account_num
}
```

This is `asdict(Property)` plus a few annotations. Cache reads MUST return this exact shape.

**Strategy:** store `parsed_payload = asdict(Property)` on insert. On cache read, return:

```python
{
  **row.parsed_payload,
  "comp_address_key": row.comp_address_key,
  "user_rating": rating_for_workspace_or_None,
  "parcel_geom": row.parcel_geom,
  "parcel_account_num": row.parcel_account_num,
  "parcel_county": row.parcel_county,
}
```

That's it. The typed columns (price, status, lat, lng, etc.) are for **filter/query** purposes only — they never appear in the response. The response comes from `parsed_payload`.

`raw_payload` is purely for forward compatibility: if next year Propelio adds a field we'd want, we can re-parse from `raw_payload` and add it to typed columns + `parsed_payload` without re-scraping.

### Cross-path key consistency (the architectural answer to Copilot #3 + #9)

Two scrape paths feed `propelio_comps`:
1. **By-polygon / by-address** (production): goes through `_parse_property` already; comps are clean `Property` instances. ✓
2. **Deep-pull** (`_insert_pass_comps`): currently passes raw CMA dicts directly to `_comp_address_key`, which produces garbled keys (stringifies the nested address dict).

**Required fix in Chunk 2:** the deep-pull path must normalize via `_parse_property` BEFORE computing keys or writing to `propelio_comps`. Pseudocode:

```python
# Inside deep_pull._insert_pass_comps, when also writing to global table:
for raw_comp in comps:
    parsed = scraper._parse_property(raw_comp, searched_address="", ...)
    if parsed is None:
        continue  # malformed comp, skip
    parsed_dict = asdict(parsed)
    comp_key = _comp_address_key(parsed_dict)
    # Now both paths produce the same key for the same comp.
    # Upsert into propelio_comps using parsed_dict + raw_comp as raw_payload.
```

For backfill: do NOT recompute the key. Use the stored `propelio_comp_archive.comp_address_key` directly — it was computed correctly at archive write time.

### The data flow

```
Propelio scrape (by-polygon, by-address, deep-pull)
    │
    │  comps come back as raw dicts
    │
    ▼
_parse_property(raw_dict) — existing function in scraper.py:1546
    │
    │  produces a Property dataclass. asdict(prop) → parsed_payload.
    │  raw_dict (original) → raw_payload.
    │
    ▼
INSERT INTO propelio_comps (
    comp_address_key, address, neighborhood, lat, lng, geom,
    status, price, sold_date, close_date, dom,
    beds, baths, baths_full, baths_half, garage,
    sqft, lot_size, year_built, mls, property_type, property_category,
    list_price, remarks,
    listing_agent_name, listing_agent_phone, listing_agent_email,
    listing_office_name, listing_office_phone,
    buyer_agent_name, buyer_agent_phone, buyer_agent_email,
    buyer_office_name, buyer_office_phone,
    photo_count, photos,
    parcel_account_num, parcel_county, parcel_geom,
    parsed_payload, raw_payload, first_seen_source
) VALUES (
    %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326),
    %s, ...
)
ON CONFLICT (comp_address_key) DO UPDATE SET
    last_seen_at = NOW(),
    last_status = propelio_comps.status,
    status = EXCLUDED.status,
    last_price = propelio_comps.price,
    price = EXCLUDED.price,
    sold_date = EXCLUDED.sold_date,
    close_date = EXCLUDED.close_date,
    dom = EXCLUDED.dom,
    lat = EXCLUDED.lat,
    lng = EXCLUDED.lng,
    geom = CASE
        WHEN EXCLUDED.lat IS NOT NULL AND EXCLUDED.lng IS NOT NULL
        THEN ST_SetSRID(ST_MakePoint(EXCLUDED.lng, EXCLUDED.lat), 4326)
        ELSE propelio_comps.geom
    END,
    -- (refresh every column from EXCLUDED that may have changed)
    parsed_payload = EXCLUDED.parsed_payload,
    raw_payload = COALESCE(EXCLUDED.raw_payload, propelio_comps.raw_payload)
    -- comp_id, first_seen_at, first_seen_source preserved
RETURNING comp_id;
    │
    │
    ▼
Comp is now in the global table. Any future spatial query against this area will hit it.
```

Workspace open path:

```
User draws polygon (or restores saved area)
    │
    ▼
POST /api/propelio/by-polygon { polygon: [[lng,lat],...], saved_area_id: ... }
    │
    ▼
Backend: wrap polygon array into proper GeoJSON Polygon (close the ring,
         add type/coordinates wrapper), then:

  SELECT
      pc.parsed_payload,
      pc.comp_address_key,
      pc.parcel_geom, pc.parcel_account_num, pc.parcel_county,
      cr.rating AS user_rating
  FROM propelio_comps pc
  LEFT JOIN comp_ratings cr
      ON cr.comp_id = pc.comp_id AND cr.workspace_id = %s
  WHERE pc.geom IS NOT NULL
    AND ST_Within(pc.geom, ST_GeomFromGeoJSON(%s)::geometry)

  Polygon param is a JSON string like:
    {"type":"Polygon","coordinates":[[[lng1,lat1],[lng2,lat2],...,[lng1,lat1]]]}
    │
    │  if rows > 0 (and PHASE_2_CACHE_READ=true):
    │    Reconstruct response: for each row, emit {
    │      **parsed_payload, comp_address_key, user_rating,
    │      parcel_geom, parcel_account_num, parcel_county
    │    }
    │    Return as the standard {comps: [...], cached: true, ...} payload.
    │  else (cache cold or flag off):
    │    Fall back to scraper.search_properties(); persist results to
    │    propelio_comps via the write path; return scraper result.
    │
    ▼
Frontend renders via existing _renderPropelioComps(data) — shape identical
```

---

## Migration plan

### Backfill from `propelio_comp_archive`

Current `propelio_comp_archive` has weeks of Mike's team's comp ratings. Don't lose them.

**CRITICAL — input format awareness:** `propelio_comp_archive.comp_data` is the `asdict(Property)` form (NOT a raw Propelio dict). The `address` field is already a formatted string. Calling `_parse_property` on this would produce wrong results because `_parse_property` expects raw Propelio shape.

The backfill instead:
- Treats `archive.comp_data` AS the `parsed_payload` directly (it's already in that shape)
- Extracts `archive.comp_data["extra"]["raw"]` for `raw_payload` IF present (older archive rows may not have it — NULL is fine)
- Reads `archive.comp_address_key` directly as the dedup key (do NOT recompute)
- Extracts typed columns (price, status, lat/lng, beds, etc.) from `archive.comp_data` keys and `archive.comp_data["extra"]`

Pseudocode:

```python
# scripts/backfill_propelio_comps.py — one-shot, idempotent

# Pass 1: comps
for archive_row in iter_archive():
    comp_data = archive_row.comp_data  # asdict(Property)
    extra = comp_data.get("extra") or {}

    fields = {
        "comp_address_key": archive_row.comp_address_key,  # stored, don't recompute
        "address": comp_data.get("address"),
        "neighborhood": comp_data.get("neighborhood"),
        "lat": extra.get("lat"),
        "lng": extra.get("lon") or extra.get("lng"),
        "status": comp_data.get("status"),
        "price": comp_data.get("price"),
        "sold_date": _parse_date(extra.get("close_date")),
        "close_date": _parse_date(extra.get("close_date")),
        "dom": extra.get("dom"),
        "beds": extra.get("beds"),
        "baths": extra.get("baths"),
        "baths_full": extra.get("baths_full"),
        "baths_half": extra.get("baths_half"),
        "garage": extra.get("garage"),
        "sqft": comp_data.get("sqft"),
        "lot_size": comp_data.get("lot_size"),
        "year_built": comp_data.get("year_built"),
        "mls": extra.get("mls"),
        "list_price": extra.get("list_price"),
        "property_type": extra.get("property_type"),
        "remarks": extra.get("remarks"),
        # parcel match (from archive)
        "parcel_account_num": archive_row.parcel_account_num,
        "parcel_county": None,  # not stored on archive — null is OK
        "parcel_geom": archive_row.parcel_geom,
        # dual payload
        "parsed_payload": comp_data,
        "raw_payload": extra.get("raw"),  # nullable
        "first_seen_source": "backfill",
        "first_seen_at": archive_row.first_seen_at,
        "last_seen_at": archive_row.last_seen_at,
    }

    cur.execute("""
        INSERT INTO propelio_comps (...all_fields..., geom)
        VALUES (...placeholders..., 
                CASE WHEN %(lat)s IS NOT NULL AND %(lng)s IS NOT NULL
                     THEN ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)
                     ELSE NULL END)
        ON CONFLICT (comp_address_key) DO UPDATE SET
            last_seen_at = GREATEST(propelio_comps.last_seen_at, EXCLUDED.last_seen_at)
            -- first_seen_source and first_seen_at preserved
        RETURNING comp_id
    """, fields)
    comp_id = cur.fetchone()[0]  # PG returns conflicting row's id on DO UPDATE

    # If this archive row had a rating, insert into comp_ratings
    if archive_row.user_rating:
        cur.execute("""
            INSERT INTO comp_ratings (workspace_id, comp_id, rating, rated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (workspace_id, comp_id) DO UPDATE SET
                rating = EXCLUDED.rating,
                rated_at = EXCLUDED.rated_at
        """, (archive_row.saved_area_id, comp_id, archive_row.user_rating, archive_row.rating_at))
```

**Note on `RETURNING comp_id`:** PostgreSQL's `ON CONFLICT DO UPDATE ... RETURNING` is guaranteed to return the conflicting row's existing `comp_id` (not a fresh sequence value). This is the canonical pattern for "upsert and get id."

Approach: dedicated `scripts/backfill_propelio_comps.py` that we run ONCE manually. Better than baking into startup — keeps startup fast, and gives us a reproducible artifact for the migration.

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
- `api/propelio/archive.py` — add new function `merge_comps_into_global(comps, source)` that handles `propelio_comps` upserts. Existing `merge_comps_into_archive` is unchanged — both write paths run in parallel during MVP.
- `api/propelio/routes.py` — `_run_by_polygon` calls `merge_comps_into_global` AFTER the existing archive merge. Wrapped in try/except that LOGS A WARNING AND SWALLOWS — global-write failures must NOT propagate. The archive write is the source of truth during this transition; global is parallel/best-effort.
- `api/propelio/deep_pull.py` — `_insert_pass_comps` ALSO calls `merge_comps_into_global` (or inline upsert) after writing to experimental tables. CRITICAL: the deep-pull's raw CMA dicts must first be normalized through `scraper._parse_property` to produce a `Property` instance, then `asdict(prop)` → use as input to `merge_comps_into_global`. See "Cross-path key consistency" section. Same try/except/swallow pattern.

**Failure semantics (explicit):**
- Archive write succeeds, global write succeeds → ideal.
- Archive write succeeds, global write fails (exception) → log warning, return success to caller. Archive is the source of truth; user sees their result; cache is just missing this comp until next scrape.
- Archive write fails → behaves as it does today (existing error handling).
- **Both writes are NOT in the same transaction.** This is intentional — the global write must never block the user-facing archive flow.

**Files to create:**
- (none — modify `archive.py`, `routes.py`, `deep_pull.py`)

**Smoke test:**
- Hit `POST /api/propelio/by-polygon` on a dev workspace with some test polygon.
- Confirm comps still flow back to the frontend (no UX regression).
- `SELECT COUNT(*) FROM propelio_comps` increases by the new unique comp count (after dedup by comp_address_key).
- `SELECT * FROM propelio_comp_archive WHERE saved_area_id = ...` still has the same rows it had before.
- Test deep-pull from Deep Pull button — verify `propelio_comps` count grows AND that comp_address_key values match what the by-polygon path produces for the same comp (no key drift between paths).
- Manually break the `merge_comps_into_global` call (raise an exception in dev) — confirm the user-facing scrape STILL returns comps successfully; only a warning shows in logs.

### Chunk 3 — Read path: workspace open hits cache first

**GATED BY ENV VAR `PHASE_2_CACHE_READ`.** This chunk's behavior change is opt-in. Cloud Run env config controls whether cache reads are active. Default: off.

**Files to modify:**
- `api/propelio/routes.py` — `_run_by_polygon()`:
  - At top, check `os.environ.get("PHASE_2_CACHE_READ") == "true"`. If not set, fall through to the existing scrape path unchanged.
  - If set: query `propelio_comps` via `load_comps_by_polygon` (see archive.py change below). If results are non-empty AND meet a minimum-count threshold (see open question in Phase 2.5), return cached. Else fall back to scrape.
  - **Polygon must be wrapped into a proper GeoJSON Polygon before passing to PostGIS.** The codebase stores polygons as raw `[[lng, lat], ...]` arrays — they need to be wrapped as `{"type": "Polygon", "coordinates": [[[lng1,lat1], [lng2,lat2], ..., [lng1,lat1]]]}` AND the ring must be CLOSED (first coord repeated at end).

- `api/propelio/archive.py` — add `load_comps_by_polygon(polygon_latlngs: list[list[float]], saved_area_id: str | None)`:
  - Wraps the `[[lng, lat], ...]` array into the GeoJSON Polygon shape described above.
  - Ensures the ring is closed.
  - Runs the SELECT shown in "data flow → workspace open" section.
  - Returns list of dicts ready for the response (each dict = `parsed_payload` merged with `comp_address_key`, `user_rating`, `parcel_geom`, `parcel_account_num`, `parcel_county`).
  - Comps with NULL `geom` (no lat/lng) are excluded — they can't be spatially queried. Known limitation; rare in practice.

**Response reconstruction (the answer to Copilot #8):**
The cache read returns rows where `parsed_payload` IS the asdict(Property) shape. No reconstruction needed beyond:
```python
{**row["parsed_payload"],
 "comp_address_key": row["comp_address_key"],
 "user_rating": row["user_rating"],
 "parcel_geom": row["parcel_geom"],
 "parcel_account_num": row["parcel_account_num"],
 "parcel_county": row["parcel_county"]}
```
This produces identical structure to a fresh scrape — frontend renders without modification.

**Smoke test:**
- Without `PHASE_2_CACHE_READ` set: confirm behavior is UNCHANGED — every scrape hits Propelio (validates that the gate works).
- Set `PHASE_2_CACHE_READ=true` on preview Cloud Run.
- Open a workspace that was scraped by Chunk 2 (cache populated). Verify the comps render correctly on the map and the response indicates cache hit (e.g., `cached: true` flag or similar — frontend doesn't need to know, but log it server-side).
- Open the same workspace twice. Confirm both renders look identical (parsed_payload reconstruction works).
- Open a workspace in an uncovered area. Confirm fallback to scrape works. Cache populates. Re-open — confirm cache hit second time.

---

## What MVP does NOT include (Phase 2.5 work)

| Feature | Why deferred |
|---|---|
| Delta refresh (auto-scrape for new closings on workspace open) | MVP works without it — first time you open a stale area, you just see the cached snapshot. Adding delta requires careful UX so it doesn't surprise users with new comps after they've started working. |
| Batched render during deep-pull | Visual polish. Deep-pull stays SQL-inspectable for now. |
| Drawn-polygon trigger for deep-pull | UX nicety. Address-typeahead trigger works fine. |
| Stale freshness — `last_seen_at` driven re-fetch | Cache is "first write wins" for now. Phase 2.5 adds "comps older than X days re-fetched." |
| Cleanup of `propelio_comp_archive` | Stays in place as historical until we're confident the migration is solid (weeks of observation). |
| Min-comp threshold for cache hit | MVP returns any cache hit ≥1 comp. This means a polygon with only 3 cached comps returns 3 (and may look sparse). Phase 2.5 adds a min-threshold OR a `cache_coverage` field signalling sparseness so the frontend can warn / re-scrape. **Mitigation in the interim:** the pre-seed campaign (Phase 2B) saturates common test areas, so sparse hits are rare during testing. Document this as a known limitation when KK flips `PHASE_2_CACHE_READ=true`. |

---

## Risks and rollback plan

### Schema migration risks

- **PostGIS extension may not be enabled on the session DB.** Check before Chunk 1 lands. If not enabled, `CREATE EXTENSION postgis` is the fix (requires Cloud SQL config change, not just SQL).
- **Backfill script could lose data on edge cases.** Address-key collisions where two `propelio_comp_archive` rows for different workspaces had identical `comp_address_key` but slightly different `comp_data` — we keep most recent `last_seen_at`. Log discards.

### Read-path risks

- **Performance under high cache hit:** PostGIS `ST_Within` against thousands of comps should be sub-100ms with the GIST index, but worth load-testing once we have realistic data volume.
- **Response shape drift:** mitigated by the `parsed_payload` design — cache results are reassembled from `parsed_payload + annotations`, identical to a fresh scrape's shape. Smoke test in Chunk 3 explicitly verifies render parity.

### Rollback plan

- **Chunk 1 (schema):** new tables can sit empty without affecting any existing code. Even if the backfill script has bugs, original `propelio_comp_archive` is untouched. Worst case: drop the new tables and re-run later. **WARNING:** if pre-seed deep-pulls have populated `propelio_comps` after Chunk 2 ships and any ratings have been added in the meantime, those ratings are unrecoverable on rollback (`comp_ratings` cascades on `propelio_comps` deletion). If KK runs the pre-seed campaign + Mike's team has rated comps and we then roll back — those ratings are lost. Mitigation: take a `pg_dump --table=comp_ratings` snapshot before any rollback.
- **Chunk 2 (persistence):** if global writes fail or look wrong, comment out the new write call. Existing archive flow continues. Since the global write was already wrapped in try/except/swallow, even un-rollback'd buggy code can't break the user-facing scrape.
- **Chunk 3 (read path):** simply flip `PHASE_2_CACHE_READ` off in Cloud Run env. The code is already gated, no code change needed. Cache reads stop, system reverts to pre-Chunk-3 behavior.

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
