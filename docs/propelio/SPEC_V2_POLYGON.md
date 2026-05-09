# Propelio Integration — Phase 2: Polygon-Driven Pulls + Purple Footprint Render

> **Build spec for Copilot.** Six chunks, each independently testable.
> Run them in order. Smoke-test at the end of each before moving on.
> Stop and report when each chunk passes.

## Context

Phase 1 (already shipped — see [`SPEC.md`](./SPEC.md) and
[`SESSION_2026-05-08.md`](./SESSION_2026-05-08.md)) put Propelio comps on
the map as **cyan pulse dots** triggered by the search bar, defaulting to
24mo / 7.5mi (the cap of 100 comps per pull).

Phase 2 evolves this into the team's actual workflow:

1. User searches an address (existing flow)
2. User **draws a polygon** around the area they care about (existing
   draw tool)
3. A flashy purple **"Get Comps"** button appears on the polygon
4. User clicks → backend does the polygon-driven Propelio query
5. Returned comps render as **transparent purple glowing parcel
   footprints** (not dots) on the map
6. A new sidebar card hosts **filters** that narrow the 100-pool
   client-side
7. When the user **saves the area**, its comps persist to a per-area
   archive — repeat opens of the saved area pull from the archive (zero
   credits) with an optional manual refresh

Existing Redfin sold + active layers stay in the code as **legacy** —
default OFF on load with localStorage persistence for the user's last
choice. Mike's team can still toggle them back on.

## SCOPE LOCK (read this first, every chunk)

**You are working in exactly one repository:** `lot-ledger-pro`, located at
`/home/kk/projects/clients/lot-ledger-pro/`.

**Forbidden — do not read, edit, run commands in, or otherwise touch:**

- `/home/kk/projects/clients/lot-ledger/` (the original lot-ledger repo)
- `/home/kk/projects/clients/real-estate-comps/` (the standalone Propelio CLI)
- `/home/kk/projects/clients/deepfin/` (the Redfin scraper)
- `/home/kk/projects/clients/redfin/` and `redfin-harvest/`
- `/home/kk/projects/clients/bylersonsgarage/` and similar sibling folders
- Any folder outside `/home/kk/projects/clients/lot-ledger-pro/`
- Any system path (`/etc`, `/var`, `~/.config`, `~/.claude`, etc.)

**If you need to know what some other code does:** the SPEC documents
the behavior. Trust the spec. Don't go peeking into adjacent repos for
context — they're either out of scope or already vendored into this
repo's `api/propelio/`.

**Allowed:** anything inside `/home/kk/projects/clients/lot-ledger-pro/`,
subject to the per-chunk "Files to NOT touch" list below.

**Confirm understanding:** before writing any code, your first response
must include the line:
> Scope confirmed: working only in /home/kk/projects/clients/lot-ledger-pro/

If at any point you find yourself wanting to read or modify a file
outside that path, **stop and report instead.**

## Hard rules across all chunks

- **No M2M.** The saved-area ↔ comps relationship is 1:N with the
  `(saved_area_id, comp_address_key)` UNIQUE constraint pattern (the
  "instance per intent" rule).
- **No new git pushes / commits without explicit user approval.**
  KK runs git.
- **Don't touch:** existing parcel rendering, the `query_sold_parcels`
  function, the existing search-box typeahead/Go behavior, the
  `/api/analyze` route, anything in `api/counties/`, anything in
  `api/auth.py`. Match the codebase's style — read 2-3 nearby files
  first to see conventions (logging, error handling, type hints).
- **Stop at the end of each chunk.** Run the chunk's smoke test. Wait
  for review before continuing.
- **The Propelio purple is its own visual identity** — not the same
  styling as the existing redfin_sold purple outline. New color
  (suggested: `#8b5cf6` violet-500 or thereabouts — Copilot picks
  something visually distinct from existing palette), transparent fill
  so satellite imagery shows through, pulsing glow.

---

