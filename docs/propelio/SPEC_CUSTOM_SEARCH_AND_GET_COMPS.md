# Spec v3 — Custom Search rename + Get Comps quick-sweep + Targets scroller

**Owner:** KK (product) + Claude (spec) + Copilot (implementation)
**Target branch:** `feat/propelio-deep-pull-experiment`
**Status:** v3 — incorporates Copilot's go/no-go #2 critique on v2.
Still pre-coding. Needs third go/no-go from Copilot before code lands.

## What changed since v2

Copilot's v2 critique flagged 4 items (1 high, 2 medium, 1 low). v3
addresses each:

- **Complete `propelioQuickRefreshBtn` reference list** (HIGH). v2
  missed two clusters: the error re-enable inside
  `_refreshRecentByPolygon` at map.js:4664-4666 and the completion-path
  re-enable inside `_pollDeepPullStatus` at map.js:8566-8569. v3
  enumerates every reference and requires they all be removed
  atomically.
- **Pass-count contract made explicit** (MEDIUM). v2 retunes
  `PASSES_RECENT` to 3 entries but the frontend hardcodes the divisor
  `3` for `rr_*` jobs at map.js:8490 and 8555. If the list is ever
  retuned to a different length, the UI silently lies. v3 adds a
  module-level `PASSES_RECENT_COUNT` constant + runtime assertion in
  `deep_pull.py`, with cross-reference comments at both ends. Adding
  `passes_total` to the status response is still deferred (would need
  a DB column or in-memory state to be accurate).
- **Geojson helper centralized** (MEDIUM). Single source in
  `api/geo.py` (or `api/propelio/_polygon_geojson.py`). Both
  `routes.py` and `deep_pull.py` import from there. No duplication.
- **Ingestion-stats merge-failure row** (LOW). If the fresh-path merge
  raises, the existing try/except in routes.py:586 still emits a
  response — v3 spec requires that response to include
  `ingestion_stats: {returned: len(comps_list), new_to_cache: 0}` so
  the schema is consistent.

## What changed since v1

(Carried forward from v2 for posterity.)

- **Reuses existing `/refresh-recent/start` infrastructure** instead
  of adding `pass_preset` to `/deep-pull/start`. Eliminates a DB
  schema decision, a status-response shape change, and the risk of
  regressing the dev Deep Pull button.
- **Retunes the existing `PASSES_RECENT` config** to KK's desired
  `[1mo × 1mi, 2mo × 1mi, 3mo × 1mi]` with `SINGLE_FAMILY` preset.
  No new `PASSES_QUICK_SWEEP` constant needed.
- **Hides the existing "Refresh Recent" button** so there's only ONE
  map button (gold "Get Comps"), wired to the recency sweep.
- **Limits `geojson` to `search_cma` only** in this chunk. `add_cma`
  unchanged — defer to a separate probe pass.
- **Drops `fast_mode` entirely**. The existing jitter at
  deep_pull.py:45 has explicit anti-fingerprint intent and the 3-pass
  recent flow already runs in ~2-3 min — fast enough for "quick sweep" UX.
- **Explicitly defines `ingestion_stats`** for every return path in
  `_run_by_polygon`, not just the happy path.
- **Adds non-goal: do not touch routes.py:335** (the just-landed
  use_cache gate). The Phase 2 cache-read short-circuit is fragile
  and we just fixed it.

## 1. Goal

Coherent comp-pull surface for the team. The sidebar "Refresh from
source" becomes **"Custom Search"** with a progress banner that shows
what was pulled and how many were new to cache. The map gold "Get
Comps" button stops firing the 6-minute full Deep Pull and instead
fires the **3-pass quick sweep** (retuned `PASSES_RECENT`: months
1/2/3 at range=1mi, single-family, polygon-constrained via `geojson`)
using the existing `/refresh-recent/start` infrastructure. The
existing "Refresh Recent" button is hidden — only one map button
remains. Full Deep Pull stays in code for marathon use; dev Deep Pull
button preserved untouched. Targets block below saved areas gets the
same scroller treatment saved areas already has.

## 2. Non-goals (do NOT touch)

- Do NOT modify `scripts/marathon_campaign/*` (marathon code).
- Do NOT change `#btn-deep-pull` (the dev Deep Pull button) — stays
  hidden but functional.
