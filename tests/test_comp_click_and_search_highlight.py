"""Inspection-based regression guards for the 2026-06-03 comp-click +
search-highlight bug fix.

Two bugs were reported by KK:

  Bug 1: comps list entry "1825 & 1827 Pollard Street" did nothing on
    click because _dedupCompsForRender picks one winner per parcel-geom
    and only the winner's layer is registered in propelioCompLayerByKey.
    flyToAndOpenPropelioComp short-circuited on the lookup miss.

  Bug 2: address-search autosuggest left the previous click-to-select
    purple outline on the map (two purples). The map-click handler
    cleared _searchHighlight already, but the search-bar path didn't
    clear _selectedOutlineLayer — asymmetric.

The fixes:

  - Extracted _compDedupKey helper from _dedupCompsForRender so both
    the dedup pass and the click fallback compute the same key.
  - Added a dedup-loser fallback in flyToAndOpenPropelioComp that
    walks registered winners to find one sharing the clicked comp's
    dedup key, and uses that winner's layer.
  - Added _clearSelectedOutline() call inside highlightSearchResult
    to mirror the map-click handler's symmetric cleanup.

These tests read frontend/map.js as source text and assert the fix
structure is intact. No DB, no browser.
"""
from __future__ import annotations

import re
from pathlib import Path

MAP_JS = Path(__file__).resolve().parent.parent / "frontend" / "map.js"


def _read() -> str:
    return MAP_JS.read_text()


def test_compDedupKey_helper_exists() -> None:
    """The extracted helper must exist as a standalone function."""
    src = _read()
    assert re.search(r"function _compDedupKey\(c\)", src), (
        "_compDedupKey helper not defined — the click fallback depends on it"
    )


def test_dedup_render_uses_extracted_helper() -> None:
    """_dedupCompsForRender must call _compDedupKey, not duplicate the logic.

    Catches future drift where someone re-inlines the key calculation in
    one place and forgets to update the other.
    """
    src = _read()
    # Find the _dedupCompsForRender body.
    m = re.search(
        r"function _dedupCompsForRender\(comps\) \{(.*?)\nfunction ",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate _dedupCompsForRender body"
    body = m.group(1)
    assert "_compDedupKey(c)" in body, (
        "_dedupCompsForRender must call _compDedupKey(c) — the dedup-loser "
        "click fallback depends on both using the same key logic. Don't "
        "inline the key calculation."
    )


def test_fly_to_comp_has_dedup_loser_fallback() -> None:
    """flyToAndOpenPropelioComp must fall back to finding a winner that
    shares the same dedup key when the direct layer lookup misses."""
    src = _read()
    m = re.search(
        r"async function flyToAndOpenPropelioComp\(compKey\) \{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate flyToAndOpenPropelioComp"
    body = m.group(1)
    # Must declare `let layer` (mutable) — old code used `const layer` which
    # would block the reassignment in the fallback path.
    assert "let layer = propelioCompLayerByKey.get(" in body, (
        "flyToAndOpenPropelioComp must use `let layer` so the fallback can "
        "reassign — old `const layer` blocks the fix."
    )
    # Must reference window._propelioLast to look up comp data.
    assert "window._propelioLast" in body, (
        "flyToAndOpenPropelioComp fallback must read from window._propelioLast "
        "to find the clicked comp's data"
    )
    # Must call _compDedupKey at least twice (once for the clicked comp,
    # once for each winner candidate).
    assert body.count("_compDedupKey(") >= 2, (
        "flyToAndOpenPropelioComp fallback must compute dedup keys for "
        "both the clicked comp AND each candidate winner. Found fewer than "
        "2 calls to _compDedupKey."
    )


def test_search_highlight_clears_click_to_select_outline() -> None:
    """highlightSearchResult must clear _selectedOutlineLayer in addition
    to clearing its own _searchHighlight. Symmetric with the map-click
    handler which clears _searchHighlight on its side."""
    src = _read()
    m = re.search(
        r"const highlightSearchResult = \(latlng\) => \{(.*?)\n\s*\};",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate highlightSearchResult"
    body = m.group(1)
    assert "_clearSelectedOutline()" in body, (
        "highlightSearchResult must call _clearSelectedOutline() so the "
        "click-to-select purple is removed when the user does an address "
        "search after clicking a parcel. Without this, both purples "
        "appear on the map simultaneously."
    )


def test_map_click_handler_still_clears_search_highlight() -> None:
    """Sanity: don't accidentally regress the OTHER side of the symmetry.
    Map-click should still clear search-highlight (it has done so since
    forever)."""
    src = _read()
    # The map-click handler clears search highlight at line ~12235 area.
    # We just need to confirm the line exists somewhere globally.
    assert "window._clearSearchHighlight?.()" in src, (
        "Map-click handler's _clearSearchHighlight cleanup is missing — "
        "this is the other side of the symmetry. Don't regress it."
    )
