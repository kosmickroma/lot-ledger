---
created: 2026-05-07
type: copilot-diagnostic
status: ready-for-review
branch: feat/share-link-fidelity
topic: share-link-fidelity
---

# Share Link Fidelity Diagnostic

## Scope audited
- Share-link flow: [frontend/map.js](frontend/map.js#L5437) -> [frontend/map.js](frontend/map.js#L5451) -> [frontend/map.js](frontend/map.js#L1690)
- Filter state round-trip: [frontend/map.js](frontend/map.js#L472) and [frontend/map.js](frontend/map.js#L595)
- Saved area API read/write paths: [api/main.py](api/main.py#L3343), [api/main.py](api/main.py#L3377), [api/main.py](api/main.py#L3493), [api/main.py](api/main.py#L3535)

## 1) Audit table: renderFeatures and runAnalysis call sites

### A. renderFeatures call sites

| Line | Call site | State before render | State after render | Missing or risk |
|---|---|---|---|---|
| [623](frontend/map.js#L623) | restoreFilterState path | Calls applyAndRenderSoldFilters first when lastAnalysisGeojson exists | renderFeatures then renderSidebar | Missing explicit applyMapVisibilityFilters after render. Can leave parcelTypeLayers attached state stale after a restore-driven rerender. |
| [909](frontend/map.js#L909) | applyAndRenderSoldFilters internals | sold filter recalculated, sold markers redrawn | renderFeatures called directly | No applyMapVisibilityFilters here by design to avoid recursion. Safe internal call. |
| [1624](frontend/map.js#L1624) | undo restore path | Calls applyAndRenderSoldFilters first | renderFeatures then renderSidebar | Same risk as restoreFilterState path: no post-render map-visibility reconciliation. |
| [1867](frontend/map.js#L1867) | restoreSavedArea normal load | runAnalysis done, sold state set, applyAndRenderSoldFilters called | renderFeatures then renderSidebar | Critical gap for share-load fidelity: no explicit post-render applyAndRenderSoldFilters + applyMapVisibilityFilters ordering after final render pass. |
| [1996](frontend/map.js#L1996) | restoreNamedSession load | runAnalysis/cached session load, applyAndRenderSoldFilters called | renderFeatures then renderSidebar | Same gap pattern as restoreSavedArea for parity/safety. |
| [2869](frontend/map.js#L2869) | applyMapVisibilityFilters sold-toggle rerender | applyAndRenderSoldFilters already called inside same function | renderFeatures then renderSidebar | Safe. This path is the manual checkbox-toggle workaround users observed. |
| [2885](frontend/map.js#L2885) | numeric filter apply path | numeric filters read and applied | renderFeatures then renderSidebar | Generally safe because this path is driven by direct UI events and uses current checkbox state. |
| [3613](frontend/map.js#L3613) | renderViewportFeatures | viewport subset built | renderFeatures only | Intended lightweight rerender; no sidebar/count update. Not a share-link root cause. |
| [4318](frontend/map.js#L4318) | draw created analysis | runAnalysis complete, applyAndRenderSoldFilters called | renderFeatures then renderSidebar | Same post-render reconciliation gap, lower impact because normal draw usually followed by manual interaction. |
| [4704](frontend/map.js#L4704) | rerunWithRedfin | runAnalysis complete, applyAndRenderSoldFilters called | renderFeatures then renderSidebar | Same gap pattern. |
| [4747](frontend/map.js#L4747) | toggle-redfin rerender | toggle state changed | renderFeatures then renderSidebar | Missing post-render applyMapVisibilityFilters; can desync parcel layer visibility from restored checkbox state in edge transitions. |
| [4784](frontend/map.js#L4784) | rerunWithSold | runAnalysis complete, applyAndRenderSoldFilters called | renderFeatures then renderSidebar | Same gap pattern. |

### B. runAnalysis call sites

| Line | Call site | include_sold source | Post-analysis sequencing | Missing or risk |
|---|---|---|---|---|
| [1814](frontend/map.js#L1814) | restoreSavedArea | Boolean(filterState.sold) after restoreFilterState | sold points assigned, applyAndRenderSoldFilters, then final renderFeatures | Final pass misses explicit reconciliation order after final render. |
| [1943](frontend/map.js#L1943) | restoreNamedSession | Boolean(filterState.sold) after restoreFilterState | same pattern as restoreSavedArea | Same gap. |
| [4135](frontend/map.js#L4135) | refreshExpiredJob | Boolean(filterState.sold) | no immediate render path here | Not primary share-link gap. |
| [4258](frontend/map.js#L4258) | draw created | Boolean(filterState.sold) | applyAndRenderSoldFilters then renderFeatures | Same post-render reconciliation gap. |
| [4672](frontend/map.js#L4672) | rerunWithRedfin | Boolean(toggle-sold checked) | applyAndRenderSoldFilters then renderFeatures | Same gap pattern. |
| [4763](frontend/map.js#L4763) | rerunWithSold | forced true | applyAndRenderSoldFilters then renderFeatures | Same gap pattern. |

## 2) Root causes for the 3 reported bugs

### Bug 1: off-market parcels not visible until checkbox toggle

Confirmed root cause:
- In share-load path, restoreFilterState executes before new analysis results exist: [frontend/map.js](frontend/map.js#L1764).
- restoreFilterState only runs visibility rerender branch when lastAnalysisGeojson is truthy: [frontend/map.js](frontend/map.js#L622).
- Later, restoreSavedArea runs analysis and calls renderFeatures: [frontend/map.js](frontend/map.js#L1867).
- No applyMapVisibilityFilters call follows that final render pass.

Why counts still show 85:
- Sidebar counts are derived from feature data and filter predicates, not from whether the per-type Leaflet layer is currently attached under markerLayer in that moment.
- Manual toggle triggers applyMapVisibilityFilters at [frontend/map.js](frontend/map.js#L2935), which attaches/removes the parcel-type layers and immediately fixes visibility.

### Bug 2: sold count mismatch sender vs recipient

Primary confirmed cause in current code:
- Share-load does not guarantee a final ordered sold/map reapply after final renderFeatures in restoreSavedArea.
- Existing sequence is applyAndRenderSoldFilters then renderFeatures then renderSidebar ([frontend/map.js](frontend/map.js#L1850), [frontend/map.js](frontend/map.js#L1867)).
- There is no final post-render consistency pass to guarantee sold marker set and sold-linked parcel styling are in sync with the restored filter state snapshot.

What is not the cause (confirmed):
- Sold filter persistence round-trip exists in client save payload and restore:
  - capture includes sold object: [frontend/map.js](frontend/map.js#L472)
  - restore hydrates sold object: [frontend/map.js](frontend/map.js#L605)
  - save/update sends full filter_state from captureFilterState: [frontend/map.js](frontend/map.js#L1393), [frontend/map.js](frontend/map.js#L1348)
  - backend stores request.filter_state JSONB as-is: [api/main.py](api/main.py#L3402), [api/main.py](api/main.py#L3552)

Residual uncertainty to verify in patch phase:
- Whether any backend analyze merge/caching path yields nondeterministic sold_points for identical polygon/time window. No direct evidence found in this audit, but worth instrumenting once fix is applied.

### Bug 3: Update button missing on deep-linked workspaces (and after fork in some user reports)

Confirmed root cause:
- _updateUpdateAreaButtonVisibility only consults _savedAreasCache for current area id: [frontend/map.js](frontend/map.js#L2164).
- Deep-linked area is loaded via by-share-id and passed to restoreSavedArea, but never added to _savedAreasCache: [frontend/map.js](frontend/map.js#L5451).
- Therefore deep-linked area cannot satisfy the Update-button lookup and remains hidden.

Related side-effect:
- Current-view banner helper also only looks in _savedAreasCache and will clear _currentLoadedAreaId when not found: [frontend/map.js](frontend/map.js#L521) to [frontend/map.js](frontend/map.js#L527).
- This means deep-linked state is not represented as a first-class loaded area in UI state unless separately modeled.

## 3) Proposed minimum-diff fix per bug

### Fix for Bug 1 (parcel visibility mismatch after share-load)
- In restoreSavedArea, after the final renderFeatures/renderSidebar block, run:
  1) applyAndRenderSoldFilters
  2) applyMapVisibilityFilters
- Keep this ordering exactly so sold state settles first, then parcel-type layer attachment aligns to restored checkbox state.

### Fix for Bug 2 (sold count/render mismatch)
- Same post-render ordered reapply in restoreSavedArea closes the most likely desync.
- For parity and future regression prevention, apply same pattern to restoreNamedSession and other runAnalysis->renderFeatures paths that represent full-state restores.
- Add temporary debug logging around sold counts in restoreSavedArea:
  - allSoldPointsRef length
  - lastSoldPanelPoints length
  - soldCompsFilter snapshot
  - filterState.sold boolean
  This isolates any backend nondeterminism if mismatch persists after UI sequencing fix.

### Fix for Bug 3 (Update button missing in deep-link context)
- Recommended: Option B (see decision section).
- Add separate side-state for currently loaded deep-linked area that is not in user cache.
- Update button logic checks:
  - cached own area with drift: show
  - deep-linked non-owned area: hide
- Keep Save Area fork flow unchanged; once forked and selected from own cache, Update naturally works with current logic.

## 4) Decision needed: Option A vs B for deep-link area state

### Option A (add deep-linked area into _savedAreasCache)
Pros:
- Reuses existing update-button and current-view logic immediately.
Cons:
- Sidebar now contains workspaces user did not save, creating ownership confusion and clutter.

### Option B (recommended)
Pros:
- Preserves semantic meaning of saved list as user-owned artifacts.
- Allows explicit behavior for borrowed deep links (view-only until fork).
- Cleanly aligns with creator-scoped writes on backend update route: [api/main.py](api/main.py#L3572).
Cons:
- Requires one additional UI state object and small branching in update/current-view helpers.

Recommendation:
- Choose Option B unless product explicitly wants “auto-pin shared links into my saved list”.

## 5) Additional issues found beyond the 3 reported bugs

1. Ownership context missing from share-link payload
- by-share-id response omits user_id: [api/main.py](api/main.py#L3493).
- Frontend ownership-aware controls cannot make precise own vs non-own decisions for deep links without it.
- This becomes more important with role-tier UI gating.

2. Restore paths inconsistently reapply map/sold visibility after final render
- restoreSavedArea and restoreNamedSession have same structural risk.
- Numeric/undo/filter restore paths also rerender without a guaranteed final explicit visibility reconciliation step.

3. Current-view banner is cache-coupled
- _renderCurrentViewingArea clears _currentLoadedAreaId when id is absent from cache ([frontend/map.js](frontend/map.js#L525)).
- Deep-linked loaded state should not be invalidated solely because it is not user-saved.

## 6) Suggested validation checklist after patch phase

- Share link opened by non-owner shows identical:
  - checkbox states
  - numeric inputs
  - sold filter inputs
  - map parcel visibility by type
  - sold panel count
  - sold marker dot count
- Off-market toggle workaround no longer needed.
- Deep-linked non-owned area keeps current-view context but Update remains hidden.
- After Save Area fork, new owned area can show Update when filters drift.
- Sender and recipient sold counts match for same share_id and same saved filter_state.