- Do NOT add `pass_preset` to `/deep-pull/start` or any field to
  `DeepPullStartRequest`.
- Do NOT add `passes_total` to the status response or any new column
  to `propelio_deep_pull_jobs`. Frontend's existing `rr_` vs `dp_`
  job-id-prefix logic already determines divisor (map.js:8490, 8555).
- Do NOT add a `fast_mode` flag or alter `_jittered_pass_sleep_seconds`
  (deep_pull.py:45). It has anti-fingerprint intent.
- Do NOT add `geojson` to `add_cma` in this chunk. Limit to `search_cma`.
- **Do NOT modify the use_cache gate at routes.py:335** (the just-landed
  fix). Spec changes elsewhere in `_run_by_polygon` must not regress
  that line.
- Do NOT add a property-type dropdown to the UI — SINGLE_FAMILY is
  hardcoded in the retuned `PASSES_RECENT` for now.
- Do NOT change the existing `/refresh-recent/start` body shape or
  `rr_` job-id prefix.
- Do NOT change `propelio_comps` schema or merge logic.
- Do NOT change the hardcoded `3`/`6` divisor logic at map.js:8490
  and 8555 in this chunk. v3 makes the contract explicit via a Python
  constant + cross-reference comments, but the JS still reads job-id
  prefix. Replacing the prefix logic with a server-side
  `passes_total` field is a separate change (out-of-scope follow-up).

## 3. End state

**Sidebar:**
- Button currently `#btn-propelio-refresh` labeled "↻ Refresh from
  source" is relabeled **"Custom Search"** with a one-line subtitle
  "Uses the filters above".
- Click → banner shows "Running custom search..." → on completion
  "Returned N comps · M new to cache".
- Empty case: "0 comps returned for these filters".
- Zero-new case: "Returned N comps · 0 new since last pull".

**Map (single button):**
- Gold `propelioStickyBtn` still labeled "Get Comps", stays
  map-bottom-center.
- Click → calls `_refreshRecentByPolygon()` (the existing function),
  which posts to `/api/propelio/refresh-recent/start` (the existing
  endpoint). Resulting job has `rr_` prefix; frontend infers 3-pass
  divisor automatically via existing prefix logic.
- Banner: "Quick sweep · queued - 3 passes (1mo, 2mo, 3mo), ~2-3 min" →
  per-pass updates → "3 passes · {captured} captured · {netnew} net-new".
- Total wall time: ~2-3 min (existing PASSES_RECENT pacing — NOT the
  unrealistic 30-45s v1 implied; the anti-fingerprint jitter prevents
  going faster without a separate decision).
- During the sweep, the button is disabled with `is-running` class.
- "Refresh Recent" button (`propelioQuickRefreshBtn`) is **removed
  from the DOM creation path** so it never renders.

**Targets block:**
- The "saved-parcels" section already has `id="saved-parcels-list"`
  on its list container per index.html:270-294 (Copilot verified —
  verify only, no new markup). Add CSS rule
  `max-height: 300px; overflow-y: auto;` matching the existing
  `#saved-areas-list` style at line 163-165.

## 4. File-by-file changes

### 4.1 frontend/index.html (~ line 82)

Rename the Custom Search button. Keep id `btn-propelio-refresh` for
JS compatibility:

```html
<!-- WAS -->
<button id="btn-propelio-refresh" class="propelio-refresh-btn" type="button">↻ Refresh from source</button>

<!-- BECOMES -->
<button id="btn-propelio-refresh" class="propelio-refresh-btn" type="button">
  <span class="propelio-refresh-main">Custom Search</span>
  <span class="propelio-refresh-subtitle">Uses the filters above</span>
</button>
```

No other HTML changes. (Saved-parcels markup is already correct per
Copilot review — verify only.)

### 4.2 frontend/map.js

**(a) Add progress banner to Custom Search** — `pullPropelioRefresh()`
at ~line 4283. Wrap the fetch call with banner show/update:

- Before fetch: `_showDeepPullBanner("Running custom search...")`
- On success: read `data.ingestion_stats.returned` and
  `data.ingestion_stats.new_to_cache`, show:
  - both > 0: "Returned {N} comps · {M} new to cache"
  - N > 0, M = 0: "Returned {N} comps · 0 new since last pull"
  - N = 0: "0 comps returned for these filters"
