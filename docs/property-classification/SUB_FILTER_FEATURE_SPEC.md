---
created: 2026-05-22
status: v2 — Copilot critique incorporated, parked pending client (Mike) validation
updated: 2026-05-22 (post-Copilot review + KK product calls)
---

# Property-Type Sub-Filter Dropdowns — Spec

## Changelog

- **v1 (initial draft):** Sub-filter dropdowns under 7 parent filters; per-state-code toggles + counts; single Denton labels dict; "all-subs-off = indeterminate parent."
- **v2 (THIS, post-Copilot deep-dive + KK product calls):**
  - **Scope corrected: 5 parents not 7.** Active (Redfin) and sold live in hidden / Legacy Filters card and are explicitly OUT of scope ("we do not use Redfin" per KK).
  - **Backend schema reshape:** Add `raw_state_code` (canonical, for filtering logic) + `state_label` (display) to all 4 county feature builders. Keep `state_code` for back-compat.
  - **Sub-filter key changed:** `(parent_bucket, source_county, raw_state_code)` tuple — county-namespaced from day one to handle A3-Collin-condo vs A3-Denton-SFR cleanly.
  - **Standard tri-state:** all-subs-off = parent UNCHECKED (clean), not indeterminate. Indeterminate ONLY for mixed.
  - **New-code init:** parent-effective (default ON if parent ON, OFF if parent OFF, OFF if parent mixed) — fixes the "leak on parent-off" risk.
  - **Filter-state schema versioned (v2)** with deterministic normalization for save/load/dirty-state.
  - **Memoized aggregation** keyed by viewport-bbox + filter-hash + data-revision — replaces the original "only when panel open" guard which Copilot showed was insufficient given multiple existing recompute paths.
  - **CSV export NOW IN SCOPE** per KK: "when you export stuff it should only be the actual snapshot of what it was filtered down to." Sub-filter state flows into the export pipeline.
  - **N/A normalization helper** for null/empty/N-A/UNKNOWN handling.
  - **DQ instrumentation added** for unknown-prop_type-routed-to-off_market + "(no code)" bucket size, surfaces silent data-quality regressions.
  - **A11y/mobile out of scope** (deferred — KK: "client needs to put more thought into it").
  - **Zero-count row visibility:** unresolved — listed as open item for client to decide.

## Problem

The 5 parent property-type filters in `frontend/index.html:169-200` (Off Market, Vacant, Multifamily, Commercial, Exempt) are all-or-nothing toggles. Two pain points:

1. **No selective filtering inside a parent bucket.** Mike's team can't filter "show duplexes but hide apartments" — both fall under the single `multifamily` checkbox.
2. **No visibility into what's in each bucket.** The cross-county classification logic (`api/counties/{dcad,tad,collin,denton}.py`) bundles different state codes into the same `prop_type`. Same parent bucket means different things across counties — e.g., Collin classifies A3 (condo) as multifamily while Denton classifies A3 as SFR. Team has no way to see this from the UI today.

Concrete usage example: Mike wants to hunt duplexes in Dallas without seeing apartments. Today, he must toggle `multifamily` ON (showing both duplexes AND apartments AND condos), or toggle it OFF (hiding all of them).

Background context for the broader classification cleanup work is in `docs/property-classification/README.md`. This spec is intentionally narrow — adds visibility and finer control without touching backend classification logic, deferring the larger taxonomy decisions until the team has used this feature.

## Goal

Under each of the 5 parent filters, surface a disclosable list of per-state-code sub-toggles. Each sub-toggle is keyed by `(parent_bucket, source_county, raw_state_code)`, shows a human-readable label, and has a live count of currently-visible features matching that triple. Sub-toggles can be independently checked/unchecked. CSV export respects the active sub-filter set.

After this ships:
- Team can selectively filter at the state-code level (e.g., "B12 • DCAD Duplexes ON, B11 • DCAD Apartments OFF" within the multifamily parent)
- Team can audit "what's in each bucket" per county by expanding the dropdown
- CSV export honors the sub-filter state (no surprise "exported things I'd hidden")
- No re-classification of state codes; no breaking schema changes; back-compat retained

## Non-goals

