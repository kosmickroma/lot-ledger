# Propelio Phase 3 — Workspace-Anchored Comps, Two-Tier Filters, Good/Bad Curation

> Six chunks for Copilot. Each chunk is independently testable.
> Run them in order. Smoke-test at end of each. Stop and report.
>
> Builds on Phase 2 (purple footprints, polygon-driven pulls). Phase 3
> is the design KK hashed out 2026-05-09: full-pool visibility (drop
> the in-polygon filter), append-only refresh tied to saved areas
> (treat saved_areas as "workspaces"), two-tier filter card (API-side
> needs refresh, client-side instant), good/bad comp tagging, status
> colors (sold/active/pending), and export-skips-bad.

## SCOPE LOCK (read first, every chunk)

**You are working in exactly one repository:** `lot-ledger-pro`, located at
`/home/kk/projects/clients/lot-ledger-pro/`.

**Forbidden — do not read, edit, run commands in, or otherwise touch:**

- `/home/kk/projects/clients/lot-ledger/`, `real-estate-comps/`, `deepfin/`, `redfin/`, `bylersonsgarage/`, or any sibling folder
- `/etc`, `~/.config`, `~/.claude`, anything outside the lot-ledger-pro tree

**Confirm understanding** before writing any code by replying:
> Scope confirmed: working only in /home/kk/projects/clients/lot-ledger-pro/

If at any point you find yourself wanting to read or modify a file
outside that path, **stop and report instead.**

## Hard rules across all chunks

- **No M2M.** Saved-area ↔ comps relationship is 1:N with the
  `(saved_area_id, comp_address_key)` UNIQUE constraint pattern.
- **No git pushes / commits without explicit user approval.** KK runs git.
- **Don't break existing flows:** parcel rendering, the analyze flow
  for parcels+sold+active, redfin layers, the search-bar address-driven
  Propelio pull, polygon-button mode.
- **Stop at end of each chunk and run the smoke test.** Wait for review
  before moving to next.
- **Match codebase style** — read 2-3 nearby files first.
- **Two databases.** This integration touches both:
  - `lotledger` (main DB): parcels tables, propelio_cache,
    propelio_quota_log
  - `lotledger_sessions` (session DB): saved_areas, saved_parcels,
    users, cached_jobs, analysis_sessions
  - **The new `propelio_comp_archive` table goes in the SESSION DB**
    so it can FOREIGN KEY to saved_areas with `ON DELETE CASCADE`.
    Use `api.config.get_session_conn()` / `release_session_conn()`,
    NOT `get_conn()`.

## ⚠️ DESIGN CLARIFICATION — Workspace = Anchor Parcel + Polygon (not just polygon)

**Updated 2026-05-09 by KK:** the spec's original definition of
"workspace = saved_areas row" is INCOMPLETE. The full mental model is:

> **Workspace = a TARGET (saved_parcel) + a POLYGON drawn around it.**
> The user is trying to value a SPECIFIC house; the polygon defines
> the geographic search area for comps that relate to that house.

### Current implementation gap

`api/propelio/routes.py:_nearest_subject_parcel` picks the **parcel
closest to the polygon's geometric centroid** as the Propelio subject.
That subject anchors what Propelio considers "comparable" — proximity,
lot size, age, etc. — for the 100 comps it returns.

**Result:** if Mike saves 4044 Williamsburg Rd as his target and draws
a polygon around it, but the polygon's centroid happens to land near
9912 Lakemont Dr (a random house 0.3mi away), Propelio's 100 comps
get ranked-relative-to-Lakemont — not Williamsburg. The user's target
is **not** the anchor.

### Fix that needs to happen (NOT in this Phase 3 cycle, but soon after)

The `/by-polygon` flow should use the **saved_parcel inside the polygon
as the Propelio subject**, falling back to centroid only when no saved
parcel exists in the area. Specifically:

1. Schema: add `saved_areas.anchor_parcel_id` (TEXT, nullable, FK or
   reference to `saved_parcels.id`) — the user-designated target
   property for this workspace. UI lets user set this when saving the
   area, or pick from saved_parcels inside the polygon at refresh time.