- On error: "Custom search failed: {reason from caught error}"
- All non-running states auto-hide via `setTimeout(_hideDeepPullBanner, 6000)`.

**(b) Rewire gold "Get Comps" button** — currently calls
`_pullPropelioByPolygon()` at line 4535. Change to call
`_refreshRecentByPolygon()`:

```js
// WAS (line 4534-4536):
L.DomEvent.on(propelioStickyBtn, "click", (evt) => {
  L.DomEvent.stopPropagation(evt);
  void _pullPropelioByPolygon();
});

// BECOMES:
L.DomEvent.on(propelioStickyBtn, "click", (evt) => {
  L.DomEvent.stopPropagation(evt);
  void _refreshRecentByPolygon();
});
```

The `_pullPropelioByPolygon` function stays in code for the dev
Deep Pull button + future use, but no longer wired to the gold sticky
button.

**(c) Remove `propelioQuickRefreshBtn` completely — every reference**

Grep on `propelioQuickRefreshBtn` (run on this branch's current
`frontend/map.js`) returns the following complete list. **All must be
removed in the same patch** — leaving any one means the symbol is
either undefined (runtime error) or references a button that no longer
exists in DOM (silent dead code).

| Line(s) | What | Action |
|---|---|---|
| 4421 | `let propelioQuickRefreshBtn = null;` (variable declaration) | **Remove.** With no consumers it's pure dead code. |
| 4520-4528 | DOM creation (`document.createElement`, classes, title, innerHTML, `controlsWrap.appendChild`) | **Remove the whole block.** |
| 4537-4540 | `L.DomEvent.on(propelioQuickRefreshBtn, "click", ...)` handler binding | **Remove the whole `L.DomEvent.on` call.** |
| 4640-4643 | Inside `_refreshRecentByPolygon`: disable-on-start block (`if (propelioQuickRefreshBtn) { ... disabled = true ... is-running }`) | **Replace** with `_setPropelioPolygonButtonState({ text: "Get Comps", disabled: true });` — see §4.2(d) below. |
| 4664-4666 | Inside `_refreshRecentByPolygon`'s catch path: error re-enable block (`if (propelioQuickRefreshBtn) { ... disabled = false ... }`) | **Replace** with `_setPropelioPolygonButtonState({ text: "Get Comps", disabled: false });` so the sticky button re-enables on error. |
| 8566-8569 | Inside `_pollDeepPullStatus` completion path: re-enable block (`if (propelioQuickRefreshBtn) { ... }`) | **Remove the whole `if` block.** The line immediately above at 8564 (`_setPropelioPolygonButtonState({ text: "Get Comps", disabled: false });`) already re-enables the only remaining button, so no replacement is needed here. |

**Atomicity is critical.** A partial removal (e.g., leaving the click
handler at 4537 with the DOM creation gone at 4520) crashes the page
load with `ReferenceError`. Copilot should treat this as one
indivisible change.

After removal, only `propelioStickyBtn` (gold Get Comps) renders in
the bottom-center sticky controls, and only it is referenced in
state-management code paths.

**Sanity grep before commit:** Copilot must run
`grep -n "propelioQuickRefreshBtn" frontend/map.js` and confirm
**zero results** before pushing.

**(d) Update `_refreshRecentByPolygon` banner copy + button-state
target** at line 4624 onwards:

- Line 4644 banner copy: change
  `"Refresh Recent · queued - 3 passes (3mo, 6mo, 12mo), ~2-3 min"`
  to
  `"Quick sweep · queued - 3 passes (1mo, 2mo, 3mo), ~2-3 min"`.
- Button-state management at lines 4640-4643 (disable-on-start) and
  4664-4666 (re-enable-on-error): replace as specified in §4.2(c)
  above. The disable-on-start block becomes
  `_setPropelioPolygonButtonState({ text: "Get Comps", disabled: true });`
  and the error re-enable becomes
  `_setPropelioPolygonButtonState({ text: "Get Comps", disabled: false });`.
- Re-enable on successful completion already happens at map.js:8564
  inside `_pollDeepPullStatus`. After §4.2(c) removes lines 8566-8569,
  the existing line 8564 covers the only remaining button. No new
  completion-path code needed.