- Re-classifying any state code into a different parent bucket
- Cross-county harmonization (resolving A3 Collin/Denton, mobile-home placement, etc.)
- Sub-filters on `active` (Redfin) or `sold` — out of scope per KK
- New colors for sub-categories (visibility only)
- Persistent named filter presets ("Mike's flip view," "Audit view")
- Comp panel filtering — this spec is parcel-map only
- A11y / mobile / keyboard UX — deferred (KK: "client needs more thought")
- "Show only" mode

## Changes (5 files)

### 1. Backend: `api/counties/dcad.py` — add raw_state_code + state_label

Currently emits `"state_code": SPTD_LABELS.get(sptd_code, sptd_code)` at line 537 — i.e., the label OR raw code as fallback. This is overloaded.

Add to the feature build path:
```python
state_label = SPTD_LABELS.get(sptd_code, sptd_code)
# NEW: explicit fields for downstream consumers
"raw_state_code": sptd_code,           # canonical key for sub-filter
"state_label": state_label,            # explicit display label
"source_county": "dcad",               # county namespace for sub-filter triple
"state_code": state_label,             # KEEP for back-compat (current consumers)
```

Result: every DCAD feature carries `raw_state_code` (e.g., `"B12"`) + `state_label` (e.g., `"Duplexes"`) + `source_county` (`"dcad"`). Legacy `state_code` keeps current behavior.

### 2. Backend: `api/counties/tad.py` — same pattern

TAD currently emits `"state_code": state_label` at line 278. Apply parallel changes:

```python
raw_code = property_class or state_use_code  # use whichever is the canonical raw code
state_label = _TAD_SPTD_LABELS.get(raw_code, raw_code)  # use existing dict at line 35
"raw_state_code": raw_code,
"state_label": state_label,
"source_county": "tad",
"state_code": state_label,             # back-compat
```

### 3. Backend: `api/counties/collin.py` — same pattern

Collin currently uses `_clean_text(raw.get("state_cd_name")) or sptd_code` at line 293. Apply:

```python
raw_code = sptd_code  # canonical raw
state_label = _clean_text(raw.get("state_cd_name")) or raw_code
"raw_state_code": raw_code,
"state_label": state_label,
"source_county": "collin",
"state_code": state_label,             # back-compat
```

### 4. Backend: `api/counties/denton.py` — add labels dict + same pattern

Denton currently emits raw code only at line 278. Add a `_DENTON_SPTD_LABELS` dict mirroring DCAD's `SPTD_LABELS`:

```python
_DENTON_SPTD_LABELS = {
    "A1": "Single Family Residence",
    "A2": "Single Family with ADU",          # verify exact PTAD wording
    "A3": "Single Family on Acreage",        # confirm against actual data
    "A4": "Townhouse / Patio Home",
    "A5": "Garden Home",
    "A6": "Condominium",
    "B1": "Multifamily (2-4 units)",
    "B2": "Multifamily (5+ units)",
    "OA1": "...",                            # populate from observed data
    "OA5": "...",
}
```

Then:
```python
raw_code = sptd_code
state_label = _DENTON_SPTD_LABELS.get(raw_code, raw_code)
"raw_state_code": raw_code,
"state_label": state_label,
"source_county": "denton",
"state_code": state_label,             # back-compat — Denton now sends label too
```

**Note:** Denton's existing `state_code` was emitting raw codes; switching to label is technically a back-compat change. Consumers expecting raw codes from Denton would need to switch to `raw_state_code`. Audit recommended:
- `frontend/map.js` consumers of `state_code`
- Any saved-area / parcel-popup display logic
- CSV export columns

### 5. Frontend: `frontend/map.js` — filter UI, state, logic, export

**5a. Filter state schema (v2):**

Extend `filterState` (line 592) with version + canonical sub-filter map:

```js
filterState = {
  version: 2,                    // NEW — bump existing snapshots
  off_market: true,
  vacant: true,
  multifamily: true,
  commercial: true,
  exempt: true,

  // NEW: keyed by `${county}:${rawCode}` within each parent bucket
  // Example: subFilters.multifamily["dcad:B12"] = true
  subFilters: {
    off_market: {},
    vacant: {},
    multifamily: {},
    commercial: {},
    exempt: {},
  },
};
```

