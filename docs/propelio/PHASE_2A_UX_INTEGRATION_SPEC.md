# Phase 2A — UX Integration Pass 1

> **Status:** spec v1, not yet implemented. Builds on Phase 2 MVP (already shipped) + parcel-match fix.
>
> **Branch (continue on):** `feat/propelio-deep-pull-experiment`
>
> **Goal:** Turn Phase 2's shipped infrastructure into the team's actual workflow. Right now the cache + deep-pull engine work but the team still has to click Get Comps to see anything. After this spec ships:
> - **Draw a polygon** → cached comps appear automatically, instantly, without clicking anything
> - **Click Get Comps** → triggers a full deep-pull (the 5-min saturation refresh)
> - **Visual progress** during the deep-pull so users know it's running
> - **Auto-save the workspace** on draw (so Get Comps doesn't have to)
>
> **Pass 2 (deferred):** batched render — comps appearing as each pass completes during the deep-pull run. Requires backend endpoint changes. Track in testing notes item 3.

---

## Defensive priorities

1. **DB safety**: no schema changes. No new tables. No data writes outside existing patterns.
2. **Backward compat**: the existing scrape path keeps working unchanged when `PHASE_2_CACHE_READ` env var is off.
3. **No new Propelio scraping triggers**: auto-cache-on-draw is CACHE-ONLY (never falls through to scraper). The only scrape trigger remains the user-clicked Get Comps button.
4. **User-visible scrape ops are explicit**: Get Comps now triggers a 5-min deep-pull. Progress UI keeps the user informed; navigation warning prevents accidental loss.

---

## What we're building

### Backend changes

#### 1. Add `cache_only` query parameter to `/api/propelio/by-polygon`

Add to `api/propelio/routes.py` — modify `_run_by_polygon` and/or the route handler.

**Behavior:**
- Request: `POST /api/propelio/by-polygon?cache_only=true` with the usual body
- When `cache_only=true` AND `PHASE_2_CACHE_READ=true` env var set:
  - Cache hit (rows returned): return cached comps in existing format with `cached: true`
  - Cache miss (zero rows): return `{cached: false, comps: [], cache_only: true, polygon_meta: {comps_in_polygon: 0, comps_outside_polygon: 0, comps_pulled: 0, centroid: {lat, lng}, subject_parcel: <derived or None>}}` WITHOUT triggering scrape
- When `cache_only=false` or not set: existing behavior unchanged (cache-first if env var on, scrape if cold)
- When `PHASE_2_CACHE_READ` env var NOT set: `cache_only` is ignored (falls through to existing behavior — protects against accidental enablement on dev)

**Pydantic schema:** `PolygonRequest` may stay as-is; pass `cache_only` as a Query param on the route handler. Default: `cache_only: bool = Query(False)`.

---

### Frontend changes

#### 2. Auto-cache-on-draw

In `frontend/map.js`, find where the polygon draw completes (likely after `L.Draw.Event.CREATED` handler or equivalent — currently fires the analysis flow). After the polygon is finalized:

a. **Auto-save the workspace.** Call the existing `saveCurrentArea(name)` flow (or its current equivalent) with a derived name. Pass the polygon and any derived metadata.

b. **Fire cache-only lookup.** Call `POST /api/propelio/by-polygon?cache_only=true` with the saved area's `area_id` and polygon.

c. **Render cached comps.** If response has comps, call existing `_renderPropelioComps(data)`. If empty, silently show a small chip near the Get Comps button: "Cache empty for this area — click Get Comps to scrape" (low-key, non-modal).

d. **Both calls are non-fatal.** Save failure (e.g., user not logged in) → log + skip cache lookup. Cache lookup failure → log + leave map empty so user can click Get Comps.

**Order matters:** save FIRST (gets `area_id`), THEN cache-only lookup (uses `area_id` for workspace-scoped rating join).

#### 3. Get Comps button repurpose

**HTML (in `frontend/index.html`):** keep the existing button ID, restructure inner content to show two-line label:

```html
<button id="btn-get-comps" class="btn-primary get-comps-btn">
  <span class="get-comps-main">Get Comps</span>
  <span class="get-comps-subtitle">Deep Pull · 5 min</span>
</button>
```

**JS (in `frontend/map.js`):** modify the Get Comps click handler. Instead of triggering the existing single-pass scrape via `/api/propelio/by-polygon`:

a. Validate state — must have a saved workspace (`_currentLoadedAreaId`) and a derivable target address.

b. Call `POST /api/propelio/deep-pull/start` with the target address.

c. Show progress banner with two parts:
   - Pass counter ("Pass 3 of 6")
   - Comp count so far ("87 comps captured")
   - Optional progress bar (visual fill 0/6 → 6/6)

d. Show prominent warning text: **"Don't refresh — comps are being saved in the background"**.

e. Show Cancel button — calls `/deep-pull/stop/{job_id}`.

f. Disable the Get Comps button while a deep-pull is running (greyed, cursor: not-allowed, tooltip: "Deep pull in progress").

g. Poll `/deep-pull/status/{job_id}` every 5s (existing pattern, reuse).

h. On completion: re-fire the cache-only lookup to pull the freshly-deep-pulled comps into the map. Hide banner after 6s "Job completed — N unique comps captured."

**Backwards compatibility:** the existing scrape path on `/api/propelio/by-polygon` (without `cache_only=true`) stays untouched and still works. We're just changing what the Get Comps BUTTON does.

#### 4. Quick Refresh placeholder button

Add a new button visually adjacent to Get Comps (split button or separate, designer's call). 

**HTML:**
```html
<button id="btn-quick-refresh"
        class="btn-secondary quick-refresh-placeholder"
        disabled
        title="Coming soon — rapid scan for new comps in the last 30 days within 1mi of target">
  <span class="quick-refresh-main">Quick Refresh</span>
  <span class="quick-refresh-subtitle">Coming soon</span>
</button>
```

**CSS:**
- `.quick-refresh-placeholder { opacity: 0.5; cursor: not-allowed; }`
- `.quick-refresh-placeholder:hover { opacity: 0.6; }`

No backend wiring. Visual only. Purpose: tease future capability for team rollout — "ohh something new coming."

#### 5. Workspace navigation warning during deep-pull

When `_activeDeepPullJobId` is set and the user attempts to:
- Click a different workspace in the saved-areas sidebar
- Draw a new polygon
- Open a saved parcel

...show a confirm dialog (use `window.confirm` for simplicity, or a custom inline notice):

```
"A deep pull is still running on this workspace.
It will continue saving comps in the background even if you switch.
Do you want to switch anyway?"
```

If user confirms: proceed with the new action. The deep-pull keeps running server-side; client-side polling can stop cleanly.

If user cancels: stay on current workspace.

---

### Files to modify

- `api/propelio/routes.py` — add `cache_only` query param handling
- `frontend/map.js` — auto-cache-on-draw, Get Comps repurpose, Quick Refresh wire-up, navigation warning
- `frontend/index.html` — Get Comps subtitle markup, Quick Refresh placeholder markup
- `frontend/style.css` — subtitle styling, disabled button, progress UI refinements

### Files NOT to modify

- `api/propelio/scraper.py` (unchanged)
- `api/propelio/archive.py` (unchanged from Phase 2 / parcel match fix)
- `api/propelio/deep_pull.py` (unchanged — the engine already supports what we need)
- `api/main.py` (no schema changes)

---

## Open design questions (decide during Copilot review)

### Q1. Workspace auto-save name on draw

What's the default workspace name when auto-saved on polygon completion?

- **A.** Derived from nearest-parcel address (e.g., "Workspace near 6800 Lakewood Blvd")
- **B.** Timestamp-based ("Workspace 2026-05-12 14:30")
- **C.** Generic ("Untitled Workspace N")
- **My lean:** A, falling back to B if no parcel match found

### Q2. Cache-miss messaging on auto-pull

What does the user see if cache is empty after a draw?

- **A.** Nothing — silently fail (user doesn't know cache was tried)
- **B.** Small inline chip "Cache empty — click Get Comps to scrape" near the Get Comps button
- **C.** Modal/banner "No cached comps for this area"
- **My lean:** B (informative without interrupting flow)

### Q3. Existing "Refresh from source" button — keep or remove?

The existing button fires the single-pass scrape (the OLD behavior). Now that Get Comps does deep-pull, what happens to it?

- **A.** Keep as "quick single-pass refresh" — power user escape hatch
- **B.** Remove entirely (Get Comps replaces it)
- **C.** Repurpose as the Quick Refresh placeholder (but it's a different feature target)
- **My lean:** A in short term (less disruptive), revisit when batched render ships and quota dynamics are clearer

### Q4. Get Comps state when a deep-pull is already running

- **A.** Button disabled while running (visual + functional)
- **B.** Warning dialog "Deep pull in progress — wait or cancel first"
- **C.** Silently start a NEW deep-pull (multiple concurrent jobs)
- **My lean:** A (cleanest, prevents confusing parallel-job UX)

### Q5. Workspace navigation block — confirm vs hard-lock vs warning toast

- **A.** Browser-style confirm dialog (current spec) — interruptive but clear
- **B.** Inline warning toast: "Deep pull running. Switch anyway?" with explicit confirm/dismiss buttons
- **C.** Hard-lock: physically disable sidebar buttons until done
- **My lean:** A for v1 (simplest), upgrade to B if confirm dialogs feel too modal

---

## Smoke test plan

After deploy:

### Auto-cache-on-draw

1. Open preview, log in as developer, hard refresh
2. Confirm `PHASE_2_CACHE_READ=true` is set on the Cloud Run service
3. Draw a polygon over Lakewood (or any pre-seeded area)
4. **EXPECTED:** workspace auto-saves (toast or sidebar update), cached comps appear within ~500ms, no Get Comps click needed
5. Check Cloud SQL: workspace row exists in `saved_areas`, cache-only request logged
6. Draw a polygon over a NEW area (no cache)
7. **EXPECTED:** workspace saves, but no comps render. Small "Cache empty" chip appears. No Propelio call fired (verify in quota log).

### Get Comps repurpose

1. With a saved workspace loaded, click Get Comps
2. **EXPECTED:** progress banner appears: "Pass 0/6, queued — first pass in ~10-30s"
3. Watch banner cycle Pass 1 → 6 over ~5-7 minutes
4. Try clicking Get Comps again during the run
5. **EXPECTED:** button disabled (greyed, not-allowed cursor, tooltip)
6. Try clicking a different workspace in the sidebar
7. **EXPECTED:** confirm dialog appears, "Deep pull is still running..."
8. Wait for completion
9. **EXPECTED:** banner shows "Job completed — N unique comps", cache refreshes, new comps appear on map

### Quick Refresh placeholder

1. Verify button is visible
2. Verify it's greyed/disabled
3. Hover over it → tooltip "Coming soon — rapid scan for new comps..."
4. Click it → nothing happens (or visible feedback that it's disabled)

### Cache-only flag (backend)

1. `curl https://lot-ledger-preview-qa7hokv3ma-uc.a.run.app/api/propelio/by-polygon?cache_only=true -X POST -H "Content-Type: application/json" -d '{"polygon": [[...], ...], "months": 24}'`
2. With covered area: returns cached comps, no Propelio call fired
3. With uncovered area: returns `{cached: false, comps: [], cache_only: true}`
4. Toggle `PHASE_2_CACHE_READ` off → `cache_only=true` should be ignored, behavior reverts to scrape

---

## What this does NOT do (Pass 2 work — track in testing notes item 3)

- **Batched render**: comps appearing on the map as each pass completes during a deep-pull run. Requires new backend endpoint for incremental comp fetches.
- **Auto-cache on workspace load**: when restoring a saved area, also auto-fire cache-only lookup before user has to interact. Phase 2A Pass 2.
- **Cache freshness indicators**: "Cached 4 days ago" + color cues. Phase 2A Pass 2.
- **Daily rate limits on Get Comps**: prevent quota burn from button-mashing. Phase 2.5.

---

## Estimated effort

- Backend (cache_only param): ~30 min
- Frontend auto-cache-on-draw + auto-save: ~45 min
- Frontend Get Comps repurpose + progress banner: ~60 min
- Frontend Quick Refresh placeholder: ~15 min
- Frontend navigation warning: ~30 min
- CSS polish (subtitles, disabled state, button arrangement): ~15-20 min
- **Total: ~3-4 hours Copilot work**

## Architecture compatibility check

All of these changes leverage already-shipped Phase 2 infrastructure:
- The cache-first read path in `_run_by_polygon` (Chunk 3)
- The deep-pull job lifecycle (`run_deep_pull`, `/deep-pull/start`, `/deep-pull/status`, `/deep-pull/stop`)
- The propelio_comps + comp_ratings tables (Chunk 1)
- Parcel match writes from deep-pull (the recent fix)

No new architectural concepts — just wiring the existing pieces into the team's actual workflow.
