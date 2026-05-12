# Pendings Not Rendering — Investigation Brief

> **Status:** Active mystery. Fresh-eyes deep dive requested. Below is the full
> picture of what's known and where to look, written for a reviewer who has
> zero context on this conversation.

## The user-visible problem

KK is comparing what Propelio's web UI shows vs what LotLedger renders on the
map for the same neighborhood. Propelio's UI shows pending listings as blue
bubbles. LotLedger's map shows zero pending dots in the same area.

Test address used throughout: **2451 Crest Ridge Dr, Dallas, TX 75228**.

Pendings KK can see on Propelio's UI for this area include addresses on
Santa Teresa Ave, Aledo Dr, Tolosa Dr, Newcombe Dr (all within ~0.5mi).

## What's already verified true

### 1. The CMA endpoint returns pendings

We confirmed by grabbing a raw response from Propelio's UI (via DevTools
Network tab capture).

- Endpoint: `GET https://api.propelio.com/legacy/cma/{lead_id}` (e.g.,
  `8345711`)
- Response shape: `[{"id": ..., "data": {"sales": [...], "leases": [...]}, ...}]`
- The `data.sales` array contains a mix of `"status": "sold"`,
  `"status": "active"`, AND `"status": "pending"` records.
- At least 5+ pendings visible in the response for the test polygon.

### 2. Our scraper parses pendings correctly

- `api/propelio/scraper.py:1145` reads `container.get("sales")` and iterates
  through every record.
- `_normalize_status()` at line 1654 maps `"pending"` → `"pending"` (checks
  pending markers BEFORE sold/active markers, order is correct).
- `_parse_property()` does not filter by status.
- Confirmed: `SELECT raw_payload->>'status', COUNT(*) FROM propelio_comps
  GROUP BY 1` returns sold=3008, active=747, **pending=143**. We capture
  every pending the CMA endpoint serves.

### 3. The cache has pendings for the test area

Deep-pull job `dp_2lS7lx4BxFk` (completed run for 2451 Crest Ridge Dr)
inserted 398 unique comps including **27 pendings**. Those pendings are
visible in `propelio_comps` under addresses like:
- `11307 Aledo Dr` → pending
- `2103 Tolosa Dr` → pending
- `10523 Newcombe Dr` → pending
- (24 more)

Confirmed via:
```sql
SELECT parsed_payload->>'address', parsed_payload->>'status'
FROM propelio_comps
WHERE parsed_payload->>'address' ILIKE '%aledo%'
   OR parsed_payload->>'address' ILIKE '%tolosa%'
   OR parsed_payload->>'address' ILIKE '%newcombe%';
```

### 4. The frontend filter does NOT drop pendings by default

`frontend/map.js:3965-4015` (`compPassesPropelioFilters`):
```javascript
if (status === "pending" && !filters.statusPending) return false;
```

Default state at line 3910 area:
```javascript
statusPending: true
```

So pending comps pass the status filter by default.

## What was just fixed (commit `99c6106`)

The `/api/propelio/by-saved-area` endpoint was reading from the **wrong
table**. It hit legacy `propelio_comp_archive` (which has only 18 pendings
total across all areas, none for Crest Ridge) instead of the new spatial
cache `propelio_comps` (which has the 143 pendings).

After the fix, clicking a saved area now calls `load_comps_by_polygon()`
which does a PostGIS `ST_Within` + `ST_DWithin` spatial query against
`propelio_comps`. This should make the Crest Ridge pendings render.

**But KK reports pendings ALSO missing in the DRAW flow** — and the draw
flow already uses `/by-polygon` which already calls `load_comps_by_polygon`.
So the saved-area fix can't explain the draw-flow symptom.

## The actual remaining mystery

**Why don't pendings render when the user draws a polygon?**

The draw flow:
1. User draws polygon in Leaflet
2. Frontend calls `POST /api/propelio/by-polygon?cache_only=true`
   (Phase 2A auto-cache on draw)
