"""Source-inspection guards for the Auto-Match Target comp POC (2026-07-11).

Verifies the structural hooks in frontend/map.js + frontend/index.html that
implement the "Auto-match target" checkbox: the comma-safe subject parser,
the ±band computation, the ephemeral state model, snapshot-restore, and the
reset / view-switch / manual-edit state machine. No browser, no DB — mirrors
tests/test_neighborhood_comp_filter.py.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP_JS = ROOT / "frontend" / "map.js"
INDEX_HTML = ROOT / "frontend" / "index.html"


def _map() -> str:
    return MAP_JS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _fn_body(src: str, pattern: str) -> str:
    """Extract a brace-balanced body starting at the first match of `pattern`."""
    m = re.search(pattern, src)
    assert m, f"Could not find block matching {pattern!r}"
    start = m.start()
    depth = 0
    i = src.index("{", start)
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    return src[start:]


# ── Task 1: pure logic + state ───────────────────────────────────────────────

def test_subject_parser_is_comma_safe() -> None:
    src = _map()
    assert "function _parseSubjectNum(" in src, "comma-safe subject parser missing"
    # SCOPE TO THE FUNCTION BODY. A whole-file search is vacuous: these exact
    # strings already exist via the _compMatchedTotVal precedent (map.js:8291),
    # so a file-wide assert would pass even a parseFloat regression. Guards
    # "2,450" -> 2450 (not 2). [Fable B-1]
    body = _fn_body(src, r"function _parseSubjectNum\(")
    assert 'replace(/[$,]/g, "")' in body
    assert "match(/^[\\d.]+/)" in body
    assert "parseFloat(" not in body


def test_no_naive_parse_on_subject_dims() -> None:
    src = _map()
    assert "function _autoMatchSubjectDims(" in src
    body = _fn_body(src, r"function _autoMatchSubjectDims\(")
    assert "_parseSubjectNum(" in body, "dims must resolve via the comma-safe helper"
    assert "parseFloat(" not in body, "never parseFloat the formatted subject strings"


def test_band_constant_and_helper() -> None:
    src = _map()
    assert "AUTO_MATCH_BAND = 0.2" in src
    assert "function _autoMatchBand(" in src


def test_no_43560_in_automatch_helpers() -> None:
    src = _map()
    for fn in ("_autoMatchSubjectDims", "_autoMatchBand", "_writeAutoMatchBands"):
        body = _fn_body(src, rf"function {fn}\(")
        assert "43560" not in body, f"{fn} must not do a units conversion (already acres)"


def test_ephemeral_state_vars_declared() -> None:
    src = _map()
    assert "let _autoMatchOn = false" in src
    assert "let _autoMatchSnapshot = null" in src
    assert "let _lastSubjectProps = null" in src


# ── Task 2: UI control + availability ────────────────────────────────────────

def test_checkbox_markup_present() -> None:
    html = _html()
    assert 'id="prop-auto-match"' in html, "auto-match checkbox missing from index.html"
    assert "Auto-match target" in html, "label text missing"
    # [Copilot] Honest "rough preset, not a comp recommendation" disclosure must
    # ride with the control — protects credibility, sharpens the upsell wedge.
    assert "preset" in html.lower(), "rough-preset disclosure microcopy missing"


def test_label_has_no_sparkle() -> None:
    html = _html()
    # Rung 0 is arithmetic — reserve the ✨ for the rung that earns it. Check the
    # ~500 chars around the control, not the whole file.
    idx = html.index('id="prop-auto-match"')
    window = html[max(0, idx - 500) : idx + 500]
    assert "✨" not in window, "no AI sparkle on the arithmetic rung"


def test_availability_refresh_defined() -> None:
    src = _map()
    assert "function _refreshAutoMatchAvailability(" in src
    body = _fn_body(src, r"function _refreshAutoMatchAvailability\(")
    assert ".disabled" in body, "must toggle .disabled on the checkbox"
    assert "Select a target property first" in body
    assert "Target has no lot/sqft data" in body


# ── Task 3: enable/disable/apply + hook ──────────────────────────────────────

def test_action_functions_defined() -> None:
    src = _map()
    for fn in ("_enableAutoMatch", "_disableAutoMatch", "_clearAutoMatchMode", "_applyAutoMatchIfEnabled"):
        assert f"function {fn}(" in src, f"{fn} missing"


def test_enable_snapshots_before_writing() -> None:
    src = _map()
    body = _fn_body(src, r"function _enableAutoMatch\(")
    assert "_autoMatchSnapshot = {" in body, "must snapshot current values at enable"
    assert "_writeAutoMatchBands(" in body
    assert "applyPropelioClientFilters()" in body


def test_disable_restores_snapshot() -> None:
    src = _map()
    body = _fn_body(src, r"function _disableAutoMatch\(")
    assert "_autoMatchSnapshot" in body
    assert "prop-lot-min" in body and "prop-sqft-min" in body, "must restore the four inputs"


def test_clear_mode_does_not_restore_or_apply() -> None:
    src = _map()
    body = _fn_body(src, r"function _clearAutoMatchMode\(")
    assert "_autoMatchOn = false" in body
    assert "_autoMatchSnapshot = null" in body
    # It only clears mode — must NOT re-apply or restore inputs.
    assert "applyPropelioClientFilters(" not in body


def test_subject_card_stashes_and_reapplies() -> None:
    src = _map()
    body = _fn_body(src, r"function _populateSubjectPropertyCard\(")
    assert "_lastSubjectProps = props || null" in body
    assert "_applyAutoMatchIfEnabled()" in body


def test_direct_apply_not_debounced_on_toggle() -> None:
    # Discrete toggles must feel instant — enable/disable call the direct apply.
    src = _map()
    for fn in ("_enableAutoMatch", "_applyAutoMatchIfEnabled"):
        body = _fn_body(src, rf"function {fn}\(")
        assert "applyPropelioClientFiltersDebounced" not in body


def test_checkbox_and_manual_edit_listeners_wired() -> None:
    src = _map()
    assert 'getElementById("prop-auto-match")' in src
    # Manual edit of a lot/sqft field while on turns auto off (user's value wins).
    assert "if (_autoMatchOn) _clearAutoMatchMode()" in src


# ── Task 4: reset / view-switch state machine ────────────────────────────────

def test_reset_clears_automatch_mode() -> None:
    src = _map()
    body = _fn_body(src, r"function resetPropelioFilters\(")
    assert "_clearAutoMatchMode()" in body, "Reset must clear auto-match mode"


def test_view_switch_and_area_load_clear_mode() -> None:
    # applyPropelioFilterStateToUI is the single choke for area load + ARV/NBV/
    # Export view switches; both stomp the four inputs, so both must clear mode.
    src = _map()
    body = _fn_body(src, r"function applyPropelioFilterStateToUI\(")
    assert "_clearAutoMatchMode()" in body


def test_automatch_not_persisted() -> None:
    # The MODE is ephemeral — must never leak into autosave/PATCH/another session.
    # [Fable R-2] Check the three realistic leak vectors, not just the default obj.
    src = _map()
    defaults = _fn_body(src, r"const DEFAULT_PROPELIO_FILTERS")
    assert "autoMatch" not in defaults and "prop-auto-match" not in defaults
    reads = _fn_body(src, r"function readPropelioFiltersFromUI\(")
    assert "autoMatch" not in reads and "prop-auto-match" not in reads
    capture = _fn_body(src, r"function captureFilterState\(")
    assert "autoMatch" not in capture and "prop-auto-match" not in capture


def test_apply_orchestrator_not_modified() -> None:
    # [Fable R-3] Global Constraint teeth: applyPropelioClientFilters must NOT
    # learn about auto-match (it is where the June regression shipped). All new
    # behavior is additive AROUND it.
    src = _map()
    body = _fn_body(src, r"function applyPropelioClientFilters\(")
    assert "automatch" not in body.lower()