**Sub-filter key format:** `${source_county}:${raw_state_code}` — e.g., `"dcad:B12"`, `"collin:A3"`, `"denton:A3"`. Same code in two counties → two distinct keys → tracked independently. Solves the A3 Collin/Denton case.

**Lazy init (revised — "parent-effective"):**
- New code first seen → look at parent's checked state:
  - Parent fully checked (no other subs off) → new sub defaults ON
  - Parent fully unchecked → new sub defaults OFF
  - Parent mixed (indeterminate) → new sub defaults OFF (conservative; prevents leak)
- This prevents the "parent off + new code defaults true → user flips parent on → suddenly sees a new code they never agreed to see" failure mode.

**5b. Cascade rules (revised — standard tri-state):**

```
checked     → all subs ON
unchecked   → all subs OFF
indeterminate → some subs ON, some OFF (READ ONLY visual state, not user-clickable to set)
```

- Parent click toggles between checked ↔ unchecked. Clicking parent while indeterminate sets it to checked (cascades all subs ON).
- All subs OFF → parent visually flips to UNCHECKED (NOT indeterminate). This matches standard HTML checkbox semantics.
- All subs ON → parent visually CHECKED.
- Mixed → parent visually INDETERMINATE (uses `input.indeterminate = true`).
- Sub-click updates parent visual state.

**5c. N/A normalization helper:**

```js
function normalizeStateKey(raw) {
  if (raw == null) return "_NO_CODE_";
  const trimmed = String(raw).trim().toUpperCase();
  if (!trimmed || trimmed === "N/A" || trimmed === "UNKNOWN" || trimmed === "NONE") {
    return "_NO_CODE_";
  }
  return trimmed;
}
```

Display `_NO_CODE_` rows as `"(unknown code)"`. Sub-toggle works like any other.

**5d. Memoized count aggregation:**

```js
let _subCountCache = { key: null, value: null };

function getSubCounts(visibleFeatures, filterState, dataRevision) {
  const key = `${viewportHash}|${filterHash(filterState)}|${dataRevision}`;
  if (_subCountCache.key === key) return _subCountCache.value;

  const subCounts = { off_market:{}, vacant:{}, multifamily:{}, commercial:{}, exempt:{} };
  for (const f of visibleFeatures) {
    const bucket = classifyFeatureForFilter(f);
    if (!(bucket in subCounts)) continue;
    const county = f.properties.source_county || "unknown";
    const code = normalizeStateKey(f.properties.raw_state_code);
    const subKey = `${county}:${code}`;
    subCounts[bucket][subKey] = (subCounts[bucket][subKey] || 0) + 1;
  }
  _subCountCache = { key, value: subCounts };
  return subCounts;
}
```

Invalidation: cache resets on viewport change, filter change, or data refresh. The 3 existing recompute paths Copilot identified (`map.js:1292, 5933, 7310`) all go through this getter; cache hit-rate should be near-100% within a single user interaction.

**5e. Filter application:**

```js
function isFeatureVisible(feature) {
  const bucket = classifyFeatureForFilter(feature);
  if (!(bucket in filterState)) return true;  // unhandled bucket → show

  const county = feature.properties.source_county || "unknown";
  const code = normalizeStateKey(feature.properties.raw_state_code);
  const subKey = `${county}:${code}`;
  const subVal = filterState.subFilters[bucket]?.[subKey];

  if (subVal !== undefined) return subVal;  // explicit sub-toggle wins

  // Lazy-init for newly observed code
  const parentFullyOn  = isParentFullyOn(bucket);
  const parentFullyOff = isParentFullyOff(bucket);
  const defaultVal = parentFullyOn ? true : false;  // conservative on mixed
  filterState.subFilters[bucket][subKey] = defaultVal;
  return defaultVal;
}
```

**5f. UI rendering:**

In the filter card (`index.html:169-200`), each `<label class="filter-row">` gets a sibling disclosure container:

```html
<label class="filter-row">
  <button class="sub-filter-disclosure" data-bucket="multifamily" aria-expanded="false">▶</button>
  <span class="filter-swatch swatch-multifamily"></span>
  <span class="filter-name">Multifamily</span>
  <span class="filter-count" id="filter-count-multifamily">0</span>
  <input type="checkbox" id="filter-multifamily">
</label>
<div class="sub-filter-list hidden" id="sub-filter-list-multifamily">
  <!-- populated dynamically: one .sub-filter-row per (county, code) currently in view -->
</div>
```