## Chunk A — Backend: polygon-driven Propelio endpoint

**Files to create:**
- nothing new — extend `api/propelio/routes.py`

**Files to modify:**
- `api/propelio/routes.py` — add new route
- `api/geo.py` (if it exists, otherwise extend `api/propelio/cache.py`) —
  may need helpers for centroid + circumradius computation

**Files to NOT touch:** everything else

### A.1 — New endpoint signature

```
POST /api/propelio/by-polygon
Content-Type: application/json

Request body:
{
  "polygon": [[lng, lat], [lng, lat], ...],   // GeoJSON-style ring, open or closed
  "months": 24,                                // optional, default 24
  "range_override_mi": null                    // optional; if null, use computed circumradius
}

Response: same shape as /api/propelio/by-address (cached, fetched_at,
balance, cma_settings, subject, comps[]) — but `comps` is filtered to
those falling INSIDE the polygon.
```

### A.2 — Algorithm

1. Validate the polygon: ≥3 points, lat/lng numeric, lat ∈ [-90, 90], lng ∈ [-180, 180].
   400 if invalid.

2. Compute the polygon's centroid (use the standard area-weighted formula or
   simple bounding-box center — area-weighted is more accurate for
   irregular shapes). Add helper if one doesn't already exist.

3. Compute circumradius:
   ```python
   centroid_lat, centroid_lng = polygon_centroid(polygon)
   circumradius_mi = max(
       haversine_miles(centroid_lat, centroid_lng, p_lat, p_lng)
       for p_lng, p_lat in polygon
   )
   range_mi = range_override_mi or min(circumradius_mi * 1.05, 10.0)  # 5% slack, cap at 10mi
   ```

