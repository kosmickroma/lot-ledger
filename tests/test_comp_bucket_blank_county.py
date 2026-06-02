"""Inspection guard: _compPropertyTypeBucket must not gate on county.

Mike-reported 2026-06-02: comps with multi-address listings (e.g. "1825 &
1827 Pollard St") often have a valid parcel_account_num but blank
parcel_county. The original code required BOTH to attempt CAD lookup,
which made the comp fall back to Propelio's coarse "Residential"
classification — leaking duplex comps past the Duplexes filter.

This test reads frontend/map.js as source text and asserts the fix is in
place. If anyone reintroduces the `acct && county &&` gating, this fails.
"""
from __future__ import annotations

import re
from pathlib import Path

MAP_JS = Path(__file__).resolve().parent.parent / "frontend" / "map.js"


def test_comp_bucket_does_not_require_county_in_outer_gate() -> None:
    """The outer guard at _compPropertyTypeBucket entry must not be `acct && county && ...`."""
    src = MAP_JS.read_text()
    m = re.search(
        r"function _compPropertyTypeBucket\(comp\) \{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate _compPropertyTypeBucket function"
    body = m.group(1)
    # The buggy form was:
    #   if (acct && county && lastAnalysisGeojson && ...)
    # which short-circuited when comp.parcel_county was blank. The fix makes
    # county optional. Assert the buggy form is gone.
    assert "acct && county && lastAnalysisGeojson" not in body, (
        "Regression: _compPropertyTypeBucket gates CAD lookup on county again. "
        "Must allow blank-county comps to match by account_num alone."
    )


def test_comp_bucket_still_honors_county_when_present() -> None:
    """When comp DOES supply a county, the inner per-feature check must still enforce it.

    This is the cross-county-collision protection. Account number is unique
    enough across our 4 counties, but if Propelio ever supplies a county
    explicitly, we still trust it as a filter.
    """
    src = MAP_JS.read_text()
    m = re.search(
        r"function _compPropertyTypeBucket\(comp\) \{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert m
    body = m.group(1)
    # Some form of `if (county && ... source_county ...)` must still be
    # present inside the loop.
    assert re.search(
        r"if \(county && String\(p\.source_county.*?\.toLowerCase\(\) !== county\) continue",
        body,
    ), (
        "Fix should still gate by source_county WHEN comp supplies a non-empty "
        "parcel_county. Inner per-feature check missing."
    )