**(e) Update the `_pollDeepPullStatus` final banner copy** at
~line 8550-8555. Currently shows "Pass {N}/3 · X captured · Y net-new"
for `rr_` jobs. Update wording to "Quick sweep · {N}/3 passes · X
captured · Y net-new" but **keep the divisor inference logic intact**
(it works correctly via the `rr_` prefix at line 8490).

**(f) Concurrency guard** — `pullPropelioRefresh()` should refuse to
fire if `_activeDeepPullJobId` is set (a quick sweep is mid-run):

```js
async function pullPropelioRefresh() {
  ...
  if (_activeDeepPullJobId) {
    _showDeepPullBanner("A quick sweep is running — wait for it to finish.");
    setTimeout(_hideDeepPullBanner, 4000);
    return;
  }
  ...
}
```

`_refreshRecentByPolygon` already has the inverse guard at line
4629-4630 (rejects if poll is active), so no change needed there.

### 4.3 frontend/style.css

**Targets scroller** — mirror the saved-areas-list rule at line 163-165:

```css
#saved-parcels-list {
  max-height: 300px;
  overflow-y: auto;
}
```

If the slim scrollbar style on `#saved-areas-list` uses a class
(rather than the id), reuse the class on `#saved-parcels-list` too.

**Custom Search subtitle** — if `.propelio-refresh-btn` doesn't already
support multi-line typography, add minimal rules for
`.propelio-refresh-main` (main label) and `.propelio-refresh-subtitle`
(smaller, dimmer subtitle below). Visual style matches the existing
`propelioQuickRefreshBtn` two-line layout (which is being removed but
its styling at `.quick-refresh-main` / `.quick-refresh-subtitle` is a
good reference; copy and adapt the rules under `.propelio-refresh-*`).

### 4.4 api/propelio/deep_pull.py

**(a) Retune `PASSES_RECENT` and lock its length with a constant**
at lines 44-48:

```python
# WAS:
PASSES_RECENT = [
    {"months": 3, "range_mi": 0.5, "label": "recent_tight"},
    {"months": 6, "range_mi": 1.0, "label": "recent_neighborhood"},
    {"months": 12, "range_mi": 2.0, "label": "year_broader"},
]

# BECOMES:
PASSES_RECENT = [
    {"months": 1, "range_mi": 1.0, "label": "quick_sweep_1mo",
     "property_type_presets": ["SINGLE_FAMILY"]},
    {"months": 2, "range_mi": 1.0, "label": "quick_sweep_2mo",
     "property_type_presets": ["SINGLE_FAMILY"]},
    {"months": 3, "range_mi": 1.0, "label": "quick_sweep_3mo",
     "property_type_presets": ["SINGLE_FAMILY"]},
]

# FRONTEND CONTRACT: frontend/map.js hardcodes the divisor 3 for rr_*
# job_ids at two sites (currently lines ~8490 and ~8555). If this list's
# length ever changes, those two sites MUST be updated in the same
# commit. The assertion below makes a length mismatch a startup error,
# not a silent UI bug. Adding a passes_total field to the status
# response (so the frontend can read length from the server) is a
# deferred follow-up — see §8.
PASSES_RECENT_COUNT = 3
assert len(PASSES_RECENT) == PASSES_RECENT_COUNT, (
    "PASSES_RECENT length changed — also update frontend hardcoded "
    "divisor at map.js:~8490 and ~8555 (search 'rr_' branches)."
)
```

