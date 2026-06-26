"""Source-inspection tests for the Chunk 2 neighborhood comp filter wiring.

Verifies that every structural hook added in Chunk 2 is present in
frontend/map.js: normalizeNbhd helper, DEFAULT_PROPELIO_FILTERS key,
readPropelioFiltersFromUI read, compPassesPropelioFilters gate (with both
normalized sides and the null-comp drop-out), and the restore/SSE plumbing.

No browser, no DB.
"""
from __future__ import annotations

import re
from pathlib import Path

MAP_JS = Path(__file__).resolve().parent.parent / "frontend" / "map.js"
STYLE_CSS = Path(__file__).resolve().parent.parent / "frontend" / "style.css"


def _src() -> str:
    return MAP_JS.read_text(encoding="utf-8")


def _css() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


def _fn_body(src: str, pattern: str) -> str:
    """Extract a function body starting at the first match of `pattern`."""
    m = re.search(pattern, src)
    assert m, f"Could not find function matching {pattern!r}"
    start = m.start()
    # Walk forward collecting braces to find the closing }
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


# ──────────────────────────────────────────────────────────────────────────────
# normalizeNbhd
# ──────────────────────────────────────────────────────────────────────────────

def test_normalizeNbhd_helper_defined() -> None:
    src = _src()
    assert "const normalizeNbhd = (s) =>" in src, (
        "normalizeNbhd helper missing from map.js"
    )


def test_normalizeNbhd_trims_collapses_lowercases() -> None:
    """The helper must trim, collapse whitespace, and lowercase."""
    src = _src()
    assert 'String(s || "").trim().replace(/\\s+/g, " ").toLowerCase()' in src, (
        "normalizeNbhd body must be: String(s||'').trim().replace(/\\s+/g,' ').toLowerCase()"
    )


# ──────────────────────────────────────────────────────────────────────────────
# DEFAULT_PROPELIO_FILTERS
# ──────────────────────────────────────────────────────────────────────────────

def test_default_propelio_filters_has_neighborhood_null() -> None:
    src = _src()
    m = re.search(r"const DEFAULT_PROPELIO_FILTERS = \{(.*?)\};", src, re.DOTALL)
    assert m, "DEFAULT_PROPELIO_FILTERS not found"
    body = m.group(1)
    assert "neighborhood: null" in body, (
        "DEFAULT_PROPELIO_FILTERS must include neighborhood: null"
    )


# ──────────────────────────────────────────────────────────────────────────────
# readPropelioFiltersFromUI
# ──────────────────────────────────────────────────────────────────────────────

def test_readPropelioFiltersFromUI_reads_prop_neighborhood() -> None:
    src = _src()
    body = _fn_body(src, r"function readPropelioFiltersFromUI\(\)")
    assert '"prop-neighborhood"' in body, (
        "readPropelioFiltersFromUI must read from #prop-neighborhood"
    )


def test_readPropelioFiltersFromUI_neighborhood_trims_to_null() -> None:
    src = _src()
    body = _fn_body(src, r"function readPropelioFiltersFromUI\(\)")
    # Must have the || null fallback so empty string becomes null
    assert '|| null' in body, (
        "readPropelioFiltersFromUI neighborhood read must coerce empty string to null"
    )


# ──────────────────────────────────────────────────────────────────────────────
# compPassesPropelioFilters gate
# ──────────────────────────────────────────────────────────────────────────────

def test_compPassesPropelioFilters_has_neighborhood_gate() -> None:
    src = _src()
    body = _fn_body(src, r"function compPassesPropelioFilters\(")
    assert "filters.neighborhood" in body, (
        "compPassesPropelioFilters must contain a filters.neighborhood gate"
    )


def test_neighborhood_gate_normalizes_comp_side_per_iter() -> None:
    """comp?.neighborhood must be normalized inside the gate (per-comp path)."""
    src = _src()
    body = _fn_body(src, r"function compPassesPropelioFilters\(")
    assert "normalizeNbhd(comp?.neighborhood)" in body, (
        "compPassesPropelioFilters gate must normalize comp?.neighborhood"
    )


def test_neighborhood_gate_normalizes_filter_side() -> None:
    """The filter-side norm must be computed (either via _nbhdNorm or inline)."""
    src = _src()
    body = _fn_body(src, r"function compPassesPropelioFilters\(")
    has_precomputed = "_nbhdNorm" in body
    has_inline_norm = "normalizeNbhd(filters.neighborhood)" in body
    assert has_precomputed or has_inline_norm, (
        "compPassesPropelioFilters must normalize the filter side "
        "(either via _nbhdNorm arg or inline normalizeNbhd(filters.neighborhood))"
    )


def test_neighborhood_gate_null_comp_drops_out() -> None:
    """When filter is active, a null-neighborhood comp must fail: the gate
    must be guarded by `if (filters.neighborhood)` so that when no filter
    is set the gate is skipped (comps with null neighborhood still pass)."""
    src = _src()
    body = _fn_body(src, r"function compPassesPropelioFilters\(")
    assert "if (filters.neighborhood)" in body, (
        "compPassesPropelioFilters gate must be inside `if (filters.neighborhood)` "
        "so that null-neighborhood comps are only filtered out when the filter is active"
    )


