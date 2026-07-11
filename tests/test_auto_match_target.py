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