Update the docstring at line 41 to reflect the new configuration
("Three passes at 1mi SFR across last 1-3 months. Catches recent
stragglers — pendings + just-listed actives.").

**Companion comments in `frontend/map.js`** — add a one-line comment
immediately above each of the two hardcoded `3` literals (currently at
lines 8490 and 8555):

```js
// Contract: 3 must match api/propelio/deep_pull.py:PASSES_RECENT_COUNT.
// A backend assertion guards length; this comment guards the JS edit.
const totalPasses = jobId.startsWith("rr_") ? 3 : 6;
```

These comments give a future-editor a grep path between the two
contract endpoints. Copilot should add the comment at both 8490 and
8555 (the completion-path mirror).

**(b) Pass `property_type_presets` + `geojson` through to scraper**
at the `search_cma` call site in `run_deep_pull` (~lines 488-493):

Read from the active pass config:
```python
property_type_presets = cfg.get("property_type_presets") or None
```

For `geojson`, fetch the polygon for the job's `saved_area_id` once
at the top of the function via the new helper (4.4(c) below). Cache
the result locally (`polygon_geojson`) and forward to every
`search_cma` call:

```python
cma_response = await asyncio.to_thread(
    client.search_cma,
    lead_id, cma_id,
    months=cfg["months"], range_mi=cfg["range_mi"],
    geojson=polygon_geojson,
    property_type_presets=property_type_presets,
)
```

For `add_cma` (~line 437-443): **no change** (per non-goal: geojson
and presets stay off `/add` in this chunk).

**(c) Add `_load_polygon_geojson_for_job` helper** — looks up the
polygon by `saved_area_id` from the job row, then delegates the
validation + GeoJSON shape to the centralized helper in `api/geo.py`
(see §4.7). Returns `None` on any failure (in which case the run
proceeds without geojson — Propelio falls back to circle search based
on months/range, no crash).

```python
from api.geo import build_polygon_geojson_feature_collection

def _load_polygon_geojson_for_job(job_id: str) -> dict[str, Any] | None:
    """Load the polygon associated with this job (via saved_area_id)
    and return a GeoJSON FeatureCollection. None if absent or invalid.
    """
    # 1. Query: SELECT saved_area_id FROM propelio_deep_pull_jobs WHERE job_id = %s
    # 2. If saved_area_id is null/missing: return None
    # 3. Query: SELECT polygon FROM saved_areas WHERE area_id = %s
    # 4. Delegate to api.geo.build_polygon_geojson_feature_collection
    #    (returns None on validation failure, logs warning).
```

On validation failure (`build_polygon_geojson_feature_collection`
returns `None`): log a warning and return `None`. Do NOT fail the
job — geojson is optional.

**(d) Call the helper once per job** — at the top of `run_deep_pull`,
right after job claim:

```python
polygon_geojson = await asyncio.to_thread(_load_polygon_geojson_for_job, job_id)
```

Use `polygon_geojson` in every `search_cma` call inside the loop.

### 4.5 api/propelio/scraper.py

**(a) `search_cma()` at line 853** — accept `geojson` and
`property_type_presets` kwargs; include in POST body when provided:

```python
def search_cma(
    self,
    lead_id: str,
    cma_id: str,
    months: int = 24,
    range_mi: float = 7.5,
    *,
    geojson: dict[str, Any] | None = None,
    property_type_presets: list[str] | None = None,
) -> Dict[str, Any]:
    ...
    body: Dict[str, Any] = {"months": int(months), "range": str(range_mi)}
    if geojson is not None:
        body["geojson"] = geojson
    if property_type_presets is not None:
        body["propertyTypePresets"] = list(property_type_presets)
    ...
```

**(b) `add_cma()` at line 789** — **no change**. Keep current
body shape. (Per non-goal: defer geojson/presets on `/add` to a
separate probe pass.)

**(c) `search_properties()` at line 977** — accept the same two
kwargs and forward them to `search_cma` (NOT `add_cma`). For `add_cma`
call site inside `search_properties`, do NOT pass geojson/presets.

The `/refresh` and `/by-polygon` endpoint flows (which call
`search_properties` via `_run_by_polygon`) need to thread these
through. See 4.6.

### 4.6 api/propelio/routes.py

**(a) Import the centralized geojson helper** from `api.geo` (see
§4.7 for the helper itself):

```python
from api.geo import build_polygon_geojson_feature_collection
```

There is no longer a `_build_polygon_geojson` defined locally in
`routes.py`. Both this route flow and the `deep_pull.py` worker flow
share one implementation.

**(b) `_run_by_polygon` at line 325** — build polygon-derived
`geojson` once and pass to `search_properties`. Insert before line
459 (the `search_properties` call):

```python
polygon_geojson = build_polygon_geojson_feature_collection(polygon)
```

Pass to `search_properties`:

```python
subject, comps_list = await asyncio.to_thread(
    scraper_mod.search_properties,
    subject_parcel["address"],
    months=months,
    range_mi=range_mi,
    geojson=polygon_geojson,
)
```

**(c) Define `ingestion_stats` for every return path** — explicit
shapes per branch. The key invariant: **every successful HTTP response
from `_run_by_polygon` must include `ingestion_stats` with both
`returned` and `new_to_cache` as integers**. The frontend banner
copy at §6 reads these unconditionally.

| Return path | `ingestion_stats` shape |
|---|---|
| Phase 2 cache-read hit (lines 388-414) | `{"returned": len(cached_global), "new_to_cache": 0}` |
| Phase 2 cache-only mode (lines 377-386) | `{"returned": len(cached_global) if cached_global else 0, "new_to_cache": 0}` |
| Legacy cache hit (lines 428-452) | `{"returned": <len cached comps>, "new_to_cache": 0}` |
| Fresh scrape happy path (lines 550-590) | Capture `merge_result` from `merge_comps_into_global(...)` at line 582; report `inserted` count |
| **Fresh scrape, merge raised** (caught by existing try/except at routes.py:586) | `{"returned": len(comps_list), "new_to_cache": 0}` — see explicit handling below |
| Empty/no-coverage warning (lines 466-528) | `{"returned": 0, "new_to_cache": 0}` |
| Scraper error (lines 530-548) | n/a — raises HTTPException, no payload |

For the fresh scrape path at ~line 561, capture the merge result:

```python
merge_result = merge_comps_into_global(payload.get("comps") or [], "by_polygon")
payload["ingestion_stats"] = {
    "returned": len(comps_list),
    "new_to_cache": int(merge_result.get("inserted", 0)) if isinstance(merge_result, dict) else 0,
}
```

For the merge-failure path (try/except wrapping
`merge_comps_into_global` at ~routes.py:586): on caught exception, log
the error as today but **also set ingestion_stats before returning**:

```python
try:
    merge_result = merge_comps_into_global(payload.get("comps") or [], "by_polygon")
    payload["ingestion_stats"] = {
        "returned": len(comps_list),
        "new_to_cache": int(merge_result.get("inserted", 0)) if isinstance(merge_result, dict) else 0,
    }
except Exception as exc:
    logger.exception("merge_comps_into_global failed; returning raw comps without cache write")
    payload["ingestion_stats"] = {
        "returned": len(comps_list),
        "new_to_cache": 0,
    }
```

This keeps the response schema consistent regardless of merge outcome,
so the frontend banner code never has to branch on missing fields.

For each other path, set `payload["ingestion_stats"]` explicitly
before the return.

**(d) `/refresh-recent/start` endpoint** at line 798 — **no body
shape change**. The endpoint already takes `DeepPullStartRequest`
and creates an `rr_*` job. The polygon-lookup-and-geojson logic lives
in `run_deep_pull` (via `_load_polygon_geojson_for_job`), so the
endpoint itself doesn't change.

**(e) `/refresh` and `/by-polygon` endpoints** — no signature changes.
Both go through `_run_by_polygon` which now threads geojson through
to `search_properties`.

### 4.7 api/geo.py — centralized polygon → GeoJSON helper

This file already exists at `api/geo.py` with `polygon_bbox`,
`polygon_centroid`, `haversine_miles`, `point_in_polygon`. Add one
more public function. Single home for the polygon → GeoJSON
FeatureCollection conversion, used by both `routes.py` (synchronous
`/refresh` path) and `deep_pull.py` (async per-pass `search_cma` path):

```python
from typing import Any

def build_polygon_geojson_feature_collection(
    polygon: list[list[float]] | None,
) -> dict[str, Any] | None:
    """Build a single-feature FeatureCollection from a [[lng,lat],...] ring.

    Used by Propelio scraper calls (search_cma) to constrain results to
    the user-drawn polygon. Returns None if malformed — callers must
    proceed without geojson; Propelio falls back to circle search based
    on months/range, no crash.

    Validation:
    - Require list-of-lists, each inner list >= 2 floats, length >= 3.
    - Coerce to float; reject on TypeError/ValueError.
    - Close the ring if first != last.

    Shape:
        {"type": "FeatureCollection",
         "features": [{"type": "Feature",
                       "geometry": {"type": "Polygon",
                                    "coordinates": [closed_ring]},
                       "properties": {}}]}
    """
    if not isinstance(polygon, list) or len(polygon) < 3:
        return None
    cleaned: list[list[float]] = []
    for p in polygon:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            return None
        try:
            lng, lat = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            return None
        cleaned.append([lng, lat])
    if cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [cleaned]},
            "properties": {},
        }],
    }
```

**Rationale for the location.** `api/geo.py` already owns the other
polygon math (`polygon_bbox`, `polygon_centroid`, `point_in_polygon`)
shared between routes and workers. Adding the GeoJSON builder there
keeps polygon-related primitives co-located. An alternative
(`api/propelio/_polygon_geojson.py`) would tie the helper to one
integration, but the function is generic enough to belong in
`api.geo`.

**Tests:** at minimum, smoke 7 in §7 exercises the malformed-input
path indirectly via the integration. If Copilot adds a tiny unit
test next to existing `api/geo.py` tests, that's bonus — not required
for this chunk.

## 5. Endpoint shapes (final)

```
POST /api/propelio/refresh                       (existing, unchanged body)
  Response NOW includes:
    ingestion_stats: { returned: int, new_to_cache: int }

POST /api/propelio/by-polygon                    (existing, unchanged body)
  Response NOW includes:
    ingestion_stats: { returned: int, new_to_cache: int }

POST /api/propelio/refresh-recent/start          (existing, unchanged body)
  Response unchanged. Internally now uses retuned PASSES_RECENT and
  sends geojson + propertyTypePresets on each pass's search_cma call.

POST /api/propelio/deep-pull/start               (existing, unchanged body)
  Untouched. Dev button still works.

GET /api/propelio/deep-pull/status/{job_id}      (existing, unchanged)
  No `passes_total` field added. Frontend continues inferring divisor
  from `rr_` vs `dp_` job-id prefix.
```

## 6. Banner state matrix (final)

| Trigger | Phase | Banner text |
|---|---|---|
| Custom Search click | starting | "Running custom search..." |
| Custom Search done, N>0, M>0 | done | "Returned {N} comps · {M} new to cache" |
| Custom Search done, N>0, M=0 | done | "Returned {N} comps · 0 new since last pull" |
| Custom Search done, N=0 | done | "0 comps returned for these filters" |
| Custom Search blocked (sweep mid-run) | blocked | "A quick sweep is running — wait for it to finish." |
| Custom Search error | error | "Custom search failed: {reason}" |
| Get Comps click | starting | "Quick sweep · queued - 3 passes (1mo, 2mo, 3mo), ~2-3 min" |
| Get Comps per-pass (from poll) | running | "Pass {k}/3 done · {captured} captured" |
| Get Comps done | done | "3 passes · {captured} captured · {netnew} net-new" |
| Get Comps error | error | "Quick sweep failed: see console" |

All non-running states auto-hide after 6s via existing
`setTimeout(_hideDeepPullBanner, 6000)`.

## 7. Smoke test plan

**Smoke 1 — Custom Search basic:**
- Load 2451 Crest Ridge, set months=1
- Click Custom Search → banner shows "Returned 50-100 · X new"
- DB check: most recent `last_seen_at` for Crest Ridge area comps
  should be within last 60s

**Smoke 2 — Get Comps quick sweep:**
- Same lead
- Click gold Get Comps → 3 passes complete in ~2-3 min
- Final banner: "3 passes · {captured} · {netnew}"
- DB check: at least 8-12 pendings in the polygon after sweep

**Smoke 3 — Empty case:**
- Low-velocity area (5528 Victor St)
- Custom Search → "0 comps returned for these filters"
- Get Comps → "3 passes · 0 captured · 0 net-new"

**Smoke 4 — Concurrency:**
- Click Get Comps, wait for "Pass 1/3" state
- Click Custom Search → banner says "A quick sweep is running — wait..."

**Smoke 5 — Targets scroller:**
- Save 10+ parcels to a workspace, open targets section, confirm scrollbar

**Smoke 6 — Geojson actually applied (server-side):**
- After Custom Search on Crest Ridge, query 5 most recent comps inserted:
  ```sql
  SELECT mls, lat, lng FROM propelio_comps
  WHERE first_seen_at > NOW() - INTERVAL '2 minutes'
  ORDER BY first_seen_at DESC LIMIT 5;
  ```
- All should fall inside the Crest Ridge polygon (ST_Contains check).

**Smoke 7 — Polygon-validation fallback:**
- Confirm that if `polygon` is malformed (e.g., manually inject 2 vertices),
  `api.geo.build_polygon_geojson_feature_collection` returns None, the
  call proceeds with circle search, no crash, log shows a warning.

**Smoke 8 — use_cache gate did not regress:**
- Confirm: with `PHASE_2_CACHE_READ=true`, hitting `/refresh` still
  bypasses cache (use_cache=False from Refresh endpoint). Run the
  existing `scripts/propelio_refresh_smoke.py` — should pass identically
  to its current state.

## 8. Out-of-scope follow-ups

- Probe `add_cma` for `geojson` honor; expand if confirmed.
- Property-type dropdown in sidebar (defer until Mike requests).
- Show `passes_total` in status response so the frontend can stop
  inferring divisors from job-id prefix. This is the durable fix for
  the v2 medium-risk item Copilot flagged. v3's
  `PASSES_RECENT_COUNT` assertion + cross-reference comments are a
  cheap guard until the better fix lands. The reason `passes_total`
  is deferred: it needs either a new column on
  `propelio_deep_pull_jobs` (DB migration) or in-memory state that
  doesn't survive runner restart — both are larger than the rest of
  this chunk.
- Marathon auto-restart + heartbeat alert.
- Decide if jitter pacing should be tunable per-preset (currently
  global). Right now PASSES_RECENT uses the same jitter as full PASSES;
  total wall time for the new 3-pass config will be ~2-3 min.
- Optional unit test in the `api/geo.py` test file covering
  `build_polygon_geojson_feature_collection` malformed-input branches
  (length<3, non-numeric vertex, missing close-of-ring). Spec
  considers this a "nice to have" not required for shipping the chunk.

## 9. Copilot critique gate #3 — go/no-go

Per `feedback_copilot_iteration_loop` upgraded loop, this is the
third pass. Each of Copilot's v2 critique items has a v3 response.
Confirm each is adequately addressed (or push back with a
counter-argument) and answer the general questions:

**v2 critique responses to verify:**

1. **(HIGH) Button-removal completeness.** §4.2(c) now enumerates all
   6 reference clusters (4421, 4520-4528, 4537-4540, 4640-4643,
   4664-4666, 8566-8569) with explicit per-cluster actions and a
   sanity-grep step. Does this list now match your grep output? Is
   the atomicity warning sufficient, or should the spec also reorder
   the deletes (e.g., remove handler bind before DOM creation)?

2. **(MEDIUM) Pass-count contract.** §4.4(a) adds
   `PASSES_RECENT_COUNT = 3` + runtime assertion, plus
   cross-reference comments in map.js at lines 8490 and 8555. Is the
   assertion-based contract enough? Or should this chunk go further
   and add a `passes_total` field to the status response (would need
   to either persist on the jobs table or derive from job_id prefix
   server-side — both have downsides documented in §8)?

3. **(MEDIUM) Geojson helper centralization.** §4.7 introduces
   `api.geo.build_polygon_geojson_feature_collection`. Both
   `routes.py` (§4.6(a)/(b)) and `deep_pull.py` (§4.4(c)) import from
   there. Is `api.geo` the right home, or do you prefer a
   propelio-specific location (`api/propelio/_polygon_geojson.py`)?
   Either works; v3 picks `api.geo` because the rest of the polygon
   math already lives there.

4. **(LOW) Ingestion-stats merge failure.** §4.6(c) now has an
   explicit table row + code block for the merge-exception path,
   emitting `{returned: len(comps_list), new_to_cache: 0}`. Does this
   cover the failure mode you had in mind?

**General go/no-go questions (carry-over):**

5. Does reusing `/refresh-recent/start` cleanly cover the Get Comps
   semantics? Any hidden assumption in the existing flow we'd break
   by retuning `PASSES_RECENT`?

6. Does anything in v3 risk regressing the use_cache gate at
   routes.py:335?

End with EXACTLY ONE of:
- `SPEC IS READY — proceed to code`
- `SPEC NEEDS ANOTHER ROUND — see critique above`

If `SPEC IS READY`, the next prompt to you will be the coding
prompt — at that point edit files only (no commits, no push;
Claude handles those after verification).
