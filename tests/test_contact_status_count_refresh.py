"""tests/test_contact_status_count_refresh.py
Role: Regression guard for live Contact Status count badge refresh.

Mike report 2026-06-05: typing a date in the popup or running a CSV
import didn't bump the Contact Status filter count badge — the blue
phone overlay refreshed but the sidebar number stayed stale until
something else triggered a re-render. Root cause: three outreach-
mutation sites (popup edit / CSV import / SSE filter apply) all
called _rebuildOutreachOverlays() but not _updateMergedSidebarCounts(),
which is what writes the badge text.

Connects to: frontend/map.js mutation sites (_putOutreachField,
outreach-import-file change handler, _writeFilterFieldDirect).
"""
from __future__ import annotations

import re
from pathlib import Path


MAP_JS = Path(__file__).resolve().parent.parent / "frontend" / "map.js"


def _read() -> str:
    return MAP_JS.read_text()


def test_popup_outreach_edit_refreshes_count_badge() -> None:
    """After _putOutreachField mutates a feature's outreach props locally,
    the Contact Status count badge must refresh. Without this, Mike sees
    Yes/date land on the parcel but the sidebar number stays 0."""
    src = _read()
    pat = re.compile(
        r"_updateLocalFeatureOutreach\(county, parcelId, field, value\);\s*"
        r"_rebuildOutreachOverlays\(\);.*?"
        r"_updateMergedSidebarCounts\(\);",
        re.DOTALL,
    )
    assert pat.search(src), (
        "_putOutreachField must call _updateMergedSidebarCounts() after "
        "_rebuildOutreachOverlays() — without it the count badge stays "
        "stale on popup edits."
    )


def test_csv_import_commit_refreshes_count_badge() -> None:
    """CSV import commit path must refresh the count badge after the
    per-row local updates + overlay rebuild. Comments may sit between
    the calls — match loosely on the three-helper sequence."""
    src = _read()
    # The CSV import block is the only place where these three helpers
    # appear in sequence inside the commit-success try-block.
    pat = re.compile(
        r"applyMapVisibilityFilters\(\);.*?"
        r"_rebuildOutreachOverlays\(\);.*?"
        r"_updateMergedSidebarCounts\(\);.*?"
        r"\}\s*catch \(err\) \{\s*console\.error\(\"\[outreach-import\] failed\"",
        re.DOTALL,
    )
    assert pat.search(src), (
        "CSV import commit handler must call _updateMergedSidebarCounts() "
        "between _rebuildOutreachOverlays() and the outer catch — without "
        "it a successful import leaves the Contact Status count stale "
        "until the next filter toggle."
    )


def test_sse_filter_apply_refreshes_count_badge() -> None:
    """When filter state arrives via SSE from another tab/user, the count
    badges must refresh too. Mirrors the popup-edit + CSV-import pattern."""
    src = _read()
    pat = re.compile(
        r"applyMapVisibilityFilters\(\); \} catch \(_\) \{\}\s*"
        r"try \{ _rebuildOutreachOverlays\(\); \} catch \(_\) \{\}\s*"
        r"// Mirror the popup-edit \+ CSV-import fix.*?"
        r"_updateMergedSidebarCounts\(\)",
        re.DOTALL,
    )
    assert pat.search(src), (
        "SSE filter-apply path must call _updateMergedSidebarCounts() "
        "so count badges stay in sync with remote filter changes."
    )


def test_clear_button_clears_outreach_overlay_layer() -> None:
    """Mike report 2026-06-05: blue phone overlays stayed on the map
    after hitting Clear. clearDrawResults() clears every other live-
    overlay layer (CAD ratings, propelio comps, redfin, sold, badges) —
    the outreach overlay must be in that list too, otherwise icons
    linger at coordinates with no underlying parcel context."""
    src = _read()
    # The fix sits right after cadRatingLayer + propelioCompLayer clears
    pat = re.compile(
        r"propelioCompLayer\.clearLayers\(\);\s*"
        r"cadRatingLayer\.clearLayers\(\);.*?"
        r"outreachOverlayLayer\.clearLayers\(\);\s*"
        r"outreachOverlayLayerByKey\.clear\(\);\s*"
        r"outreachOverlayGeomSeen\.clear\(\);",
        re.DOTALL,
    )
    assert pat.search(src), (
        "clearDrawResults must wipe outreachOverlayLayer + "
        "outreachOverlayLayerByKey + outreachOverlayGeomSeen alongside the "
        "other live-overlay layers it already clears."
    )
