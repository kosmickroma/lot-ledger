# Spec — Custom Search rename + Get Comps quick-sweep + Targets scroller

**Owner:** KK (product) + Claude (spec) + Copilot (implementation)
**Target branch:** `feat/propelio-deep-pull-experiment` (rebase after merging `fix/propelio-refresh-respects-use-cache`)
**Status:** Draft for Copilot deep-dive critique. Do NOT code yet.

## 1. Goal in one paragraph

Rename the user-facing comp-pull surface to be coherent. The sidebar
"Refresh from source" becomes **"Custom Search"** — a user-driven
filtered pull with a progress banner that shows what was pulled and
what was new. The map-bottom-center gold "Get Comps" button stops
firing the 6-minute marathon-style Deep Pull and instead fires a
**3-pass quick sweep** (months 1/2/3 at range=1mi, single-family,
constrained to the user's drawn polygon via the `geojson` param) with
the same progress banner pattern. Targets block below saved areas
gets the same scroller treatment saved areas already has.

## 2. Non-goals (do NOT do in this chunk)

- Do NOT change the marathon code (`scripts/marathon_campaign/*`).
- Do NOT remove the dev "Deep Pull (dev)" button (`#btn-deep-pull`) —
  it stays hidden but functional for developer/admin use.
- Do NOT add property type dropdown to the sidebar — single-family
  is hardcoded in the quick sweep config for now.
- Do NOT add `propertyTypePresets` to the Custom Search (sidebar)
  path — only Get Comps uses it in this chunk.
- Do NOT touch the rendering pipeline, status filters, or comp colors.
- Do NOT change `propelio_comps` schema or merge logic.
- Do NOT cherry-pick the Refresh-respect-use_cache fix as part of
  this chunk — that's a separate prerequisite (commit `2b8a2ba`).

## 3. End state (what KK + team see after this ships)

**Sidebar:**
- The button currently labeled "↻ Refresh from source" is now
  labeled **"Custom Search"** with a subtitle line "Uses the filters
  above" or similar (one line, short).
- Clicking it shows a banner identical in shape to Deep Pull's banner:
  "Running custom search..." then on completion "Returned 47 comps,
  12 new to cache." Banner auto-hides ~6s after completion.
- If the response is empty (0 returned), banner says "0 comps returned
  for these filters" instead of disappearing silently.
- If the response returns >0 but 0 new to cache, banner says
  "Returned 47 comps · 0 new since last pull."

**Map (gold button):**
- The gold "Get Comps" button still sits map-bottom-center.
- Clicking it fires a 3-pass quick sweep using the same Deep Pull
  infrastructure but with `pass_preset: "quick_sweep"`. Body includes
  the saved_area_id so the server can look up the polygon and send it
  as `geojson` in each Propelio call.
- Banner: "Pass 0/3, quick sweep in progress..." then "Pass 1/3 done..."
  then on completion "3 passes · 168 captured · 42 net-new."
- Total wall time: ~30-45s (3 calls × ~10s each + 2-3s inter-pass
  sleeps).
- During the sweep, the button is disabled with `is-running` class.

**Targets block:**
- The "saved-parcels" section (`<details data-target="saved-parcels-body">`)
  matches the saved-areas scroller — `max-height: 300px; overflow-y: auto;`
  with the slim scrollbar style.

## 4. File-by-file changes

### 4.1 frontend/index.html

**Rename Custom Search button** (line ~82, look for `id="btn-propelio-refresh"`):

```html
<!-- WAS -->
<button id="btn-propelio-refresh" class="propelio-refresh-btn" type="button">↻ Refresh from source</button>

<!-- BECOMES -->
<button id="btn-propelio-refresh" class="propelio-refresh-btn" type="button">
  <span class="propelio-refresh-main">Custom Search</span>
  <span class="propelio-refresh-subtitle">Uses the filters above</span>
</button>
```

Keep the existing `id` so all JS wire-up still works.

**Saved-parcels scroller** (line ~284, the `<details>` block whose toggle
is `data-target="saved-parcels-body"`): inside the body div, find the
list container and give it an id `saved-parcels-list` if it doesn't have
one. (Copilot to confirm exact existing markup and decide whether the
list is in `.section-body` directly or wrapped.)

### 4.2 frontend/map.js

**(a) Banner content for Custom Search** — `pullPropelioRefresh()` at
~line 4283. Wrap the fetch call with banner show/update:

- Before fetch: `_showDeepPullBanner("Running custom search...")`
- On success: `_showDeepPullBanner("Returned N comps · M new to cache")`
- On 0-comp success: `_showDeepPullBanner("0 comps returned for these filters")`
- On 0-new success: `_showDeepPullBanner("Returned N comps · 0 new since last pull")`
- On error: `_showDeepPullBanner("Custom search failed: ...")`
- Auto-hide after ~6s via `setTimeout(_hideDeepPullBanner, 6000)` on
  all non-running states.

The response needs to include the counts. See §4.4 for the response
shape change.

**(b) Get Comps gold button → quick sweep** — `_pullPropelioByPolygon()`
at ~line 4583. Change the POST body to include `pass_preset: "quick_sweep"`:

```js
body: JSON.stringify({
  target_address: targetAddress,
  saved_area_id: _currentLoadedAreaId || null,
  pass_preset: "quick_sweep",  // <— new
}),
```

Change the initial banner copy from `"Pass 0/6"` to `"Pass 0/3, quick
sweep starting..."`.

The status poll at `_pollDeepPullStatus()` (~line 8484) hardcodes
`/6` in its banner text — change to read from the response if the
server returns `passes_total` (preferred — see §4.4), or hardcode `/3`
when `pass_preset === "quick_sweep"` is known client-side.

**(c) `readPropelioFiltersFromUI()` at ~line 3971** — no changes needed.
Custom Search continues to pull `months` and `range` from the existing
sidebar inputs.

### 4.3 frontend/style.css

**Saved-parcels scroller** — mirror the saved-areas-list rule at line 163-165:

```css
#saved-parcels-list {
  max-height: 300px;
  overflow-y: auto;
}
```

If the slim scrollbar style is on a shared class, reuse it. Otherwise
copy whatever rule applies to `#saved-areas-list`.

**Banner content styling** — no new banner CSS unless the existing
banner can't accommodate the two-line "Returned N · M new" format
gracefully. If it can't, add a `.deep-pull-banner-detail` line style.

**Custom Search subtitle** — match the existing "↻ Refresh from
source" font sizes / colors but two-line. If the existing
`.propelio-refresh-btn` doesn't have multi-line styling, add a
`.propelio-refresh-main` and `.propelio-refresh-subtitle` block.

### 4.4 api/propelio/routes.py

**(a) Extend `DeepPullStartRequest`** at line 60:

```python
class DeepPullStartRequest(BaseModel):
    target_address: str
    saved_area_id: str | None = None
    pass_preset: str | None = None  # <— new; "quick_sweep" supported
```

**(b) In `start_deep_pull()`** at line 760: pass the preset to the
worker. Currently it calls `asyncio.create_task(run_deep_pull(job_id))`
at line 790. Change to pass the preset string through.

**(c) Modify `_run_by_polygon()` at line ~325** — build a geojson
FeatureCollection from the polygon and thread it through to
`search_properties()`. Insert just before the `scraper_mod.search_properties`
call at line 459:

```python
polygon_geojson = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [polygon + [polygon[0]] if polygon and polygon[0] != polygon[-1] else polygon],
        },
        "properties": {},
    }],
}
```

(Copilot to verify the LngLat coordinate order matches what Propelio
expects — based on our probes, `[lng, lat]` pairs are correct for the
geojson body Propelio echoes back.)

Then pass to `search_properties(..., geojson=polygon_geojson)`.

**(d) Add response counts to `_run_by_polygon` payload** — at the
`payload["polygon_meta"] = ...` block (~line 561), also surface:

```python
payload["ingestion_stats"] = {
    "returned": len(comps_list),
    "new_to_cache": int(merge_result.get("inserted", 0)) if merge_result else 0,
}
```

Where `merge_result` is the return value of `merge_comps_into_global()`
(already called at line ~582). Capture its return value.

### 4.5 api/propelio/scraper.py

**(a) `search_cma()` at line 853** — accept `geojson` and
`property_type_presets` kwargs, include in the POST body when provided:

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
    body = {"months": int(months), "range": str(range_mi)}
    if geojson is not None:
        body["geojson"] = geojson
    if property_type_presets is not None:
        body["propertyTypePresets"] = list(property_type_presets)
    ...
```

**(b) `add_cma()` at line 789** — same kwarg additions, same body
extension. Confirmed via probes that Propelio ignores `months`/`range`
in `/add` but does NOT ignore `geojson` in `/add` — Copilot to verify
by reading the existing `add_cma` body construction.

**(c) `search_properties()` at line 977** — accept the same two kwargs
and forward them to both `add_cma` and `search_cma`:

```python
def search_properties(
    address: str,
    *,
    client: Optional[PropelioClient] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    months: int = 24,
    range_mi: float = 1.0,
    geojson: dict[str, Any] | None = None,
    property_type_presets: list[str] | None = None,
) -> Tuple[Property, List[Property]]:
    ...
```

### 4.6 api/propelio/deep_pull.py

**(a) New pass config** — add at top of file near line 31:

```python
PASSES_QUICK_SWEEP = [
    {
        "months": 1,
        "range_mi": 1.0,
        "label": "quick_sweep_1mo",
        "property_type_presets": ["SINGLE_FAMILY"],
    },
    {
        "months": 2,
        "range_mi": 1.0,
        "label": "quick_sweep_2mo",
        "property_type_presets": ["SINGLE_FAMILY"],
    },
    {
        "months": 3,
        "range_mi": 1.0,
        "label": "quick_sweep_3mo",
        "property_type_presets": ["SINGLE_FAMILY"],
    },
]
```

**(b) Modify `run_deep_pull()` at line 396** — add a `fast_mode` flag
that uses 2-3s inter-pass sleeps instead of `_jittered_pass_sleep_seconds()`:

```python
async def run_deep_pull(
    job_id: str,
    passes: list[dict[str, Any]] | None = None,
    *,
    fast_mode: bool = False,
) -> None:
    ...
```

At the inter-pass sleep site (~line 469), choose sleep duration based
on `fast_mode`:

```python
if fast_mode:
    sleep_s = 2.0 + random.uniform(0, 1.0)  # 2-3s
else:
    sleep_s = _jittered_pass_sleep_seconds()
```

**(c) Plumb `geojson` + `property_type_presets` through** — at the
`client.add_cma(...)` call (~line 437) and the `client.search_cma(...)`
call (~line 491), look up the polygon from `saved_area_id` (if present
on the job row) and pass as `geojson`. Read `property_type_presets`
from the active pass config and pass through.

The polygon-from-saved-area lookup needs a helper, e.g.
`_load_polygon_for_job(job_id) -> list[list[float]] | None` that reads
`saved_area_id` from `propelio_deep_pull_jobs` and joins to
`saved_areas.polygon`. Convert to geojson FeatureCollection same shape
as §4.4(c).

**(d) Wire `pass_preset` from request → run_deep_pull** — at the
endpoint at line 760 (already discussed in §4.4(b)):

```python
preset = (request.pass_preset or "").strip().lower()
if preset == "quick_sweep":
    passes = PASSES_QUICK_SWEEP
    fast_mode = True
else:
    passes = None  # default PASSES
    fast_mode = False
asyncio.create_task(run_deep_pull(job_id, passes=passes, fast_mode=fast_mode))
```

### 4.7 Status endpoint response shape

`GET /api/propelio/deep-pull/status/{job_id}` at ~line 794 — currently
returns `passes_completed`. Add a `passes_total` field so the frontend
banner can show "Pass 2/3" or "Pass 5/6" without hardcoding the divisor:

```python
return {
    ...,
    "passes_completed": ...,
    "passes_total": <length of the pass config in use>,
    ...,
}
```

This requires storing the pass config length on the job row OR
inferring it from the seed/pass config that was used. Simplest: add a
`passes_total INTEGER` column to `propelio_deep_pull_jobs` (or include
in a JSONB metadata column if one exists) populated at job-claim time.

(Copilot to evaluate: column add vs. inferring server-side vs. passing
through the response without storage. If column add is needed,
include the DDL migration in the spec response.)

## 5. New endpoint shapes (summary)

```
POST /api/propelio/refresh                           (existing)
  Body unchanged. Response now includes:
    ingestion_stats: { returned: N, new_to_cache: M }

POST /api/propelio/by-polygon                        (existing)
  Body unchanged. Response now includes:
    ingestion_stats: { returned: N, new_to_cache: M }

POST /api/propelio/deep-pull/start                   (existing)
  Body adds optional field:
    pass_preset: "quick_sweep" | null
  Response unchanged.

GET /api/propelio/deep-pull/status/{job_id}          (existing)
  Response adds:
    passes_total: int
```

## 6. Banner state matrix

| Trigger | Phase | Banner text |
|---|---|---|
| Custom Search click | starting | "Running custom search..." |
| Custom Search done, N>0, M>0 | done | "Returned {N} comps · {M} new to cache" |
| Custom Search done, N>0, M=0 | done | "Returned {N} comps · 0 new since last pull" |
| Custom Search done, N=0 | done | "0 comps returned for these filters" |
| Custom Search error | error | "Custom search failed: {reason}" |
| Get Comps click | starting | "Pass 0/3, quick sweep starting..." |
| Get Comps mid | running | "Pass {k}/3 done..." (uses passes_total from status response) |
| Get Comps done | done | "3 passes · {captured} captured · {netnew} net-new" |
| Get Comps error | error | "Quick sweep failed: see console" |

All non-running states auto-hide after 6s via existing
`setTimeout(_hideDeepPullBanner, 6000)` pattern.

## 7. Smoke test plan

**Smoke 1 — Custom Search basic:**
- Load 2451 Crest Ridge in preview
- Set months=1 in sidebar
- Click Custom Search
- Expected: banner shows "Returned 50-100 comps · X new to cache"
- Expected: page reflects new comps within 2-3 seconds

**Smoke 2 — Get Comps quick sweep:**
- Same lead
- Click gold Get Comps
- Expected: 3 passes complete in ~30-45s
- Expected: cumulative captured 150-300, net-new 30-80
- Expected: banner final says "3 passes · ..."

**Smoke 3 — Empty case:**
- Pick a low-velocity area (5528 Victor St, Dallas)
- Custom Search → banner says "0 comps returned for these filters"
- Get Comps → banner says "3 passes · 0 captured · 0 net-new"

**Smoke 4 — Targets scroller:**
- Save 10+ parcels to a workspace
- Open targets section
- Confirm scrollbar appears, list scrolls smoothly

**Smoke 5 — Geojson actually applied (server-side check):**
- After running Custom Search on a polygon, query the most recent
  comps in `propelio_comps` — verify all (or nearly all) are inside
  the saved polygon using `ST_Contains`. Stray out-of-polygon comps
  would suggest the geojson param isn't being honored.

## 8. Out-of-scope follow-ups (track for later)

- Add `propertyTypePresets` as a sidebar input on Custom Search.
- Make Get Comps configurable (1-pass vs 3-pass) via a preset selector.
- Add `passes_total` migration if needed (§4.7 is a question for Copilot).
- "Show comps within buffer of polygon" toggle (LL is strict, Propelio
  is fuzzy — separate UX decision).
- Marathon auto-restart + heartbeat alert (separate operational chunk).

## 9. Copilot critique gates (read carefully before coding)

Per [[feedback_copilot_iteration_loop]] — Copilot reviews this spec
and does NOT code yet. Surface:

- **Gaps:** anywhere the spec is ambiguous or missing detail.
- **Conflicts:** with existing code patterns (e.g., does
  `_jittered_pass_sleep_seconds` have a reason fast_mode would break?)
- **Edge cases:** what happens if `saved_area_id` is null when Get
  Comps is clicked? What if Custom Search fires while a Get Comps
  is mid-sweep?
- **Alternative approaches:** is there a simpler path I'm missing?
- **Risk:** does any change risk breaking marathon or the dev Deep
  Pull button?

After critique, Claude adjusts spec, Copilot gives go/no-go on the
adjusted version, then Copilot codes.
