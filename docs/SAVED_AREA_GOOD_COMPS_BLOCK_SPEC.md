---
created: 2026-05-23
status: v2 — Copilot deep-dive + KK product calls incorporated; ready for implementation
updated: 2026-05-23
---

# Saved-Area Good Comps Block — Spec

## Changelog

- **v1 (initial draft):** Rich-card good-comps section between Subject card and Comps list, click-to-pan + Remove + Bad Comp actions, reuses comp_ratings. Pre-Copilot critique.
- **v2 (THIS, post-Copilot deep-dive + KK product calls):**
  - **Rating-change event bus.** v1 assumed real-time refresh via "listen to rating mutations" but no event was emitted. v2: emit `comp-rating-changed` custom event from canonical mutation point (`map.js:5061`) and listen from the new section. Fallback: also rerender from `map.js:4867` (existing filter/apply path).
  - **Remove = explicit `null` rating** via the existing `routes.py:1101` endpoint → `archive.py:240` DELETE. v1's "click-active-Good-to-clear" assumption was wrong — that behavior doesn't exist in current code.
  - **Comp list rows don't have rating buttons.** v1 incorrectly assumed Good buttons live in row HTML. They actually live in popup/panel HTML (`map.js:3944, 4314, 6323, 6670`). Real-time refresh hooks the popup rating handler at `map.js:5083`, not list rows.
  - **Shared display formatters extracted** from `map.js:883` (Subject card) + `map.js:4938` (comp list row) into small helpers; new card reuses them. Avoids parcel-vs-comp shape mismatch.
  - **Centralized workspace-changed hook.** Multiple area-id assignment paths exist (`map.js:2181, 3084, 3417, 7900, 8074, 9318`) — v2 introduces `_onWorkspaceChanged(newAreaId, oldAreaId)` helper and calls it from ALL sites. Good Comps reset/hydrate runs through this helper, fixing the fork-bypass-restore gap.
  - **Filter independence locked per KK:** Good Comps section always shows ALL good-rated comps in the workspace, ignoring active filter chips. Filters hunt for new comps; the Good Comps list is a curated bookmark.
  - **Optimistic UI:** comp leaves the good list immediately on Remove/Bad click; server-confirms in background; reverts on failure.
  - **A11y v1:** tabindex + Enter/Space row activation, aria-labels on Remove/Bad buttons, aria-live count region. Advanced keyboard nav (arrow keys, Shift+select) deferred.
  - **Touch targets:** 40-44px hit area on Remove + Bad buttons via padding (not visible button size).
  - **Naming:** `.propelio-good-comp-card` + `#propelio-good-comps-section` to match existing `propelio-*` namespace.
  - **Performance:** full-replace render is current pattern; acceptable for ~30-row good lists. Memoization deferred to V2 if churn becomes noticeable.

## Problem

Mike's team manually rates comps as Good or Bad inside each saved area. The good ratings persist correctly (`comp_ratings` table, FK to `saved_areas.area_id`) and influence map-side visualization (`_maybeAddGoodCompMark`), but the team has no consolidated VIEW of which comps they've already marked Good in the current workspace. Reviewing one's own decisions means scrolling the full Comps List and visually scanning for highlighted rows — slow and error-prone.

The client wants a dedicated, always-visible Good Comps block per saved area: a curated bookmark list of comps the team has tagged Good. Ships with the saved area (already does via `comp_ratings.workspace_id`). Lets the team quickly review, re-examine, or revoke individual comp decisions.

## Goal

Add a new sidebar section `#propelio-good-comps-section` inside `#comps-list-block-body`, between `#comps-block-target-row` (Subject card) and `.propelio-comp-list-section` (main comps list). The new section:

1. Renders one rich card per comp where `comp_ratings.rating = 'good'` for the current `workspace_id`.
2. Each card mirrors the Subject Property card's visual structure (price, addr, neighborhood, 3 meta lines) for visual consistency.
3. Clicking the card body pans/zooms the map to that comp + highlights it (reuses existing comp-row click handler).
4. Each card has two small action buttons:
   - **Remove** (`×`): clears the rating (DELETE comp_ratings row). Comp returns to neutral.
   - **Bad Comp**: flips the rating from `good` → `bad`. Comp leaves this list AND picks up existing bad-comp visual treatment on the map.
5. Section header: "Good Comps (N)" with live count.
6. Empty state: muted instructional text when zero good comps.
7. Always shows ALL good-rated comps in the workspace, regardless of active filter chips.
8. Updates in real time when ratings change anywhere (via `comp-rating-changed` event).
9. Bonded to saved area automatically via existing `workspace_id` persistence.
10. Hidden when no saved area is loaded (same visibility logic as `#comps-block-target-row`).

