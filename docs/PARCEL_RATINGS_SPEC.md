---
created: 2026-05-24
status: v2 — Copilot deep-dive + KK product calls incorporated; LOCKED, ready for implementation
updated: 2026-05-24
---

# Parcel (CAD) Ratings — Good / Bad / Clear

## Changelog

- **v1 (initial draft):** Parcel ratings table mirroring comp_ratings + popup buttons + map markers + bundled race-condition fix. Either-bad-wins for CSV exclusion. Public endpoint user_rating hydrate assumed safe. No ownership enforcement on rate endpoints.

- **v2 (THIS, post-Copilot deep-dive + KK product calls):**
  - **CSV exclusion semantics changed: comp rating ALWAYS wins.** Per KK product call. If a matched comp has a rating (good or bad), that determines inclusion regardless of direct parcel rating. Direct parcel rating only applies when no comp rating exists. NOT either-bad-wins.
  - **No `user_rating` hydrate on public/unauth parcel endpoints.** Per KK product call. `/api/parcel/{county}/{account_num}` (main.py:2911) and `/api/parcel/near` (main.py:2861) stay as-is. The rating data only flows through authenticated, workspace-scoped read paths. Popup buttons gracefully degrade to "Save area to enable ratings" hint when no `_currentLoadedAreaId` (mirrors existing comp pattern).
  - **Ownership check on BOTH rate endpoints.** Per KK product call. Closes the pre-existing weakness in `/api/propelio/comp/rate` (archive.py:189-267 currently writes any workspace_id without auth check) AND secures the new `/api/parcels/rate`. Both endpoints verify `saved_areas.user_id = current_user.id` before mutating, return 403 otherwise.
  - **Per-key mutation versioning for optimistic rollback.** Per Copilot Critical-3. Naive `oldRating` capture loses to rapid repeat clicks. New `_ratingMutationSeq` Map keyed by `${kind}:${key}` (kind ∈ {comp, parcel}) increments on each click; rollback only applies if the in-flight seq matches the current seq.
  - **Reuse the existing comp optimistic helper at map.js:5249.** Per Copilot anti-pattern note #14. There's already a `_setCompRatingMarkOptimistic`-like helper at map.js:5249 that's currently unused — the popup click path at map.js:5487 still calls ratePropelioComp directly. v2 adopts the helper for both comp + parcel paths; resolves the split logic.
  - **`cadRatingLayer` lifecycle wiring.** Per Copilot Medium-5. Must call `cadRatingLayer.clearLayers()` + `cadRatingLayerByKey.clear()` at every parcel-redraw boundary (map.js:7179-7186) and draw-clear path (map.js:8234). Otherwise stale marks survive across workspace/filter transitions.
  - **Single workspace rating-map prefetch** (not per-row joins). Per Copilot Recommendation-2. New `_load_parcel_ratings_for_workspace(workspace_id) -> dict[(county, account_num), rating]` runs ONCE per analyze/CSV request, results applied in-memory to each feature.
  - **Section labels conditional.** Per Copilot Open-item-7 + Recommendation. Show "Parcel rating" label only when matched comp section also renders (dual-section popup). Hide it in parcel-only popups for visual balance.
  - **Multipolygon centroid fallback chain.** Per Copilot Low-6. Use `featureCentroidLngLat` at map.js:7637 (existing helper) as the second-tier fallback before properties.lat/lng.
  - **Updated all file:line refs to current code.** v1's references were stale relative to current develop. Copilot's audit corrected them: `makePopupHtml` is at 6751 (not 7951+), popup click delegation at 5463 (not 6499), `ratePropelioComp` at 5487 (not 6273), comp click handler debounce theory was wrong — there is NO debounce on the click path (debounce only on filter inputs at 5274/5408).

## Problem

Today the team can rate Propelio COMPS as Good/Bad/Clear (the `comp_ratings` table at `api/main.py:398-406`, mutation via `ratePropelioComp` at `frontend/map.js:5487`, visual checkmark via `_maybeAddGoodCompMark` at `frontend/map.js:4601`). The rating ships with the saved area, drives map visualization, and excludes bad-rated parcels from CSV export.

