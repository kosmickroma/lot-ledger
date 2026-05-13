# Spec v2 — Custom Search rename + Get Comps quick-sweep + Targets scroller

**Owner:** KK (product) + Claude (spec) + Copilot (implementation)
**Target branch:** `feat/propelio-deep-pull-experiment`
**Status:** v2 — incorporates Copilot's deep-dive critique from v1.
Still pre-coding. Needs second go/no-go from Copilot before code lands.

## What changed since v1

Copilot's critique surfaced one big architectural simplification and
several smaller risk-reductions. v2:

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

**(c) Hide the "Refresh Recent" button creation** — remove or
comment-out the block at lines 4520-4528 (the `propelioQuickRefreshBtn`
creation) AND the corresponding click handler at lines 4537-4540. Both
go. The variable declaration at line 4421 (`let propelioQuickRefreshBtn
= null;`) can stay (it's null and never used elsewhere now) or be
removed — Copilot's call.

Grep confirms `propelioQuickRefreshBtn` is only referenced at lines
4421, 4520-4528, 4537-4540, and inside `_refreshRecentByPolygon` at
4640-4643 (which we update in step (d)). No other consumers.

After removal, only `propelioStickyBtn` (gold Get Comps) renders in
the bottom-center sticky controls.

**(d) Update `_refreshRecentByPolygon` banner copy + button-state
target** at line 4624 onwards:

- Line 4644 banner copy: change
  `"Refresh Recent · queued - 3 passes (3mo, 6mo, 12mo), ~2-3 min"`
  to
  `"Quick sweep · queued - 3 passes (1mo, 2mo, 3mo), ~2-3 min"`.
- Button-state management at lines 4640-4643 currently disables
  `propelioQuickRefreshBtn`. Change to disable `propelioStickyBtn`
  via the existing helper:
  ```js
  _setPropelioPolygonButtonState({ text: "Get Comps", disabled: true });
  ```
  And re-enable on completion via the existing path at
  `_pollDeepPullStatus` (~line 8500).

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

**(a) Retune `PASSES_RECENT`** at lines 44-48:

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
```

Update the docstring at line 41 to reflect the new configuration
("Three passes at 1mi SFR across last 1-3 months. Catches recent
stragglers — pendings + just-listed actives.").

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
polygon by `saved_area_id` from the job row, validates, and converts
to a GeoJSON FeatureCollection. Returns `None` on any failure (in
which case the run proceeds without geojson — Propelio falls back to
circle search based on months/range, no crash).

```python
def _load_polygon_geojson_for_job(job_id: str) -> dict[str, Any] | None:
    """Load the polygon associated with this job (via saved_area_id)
    and return a GeoJSON FeatureCollection. None if absent or invalid.
    """
    # 1. Query: SELECT saved_area_id FROM propelio_deep_pull_jobs WHERE job_id = %s
    # 2. If saved_area_id is null/missing: return None
    # 3. Query: SELECT polygon FROM saved_areas WHERE area_id = %s
    # 4. Validate: list of >=3 [lng,lat] pairs, numeric, ring closure.
    # 5. Build FeatureCollection (see routes.py _build_polygon_geojson
    #    for the shape — extract to a shared module if both call sites
    #    end up identical).
```

Polygon validation steps (same as `routes.py._build_polygon_geojson`):
1. Require list-of-lists, each inner list ≥ 2 floats, length ≥ 3.
2. Close the ring if first != last.
3. Build:
   ```python
   {"type": "FeatureCollection",
    "features": [{"type": "Feature",
                  "geometry": {"type": "Polygon",
                               "coordinates": [closed_ring]},
                  "properties": {}}]}
   ```
   where `closed_ring` is `[[lng, lat], ..., [lng_first, lat_first]]`.

On validation failure: log a warning and return `None`. Do NOT fail
the job — geojson is optional.

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

**(a) `_build_polygon_geojson` helper** — add at module level near
other geometry helpers (~line 200 area). This is the SAME shape as
the deep_pull.py helper in 4.4(c) — Copilot to evaluate whether to
extract to a shared module (`api.geo`?) or duplicate.

```python
def _build_polygon_geojson(polygon: list[list[float]]) -> dict[str, Any] | None:
    """Build a single-feature FeatureCollection from a [[lng,lat],...] ring.

    Returns None if malformed; callers proceed without geojson and
    Propelio falls back to circle search.
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

**(b) `_run_by_polygon` at line 325** — build polygon-derived
`geojson` once and pass to `search_properties`. Insert before line
459 (the `search_properties` call):

```python
polygon_geojson = _build_polygon_geojson(polygon)
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
shapes per branch:

| Return path | `ingestion_stats` shape |
|---|---|
| Phase 2 cache-read hit (lines 388-414) | `{"returned": len(cached_global), "new_to_cache": 0}` |
| Phase 2 cache-only mode (lines 377-386) | `{"returned": len(cached_global) if cached_global else 0, "new_to_cache": 0}` |
| Legacy cache hit (lines 428-452) | `{"returned": <len cached comps>, "new_to_cache": 0}` |
| Fresh scrape happy path (lines 550-590) | Capture `merge_result` from `merge_comps_into_global(...)` at line 582; report `inserted` count |
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
  `_build_polygon_geojson` returns None, the call proceeds with circle
  search, no crash, log shows a warning.

**Smoke 8 — use_cache gate did not regress:**
- Confirm: with `PHASE_2_CACHE_READ=true`, hitting `/refresh` still
  bypasses cache (use_cache=False from Refresh endpoint). Run the
  existing `scripts/propelio_refresh_smoke.py` — should pass identically
  to its current state.

## 8. Out-of-scope follow-ups

- Probe `add_cma` for `geojson` honor; expand if confirmed.
- Property-type dropdown in sidebar (defer until Mike requests).
- Show `passes_total` in status response (defer until UI needs more
  flexibility than `rr_`/`dp_` prefix).
- Marathon auto-restart + heartbeat alert.
- Decide if jitter pacing should be tunable per-preset (currently
  global). Right now PASSES_RECENT uses the same jitter as full PASSES;
  total wall time for the new 3-pass config will be ~2-3 min.

## 9. Copilot critique gate #2 — go/no-go

Per `feedback_copilot_iteration_loop` upgraded loop, this is the
second pass. Copilot reviews this v2 and answers these specific
questions in addition to general critique:

1. Does reusing `/refresh-recent/start` cleanly cover the Get Comps
   semantics, or is there a hidden assumption in the existing flow
   we'd break by retuning `PASSES_RECENT`?

2. Is `_load_polygon_geojson_for_job` (helper in `run_deep_pull`)
   the right place for the polygon-to-geojson conversion, or should
   it live in `routes.py` and be passed in via the job row? Or should
   both call sites import a shared helper from `api.geo`?

3. Does my `ingestion_stats` table in §4.6(c) cover every actual
   return path in `_run_by_polygon`, or did I miss one?

4. Is removing `propelioQuickRefreshBtn` from the DOM creation safe?
   Grep result shows references only at lines 4421, 4520-4528,
   4537-4540, and inside `_refreshRecentByPolygon`. Anywhere else?

5. Does anything in this v2 risk regressing the use_cache gate
   at routes.py:335?

End with EXACTLY ONE of:
- `SPEC IS READY — proceed to code`
- `SPEC NEEDS ANOTHER ROUND — see critique above`