3. Backend `_run_by_polygon()` in `api/propelio/routes.py:325` calls
   `load_comps_by_polygon(polygon, saved_area_id)`
4. Returns `{"comps": [...]}`
5. Frontend `_renderPropelioComps()` paints them on the map

The spatial query should return the Aledo/Tolosa/Newcombe pendings as
spillover (they're within the circumradius of a Crest Ridge polygon). So
why aren't they rendering?

## Hypotheses to test (ranked by likelihood)

### Hypothesis 1: OAC toggle is filtering spillover

- The spatial query (`load_comps_by_polygon` in `api/propelio/archive.py:511`)
  returns comps INSIDE the polygon AND in the circumradius spillover.
- Spillover comps get tagged `extra.is_outside_polygon = true`.
- Frontend filter at `frontend/map.js:3977-3982`:
  ```javascript
  if (!filters.showOutsideArea && Array.isArray(lastPolygon) && lastPolygon.length >= 3) {
    const latlng = _propelioCompLatLng(comp);
    if (!latlng || !_pointInPolygonLngLat(latlng[1], latlng[0], lastPolygon)) {
      return false;
    }
  }
  ```
- If the Crest Ridge pendings are spillover (likely — they're 0.3-0.5mi
  away) AND the OAC toggle is off (default state unknown), they'd be
  silently filtered out.
- **Test:** open DevTools, inspect `window._propelioLast.comps` after a draw,
  count pendings present in the array vs pending DOM markers on map.

### Hypothesis 2: PHASE_2_CACHE_READ flag is OFF

- Per memory notes, `PHASE_2_CACHE_READ=true` is the env-gate for cache-first
  reads in `_run_by_polygon()`.
- The auto-cache-on-draw flow uses `cache_only=True` query param which
  bypasses the env flag — confirmed in routes.py.
- But there might be a code path where the env flag still matters.
- **Test:** in routes.py:325 `_run_by_polygon` function, trace the
  `cache_only=True` path and verify it actually reaches
  `load_comps_by_polygon()`.

### Hypothesis 3: parsed_payload vs raw_payload divergence

- `load_comps_by_polygon` returns `parsed_payload` to the frontend.
- We confirmed `raw_payload->>'status'` has 143 pendings.
- Need to confirm `parsed_payload->>'status'` ALSO has 143 pendings.
- If they diverge (e.g., parsed_payload has fewer pendings due to a parse
  edge case), the frontend wouldn't see them.
- **Test:** `SELECT parsed_payload->>'status', COUNT(*) FROM propelio_comps
  GROUP BY 1;` — does this also show 143 pendings?

### Hypothesis 4: Frontend render path drops pendings

- After `compPassesPropelioFilters` accepts a comp, it gets passed to render.
- `_renderPropelioComps` at `map.js:3777` handles dot rendering.
- Need to verify pending comps actually enter this render loop.
- **Test:** add `console.log` at top of `_renderPropelioComps` to log
  status counts of incoming comps array.

### Hypothesis 5: Status checkbox UI state divergence

- Even though default is `statusPending: true`, the UI checkbox might be
  reading from `localStorage` or a saved filter state that has it false.
- **Test:** in browser console, run `getPropelioFilters()` and check
  `statusPending` value. Also check what's stored in `localStorage`.

### Hypothesis 6: Saved filter state on the area is stale

- When KK loads a saved area, `restoreFilterState()` runs at `map.js:2347`.
- If the saved area has `filter_state.statusPending = false`, it'd override
  the default true.
- **Test:** check the saved area row in DB:
  ```sql
  SELECT filter_state FROM saved_areas WHERE name ILIKE '%crest%';
  ```

### Hypothesis 7: Cache query is correct but rendering layer dimensions wrong

- Pending dots use `.propelio-fallback-dot.pending` class
  (`map.js:5387` defines color `#0284c7` blue).
- If the dot is being painted but at z-index below something else, or with
  zero opacity, user sees no blue dots.
- **Test:** inspect map DOM via DevTools, search for `pending` class in
  Leaflet panes.

## Key files & line numbers

- `api/propelio/scraper.py:1145` — sales array parsing
- `api/propelio/scraper.py:1654` — `_normalize_status`
- `api/propelio/archive.py:22` — `_comp_address_key` (NOTE: has a known bug
  for raw Propelio comp dicts but doesn't affect propelio_comps inserts —
  see below)
- `api/propelio/archive.py:284` — `merge_comps_into_global`
- `api/propelio/archive.py:511` — `load_comps_by_polygon` (spatial query)
- `api/propelio/routes.py:325` — `_run_by_polygon`
- `api/propelio/routes.py:737` — `get_by_saved_area` (recently fixed)
- `frontend/map.js:3777` — `_renderPropelioComps`
- `frontend/map.js:3910` — default filter state with `statusPending: true`
- `frontend/map.js:3965` — `compPassesPropelioFilters`
- `frontend/map.js:3977` — OAC spillover filter
- `frontend/map.js:5387` — `PROPELIO_HEADER_COLORS` includes pending=#0284c7

## Known peripheral issue (NOT the root cause but worth flagging)

In `api/propelio/deep_pull.py:208`, the experimental table's
`comp_address_key` column has broken keys like `{'LINE1': '10523 NEWCOMBE DR'`
(literal Python dict repr). This is because the raw Propelio comp has
`address` as a NESTED dict, and `_comp_address_key()` in archive.py:23 calls
`str(comp.get("address"))` on it. This bug is contained to the experimental
table — `merge_comps_into_global()` parses comps through `_parse_property()`
first, so `propelio_comps` has correctly-formed keys.

## Database queries to run

```sql
-- Verify parsed_payload has pendings (not just raw_payload)
SELECT parsed_payload->>'status', COUNT(*)
FROM propelio_comps GROUP BY 1;

-- Confirm Crest Ridge area pendings are correctly stored
SELECT
  parsed_payload->>'address',
  parsed_payload->>'status',
  parsed_payload->>'extra'->>'is_outside_polygon',
  ST_AsText(geom)
FROM propelio_comps
WHERE parsed_payload->>'status' = 'pending'
  AND ST_DWithin(
    geom::geography,
    ST_SetSRID(ST_MakePoint(-96.673, 32.825), 4326)::geography,
    1609.344
  )  -- 1mi around Crest Ridge area
ORDER BY ST_Distance(geom, ST_SetSRID(ST_MakePoint(-96.673, 32.825), 4326));

-- Check filter_state on the saved Crest Ridge area
SELECT name, filter_state
FROM saved_areas
WHERE name ILIKE '%crest%' OR name ILIKE '%2451%';
```

## Smoke test sequence for the reviewer

1. Hard-refresh preview (`Ctrl+Shift+R`).
2. Open DevTools Network tab, filter to "by-polygon".
3. Draw polygon over 2451 Crest Ridge Dr area.
4. Inspect the `/by-polygon` response body — search for `"status":"pending"`.
5. **If pendings ARE in the response but NOT on map:** the bug is frontend
   (rendering or filtering).
6. **If pendings are NOT in the response:** the bug is backend (spatial
   query or cache_only path).

## Expected diagnosis paths

- **Most likely:** OAC toggle is off by default → spillover pendings hidden.
  Fix: either default OAC on, or guarantee pendings inside polygon get
  rendered regardless.
- **Second most likely:** filter_state on saved areas has `statusPending:
  false` from some legacy save. Fix: defensive default override.
- **Third:** parsed_payload missing status field due to parse edge case.
  Fix: schema/parse logic.

## What we explicitly do NOT need

- Adding a new Propelio endpoint (we confirmed the CMA endpoint already
  returns pendings).
- Parsing `data.leases` (lease records, not relevant to comps).
- Changing the scraper (it captures pendings correctly).

## Asking the reviewer

After investigating, please report:
1. Which hypothesis was correct.
2. What the actual filter/render chain does for a pending comp.
3. Minimal-change fix proposal.
4. Whether to ship as hot-fix or bundle into polish.
