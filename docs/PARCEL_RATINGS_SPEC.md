---
created: 2026-05-24
status: v1 draft — pending Copilot deep-dive critique
updated: 2026-05-24
---

# Parcel (CAD) Ratings — Good / Bad / Clear

## Changelog

- **v1 (this draft):** Initial spec for adding Good/Bad/Clear ratings to CAD parcels, mirroring the existing comp ratings pattern. Includes bundled fix for the "first Good Comp checkmark slow to appear" race condition.

## Problem

Today the team can rate Propelio COMPS as Good/Bad/Clear (the `comp_ratings` table at `api/main.py:398-406`, mutation via `ratePropelioComp` at `frontend/map.js:6273-6301`, visual checkmark via `_maybeAddGoodCompMark` at `frontend/map.js:5499-5522`). The rating ships with the saved area, drives map visualization, and excludes bad-rated parcels from CSV export.

But the same affordance doesn't exist for the underlying CAD PARCELS themselves. The team currently can only rate comps, even when they want to flag a parcel as good or bad independent of any matched comp (e.g., the parcel may have no comp match at all, or the team wants to express judgment on the parcel as a target rather than as a comparable).

Plus a related bug: the first time a user clicks Good on a comp in a workspace, the checkmark takes noticeably longer to appear on the map than subsequent clicks. Diagnosis: the click handler at `map.js:6499-6524` awaits the server POST round-trip BEFORE rendering the mark; subsequent clicks feel faster because the user's attention has shifted to the next action. Mirror-feature for parcels would inherit the same lag without a fix.

## Goal

1. New `parcel_ratings` table, parallel to `comp_ratings`, keyed by `(workspace_id, county, account_num)`.
2. New backend endpoint `POST /api/parcels/rate` accepting `{saved_area_id, county, account_num, rating}` with `rating ∈ {"good", "bad", null}` (null = clear).
3. Parcel features rehydrate with `user_rating` on every server-side read (LEFT JOIN parcel_ratings on workspace_id + county + account_num).
4. New popup affordance: when a CAD parcel popup opens AND a saved area is loaded, render a "Parcel rating" mini-section with `[Good] [Bad] [Clear]` buttons mirroring the existing comp rating buttons. When the parcel ALSO has a matched comp, both sections appear stacked (Parcel rating above, Comp rating below) with explicit section headers to disambiguate.
5. Map markers:
   - `.cad-good-mark` — white circle + red ✓ + red glow (visually identical to `.propelio-good-mark`)
   - `.cad-bad-mark` — white circle + black ✗ + black border + dark shadow (new visual)
   - Both fixed 16px (no zoom scaling) and rendered at the parcel polygon's centroid
   - Both added to a new `cadRatingLayer` `L.layerGroup()`
6. Bad parcel rating EXCLUDES that parcel row from CSV export. Either-bad-wins union with the existing bad-comp exclusion at `api/main.py:4133-4135`.
7. Saved-area binding: parcel_ratings.workspace_id REFERENCES saved_areas.area_id ON DELETE CASCADE. Ratings ship with the saved area, restore on load, copy on fork.
8. Race-condition fix bundled: optimistic UI on rating clicks. The rating mark renders on the map IMMEDIATELY (synchronously, no server wait), with revert + toast on server failure. Applies to BOTH comp and parcel ratings — one fix, both paths.

NO sidebar list for parcel ratings (KK call). The visual state on the map IS the list.

## Non-goals

- Sidebar block / Good Parcels list (intentionally omitted)
- Filter chip "show only good parcels" / "hide bad parcels" toggle (deferred to V2 if requested)
- Bulk parcel rating UI (rate one at a time via popup)
- Per-user vs per-workspace rating (workspace-scoped only, like comp_ratings)
- Multi-state ratings beyond good/bad/null (no "neutral", no numeric scoring)
- Re-coloring the parcel polygon based on rating (KK explicitly said NOT to dim the polygon; just add the X marker)
- Backfill historical ratings (no source data; users start fresh)
- Rating data in propelio CSV columns (rating affects ROW INCLUSION via bad-exclusion, not new columns)

## Changes (5 files)

### 1. Database schema — `api/main.py` (new table in initial-schema block)

Add alongside the existing `comp_ratings` CREATE at `api/main.py:398-413`:

```python
cur.execute(
    """
    CREATE TABLE IF NOT EXISTS parcel_ratings (
        rating_id BIGSERIAL PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
        county TEXT NOT NULL,
        account_num TEXT NOT NULL,
        rating TEXT NOT NULL CHECK (rating IN ('good', 'bad')),
        rated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        rated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (workspace_id, county, account_num)
    )
    """
)
cur.execute(
    """
    CREATE INDEX IF NOT EXISTS idx_parcel_ratings_workspace
        ON parcel_ratings (workspace_id)
    """
)
```

Idempotent (CREATE IF NOT EXISTS). Runs at startup like the rest of the schema bootstrap.

### 2. Backend — `api/main.py` new endpoint + CSV exclusion + fork copy + hydrate join

**A. New endpoint** `POST /api/parcels/rate` (new function, similar shape to the comp/rate route at `api/propelio/routes.py:1101-1126`):

```python
class ParcelRateRequest(BaseModel):
    saved_area_id: str
    county: str
    account_num: str
    rating: str | None = None  # 'good', 'bad', or None to clear

@app.post("/api/parcels/rate")
async def rate_parcel(request: ParcelRateRequest, req: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    require_csrf(req)
    area_id = str(request.saved_area_id or "").strip()
    county = str(request.county or "").strip().lower()
    account_num = str(request.account_num or "").strip()
    rating = request.rating
    if not area_id or not county or not account_num:
        raise HTTPException(status_code=400, detail="saved_area_id, county, account_num all required")
    if rating not in (None, "good", "bad"):
        raise HTTPException(status_code=400, detail="rating must be 'good', 'bad', or null")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            if rating is None:
                cur.execute(
                    "DELETE FROM parcel_ratings WHERE workspace_id = %s AND county = %s AND account_num = %s",
                    (area_id, county, account_num),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO parcel_ratings (workspace_id, county, account_num, rating, rated_by_user_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (workspace_id, county, account_num) DO UPDATE
                    SET rating = EXCLUDED.rating,
                        rated_by_user_id = EXCLUDED.rated_by_user_id,
                        rated_at = NOW()
                    """,
                    (area_id, county, account_num, rating, int(user["id"])),
                )
        conn.commit()
    finally:
        release_session_conn(conn)
    return {"ok": True, "rating": rating}
```

**B. Hydrate `user_rating` on parcel reads.** Wherever parcel features are returned to the frontend, LEFT JOIN parcel_ratings to emit `user_rating`. Specifically the analyze/by-polygon endpoint(s) and the saved-area restore. New helper:

```python
def _load_parcel_ratings_for_workspace(workspace_id: str) -> dict[tuple[str, str], str]:
    """Return {(county_lower, account_num): rating} for the workspace, or {} if no workspace."""
    if not workspace_id:
        return {}
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT county, account_num, rating FROM parcel_ratings WHERE workspace_id = %s",
                (workspace_id,),
            )
            return {(str(c).lower(), str(a)): str(r) for c, a, r in cur.fetchall()}
    finally:
        release_session_conn(conn)
```

Then at each parcel-emitting site, attach `feature.properties.user_rating` from the lookup dict.

**C. CSV exclusion extension** at `api/main.py:4133-4135`. Today: bad-rated comps cause the matched parcel row to be excluded. New: also exclude when a parcel_ratings row marks it bad.

```python
# Existing comp-based bad-exclusion
_parcel_rating_via_comp = parcel_rating_by_key.get(row_key)
# NEW: direct parcel rating
_parcel_rating_direct = parcel_rating_direct_by_key.get(row_key)
if _parcel_rating_via_comp == "bad" or _parcel_rating_direct == "bad":
    continue
```

`parcel_rating_direct_by_key` populated from `_load_parcel_ratings_for_workspace(job_saved_area_id)` at the top of `_run_download_csv`, alongside the existing `parcel_rating_by_key` build at line 3699-3736.

**D. Fork copy** at `api/main.py:5492-5500`. After the existing comp_ratings copy block, add:

```python
cur.execute(
    """
    INSERT INTO parcel_ratings (workspace_id, county, account_num, rating, rated_by_user_id)
    SELECT %s, county, account_num, rating, rated_by_user_id
    FROM parcel_ratings
    WHERE workspace_id = %s
    """,
    (new_area_id, source_area_id),
)
```

Forked workspaces inherit the source's parcel ratings.

### 3. Frontend — popup buttons + click delegation — `frontend/map.js`

**A. New `_buildParcelRatingButtonsHtml(parcel)` helper**, parallel to `_buildRatingButtonsHtml(comp)` at `map.js:4794-4809`:

```js
function _buildParcelRatingButtonsHtml(parcel) {
  const county = String(parcel?.source_county || parcel?.county || "").trim().toLowerCase();
  const accountNum = String(parcel?.account_num || "").trim();
  const ratingsEnabled = Boolean(_currentLoadedAreaId && county && accountNum);
  const currentRating = parcel?.user_rating === "good" || parcel?.user_rating === "bad" ? parcel.user_rating : null;
  const goodActive = ratingsEnabled && currentRating === "good" ? " is-active" : "";
  const badActive = ratingsEnabled && currentRating === "bad" ? " is-active" : "";
  const countyAttr = _propelioEscape(county);
  const acctAttr = _propelioEscape(accountNum);
  const disabledAttr = ratingsEnabled ? "" : " disabled";
  const hintHtml = ratingsEnabled ? "" : `<div class="cad-rate-hint">Save area to enable ratings</div>`;
  return `
    <div class="cad-popup-rating${ratingsEnabled ? "" : " is-disabled"}" data-county="${countyAttr}" data-account-num="${acctAttr}">
      <div class="cad-rate-section-label">Parcel rating</div>
      <div class="cad-rate-buttons">
        <button type="button" class="cad-rate-btn good${goodActive}" data-rating="good" data-county="${countyAttr}" data-account-num="${acctAttr}"${disabledAttr}>Good</button>
        <button type="button" class="cad-rate-btn bad${badActive}" data-rating="bad" data-county="${countyAttr}" data-account-num="${acctAttr}"${disabledAttr}>Bad</button>
        <button type="button" class="cad-rate-btn clear" data-rating="clear" data-county="${countyAttr}" data-account-num="${acctAttr}"${disabledAttr}>Clear</button>
      </div>
      ${hintHtml}
    </div>`;
}
```

Insert it into the parcel popup HTML (built by `makePopupHtml(p)` at `map.js:7951+`) — placed at the top of the popup BEFORE the matched-comp section. When matched comp section also renders, the existing comp rating row gets a matching `Comp rating` section label for visual symmetry.

**B. New mutation function** `rateParcel(county, accountNum, rating)`, parallel to `ratePropelioComp` at `map.js:6273-6301`:

```js
async function rateParcel(county, accountNum, rating) {
  const areaId = (typeof _currentLoadedAreaId === "string" ? _currentLoadedAreaId : "") || "";
  if (!areaId || !county || !accountNum) return false;
  const body = {
    saved_area_id: areaId,
    county,
    account_num: accountNum,
    rating: rating === "good" || rating === "bad" ? rating : null,
  };
  try {
    const resp = await fetch("/api/parcels/rate", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      console.warn("[cad] rate parcel failed:", resp.status);
      return false;
    }
    // Sync in-memory parcel cache so re-renders reflect the rating
    _updateParcelUserRatingInCache(county, accountNum, body.rating);
    return true;
  } catch (err) {
    console.error("[cad] rate parcel error:", err);
    return false;
  }
}

function _updateParcelUserRatingInCache(county, accountNum, rating) {
  // Walk allAnalysisFeatures and update the matching feature's properties.user_rating.
  // The next render pass picks it up via _maybeAddParcelRatingMark.
  if (!Array.isArray(allAnalysisFeatures)) return;
  const c = String(county || "").toLowerCase();
  const a = String(accountNum || "").trim();
  for (const f of allAnalysisFeatures) {
    const fp = f?.properties || {};
    if (String(fp.source_county || "").toLowerCase() === c && String(fp.account_num || "").trim() === a) {
      fp.user_rating = rating;
      break;
    }
  }
}
```

**C. Document-level click delegation** for `.cad-rate-btn`, parallel to the existing `.propelio-rate-btn` handler at `map.js:6499-6524`:

```js
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".cad-rate-btn");
  if (!btn) return;
  const county = btn.getAttribute("data-county");
  const accountNum = btn.getAttribute("data-account-num");
  const rating = btn.getAttribute("data-rating");
  if (!county || !accountNum) return;
  // Optimistic popup button styling (mirrors comp pattern)
  const container = btn.parentElement;
  if (container) {
    container.querySelectorAll(".cad-rate-btn").forEach((b) => {
      if (rating === "good") b.classList.toggle("is-active", b.classList.contains("good"));
      else if (rating === "bad") b.classList.toggle("is-active", b.classList.contains("bad"));
      else b.classList.remove("is-active");
    });
  }
  // OPTIMISTIC map mark (race-condition fix — see §4 in spec)
  const newRating = rating === "clear" ? null : rating;
  _setParcelRatingMarkOptimistic(county, accountNum, newRating);
  void rateParcel(county, accountNum, newRating).then(ok => {
    if (!ok) {
      // Revert: re-read the previous rating from cache (already restored by _setParcelRatingMarkOptimistic on failure)
      _setParcelRatingMarkOptimistic(county, accountNum, _getCachedParcelRating(county, accountNum));
      _showToast("Rating update failed — reverted", "error");
    }
  });
});
```