2. Route: `_nearest_subject_parcel` becomes `_resolve_subject_parcel`
   that prefers `anchor_parcel_id` → first `saved_parcels` row inside
   polygon → falls back to closest-to-centroid.
3. Frontend: when user clicks "Save area," prompt "Which saved parcel
   is your target for this area?" (default = the most-recently-saved
   parcel inside the polygon). Stored on `saved_areas.anchor_parcel_id`.
4. Validation: if anchor_parcel_id is set, ensure it's inside the
   polygon (or warn user). Re-pull recomputes off the new anchor if
   user changes it.

### Why this is critical (KK's words)

> "we are trying to get comps for the saved parcel remember thats very
> important... we use the center of that for the area but we really
> need relatable stuff to our target parcel 'house'"

Propelio's value proposition is **comps relevant to a subject property**
— same lot size, same neighborhood, same age band. If we anchor on a
random centroid parcel, the relevance ranking is misdirected and we
get less useful comps.

### Sequencing

Don't build this during the current Phase 3 chunks (A–F). Phase 3
foundation (archive, refresh, filters, good/bad, status colors) lands
first. Then a **Phase 3.5 mini-spec** wires anchor-parcel-as-subject
through the schema + route + UI. Estimate ~3 hours total.

When this lands, the spec section "Architectural decisions locked in"
gets updated to reflect: `Workspace = saved_areas row + anchor_parcel_id`.
Until then, treat the "workspace = saved_areas row" line below as a
v1 simplification.

## Architectural decisions locked in (no relitigating)

| Decision | Value |
|---|---|
| "Workspace" = | A row in `saved_areas` (existing table). No new abstraction. |
| Comp archive table location | Session DB (`lotledger_sessions`) |
| Refresh behavior | Append-only merge — never delete, only update existing rows + insert new |
| Comp uniqueness key | `(saved_area_id, comp_address_key)` where `comp_address_key` uses `_propelio_match_key` |
| API-side filter knobs (need refresh) | `months`, `range` |
| Client-side filter knobs (instant) | status, sold-within days, lot acres, sqft, year built, price, beds, baths |
| Color: sold | `#8b5cf6` (current bright purple) |
| Color: for_sale / active | `#dc2626` (red) |
| Color: pending | `#f59e0b` (amber) |
| Bad-comp visual | Same hue, ~30% opacity / desaturated |
| Bad-comp persistence | Stored in `user_rating` column on `propelio_comp_archive` |
| Bad-comp export behavior | Skipped from CSV/Excel exports; map shows them dull |
| Drop point-in-polygon filter? | **Yes.** Mask still applies for visual context. |
| Pending color confidence | Low — picked `#f59e0b` until client weighs in; easy to swap |

---

## Chunk A — Show all comps (drop point-in-polygon filter), status colors

**Goal:** unblock KK's testing immediately. Drop the in-polygon filter so
the user sees everything Propelio returned. Repaint comps by status:
sold=purple, active=red, pending=amber.

### Files to modify

- `api/propelio/routes.py` — remove the post-filter on `comps` in
  `get_by_polygon`, return all comps. Keep the `comps_in_polygon` count
  field but ALSO add `comps_outside_polygon` so the chip can show both.
- `frontend/map.js` — `_renderPropelioComps` reads `comp.status` and
  picks color via a small helper.
- `frontend/style.css` — three new classes: `.propelio-footprint-glow.sold`,
  `.propelio-footprint-glow.active`, `.propelio-footprint-glow.pending`
  for the SVG paths; same for `.propelio-fallback-dot` variants.

### Implementation notes

- The existing `.propelio-footprint-glow` becomes a base class. Add
  modifier classes per status.
- For `comp.status` values: Propelio returns `"sold"`, `"for_sale"`,
  `"pending"`. Treat anything else as `"active"` fallback.
- Update `propelioCmaChip.setData` to render: `"54 inside polygon · 46
  outside · 100 fetched · 101 total in window"` so the math is obvious.