## Non-goals

- New backend persistence (reuses `comp_ratings`)
- New API endpoints (reuses `routes.py:1101` for rate set/clear)
- Schema changes
- Sort: section uses parent Comps List's existing sort dropdown — no separate sort control
- Filter chips integration (intentionally ignored — per KK)
- Advanced keyboard nav (arrow keys, Shift+select multi-action) — defer to v2
- Bulk actions (clear all good comps, mass-flip) — defer
- Photos / external Propelio listing link from inside the card — defer
- Diff badges or change-log per comp — defer

## Changes (3 files)

### 1. `frontend/index.html` — new section markup

Inside `#comps-list-block-body`, between `#comps-block-target-row` and `.propelio-comp-list-section`:

```html
<section id="propelio-good-comps-section" class="propelio-good-comps-section hidden" aria-label="Good comps in this saved area">
  <header class="propelio-good-comps-head">
    <span class="propelio-section-label">Good Comps</span>
    <span class="propelio-good-comps-count" id="propelio-good-comps-count" aria-live="polite">0</span>
  </header>
  <div id="propelio-good-comps-list" class="propelio-good-comps-list">
    <!-- populated by JS -->
  </div>
  <p class="propelio-good-comps-empty hidden" id="propelio-good-comps-empty">
    No comps marked Good yet. Open a comp's detail panel and click <strong>Good</strong> to add it here.
  </p>
</section>
```

Wrapper has `.hidden` class on load; JS toggles based on saved-area state.

### 2. `frontend/style.css` — section + card styling

```css
/* Good Comps section — appears between Subject card and main comp list
   inside #comps-list-block-body. Always-visible mini-section (not its
   own collapsible). */
.propelio-good-comps-section {
  padding: 8px 0 4px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  margin-bottom: 8px;
}

.propelio-good-comps-section.hidden { display: none; }

.propelio-good-comps-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.propelio-good-comps-head .propelio-section-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-soft);
  font-weight: 700;
}

.propelio-good-comps-count {
  font-size: 11px;
  font-weight: 700;
  color: var(--gold);
  background: rgba(201, 162, 79, 0.12);
  border: 1px solid rgba(201, 162, 79, 0.3);
  border-radius: 10px;
  padding: 1px 8px;
  min-width: 18px;
  text-align: center;
}

.propelio-good-comps-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.propelio-good-comps-empty {
  font-size: 12px;
  color: var(--text-soft);
  line-height: 1.4;
  padding: 8px 4px;
  margin: 0;
}

/* Good Comp card — mirrors .comps-block-target-card visual structure
   (gold left border, dark green background, rich data layout). */
.propelio-good-comp-card {
  position: relative;
  background: linear-gradient(180deg, rgba(13, 55, 42, 0.7), rgba(10, 40, 30, 0.85));
  border-left: 3px solid var(--gold);
  border-radius: 4px;
  padding: 8px 10px 8px 12px;
  cursor: pointer;
  transition: background 0.12s ease, transform 0.08s ease;
}

.propelio-good-comp-card:hover {
  background: linear-gradient(180deg, rgba(13, 55, 42, 0.9), rgba(10, 40, 30, 0.95));
}

.propelio-good-comp-card:focus-visible {
  outline: 2px solid var(--gold);
  outline-offset: 1px;
}

.propelio-good-comp-card-top {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.propelio-good-comp-card-price {
  font-size: 14px;
  font-weight: 700;
  color: #00d2c5;  /* matches subject card price color */
  flex: 1;
}

.propelio-good-comp-card-badge {
  font-size: 9px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--gold);
  background: rgba(201, 162, 79, 0.15);
  border: 1px solid rgba(201, 162, 79, 0.4);
  border-radius: 3px;
  padding: 2px 6px;
}

.propelio-good-comp-card-actions {
  display: flex;
  gap: 4px;
  align-items: center;
}

/* Touch-friendly hit area without visually-large buttons */
.propelio-good-comp-action-btn {
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-soft);
  font-size: 11px;
  padding: 8px 10px;  /* 40px+ hit area via vertical padding */
  border-radius: 3px;
  cursor: pointer;
  line-height: 1;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}

.propelio-good-comp-action-btn:hover {
  border-color: rgba(255, 255, 255, 0.15);
}

.propelio-good-comp-action-btn.remove {
  font-weight: 700;
}

.propelio-good-comp-action-btn.remove:hover {
  color: #f5e9c8;
  background: rgba(255, 255, 255, 0.08);
}

.propelio-good-comp-action-btn.bad {
  color: #ff8a92;
  border-color: rgba(224, 36, 50, 0.4);
}

.propelio-good-comp-action-btn.bad:hover {
  background: rgba(224, 36, 50, 0.18);
  border-color: rgba(224, 36, 50, 0.9);
}

.propelio-good-comp-card-addr {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-main);
  margin-bottom: 2px;
  word-break: break-word;
}

.propelio-good-comp-card-nbhd {
  font-size: 11px;
  font-style: italic;
  color: var(--text-soft);
  margin-bottom: 4px;
}

.propelio-good-comp-card-meta,
.propelio-good-comp-card-meta2,
.propelio-good-comp-card-meta3 {
  font-size: 11px;
  color: var(--text-soft);
  line-height: 1.4;
}

@media (pointer: coarse) {
  .propelio-good-comp-action-btn {
    padding: 12px 14px;  /* slightly larger hit area on touch */
  }
}
```

