# Parcel Distance Tools — Design Spec v1.2

**Status:** Copilot R1 + KK refinement folded in (2026-05-15). Build-eligible.
**Branch:** `feat/preview-bundle-2026-05-14` (adds to the OAC fix already on this branch — both ship to preview together)
**Author:** KK + Copilot (initial brainstorm) + Claude (refinement and spec, 2026-05-15)

## Changes v1.1 → v1.2 (KK refinement)

1. **§5.2 / §6 — dropped the dedicated Clear button.** Reverted Copilot's nice-to-have addition. Single toggle button is cleaner. Esc now clears (and stays in mode); clicking the toggle button exits (and clears anything in flight). Two-predictable-behaviors UX, one fewer button to maintain.

## Changes v1 → v1.1 (Copilot review folded in)

1. **§4.2 surface scope (IMPORTANT):** removed "sidebar parcel list rows" — the current sidebar does not render parcel rows. v1 targets parcel popups only.
2. **§4.5 target lat/lng resolution (IMPORTANT):** added explicit rule for resolving target coordinates once on workspace load + UI behavior during async resolution.
3. **§5.7 measure-mode click suppression (IMPORTANT, new section):** added explicit central-gate enforcement requirement + validation case that no popup opens from any click path during measure mode.
4. ~~§5.2 explicit Clear button (nice-to-have)~~ — reverted in v1.2 per KK.
5. **§5.5 Shift-click non-snap modifier (nice-to-have):** noted as deferred behavior.
6. **§4.3 star icon (nice-to-have):** emoji acceptable for v1; SVG migration noted as deferred polish if preview feedback shows OS inconsistency.

---

## 1. Purpose

Add two related distance-measurement features to the lot-ledger map UI:

1. **Distance-to-target row** — passive display: every parcel in a workspace shows how far it is from the workspace's intended-target parcel (the gold star).
2. **Ruler tool** — interactive tool: click two map points (or parcels), see a line drawn between them with the distance labeled.

Both share the same underlying distance math (haversine great-circle). The distance-to-target row is a passive readout; the ruler tool is an active measurement interaction.

## 2. Non-goals

- Multi-leg path measurement (more than two points). YAGNI — investors measure A-to-B, not winding paths.
- Persisted measurements across sessions. Ephemeral only.
- Measurement comparison UI (multi-measurement side-by-side). Single measurement at a time for v1.
- Walking/driving routing or travel time. Straight-line only.
- Imperial/metric toggle. Imperial only (this is Texas; investors think in feet/miles).
- Saving measurements to a workspace. Out of scope.

## 3. Distance display format (shared by both features)

Auto-switching thresholds based on distance magnitude:

| Distance range | Display format |
|---|---|
| `< 500 ft` | `423 ft` |
| `500 ft to 1 mile (5,280 ft)` | `1,247 ft (0.24 mi)` |
| `≥ 1 mile` | `0.43 mi (2,270 ft)` |

Rationale:
- Investors think in feet for "same block / across the street" scale
- Investors think in miles for "across town / different neighborhood" scale
- Showing both in the middle band avoids cognitive lookup ("how far is 0.24 mi?")
- Comma-separating thousands (`2,270 ft`) for readability

Implementation: one shared `formatDistanceLabel(feet)` helper in JS. Both features call it.

## 4. Feature A — Distance-to-target row

### 4.1 Trigger

A parcel's distance-to-target row appears **only when both conditions are met**:

1. The current workspace has an intended-target parcel set (`saved_areas.originator_parcel_county` + `originator_parcel_account_num` populated)
2. The parcel being displayed has valid centroid lat/lng

If either condition isn't met (no target, no centroid), the row is hidden (not "—" or "unknown" — just absent).

### 4.2 Where the distance appears

**v1 scope: parcel detail surfaces only.** The current sidebar does NOT render parcel list rows, but the app does have two parcel-detail surfaces that analysts actively use.

| Surface | Existing location | New placement |
|---|---|---|
| **Parcel popup** (Leaflet popup on click) | `frontend/map.js` parcel popup builder | In the popup status row, right-aligned with status + price |
| **Parcel side panel** (rich parcel details opened by click) | `frontend/map.js` parcel panel builder | In the header meta row, right-aligned with status + price + delta |
| **Sidebar parcel list rows** | **DOES NOT EXIST in current UI** | Out of scope for v1 — defer to whenever a parcel-list surface gets built |
| **CSV export** | Covered by separate CSV refactor spec | Out of scope for this spec |