### Smoke test

1. Draw polygon over Glenridge / Williamsburg
2. Click "Get Comps"
3. Verify 100 footprints render (not just the in-polygon subset)
4. Verify color split: ~most purple (sold), some red (for_sale), few amber (pending)
5. Verify chip shows the 4-number breakdown

---

## Chunk B — `propelio_comp_archive` table + workspace persistence

**Goal:** when a user clicks "Get Comps" on a saved-area workspace, the
returned comps persist to `propelio_comp_archive`. Reopening the area
hydrates from the archive (no Propelio call). A separate "Refresh" button
re-pulls from Propelio and smart-merges.

### Files to create

- `api/propelio/archive.py` — table creation, smart-merge helpers,
  archive-load helper.

### Files to modify

- `api/propelio/routes.py` — extend `/by-polygon` to accept optional
  `saved_area_id` query param. When present, merge comps to archive.
  New endpoint `POST /api/propelio/refresh` that re-pulls + merges for a
  saved area. New endpoint `GET /api/propelio/by-saved-area?saved_area_id=...`
  that returns from archive without Propelio call.
- `api/main.py` — wire archive-merge into the existing save-area flow
  (when an area is saved, its current Propelio pull persists).

### Schema

```sql
CREATE TABLE IF NOT EXISTS propelio_comp_archive (
  id                    SERIAL PRIMARY KEY,
  saved_area_id         TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
  comp_address_key      TEXT NOT NULL,
  comp_mls              TEXT,
  comp_data             JSONB NOT NULL,             -- full asdict(Property) blob
  parcel_geom           JSONB,                      -- denormalized from parcel_match for fast render
  parcel_account_num    TEXT,
  status                TEXT,                       -- denormalized from comp_data for filter speed
  last_status           TEXT,                       -- alias of status; tracked for history
  last_price            NUMERIC,                    -- denormalized
  user_rating           TEXT,                       -- 'good' | 'bad' | NULL
  rating_at             TIMESTAMPTZ,                -- when user_rating was set
  first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (saved_area_id, comp_address_key)
);
CREATE INDEX IF NOT EXISTS propelio_comp_archive_area
  ON propelio_comp_archive (saved_area_id);
CREATE INDEX IF NOT EXISTS propelio_comp_archive_rating
  ON propelio_comp_archive (saved_area_id, user_rating)
  WHERE user_rating IS NOT NULL;
```

`ensure_tables()` runs on import like `cache.py`. Use SESSION DB
connection.

### Smart-merge helper

```python
def merge_comps_into_archive(
    saved_area_id: str,
    comps: list[dict],
) -> dict[str, int]:
    """Upsert comps into the archive for a saved area.

    For each comp:
      - If (saved_area_id, comp_address_key) exists → UPDATE last_seen_at,
        comp_data, parcel_geom, status, last_price (preserving user_rating)
      - Else → INSERT (sets first_seen_at + last_seen_at = NOW)

    Returns {"inserted": N, "updated": M, "total": T}
    """
```

### Archive load helper

```python
def load_archived_comps(
    saved_area_id: str,
) -> list[dict]:
    """Return all comps for a saved area, hydrated as the same dict shape
    the frontend already consumes (status, address, price, parcel_geom,
    extra, etc.) plus user_rating.
    """
```

### Route changes

- `POST /api/propelio/by-polygon`:
  - New optional `saved_area_id` body field (string)
  - When present: after the Propelio pull + parcel-match enrichment,
    call `merge_comps_into_archive(saved_area_id, comps)`. Include the
    merge summary in the response under `archive_meta`.
- `POST /api/propelio/refresh`:
  - Body: `{"saved_area_id": "...", "months": 24, "range": null}`
  - Loads the saved area's polygon, re-runs the polygon search, merges,
    returns the full archive set (post-merge).
- `GET /api/propelio/by-saved-area?saved_area_id=...`:
  - Returns the archive's current state for that area as `{"comps": [...]}`
  - Same shape as `/by-polygon` minus the polygon_meta block.

