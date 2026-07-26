"""Inspection-based regression guards for the 2026-07-25 Subject Property
row → zoom-to-parcel feature.

Clicking the Subject Property row above the Workspace block (top
active-item slot, `#active-item-target-row`) moves the map to that
parcel: fits the lot when the toolbar ZOOM toggle is on (`getClickMode()
=== "jump"`), or only pans if the parcel is off-screen when it's off
("stay") — never changing zoom in stay mode.

Key contract points this file guards:

  - The stay-mode zoom cap must be `maxZoom: map.getZoom()` (copied from
    the existing fitBounds-on-click pattern), NOT `Math.max(...)` — the
    saved-bookmark handler elsewhere uses `Math.max(map.getZoom(), 15)`,
    which zooms the user IN while in Keep View mode. That inconsistency
    must not be cloned here.
  - Bounds resolution is three-tiered: rendered outline layer →
    geometry cache → centroid. Skipping the geometry-cache tier would
    silently degrade "fit the lot" to a centroid guess whenever the
    user is zoomed out below the zoom-14 outline-render threshold.
  - A race guard (`_sameParcelIdentity`) must abort before touching the
    map if the staged subject changed while coords were resolving —
    mirrors the existing pattern at _refreshOriginatorTargetLabel /
    _ensureCurrentTargetParcelCoords.
  - The click/keydown listener must be wired exactly once, from a
    dedicated init function called at startup — NOT attached inside
    _setOriginatorTargetLabel, which runs on every label refresh and
    would stack duplicate listeners (N flyTos per click).

These tests read frontend/map.js and frontend/index.html as source text
and assert the fix structure is intact. No DB, no browser.
"""
from __future__ import annotations

import re
from pathlib import Path

MAP_JS = Path(__file__).resolve().parent.parent / "frontend" / "map.js"
INDEX_HTML = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


def _read_map_js() -> str:
    return MAP_JS.read_text()


def _read_index_html() -> str:
    return INDEX_HTML.read_text()


def _zoom_to_subject_property_body(src: str) -> str:
    m = re.search(
        r"async function _zoomToSubjectProperty\(\) \{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate _zoomToSubjectProperty"
    return m.group(1)


def test_zoom_to_subject_property_defined() -> None:
    """1. _zoomToSubjectProperty must be defined in frontend/map.js."""
    src = _read_map_js()
    assert re.search(r"async function _zoomToSubjectProperty\(\)", src), (
        "_zoomToSubjectProperty is not defined — the Subject Property row "
        "click handler is missing"
    )


def test_zoom_to_subject_property_gates_on_click_mode() -> None:
    """2. Must call getClickMode() — the ZOOM toggle actually gates it."""
    src = _read_map_js()
    body = _zoom_to_subject_property_body(src)
    assert "getClickMode()" in body, (
        "_zoomToSubjectProperty must call getClickMode() so the toolbar "
        "ZOOM toggle actually gates jump-vs-stay behavior"
    )


def test_zoom_to_subject_property_has_race_guard() -> None:
    """3. Must contain the _sameParcelIdentity race guard."""
    src = _read_map_js()
    body = _zoom_to_subject_property_body(src)
    assert "_sameParcelIdentity(" in body, (
        "_zoomToSubjectProperty must guard with _sameParcelIdentity so a "
        "subject switch mid-resolve doesn't stomp the map with a stale "
        "target's bounds"
    )


def test_zoom_to_subject_property_wires_both_bounds_tiers() -> None:
    """4. Must reference _subjectPropertyOutlineLayers AND
    _subjectPropertyGeometryCache (both bounds tiers wired, not just the
    easy one)."""
    src = _read_map_js()
    m = re.search(
        r"function _subjectPropertyBoundsFor\(identity\) \{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate _subjectPropertyBoundsFor"
    body = m.group(1)
    assert "_subjectPropertyOutlineLayers" in body, (
        "_subjectPropertyBoundsFor must check _subjectPropertyOutlineLayers "
        "(tier 1: rendered outline layer)"
    )
    assert "_subjectPropertyGeometryCache" in body, (
        "_subjectPropertyBoundsFor must check _subjectPropertyGeometryCache "
        "(tier 2) — without it, the feature silently degrades to a "
        "centroid guess whenever the user is zoomed out below the "
        "outline-render threshold"
    )


def test_stay_mode_uses_current_zoom_not_math_max() -> None:
    """5. Stay-mode branch uses maxZoom: map.getZoom(), not Math.max(.

    The saved-bookmark handler elsewhere uses
    Math.max(map.getZoom(), 15), which zooms the user IN while in Keep
    View mode. That is an existing inconsistency this feature must not
    clone.
    """
    src = _read_map_js()
    body = _zoom_to_subject_property_body(src)
    assert "maxZoom: map.getZoom()" in body, (
        "_zoomToSubjectProperty stay-mode branch must cap zoom at "
        "map.getZoom() — copied from the existing fitBounds-on-click "
        "pattern, not the saved-bookmark handler's Math.max variant"
    )
    assert "Math.max(" not in body, (
        "_zoomToSubjectProperty must not use Math.max(...) for the "
        "stay-mode zoom cap — that would zoom the user IN while in Keep "
        "View mode, cloning an existing inconsistency this feature is "
        "explicitly meant to avoid"
    )


def test_init_subject_property_row_zoom_defined_and_called() -> None:
    """6. initSubjectPropertyRowZoom is defined AND called."""
    src = _read_map_js()
    assert re.search(r"function initSubjectPropertyRowZoom\(\)", src), (
        "initSubjectPropertyRowZoom is not defined"
    )
    # The definition itself contains one call to _zoomToSubjectProperty
    # per listener (click + keydown); a real call site is a bare
    # `initSubjectPropertyRowZoom();` statement outside the definition.
    assert re.search(r"^initSubjectPropertyRowZoom\(\);", src, re.MULTILINE), (
        "initSubjectPropertyRowZoom is defined but never called at "
        "startup — the row would never become clickable"
    )


def test_index_html_row_has_button_semantics() -> None:
    """7. frontend/index.html row carries role="button" and tabindex="0"."""
    src = _read_index_html()
    m = re.search(r'<div id="active-item-target-row"[^>]*>', src)
    assert m, "couldn't locate #active-item-target-row in frontend/index.html"
    tag = m.group(0)
    assert 'role="button"' in tag, (
        "#active-item-target-row must carry role=\"button\" for "
        "accessibility now that it's clickable"
    )
    assert 'tabindex="0"' in tag, (
        "#active-item-target-row must carry tabindex=\"0\" so it's "
        "keyboard-reachable"
    )


def test_listener_not_attached_inside_set_originator_target_label() -> None:
    """8. The listener is NOT attached inside _setOriginatorTargetLabel.

    _setOriginatorTargetLabel runs on every label refresh; attaching the
    click listener there would stack duplicate listeners and fire N
    flyTos per click.
    """
    src = _read_map_js()
    m = re.search(
        r"function _setOriginatorTargetLabel\(addr\) \{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate _setOriginatorTargetLabel"
    body = m.group(1)
    assert "addEventListener" not in body, (
        "_setOriginatorTargetLabel must not attach any event listener — "
        "it runs on every label refresh, so a listener attached here "
        "would stack duplicates and fire N flyTos per click. Wire the "
        "click handler only in initSubjectPropertyRowZoom."
    )