### 3. `frontend/map.js` — render, state, hooks

**Add shared display formatters** (extracted from existing subject + comp render paths, callable by both):

```js
// Shared formatters — extracted so the Subject card, regular comp rows,
// and the new good-comp cards stay visually consistent without duplicating
// field-mapping logic. Existing renderers (map.js:883 subject, map.js:4938
// comp row) call these too.
function _formatCompPriceLine(comp) { /* returns "$425,000" or "—" */ }
function _formatCompAddrLine(comp) { /* returns "1234 STREET, CITY" */ }
function _formatCompNeighborhood(comp) { /* italic neighborhood or "" */ }
function _formatCompMetaLine1(comp) { /* sqft · beds/baths · year built */ }
function _formatCompMetaLine2(comp) { /* sold date · DOM · status */ }
function _formatCompMetaLine3(comp, subject) { /* distance · price/sqft */ }
```

**Render function:**
```js
function _renderGoodCompsSection() {
  const section = document.getElementById("propelio-good-comps-section");
  const list = document.getElementById("propelio-good-comps-list");
  const countEl = document.getElementById("propelio-good-comps-count");
  const emptyEl = document.getElementById("propelio-good-comps-empty");
  if (!section || !list) return;

  // Visibility: only when a saved area is loaded
  if (!_currentLoadedAreaId) {
    section.classList.add("hidden");
    return;
  }
  section.classList.remove("hidden");

  // Source: ALL comps in the workspace with user_rating === "good".
  // Ignores active filter chips (per KK product call).
  const goodComps = (allPropelioComps || []).filter(c => c.user_rating === "good");

  // Sort: use the existing Comps List sort selection (single source of truth)
  const sortedGoodComps = _sortComps(goodComps, _currentCompSortKey);

  countEl.textContent = String(sortedGoodComps.length);
  if (sortedGoodComps.length === 0) {
    list.innerHTML = "";
    emptyEl.classList.remove("hidden");
    return;
  }
  emptyEl.classList.add("hidden");

  list.innerHTML = sortedGoodComps.map(comp => _renderGoodCompCard(comp)).join("");
  _wireGoodCompCardHandlers(list);
}

function _renderGoodCompCard(comp) {
  const compKey = _esc(comp.comp_address_key || "");
  return `
    <article class="propelio-good-comp-card" tabindex="0" data-comp-key="${compKey}" role="button" aria-label="Good comp: ${_esc(comp.address || "")}. Press Enter to focus on map.">
      <div class="propelio-good-comp-card-top">
        <span class="propelio-good-comp-card-price">${_formatCompPriceLine(comp)}</span>
        <span class="propelio-good-comp-card-badge">Good</span>
        <div class="propelio-good-comp-card-actions">
          <button type="button" class="propelio-good-comp-action-btn remove" data-action="remove" data-comp-key="${compKey}" aria-label="Remove Good rating from this comp">×</button>
          <button type="button" class="propelio-good-comp-action-btn bad" data-action="bad" data-comp-key="${compKey}" aria-label="Change rating to Bad">Bad</button>
        </div>
      </div>
      <div class="propelio-good-comp-card-addr">${_formatCompAddrLine(comp)}</div>
      <div class="propelio-good-comp-card-nbhd">${_formatCompNeighborhood(comp)}</div>
      <div class="propelio-good-comp-card-meta">${_formatCompMetaLine1(comp)}</div>
      <div class="propelio-good-comp-card-meta2">${_formatCompMetaLine2(comp)}</div>
      <div class="propelio-good-comp-card-meta3">${_formatCompMetaLine3(comp, _currentSubjectComp)}</div>
    </article>`;
}
```