### Smoke test

1. Draw polygon, save the area (existing save flow), click Get Comps
2. Verify `propelio_comp_archive` has N rows for that saved_area_id
3. Reopen the saved area: verify `/api/propelio/by-saved-area` returns
   the same N comps without a fresh Propelio call (check Cloud Run logs
   show no `/legacy/cma/*` request)
4. Click a "Refresh" button → re-fetches Propelio, last_seen_at updates
   on existing rows, possibly inserts new
5. Delete the saved area → archive rows cascade out (verify via SQL
   `SELECT COUNT(*) FROM propelio_comp_archive WHERE saved_area_id = ?`)

---

## Chunk C — Propelio-specific filter card in sidebar

**Goal:** clone the existing comps/listings filter UX into a dedicated
Propelio card. Two tiers: API-side filters with explicit "Refresh"
button, client-side filters that update instantly.

### Files to modify

- `frontend/index.html` — new card inside the sidebar, below the
  existing comps/listings card
- `frontend/style.css` — minimal additions; reuse existing
  `.filter-card`, `.filter-row`, `.numeric-filter-row`, etc.
- `frontend/map.js` — filter state + apply function

### Card structure

```html
<section id="propelio-filters" class="filter-card collapsible-section">
  <div class="filter-card-head">
    <button class="section-toggle" data-target="propelio-filters-body">
      <span>Propelio Filters</span>
    </button>
    <button class="filter-reset-btn">Reset</button>
  </div>
  <div id="propelio-filters-body" class="collapsible-body">

    <!-- API-side: needs refresh -->
    <div class="propelio-api-filters">
      <div class="filter-row">
        <label>Time window (mo)</label>
        <input type="number" id="prop-months" min="1" max="60" value="24" />
      </div>
      <div class="filter-row">
        <label>Search radius (mi)</label>
        <input type="number" id="prop-range" min="0.1" max="10" step="0.1" value="1.0" />
      </div>
      <button id="prop-refresh-btn" class="propelio-refresh-btn">↻ Refresh from Propelio</button>
    </div>

    <!-- Client-side: instant filters -->
    <div class="propelio-client-filters">
      <div class="filter-row">
        <label>Status</label>
        <input type="checkbox" id="prop-status-sold" checked />
        <label for="prop-status-sold">Sold</label>
        <input type="checkbox" id="prop-status-active" checked />
        <label for="prop-status-active">Active</label>
        <input type="checkbox" id="prop-status-pending" checked />
        <label for="prop-status-pending">Pending</label>
      </div>
      <div class="filter-row">
        <label>Sold within (days)</label>
        <input type="number" id="prop-sold-within" placeholder="any" />
      </div>
      <div class="numeric-filter-row">
        <label>Lot size (acres)</label>
        <input type="number" id="prop-lot-min" placeholder="min" step="0.01" />
        <input type="number" id="prop-lot-max" placeholder="max" step="0.01" />
      </div>
      <div class="numeric-filter-row">
        <label>Living sqft</label>
        <input type="number" id="prop-sqft-min" placeholder="min" />
        <input type="number" id="prop-sqft-max" placeholder="max" />
      </div>
      <div class="numeric-filter-row">
        <label>Year built</label>
        <input type="number" id="prop-year-min" placeholder="min" />
        <input type="number" id="prop-year-max" placeholder="max" />
      </div>
      <div class="numeric-filter-row">
        <label>Price</label>
        <input type="text" id="prop-price-min" placeholder="any (1m, 500k)" />
        <input type="text" id="prop-price-max" placeholder="any (1m, 500k)" />
      </div>
    </div>
  </div>
</section>
```

### Behavior

- **Refresh button** fires `POST /api/propelio/refresh` with the current
  saved_area_id (if one is loaded) OR re-fires `/by-polygon` for the
  current `lastPolygon`. Spinner during the 10-30s wait.