### 4. Map rendering — markers + race-condition fix — `frontend/map.js` + `frontend/style.css`

**A. New `cadRatingLayer`** initialized at module load alongside `propelioCompLayer` (around `map.js:554`):
```js
const cadRatingLayer = L.layerGroup().addTo(map);
const cadRatingLayerByKey = new Map();  // "county:account_num" → leaflet marker
```

**B. New `_maybeAddParcelRatingMark(parcel, footprint, fallbackLatLng)`** parallel to `_maybeAddGoodCompMark` at `map.js:5499-5522`. Routes by `parcel.user_rating`:
- `"good"` → red ✓ divIcon, class `.cad-good-mark`
- `"bad"` → black ✗ divIcon, class `.cad-bad-mark`
- anything else → no mark

Adds to `cadRatingLayer` and registers in `cadRatingLayerByKey` for later updates.

**C. New `_setParcelRatingMarkOptimistic(county, accountNum, rating)`** — the race-condition fix:
- Looks up existing marker in `cadRatingLayerByKey`
- If present, removes it
- If `rating ∈ {"good", "bad"}`, creates new marker + adds to layer + registers in map
- If `rating === null`, leaves the slot empty
- SYNCHRONOUS — no server wait. Called from click handler BEFORE the async POST.

**D. Same optimistic mechanism for COMP rating** (bundled per KK). New `_setCompRatingMarkOptimistic(compKey, rating)` parallel to the parcel version, called from the existing comp click handler at `map.js:6499-6524` BEFORE `ratePropelioComp`. Removes the current 150ms+ debounce delay perceived as "first click slow."

**E. Render integration.** Wherever parcels render (e.g., in the main analyze-render pass that draws parcel polygons), after rendering a polygon call `_maybeAddParcelRatingMark(parcel, polygon, fallbackLatLng)`. Also call on saved-area restore.

**F. CSS** — add to `frontend/style.css` near the existing `.propelio-good-mark` at line ~3497:
```css
.cad-good-mark-wrap, .cad-bad-mark-wrap {
  background: transparent;
  border: 0;
  pointer-events: none;
}
.cad-good-mark {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #ffffff;
  color: #dc2626;
  font-size: 12px;
  font-weight: 800;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 0 0 2px #dc2626, 0 0 6px rgba(220, 38, 38, 0.7);
}
.cad-bad-mark {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #ffffff;
  color: #111111;
  font-size: 12px;
  font-weight: 800;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 0 0 2px #111111, 0 0 6px rgba(0, 0, 0, 0.7);
}
.cad-rate-section-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-soft);
  margin-bottom: 4px;
}
.cad-popup-rating {
  margin: 6px 0;
}
.cad-rate-buttons {
  display: flex;
  gap: 4px;
}
.cad-rate-btn {
  /* mirrors .propelio-rate-btn styling */
  padding: 4px 8px;
  font-size: 11px;
  font-weight: 600;
  border-radius: 3px;
  cursor: pointer;
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: transparent;
  color: var(--text-soft);
}
.cad-rate-btn.good.is-active { background: rgba(220, 38, 38, 0.15); color: #ff6b6b; border-color: #dc2626; }
.cad-rate-btn.bad.is-active { background: rgba(17, 17, 17, 0.35); color: #f5e9c8; border-color: #111111; }
.cad-rate-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.cad-rate-hint { font-size: 10px; color: var(--text-soft); font-style: italic; margin-top: 2px; }
```

Also: mirror the same `.propelio-rate-section-label` class for the COMP rating section (visual symmetry in popups that show both).

### 5. No changes (intentional)

- No new sidebar block (per KK)
- No filter chip integration (per non-goals)
- No new CSV columns (rating affects row inclusion only)

## Sequencing

1. Schema first (idempotent CREATE) — safe to deploy without any code changes
2. Backend endpoint + hydrate join + CSV exclusion + fork copy — all backend, can ship in one commit
3. Frontend popup HTML + click delegation + map markers + optimistic UI — all frontend, ships together

