"""tests/test_flood_zones_phase3.py
Role: Inspection guards for Phase 3 — frontend overlay + popup row + toolbar.

NOTE 2026-06-06: This file was rewritten when Phase 3 pivoted from a live
GET /api/flood-zones endpoint (with L.geoJSON + bbox-refetch on moveend)
to PMTiles via protomaps-leaflet. The Phase 3 assertions kept here cover
the new PMTiles wiring + the unchanged popup row + the unchanged toolbar
toggle. The deleted L.geoJSON-specific assertions (debounce timer, bbox
memo, etc.) are gone with the code.

Asserts:
  - Flood toggle button registered programmatically in the map toolbar
    (not static HTML, per Copilot critique)
  - FLOOD_PMTILES_URL points at the GCS-hosted .pmtiles
  - protomapsL.leafletLayer used (PMTiles), NOT L.geoJSON (deleted)
  - Severity-driven paint rules (fill + stroke functions read feature.props.severity)
  - Custom Leaflet pane below parcels, above basemap (z-index 410)
  - Popup builder includes Flood Zone row with verbose plain-English wording
  - Toggle off removes the layer (no clearLayers needed for protomaps tile layer)

Connects to: frontend/map.js
"""
from __future__ import annotations

import re
from pathlib import Path


MAP_JS = Path(__file__).resolve().parent.parent / "frontend" / "map.js"


def _read() -> str:
    return MAP_JS.read_text()


# ---- Toolbar -------------------------------------------------------------


def test_toolbar_flood_toggle_lives_in_lyrs_popover() -> None:
    """LYRS dropdown introduced 2026-06-06 after the chevron started
    overlapping HOA/FLOOD/CNTY when those were 3 stacked buttons. The
    flood toggle is now inside the LYRS popover with id=btn-flood-toggle
    so the existing toggleFloodZonesLayer() code keeps working unchanged."""
    src = _read()
    assert 'lyrsBtn.id = "btn-layers-toggle"' in src, (
        "Top-level LYRS toolbar button missing."
    )
    assert 'lyrsBtn.textContent = "LYRS"' in src
    # The popover HTML must contain a row with the flood toggle id.
    assert 'id="btn-flood-toggle"' in src, (
        "Popover row for flood toggle missing — would break "
        "toggleFloodZonesLayer's getElementById call."
    )
    # The flood row's click handler must call toggleFloodZonesLayer.
    pat = re.compile(
        r'lyrsPopover\.querySelector\("#btn-flood-toggle"\).*?'
        r"toggleFloodZonesLayer\(\)",
        re.DOTALL,
    )
    assert pat.search(src), (
        "LYRS popover click handler for flood row must invoke "
        "toggleFloodZonesLayer."
    )


def test_lyrs_popover_includes_hoa_and_county_too() -> None:
    """All 3 overlay toggles live in the popover so the toolbar stays
    short and future overlays slot in here instead of growing taller."""
    src = _read()
    assert 'id="btn-hoa-toggle"' in src
    assert 'id="btn-county-toggle"' in src
    assert 'id="btn-flood-toggle"' in src
    # All 3 togglers wired to their respective functions inside the
    # popover's row click handlers.
    assert "toggleHoaLayer()" in src
    assert "toggleCountyLayer()" in src
    assert "toggleFloodZonesLayer()" in src


def test_lyrs_popover_active_state_reflects_any_overlay() -> None:
    """The LYRS button itself shows .active when ANY overlay is on, so
    the user can see something is enabled even with the popover closed."""
    src = _read()
    pat = re.compile(
        r"function _refreshLyrsActiveState\(\).*?"
        r"const anyOn = hoaVisible \|\| floodZonesVisible \|\| countyVisible",
        re.DOTALL,
    )
    assert pat.search(src), "_refreshLyrsActiveState must OR the 3 visibility flags."


def test_lyrs_popover_closes_on_outside_click() -> None:
    src = _read()
    assert 'document.addEventListener("mousedown"' in src
    assert "_closeLyrsPopover" in src


# ---- Pane ---------------------------------------------------------------


def test_flood_zones_pane_below_parcels_above_basemap() -> None:
    """Z-index 410: above default tile pane (200) + default overlay (400),
    below parcel polygons (620+). Lets parcel colors stay visible on top
    while flood fills tint the surrounding map."""
    src = _read()
    pat = re.compile(
        r'map\.createPane\("floodZonesPane"\).*?'
        r'getPane\("floodZonesPane"\)\.style\.zIndex = "410"',
        re.DOTALL,
    )
    assert pat.search(src)


def test_flood_zones_pane_is_non_interactive() -> None:
    """pointerEvents=none so clicks pass through to parcel polygons below."""
    src = _read()
    pat = re.compile(
        r'getPane\("floodZonesPane"\)\.style\.pointerEvents = "none"',
    )
    assert pat.search(src)


# ---- PMTiles wiring -----------------------------------------------------


def test_uses_protomaps_leaflet_layer_not_geojson() -> None:
    """Visual overlay must come from PMTiles (browser pulls ~10KB tiles
    directly from GCS, zero per-request DB work). L.geoJSON + live API
    OOM'd Cloud Run at wide zoom."""
    src = _read()
    assert "floodZonesLayer = protomapsL.leafletLayer({" in src, (
        "Visual overlay must use protomapsL.leafletLayer (PMTiles), not L.geoJSON."
    )


