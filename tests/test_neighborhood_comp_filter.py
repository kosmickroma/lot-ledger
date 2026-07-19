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

def test_readPropelioFiltersFromUI_no_longer_reads_dom_neighborhood() -> None:
    """docs/AI/CODER_SPEC_MULTI_NEIGHBORHOOD_2026-07-18 Part 1: the module
    array (propelioNbhds) is now the source of truth; the DOM read must be
    gone or a stale/hidden input could silently override live state."""
    src = _src()
    body = _fn_body(src, r"function readPropelioFiltersFromUI\(\)")
    assert 'getElementById("prop-neighborhood")' not in body, (
        "readPropelioFiltersFromUI must NOT read #prop-neighborhood directly "
        "-- propelioNbhds is the source of truth"
    )


def test_readPropelioFiltersFromUI_dual_writes_both_keys_in_order() -> None:
    """Part 1/3: emit `neighborhood` BEFORE `neighborhoods` -- the pending-
    PATCH Map preserves insertion order, so a receiver processing the two SSE
    echoes in order must land on the array LAST, not the single value
    (Part 8's emit-order half of the echo-clobber fix)."""
    src = _src()
    body = _fn_body(src, r"function readPropelioFiltersFromUI\(\)")
    assert "propelioNbhds.length ? propelioNbhds[0] : null" in body, (
        "readPropelioFiltersFromUI must derive the legacy single key from propelioNbhds[0]"
    )
    assert "neighborhoods: [...propelioNbhds]" in body, (
        "readPropelioFiltersFromUI must emit a fresh copy of propelioNbhds as `neighborhoods`"
    )
    one_idx = body.find("neighborhood:")
    many_idx = body.find("neighborhoods:")
    assert 0 <= one_idx < many_idx, (
        "`neighborhood` must be emitted BEFORE `neighborhoods` in the returned object "
        "(dual-write emit order, Part 1/8)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# compPassesPropelioFilters gate — OR set (docs/AI/CODER_SPEC_MULTI_
# NEIGHBORHOOD_2026-07-18 Part 4)
# ──────────────────────────────────────────────────────────────────────────────

def test_compPassesPropelioFilters_has_neighborhoods_gate() -> None:
    src = _src()
    body = _fn_body(src, r"function compPassesPropelioFilters\(")
    assert "filters.neighborhoods" in body, (
        "compPassesPropelioFilters must contain a filters.neighborhoods (OR-set) gate"
    )


def test_neighborhood_gate_normalizes_comp_side_per_iter() -> None:
    """comp?.neighborhood must be normalized inside the gate (per-comp path)."""
    src = _src()
    body = _fn_body(src, r"function compPassesPropelioFilters\(")
    assert "normalizeNbhd(comp?.neighborhood)" in body, (
        "compPassesPropelioFilters gate must normalize comp?.neighborhood"
    )


def test_neighborhood_gate_uses_a_set_for_membership() -> None:
    """Part 4: O(1) per-comp via a precomputed normalized Set, not an
    O(M) .includes() scan of the neighborhoods array per comp."""
    src = _src()
    body = _fn_body(src, r"function compPassesPropelioFilters\(")
    assert "_nbhdNormSetFor(filters).has(" in body, (
        "compPassesPropelioFilters must test membership via a Set (.has()), "
        "not an array scan"
    )


def test_neighborhood_gate_empty_set_drops_no_gate() -> None:
    """When the set is empty, the gate must be skipped entirely (today's
    null-filter behavior) — guarded by `filters.neighborhoods.length`."""
    src = _src()
    body = _fn_body(src, r"function compPassesPropelioFilters\(")
    assert "filters.neighborhoods && filters.neighborhoods.length" in body, (
        "compPassesPropelioFilters gate must be guarded by "
        "`filters.neighborhoods && filters.neighborhoods.length` so an empty "
        "OR set never filters anything out"
    )


def test_neighborhood_gate_returns_false_on_mismatch() -> None:
    src = _src()
    body = _fn_body(src, r"function compPassesPropelioFilters\(")
    # The gate should end with `return false` inside the neighborhood block
    nbhd_start = body.find("filters.neighborhoods")
    assert nbhd_start != -1
    nbhd_block = body[nbhd_start : nbhd_start + 300]
    assert "return false" in nbhd_block, (
        "Neighborhood gate must return false on mismatch"
    )


def test_compPassesPropelioFilters_dropped_the_dead_third_param() -> None:
    """Part 4 (Copilot #6): the old optional _nbhdNorm 3rd param and its
    single hot-path call site were dead code once the Set replaced it.
    docs/AI/CODER_SPEC_VACANT_ROUTING_2026-07-19 later added a NEW, live 3rd
    param (`view`, for the vacant-lot routing fork) -- this now confirms
    THAT signature, not "no 3rd param at all"; the dead _nbhdNorm call site
    must still be gone."""
    src = _src()
    m = re.search(r"function compPassesPropelioFilters\(([^)]*)\)", src)
    assert m, "compPassesPropelioFilters signature not found"
    assert m.group(1).strip() == "comp, filters, view = _activeView", (
        f"compPassesPropelioFilters must take exactly (comp, filters, view = _activeView), "
        f"got ({m.group(1).strip()})"
    )
    assert "compPassesPropelioFilters(c, propelioFilterState, _nbhdNorm)" not in src, (
        "the dead 3rd-arg call site (_nbhdNorm) must be gone"
    )


# ──────────────────────────────────────────────────────────────────────────────
# The Set-membership precompute (docs/AI/CODER_SPEC_MULTI_NEIGHBORHOOD_
# 2026-07-18 Part 4) — replaces the old applyPropelioClientFilters-local
# _nbhdNorm precompute
# ──────────────────────────────────────────────────────────────────────────────

def test_nbhd_norm_set_for_helper_defined_and_reference_memoized() -> None:
    src = _src()
    assert "function _nbhdNormSetFor(filters)" in src, (
        "_nbhdNormSetFor(filters) helper missing from map.js"
    )
    body = _fn_body(src, r"function _nbhdNormSetFor\(")
    assert "filters !== _nbhdNormSetFilters" in body, (
        "_nbhdNormSetFor must be reference-memoized on the filters object "
        "(rebuild only when a DIFFERENT filters object is passed) so it stays "
        "correct at every call site, not just the one that recomputes a "
        "single global before its own .filter() pass"
    )
    assert "new Set((filters.neighborhoods || []).map(normalizeNbhd))" in body, (
        "_nbhdNormSetFor must build the Set from filters.neighborhoods"
    )


def test_apply_propelio_client_filters_no_longer_has_dead_nbhd_norm_local() -> None:
    src = _src()
    body = _fn_body(src, r"function applyPropelioClientFilters\(\)")
    assert "const _nbhdNorm = normalizeNbhd(propelioFilterState.neighborhood)" not in body, (
        "the dead _nbhdNorm local precompute must be removed (Part 4)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# applyPropelioFilterStateToUI — restore path (docs/AI/CODER_SPEC_MULTI_
# NEIGHBORHOOD_2026-07-18 Part 1)
# ──────────────────────────────────────────────────────────────────────────────

def test_applyPropelioFilterStateToUI_reconciles_via_coerceNbhds() -> None:
    src = _src()
    body = _fn_body(src, r"function applyPropelioFilterStateToUI\(")
    assert "propelioNbhds = coerceNbhds(persisted)" in body, (
        "applyPropelioFilterStateToUI must reconcile from the RAW incoming "
        "`persisted` blob via coerceNbhds -- not `merged` (which would turn an "
        "absent `neighborhoods` key into a present empty array via the "
        "DEFAULT_PROPELIO_FILTERS spread and break old-area back-compat)"
    )
    coerce_idx = body.find("propelioNbhds = coerceNbhds(persisted)")
    # The assignment form only appears in the real re-derive call, not in the
    # explanatory comment above it (which also mentions the function name).
    read_idx = body.find("propelioFilterState = readPropelioFiltersFromUI()")
    assert 0 <= coerce_idx < read_idx, (
        "propelioNbhds must be populated BEFORE the readPropelioFiltersFromUI() "
        "re-derive, which reads propelioNbhds to build its dual-write"
    )


# ──────────────────────────────────────────────────────────────────────────────
# _applyFilterFieldToUI — SSE inbound path (docs/AI/CODER_SPEC_MULTI_
# NEIGHBORHOOD_2026-07-18 Part 8 — the BLOCKING echo-clobber fix)
# ──────────────────────────────────────────────────────────────────────────────

def test_applyFilterFieldToUI_has_neighborhood_case() -> None:
    src = _src()
    body = _fn_body(src, r"function _applyFilterFieldToUI\(")
    assert '"propelio.neighborhood"' in body, (
        "_applyFilterFieldToUI must have a case for propelio.neighborhood "
        "so SSE updates from teammates reflect live"
    )


def test_applyFilterFieldToUI_neighborhood_branch_is_echo_guarded() -> None:
    """The dual-write's OWN two SSE echoes would otherwise clobber the array
    on every co-viewer -- this is what makes SAME-version collaboration work,
    not just mixed-version. Must skip when the incoming single value already
    matches propelioNbhds[0]."""
    src = _src()
    body = _fn_body(src, r"function _applyFilterFieldToUI\(")
    nbhd_start = body.find('"propelio.neighborhood"')
    assert nbhd_start != -1
    # Generous window -- the branch carries substantial explanatory comment
    # before the code (echo-guard rationale, the Part 3/7 reconcile citation).
    next_branch = body.find('"propelio.neighborhoods"', nbhd_start)
    block = body[nbhd_start : next_branch if next_branch != -1 else nbhd_start + 1500]
    assert "normalizeNbhd(value) === normalizeNbhd(propelioNbhds[0])" in block, (
        "propelio.neighborhood branch must echo-guard: skip when the incoming "
        "value already equals propelioNbhds[0]"
    )
    assert "coerceNbhds(" in block, (
        "a genuine (non-echo) legacy edit/clear must reconcile via coerceNbhds"
    )
    assert '"prop-neighborhood"' not in block, (
        "the branch must not write the vestigial hidden #prop-neighborhood input"
    )


def test_applyFilterFieldToUI_has_neighborhoods_array_branch() -> None:
    """Part 8: keep BOTH branches alive -- a new propelio.neighborhoods
    branch reconciles the array-bearing event (the one same-version
    collaboration actually lands on, since it's emitted LAST)."""
    src = _src()
    body = _fn_body(src, r"function _applyFilterFieldToUI\(")
    assert '"propelio.neighborhoods"' in body, (
        "_applyFilterFieldToUI must have a case for propelio.neighborhoods"
    )
    arr_start = body.find('"propelio.neighborhoods"')
    assert arr_start != -1
    block = body[arr_start : arr_start + 500]
    assert "coerceNbhds(" in block, (
        "propelio.neighborhoods branch must reconcile via coerceNbhds"
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
    the neighborhood filter itself excluded so the user can still re-pick.
    Part 7: the ONE strip-site must strip BOTH keys, not just the legacy one
    -- otherwise a non-empty `neighborhoods` array would keep gating the
    options list even after `neighborhood` is nulled."""
    src = _src()
    body = _fn_body(src, r"function _buildNbhdOptionsCache\(")
    assert "compPassesPropelioFilters(c, filtersNoNbhd)" in body, (
        "_buildNbhdOptionsCache must filter source comps through "
        "compPassesPropelioFilters so the options reflect what's visible (OAC etc.)"
    )
    assert "neighborhoods: [], neighborhood: null" in body, (
        "_buildNbhdOptionsCache must strip BOTH neighborhoods (to []) and "
        "neighborhood (to null) when building options so a selected OR set "
        "doesn't collapse the option list"
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


def test_renderNbhdOptions_ranks_prefix_first() -> None:
    """Typeahead keeps substring matching but ranks prefix matches first, so a
    short query like 's' surfaces names that start with it on top instead of an
    unordered pile of contains-an-s matches."""
    src = _src()
    body = _fn_body(src, r"function _renderNbhdOptions\(")
    assert ".startsWith(q)" in body and "matches.sort" in body, (
        "_renderNbhdOptions must sort matches so display.startsWith(query) "
        "ranks ahead of mid-string substring matches"
    )


def test_renderNbhdOptions_matching_is_whitespace_insensitive() -> None:
    """Source has letter-spaced names like 'J V C'; typing 'jvc' must match.
    The matcher compares with internal whitespace stripped on both sides."""
    src = _src()
    body = _fn_body(src, r"function _renderNbhdOptions\(")
    assert "qNo" in body and 'replace(/\\s+/g, "")' in body, (
        "_renderNbhdOptions must compare with whitespace stripped so 'jvc' "
        "matches the stored 'J V C'"
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


# ──────────────────────────────────────────────────────────────────────────────
# docs/AI/CODER_SPEC_MULTI_NEIGHBORHOOD_2026-07-18 — OR-set state, coercion,
# dual-write, diff deep-compare, multi-chip UI
# ──────────────────────────────────────────────────────────────────────────────

def test_propelio_nbhds_module_var_declared() -> None:
    """Part 1: the OR-set state lives in a module variable OUTSIDE the DOM-
    read cycle, mirroring the propelioCompSortMode precedent -- NOT a field
    only on the DOM-rebuilt propelioFilterState object."""
    src = _src()
    assert re.search(r"let propelioNbhds = \[\];", src), (
        "propelioNbhds module variable missing"
    )


def test_default_propelio_filters_has_neighborhoods_array() -> None:
    src = _src()
    m = re.search(r"const DEFAULT_PROPELIO_FILTERS = \{(.*?)\};", src, re.DOTALL)
    assert m, "DEFAULT_PROPELIO_FILTERS not found"
    assert "neighborhoods: []" in m.group(1), (
        "DEFAULT_PROPELIO_FILTERS must include a static neighborhoods: [] "
        "so every view-seed snapshot built from it carries the key"
    )


def test_sort_dedupe_helper_defined() -> None:
    src = _src()
    assert "function sortDedupe(arr)" in src, "sortDedupe(arr) helper missing"
    body = _fn_body(src, r"function sortDedupe\(")
    assert "normalizeNbhd" in body, (
        "sortDedupe must sort/dedup on the normalized value"
    )


def test_coerce_nbhds_defined_with_reconcile_rules() -> None:
    """Part 1/3: the ONE shared coercion helper. Encodes the
    neighborhood === neighborhoods[0] freshness oracle: when the two keys
    disagree, the single legacy value wins (it's the fresher edit/clear)."""
    src = _src()
    assert "function coerceNbhds(propelio)" in src, "coerceNbhds(propelio) helper missing"
    body = _fn_body(src, r"function coerceNbhds\(")
    assert 'Array.isArray(propelio?.neighborhoods)' in body, (
        "coerceNbhds must check propelio?.neighborhoods with Array.isArray"
    )
    assert "one == null" in body and "out = [];" in body, (
        "coerceNbhds must treat an explicit legacy CLEAR (neighborhood null "
        "with an array present) as authoritative -- out = []"
    )
    assert "normalizeNbhd(one) !== normalizeNbhd(arr[0])" in body, (
        "coerceNbhds must detect a fresher legacy EDIT via the "
        "neighborhood !== neighborhoods[0] freshness oracle"
    )
    assert 'normalizeNbhd(x) !== ""' in body, (
        "coerceNbhds must blank-guard -- a junk empty string must never enter "
        "the OR set (it would false-positive every neighborhood-less comp in)"
    )
    assert "sortDedupe(" in body, (
        "coerceNbhds must return a sorted+deduped array via sortDedupe"
    )


def test_diff_values_equal_deep_compares_arrays() -> None:
    """Part 5 (BLOCKING): readPropelioFiltersFromUI returns a fresh
    `[...propelioNbhds]` every call, so two captures of the same set are
    different references -- plain `===` would spuriously PATCH
    propelio.neighborhoods on every filter redraw (OAC toggle, comp load,
    sort, and the view-seed snapshot)."""
    src = _src()
    assert "function _diffValuesEqual(a, b)" in src, "_diffValuesEqual(a, b) helper missing"
    body = _fn_body(src, r"function _diffValuesEqual\(")
    assert "Array.isArray(a) || Array.isArray(b)" in body, (
        "_diffValuesEqual must deep-compare when EITHER side is an array"
    )
    assert "JSON.stringify(a) === JSON.stringify(b)" in body, (
        "_diffValuesEqual must deep-compare arrays via JSON.stringify"
    )
    diff_body = _fn_body(src, r"function _diffFilterState\(")
    assert "_diffValuesEqual(a, b)" in diff_body, (
        "_diffFilterState must use _diffValuesEqual at its compare site, not a "
        "bare === reference check"
    )


def test_reset_propelio_filters_clears_nbhds_array() -> None:
    """Part 6: resetPropelioFilters() writes the DOM directly and BYPASSES
    applyPropelioFilterStateToUI (and therefore coerceNbhds) -- it must clear
    propelioNbhds itself or "Reset" would leave the OR set silently active."""
    src = _src()
    body = _fn_body(src, r"function resetPropelioFilters\(")
    assert "propelioNbhds = [];" in body, (
        "resetPropelioFilters must clear propelioNbhds = []"
    )


def test_select_nbhd_option_appends_and_dedupes() -> None:
    """Part 6: today's single-select REPLACED the value; multi-select must
    APPEND (dedup on normalized value) instead."""
    src = _src()
    body = _fn_body(src, r"function _selectNbhdOption\(")
    assert "sortDedupe([...propelioNbhds, val])" in body, (
        "_selectNbhdOption must append to propelioNbhds via sortDedupe, not "
        "replace the array"
    )


def test_clear_nbhd_filter_removes_one_chip_not_all() -> None:
    """Part 6: per-chip remove (splice one), not a blanket clear-all -- no
    separate clear-all control exists today, so each chip's ✕ is the only
    removal mechanism."""
    src = _src()
    body = _fn_body(src, r"function _clearNbhdFilter\(")
    assert re.search(r"function _clearNbhdFilter\(display\)", src), (
        "_clearNbhdFilter must take the specific neighborhood to remove"
    )
    assert "propelioNbhds.filter(" in body, (
        "_clearNbhdFilter must filter propelioNbhds down (remove just the "
        "one being cleared), not reset it wholesale"
    )


def test_render_nbhd_chip_is_multi_chip_over_module_array() -> None:
    """Part 6: WYSIWYG -- the chips shown ARE the exact OR set applied. No
    `display` param anymore; reads propelioNbhds directly so every call site
    (SSE, reset, select, remove, restore) shares one render path."""
    src = _src()
    assert re.search(r"function _renderNbhdChip\(\)\s*\{", src), (
        "_renderNbhdChip must take no arguments -- it reads propelioNbhds directly"
    )
    body = _fn_body(src, r"function _renderNbhdChip\(")
    assert "propelioNbhds\n    .map(" in body or "propelioNbhds.length" in body, (
        "_renderNbhdChip must render from propelioNbhds"
    )
    assert 'data-nbhd="' in body, (
        "each chip's ✕ must carry a data-nbhd key so removal targets just that one"
    )


def test_chip_removal_delegation_passes_the_specific_neighborhood() -> None:
    src = _src()
    assert '_clearNbhdFilter(xEl.dataset.nbhd || "")' in src, (
        "the ✕ click/keydown delegation must pass the clicked chip's own "
        "data-nbhd value to _clearNbhdFilter, not call it with no argument"
    )