Sub-row template:
```
[✓] B12 • DCAD — Duplexes (824)
```

Format: `[checkbox] {raw_code} • {county_token} — {label} ({count})`.

**5g. CSV export integration:**

CSV export currently filters by parent bucket only. Extend the export pipeline to also apply `isFeatureVisible(feature)` per-row before writing. KK explicit requirement: "when you export stuff it should only be the actual snapshot of what it was filtered down to."

**5h. Saved-area snapshot compatibility:**

`captureFilterState` (line 1380) and `restoreFilterState` (line 1745) need updates:

- On capture: include `version: 2` + full `subFilters` map (with `_NO_CODE_` keys normalized)
- On restore from v1 snapshot (no version key, no subFilters): treat as "v2 with all subs default-effective" — apply lazy-init pattern on first feature visibility check
- On restore from v2: validate `subFilters` structure, normalize keys, apply
- Dirty-state compare (`_filterStatesEqual` line 1455) deeply compares the normalized `subFilters` maps

**5i. DQ instrumentation (lightweight):**

Add two debug counters surfaced to console + saved-area DQ panel:
- `unknown_prop_type_count` — features with `prop_type` not in handled set (currently silently route to `off_market`)
- `no_code_count` per bucket — count of features bucketed under `_NO_CODE_`

Surface as a small "data quality" badge near the filter list. Doesn't change behavior, just makes silent issues visible.

## Sequencing

1. Backend changes (1-4) ship first — additive, no consumer-side breakage if they ignore the new fields. Denton back-compat: audit + update consumers expecting raw code before flipping the legacy `state_code` field to label.
2. Frontend changes (5a-5i) ship together in a single PR — sub-filter state, visibility logic, UI, export, snapshot compat are interdependent.

## Verification plan

