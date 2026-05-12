# Phase 2A — UX Integration Pass 1

> **Status:** spec v2, not yet implemented. Builds on Phase 2 MVP (already shipped) + parcel-match fix.
>
> **Branch (continue on):** `feat/propelio-deep-pull-experiment`
>
> **Revision v2 (2026-05-12):** Addresses Copilot first-review findings. Key changes:
> - Cache_only miss response shape FULLY enumerated (was missing polygon_meta/cma_settings/balance/subject parity)
> - Get Comps button architecture corrected — it's a DYNAMIC sticky map control created at `map.js:4385-4392`, NOT static HTML. Modify the JS that creates it, not index.html.
> - Auto-save reuses existing pattern from `_pullPropelioByPolygon` (`map.js:4448-4457`) — no new save semantics
> - Name derivation uses existing helper `_suggestAreaNameFromContainedParcels` (`map.js:7188`) with timestamp fallback
> - Deep-pull role gate decision documented (drop or keep). New blocker section explicitly addresses this.
> - Cache_only handler ordering: explicit early-return BEFORE polygon cache lookup, scraper, archive merge, quota logging
> - Existing "Refresh from source" button: KEEP unchanged (not repurposed, not removed)
> - Open design Q1-Q5 all resolved with confirmed answers
> - Workspace name dedupe explicitly deferred to Pass 2 (v1 accepts duplicate auto-saves on redraw)
>
> **Goal:** Turn Phase 2's shipped infrastructure into the team's actual workflow. After this spec ships:
> - **Draw a polygon** → cached comps appear automatically, instantly, without clicking anything
> - **Click Get Comps** → triggers a full deep-pull (the 5-min saturation refresh)
> - **Auto-save the workspace** on draw (so Get Comps doesn't have to)
> - **Visual progress** during the deep-pull
>
> **Pass 2 (deferred):** batched render — comps appearing as each pass completes during the deep-pull run. Requires backend endpoint changes. Track in testing notes item 3.

---

## Defensive priorities

1. **DB safety**: no schema changes. No new tables. No data writes outside existing patterns.
2. **Backward compat**: the existing scrape path keeps working unchanged when `PHASE_2_CACHE_READ` env var is off.
3. **No new Propelio scraping triggers**: auto-cache-on-draw is CACHE-ONLY (never falls through to scraper). The only scrape trigger remains the user-clicked Get Comps button.
4. **User-visible scrape ops are explicit**: Get Comps now triggers a 5-min deep-pull. Progress UI keeps the user informed; navigation warning prevents accidental loss.
5. **Preserve existing Refresh from source button**: do NOT remove or repurpose. Keep as power-user single-pass escape hatch.

---

## ⚠️ Blocker: deep-pull role gate decision (must resolve before implementation)

Current state: `/api/propelio/deep-pull/start` and friends are gated to `developer` and `owner` roles only at `api/propelio/routes.py:65-68`. Per Mike's team's role structure (developer / owner / power_user / user / member), the `user` and `member` roles WOULD 403 if they clicked the new Get Comps button.

Three options to decide before implementation:

**A. Drop the role gate entirely.** Any authenticated user can trigger a deep-pull. Simplest. Risk: quota burn if power users / members button-mash. Mitigation: add a per-user daily rate limit (Phase 2.5 follow-on).

**B. Keep the role gate.** Get Comps only works for developer/owner/power_user. user/member roles see Get Comps disabled with tooltip "Deep refresh requires power_user+. Use Refresh from source for single-pass scrape."

**C. New non-gated endpoint.** Create `/api/propelio/deep-pull/start-public` that's open to all users but with built-in rate limiting (e.g., max 1 per user per hour, max 3 per workspace per day).

**Recommendation: A** (drop the gate), pair with a per-user rate-limit added in a Phase 2.5 chunk. Simpler now, adds the safety later when quota patterns are known. Document the trade-off in testing notes.

**This decision determines:**
- Whether the Get Comps button check role at all
- What the visible behavior is for low-privilege users

Spec assumes Option A below. Toggle if KK decides differently.

---

## What we're building

### Backend changes

#### 1. Add `cache_only` query parameter to `/api/propelio/by-polygon`

**Add to** `api/propelio/routes.py` — modify the route handler signature and the cache gate block inside `_run_by_polygon`.

```python
# At the route handler (~routes.py:693)
@router.post("/by-polygon")
async def by_polygon(
    request: PolygonRequest,
    cache_only: bool = Query(False),
) -> dict[str, Any]:
    return await _run_by_polygon(request, use_cache=True, cache_only=cache_only)
```

**Behavior in `_run_by_polygon`** — add at the TOP of the existing cache gate block (currently at `routes.py:325-401`), BEFORE the polygon cache lookup, scraper call, archive merge, or quota log:

```python
if os.environ.get("PHASE_2_CACHE_READ") == "true":
    cached_global = load_comps_by_polygon(polygon, saved_area_id)
    # ... existing centroid + subject derivation
    
    # When cache_only=true, ALWAYS return immediately — whether cache hit or miss.
    # NEVER fall through to scraper, polygon cache, or archive merge.
    if cache_only:
        return {
            "cached": bool(cached_global),
            "cache_only": True,
            "comps": cached_global if cached_global else [],
            "polygon_meta": polygon_meta,  # same shape as cache-hit (centroid, comps_in_polygon, comps_outside_polygon, comps_pulled, subject_parcel)
            "cma_settings": {
                "months": int(request.months),
                "range": "(cached)" if cached_global else "(cache-only)",
                "sales_count": len(cached_global),
            },
            "balance": cache_mod.latest_quota_balance() if cached_global else None,
            "subject": subject,
            # archive_meta intentionally omitted in cache_only path
        }
    
    # cache_only=false → existing behavior (cache-hit returns, cache-miss falls through)
    if cached_global:
        # ... existing rich-response build
        return { ... }
    # ... existing fall-through to scrape path
```

**Key invariants:**
- `cache_only=true` ALWAYS returns from this block (whether comps found or not)
- `cache_only=true` NEVER triggers `propelio_cache.get_cached`, scraper, archive merge, or quota log
- `cache_only=false` preserves all existing behavior exactly
- Response shape on cache_only-miss matches cache-hit shape (all parity fields populated) so frontend renders normally

**When `PHASE_2_CACHE_READ` env var is NOT set:** `cache_only` is silently ignored. The existing scrape path runs as today. (Protects dev from accidental enablement.)

---

### Frontend changes

#### 2. Auto-cache-on-draw + auto-save

**Integration point:** in `frontend/map.js`, the polygon-completion handler is the `draw:created` event handler at `map.js:6894`. The polygon state is finalized at `map.js:6929-6936` (where `lastPolygon` and `lastDrawnLatLngs` are set). Hook right after that, BEFORE the existing Get Comps button is shown at `map.js:6943`.

**Logic:**

```javascript
// After lastPolygon is set at map.js:6929-6936:

// Step 1: auto-save the workspace using the same pattern as the
// existing pre-pull auto-save at map.js:4448-4457.
// Use _suggestAreaNameFromContainedParcels (map.js:7188) as the
// primary name source; fall back to timestamp if no parcels matched.
const suggestedName = _suggestAreaNameFromContainedParcels()
    || `Workspace ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`;

let savedAreaId = null;
try {
    const saved = await saveCurrentArea(suggestedName);
    savedAreaId = saved?.id || null;
} catch (err) {
    console.warn("[auto-cache-on-draw] auto-save failed, continuing without area_id:", err);
}

// Step 2: fire cache-only lookup
try {
    const resp = await _apiJson("/api/propelio/by-polygon?cache_only=true", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
            polygon: lastPolygon,
            months: 24,
            saved_area_id: savedAreaId,
        }),
    });
    
    if (resp.comps && resp.comps.length > 0) {
        // Render cached comps using existing path
        _renderPropelioComps(resp);
        console.log("[auto-cache-on-draw] cache hit:", resp.comps.length, "comps");
    } else {
        // Cache miss — show small inline chip (Q2 answer)
        _showCacheEmptyChip();
        console.log("[auto-cache-on-draw] cache miss for this area");
    }
} catch (err) {
    console.warn("[auto-cache-on-draw] cache lookup failed:", err);
}
```

**Helper to add:**

```javascript
function _showCacheEmptyChip() {
    // Show a small dismissable chip near the Get Comps button:
    // "Cache empty — click Get Comps for fresh data"
    // Auto-fade after 6s
}
```

**Known limitation (v1, deferred polish):** re-drawing the polygon creates a new auto-saved workspace each time. No dedupe in v1. User can delete duplicates from the sidebar. Track in testing notes for Pass 2 cleanup.

#### 3. Get Comps button repurpose

**IMPORTANT:** the Get Comps button is NOT static HTML — it's a dynamic sticky map control created at `map.js:4385-4392`. Modify the JS that creates it, not index.html.

**Steps:**

a. **At creation (`map.js:4385-4392`):** when the button element is constructed, restructure its inner HTML to show two lines:

```javascript
btn.innerHTML = `
    <span class="get-comps-main">Get Comps</span>
    <span class="get-comps-subtitle">Deep Pull · ~5 min</span>
`;
```

b. **Click handler (`map.js:4475`)** — currently calls `/api/propelio/by-polygon`. Change to call `/api/propelio/deep-pull/start` instead. The deep-pull endpoint requires `target_address`:

```javascript
btn.addEventListener("click", async () => {
    // Guard: don't allow click if a deep-pull is already running
    if (_activeDeepPullJobId) {
        return;  // button is also visually disabled in this state
    }
    
    // Derive target address. Preference order:
    //   1. _lastSearchedAddress (typeahead-set)
    //   2. _suggestAreaNameFromContainedParcels (saved parcels in polygon)
    //   3. fallback: workspace name (less ideal — has timestamp prefix)
    const targetAddress = _lastSearchedAddress
        || _suggestAreaNameFromContainedParcels()
        || (workspace?.name);  // last resort
    
    if (!targetAddress) {
        _showInlineWarning("Search for an address or save a parcel first");
        return;
    }
    
    try {
        const resp = await _apiJson("/api/propelio/deep-pull/start", {
            method: "POST",
            headers: { "Content-Type": "application/json", ...authHeaders() },
            body: JSON.stringify({
                target_address: targetAddress,
                saved_area_id: _currentLoadedAreaId || null,
            }),
        });
        _activeDeepPullJobId = resp.job_id;
        _disableGetCompsButton();
        _showDeepPullBanner(`Pass 0/6 — queued. ${resp.job_id}`);
        _showDoNotRefreshWarning();
        _startDeepPullPolling();
    } catch (err) {
        console.error("[get-comps] deep-pull failed to start:", err);
    }
});
```

c. **Disabled state when deep-pull running** — add CSS class + visual:

```css
#btn-get-comps[disabled],
#btn-get-comps.is-running {
    opacity: 0.55;
    cursor: not-allowed;
    pointer-events: none;
}
```

d. **Progress banner — reuse existing `.deep-pull-banner` CSS at `style.css:3049`.** The polling logic from `_pollDeepPullStatus` at `map.js:8369` is reusable. Just swap the trigger source from the experimental Deep Pull button to the production Get Comps button.

e. **Cancel button** — visible during the run inside the banner, calls `/deep-pull/stop/{job_id}` (existing).

f. **On deep-pull completion:** banner shows summary, then after 6s:
- Hide banner
- Re-enable Get Comps button
- Re-fire the cache-only lookup to pull the freshly-deep-pulled comps into the map
- Render the updated set

g. **Deep-pull role gate** — assuming Option A from blocker section: drop the role gate. If we keep it: Get Comps shows as disabled for user/member roles with tooltip "Requires power_user+ — use Refresh from source for quick refresh."

#### 4. Quick Refresh placeholder button

Add a new button visually adjacent to Get Comps (also dynamic — created in the same map.js section). 

```javascript
const quickRefreshBtn = L.DomUtil.create("button", "quick-refresh-placeholder", container);
quickRefreshBtn.disabled = true;
quickRefreshBtn.innerHTML = `
    <span class="quick-refresh-main">Quick Refresh</span>
    <span class="quick-refresh-subtitle">Coming soon</span>
`;
quickRefreshBtn.title = "Coming soon — rapid scan for new comps in last 30 days within 1mi of target";
```

**CSS:**

```css
.quick-refresh-placeholder {
    opacity: 0.5;
    cursor: not-allowed;
}
.quick-refresh-placeholder:hover {
    opacity: 0.6;
}
.quick-refresh-subtitle {
    font-size: 0.8em;
    font-style: italic;
}
```

No backend wiring. Visual only.

#### 5. Workspace navigation warning during deep-pull

Use `window.confirm` for v1 (consistent with existing use at `map.js:8028`).

Hook into the existing workspace-switch handler (find via search for `restoreSavedArea` or sidebar click handler) and the draw-mode entry handler:

```javascript
function _navigationGuardForActiveDeepPull(action_description) {
    if (!_activeDeepPullJobId) return true;
    return window.confirm(
        "A deep pull is still running on this workspace. " +
        "It will continue saving comps in the background even if you switch. " +
        `Do you want to ${action_description} anyway?`
    );
}

// Wrap calls to restoreSavedArea, draw-start, etc.:
if (!_navigationGuardForActiveDeepPull("switch workspace")) return;
restoreSavedArea(area);
```

If user proceeds: deep-pull continues server-side, client just stops polling. If they return to the workspace later (e.g., re-open it), the polling can resume via the saved job_id IF we store it (in-memory only for v1 — survives session, not page refresh).

---

## Files to modify

- `api/propelio/routes.py` — add `cache_only` query param + explicit early-return logic
- `frontend/map.js` — auto-cache-on-draw at draw completion, Get Comps button creation modifications (subtitle + click handler swap), Quick Refresh placeholder creation, navigation warning hooks
- `frontend/style.css` — subtitle styles, disabled button, quick-refresh-placeholder, cache-empty chip

## Files NOT to modify

- `api/propelio/scraper.py` (unchanged)
- `api/propelio/archive.py` (unchanged)
- `api/propelio/deep_pull.py` (unchanged)
- `api/main.py` (no schema changes)
- `frontend/index.html` (Get Comps + Quick Refresh are dynamic, not static — no HTML changes needed)
- The existing "Refresh from source" button (`index.html:95` + handler `map.js:4246`) stays UNCHANGED

---

## Resolved open design questions

**Q1. Auto-save name derivation:**
✓ Use existing `_suggestAreaNameFromContainedParcels` (`map.js:7188`) as primary, timestamp-formatted name as fallback. Matches existing auto-save pattern from `_pullPropelioByPolygon`.

**Q2. Cache-miss messaging on auto-pull:**
✓ Small inline chip "Cache empty — click Get Comps for fresh data" near the Get Comps button. Auto-fade after 6s.

**Q3. Existing "Refresh from source" button:**
✓ KEEP unchanged. Acts as the explicit single-pass refresh escape hatch. Revisit retirement after Pass 1 adoption data.

**Q4. Get Comps state while deep-pull running:**
✓ Visually disabled (opacity 0.55, not-allowed cursor) + click handler bails. Aligned with existing `_activeDeepPullJobId` guard.

**Q5. Navigation warning UX:**
✓ `window.confirm` for v1 (consistent with `map.js:8028` existing use). Upgrade to inline toast if confirm dialogs feel too modal.

---

## Smoke test plan

After deploy:

### Auto-cache-on-draw

1. Open preview, log in, hard refresh
2. Confirm `PHASE_2_CACHE_READ=true` is set on Cloud Run service
3. Draw a polygon over Lakewood (or any pre-seeded area)
4. **EXPECTED:** workspace auto-saves (visible in sidebar), cached comps appear within ~500ms WITHOUT clicking anything
5. Check Cloud SQL: workspace row exists in `saved_areas`
6. Draw a polygon over a NEW area (no cache coverage)
7. **EXPECTED:** workspace saves, no comps render, small "Cache empty" chip appears. Verify in Propelio quota log: NO new call fired.

### Get Comps repurpose

1. With a saved workspace loaded, click Get Comps
2. **EXPECTED:** progress banner "Pass 0/6 — queued. dp_xxx"
3. Watch banner cycle Pass 1 → 6 over ~5-7 minutes
4. Click Get Comps again during run
5. **EXPECTED:** disabled state (opacity 0.55, no action)
6. Click different workspace in sidebar
7. **EXPECTED:** window.confirm dialog "Deep pull still running..."
8. Wait for deep-pull completion
9. **EXPECTED:** banner summary, then ~6s later cache refreshes and new comps appear on map

### Quick Refresh placeholder

1. Hover button → tooltip "Coming soon — rapid scan..."
2. Visible but disabled (opacity 0.5, no click action)

### cache_only=true endpoint smoke

```bash
# Cache hit (covered area)
curl -X POST 'https://lot-ledger-preview-qa7hokv3ma-uc.a.run.app/api/propelio/by-polygon?cache_only=true' \
  -H "Cookie: lot_ledger_session=..." \
  -H "Content-Type: application/json" \
  -d '{"polygon": [[lng1,lat1],...], "months": 24}'
# Expect: {cached: true, cache_only: true, comps: [...], polygon_meta: {...}, cma_settings: {...}, balance: ..., subject: ...}

# Cache miss (uncovered area)
# Same call with polygon over a new area
# Expect: {cached: false, cache_only: true, comps: [], polygon_meta: {...}, cma_settings: {...}, balance: null, subject: ...}

# Verify NO new Propelio call
# Check propelio_quota_log: latest entry should be unchanged from before the request
```

### Verify cache_only is ignored when env var off

1. Toggle `PHASE_2_CACHE_READ` off on a test deploy (don't do this on preview during testing)
2. Same `cache_only=true` request
3. **EXPECTED:** falls through to existing scrape path, returns scrape results

---

## What this does NOT do (Pass 2 work)

- **Batched render** (comps appearing on map as each pass completes during a deep-pull run). Requires new backend endpoint. Phase 2A Pass 2.
- **Auto-cache on workspace load** (restoring a saved area also auto-fires cache-only lookup). Phase 2A Pass 2.
- **Cache freshness indicators** ("Cached 4 days ago" + color cues). Phase 2A Pass 2.
- **Daily rate limits on Get Comps** to prevent quota burn. Phase 2.5.
- **Auto-save dedupe** to prevent workspace spam on re-draws. Phase 2.5.
- **Workspace name dedupe** if multiple draws produce identical-looking polygons. Phase 2.5.
- **Page-refresh recovery for active deep-pulls** (currently `_activeDeepPullJobId` is in-memory only). Phase 2.5.

---

## Estimated effort

- Backend (cache_only param + response shape parity): ~45 min
- Frontend auto-cache-on-draw + auto-save: ~45 min
- Frontend Get Comps button: subtitle + click handler swap + disabled state: ~60 min
- Frontend Quick Refresh placeholder: ~15 min
- Frontend navigation warning: ~30 min
- CSS polish (subtitle, disabled, chip): ~15-20 min
- **Total: ~3.5-4 hours Copilot work**

## Architecture compatibility check

All changes leverage already-shipped Phase 2 infrastructure:
- Cache-first read path in `_run_by_polygon` (Chunk 3) — extended with `cache_only` flag
- Deep-pull job lifecycle (`/start`, `/status`, `/stop`) — reused
- propelio_comps + comp_ratings tables (Chunk 1) — unchanged
- Parcel match writes from deep_pull (recent fix) — unchanged
- Existing `_renderPropelioComps`, `_suggestAreaNameFromContainedParcels`, `saveCurrentArea`, `_apiJson`, `authHeaders` JS helpers — all reused

No new architectural concepts.