But the same affordance doesn't exist for the underlying CAD PARCELS themselves. The team currently can only rate comps, even when they want to flag a parcel as good or bad independent of any matched comp (e.g., the parcel may have no comp match at all, or the team wants to express judgment on the parcel as a target rather than as a comparable).

Plus a related bug: the first time a user clicks Good on a comp in a workspace, the checkmark takes noticeably longer to appear on the map than subsequent clicks. Diagnosis (v2 corrected): the click handler at `map.js:5463-5487` awaits the server POST round-trip BEFORE rendering the mark via the full `applyPropelioClientFilters` re-render. There is NO debounce on the click path (only filter inputs at 5274/5408 are debounced). Server round-trip + full layer clear+rebuild is the actual lag source. Subsequent clicks feel faster because attention has shifted. Mirror-feature for parcels would inherit the same lag without a fix.

## Goal

1. New `parcel_ratings` table, parallel to `comp_ratings`, keyed by `(workspace_id, county, account_num)`.
2. New backend endpoint `POST /api/parcels/rate` accepting `{saved_area_id, county, account_num, rating}` with `rating ∈ {"good", "bad", null}` (null = clear). Endpoint enforces saved_area ownership.
3. Parcel features hydrate with `user_rating` on AUTHENTICATED, workspace-scoped server-side read paths ONLY. Public/unauth endpoints do not carry rating data.
4. Existing `/api/propelio/comp/rate` ALSO gets ownership enforcement (fixes pre-existing weakness).
5. New popup affordance: when a CAD parcel popup opens AND a saved area is loaded, render a "Parcel rating" mini-section with `[Good] [Bad] [Clear]` buttons mirroring the existing comp rating buttons. When the parcel ALSO has a matched comp, both sections appear stacked (Parcel rating above, Comp rating below) with section header labels for disambiguation. Hide the "Parcel rating" label when only the parcel rating section renders (no matched comp).
6. Map markers:
   - `.cad-good-mark` — white circle + red ✓ + red glow (visually identical to `.propelio-good-mark`)
   - `.cad-bad-mark` — white circle + black ✗ + black border + dark shadow
   - Both fixed 16px (no zoom scaling) and rendered at the parcel polygon's centroid
   - Both added to a new `cadRatingLayer` `L.layerGroup()`
7. **CSV exclusion: comp rating ALWAYS wins.** When a matched comp has a rating, that determines inclusion regardless of direct parcel rating. Direct parcel rating only matters when no comp rating exists.
8. Saved-area binding: `parcel_ratings.workspace_id REFERENCES saved_areas.area_id ON DELETE CASCADE`. Ratings ship with the saved area, restore on load, copy on fork.
9. Race-condition fix bundled: optimistic UI on rating clicks via the existing helper at map.js:5249 (extended for parcels). The rating mark renders on the map IMMEDIATELY (synchronously, no server wait), with per-key mutation versioning and revert + toast on server failure. Applies to BOTH comp and parcel ratings.
10. **No sidebar list for parcel ratings.** The visual state on the map IS the list.

## Non-goals

- Sidebar block / Good Parcels list (intentionally omitted)
- Filter chip "show only good parcels" / "hide bad parcels" toggle (deferred to V2 if requested)
- Bulk parcel rating UI (rate one at a time via popup)
- Per-user vs per-workspace rating (workspace-scoped only, like comp_ratings)
- Multi-state ratings beyond good/bad/null (no "neutral", no numeric scoring)
- Re-coloring the parcel polygon based on rating (KK explicitly said NOT to dim the polygon; just add the X marker)
- Backfill historical ratings (no source data; users start fresh)
- Rating data in propelio CSV columns (rating affects ROW INCLUSION via comp-rating-wins, not new columns)
- Public-endpoint rating hydrate (per KK call — security boundary)
- Shared workspace WRITE access for non-owners (use fork-then-rate instead)

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