### Backend checks
1. Hit each county's parcel endpoint; verify response includes `raw_state_code`, `state_label`, `source_county` on every feature.
2. Spot-check 5 known parcels per county: codes match expected (DCAD B12 → Duplexes, etc.).
3. Confirm `state_code` legacy field still emits a label everywhere (Denton's behavior change).

### Frontend visual checks (preview deploy)
4. Each of the 5 parent filter rows has a chevron + checkbox + count.
5. Expand `multifamily` in Dallas-only view → sees DCAD codes (`B11 • DCAD — Apartments (N)`, `B12 • DCAD — Duplexes (M)`, `A13 • DCAD — Condominiums (K)`, `A14 • DCAD — (unlabeled) (X)`).
6. Expand `multifamily` in Collin-only view → sees Collin codes (`A3 • Collin — Condos (N)`, `B1 • Collin — MF 2-4 (M)`, ...).
7. Expand `multifamily` in Denton-only view → sees Denton codes with proper labels via new dict.
8. Pan to span Dallas + Collin → both counties' codes appear, namespaced by `• DCAD` / `• Collin`.
9. Empty bucket → chevron disabled, list shows nothing.

### Behavior checks
10. Toggle off `B11 • DCAD — Apartments` → apartment parcels disappear. Parent `multifamily` visually flips to INDETERMINATE.
11. Toggle parent `multifamily` OFF → all subs flip OFF. Parent shows UNCHECKED.
12. Toggle parent `multifamily` ON → all subs flip ON. Parent shows CHECKED.
13. Toggle off all subs one-by-one → parent transitions through INDETERMINATE to UNCHECKED when the last one is toggled off.
14. Parent OFF, lazy-init: pan to area with a new code → new sub-row appears already OFF (parent-effective init).
15. Counts: zoom in/out, counts update. Counts always = visible feature count per (county, code).

### Persistence
16. Toggle subs, reload → restored from localStorage.
17. Save area with non-default subFilters → load in new session → state restored.
18. Load a v1 saved area (no subFilters) → no errors; lazy-init kicks in as features render.

### CSV export
19. Toggle off `B11 • DCAD — Apartments`. Export CSV. Apartments NOT in the CSV. Duplexes ARE.
20. Toggle parent `multifamily` OFF. Export CSV. No multifamily rows.
21. Sub-filter state via shareable URL (if URL state implemented; otherwise document as known gap).

### Cross-county audit (implicit goal)
22. Visit each county individually, expand each parent. Confirm classification matches our reference table:
    - DCAD MF: B11, B12, A13, A14
    - TAD MF: B1, B2, B3, B4, M1, M2 (note mobile homes — worth surfacing as DQ flag)
    - Collin MF: A3, B1, B2, B3, B4, B6, B9
    - Denton MF: B1, B2 only (and Denton A3 appears in SFR, NOT MF)

### DQ
23. Confirm "unknown prop_type" + "(no code)" counters render. Verify counts are zero or near-zero in production data.

## Risk

| Risk | Severity | Mitigation |
|---|---|---|
| Denton `state_code` legacy field changes from raw code to label — possible consumer breakage | High | Audit map.js + popup + CSV consumers BEFORE flipping; switch them to `raw_state_code` where they were depending on raw codes |
| Memoized aggregation cache miss-storms on rapid viewport changes | Medium | Cache key includes viewport bbox at coarse resolution; bbox tolerance ~tile-size to avoid sub-pixel invalidations |
| Long sub-lists (Collin MF has 7+ codes) | Low | Scrollable container with max-height |
| Saved v1 snapshots interpret-as-all-on may surprise users who saved with parent OFF then this feature ships | Low | v1 snapshots keep parent state; lazy-init only fires when parent ON and feature is visible — net effect: same as before |
| New code lazy-init may default OFF when parent indeterminate → user misses new parcels | Medium | Surface a small "(N new codes hidden — click here)" notice when lazy-init defaults something to OFF |
| CSV export pipeline currently does its own filtering — may diverge from `isFeatureVisible` | Medium | Centralize visibility logic in one function; both map render and export call it |
| URL state for shareable links | Low | Document as known gap; sub-filter state NOT in URL for v2 |

## Rollback

Backend changes are additive — safe to leave the new fields even if the frontend feature is reverted. Frontend revert removes the UI; localStorage `subFilters` keys become stale but ignored. Denton `state_code` legacy-field switch is the one breaking change; rolling back requires reverting that emit logic if downstream consumers were updated.

## Out of scope

- Re-classifying any state code
- Cross-county harmonization (A3, mobile homes, etc.)
- Comp panel filtering integration
- A11y / mobile / keyboard navigation (deferred)
- Persistent named filter presets
- "Show only" mode
- Bulk select / "uncheck all duplexes everywhere"
- URL state / shareable filter links
- Sub-filter for active (Redfin) or sold buckets — not used by Mike per KK

## Open items pending client validation

1. **Zero-count row visibility:** when a user explicitly toggled a sub-row OFF and then pans to an area where that code has zero features, should the row (a) stay visible in disabled state to preserve user intent, or (b) disappear and reappear on return? Copilot leans (a); KK marked as "not sure yet."
2. **CSV export header columns:** should the export include `raw_state_code` + `state_label` + `source_county` columns? Currently spec assumes yes (since they're already in the feature payload), but worth Mike-confirming.
3. **DQ counter visibility:** surface to all users always, or behind a power-user toggle? Spec defaults to always-visible.
4. **Denton labels dict completeness:** spec lists a starter set; needs full audit against actual Denton data to catch all codes in use.

## Implementation effort estimate (revised v2)

- Backend changes (4 county files + audit + back-compat tests): 0.75 day
- Frontend filter state v2 schema + lazy-init + cascade: 0.5 day
- Frontend UI (chevron, sub-rows, count display, indeterminate state): 0.5 day
- Memoized aggregation + cache invalidation: 0.5 day
- CSV export pipeline integration: 0.25 day
- Saved-area snapshot v1→v2 compat + dirty-state: 0.25 day
- DQ instrumentation: 0.25 day
- Verification + preview deploy + iteration: 0.5 day
- **Total: ~3.5 days** (up from v1's 2-3 day estimate due to export integration + schema versioning)

## Status

**Parked pending Mike's input on the open items.** When Mike has weighed in, this spec is ready to hand to Copilot for implementation. No code work until then.