Three commits within one PR is reasonable.

## Verification plan

### Visual / behavior

1. Open a CAD parcel popup with a saved area loaded → "Parcel rating" section visible with [Good] [Bad] [Clear] buttons.
2. Open the same parcel popup with no saved area loaded → buttons disabled, hint shows "Save area to enable ratings".
3. Click Good on a parcel → red ✓ appears INSTANTLY at parcel centroid (no perceptible lag). Popup button highlights as active.
4. Refresh the page, reload the saved area → red ✓ still visible on the parcel.
5. Click Bad on a different parcel → black ✗ appears INSTANTLY at parcel centroid. Polygon NOT dimmed (per spec — only the X marker).
6. Click Clear on a rated parcel → marker disappears INSTANTLY.
7. Open a parcel popup with a matched comp → BOTH "Parcel rating" and "Comp rating" sections visible, each with their own [Good] [Bad] [Clear] row. Rating one does NOT affect the other.

### Race-condition (bundled fix)

8. Fresh page load, fresh saved area, click Good on a comp → checkmark appears INSTANTLY (not after 200-500ms lag).
9. Same for parcel Good — instant.
10. Disconnect network in DevTools → click Good on a parcel → mark appears optimistically, then reverts within a second + toast: "Rating update failed — reverted".

### CSV exclusion

11. Rate a parcel Bad → download CSV → that parcel's row is NOT present.
12. Rate a parcel Good → CSV row IS present.
13. Parcel rated Good but has a matched bad-rated comp → row NOT present (either-bad-wins).
14. Parcel rated Bad but no comp link → row NOT present.

### Saved-area lifecycle

15. Save area → rate some parcels → close + reopen → ratings restore correctly.
16. Fork the saved area → forked area shows the same parcel ratings (per fork-copy block).
17. Delete the saved area → all parcel_ratings rows for that workspace are deleted (CASCADE).

### Multi-county

18. Test parcel ratings in all 4 counties (DCAD, TAD, Collin, Denton) — confirm county is properly normalized in the FK key.

### Cleanup gates (merge-gate)

19. `git grep _showBanner` returns zero results (must use `_showToast`).
20. No unintentional changes to `comp_ratings` schema or endpoint behavior.
21. No new sidebar elements added.

## Risk

| Risk | Severity | Mitigation |
|---|---|---|
| Optimistic UI mark gets out of sync if server denies the rating | Medium | On failure: revert the mark by reading the pre-click rating from cache + showing toast |
| Multiple polygon-shapes for same parcel (duplicate rows) → multiple markers stack | Low | `cadRatingLayerByKey` keyed on `${county}:${account_num}` ensures one marker per parcel even if rendered multiple times |
| Parcel centroid calculation fails on degenerate polygons → marker appears at wrong location | Low | Reuse existing centroid logic from `_maybeAddGoodCompMark`'s footprint.getBounds().getCenter() fallback chain |
| Fork copy adds parcel_ratings rows BEFORE the new workspace_id row exists → FK violation | Medium | Sequence: INSERT saved_area → capture new_area_id → INSERT parcel_ratings in same transaction (same pattern as comp_ratings fork copy at line 5492) |
| CSV exclusion double-counts bad rows (a row excluded once by comp-bad, then again by parcel-bad) | Low | `continue` exits the row loop early; either-bad-wins is naturally idempotent |
| Optimistic mark survives a failed POST if revert path errors | Medium | Wrap revert in its own try/catch; toast even if revert fails silently |
| New cadRatingLayer obscures click-targets if z-index isn't right | Medium | Match z-index pattern of propelioCompLayer; markers are non-interactive (`interactive: false, keyboard: false`) |
| Bad-X marker overlaps a tagged Good ✓ marker on the same parcel center (mid-toggle) | Low | Optimistic update REMOVES previous mark before adding new one; visual gap is sub-frame |
| Existing `_buildRatingButtonsHtml` for comps doesn't have a section label — adding "Comp rating" header to it MIGHT shift unrelated layouts | Low | Cautious change: add the label inside `.propelio-popup-rating` wrapper; doesn't affect existing CSS dimensions |
| Hydrating user_rating on every parcel read adds a DB roundtrip per request | Low | One small lookup table per workspace per request; cheap with the indexed `idx_parcel_ratings_workspace` |

## Rollback

Single-PR revert. The schema migration (`CREATE TABLE IF NOT EXISTS`) is idempotent and safe to leave in place; rollback only reverts the code paths. Data in parcel_ratings is orphaned but inert until the feature reships.