- **Client-side filters** run on every input change, debounced 150ms.
  Hide/show comps on the map (don't remove from data — just toggle layer
  membership). Update sidebar comp list (Chunk D).
- **Filter state** persists per saved area to `saved_areas.filter_state`
  jsonb column (existing). Restored on area reload.

### Smoke test

1. Pull comps for Glenridge polygon
2. Card auto-opens, shows N comps total
3. Type "1500" in price-min — comps under $1.5M disappear from map +
   sidebar list (Chunk D); count updates
4. Uncheck "Active" — only sold + pending remain
5. Change months from 24 to 12 → click Refresh → fresh fetch, archive
   updates
6. Reload page → filter values persist

---

## Chunk D — Good/bad comp tagging + sidebar comp list

**Goal:** repurpose the verify-vacant toolbox pattern (per KK) for
Propelio comps. Click a comp → mark good or bad. Bad comps go dull on
the map and disappear from the sidebar list.

### Files to modify

- `frontend/map.js` — extend `_propelioBuildPopup` to include good/bad
  buttons; new render path for the sidebar comp list
- `api/propelio/routes.py` — new endpoint `POST /api/propelio/comp/rate`
  that updates `user_rating` for a comp
- `api/propelio/archive.py` — `set_comp_rating(saved_area_id,
  comp_address_key, rating)` helper
- `frontend/style.css` — dull/desaturated variants for bad comps
- `frontend/index.html` — sidebar list inside the propelio-filters card

### Popup additions

```
[address line]
[$price · status]
[meta lines...]

✓ Good comp     ✗ Bad comp     Clear
```

Click handlers POST to `/api/propelio/comp/rate` with
`{saved_area_id, comp_address_key, rating}`. On success, update the
`user_rating` field locally and re-render the map color + sidebar list.

### Sidebar comp list

Inside the propelio-filters card, below the filter strip:

```html
<div class="propelio-comp-list" id="propelio-comp-list">
  <!-- Populated by JS, sorted by user-selected key -->
</div>
```

Each row:

```
┌──────────────────────────────────────────────────┐
│ Glenridge Estates 2                  $1,675,000  │
│ 3947 Beechwood Ln · for_sale · 4337 sqft         │
└──────────────────────────────────────────────────┘
```

