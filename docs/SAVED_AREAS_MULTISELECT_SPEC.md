---
created: 2026-05-23
status: v3 — 2nd Copilot critique + KK product calls incorporated; LOCKED, ready for implementation
updated: 2026-05-23
---

# Saved Areas + Saved Targets — Multi-Select, Bulk Actions, Hover-Expand Redesign

## Changelog

- **v1 (initial draft):** Hover-revealed left checkbox + bottom-sticky bulk-action toolbar. Per-row trash removed. Type icon (incorrectly called "drag handle") removed. JS-positioned popup tooltip planned for long-name overflow. Scope: Areas + Sessions.

- **v3 (THIS, post-2nd Copilot deep-dive + KK product calls):**
  - `_showBanner` (which doesn't exist) replaced with `_showToast` (mature function at `map.js:3590`).
  - **Hover-expand: no display-mode flip.** Keep `display: -webkit-box` for both states; only change `-webkit-line-clamp` (`2` → `99`) and `overflow` (`hidden` → `visible`). Avoids the jank Copilot flagged.
  - **Hover-expand delay = 200ms** via CSS `transition-delay`. Per KK: prevents accidental expansions during fast mouse travel across the list.
  - **New `.saved-list-head` container** added inside each `.collapsible-body` (before search input) to hold the Select-mode toggle button. Doesn't disrupt the section-toggle collapse affordance.
  - **Type icon a11y:** the emoji `<span class="saved-area-icon">` gets `aria-hidden="true"`. A new `<span class="visually-hidden">` sibling carries the type label ("Parcel" / "Location" / "Workspace") so screen readers announce semantic type.
  - **`aria-labelledby` ids prefixed by listKey now** (not deferred). E.g., `name-saved-areas-${area.id}` and `name-saved-parcels-${area.id}`. Defensive against id collision across lists.
  - **Bulk delete = visible-only.** Per KK product call: deletion respects current search filter. When a search filter hides rows, their ids are dropped from `selectedIds` automatically on the resulting list refresh. Toolbar count always reflects visible-selected. Less powerful for the "select then filter" workflow but safer mental model.
  - **Grep verification gate added** to verification plan: before merge, `git grep saved-area-quick-delete-btn` and `git grep actions-secondary` MUST return zero results.
  - **Sessions UX cue: skipped per KK** (small team; verbal heads-up suffices).

- **v2 (post-1st Copilot deep-dive + KK product calls):**
  - **Diagnosis correction:** The left-side icon is a TYPE marker (`📌` = parcel, `📍` = location, `▭` = workspace) at `map.js:3960`. KEEPING it. Spec v1 incorrectly called it a decorative drag handle.
  - **Scope corrected:** Saved **Areas + Saved Targets/Parcels** (the two `_renderList`-rendered lists). Saved Sessions explicitly OUT of v1 (no search input, no bounded scroll, structurally different — defer to v2 if needed).
  - **Popup tooltip approach DROPPED.** Replaced by **row-hover-expand**: row's `-webkit-line-clamp` releases to `unset` on `:hover` / `:focus-within`, name shows in full inline, row grows naturally. No JS, no positioning math, no clip risk. KK's idea 2026-05-23.
  - **Toolbar placement reworked:** Non-sticky sibling at end of `.collapsible-body` (not sticky-bottom inside the list, which clipped against `overflow-y: auto`). Padding-based layout, no negative margins.
  - **Selection anchor by ID, not index:** `lastAnchorId` (string) instead of `lastAnchorIndex` (number). Range select computes DOM order at click time. Survives cache mutations (`map.js:2082, 2084, 2176, 2305, 2346`).
  - **Row click ALWAYS opens** — only checkbox click toggles selection. Per KK product call. Simpler mental model. Matches Notion.
  - **Touch UX:** explicit "Select" mode toggle button on each section header. No coarse-pointer hack. Per KK product call.
  - **Server-side deletion mid-selection:** On every list refresh, intersect `selectedIds` with available IDs in cache. Treat 404 on bulk delete as non-fatal "already deleted." Surface passive banner if items pruned.
  - **Esc scoped:** bound on the focused list container, not document. Bail when `event.target` matches `input`/`textarea`/`[contenteditable]`. Avoids stealing Esc from search input or rename input.
  - **Bulk delete strategy:** bounded concurrency (4 parallel) via `Promise.allSettled`. Existing banner pattern (not `window.alert`) for completion summary.
  - **Custom checkbox a11y:** native `<input type="checkbox">` element + `appearance: none` for styling. Forced-colors media query fallback. `aria-labelledby` referencing the row's name span (not just `aria-label`).
  - **Cleanup audit:** dead refs to `.saved-area-quick-delete-btn` in CSS (`style.css:1992`), JS click branch (`map.js:3440`), and role-gating logic (`map.js:3367, 3374`) all removed in same commit.
  - **Two simultaneous toolbars:** kept independent (one per list). Default behavior; revisit only if user feedback says confusing.
  - **Hidden-by-search items in bulk delete:** included by default (they're semantically still selected). The visible toolbar count reflects the FULL `selectedIds` size, not just visible. Bulk delete operates on the full set.

## Problem

The saved-areas + saved-targets sidebar lists have these pain points:

1. **No bulk delete.** Deleting >1 item requires per-row trash → confirm → wait → next. Painful for cleanup.
2. **Long titles clip or hide behind the right-edge action icons.** Multi-row wraps to 2 lines but absurdly long names still don't display fully; popup approaches (tried earlier today, removed in commit `4cc2e76e`) clip outside the sidebar into the map area.
3. **Per-row trash icons crowd narrow rows.** Combined with type icon + name + share, the row is dense.
4. **Long-title workaround forces destructive interaction.** Today the client opens or renames a saved area JUST to read its full name — and opening leaves the workspace he was on. Bad UX.

Solution: hover-revealed left checkbox + bulk-action toolbar (delete now, export/share later) + hover-expand-in-place on the row itself (eliminates need for popup and eliminates need to open the area to read its name).

Pattern researched 2026-05-23 from Linear, Notion, Gmail, Outlook Web, Todoist, Google Drive, Dropbox, ClickUp. Hover-reveal-left + contextual toolbar is the universal modern pattern.

## Goal

For BOTH the Saved Areas list (`#saved-areas-list`) and the Saved Targets/Parcels list (`#saved-parcels-list`):

1. **Hover-revealed left checkbox per row.** Hidden at rest. Appears on `:hover` / `:focus-within` / when the list is in active selection mode (≥1 selected).
2. **Type icon retained** at its current position (inline before title). NOT removed.
3. **Per-row trash icon removed.** Bulk delete is the only delete path.
4. **Per-row share icon retained.** Quick action stays.
5. **Title hover-expand:** at rest, `-webkit-line-clamp: 2`. On `:hover` / `:focus-within`, line-clamp releases — row grows inline to fit the full name. No popup.
6. **Bulk-action toolbar:** appears as a sibling element at the end of `.collapsible-body` whenever ≥1 row in that list is selected. Shows "N selected | 🗑 Delete | ✕ clear". Future-extensible for Export / Share.
7. **Selection mechanics:** checkbox click toggles. Shift+click ranges (by DOM order at click). Esc clears (scoped). Cmd/Ctrl+A selects all visible. Row body click ALWAYS opens (never toggles).
8. **Touch:** explicit "Select" mode toggle button on each section header. Tap to enter, all checkboxes appear; tap "Done" or Esc to exit.
9. **Bulk delete:** loops existing per-id DELETE calls with bounded concurrency (4 parallel), `Promise.allSettled`. Native confirm dialog. 404 treated as non-fatal. Completion banner (not alert).

Saved Sessions is OUT of scope for v1. Will revisit if needed once Areas + Targets ship.

## Non-goals

- New bulk-delete API endpoint (loop existing per-id endpoints for v1)
- Toast-undo system (confirm dialog is the safeguard for v1)
- Bulk export / bulk share handlers (toolbar slot reserved for V2)
- Drag-to-reorder rows (deferred)
- Mobile-first redesign (touch supported via "Select" mode toggle; primary is desktop)
- Right-click context menu / Rename flow changes (existing behavior preserved)
- Selection persistence across page reloads or workspace navigation
- Saved Sessions list (separate structure, deferred to v2)

## Changes (3 files)

### 1. `frontend/index.html` — saved-list-head + bulk-action toolbar + Select-mode toggle

Three new patterns to insert in BOTH `#saved-areas-body` and `#saved-parcels-body`.

**New `.saved-list-head` container** at the TOP of each body (before search input) — hosts the Select-mode toggle button. Doesn't disrupt the section-toggle collapse affordance which lives on the section header itself.

```html
<!-- Inside #saved-areas-body, BEFORE #saved-areas-search -->
<div class="saved-list-head">
  <button id="saved-areas-select-mode-btn" class="select-mode-toggle hidden" type="button" data-list="saved-areas">Select</button>
</div>
```

Same for `#saved-parcels-select-mode-btn` with `data-list="saved-parcels"`.

Visibility logic: on touch / coarse-pointer media query, the toggle button is always visible. On non-touch (desktop), it's hidden — keyboard/mouse users get hover-reveal of the checkboxes themselves.

**Bulk-action toolbar — placed at end of `.collapsible-body`, AFTER `#saved-areas-list`:**
```html
<div id="saved-areas-bulk-toolbar" class="bulk-action-toolbar hidden" role="toolbar" aria-label="Bulk actions for selected saved areas">
  <span class="bulk-action-count" id="saved-areas-bulk-count" aria-live="polite">0 selected</span>
  <button type="button" class="bulk-action-btn bulk-action-delete" data-action="delete-selected" data-list="saved-areas" title="Delete selected">🗑 Delete</button>
  <button type="button" class="bulk-action-btn bulk-action-clear" data-action="clear-selection" data-list="saved-areas" aria-label="Clear selection">✕</button>
</div>
```

Same structure mirrored as `#saved-parcels-bulk-toolbar` (with `data-list="saved-parcels"`) inside `#saved-parcels-body` after `#saved-parcels-list`.

### 2. `frontend/style.css` — checkboxes, hover-expand, toolbars, selected state

**Row hover-expand (replaces all prior popup/tooltip approaches):**
```css
.saved-area-name {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.3;
  color: var(--text-main);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
  /* 200ms delay before expansion fires — prevents accidental expansions
     during fast mouse travel across the list. Per KK product call v3. */
  transition: -webkit-line-clamp 0s 200ms, overflow 0s 200ms;
}

/* Hover-expand: release the clamp so the row's name shows in full.
   IMPORTANT: keep `display: -webkit-box` stable. Only change
   -webkit-line-clamp + overflow. Per Copilot v3 critique — display-mode
   flip caused jank. */
.saved-area-row:hover .saved-area-name,
.saved-area-row:focus-within .saved-area-name {
  -webkit-line-clamp: 99;
  overflow: visible;
}
```

**Visually-hidden helper class** (if not already present):
```css
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
```

**Hover-revealed checkbox (custom-styled native input):**
```css
.saved-area-checkbox {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  margin-right: 8px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.12s ease;
  appearance: none;
  -webkit-appearance: none;
  background: rgba(13, 55, 42, 0.6);
  border: 1.5px solid rgba(201, 162, 79, 0.5);
  border-radius: 3px;
  cursor: pointer;
  position: relative;
}

.saved-area-checkbox:checked {
  background: var(--gold);
  border-color: var(--gold);
}

.saved-area-checkbox:checked::after {
  content: "✓";
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  color: #0d372a;
  font-size: 13px;
  font-weight: 700;
  line-height: 1;
}

/* Reveal: row hover/focus, list-wide selection-active, or touch mode active */
.saved-area-row:hover .saved-area-checkbox,
.saved-area-row:focus-within .saved-area-checkbox,
.has-selection .saved-area-checkbox,
.select-mode-active .saved-area-checkbox {
  opacity: 1;
  pointer-events: auto;
}

.saved-area-checkbox:focus-visible {
  outline: 2px solid var(--gold);
  outline-offset: 2px;
}

/* Forced-colors / high-contrast mode — let the system render the checkmark */
@media (forced-colors: active) {
  .saved-area-checkbox {
    appearance: auto;
    -webkit-appearance: auto;
    background: ButtonFace;
    border: 1px solid ButtonText;
  }
  .saved-area-checkbox:checked::after {
    content: none;
  }
}

/* Touch / coarse-pointer: keep Select-mode toggle button always visible */
@media (pointer: coarse) {
  .select-mode-toggle {
    /* Stays visible by default — JS removes .hidden on initial load */
  }
}
```

**Selected row state:**
```css
.saved-area-row.is-selected {
  background: rgba(201, 162, 79, 0.16);
  box-shadow: inset 3px 0 0 var(--gold);
}
```
Composes with existing `:hover` / `:focus-within` (background blend; left bar stays gold).

**Bulk-action toolbar (non-sticky, sibling of the list):**
```css
.bulk-action-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  margin-top: 6px;
  background: linear-gradient(180deg, rgba(13, 55, 42, 0.96), rgba(10, 40, 30, 0.98));
  border-top: 1px solid rgba(201, 162, 79, 0.3);
  border-radius: 4px;
  animation: slideUpFade 150ms ease-out;
}

@keyframes slideUpFade {
  from { transform: translateY(6px); opacity: 0; }
  to   { transform: translateY(0);   opacity: 1; }
}

.bulk-action-toolbar.hidden { display: none; }

.bulk-action-count {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-soft);
  flex: 1;
}

.bulk-action-btn {
  background: transparent;
  border: 1px solid rgba(201, 162, 79, 0.4);
  color: #f5e9c8;
  padding: 5px 10px;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease;
}

.bulk-action-btn:hover { background: rgba(201, 162, 79, 0.15); border-color: var(--gold); }

.bulk-action-btn.bulk-action-delete {
  border-color: rgba(224, 36, 50, 0.5);
  color: #ff8a92;
}
.bulk-action-btn.bulk-action-delete:hover {
  background: rgba(224, 36, 50, 0.18);
  border-color: rgba(224, 36, 50, 0.9);
}

.bulk-action-btn.bulk-action-clear { padding: 5px 8px; font-weight: 700; }
```

**Select-mode toggle button (header):**
```css
.select-mode-toggle {
  background: transparent;
  border: 1px solid rgba(201, 162, 79, 0.4);
  color: var(--text-soft);
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 3px;
  cursor: pointer;
}
.select-mode-toggle.is-active {
  background: rgba(201, 162, 79, 0.18);
  color: var(--gold);
  border-color: var(--gold);
}
```

**Dead-code cleanup:**
- Remove `.saved-area-quick-delete-btn` rules at `style.css:1987-2008` (entire block).
- Drop the `.actions-secondary` rule at `style.css:907` (already orphan from prior commit; finish removing).

### 3. `frontend/map.js` — row HTML, selection state, handlers

**Row HTML construction (inside `_renderList` ~line 3978; same renderer handles both Areas and Targets):**

```html
<div class="saved-area-row${activeClass}${selectedClass}" tabindex="0" data-id="${area.id}" data-type="${area.type}">
  <div class="saved-area-main">
    <input type="checkbox"
           class="saved-area-checkbox"
           data-action="toggle-selection"
           data-id="${area.id}"
           aria-labelledby="name-${listKey}-${area.id}">
    <span class="saved-area-icon" aria-hidden="true">${icon}</span>
    <span class="visually-hidden">${_typeLabel(area.type)}</span>
    <span class="saved-area-name-wrap"><span class="saved-area-name" id="name-${listKey}-${area.id}">${displayName}</span></span>
    ${canShare ? `<button type="button" class="saved-area-action-btn saved-area-share-btn" data-action="share" data-share-id="${_esc(area.share_id)}" title="Share">🔗</button>` : ""}
  </div>
  <div class="saved-area-secondary-line">${secondaryLine}</div>
  <div class="saved-area-row-secondary-actions">
    <hr class="saved-area-actions-divider">
    <div class="saved-area-secondary-btns">
      ${!showFullControls && canShare ? `<button type="button" class="saved-area-action-btn" data-action="fork" data-share-id="${_esc(area.share_id)}" title="Make my own copy">📋 Make my copy</button>` : ""}
      ${showFullControls && canRename ? `<button type="button" class="saved-area-action-btn rename" data-action="rename" title="Rename">✎ Rename</button>` : ""}
    </div>
  </div>
</div>
```

Where `_typeLabel(type)` returns `"Parcel"` for `parcel`, `"Location"` for `location`, `"Workspace"` for `area` (or whatever the default `▭` represents).

The `listKey` (e.g., `"saved-areas"` or `"saved-parcels"`) is passed into `_renderList` and used to disambiguate the `aria-labelledby` id so two lists can't collide.

Notes:
- **Removed:** the per-row `🗑` trash button (`.saved-area-quick-delete-btn`)
- **Removed:** `data-tooltip` attribute on `.saved-area-name-wrap` (no longer needed; hover-expand replaces popup)
- **Removed:** `data-list-index` (replaced by DOM-order-at-click-time computation)
- **Kept:** the type-icon `.saved-area-icon` — KK confirmed it stays inline before the title
- **Kept:** share button, secondary-actions block (with Rename / Fork)
- **Added:** `<input type="checkbox" class="saved-area-checkbox">` as FIRST child of `.saved-area-main`. `aria-labelledby` references the name span (proper a11y label association).
- **Added:** `id="name-${area.id}"` on the name span to support `aria-labelledby`.

**Selection state (per list):**
```js
const _listSelections = {
  "saved-areas": { selectedIds: new Set(), lastAnchorId: null, selectModeActive: false },
  "saved-parcels": { selectedIds: new Set(), lastAnchorId: null, selectModeActive: false },
};

function _getSelectionForList(listKey) { return _listSelections[listKey]; }
```

**Anchor by ID (not index) + DOM-order-at-click-time:**
```js
function _handleCheckboxClick(ev) {
  const checkbox = ev.target.closest('.saved-area-checkbox');
  if (!checkbox) return;
  ev.stopPropagation();
  const row = checkbox.closest('.saved-area-row');
  const listEl = row.closest('[id^="saved-"][id$="-list"]');
  const listKey = listEl.id.replace("-list", "");  // "saved-areas" or "saved-parcels"
  const sel = _getSelectionForList(listKey);
  const id = checkbox.dataset.id;

  if (ev.shiftKey && sel.lastAnchorId) {
    // Compute DOM-order range AT CLICK TIME — robust to cache mutations
    const allRows = Array.from(listEl.querySelectorAll('.saved-area-row[data-id]'));
    const idsInOrder = allRows.map(r => r.dataset.id);
    const anchorIdx = idsInOrder.indexOf(sel.lastAnchorId);
    const currentIdx = idsInOrder.indexOf(id);
    if (anchorIdx >= 0 && currentIdx >= 0) {
      const [lo, hi] = [anchorIdx, currentIdx].sort((a, b) => a - b);
      for (let i = lo; i <= hi; i++) sel.selectedIds.add(idsInOrder[i]);
    }
  } else {
    if (sel.selectedIds.has(id)) sel.selectedIds.delete(id);
    else sel.selectedIds.add(id);
    sel.lastAnchorId = id;
  }
  _refreshSelectionUI(listKey);
}
```

**Refresh UI on every render:**
After every `_renderList` invocation, run `_refreshSelectionUI(listKey)` to:
1. Intersect `sel.selectedIds` with the **currently-rendered** row IDs in the DOM → drop any that aren't visible. This handles BOTH (a) server-side deletion and (b) search-filter narrowing the visible set. Per KK v3 product call: bulk delete is visible-only, so hidden-by-search rows leave the selection automatically.
2. If server-side deletions caused the drop (vs search-filter), show passive toast: `_showToast("N saved areas were removed elsewhere — selection updated.")`. Detect via: did the underlying cache lose ids that weren't in the search-hidden set? If yes → server deletion. If just search-filter → silent.
3. Sync `.is-selected` class on rendered rows + checkbox `.checked` state from `selectedIds`.
4. Toggle `.has-selection` class on the list root element (so checkboxes stay sticky-visible across the list).
5. Update bulk toolbar count + visibility (count = `selectedIds.size` post-intersection, i.e., visible-selected).

**Row body click ALWAYS opens (does not toggle selection):**
Existing row click handler unchanged. Checkbox click `stopPropagation`'s to prevent the row click from firing (only when checkbox itself is clicked).

**Bulk delete with bounded concurrency:**
```js
async function _handleBulkDelete(listKey) {
  const sel = _getSelectionForList(listKey);
  const n = sel.selectedIds.size;
  if (n === 0) return;
  const noun = (listKey === "saved-areas") ? "saved area" : "target";
  const ok = window.confirm(`Delete ${n} ${noun}${n > 1 ? "s" : ""}? This cannot be undone.`);
  if (!ok) return;

  const ids = Array.from(sel.selectedIds);
  const CONCURRENCY = 4;
  let successCount = 0;
  const failed = [];
  const alreadyDeleted = [];

  // Bounded-concurrency pool
  const queue = [...ids];
  const workers = Array.from({ length: Math.min(CONCURRENCY, queue.length) }).map(async () => {
    while (queue.length) {
      const id = queue.shift();
      try {
        await _deleteByIdForList(listKey, id);
        successCount++;
      } catch (err) {
        if (err && err.status === 404) alreadyDeleted.push(id);
        else failed.push({ id, err });
      }
    }
  });
  await Promise.allSettled(workers);

  sel.selectedIds.clear();
  sel.lastAnchorId = null;
  await _refreshListAfterBulkDelete(listKey);

  // Existing toast system — NOT window.alert. _showToast at map.js:3590.
  if (failed.length > 0) {
    _showToast(`Deleted ${successCount}. ${failed.length} failed — check console.`, "error");
    console.error("bulk delete failures", failed);
  } else if (alreadyDeleted.length > 0) {
    _showToast(`Deleted ${successCount}. ${alreadyDeleted.length} were already removed.`, "info");
  } else {
    _showToast(`Deleted ${successCount} ${noun}${successCount > 1 ? "s" : ""}.`, "success");
  }
}
```

**Esc handling (scoped):**
```js
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  // Bail if user is in an input — they probably want to clear that field
  const t = ev.target;
  if (t && (t.matches("input, textarea, [contenteditable=true]"))) return;
  // Find which list (if any) has keyboard focus inside it
  const activeList = document.activeElement?.closest('[id^="saved-"][id$="-list"]');
  if (!activeList) return;
  const listKey = activeList.id.replace("-list", "");
  if (!_listSelections[listKey]) return;
  _clearSelection(listKey);
});
```

**Cmd/Ctrl+A (select all visible, scoped):**
```js
document.addEventListener("keydown", (ev) => {
  if (!(ev.key === "a" && (ev.metaKey || ev.ctrlKey))) return;
  const t = ev.target;
  if (t && t.matches("input, textarea, [contenteditable=true]")) return;
  const activeList = document.activeElement?.closest('[id^="saved-"][id$="-list"]');
  if (!activeList) return;
  ev.preventDefault();
  const listKey = activeList.id.replace("-list", "");
  const sel = _listSelections[listKey];
  if (!sel) return;
  activeList.querySelectorAll('.saved-area-row[data-id]').forEach(r => sel.selectedIds.add(r.dataset.id));
  _refreshSelectionUI(listKey);
});
```

**Select mode toggle (touch):**
```js
function _toggleSelectMode(listKey) {
  const sel = _getSelectionForList(listKey);
  sel.selectModeActive = !sel.selectModeActive;
  const listEl = document.getElementById(`${listKey}-list`);
  listEl?.classList.toggle("select-mode-active", sel.selectModeActive);
  if (!sel.selectModeActive) _clearSelection(listKey);
}
```

The `.select-mode-active` class on the list element is what keeps checkboxes always-visible on touch (already covered in the CSS above).

**Cleanup of removed handlers:**
- `style.css:1987-2008` — `.saved-area-quick-delete-btn` rules: removed in same commit
- `map.js:3440` — quick-delete click branch in delegated row handler: removed
- `map.js:3367, 3374` — role-gating checks tied to `showFullControls` for the per-row trash: simplified now that the trash is gone (role-gating logic remains for Rename / Fork but quick-delete branch is dropped)

## Sequencing

All 3 files change together in one PR. No backend / schema changes.

## Verification plan

### Visual checks (preview deploy) — desktop

1. Open preview, expand Saved Areas section. Rows show type icon + name (2-line clamp) + share icon. No trash icon. No drag handle.
2. Hover a row → checkbox appears on the left edge; row's name expands to its full length (any number of lines) inline.
3. Click a checkbox → row highlighted, bulk toolbar slides in below the list with "1 selected | 🗑 Delete | ✕".
4. Click another checkbox → "2 selected", both highlighted.
5. Shift+click a 3rd farther down → range from anchor-to-this-id selected.
6. Click row BODY (not checkbox) → row opens (loads the area). Existing behavior unchanged.
7. Click ✕ in toolbar → selection clears, toolbar slides out.
8. Same all the above in Saved Targets/Parcels section (independent selection).
9. Confirm Saved Sessions section is UNCHANGED.

### Long-title hover-expand

10. Save an area with a very long name (300+ chars). At rest, name clamps to 2 lines. On hover, row grows inline to show the full name. On unhover, collapses back.
11. Keyboard: tab to a long-named row → `:focus-within` triggers expansion same as hover.

### Behavior checks

12. Bulk delete 3 selected → confirm "Delete 3 saved areas?" → OK → rows disappear, success banner. No alert dialog.
13. Cancel confirm → nothing happens.
14. Simulate a 404 (modify one row's id in DevTools then bulk-delete) → completion banner: "Deleted 2. 1 was already removed."
15. Simulate a 500 (DevTools network throttle / throw) → "Deleted 2. 1 failed — check console."
16. Range select edge case: shift+click on the same row twice — anchor stays.
17. Search-filter list → hidden rows that were selected remain in `selectedIds`. Bulk delete still operates on the full set (count shows full size).
18. Esc inside search-input → search clears (browser default), selection untouched.
19. Esc inside list (row focused) → selection clears.
20. Cmd/Ctrl+A inside list → all visible select.
21. Cmd/Ctrl+A inside search input → standard browser select-all in the input, NOT list.

### Cross-list independence

22. Select 2 in Areas + 1 in Targets → both toolbars visible, independent.

### Server-side deletion mid-selection

23. With 3 items selected, manually delete one from a different tab → refresh the list. Selection updates to 2; passive banner: "1 saved area was removed elsewhere — selection updated."

### Touch (best-effort, requires touch device)

24. On touch device: "Select" button in section header visible. Tap → enters select mode; all checkboxes show. Tap rows to toggle. Tap Select again (now "Done") → exits, selection clears.

### Cleanup (MERGE GATE — must pass before PR merge)

25. `git grep saved-area-quick-delete-btn` returns ZERO results across all files.
26. `git grep actions-secondary` returns ZERO results across all files.
27. `git grep "_showBanner"` returns ZERO results (only `_showToast`).
28. Console: no errors about dead handlers or missing elements when interacting with both lists.
29. `aria-labelledby` ids on checkboxes match the `id` on the name span (no orphan references).

### Search-filter behavior (NEW v3 — visible-only bulk delete)

30. Select 3 rows in Saved Areas. Type a search query that filters out 1 of the 3. Toolbar count drops to 2. Bulk delete operates on 2.
31. Clear the search filter. The previously-hidden row does NOT come back into selection (it was de-selected when filter hid it). Verify count stays accurate.
32. Server-side deletion DOES trigger the "removed elsewhere" toast; search-filter does NOT (it's silent — expected user action).

## Risk

| Risk | Severity | Mitigation |
|---|---|---|
| Row hover-expand causes layout shift in list (neighbors push down) | Medium | Intended — KK explicitly approved hover-to-grow behavior. If user feedback says too much shift, gate expansion behind a longer hover delay (~300ms) or use `transition: max-height` for visual smoothness |
| Hover-expand fires on every mouseover during list scrolling | Low | Browser handles this fine; expansion is CSS-only, no JS. If perf issues, add `transition` to dampen |
| `:focus-within` expansion fires when keyboard focus lands inside the row's secondary-actions (Rename, Fork) → unexpected expansion when interacting with those buttons | Low | Acceptable — expansion is non-destructive; secondary-actions only show when row is `:focus-within` anyway, so user is already interacting with that row |
| Shared `_renderList` for areas + targets means a bug in one bleeds into the other | Medium | Add explicit test cases for both lists in verification plan (steps 1-7 for Areas, step 8 for Targets) |
| `aria-labelledby` requires unique `id` on the name span — collisions if same `area.id` appears in both lists | Low | Areas and Targets caches have disjoint IDs (areas: UUIDs, targets: composite). If a collision is theoretically possible, prefix the span id with listKey: `name-${listKey}-${area.id}` |
| Bulk delete bounded concurrency = 4 might still rate-limit on backend if N > 50 | Low | If backend limits become an issue, reduce concurrency or add jitter. Volumes typically ≤20 |
| Banner system `_showBanner` may not exist or behave differently — fallback unclear | Medium | Audit before implementation: confirm a banner/toast pattern exists. If not, ship with inline status text in the toolbar (no new system) |
| Server-side-deletion banner spam if multiple stale items detected per refresh | Low | Coalesce into a single banner with count; don't fire per-item |
| Touch "Select" mode toggle button placement competes with the existing section header collapse button | Medium | Place the Select button INSIDE the `.collapsible-body` head row (alongside Reset button on map-filters), not in the `.section-toggle` header itself. Don't disrupt collapse affordance |
| Existing role-gated `showFullControls` logic might still reference the removed trash | Low | Audit + remove in same commit (covered in §3 cleanup) |
| Forced-colors fallback may render differently than the gold theme intends | Low | Acceptable — forced-colors users get system colors deliberately for accessibility |

## Rollback

Single-PR revert. No DB migration, no API endpoint, no schema change.

## Out of scope

- Saved Sessions multi-select (defer to v2; structurally different)
- Bulk-delete API endpoint (loop per-id is fine for v1 volumes)
- Toast-undo (confirm dialog is the safeguard)
- Bulk export / bulk share handlers
- Drag-to-reorder rows
- Right-click context menu changes
- Cmd+S shortcut rebinding (separately tracked)
- Selection persistence across page reloads
- ARIA live-region announcements beyond the count
- Animating the hover-expand transition (instant is acceptable for v1)

## Open items for second Copilot critique

1. **`_showBanner` existence + signature** — does this function exist in the codebase? If not, what's the recommended fallback for completion messaging without `window.alert`?
2. **Type-icon `aria-hidden="true"` correctness** — icons are semantic (type indicator). Should screen readers announce them as type info instead of hiding? E.g., `aria-label="parcel"` / `aria-label="location"` / `aria-label="workspace"`?
3. **Search-filter visibility logic** — current `.saved-list-search-input` filters via DOM-show/hide or via re-render? If re-render, selection persists by id and refresh-UI handles it. If DOM-show/hide, hidden rows are still in DOM and Cmd+A would over-select them. Confirm.
4. **Shared `_renderList` for two lists — any list-specific state that v1 spec misses?** Specifically: the Targets list might have additional per-row controls (e.g., quick-load buttons) that the spec doesn't account for. Audit the targets-only branches in `_renderList`.
5. **Anchor-id staleness across cache replacement** — if `_savedAreasCache` is fully replaced and the old anchor-id no longer exists, range-select silently falls back to single-toggle. Is this acceptable, or should we surface a hint?
6. **Layout shift on hover-expand — is the smoothness acceptable without animation?** Spec defers animation to V2. Real-world feel will determine.
7. **Two-toolbar UX confusion** — kept independent. Confirm this isn't a regression risk if a user selects items in both lists simultaneously and forgets one before navigating away.
8. **Existing right-click context-menu for single-row delete** — does one exist? If so, does it still need updating now that quick-delete is gone?
9. **Anything else** — scope misses, anti-patterns, file:line cleanup we should bundle.

## Implementation effort estimate

- HTML changes (2 sections × toolbar + Select mode toggle + Select-mode button placement): 0.5 day
- CSS (checkbox + hover-expand + toolbar + selected state + cleanup): 0.5 day
- JS (selection state per list, click handlers, range/Esc/Cmd+A scoped, bulk delete with concurrency, _refreshSelectionUI, _toggleSelectMode, server-side-deletion handling): 1 day
- Cleanup audit (dead refs + role gating) + smoke: 0.25 day
- Verification + preview deploy + iteration: 0.5 day
- **Total: ~2.75 days** (slight up from v1's 2.5 due to expanded scope to Targets + Select mode toggle + concurrency)

## Status

**v2 ready for 2nd Copilot critique.** Once Copilot signs off, lock as v3 (or ship directly if no new criticals). Then implementation: Claude → Copilot codes → Claude verifies → preview → KK approval → main.
