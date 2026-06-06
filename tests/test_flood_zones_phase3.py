"""tests/test_flood_zones_phase3.py
Role: Inspection guards for Phase 3 — frontend overlay + popup row + toolbar.

Asserts:
  - Flood toggle button registered in the map toolbar (programmatic, not
    static HTML, per Copilot critique)
  - L.geoJSON layer + style function with severity gradient
  - 250ms debounced moveend refetch
  - Custom Leaflet pane below parcels, above basemap (z-index 410)
  - Popup builder includes Flood Zone row with verbose plain-English
  - Toggle off clears the layer
  - Unknown-zone fallback style present

Connects to: frontend/map.js
"""
from __future__ import annotations

import re
from pathlib import Path


MAP_JS = Path(__file__).resolve().parent.parent / "frontend" / "map.js"


def _read() -> str:
    return MAP_JS.read_text()


def test_toolbar_flood_button_defined_programmatically() -> None:
    """Per Copilot critique fold (spec decision #20): the toggle is
    defined in the map.js control panel, NOT as static HTML in
    index.html (which would create dead UI)."""
    src = _read()
    assert 'floodBtn.id = "btn-flood-toggle"' in src
    assert 'floodBtn.textContent = "FLOOD"' in src
    assert "toggleFloodZonesLayer()" in src


def test_toolbar_flood_button_lives_next_to_hoa() -> None:
    """Place FLOOD next to HOA so the related-overlay toggles cluster."""
    src = _read()
    # hoaBtn lines must appear before floodBtn lines
    hoa_idx = src.find('hoaBtn.id = "btn-hoa-toggle"')
    flood_idx = src.find('floodBtn.id = "btn-flood-toggle"')
    assert hoa_idx > 0 and flood_idx > 0
    assert hoa_idx < flood_idx, "FLOOD button must be defined after HOA button"


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


def test_style_function_floodway_distinct_color() -> None:
    """FLOODWAY = darkest red with bold outline so Mike can't miss it."""
    src = _read()
    pat = re.compile(
        r'if \(subty === "FLOODWAY"\).*?fillColor: "#8B0000".*?fillOpacity: 0\.50',
        re.DOTALL,
    )
    assert pat.search(src)


def test_style_function_ae_a_v_red() -> None:
    """All regulatory 100-yr SFHA zones share the red palette."""
    src = _read()
    pat = re.compile(
        r'zone === "AE" \|\| zone === "A" \|\| zone === "AH" \|\| '
        r'zone === "AO" \|\| zone === "V" \|\| zone === "VE".*?'
        r'fillColor: "#DC2626".*?fillOpacity: 0\.35',
        re.DOTALL,
    )
    assert pat.search(src)


def test_style_function_x_shaded_amber() -> None:
    """X-shaded (500-yr floodplain) = amber, lower opacity than SFHA."""
    src = _read()
    pat = re.compile(
        r'zone === "X" && subty\.indexOf\("0\.2 PCT"\) !== -1.*?'
        r'fillColor: "#F59E0B".*?fillOpacity: 0\.25',
        re.DOTALL,
    )
    assert pat.search(src)


def test_style_function_x_unshaded_minimal() -> None:
    """X-unshaded (minimal risk) = barely visible so it doesn't clutter."""
    src = _read()
    pat = re.compile(
        r'zone === "X"\) \{.*?fillOpacity: 0\.05',
        re.DOTALL,
    )
    assert pat.search(src)


def test_style_function_unknown_zone_fallback() -> None:
    """Defensive: any FEMA zone code not in the palette (future additions,
    D zones, B/C legacy codes) gets a gray outline so rendering doesn't
    silently break."""
    src = _read()
    # The fallback return at the end of _floodZoneStyle
    pat = re.compile(
        r'function _floodZoneStyle\(feature\).*?'
        r'return \{ color: "#6b7280".*?fillColor: "#6b7280"',
        re.DOTALL,
    )
    assert pat.search(src)


def test_moveend_debounced_250ms() -> None:
    """Leaflet fires moveend on every pan + zoom step. 250ms debounce
    catches the 'user paused' moment without firing 10 fetches per drag."""
    src = _read()
    pat = re.compile(
        r"_floodZonesFetchTimer = setTimeout\(_refetchFloodZonesForViewport, 250\)",
    )
    assert pat.search(src)


def test_moveend_listener_registered() -> None:
    src = _read()
    assert 'map.on("moveend", _scheduleFloodZonesRefetch)' in src


def test_refetch_skips_when_layer_invisible() -> None:
    """The handler must self-gate on floodZonesVisible so it's a cheap
    no-op when the toggle is OFF (default)."""
    src = _read()
    pat = re.compile(
        r"async function _refetchFloodZonesForViewport\(\) \{\s*"
        r"if \(!floodZonesVisible\) return;",
        re.DOTALL,
    )
    assert pat.search(src)


def test_refetch_skips_when_same_bbox() -> None:
    """Don't refetch if user pans inside the cached extent."""
    src = _read()
    assert "_floodZonesLastBboxKey" in src
    pat = re.compile(
        r"if \(bboxStr === _floodZonesLastBboxKey\) return",
    )
    assert pat.search(src)


def test_toggle_off_clears_layer() -> None:
    """Toggle OFF must removeLayer + clearLayers + reset bbox key so the
    next toggle-on triggers a fresh fetch."""
    src = _read()
    pat = re.compile(
        r"async function toggleFloodZonesLayer\(\).*?"
        r"if \(floodZonesVisible\) \{.*?"
        r"map\.removeLayer\(floodZonesLayer\).*?"
        r"floodZonesLayer\.clearLayers\(\).*?"
        r'_floodZonesLastBboxKey = "".*?'
        r"floodZonesVisible = false",
        re.DOTALL,
    )
    assert pat.search(src)


def test_popup_includes_flood_zone_row() -> None:
    """Popup CAD section must include a Flood Zone row just above the
    Parcel ID row."""
    src = _read()
    # Flood Zone row IIFE must call _buildParcelDetailTableRow with
    # "Flood Zone" as the label
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


def test_layer_uses_custom_pane() -> None:
    """L.geoJSON must use the floodZonesPane so the z-index is honored."""
    src = _read()
    pat = re.compile(
        r"floodZonesLayer = L\.geoJSON\(null, \{\s*pane: \"floodZonesPane\"",
    )
    assert pat.search(src)


def test_layer_has_tooltip_with_label() -> None:
    """Hover tooltip on each flood polygon shows the verbose label."""
    src = _read()
    pat = re.compile(
        r"const lbl = _floodZoneFeatureLabel\(feature\?\.properties\);",
    )
    assert pat.search(src)
    assert "layer.bindTooltip(lbl" in src
