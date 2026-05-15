# Status Badge OAC-Awareness — Design Spec v1

**Status:** Draft pending KK sign-off. Then implement.
**Author:** KK + Claude (2026-05-14).
**Branch:** TBD (suggest `feat/status-badge-oac-aware`).

---

## 1. Purpose

The Sold / Active / Pending count badges in the sidebar currently show **total** counts across the workspace's Propelio comps, even when the OAC ("show out-of-area comps") toggle is OFF. This makes the badges feel disconnected from what's actually rendered on the map — flipping OAC off changes which comps you see but doesn't change which numbers are reported.

Rewire the badges so each one answers **"if I turn this toggle on with my current settings, how many comps would I see?"** That means the OAC toggle state propagates into the Sold/Active/Pending counts.

The OAC badge itself stays informational: **"X comps are currently out of view"** regardless of the toggle's state.

## 2. Current behavior (frontend/map.js:4054-4085)

The `_updatePropelioStatusCounts()` function builds a synthetic filter state with **all four toggles forced ON**:

```js
const baselineFilters = {
    ...propelioFilterState,
    statusSold: true,
    statusActive: true,
    statusPending: true,
    showOutsideArea: true,   // ← OAC forced on regardless of actual state
};
const baselineVisible = window._propelioLast.comps.filter(
    (c) => compPassesPropelioFilters(c, baselineFilters)
);
const baselineWinners = _dedupCompsForRender(baselineVisible);

let sold = 0, active = 0, pending = 0, oac = 0;
for (const c of baselineWinners) {
    const bucket = _propelioStatusBucket(c);
    if (bucket === "sold") sold++;
    else if (bucket === "pending") pending++;
    else active++;
    if (c?.extra?.is_outside_polygon) oac++;
}
```

All four badges are then set from this single pass. The OAC badge correctly reflects the count of out-of-polygon comps; the status badges correctly reflect status totals — but **the status totals do not respect the user's actual OAC toggle state**.

## 3. New behavior

Each badge answers a specific question:

| Badge | Question it answers |
|---|---|
| Sold | "If I turn Sold ON with my current settings, how many sold comps will I see?" |
| Active | "If I turn Active ON with my current settings, how many active (for_sale) comps will I see?" |
| Pending | "If I turn Pending ON with my current settings, how many pending comps will I see?" |
| OAC | "How many comps are currently filtered out by OAC being off?" (informational) |

For Sold/Active/Pending, the OAC state matters: when OAC is OFF and a polygon is drawn, out-of-polygon comps don't pass the gate, so they shouldn't be counted in the status totals.

For the OAC badge itself, the OAC state is the toggle being measured, so the baseline forces OAC ON (current behavior preserved).

## 4. Behavior matrix

Assumes a polygon has been drawn (the OAC gate is a no-op without one — see §6 edge case).

| OAC toggle | Status badges show | OAC badge shows |
|---|---|---|
| ON | Total Sold / Active / Pending (in-area + out-of-area) | Count of out-of-polygon comps |
| OFF | In-area Sold / Active / Pending only | Count of out-of-polygon comps (unchanged) |

The status badge counts respect OAC's actual state. The OAC badge stays informational either way.

## 5. Implementation

Two filter passes in `_updatePropelioStatusCounts()` instead of one. Roughly 15-20 lines changed in `frontend/map.js` between lines 4054-4085.

```js
// Pass 1: status counts — honor actual showOutsideArea state.
// "If I turn this status ON with my current settings, how many will I see?"
const statusBaselineFilters = {
    ...propelioFilterState,
    statusSold: true,
    statusActive: true,
    statusPending: true,
    // showOutsideArea NOT forced — inherits from propelioFilterState
};
const statusVisible = window._propelioLast.comps.filter(
    (c) => compPassesPropelioFilters(c, statusBaselineFilters)
);
const statusWinners = _dedupCompsForRender(statusVisible);

let sold = 0, active = 0, pending = 0;
for (const c of statusWinners) {
    const bucket = _propelioStatusBucket(c);
    if (bucket === "sold") sold++;
    else if (bucket === "pending") pending++;
    else active++;
}

// Pass 2: OAC count — always count all out-of-polygon comps regardless
// of toggle state. The OAC badge is informational ("X comps out of view"),
// so its baseline forces showOutsideArea ON to count everything.
const oacBaselineFilters = {
    ...propelioFilterState,
    statusSold: true,
    statusActive: true,
    statusPending: true,
    showOutsideArea: true,
};
const oacVisible = window._propelioLast.comps.filter(
    (c) => compPassesPropelioFilters(c, oacBaselineFilters)
);
const oacWinners = _dedupCompsForRender(oacVisible);

let oac = 0;
for (const c of oacWinners) {
    if (c?.extra?.is_outside_polygon) oac++;
}

const setText = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = String(val);
};
setText("prop-count-sold", sold);
setText("prop-count-active", active);
setText("prop-count-pending", pending);
setText("cf-count-sold", sold);
setText("cf-count-active", active);
setText("cf-count-pending", pending);
setText("prop-count-oac", oac);
```