def test_flood_pmtiles_url_points_at_gcs() -> None:
    """Tile URL must point at the GCS-hosted .pmtiles file built by
    scripts/build_flood_pmtiles.py."""
    src = _read()
    assert "FLOOD_PMTILES_URL" in src
    assert "https://storage.googleapis.com/lot-ledger-tiles/flood_zones.pmtiles" in src


def test_paint_rules_use_severity_numeric() -> None:
    """The build script bakes a numeric `severity` field (1-5) into each
    tile feature so the symbolizer is a fast switch on a number instead
    of a string comparison per polygon."""
    src = _read()
    assert "feature?.props?.severity" in src
    # Severity 5 (FLOODWAY) → dark red
    assert "if (sev === 5)" in src
    assert 'return "rgba(139,0,0' in src
    # Severity 4 (AE / A / V) → red
    assert "if (sev === 4)" in src
    assert 'return "rgba(220,38,38' in src
    # Severity 3 (X-shaded / 500-yr) → amber
    assert "if (sev === 3)" in src
    assert 'return "rgba(245,158,11' in src


def test_paint_rules_use_polygon_symbolizer_per_feature() -> None:
    """protomapsL.PolygonSymbolizer with perFeature: true so the fill/stroke
    fns receive each feature individually."""
    src = _read()
    pat = re.compile(
        r"symbolizer: new protomapsL\.PolygonSymbolizer\(\{.*?"
        r"fill: _floodZoneFillByFeature.*?"
        r"stroke: _floodZoneStrokeByFeature.*?"
        r"perFeature: true",
        re.DOTALL,
    )
    assert pat.search(src)


def test_paint_rules_target_flood_zones_data_layer() -> None:
    """The tippecanoe build uses `-l flood_zones` so the dataLayer name
    on the frontend must match."""
    src = _read()
    assert 'dataLayer: "flood_zones"' in src


def test_pmtiles_layer_uses_custom_pane() -> None:
    """L.geoJSON used `pane:` directly. protomapsL.leafletLayer must do
    the same so the z-index is honored."""
    src = _read()
    pat = re.compile(
        r'protomapsL\.leafletLayer\(\{[^}]*pane: "floodZonesPane"',
        re.DOTALL,
    )
    assert pat.search(src)


def test_pmtiles_layer_disables_pointer_events() -> None:
    """Match the browse layer pattern: disable pointer events on the
    canvas so parcel popups still receive clicks."""
    src = _read()
    pat = re.compile(
        r"floodZonesLayer\.getContainer && floodZonesLayer\.getContainer\(\).*?"
        r'_container\.style\.pointerEvents = "none"',
        re.DOTALL,
    )
    assert pat.search(src)


# ---- Toggle behavior ----------------------------------------------------


def test_toggle_off_removes_layer() -> None:
    """Toggle OFF removes the layer from the map. Unlike L.geoJSON we
    don't need clearLayers — protomaps re-renders on next addTo."""
    src = _read()
    pat = re.compile(
        r"if \(floodZonesVisible && floodZonesLayer\) \{\s*"
        r"map\.removeLayer\(floodZonesLayer\);\s*"
        r"floodZonesVisible = false",
        re.DOTALL,
    )
    assert pat.search(src)


def test_toggle_on_creates_layer_once_then_reuses() -> None:
    """First toggle-on creates the protomapsL layer; subsequent toggle-ons
    just re-add the existing layer."""
    src = _read()
    pat = re.compile(
        r"if \(!floodZonesLayer\) \{\s*"
        r"floodZonesLayer = protomapsL\.leafletLayer\(",
        re.DOTALL,
    )
    assert pat.search(src)
    assert "floodZonesLayer.addTo(map)" in src


# ---- Popup row ----------------------------------------------------------


def test_popup_includes_flood_zone_row() -> None:
    """Popup CAD section must include a Flood Zone row just above the
    Parcel ID row."""
    src = _read()
    pat = re.compile(
        r'_buildParcelDetailTableRow\("Flood Zone",',
    )
    assert pat.search(src), "Popup builder missing Flood Zone row"


def test_popup_floodway_warning_distinct() -> None:
    """Popup must show 'FLOODWAY (no build)' text so Mike can't miss it."""
    src = _read()
    assert '`${_fz} — FLOODWAY (no build)`' in src


def test_popup_ae_bfe_formatting() -> None:
    """AE rows with a known BFE display 'AE (BFE 502.3 ft)'."""
    src = _read()
    pat = re.compile(
        r'`\$\{_fz\} \(BFE \$\{Number\(_bfe\)\.toFixed\(1\)\} ft\)`',
    )
    assert pat.search(src)


def test_popup_x_shaded_500yr_label() -> None:
    src = _read()
    assert '"X — 500-yr floodplain"' in src


def test_popup_x_unshaded_minimal_risk_label() -> None:
    src = _read()
    assert '"X — minimal risk"' in src


# ---- Old live-API code paths are gone ----------------------------------


def test_no_live_api_refetch_logic() -> None:
    """The old debounced moveend bbox-refetch logic must be gone. The
    PMTiles tile layer handles viewport changes automatically — no manual
    fetch needed."""
    src = _read()
    assert "_refetchFloodZonesForViewport" not in src, (
        "Live-API refetch code must be deleted — PMTiles handles viewport."
    )
    assert "_floodZonesLastBboxKey" not in src, (
        "bbox memo is unused now that we don't fetch by bbox."
    )
    assert '"/api/flood-zones?bbox=' not in src, (
        "Frontend must not call the deleted /api/flood-zones endpoint."
    )