4. Find a representative parcel address near centroid for Propelio's
   subject. Query our parcel DB (DCAD/TAD/Collin/Denton — use existing
   `query_parcels` style helper or write a new `find_nearest_parcel(lat, lng)`):
   - Returns the parcel address, account_num, and county
   - Use the **same DB schema and ST_Distance ordering** that
     `api/main.py:_parcel_addr_match_key` and friends use
   - If no parcel within 1 mile: 404 with a clear message ("no parcel
     near polygon centroid")

5. Cache key = SHA256 of (polygon coordinates JSON-canonicalized + months + range_mi).
   7-day TTL same as existing cache.

6. On miss: call `scraper_mod.search_properties(parcel_address,
   months=months, range_mi=range_mi)`. Then post-filter:

   ```python
   in_polygon_comps = [
       c for c in comps_list
       if point_in_polygon(c.extra.get("lat"), c.extra.get("lon"), polygon)
   ]
   ```

   Use `api/geo.py:point_in_polygon` if it exists. Otherwise write a
   ray-casting implementation in `cache.py` or a new helper module.

7. Return the same payload shape as `/by-address`, but with comps array
   filtered. Add a top-level `polygon_meta` field:

   ```json
   {
     "centroid": {"lat": ..., "lng": ...},
     "circumradius_mi": ...,
     "subject_parcel": {"address": ..., "county": ..., "account_num": ...},
     "comps_pulled": 100,
     "comps_in_polygon": 47
   }
   ```

8. Cache + return.

### A.3 — Smoke test (Chunk A)

After implementation, run:

```bash
curl -s -X POST http://localhost:8000/api/propelio/by-polygon \
  -H 'Content-Type: application/json' \
  -d '{
    "polygon": [
      [-96.852, 32.880],
      [-96.842, 32.880],
      [-96.842, 32.872],
      [-96.852, 32.872]
    ],
    "months": 24
  }' \
  -b /tmp/auth-cookie.txt | python3 -m json.tool | head -40
```

(Auth cookie required — same auth gate as `/by-address`. Get it by
logging in via the UI and copying the session cookie.)

Expected: 200 with comps_pulled around 100, comps_in_polygon some
fraction (probably 30-60 for that Glenridge-Estates-ish box around
Williamsburg). Verify `polygon_meta.subject_parcel` resolves to a real
Glenridge Estates address.

---

## Chunk B — Backend: enrich each comp with its parcel footprint geometry

**Files to create:**
- `api/propelio/parcel_match.py` (new) — comp-to-parcel address matcher

**Files to modify:**
- `api/propelio/routes.py` — call the matcher before returning the response

**Files to NOT touch:** the scraper, frontend, anything else

### B.1 — Matcher signature

```python
def match_comps_to_parcels(
    comps: list[dict],
) -> list[dict]:
    """Return comps with `parcel_geom` and `parcel_account_num` injected.

    For each comp:
      1. Build addr_key = normalize_addr_key(comp.address) [reuse api.redfin]
      2. Search across DCAD/TAD/Collin/Denton parcel tables for a row
         with matching addr_key (case-insensitive, county priority order
         based on lat/lon; city centroid lookup OK)
      3. If matched, inject `parcel_geom` (GeoJSON Polygon/MultiPolygon)
         and `parcel_account_num` into the comp dict
      4. If no match, leave both fields None — frontend falls back to dot
    """
```

### B.2 — Implementation notes

- Reuse `api/redfin.py:normalize_addr_key` for address normalization
- Run as a single batched query per county (NOT N queries per comp) —
  build a temp `IN ('addr_key1', 'addr_key2', ...)` query
- County selection: each comp's `extra.lat/lon` falls in exactly one
  county; use that county's table first, fall back to all four if no
  hit
- Return polygon as GeoJSON Geometry, not raw WKB — use
  `ST_AsGeoJSON(geom)` in the SQL
- Performance: batched query for 100 comps should complete in <500ms

### B.3 — Smoke test (Chunk B)

After implementation, hit `/api/propelio/by-polygon` with the same
Glenridge box from Chunk A. In the response, verify:

- `comps_in_polygon` count is unchanged
- Each comp now has either `parcel_geom: <GeoJSON>` (when matched) or
  `parcel_geom: null`
- Match rate should be HIGH (>80%) for in-county comps, since Glenridge
  Estates is in DCAD's coverage. Spot-check 2-3 comps' addresses against
  the parcel polygon — they should overlap on a satellite map
- Out-of-county comps (rare; check by lat/lon) should have null
  `parcel_geom`

---

## Chunk C — Frontend: purple glowing footprint render + button trigger

**Files to modify:**
- `frontend/style.css` — new pulse glow class + button styling
- `frontend/map.js` — render footprints, build the "Get Comps" pill button

**Files to NOT touch:** the cyan dot pattern lives elsewhere — we're
*replacing* the cyan-dot render path. Keep the search-bar address-driven
flow for now (Chunk D will eventually filter through the new card too).

### C.1 — CSS: Propelio purple footprint glow

Mirror the `.saved-parcel-glow` keyframe pattern in `style.css`:

```css
.propelio-footprint-glow {
  fill: #8b5cf6;          /* violet-500 — Copilot can adjust within the purple family for visual distinction from existing redfin_sold */
  fill-opacity: 0.12;     /* SEE-THROUGH — satellite must show underneath; this is critical to KK's spec */
  stroke: #8b5cf6;
  stroke-width: 2.5;
  will-change: filter;
  filter:
    drop-shadow(0 0 4px rgba(139, 92, 246, 0.95))
    drop-shadow(0 0 12px rgba(139, 92, 246, 0.55));
  animation: propelioFootprintPulse 2.2s ease-in-out infinite;
}

@keyframes propelioFootprintPulse {
  0%, 100% {
    filter:
      drop-shadow(0 0 4px rgba(139, 92, 246, 0.95))
      drop-shadow(0 0 10px rgba(139, 92, 246, 0.45));
    fill-opacity: 0.10;
  }
  50% {
    filter:
      drop-shadow(0 0 8px rgba(139, 92, 246, 1))
      drop-shadow(0 0 18px rgba(139, 92, 246, 0.75));
    fill-opacity: 0.18;
  }
}

/* Fallback dot for comps without parcel_geom — same purple, smaller, less effect */
.propelio-fallback-dot {
  /* circle marker styling */
  background: #8b5cf6;
  border: 2px solid white;
  border-radius: 50%;
  box-shadow: 0 0 8px rgba(139, 92, 246, 0.8);
  animation: propelioFootprintPulse 2.2s ease-in-out infinite;
}
```

### C.2 — CSS: "Get Comps" pill button (the wow-factor element)

KK's spec: **purple with white text, pill, flashy, matches the
parcel-overlay purple.** Copilot has creative latitude on the wow
factor — examples that work well:

- Soft inner glow that pulses in sync with the parcel-overlay glow
- Subtle gradient (e.g., `linear-gradient(135deg, #8b5cf6, #6d28d9)`)
- Bounce/scale on hover (`transform: scale(1.03)`)
- Brief sparkle/shimmer via a `::before` pseudo-element with a translating gradient
- Soft drop-shadow that gets richer on hover

The button appears **on top of the drawn polygon** (use a Leaflet
control or absolutely-positioned element pinned to polygon bounds).
Anchor: above the polygon's top edge, centered horizontally, with a
tail/pointer optional. Disappears when polygon is cleared or comps are
loaded.

Suggested base styles (Copilot riffs from here):

```css
.propelio-get-comps-btn {
  background: linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%);
  color: white;
  border: 0;
  border-radius: 999px;          /* pill */
  padding: 10px 24px;
  font-weight: 600;
  font-size: 13px;
  letter-spacing: 0.02em;
  cursor: pointer;
  box-shadow: 0 4px 16px rgba(139, 92, 246, 0.55);
  position: relative;
  overflow: hidden;
  transition: transform 0.18s ease, box-shadow 0.18s ease;
  /* optional pulsing glow that matches parcel overlay */
  animation: propelioBtnGlow 2.4s ease-in-out infinite;
}

.propelio-get-comps-btn:hover {
  transform: scale(1.04);
  box-shadow: 0 6px 22px rgba(139, 92, 246, 0.75);
}

.propelio-get-comps-btn:active {
  transform: scale(0.98);
}

.propelio-get-comps-btn:disabled {
  opacity: 0.6;
  cursor: wait;
  animation: none;
}

@keyframes propelioBtnGlow {
  0%, 100% { box-shadow: 0 4px 16px rgba(139, 92, 246, 0.55); }
  50%      { box-shadow: 0 6px 22px rgba(139, 92, 246, 0.85); }
}

/* Optional shimmer effect — go to town */
.propelio-get-comps-btn::before {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(120deg, transparent 30%, rgba(255,255,255,0.18) 50%, transparent 70%);
  transform: translateX(-100%);
  animation: propelioBtnShimmer 3.5s ease-in-out infinite;
}

@keyframes propelioBtnShimmer {
  0% { transform: translateX(-100%); }
  60%, 100% { transform: translateX(100%); }
}
```

### C.3 — JS: button lifecycle

In `frontend/map.js`:

1. Listen for the existing draw event (whatever fires when a polygon is
   completed). Hook in the button-show logic there.
2. When polygon is drawn, position the button above the polygon's
   north-most point (use `polygon.getBounds().getNorth()` and middle
   longitude).
3. On click:
   - Disable the button, change text to "Pulling…"
   - POST to `/api/propelio/by-polygon` with the polygon coords +
     defaults `months: 24`
   - On success: render footprints (next section), hide the button
   - On error: show a toast, re-enable button
4. When polygon is cleared / re-drawn, hide the button.

### C.4 — JS: footprint render

Replace the existing cyan dot path in `firePropelioFetch` (and rename if
needed — the new function might be `firePropelioPolygonFetch`):

```javascript
function renderPropelioComps(data) {
  propelioCompLayer.clearLayers();
  if (!Array.isArray(data?.comps)) return;

  data.comps.forEach((comp) => {
    if (comp.parcel_geom) {
      // Render as glowing footprint
      const layer = L.geoJSON(comp.parcel_geom, {
        className: "propelio-footprint-glow",
        style: { /* className handles styling */ },
      });
      layer.bindPopup(_propelioBuildPopup(comp));
      layer.addTo(propelioCompLayer);
    } else {
      // Fallback: small purple pulse dot (existing pattern, just purple)
      const lat = Number(comp?.extra?.lat);
      const lon = Number(comp?.extra?.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
      const icon = L.divIcon({
        className: "propelio-fallback-marker-wrap",
        html: '<div class="propelio-fallback-dot"></div>',
        iconSize: [14, 14],
        iconAnchor: [7, 7],
      });
      const marker = L.marker([lat, lon], { icon, riseOnHover: true });
      marker.bindPopup(_propelioBuildPopup(comp));
      marker.addTo(propelioCompLayer);
    }
  });
  propelioCmaChip.setData(data);
}
```

### C.5 — Smoke test (Chunk C)

1. Draw a polygon over the Glenridge Estates / Williamsburg area
2. The "Get Comps" pill button should appear above the polygon, gently
   pulsing/glowing in purple
3. Hover the button → it scales slightly + glow intensifies
4. Click → button text changes to "Pulling…", takes ~10-30s
5. Footprints render: ~30-60 transparent purple glowing parcel polygons,
   pulsing in sync, with satellite imagery clearly visible underneath
6. Click any footprint → existing popup with all the comp data
7. The CMA chip updates at bottom-left
8. Re-draw polygon → button reappears, ready for another pull

---

## Chunk D — Frontend: Propelio sidebar card with filter strip

**Files to modify:**
- `frontend/index.html` — add the new card markup
- `frontend/style.css` — card styling
- `frontend/map.js` — filter state + apply-filter function

### D.1 — Card markup (in the sidebar, alongside the existing sold-comps card)

```html
<section class="sidebar-card propelio-card" id="propelio-card">
  <header class="sidebar-card-header section-toggle" data-target="propelio-card-body">
    <span class="propelio-card-title">Propelio Comps</span>
    <span class="propelio-card-count" id="propelio-card-count">—</span>
    <span class="section-toggle-chevron">▾</span>
  </header>
  <div class="sidebar-card-body" id="propelio-card-body">
    <!-- Filter strip — pattern matches existing sold-comps filters -->
    <div class="propelio-filter-row">
      <label>Status</label>
      <div class="propelio-status-chips">
        <input type="checkbox" id="prop-status-sold" checked />
        <label for="prop-status-sold">Sold</label>
        <input type="checkbox" id="prop-status-active" checked />
        <label for="prop-status-active">Active</label>
        <input type="checkbox" id="prop-status-pending" checked />
        <label for="prop-status-pending">Pending</label>
      </div>
    </div>
    <div class="propelio-filter-row">
      <label>Sold within (days)</label>
      <input type="number" id="prop-sold-within" value="180" min="1" max="3650" />
    </div>
    <div class="propelio-filter-row">
      <label>Lot size (acres)</label>
      <input type="number" id="prop-lot-min" placeholder="any" step="0.01" />
      <span>–</span>
      <input type="number" id="prop-lot-max" placeholder="any" step="0.01" />
    </div>
    <div class="propelio-filter-row">
      <label>Sqft</label>
      <input type="number" id="prop-sqft-min" placeholder="any" />
      <span>–</span>
      <input type="number" id="prop-sqft-max" placeholder="any" />
    </div>
    <div class="propelio-filter-row">
      <label>Year built</label>
      <input type="number" id="prop-year-min" placeholder="any" />
      <span>–</span>
      <input type="number" id="prop-year-max" placeholder="any" />
    </div>
    <div class="propelio-filter-row">
      <label>Price</label>
      <input type="text" id="prop-price-min" placeholder="any (1m, 500k)" />
      <span>–</span>
      <input type="text" id="prop-price-max" placeholder="any (1m, 500k)" />
    </div>
    <div class="propelio-comp-list" id="propelio-comp-list">
      <!-- Each comp = one row, populated by JS, sorted by user pick -->
    </div>
  </div>
</section>
```

### D.2 — Filter behavior

- Filters apply LIVE on input change (debounced ~150ms)
- Apply against the most recent `window._propelioLast.comps` array
- Each comp passing all filters is rendered on the map (footprint
  visible) AND listed in `#propelio-comp-list`
- Comps failing filters are hidden (remove from layer + don't list)
- Header count `#propelio-card-count` updates: e.g., "47 / 100" meaning
  47 visible after filter, 100 pulled
- Card defaults to expanded (open) when comps are first loaded; user
  toggle persists in localStorage

### D.2.5 — Per-comp row layout (the comp list inside the card)

Each row in `#propelio-comp-list` represents one comp. Layout
(KK requested neighborhood be prominent — bake it in on every row):

```
┌──────────────────────────────────────────────────────────┐
│ Glenridge Estates 2                          $1,675,000  │  ← neighborhood + price (bold, 13px)
│ 3947 Beechwood Ln · for_sale                             │  ← address · status (muted, 11px)
│ 4,337 sqft · 9,277 sqft lot · built 2025 · DOM 85        │  ← key metrics (muted, 11px)
└──────────────────────────────────────────────────────────┘
```

When `comp.neighborhood` is null/empty, fall back to `"—"` so the row
layout stays consistent. Row click should fly the map to the comp's
location and open its popup. Hover row → highlight the matching
footprint on the map (apply a temporary `.propelio-footprint-highlight`
class with brighter glow / stronger fill, removed on row mouseleave).

### D.3 — Smoke test (Chunk D)

1. Pull comps for Glenridge polygon (Chunk C flow)
2. Card auto-opens; lists 30-60 comp rows; map shows their footprints
3. Type "1500" in price-min — comps under $1.5M should disappear from
   both list and map
4. Uncheck "Active" — only sold + pending remain
5. Set sold-within to 30 days — only very recent solds remain
6. Clear all filters — full pool returns
7. Reload the page; check sidebar collapse state was preserved

---

## Chunk E — Frontend: legacy layer defaults + persistence

**Files to modify:**
- `frontend/map.js` — initial state for the existing redfin_sold + active
  layer toggles; add localStorage hooks

**Files to NOT touch:** the layer rendering itself (stays exactly as-is —
just changing default visibility)

### E.1 — Logic

On app load, for each of:
- redfin_sold layer toggle
- redfin_active layer toggle
- new propelio_comps toggle

Look up `localStorage.getItem('lotledger.layer.<key>')`:
- If set, restore that value
- If not, use defaults: redfin_sold = OFF, redfin_active = OFF,
  propelio_comps = ON

On user toggle change, save to localStorage.

### E.2 — Smoke test (Chunk E)

1. Fresh tab: redfin_sold + active checkboxes are unchecked, no layers
   render even after a draw analyze
2. Toggle redfin_sold ON; draw + analyze; sold layer renders normally
3. Reload tab; redfin_sold toggle is still ON (preserved)
4. Toggle redfin_sold OFF; reload; still OFF

---

## Chunk F — Backend: saved-area persistence (the archive)

**Files to create:**
- `api/propelio/archive.py` — table creation + smart-merge helpers

**Files to modify:**
- `api/propelio/routes.py` — wire archive into the polygon endpoint when
  a saved_area_id is provided; new `/refresh` endpoint
- `api/main.py` — extend the existing save-area flow to call the archive
  merge

### F.1 — Schema

```sql
CREATE TABLE IF NOT EXISTS propelio_comp_archive (
  id                  SERIAL PRIMARY KEY,
  saved_area_id       INTEGER NOT NULL REFERENCES saved_areas(id) ON DELETE CASCADE,
  comp_address_key    TEXT NOT NULL,
  comp_mls            TEXT,
  comp_data           JSONB NOT NULL,
  parcel_geom         JSONB,                    -- denormalized from Chunk B for instant render
  first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_status         TEXT,
  last_price          NUMERIC,
  UNIQUE (saved_area_id, comp_address_key)
);
CREATE INDEX IF NOT EXISTS propelio_comp_archive_area
  ON propelio_comp_archive (saved_area_id);
CREATE INDEX IF NOT EXISTS propelio_comp_archive_seen
  ON propelio_comp_archive (last_seen_at);
```

`ensure_tables()` runs on import like the existing cache module.

### F.2 — Smart-merge helper

```python
def merge_comps_into_archive(
    saved_area_id: int,
    comps: list[dict],
) -> dict:
    """Upsert comps into the archive for a saved area. Returns a summary
    dict with counts: {inserted: N, updated: M, total_in_archive: T}.

    For each comp:
      - If (saved_area_id, comp_address_key) exists: UPDATE last_seen_at,
        comp_data, last_status, last_price
      - Else: INSERT (sets first_seen_at = NOW)
    """
```

### F.3 — Route hooks

- When polygon endpoint is called WITH `saved_area_id` param: after the
  Propelio response, call `merge_comps_into_archive(saved_area_id, comps)`
  and include the merge summary in the response.
- New `POST /api/propelio/refresh?saved_area_id=...` route: re-runs the
  polygon search (uses the saved area's polygon), merges into archive,
  returns the merged result.
- New `GET /api/propelio/archive?saved_area_id=...`: pulls everything in
  the archive for that area, no Propelio call. Default behavior on
  reopening a saved area.

### F.4 — Smoke test (Chunk F)

1. Draw polygon, click "Get Comps", save the area
2. Verify `propelio_comp_archive` has N rows for that saved_area_id
3. Reopen the same saved area — archived comps render instantly with no
   Propelio call (verify via Cloud Run logs / cache hit)
4. Click a "Refresh" button → re-fetches Propelio, merge updates
   `last_seen_at` on existing rows, possibly inserts new rows
5. Delete the saved area → archive rows cascade out (verify via SQL)

---

## Verification rolling forward

After all six chunks land:

- Polygon search workflow end-to-end (draw → button → footprints →
  filter → save → reload → archive)
- Search-bar address workflow still works (Phase 1 cyan path retained
  or quietly retired — Copilot's call after reviewing the code; if
  retired, unify on the new purple footprint render so consistency wins)
- Existing draw → analyze flow for parcels + sold + active is
  unchanged
- Redfin layers default off but toggle-restorable

## Notes for Copilot

- KK has memory rules: `feedback_no_many_to_many.md`, `feedback_git.md`,
  `feedback_copilot_handoff.md`, `feedback_no_coauthor_trailer.md`. Read
  them in the agent's onboarding pass.
- The existing `.saved-parcel-glow` pattern in `style.css` is the
  reference implementation for pulsing glow — match its keyframe shape,
  swap the colors. The fill-opacity is the critical knob KK wants visible
  through — opaque saved-parcel doesn't let you see the satellite, the
  new purple footprint must.
- The existing `propelio-pulse-marker` (cyan from Phase 1) can be deleted
  once Chunk C lands and the new purple is the canonical render. Or
  keep it under a `if (DEBUG_CYAN)` flag for fallback testing — your
  call.
- Latency on first-pull is 10-30 seconds. The button's "Pulling…" state
  needs to be visible the entire time. A small spinner inside or near
  the pill is welcome; a progress bar is overkill.
- Don't break the existing `feat(propelio)` history — the prior commits
  (`e9ed5cb`, `837a421`, `7df7885`, `a9643ac`) are the foundation.