Idempotent. `saved_areas` (line 235) and `users` (line 201) exist before this point — FK-safe.

### 2. Backend — `api/main.py` new endpoint + hydrate + CSV exclusion + fork copy + comp endpoint hardening

**A. Ownership-check helper** (new, reusable by both rate endpoints):

```python
def _assert_user_owns_area(area_id: str, user_id: int) -> None:
    """Raise 403 if the calling user doesn't own the saved area. Used by
    both /api/parcels/rate and /api/propelio/comp/rate to prevent
    cross-workspace rating mutation by anyone who knows an area_id."""
    if not area_id or not user_id:
        raise HTTPException(status_code=400, detail="Missing area_id or user")
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM saved_areas WHERE area_id = %s AND user_id = %s LIMIT 1",
                (area_id, int(user_id)),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=403, detail="You don't own this workspace")
    finally:
        release_session_conn(conn)
```

**B. New endpoint** `POST /api/parcels/rate`:

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
    _assert_user_owns_area(area_id, int(user["id"]))

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

**C. Existing comp rate endpoint hardening** at `api/propelio/routes.py:1101-1126`:

Add `_assert_user_owns_area(saved_area_id, int(user["id"]))` call right after the input validation, before `set_comp_rating()`. Closes the existing weakness where any logged-in user could mutate any workspace's ratings by knowing the area_id.

**D. Workspace rating-map prefetch helper** (single SQL query, in-memory join):

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

**E. Hydrate `user_rating` on AUTHENTICATED parcel emit paths only** (per KK product call — public endpoints intentionally not hydrated):

Audit-confirmed sites needing hydrate:
- `api/main.py:2964` — main analyze response (draw + saved-area payload)
- `api/main.py:987` — session feature rebuild on restore
- `api/main.py:4709` — session cached payload rebuild

At each site, before serializing features:
1. Compute `workspace_id` from request context (saved_area_id from request body / current_loaded area)
2. Call `parcel_ratings_map = _load_parcel_ratings_for_workspace(workspace_id)` ONCE
3. In the feature-build loop, attach `feature.properties["user_rating"] = parcel_ratings_map.get((county_lower, account_num))`

**Sites that DO NOT get hydrate** (per KK product call):
- `api/main.py:2911` — `/api/parcel/{county}/{account_num}` (public, no auth)
- `api/main.py:2861` — `/api/parcel/near` (public, no auth)

These remain unchanged. Popup rendering on the frontend already gates rating buttons via `_currentLoadedAreaId`, so the absence of user_rating in these paths is invisible to the user.

**F. CSV exclusion logic — comp-wins-absolute** at `api/main.py:4133-4135`:

Compute the parcel direct ratings prefetch alongside the existing comp-derived map at line 3686-3734:

```python
parcel_rating_direct_by_key = _load_parcel_ratings_for_workspace(job_saved_area_id) if job_saved_area_id else {}
```

Modified exclusion gate at line 4133-4135:

```python
_parcel_rating_via_comp = parcel_rating_by_key.get(row_key)  # existing (comp-derived)
_parcel_rating_direct = parcel_rating_direct_by_key.get(row_key)  # new (direct parcel rating)

# Comp rating WINS when present (KK product call v2). Direct parcel
# rating only applies when no comp rating exists for that parcel.
if _parcel_rating_via_comp == "bad":
    continue  # comp says bad → row excluded regardless of direct parcel rating
if _parcel_rating_via_comp == "good":
    pass  # comp says good → row included regardless of direct parcel rating
elif _parcel_rating_direct == "bad":
    continue  # no comp rating, direct parcel rating is bad → excluded
# else: included (no signal, or direct=good with no comp)
```

**G. Fork copy** at `api/main.py:5492-5500` (extend the existing comp_ratings fork block). Add parallel INSERT…SELECT for parcel_ratings using the same `new_area_id` / `source_area_id` already in scope:

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

Forked workspaces inherit the source's parcel ratings (parallel to comp_ratings inheritance).

