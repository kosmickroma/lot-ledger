"""Tests for DCAD city resolution.

Exercises the (DALLAS CO) suffix-strip normalizer used by both
scripts/build_db.py and scripts/build_dcad_property_city.py.
The frontend formatter (frontend/map.js:_formatPropertyAddress) is
verified manually on preview — no JS test framework wired up.
"""
import pytest
from scripts.build_db import _clean_property_city


def test_strips_dallas_co_suffix():
    assert _clean_property_city("GARLAND (DALLAS CO)") == "GARLAND"


def test_strips_dallas_co_suffix_with_extra_whitespace():
    assert _clean_property_city("MESQUITE  (DALLAS CO)  ") == "MESQUITE"


def test_case_insensitive_suffix_match():
    # Suffix matching should be case-insensitive (handles "(Dallas Co)" etc.)
    assert _clean_property_city("GRAND PRAIRIE (Dallas Co)") == "GRAND PRAIRIE"
    assert _clean_property_city("SEAGOVILLE (dallas co)") == "SEAGOVILLE"


def test_no_suffix_passes_through_uppercase():
    assert _clean_property_city("dallas") == "DALLAS"
    assert _clean_property_city("Addison") == "ADDISON"


def test_empty_returns_none():
    assert _clean_property_city("") is None
    assert _clean_property_city(None) is None
    assert _clean_property_city("   ") is None


def test_only_suffix_returns_none():
    # If the string is literally "(DALLAS CO)" with no city name, strip
    # leaves an empty string → return None.
    assert _clean_property_city(" (DALLAS CO) ") is None


def test_suffix_appears_only_at_end():
    # The strip is anchored at end-of-string — "(DALLAS CO)" appearing
    # mid-string shouldn't be removed (defensive — shouldn't happen in
    # real data but worth asserting).
    assert _clean_property_city("(DALLAS CO) PARK") == "(DALLAS CO) PARK"


def test_preserves_special_city_names():
    """NO TOWN, UNASSIGNED, HIGHLAND PARK etc. — pass through unchanged after upper."""
    assert _clean_property_city("NO TOWN") == "NO TOWN"
    assert _clean_property_city("UNASSIGNED") == "UNASSIGNED"
    assert _clean_property_city("HIGHLAND PARK") == "HIGHLAND PARK"
    assert _clean_property_city("UNIVERSITY PARK") == "UNIVERSITY PARK"