def test_neighborhood_gate_returns_false_on_mismatch() -> None:
    src = _src()
    body = _fn_body(src, r"function compPassesPropelioFilters\(")
    # The gate should end with `return false` inside the neighborhood block
    nbhd_start = body.find("filters.neighborhood")
    assert nbhd_start != -1
    nbhd_block = body[nbhd_start : nbhd_start + 300]
    assert "return false" in nbhd_block, (
        "Neighborhood gate must return false on mismatch"
    )


# ──────────────────────────────────────────────────────────────────────────────
# applyPropelioClientFilters — pre-computed _nbhdNorm
# ──────────────────────────────────────────────────────────────────────────────

def test_applyPropelioClientFilters_precomputes_nbhd_norm() -> None:
    src = _src()
    body = _fn_body(src, r"function applyPropelioClientFilters\(\)")
    assert "normalizeNbhd(propelioFilterState.neighborhood)" in body, (
        "applyPropelioClientFilters must pre-compute normalizeNbhd(propelioFilterState.neighborhood) "
        "once before the .filter() loop"
    )


# ──────────────────────────────────────────────────────────────────────────────
# applyPropelioFilterStateToUI — restore path
# ──────────────────────────────────────────────────────────────────────────────

def test_applyPropelioFilterStateToUI_sets_neighborhood_input() -> None:
    src = _src()
    body = _fn_body(src, r"function applyPropelioFilterStateToUI\(")
    assert '"prop-neighborhood"' in body, (
        "applyPropelioFilterStateToUI must set #prop-neighborhood from merged.neighborhood"
    )
    assert "merged.neighborhood" in body, (
        "applyPropelioFilterStateToUI must read merged.neighborhood"
    )


# ──────────────────────────────────────────────────────────────────────────────
# _applyFilterFieldToUI — SSE inbound path
# ──────────────────────────────────────────────────────────────────────────────

def test_applyFilterFieldToUI_has_neighborhood_case() -> None:
    src = _src()
    body = _fn_body(src, r"function _applyFilterFieldToUI\(")
    assert '"propelio.neighborhood"' in body, (
        "_applyFilterFieldToUI must have a case for propelio.neighborhood "
        "so SSE updates from teammates reflect live"
    )


def test_applyFilterFieldToUI_neighborhood_sets_hidden_input() -> None:
    src = _src()
    body = _fn_body(src, r"function _applyFilterFieldToUI\(")
    nbhd_start = body.find('"propelio.neighborhood"')
    assert nbhd_start != -1
    block = body[nbhd_start : nbhd_start + 400]
    assert '"prop-neighborhood"' in block, (
        "_applyFilterFieldToUI neighborhood case must set #prop-neighborhood value"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Preview smoke fixes (2026-06-25): empty dropdown + stray empty chip pill
# ──────────────────────────────────────────────────────────────────────────────

def test_renderNbhdOptions_builds_cache_on_demand() -> None:
    """Regression: typing showed nothing because the options cache was only
    built by applyPropelioClientFilters. _renderNbhdOptions must build it on
    demand so the typeahead works after a saved-area restore."""
    src = _src()
    body = _fn_body(src, r"function _renderNbhdOptions\(")
    assert "_buildNbhdOptionsCache()" in body, (
        "_renderNbhdOptions must call _buildNbhdOptionsCache() so the cache is "
        "fresh when the user types (not just after a filter-apply)"
    )


def test_options_built_from_visible_set_respecting_filters() -> None:
    """Options must mirror the visible comp set: built through the same
    compPassesPropelioFilters gate (so OAC + other filters scope them), with
    the neighborhood filter itself excluded so the user can still re-pick."""
    src = _src()
    body = _fn_body(src, r"function _buildNbhdOptionsCache\(")
    assert "compPassesPropelioFilters(c, filtersNoNbhd)" in body, (
        "_buildNbhdOptionsCache must filter source comps through "
        "compPassesPropelioFilters so the options reflect what's visible (OAC etc.)"
    )
    assert "neighborhood: null" in body, (
        "_buildNbhdOptionsCache must null the neighborhood filter when building "
        "options so a selected neighborhood doesn't collapse the option list"
    )


def test_options_cache_keyed_on_filter_signature() -> None:
    """The cache must rebuild on filter change (e.g. OAC toggle), not just when
    the comps array changes — otherwise toggling OAC wouldn't update options."""
    src = _src()
    body = _fn_body(src, r"function _buildNbhdOptionsCache\(")
    assert "_nbhdOptionsCacheSig" in body, (
        "_buildNbhdOptionsCache must key its cache on a filter signature so OAC/"
        "filter changes (same comps array) trigger a rebuild"
    )


def test_nbhd_chip_hidden_rule_present() -> None:
    """Regression: `.nbhd-chip { display: inline-flex }` overrode the [hidden]
    attribute, so the empty chip rendered as a stray pill. A more-specific
    `.nbhd-chip[hidden]` rule must restore the hide."""
    css = _css()
    assert re.search(r"\.nbhd-chip\[hidden\]\s*\{[^}]*display:\s*none", css), (
        ".nbhd-chip[hidden] { display: none } must exist to override the "
        "inline-flex display when the chip is empty/hidden"
    )