### 3. Frontend — popup buttons + click delegation — `frontend/map.js`

**A. New `_buildParcelRatingButtonsHtml(parcel, hasMatchedComp)` helper**, parallel to `_buildRatingButtonsHtml(comp)` at `map.js:3948`. The `hasMatchedComp` flag controls whether the section label renders:

```js
function _buildParcelRatingButtonsHtml(parcel, hasMatchedComp) {
  const county = String(parcel?.source_county || "").trim().toLowerCase();
  const accountNum = String(parcel?.account_num || "").trim();
  const ratingsEnabled = Boolean(_currentLoadedAreaId && county && accountNum);
  const currentRating = parcel?.user_rating === "good" || parcel?.user_rating === "bad" ? parcel.user_rating : null;
  const goodActive = ratingsEnabled && currentRating === "good" ? " is-active" : "";
  const badActive = ratingsEnabled && currentRating === "bad" ? " is-active" : "";
  const countyAttr = _propelioEscape(county);
  const acctAttr = _propelioEscape(accountNum);
  const disabledAttr = ratingsEnabled ? "" : " disabled";
  const hintHtml = ratingsEnabled ? "" : `<div class="cad-rate-hint">Save area to enable ratings</div>`;
  // Per spec v2: section label only renders when dual-section popup (matched comp also present).
  const labelHtml = hasMatchedComp ? `<div class="cad-rate-section-label">Parcel rating</div>` : "";
  return `
    <div class="cad-popup-rating${ratingsEnabled ? "" : " is-disabled"}" data-county="${countyAttr}" data-account-num="${acctAttr}">
      ${labelHtml}
      <div class="cad-rate-buttons">
        <button type="button" class="cad-rate-btn good${goodActive}" data-rating="good" data-county="${countyAttr}" data-account-num="${acctAttr}"${disabledAttr}>Good</button>
        <button type="button" class="cad-rate-btn bad${badActive}" data-rating="bad" data-county="${countyAttr}" data-account-num="${acctAttr}"${disabledAttr}>Bad</button>
        <button type="button" class="cad-rate-btn clear" data-rating="clear" data-county="${countyAttr}" data-account-num="${acctAttr}"${disabledAttr}>Clear</button>
      </div>
      ${hintHtml}
    </div>`;
}
```

Inserted into the parcel popup HTML built by `makePopupHtml(p)` at `map.js:6751`, AFTER the CAD parcel data table and BEFORE the matched-comp section/actions block. When the matched-comp section renders below it, also wrap the existing comp rating with a matching "Comp rating" label (new — adds visual symmetry).

**B. New mutation function** `rateParcel(county, accountNum, rating)`, parallel to `ratePropelioComp` at `map.js:5487`:

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
    _updateParcelUserRatingInCache(county, accountNum, body.rating);
    return true;
  } catch (err) {
    console.error("[cad] rate parcel error:", err);
    return false;
  }
}