## Out of scope

- Sidebar list / "Good Parcels" block
- Filter chips for parcel rating
- Bulk rating UI / mass-flip
- Per-user vs per-workspace rating
- CSV columns for parcel rating value
- Polygon recoloring based on rating
- Animations on mark add/remove
- Notification when a rating changes elsewhere
- Sound effects (just in case)

## Open items for Copilot deep-dive critique

1. **`_load_parcel_ratings_for_workspace` placement.** Does it belong in `api/main.py` next to the existing `_job_share_id` helpers, or in a new module? What's the cleanest hydrate-on-parcel-read pattern?

2. **All parcel-emitting endpoints.** Audit: where do parcel features get serialized? `/api/analyze`, `/api/areas/{area_id}` GET, `/api/area/by-share-id/{share_id}`, polygon analyze endpoint, more? Each needs the `user_rating` hydrate. List the full set with file:line.

3. **The "all parcels" cache on frontend.** `_updateParcelUserRatingInCache` assumes `allAnalysisFeatures` is the canonical client-side parcel cache. Confirm name + structure. Also confirm whether the cache survives re-render or gets rebuilt.

4. **`makePopupHtml(p)` mutation site.** Where exactly to insert `_buildParcelRatingButtonsHtml(parcel)`? Before the matched-comp section, after? Should it be in a different visual position when the popup is for a parcel-only vs parcel-with-comp scenario?

5. **`_maybeAddParcelRatingMark` invocation sites.** Where is the parcel-polygon render loop that should call this? Per-feature on geoJSON layer creation? Or a separate post-render pass?

6. **Comp rating optimistic fix collision.** If we apply optimistic UI to comp rating clicks too, does the existing `applyPropelioClientFilters` post-server full re-render conflict (e.g., briefly removes the optimistic mark and re-adds it)? Need to confirm idempotent re-add — `cadRatingLayerByKey` / `propelioCompLayerByKey` handle this but verify.

7. **Edge case: race between optimistic mark and server-confirm full re-render.** User clicks Good → optimistic mark added → server responds 200 → applyPropelioClientFilters re-renders → during the clearLayers(), the optimistic mark is wiped → re-added when render reaches the rated comp. Brief visual flicker possible. Acceptable? Or should optimistic marks register in a side-layer that survives the comp layer's clearLayers()?

8. **Section labels for parcel-only popups.** When the popup is for a parcel WITHOUT a matched comp, the "Parcel rating" label might feel redundant (the only rating block). Better: show the label only when both sections coexist, otherwise hide it?

9. **`_buildParcelRatingButtonsHtml` parcel field name.** Is `source_county` or `county` the canonical name on the parcel object? Spec hedges with `parcel?.source_county || parcel?.county`. Confirm via the parcel data shape.

10. **County normalization.** Spec says `.toLowerCase()` everywhere county is keyed. Confirm all the parcel-feature emit paths normalize the same way.

11. **CSV `parcel_rating_direct_by_key` build.** Spec proposes building it at `_run_download_csv` top alongside the existing `parcel_rating_by_key`. Confirm there's a clean insertion point and the dict's lookup pattern at line 4133 will work without restructuring the loop.

12. **Schema migration ordering.** New table references `saved_areas(area_id)` and `users(id)`. Confirm both tables exist at the bootstrap point where the new CREATE will live.

13. **Bonus race-condition fix scope.** Spec applies optimistic UI to both parcel AND comp ratings. Does the existing comp rating fix introduce regressions in the Good Comps sidebar section (just shipped) that subscribes to `applyPropelioClientFilters`? It should still work since the section re-renders from cache + the cache updates synchronously, but verify.

14. **Anything else** — anti-patterns, missed scope, file:line cleanup we should bundle. Specifically: if the existing comp click handler at `map.js:6499-6524` has any subtleties (e.g., already-active button toggle behavior), make sure the parcel handler mirrors them faithfully.

## Implementation effort estimate

- Schema migration: 0.25 day
- Backend endpoint + hydrate + CSV exclusion + fork copy: 1 day
- Frontend popup HTML + click delegation + cadRatingLayer + markers: 0.75 day
- Race-condition optimistic fix (parcel + comp): 0.5 day
- Verification + preview iterate: 0.5 day
- **Total: ~3 days**

## Status

**v1 draft.** Pending Copilot deep-dive critique. After Copilot's response and KK product calls on any open items → v2 lock → implementation.