**Handlers + optimistic UI:**
```js
function _wireGoodCompCardHandlers(listEl) {
  listEl.querySelectorAll(".propelio-good-comp-card").forEach(card => {
    card.addEventListener("click", (ev) => {
      const actionEl = ev.target.closest("[data-action]");
      if (actionEl) {
        ev.stopPropagation();
        const compKey = actionEl.dataset.compKey;
        if (actionEl.dataset.action === "remove") {
          _setCompRatingOptimistic(compKey, null);
        } else if (actionEl.dataset.action === "bad") {
          _setCompRatingOptimistic(compKey, "bad");
        }
        return;
      }
      // Click row body → pan/zoom map to comp + highlight.
      // Reuses existing comp-row click handler at map.js:4990.
      _focusCompOnMap(card.dataset.compKey);
    });
    card.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        _focusCompOnMap(card.dataset.compKey);
      }
    });
  });
}

// Optimistic rating mutation: update local state immediately, dispatch
// rating-change event, persist to server, revert on failure.
async function _setCompRatingOptimistic(compKey, newRating) {
  const comp = (allPropelioComps || []).find(c => c.comp_address_key === compKey);
  if (!comp) return;
  const oldRating = comp.user_rating;
  comp.user_rating = newRating;  // optimistic local mutation
  document.dispatchEvent(new CustomEvent("comp-rating-changed", {
    detail: { compKey, oldRating, newRating }
  }));
  try {
    await _persistCompRating(compKey, newRating);  // reuses routes.py:1101
  } catch (err) {
    // Rollback
    comp.user_rating = oldRating;
    document.dispatchEvent(new CustomEvent("comp-rating-changed", {
      detail: { compKey, oldRating: newRating, newRating: oldRating }
    }));
    _showToast("Rating update failed — reverted", "error");
  }
}
```

**Rating-change event bus:**
- Existing canonical mutation point at `map.js:5061` ALSO dispatches `comp-rating-changed` (alongside its existing logic) so popup-rated changes flow through the same event channel.
- New section subscribes:
  ```js
  document.addEventListener("comp-rating-changed", () => _renderGoodCompsSection());
  ```

**Centralized workspace-changed hook:**
```js
function _onWorkspaceChanged(newAreaId, oldAreaId) {
  // Called from ALL area-id assignment sites. Replaces ad-hoc cleanup
  // currently scattered across map.js:2181, 3084, 3417, 7900, 8074, 9318.
  _currentLoadedAreaId = newAreaId;
  // Existing per-site side effects (originator star, etc.) call this and stay.
  // NEW: clear Good Comps immediately, then re-hydrate from new workspace.
  _renderGoodCompsSection();  // empties if newAreaId is null or different
  // Other future per-workspace side effects can be added here.
}
```

Then audit all 6 assignment sites and route them through `_onWorkspaceChanged`. Keeps fork flow consistent with normal load.

**Saved-area load + restore:**
- Existing `restoreSavedArea` already fetches comp_ratings as part of the workspace load — no new fetch needed.
- After ratings restore into `allPropelioComps[i].user_rating`, call `_renderGoodCompsSection()` once.

## Sequencing

All 3 files (index.html, style.css, map.js) ship together in one PR. No backend changes.

## Verification plan

### Visual

1. Open a saved area with several already-rated comps → Good Comps section visible inside Comps List block, between Subject card and main comp list. Section header shows correct count.
2. Cards mirror Subject card visual structure (gold left border, dark green bg, price + Good badge + addr + nbhd + 3 meta lines).
3. Click on the row body → map pans/zooms to that comp + highlights it (same as clicking a regular comp).
4. Empty state shows when no good comps in saved area.
5. Section hidden when no saved area loaded.

### Behavior

6. Click Remove (`×`) on a good-card → card disappears immediately (optimistic), server confirms in background, count updates, comp returns to neutral on map.
7. Click Bad Comp → card disappears immediately, comp picks up bad-comp visual treatment on map.
8. From a popup elsewhere, mark a NEW comp as Good → that comp appears in the Good Comps section immediately (via `comp-rating-changed` event).
9. From the popup, change a Good rating to Bad → comp leaves the Good Comps section.
10. From the popup, clear a Good rating (set to null) → comp leaves the section.
11. Server failure on Remove/Bad → comp re-appears in section + error toast.

