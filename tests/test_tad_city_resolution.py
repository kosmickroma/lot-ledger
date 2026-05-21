"""Tests for TAD city resolution (Cities.shp DBF → tad_city_lookup → property_city).

These exercise the Python row-normalization path (api/counties/tad.py).
Frontend formatter (frontend/map.js:_formatPropertyAddress) is verified
manually on preview since there's no JS test framework wired up.
"""
import pytest
from api.counties.tad import _normalize_tad_row


def test_normalize_passes_through_property_city():
    """Happy path: property_city present in raw row → flows to normalized output."""
    raw = {
        "parcel_key": "12345:000",
        "account_num": "12345",
        "city_code": "026",
        "property_city": "ARLINGTON",
        "situs_addr": "2921 GIPSON",
        "owner_name": "DOE JOHN",
        "owner_zip": "76010",
        "year_built": 1970,
        "land_value": 50000,
        "improvement_value": 150000,
        "total_value": 200000,
    }
    normalized = _normalize_tad_row(raw)
    assert normalized["property_city"] == "ARLINGTON"


def test_normalize_handles_missing_property_city():
    """When property_city is missing/None, normalized value is empty (not crash)."""
    raw = {
        "parcel_key": "67890:000",
        "account_num": "67890",
        "city_code": "999",
        "situs_addr": "100 UNINCORPORATED RD",
        "owner_name": "DOE JANE",
        "owner_zip": "76001",
        "year_built": 1985,
        "land_value": 30000,
        "improvement_value": 100000,
        "total_value": 130000,
    }
    normalized = _normalize_tad_row(raw)
    assert normalized.get("property_city") in (None, "")


def test_normalize_strips_whitespace_in_property_city():
    """_clean_text() should trim surrounding whitespace on city values."""
    raw = {
        "parcel_key": "11111:000",
        "account_num": "11111",
        "city_code": "001",
        "property_city": "  AZLE  ",
        "situs_addr": "1 MAIN ST",
        "owner_name": "OWNER",
        "owner_zip": "76020",
        "year_built": 2000,
        "land_value": 40000,
        "improvement_value": 200000,
        "total_value": 240000,
    }
    normalized = _normalize_tad_row(raw)
    assert normalized["property_city"] == "AZLE"


def test_normalize_treats_empty_string_as_empty():
    """Empty string property_city → normalized output should also be empty/None."""
    raw = {
        "parcel_key": "22222:000",
        "account_num": "22222",
        "city_code": "",
        "property_city": "",
        "situs_addr": "2 OAK ST",
        "owner_name": "OWNER2",
        "owner_zip": "76050",
        "year_built": 2010,
        "land_value": 60000,
        "improvement_value": 300000,
        "total_value": 360000,
    }
    normalized = _normalize_tad_row(raw)
    assert normalized.get("property_city") in (None, "", "EMPTY".replace("EMPTY", ""))