Use one shared frontend helper so both detail surfaces render the same label and target logic.

### 4.3 Status-row placement and content

The distance is rendered as a compact inline chip in the same horizontal meta/status line as the parcel status and price.

Examples:

```
⭐ 423 ft
⭐ 1,247 ft (0.24 mi)
⭐ Target parcel
```

Placement rules:

- **Popup:** second line (`popup-status-row`), right-aligned alongside the existing status + price
- **Side panel:** header meta row (`parcel-panel-header-meta`), right-aligned as the last item in the row

Where `⭐` is the same gold-star glyph already used for the intended-target visual indicator. Maintains visual continuity.

**Star icon: emoji for v1, SVG migration deferred.** Emoji `⭐` is acceptable for v1 — fast to ship, no asset work. If preview feedback shows OS-inconsistent rendering (Windows / Linux / macOS sometimes render emoji differently), migrate to an inline SVG matching the existing gold-star map marker styling in a follow-up.

### 4.4 The target parcel itself

The intended-target parcel shows a slightly different label:

```
⭐ Target parcel
```

(Distance 0 ft would be confusing — replace with explicit text.)

### 4.5 Compute timing

**Frontend** (`frontend/map.js`). Reasoning:

- The data needed (workspace target lat/lng + each parcel's centroid) is in the frontend's data structures (loaded with the workspace + parcel data)
- Haversine is ~10 lines of JS
- Avoids server round-trips on every popup open
- Recomputes for free if the user navigates to a different workspace

### 4.6 Target coordinate resolution

**Important nuance** (Copilot R1 finding): the workspace's target identity (`originator_parcel_county` + `originator_parcel_account_num`) is restored from saved area state BEFORE the target parcel's lat/lng is known. The lat/lng is currently resolved later by the star-render flow (around `map.js:2703`).

Without explicit handling, the distance row could stay hidden during the brief window where the workspace HAS a target but the target's coordinates haven't been resolved yet.

**Implementation rule:**

1. On workspace load: if `originator_parcel_county` + `originator_parcel_account_num` are set but no lat/lng is yet known, trigger a one-time parcel-detail fetch to resolve the coordinates.
2. Cache the resolved `target_lat` + `target_lng` into the current workspace state alongside the identity fields.
3. Once cached, distance computation can proceed for all parcel detail surfaces.
4. If a parcel detail surface is opened DURING resolution (before lat/lng arrives), show no distance chip — same as the "no target set" case. Don't show a "loading…" state for v1 — keeps the UI quiet rather than noisy with transient text. The reopen-after-resolve case is rare and the chip will appear correctly on the next open.

**UI behavior matrix:**

| Workspace has target identity? | Target lat/lng resolved? | Parcel has centroid? | Distance row shows? |
|---|---|---|---|
| No | — | — | Hidden |
| Yes | No (still resolving) | — | Hidden |
| Yes | Yes | No | Hidden |
| Yes | Yes | Yes | **Shown** with distance |

This avoids the "row should show but doesn't" race condition that would otherwise happen on the first popup after loading a workspace.

Shared helper:

```javascript
function haversineFeet(lat1, lng1, lat2, lng2) {
  const R_FEET = 20902231.6;  // Earth radius in feet
  const toRad = d => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a = Math.sin(dLat/2)**2 +
            Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
            Math.sin(dLng/2)**2;
  return 2 * R_FEET * Math.asin(Math.sqrt(a));
}

function formatDistanceLabel(feet) {
  if (feet < 500) return `${Math.round(feet)} ft`;
  if (feet < 5280) return `${feet.toLocaleString('en-US', {maximumFractionDigits: 0})} ft (${(feet/5280).toFixed(2)} mi)`;
  return `${(feet/5280).toFixed(2)} mi (${feet.toLocaleString('en-US', {maximumFractionDigits: 0})} ft)`;
}
```

## 5. Feature B — Ruler tool

### 5.1 Toggle and state

A toolbar button at the top of the map (next to the polygon-draw button) toggles "measure mode" on/off.

When measure mode is **on**:
- Map cursor changes (CSS `cursor: crosshair`)
- The measure-mode button is visually active (highlighted ring or filled background)
- Parcel-click behavior is suppressed (clicks don't open parcel popups in measure mode)
- Esc clears any in-progress measurement and keeps measure mode active

When measure mode is **off** (default):
- Standard map interaction (click parcels to open popups, etc.)

### 5.2 Measurement interaction

While measure mode is on:

| User action | System response |
|---|---|
| First click on map | Plant **start point** marker. If click was inside a parcel polygon, snap to that parcel's centroid; otherwise use the click point. |
| Second click on map | Plant **end point** marker (same parcel-snap behavior). Draw a line from start to end. Display distance label at the midpoint of the line. **Measurement complete.** |
| Third click | Treat as start of a **new measurement**: clear the previous line/markers, plant new start point. |
| **Esc** | **Clear the current measurement** (markers + line + label removed). **Stay in measure mode** — user can immediately take another measurement without re-entering. |
| Click on the measure-mode button again | **Exit measure mode** AND clear any in-flight measurement. |

**Two predictable behaviors:**
- **Esc = clear** (stay ready to measure again)
- **Toggle button = exit** (out of measure mode entirely)

No dedicated Clear button. Esc handles the "I want to start over but stay in measure mode" case. The toggle button handles the "I'm done measuring" case. Third-click-clears is still the fast inline path during continuous measurement.

### 5.3 Single-measurement mode

Only one measurement visible at a time. Subsequent measurements replace the previous. (Multi-measurement comparison is out of scope per §2.)

### 5.4 Visual styling

- **Start/end point markers:** Small circle markers, distinct color (suggest yellow or amber to distinguish from existing parcel/comp markers). Pixel size ~8px.
- **Connecting line:** Solid stroke, same yellow/amber color, ~3px width, dashed pattern to differentiate from polygon-draw lines.
- **Distance label:** Tooltip-style box at the midpoint of the line. White background, black text, slight shadow. Uses `formatDistanceLabel()`.

### 5.5 Snap-to-parcel behavior

When a click lands inside an existing parcel polygon:

1. Look up the parcel from the frontend's loaded parcel data
2. Use that parcel's centroid lat/lng as the actual measurement point
3. Visual marker still appears at the centroid (not the click point)

When a click lands outside any parcel polygon (empty area, road, undeveloped land):

- Use the literal click point as the measurement point. No snapping.

This gives the user both modes seamlessly:
- "Parcel A to parcel B" → snap-snap (centroid-to-centroid distance)
- "Across this empty field" → free-click measurements

**Deferred: Shift-click non-snap modifier.** A future enhancement: holding Shift while clicking inside a parcel polygon would force the literal click point (skip the snap). Useful for measuring "from this specific corner of a parcel" rather than centroid-to-centroid. Not in v1 — adds interaction complexity without clear demand. Documented here so we don't re-derive the idea later.

### 5.6 Leaflet plumbing

Existing infrastructure to leverage:

- **Leaflet is already loaded** (the whole app runs on it)
- **Leaflet-draw is already wired** for polygon mode (`L.Control.Draw` in `frontend/map.js` near line 372)
- Don't need leaflet-measure library — implement directly using `L.marker` + `L.polyline` + `L.tooltip` primitives

New code surface:

| Function / module | Purpose |
|---|---|
| `toggleMeasureMode()` | Enter/exit measure mode, toggle CSS cursor, button state |
| `handleMeasureClick(latlng)` | Process clicks during measure mode (start/end/restart logic) |
| `snapToParcelCentroid(latlng)` | Look up parcel at click → return centroid or null |
| `drawMeasurement(start, end)` | Render line + markers + label on the map |
| `clearMeasurement()` | Remove all measurement layers |

Estimate: ~80-120 lines of new JS in `frontend/map.js`, plus a button in `frontend/index.html` and styles in `frontend/style.css`.

### 5.7 Measure-mode click suppression — central enforcement gate

**Critical implementation rule** (Copilot R1 finding): the current frontend has multiple click entry paths into parcel-detail handling — not a single chokepoint. Specifically:

- `frontend/map.js:8205` — browse-layer hit-testing on click
- `frontend/map.js:8248` — another click path on parcel features
- `frontend/map.js:6092` — popup-open path

If we only intercept clicks in ONE of these paths, a parcel popup could still open from another path during measurement, breaking the "measure mode is exclusive" contract.

**Implementation requirement:**

1. Define a single boolean state variable: `_measureModeActive` (initially `false`)
2. Set it `true` on `toggleMeasureMode()` activation, `false` on deactivation
3. In EVERY click entry point that could open a parcel popup, add an early-return guard:
   ```javascript
   if (_measureModeActive) {
     handleMeasureClick(latlng);
     return;  // do not proceed to parcel-popup logic
   }
   ```
4. Or — preferred — centralize parcel-popup-opening behind a single `_openParcelDetailIfAllowed(parcel)` function that does the guard check internally. Then every click path calls that function instead of opening popups directly.

**Validation:** add an explicit manual test case (also called out in §8.2):

- Enter measure mode
- Click each of: a parcel polygon (Browse mode rendering), a marker, an analyze-mode rendering, a saved-parcel marker
- Confirm: NO parcel popup opens from any of these clicks while measure mode is active
- The only thing that happens on click is the measurement points get planted

If any click path bypasses the gate, the spec is violated and needs a fix before merge.

## 6. UI layout — where the button goes

The map currently has a toolbar / floating-button area near the top-right or top-left of the map canvas (Leaflet's default control position). **One button** gets added:

| Button | Icon | Title | Behavior |
|---|---|---|---|
| **Measure** | 📏 (or SVG ruler) | "Measure distance (Esc to clear)" | Click to enter measure mode. Click again (or press the toolbar button while active) to exit measure mode + clear any in-flight measurement. |

Behavior details:
- Sits adjacent to the polygon-draw button
- Active state visually distinct (filled background or border ring)
- Title attribute hints at Esc shortcut so users discover it

No dedicated Clear button per v1.2 — Esc handles the "clear, stay in mode" case directly.

If the existing toolbar structure doesn't have natural room, KK approves whether to add a new toolbar group or fold into an existing one during implementation review.

## 7. Edge cases

### 7.1 Distance-to-target with no centroid on the parcel

Some parcels in our DB have NULL `centroid` (condos, multi-units, recent imports not yet geocoded). When the parcel-to-target distance can't be computed:

- Hide the distance row entirely (don't show "Unknown distance" or "N/A")
- Existing parcel popup still works for everything else

### 7.2 Distance-to-target when workspace has no intended-target parcel set

Many older saved areas don't have an originator_parcel set (the feature shipped recently, pre-existing workspaces aren't backfilled). For these:

- Hide the distance row entirely
- No errors, just no distance section

### 7.3 Ruler mode + saved-area click

If the user is in measure mode and clicks on a saved-area marker (a polygon edge or workspace anchor), treat as a regular click point — don't trigger the "load this workspace" behavior while in measure mode.

### 7.4 Ruler mode + extreme distances

Haversine handles antipodal points correctly. For practical lot-ledger use (single county to single county), distances are <100mi, well within haversine's accurate range.

### 7.5 Cross-county distance

Both features should work cross-county within DFW (parcel in Dallas → target in Tarrant, etc.). The math doesn't care about county boundaries.

## 8. Testing / validation

### 8.1 Automated (frontend smoke test) — defer

The lot-ledger frontend doesn't have automated JS tests currently. Don't introduce a new test framework for this. Manual validation is the bar.

### 8.2 Manual validation steps

Run on the preview URL after deploy:

**Distance-to-target row:**
1. Load a saved workspace with an intended-target parcel (any saved area with the gold star)
2. Open the parcel popup for another parcel in that workspace
3. **Expected:** distance row appears with "⭐ X ft from target" or "X mi (Y ft) from target"
4. Open the popup for the target parcel itself
5. **Expected:** distance row shows "⭐ This is the target parcel"
6. Load a workspace without an intended-target set (pre-feature saved area)
7. **Expected:** no distance row at all in any popup

**Ruler tool:**
1. Click measure-mode button — cursor changes, button highlights
2. Click on a parcel A — marker drops at A's centroid
3. Click on a parcel B — marker drops at B's centroid, line drawn, distance label visible
4. Click on parcel C — measurement clears, new start point at C (third-click-clears)
5. Click on an empty grass area — measurement clears, new start point at click location (no snap)
6. Click on parcel D — line drawn, distance label visible
7. **Press Esc** — measurement clears (markers + line + label gone) but **measure mode stays on** (cursor still crosshair, button still highlighted)
8. Click parcel A again — fresh start point planted (proves measure mode survived the Esc-clear)
9. **Click measure-mode button** — measure mode exits + any in-flight measurement clears
10. Click measure-mode button again — re-enters measure mode (clean state)

**Ruler tool — click suppression test (§5.7):**
1. Enter measure mode
2. Click each of the following surfaces, one per attempt:
   - A parcel polygon in browse mode
   - A parcel marker in analyze mode
   - A saved-parcel marker (gold star)
   - A Propelio comp marker (red/green dot)
3. **Expected for every one:** NO parcel popup opens; only the measurement points get planted
4. **If any click opens a popup or comp panel**: measure-mode central-gate is incomplete — spec violation, fix before merge

### 8.3 Distance-correctness check

Pick two parcels with known addresses about 1 mile apart on the map. Compare the ruler tool's reading to Google Maps' distance measurement. Should agree within ~1% (Google Maps and haversine use slightly different earth models but both are accurate for short distances).

## 9. Branch + commit strategy

- Lives on **`feat/preview-bundle-2026-05-14`** (same branch as the OAC count badge fix)
- This branch becomes the "preview bundle" of multiple small UX additions before merging to develop together
- Single commit per feature suggested:
  - `feat(map): distance-to-target row in parcel popups and sidebar`
  - `feat(map): ruler tool for arbitrary distance measurements`
- After Copilot implements + review passes, redeploy to preview with `gcloud builds submit --config cloudbuild-preview.yaml --project=lot-ledger`

## 10. Out of scope (parked for later)

- CSV export integration (separate refactor spec in `docs/lot-ledger/CSV_EXPORT_REFACTOR_BRAINSTORM_WIP.md`)
- Multi-leg path measurement (3+ points)
- Persisted measurements
- Measurement comparison UI (showing multiple measurements simultaneously)
- Imperial/metric toggle
- Walking/driving route distance
- Server-side distance pre-computation for the parcel list (computed on render is fine for now)

## 11. Copilot review — outcomes (2026-05-15)

### Round 1 (v1 → v1.1)

Three IMPORTANTs and four nice-to-haves. No blockers. All addressed in v1.1.

| # | Section | Severity | Resolution |
|---|---|---|---|
| 1 | §4.2 | IMPORTANT | Spec claimed "sidebar parcel list rows" but current sidebar doesn't render a parcel list — only count badges and sold-comp panel. Reworded to **popup-only for v1**; sidebar deferred to whenever a parcel-list surface gets built. |
| 2 | §4.5 → §4.6 | IMPORTANT | Target identity is restored before lat/lng resolution (the star-render flow resolves coords later). Without explicit rule, distance row would silently hide during the resolution window. Added §4.6 with explicit resolution rule + UI behavior matrix. |
| 3 | §5.7 (new) | IMPORTANT | Multiple click entry points (map.js:8205, 8248, 6092) means measure-mode click suppression can't live in one place. Added §5.7 with central-gate requirement + new validation case in §8.2 confirming no popup opens from any click path during measure mode. |
| 4 | §5.2 | NICE-TO-HAVE | Added explicit Clear button next to the measure-mode button (visible only when there's an active measurement). Stays in measure mode (unlike Esc which exits). Addresses Esc-only discoverability concern. |
| 5 | §5.5 | NICE-TO-HAVE | Noted Shift-click-for-no-snap as deferred enhancement so we don't re-derive the idea. |
| 6 | §4.3 | NICE-TO-HAVE | Emoji `⭐` acceptable for v1 (fast to ship). Inline SVG migration deferred to follow-up if preview feedback shows OS-inconsistent rendering. |
| 7 | §5.6 | (confirmed) | Stay with Leaflet primitives — no measurement plugin needed. Current map plumbing is custom and stable. |

Copilot R1 verdict: with §4.2 / §4.6 / §5.7 addressed, **build-eligible**. No further review round needed for the spec — moves to implementation next.

### Round 1.5 (KK refinement, v1.1 → v1.2)

KK pushed back on Copilot's "add dedicated Clear button" nice-to-have:

> "the x button near clear now could just do dual purpose and maybe esc button clears it instead of adding a whole new button for clear"

| # | Section | Resolution |
|---|---|---|
| KK-1 | §5.2 + §6 | Removed dedicated Clear button. Esc now clears + stays in measure mode (was: clear + exit). Toggle button still exits. Two predictable behaviors, one fewer button. Better discoverability via the title attribute "Measure distance (Esc to clear)". |

This is what shipped to implementation.