- Only comps with `user_rating != 'bad'` appear in the list (matches
  KK's spec: bad comps stay on map dull, drop from list).
- Sort options: price (default), sqft, year built, distance from polygon
  centroid. Sort dropdown above the list.
- Click row → flyTo comp + open its map popup.
- Hover row → highlight comp's footprint via a temporary
  `.propelio-footprint-highlight` class.

### Color CSS

```css
.propelio-footprint-glow { /* base, sold purple */ }
.propelio-footprint-glow.sold { fill: #8b5cf6; stroke: #8b5cf6; ...drop-shadow rgba(139,92,246) }
.propelio-footprint-glow.active { fill: #dc2626; stroke: #dc2626; ...drop-shadow rgba(220,38,38) }
.propelio-footprint-glow.pending { fill: #f59e0b; stroke: #f59e0b; ...drop-shadow rgba(245,158,11) }

.propelio-footprint-glow.bad-comp {
  opacity: 0.32;
  filter: saturate(0.4);
}
```

Fallback dots get the same modifier classes.

### Smoke test

1. Pull comps for a saved area
2. Click a sold comp footprint → popup → mark "Bad comp"
3. That footprint goes dull on the map immediately
4. That comp disappears from the sidebar list
5. Reload the saved area → bad-comp tagging persists
6. Mark another as Good → still bright, still in list, but row gets a
   small "✓ Good" badge

---

## Chunk E — Export integration: skip bad comps

**Goal:** the existing CSV export endpoint
(`/api/download/{job_id}` in `api/main.py:2829`) doesn't know about
Propelio comps. Wire Propelio archive into the existing export so that
when a user downloads a saved-area's CSV, only good (or unrated) Propelio
comps appear; bad ones are skipped.

### Files to modify

- `api/main.py` — extend `_extract_propelio_data_for_download` (write
  this helper if absent) that pulls archive rows for the saved-area
  associated with the job, filters out `user_rating = 'bad'`, returns
  list-of-dicts.
- The CSV header row (`api/main.py:2887`) gets new columns prefixed with
  `Propelio ` for fields the user actually cares about: address, price,
  status, sold_date, dom, sqft, lot_size, year_built, neighborhood, MLS#,
  user_rating.
- The CSV writer loop appends one **extra row per Propelio comp** so each
  comp gets its own line. (Don't try to inline them into parcel rows;
  too many comps per parcel for that.)

### Smoke test

1. Pull comps, mark 2 as bad, mark 3 as good, leave rest unrated
2. Download CSV
3. Open in Excel: bad comps NOT present; good + unrated comps DO appear
   as their own rows with `Propelio ` columns populated
4. The existing parcel rows are unchanged

---

## Chunk F — Backburner: probe Propelio for >100 cap

**Goal (low priority):** see if we can break the 100-comp cap. Probably
can't — but worth ~30 min of investigation.

### What to try (in this exact order, stop on first win)

1. Add `?limit=200` and `?per_page=200` query params to the existing
   `/legacy/cma/search/{lead_id}/{cma_id}` POST. Check if response
   contains >100 sales.
2. Add `?page=2` or `?offset=100` query params. Check for pagination.
3. Inspect the raw response body for any `next`, `cursor`, `pagination`,
   or `links` fields we missed in the original capture.
4. Check if `/legacy/cma/{lead_id}` (no `/search`) returns a different
   shape for already-generated CMAs — maybe more than 100 there.
5. Check Propelio's web UI again with dev tools open: if a user scrolls
   the comps list, does a SECOND request fire? That'd be the pagination.

### Outcome

- If we can break the cap: implement multi-page pull behind the existing
  `/refresh` endpoint. Costs more time per refresh but pulls a deeper
  archive.
- If we can't: cap is hard. Document and move on. Append-only refresh
  pattern (Chunk B) still combats the cap over time as Propelio's
  pool churns.

### Smoke test

Just write up findings in `docs/propelio/CAP_INVESTIGATION.md` (new
file, gitignored or not — your call). No code change unless we find a
way to break the cap.

---

## Verification rolling forward

After all six chunks land, verify end-to-end:

1. Search address → flies to it, no Propelio data shown (search-bar
   path stays disabled per current behavior)
2. Draw polygon → "Get Comps" pill → click → 100 comps render with
   status colors (sold purple, active red, pending amber). Chip shows
   inside/outside/total breakdown.
3. Save area → archive populates. Refresh later → smart-merge.
4. Reopen saved area → comps hydrate from archive instantly (no
   credit burn).
5. Filter card narrows the visible set. API-side filters fire refresh.
6. Click comp → mark good/bad → persists across reloads.
7. CSV download includes only non-bad Propelio comps.

## What this does NOT change

- Existing parcel render paths (DCAD/TAD/Collin/Denton)
- Existing redfin sold/active layers (still default OFF post-Phase 2)
- Existing analyze flow (parcels + sold + active inside drawn polygon)
- Existing search-bar + Propelio auto-fire (unchanged from current state)
- Polygon-button and address-search Propelio entry points

## Notes for Copilot

- KK has memory rules: `feedback_no_many_to_many.md`, `feedback_git.md`,
  `feedback_copilot_handoff.md`, `feedback_no_coauthor_trailer.md`.
- The session DB connection helpers are `get_session_conn()` and
  `release_session_conn()` in `api/config.py`. Distinct from the main
  DB's `get_conn()` / `release_conn()`.
- The propelio comp matcher (`api/propelio/parcel_match.py`) and route
  layer (`api/propelio/routes.py`) are existing — don't reimplement,
  extend.
- The verification toolbox pattern lives around
  `frontend/map.js:4214-4331`. Use it as a reference for the good/bad
  comp UI but do NOT modify the existing verification logic — bad/good
  for Propelio is a parallel system tied to comps not parcels.