## 6. Edge cases

- **No polygon drawn yet** (e.g., by-address pulls): `compPassesPropelioFilters` falls open on the polygon-containment check (line 4369 guards on `lastPolygon.length >= 3`). So when there's no polygon, status badges show totals regardless of OAC state — same behavior as today.

- **OAC badge when no polygon**: `is_outside_polygon` flag only gets set when there IS a polygon (server-side flagging in the spatial query). Without a polygon, the OAC badge naturally reads 0. Correct.

- **Workspace with zero comps**: existing early-return at line 4041 zeroes all badges. Unchanged.

- **Status filter currently OFF, OAC currently OFF**: the status badges still show the count as if THAT status were on (per the synthetic `statusSold: true, statusActive: true, statusPending: true` in the baseline). Only OAC propagates from `propelioFilterState`. This preserves the existing baseline-on-self semantic for each badge.

- **Recompute trigger**: `_updatePropelioStatusCounts()` is currently called once at startup (line 4088) and presumably re-invoked when filters change. Verify during implementation that toggling OAC re-triggers the count update — otherwise the badges won't refresh until the next workspace load.

## 7. UX considerations

- **No visual styling changes.** The numbers themselves communicate state; no need for badge color shifts or strikethroughs when OAC is off.

- **No tooltip changes.** Existing tooltips (if any) on the badges remain accurate under the new semantics.

- **Frontend-only change.** Server endpoints and DB schema unchanged. The "Note under OAC reads 'Some comps filtered out' when filters reduce visibility" (per `feedback_filters_oac_note` memory equivalent) still works the same way — that note is independent of the status badge counts.

## 8. Testing

Two interactive smoke tests on the dev URL:

1. **Toggle OAC off with a polygon drawn**:
   - Sold/Active/Pending badge counts should decrease (out-of-polygon comps no longer counted)
   - OAC badge count unchanged
   - Visual map render unchanged (already respected OAC)

2. **Toggle OAC back on**:
   - Sold/Active/Pending badge counts return to totals
   - OAC badge unchanged
   - Visual map render shows out-of-area comps again

No automated tests needed for a frontend-only filter recalc — existing render tests already validate the count display mechanism.

## 9. Scope

**In scope:**
- The 15-20 line refactor in `_updatePropelioStatusCounts()` at `frontend/map.js:4054-4085`
- Verify the recompute trigger fires on OAC toggle (may already; if not, hook it up)

**Out of scope:**
- Other count badges (CAD parcel-type badges per memory `lotledger-current-status-2026-05-14`'s "CAD count badges show baseline" — those have different semantics)
- Sold-within-days filter interaction with the count badges (separate concern, can be addressed in a follow-up if needed)
- Tooltip / UI / styling changes to the badges
- Backend / API changes

## 10. Branch + commit

- Feature branch: `feat/status-badge-oac-aware` off `develop`
- Single commit suggested message: `fix(filters): status count badges now respect OAC toggle state`
- Spec lives at `docs/propelio/STATUS_BADGE_OAC_AWARENESS_SPEC.md`
- Auto-deploys to `lot-ledger-dev` Cloud Run service on merge to `develop` (per memory `lotledger-current-status-2026-05-14`)

## 11. Validation plan

After merge to `develop` and auto-deploy:

1. Load a saved area with known mixed in-area/out-of-area comps
2. Confirm: with OAC ON, status badges show full counts (matches today's behavior)
3. Toggle OAC OFF
4. Confirm: status badges decrease by the out-of-area count; OAC badge unchanged
5. Spot-check by clicking through a few of the now-decounted comps to verify they're indeed outside the polygon

KK does the validation on dev URL before merging to main.
