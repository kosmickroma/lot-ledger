"""Unit tests for the save_verification fast-path helpers.

Covers the pure-function helpers added in docs/SAVE_VERIFICATION_FAST_PATH_SPEC.md:
- _county_from_division: division_cd → canonical county string
- _row_county: row dict → canonical county string (delegates to _county_from_division)
- _counties_from_pairs: list of (account, county) → sorted unique counties

DB-dependent helpers (_load_cached_job_metadata, _load_cached_account_county_pairs)
are covered by manual integration smoke test on preview, not unit-tested here
(would require Postgres fixtures with JSONB rows).
"""
from __future__ import annotations

import pytest

from api.main import _county_from_division, _row_county, _counties_from_pairs


class TestCountyFromDivision:
    """Single source of truth for division_cd → county. Drift-prevention helper."""

    def test_tad(self):
        assert _county_from_division("TAD") == "tad"

    def test_tad_lowercase(self):
        assert _county_from_division("tad") == "tad"

    def test_tad_with_whitespace(self):
        assert _county_from_division("  TAD  ") == "tad"

    def test_collin(self):
        assert _county_from_division("COLLIN") == "collin"

    def test_denton(self):
        assert _county_from_division("DENTON") == "denton"

    def test_dcad_via_res(self):
        # DCAD parcels carry "RES" / "COM" in division_cd, not "DCAD".
        # Anything not matching the three explicit branches falls through
        # to "dcad" as the default.
        assert _county_from_division("RES") == "dcad"

    def test_dcad_via_com(self):
        assert _county_from_division("COM") == "dcad"

    def test_dcad_via_empty(self):
        assert _county_from_division("") == "dcad"

    def test_dcad_via_none(self):
        assert _county_from_division(None) == "dcad"

    def test_dcad_via_unknown(self):
        # Future county codes we don't recognize default to dcad — matches
        # the behavior of the pre-refactor _row_county.
        assert _county_from_division("FUTURE_CAD") == "dcad"

    def test_handles_non_string_input(self):
        # str() coercion at the start of _county_from_division should
        # handle any object that has a __str__. Bytes, ints, etc. shouldn't
        # raise — they should just return "dcad" since they don't match.
        assert _county_from_division(0) == "dcad"
        assert _county_from_division([]) == "dcad"


class TestRowCounty:
    """Delegates to _county_from_division — same outputs as the
    pre-refactor implementation for every input."""

    def test_tad_row(self):
        assert _row_county({"division_cd": "TAD"}) == "tad"

    def test_collin_row(self):
        assert _row_county({"division_cd": "COLLIN"}) == "collin"

    def test_denton_row(self):
        assert _row_county({"division_cd": "DENTON"}) == "denton"

    def test_dcad_res(self):
        assert _row_county({"division_cd": "RES"}) == "dcad"

    def test_dcad_com(self):
        assert _row_county({"division_cd": "COM"}) == "dcad"

    def test_missing_key(self):
        # Old impl: row.get("division_cd", "") → "" → "dcad"
        # New impl: row.get("division_cd") → None → _county_from_division(None) → "dcad"
        assert _row_county({}) == "dcad"

    def test_explicit_none(self):
        assert _row_county({"division_cd": None}) == "dcad"


class TestCountiesFromPairs:
    """Returns sorted unique county codes from (account, county) tuples."""

    def test_empty(self):
        assert _counties_from_pairs([]) == []

    def test_single_county(self):
        assert _counties_from_pairs([("a", "dcad"), ("b", "dcad")]) == ["dcad"]

    def test_multi_county_sorted(self):
        result = _counties_from_pairs([
            ("a", "dcad"), ("b", "tad"), ("c", "collin"),
            ("d", "denton"), ("e", "dcad"),
        ])
        assert result == ["collin", "dcad", "denton", "tad"]

    def test_empty_county_skipped(self):
        # Defensive: if the thin query returns a row with empty county
        # (shouldn't happen in practice but documents the behavior).
        result = _counties_from_pairs([
            ("a", "dcad"), ("b", ""), ("c", "tad"),
        ])
        assert result == ["dcad", "tad"]
