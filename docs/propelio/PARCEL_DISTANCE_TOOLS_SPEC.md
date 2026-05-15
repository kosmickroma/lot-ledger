# Parcel Distance Tools — Design Spec v1

**Status:** Draft pending Copilot deep-dive review, then implementation.
**Branch:** `feat/preview-bundle-2026-05-14` (adds to the OAC fix already on this branch — both ship to preview together)
**Author:** KK + Copilot (initial brainstorm) + Claude (refinement and spec, 2026-05-15)

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

Implementation: one shared `formatDistanceLabel(meters)` helper in JS. Both features call it.

## 4. Feature A — Distance-to-target row

### 4.1 Trigger

A parcel's distance-to-target row appears **only when both conditions are met**:

1. The current workspace has an intended-target parcel set (`saved_areas.originator_parcel_county` + `originator_parcel_account_num` populated)
2. The parcel being displayed has valid centroid lat/lng

If either condition isn't met (no target, no centroid), the row is hidden (not "—" or "unknown" — just absent).

### 4.2 Where the row appears

Three surfaces in the existing UI:

| Surface | Existing location | New row position |
|---|---|---|
| **Parcel popup** (Leaflet popup on click) | `frontend/map.js` parcel popup builder | After address, before owner section |
| **Sidebar parcel list rows** | The "parcels" panel in the sidebar | Compact form, next to the parcel address |
| **CSV export** (out of scope for this spec — covered by separate CSV refactor) | — | — |

For consistency, both popup + sidebar use the same `formatDistanceLabel()` helper.

### 4.3 Row content

Plain text in the existing styling, with target-star icon prefix to make it visually anchored:

```
⭐ 423 ft from target
```

Where `⭐` is the same gold-star glyph already used for the intended-target visual indicator. Maintains visual continuity.

### 4.4 The target parcel itself

The intended-target parcel shows a slightly different row:

```
⭐ This is the target parcel
```

(Distance 0 ft would be confusing — replace with explicit text.)

### 4.5 Compute timing

**Frontend** (`frontend/map.js`). Reasoning:

- The data needed (workspace target lat/lng + each parcel's centroid) is already in the frontend's data structures (loaded with the workspace + parcel data)
- Haversine is ~10 lines of JS
- Avoids server round-trips on every popup open
- Recomputes for free if the user navigates to a different workspace

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
- Esc key exits measure mode and clears any in-progress measurement

When measure mode is **off** (default):
- Standard map interaction (click parcels to open popups, etc.)

### 5.2 Measurement interaction

While measure mode is on:

| User action | System response |
|---|---|
| First click on map | Plant **start point** marker. If click was inside a parcel polygon, snap to that parcel's centroid; otherwise use the click point. |
| Second click on map | Plant **end point** marker (same parcel-snap behavior). Draw a line from start to end. Display distance label at the midpoint of the line. **Measurement complete.** |
| Third click | Treat as start of a **new measurement**: clear the previous line/markers, plant new start point. |
| Esc | Cancel any in-progress measurement, clear all measurement layers, exit measure mode. |
| Click on the measure-mode button again | Same as Esc. |

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

## 6. UI layout — where the button goes

The map currently has a toolbar / floating-button area near the top-right or top-left of the map canvas (Leaflet's default control position). The "measure" button should:

- Sit adjacent to the polygon-draw button
- Use a ruler icon (📏 or SVG equivalent)
- Title attribute: "Measure distance"
- Active state: visually distinct (filled background or border ring)

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
4. Click on parcel C — measurement clears, new start point at C
5. Click on an empty grass area — measurement clears, new start point at click location (no snap)
6. Click on parcel D — line drawn, distance label visible
7. Press Esc — measurement clears, measure mode exits
8. Click measure-mode button again — re-enters measure mode

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

## 11. For Copilot's deep-dive review

Focus areas:

1. **§5.5 Snap behavior:** is "snap to parcel centroid when click is inside a polygon" the right default? Or should snapping be only on parcel-click via the parcel list / popup, not on map click?
2. **§5.2 Third-click behavior:** is "third click starts new measurement" intuitive enough, or should there be an explicit "clear" button + Esc-only-clears?
3. **§4.2 Sidebar row placement:** the sidebar parcel-list rows are dense already. Does adding a distance line make rows too tall? Should it be a compact inline icon + value like `⭐423` instead of full sentence?
4. **§4.3 / §5.4 Star icon:** is `⭐` emoji acceptable, or should it be an inline SVG to match the existing gold-star marker styling on the map?
5. **§5.6 Leaflet plumbing:** any existing leaflet-measure-style libraries that would save us implementing from primitives? (`leaflet-measure`, `leaflet-distance` — worth using or not given we already control the map config?)
6. **Anything else** that looks fragile or non-obvious before code lands.

Format findings as `BLOCKER` / `IMPORTANT` / `NICE-TO-HAVE`. Same iteration pattern as previous specs.