### Workspace lifecycle

12. Load workspace A → good comps populate. Load workspace B → good comps re-populate (different set).
13. Fork a workspace → forked workspace's good comps appear (CURRENT BUG IF NOT WIRED — fork bypassed restore/hydrate; v2 spec wires it via `_onWorkspaceChanged`).
14. Clear workspace (Clear button) → section hides.

### Filter independence

15. Apply a filter that would hide a regular comp (e.g., status=sold, but this good comp is active) → Good Comps section still shows it. Confirms filters are ignored.

### A11y

16. Tab to a good card → focus ring visible. Press Enter → pans to comp.
17. Tab to Remove / Bad buttons → focusable, ARIA labels announced by screen reader.
18. Count change announced via aria-live="polite" on the count element.

### Performance

19. ~30 good comps render without noticeable lag.
20. Rating change → full re-render in <100ms.

## Risk

| Risk | Severity | Mitigation |
|---|---|---|
| Centralized `_onWorkspaceChanged` audit misses one of the 6 sites → stale state | Medium | Verification checklist explicitly tests fork, share-load, clear, normal load |
| Optimistic UI reverts cause confusing UX | Low | Toast on failure; revert is brief; standard pattern |
| `comp-rating-changed` event dispatched too frequently (e.g., during bulk operations) → render thrash | Low | Acceptable at current 30-row scale; coalesce later if needed |
| Subject card render path (`map.js:883`) refactored to use shared formatters → regressions in Subject card | Medium | Verification step explicitly checks Subject card unchanged; small extraction, low risk |
| Filter-independence creates UX confusion ("why is this in Good but hidden in main list?") | Low | Per KK call. Could add a subtle "(filtered out)" muted note on the good card if it doesn't match active filters — defer to V2 |
| Touch hit-areas of Remove + Bad too close → mis-tap | Low | 8px gap + 8px vertical padding + coarse-pointer media query buffer |
| Card price color (`#00d2c5`) clashes with theme on certain comps with missing data | Low | Existing subject card already uses this; consistent |
| Existing role-based filter inheritance bug (`_master_todo.md:191`) could affect Good Comps count visibility across user roles | Medium | Out of scope for this spec; flag for separate fix. Filter independence (always show all) actually MITIGATES this for good comps. |

## Rollback

Single-PR revert. No DB migration. `comp_ratings` data untouched. Bonus side effects:
- The shared formatters extraction is additive; old call sites continue to work if rollback leaves the extraction intact.
- `_onWorkspaceChanged` helper is additive; if rolled back the existing ad-hoc cleanup paths remain.

## Out of scope

- Bulk actions (clear all good comps, mass-flip to bad)
- Advanced keyboard nav (arrow keys, Shift+select, multi-row actions)
- Photos / external Propelio listing link inside the card
- Diff badge per comp (e.g., "↑ since last view")
- Filter-aware "(filtered out)" muted note on cards
- A "Bad Comps" parallel section (defer; symmetric concept but not requested)
- CSV export integration (does the CSV already include the Good rating? Confirm separately)
- Memoization / virtualized rendering for very large workspaces

## Open items for second Copilot critique (optional)

1. **`_focusCompOnMap` correctness**: spec assumes a clean reusable helper exists derived from `map.js:4990`. Confirm or recommend extraction signature.
2. **`_persistCompRating` signature**: spec assumes a function that POSTs to `routes.py:1101`. Confirm function name + signature in current code.
3. **`_currentSubjectComp` global**: spec references this for distance/price-per-sqft calculations. Confirm name + availability in current scope.
4. **`allPropelioComps` global**: same — confirm name + structure.
5. **`_sortComps` + `_currentCompSortKey`**: spec assumes a sort function + current sort key state. Confirm.
6. **Visibility tied to `_currentLoadedAreaId`**: spec uses this as the gate. Confirm this is set by every workspace-load path (including fork) AFTER v2's `_onWorkspaceChanged` rollout.

## Implementation effort estimate

- HTML markup: 0.25 day
- CSS for section + card: 0.5 day
- Shared formatter extraction: 0.25 day
- Render function + event bus + workspace-changed hook: 0.5 day
- Optimistic UI wiring + handlers: 0.25 day
- Verification + preview deploy + iteration: 0.5 day
- **Total: ~2.25 days**

## Status

**v2 locked. Ready for implementation.** Open items above are confirm-with-code questions, not blocking design decisions. Address inline during implementation.