function _updateParcelUserRatingInCache(county, accountNum, rating) {
  // Walk allAnalysisFeatures (map.js:573 — confirmed canonical) and
  // update the matching feature's properties.user_rating. The next
  // render pass picks it up via _maybeAddParcelRatingMark.
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

**C. Per-key mutation versioning + document-level click delegation** parallel to the existing `.propelio-rate-btn` handler at `map.js:5463-5487`. Address the rapid-repeat-click race per Copilot Critical-3:

```js
// Module-level counter — increments on every rating click to make
// stale rollbacks idempotent.
const _ratingMutationSeq = new Map();  // key: `${kind}:${id}` → integer

function _bumpMutationSeq(kind, id) {
  const key = `${kind}:${id}`;
  const next = (_ratingMutationSeq.get(key) || 0) + 1;
  _ratingMutationSeq.set(key, next);
  return next;
}

function _isLatestMutation(kind, id, capturedSeq) {
  const key = `${kind}:${id}`;
  return _ratingMutationSeq.get(key) === capturedSeq;
}

document.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".cad-rate-btn");
  if (!btn) return;
  const county = btn.getAttribute("data-county");
  const accountNum = btn.getAttribute("data-account-num");
  const rating = btn.getAttribute("data-rating");
  if (!county || !accountNum) return;
  const newRating = rating === "clear" ? null : rating;
  const mutKey = `${county}:${accountNum}`;
  const seq = _bumpMutationSeq("parcel", mutKey);
  const previousRating = _getCachedParcelRating(county, accountNum);

  // Optimistic popup button styling
  const container = btn.parentElement;
  if (container) {
    container.querySelectorAll(".cad-rate-btn").forEach((b) => {
      if (rating === "good") b.classList.toggle("is-active", b.classList.contains("good"));
      else if (rating === "bad") b.classList.toggle("is-active", b.classList.contains("bad"));
      else b.classList.remove("is-active");
    });
  }

  // Optimistic map mark (synchronous — fixes the "first checkmark slow" lag)
  _setParcelRatingMarkOptimistic(county, accountNum, newRating);

  void rateParcel(county, accountNum, newRating).then(ok => {
    if (!ok && _isLatestMutation("parcel", mutKey, seq)) {
      // Revert only if this is still the latest mutation for this key.
      // Rapid repeat-click would have bumped seq again; the LATER click's
      // state is the user's actual intent, not this stale revert.
      _setParcelRatingMarkOptimistic(county, accountNum, previousRating);
      _showToast("Rating update failed — reverted", "error");
    }
  });
});
```

**D. Extend the existing comp optimistic helper at `map.js:5249`** (currently unused) for parcels. The helper signature becomes `_setRatingMarkOptimistic(layer, layerByKey, kind, key, rating, anchorFn)` and is called from both comp and parcel click paths:

```js
// Pre-existing at map.js:5249 (currently unused). v2 adopts it.
function _setRatingMarkOptimistic(layer, layerByKey, kind, key, rating, anchorFn) {
  // Remove existing mark for this key
  const existing = layerByKey.get(key);
  if (existing) {
    layer.removeLayer(existing);
    layerByKey.delete(key);
  }
  if (rating !== "good" && rating !== "bad") return;
  const target = anchorFn();
  if (!target) return;
  const className = kind === "comp" ? "propelio-good-mark-wrap" :
                    (rating === "good" ? "cad-good-mark-wrap" : "cad-bad-mark-wrap");
  const innerHtml = kind === "comp" ? `<div class="propelio-good-mark">&#10003;</div>` :
                    (rating === "good" ? `<div class="cad-good-mark">&#10003;</div>` :
                                          `<div class="cad-bad-mark">&#10007;</div>`);
  const icon = L.divIcon({ className, html: innerHtml, iconSize: [16, 16], iconAnchor: [8, 8] });
  const marker = L.marker(target, { icon, interactive: false, keyboard: false });
  marker.addTo(layer);
  layerByKey.set(key, marker);
}
```

Thin wrappers:
- `_setCompRatingMarkOptimistic(compKey, rating)` → calls `_setRatingMarkOptimistic(propelioCompLayer, propelioCompLayerByKey, "comp", compKey, rating, () => _resolveCompAnchor(compKey))`
- `_setParcelRatingMarkOptimistic(county, accountNum, rating)` → calls with cadRatingLayer + cadRatingLayerByKey + `${county}:${accountNum}` key

### 4. Map rendering — markers + race-condition fix — `frontend/map.js` + `frontend/style.css`

**A. New layer + key map** initialized at module load alongside `propelioCompLayer`:
```js
const cadRatingLayer = L.layerGroup().addTo(map);
const cadRatingLayerByKey = new Map();  // "county:account_num" → leaflet marker
```

**B. New `_maybeAddParcelRatingMark(parcel, footprint, fallbackLatLng)`** parallel to `_maybeAddGoodCompMark` at `map.js:4601`. Centroid resolution chain (per Copilot Low-6):
1. `footprint.getBounds().getCenter()` (most accurate for polygons)
2. `featureCentroidLngLat(parcel)` (existing helper at map.js:7637 — handles multipolygons cleanly)
3. `fallbackLatLng` (last resort)

Adds to `cadRatingLayer` via the shared `_setRatingMarkOptimistic` helper from §3D.

**C. Render integration** at `map.js:7175` (per Copilot Open-item-5 — confirmed this is the per-feature parcel render slot, where verification badges and polygons get created). After polygon creation, call `_maybeAddParcelRatingMark(parcel, polygon, fallbackLatLng)`. Marks survive subsequent redraws because the layer is independent.

**D. cadRatingLayer lifecycle wiring** (per Copilot Medium-5 — critical, otherwise stale marks survive transitions). Clear the layer + key map at:
- Parcel redraw boundary at `map.js:7179-7186` (currently clears parcel layers + badges)
- Draw-clear path at `map.js:8234`
- Workspace switch (wherever `_currentLoadedAreaId` changes — including the fork flow that bypasses standard restore)

**E. CSS** — add to `frontend/style.css` near the existing `.propelio-good-mark` at line ~3497:

```css
.cad-good-mark-wrap, .cad-bad-mark-wrap {
  background: transparent;
  border: 0;
  pointer-events: none;
}
.cad-good-mark {
  width: 16px; height: 16px; border-radius: 50%;
  background: #ffffff; color: #dc2626;
  font-size: 12px; font-weight: 800; line-height: 1;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 0 0 2px #dc2626, 0 0 6px rgba(220, 38, 38, 0.7);
}
.cad-bad-mark {
  width: 16px; height: 16px; border-radius: 50%;
  background: #ffffff; color: #111111;
  font-size: 12px; font-weight: 800; line-height: 1;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 0 0 2px #111111, 0 0 6px rgba(0, 0, 0, 0.7);
}
.cad-rate-section-label, .propelio-rate-section-label {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--text-soft); margin-bottom: 4px;
}
.cad-popup-rating { margin: 6px 0; }
.cad-rate-buttons { display: flex; gap: 4px; }
.cad-rate-btn {
  padding: 4px 8px; font-size: 11px; font-weight: 600;
  border-radius: 3px; cursor: pointer;
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: transparent; color: var(--text-soft);
}
.cad-rate-btn.good.is-active { background: rgba(220, 38, 38, 0.15); color: #ff6b6b; border-color: #dc2626; }
.cad-rate-btn.bad.is-active { background: rgba(17, 17, 17, 0.35); color: #f5e9c8; border-color: #111111; }
.cad-rate-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.cad-rate-hint { font-size: 10px; color: var(--text-soft); font-style: italic; margin-top: 2px; }
```

### 5. Comp click path update — `frontend/map.js`

Update the existing popup click delegation at `map.js:5463-5487` to ALSO use:
- `_setCompRatingMarkOptimistic` BEFORE the async `ratePropelioComp` call
- Per-key mutation versioning (kind = "comp", key = `compKey`)
- Same revert-only-if-still-latest pattern as parcels

Verification: Good Comps sidebar block (just-shipped, commit `cc7ee14`) subscribes to `applyPropelioClientFilters` re-render. Since the optimistic helper mutates the cache + the subsequent successful `ratePropelioComp` still triggers `applyPropelioClientFilters`, the Good Comps section continues to reflect changes correctly. No regression.

## Sequencing

1. Schema (idempotent CREATE) — safe to deploy alone
2. Backend: ownership helper + rate endpoint + hardening of existing comp rate + hydrate + CSV exclusion + fork copy — backend commit
3. Frontend: popup HTML + rate function + click delegation + optimistic helper extension + cadRatingLayer + lifecycle wiring + CSS — frontend commit
4. Comp click path update (use optimistic helper + versioning) — bundled in the frontend commit

Three commits in one PR is reasonable.

## Verification plan

### Visual / behavior

1. Open CAD parcel popup with saved area loaded → "Parcel rating" buttons render. If matched comp also present → both sections appear with explicit section labels.
2. Open parcel popup with saved area loaded but NO matched comp → "Parcel rating" buttons render WITHOUT a label (single-block).
3. Open parcel popup with NO saved area loaded → buttons disabled with "Save area to enable ratings" hint.
4. Click Good on a parcel → red ✓ appears INSTANTLY at parcel centroid. Popup Good button highlights.
5. Click Bad on a different parcel → black ✗ appears INSTANTLY. Parcel polygon NOT dimmed (only the marker).
6. Click Clear on a rated parcel → marker disappears INSTANTLY.
7. Refresh page, reload saved area → ratings restore correctly on the map.

### Race-condition (bundled fix)

8. Fresh page load, click Good on a comp for the first time → checkmark appears INSTANTLY (not after ~300-500ms lag).
9. Same for first parcel Good → instant.
10. DevTools throttle network to slow 3G → click Good → mark appears immediately, server resolves in background, marker stays (server success).
11. DevTools disconnect network → click Good → mark appears immediately, then reverts within ~1s + toast "Rating update failed — reverted".
12. Rapid 3-click sequence on same parcel (good → bad → clear within 200ms) → final state matches the LAST click; no flickering or wrong-state revert. Tests the per-key mutation versioning.

### CSV exclusion — comp-wins-absolute

13. Parcel with NO matched comp, rated Good → CSV row present.
14. Parcel with NO matched comp, rated Bad → CSV row NOT present.
15. Parcel with matched Bad comp, rated Good directly → CSV row NOT present (comp wins).
16. Parcel with matched Good comp, rated Bad directly → CSV row PRESENT (comp wins).
17. Parcel with no rating at all → CSV row present.

### Saved-area lifecycle

18. Save area → rate some parcels → close + reopen → ratings restore.
19. Fork the saved area → forked area shows the same parcel ratings (per fork-copy block).
20. Delete the saved area → all parcel_ratings rows for that workspace cascade-deleted.

### Security / access control

21. User A creates workspace, rates a parcel → User B (logged in, different account) calls POST /api/parcels/rate with A's area_id → 403 Forbidden.
22. Same test for /api/propelio/comp/rate → 403 Forbidden (NEW — fixes existing weakness).
23. Public parcel endpoint /api/parcel/{county}/{account_num} → response has NO user_rating field (verified). Popup opened in browse mode shows no rating buttons (or disabled).

### Multi-county

24. Test parcel ratings in all 4 counties (DCAD, TAD, Collin, Denton) — confirm county lowercase normalization in the FK key.

### Cleanup gates (merge-gate)

25. `git grep _showBanner` returns zero results.
26. `git grep "comp_rating_via_comp"` returns expected refs only (no shadowed old logic).
27. No unintentional changes to comp_ratings schema or endpoint behavior beyond the ownership check addition.
28. cadRatingLayer cleared on workspace switch (no stale markers from previous workspace).

## Risk

| Risk | Severity | Mitigation |
|---|---|---|
| Existing comp rate endpoint suddenly returns 403 for users who were rating shared workspaces (non-owners) | High | If any production usage exists where non-owners rate shared workspaces, the ownership check would break it. Mitigation: check Mike's user table — only one user account is "owner" (Mike); VAs may be on different role. If VAs are accessing shared workspaces and need to rate, we may need a shared-write policy. Confirm via prod query before deploying. |
| Public endpoint hydrate gap causes a visual regression in browse-mode popups (no rating buttons where they previously appeared) | Low | Per investigation, browse-mode popups currently DON'T have parcel rating buttons (those are new). Comp rating buttons only appear when matched comp present, which requires a workspace. Confirmed no regression. |
| Optimistic mark survives a failed POST if revert path errors | Medium | Wrap revert in try/catch; the user-facing toast still fires. Marker simply remains in optimistic state — fine because mutation versioning prevents double-revert. |
| cadRatingLayer not cleared on workspace switch → stale marks persist | High | EXPLICIT lifecycle wiring at all relevant transition points (verification step 28). Easy to verify by smoke-test. |
| Multipolygon centroel calculation: featureCentroidLngLat returns null on degenerate geometry | Low | Fallback chain: bounds.getCenter → featureCentroidLngLat → properties.lat/lng → null (no marker). Marker absence is graceful. |
| Per-key mutation versioning races with applyPropelioClientFilters full re-render | Medium | applyPropelioClientFilters re-creates the comp layer from cache via clearLayers() + re-add. cadRatingLayer is INDEPENDENT — it's NOT cleared by applyPropelioClientFilters. Comp layer's optimistic mark may briefly flicker during re-render but re-appears immediately from cache. Acceptable. |
| Schema migration fails if saved_areas.area_id has any orphaned cached_jobs/etc referencing it | Low | CREATE TABLE has nothing to migrate from. FK reference to saved_areas just means parcel_ratings can't insert non-existent workspace_id — no migration issue. |
| Fork copy fails if source workspace has zero parcel_ratings (INSERT…SELECT returns 0 rows) | Low | Acceptable behavior — 0 rows copied is correct for a fresh source area. |

## Rollback

Single-PR revert. The schema migration is idempotent and safe to leave in place; rollback only reverts code. Data in parcel_ratings becomes orphaned but inert. The ownership check addition to the comp rate endpoint is a one-line change — easy to revert if it breaks Mike's workflow.

## Out of scope

- Sidebar list / "Good Parcels" block
- Filter chips for parcel rating
- Bulk rating UI / mass-flip
- Per-user vs per-workspace rating
- CSV columns for parcel rating value
- Polygon recoloring based on rating
- Animations on mark add/remove
- Shared workspace WRITE access (use fork)
- Public endpoint user_rating hydrate (security)
- Backfill historical ratings

## Open items for second-round Copilot critique (optional)

1. **Prod check on multi-user ratings.** Before deploying the comp rate endpoint ownership check, confirm Mike's user table: is there any current non-owner rating activity that would break? Run `SELECT DISTINCT rated_by_user_id, COUNT(*) FROM comp_ratings WHERE workspace_id NOT IN (SELECT area_id FROM saved_areas WHERE user_id = rated_by_user_id) GROUP BY 1;`. If non-zero, the check breaks an existing workflow.

2. **All authenticated parcel-emit sites covered?** Spec lists `main.py:2964, 987, 4709`. Confirm no other authenticated endpoint emits parcel features that should hydrate rating data.

3. **Optimistic helper extraction.** Spec extracts `_setRatingMarkOptimistic` as a shared helper with a `kind` parameter. Alternative: two parallel functions (comp + parcel) with no shared base. Worth the extraction or keep separate?

4. **Lifecycle wiring depth.** cadRatingLayer must clear on: parcel redraw (7179-7186), draw-clear (8234), workspace switch (multiple sites). Any more transitions to wire? Specifically: search-area-changed? Pan/zoom-driven viewport refresh in browse mode (probably no — browse mode doesn't show ratings)?

5. **CSV semantics edge case.** Comp-wins-absolute means a Bad comp excludes a parcel even if the user explicitly rated the parcel Good (overriding the comp). Surface to the user somehow (a "comp-overridden" indicator)?

6. **Frontend cache name confirmation.** Spec assumes `allAnalysisFeatures` at map.js:573 is canonical. Copilot confirmed. Any edge case where ratings should hydrate into a DIFFERENT cache (e.g., a secondary cache for browse mode)?

7. **Anything else.**

## Implementation effort estimate

- Schema migration: 0.25 day
- Backend ownership helper + new rate endpoint + comp endpoint hardening + hydrate + CSV exclusion + fork copy: 1 day
- Frontend popup HTML + rate function + click delegation + optimistic helper extension + cadRatingLayer + lifecycle wiring + CSS: 1 day
- Comp click path update + race-fix bundled: 0.25 day
- Verification + preview iterate: 0.5 day
- **Total: ~3 days**

## Status

**v2 LOCKED.** Pending final KK review of the spec file. After approval → implementation (Claude codes; Copilot reviews; per the workflow override KK chose earlier).
