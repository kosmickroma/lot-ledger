"""Tests for Collin prior-year value fallback logic (Phase 1)."""
import pytest
from api.counties.collin import _normalize_collin_row


def test_fallback_fires_when_current_zero_and_cert_positive():
    """Fallback should activate: current=0, cert>0, both have year."""
    row = {"total_value": 0, "cert_total_value": 250000, "cert_val_year": "2024"}
    normalized = _normalize_collin_row(row)
    assert normalized["tot_val"] == 250000
    assert normalized["total_value_source"] == "prior_year_cert_2024"


def test_fallback_skipped_when_current_positive():
    """Current wins: total_value > 0 always takes precedence."""
    row = {"total_value": 300000, "cert_total_value": 250000, "cert_val_year": "2024"}
    normalized = _normalize_collin_row(row)
    assert normalized["tot_val"] == 300000
    assert normalized["total_value_source"] is None


def test_fallback_skipped_when_both_zero():
    """Neither current nor cert has value: no fallback, empty tag."""
    row = {"total_value": 0, "cert_total_value": 0}
    normalized = _normalize_collin_row(row)
    assert normalized["tot_val"] == 0
    assert normalized["total_value_source"] is None


def test_fallback_with_missing_cert_year():
    """Cert year missing: fallback fires, tag uses no-year suffix."""
    row = {"total_value": 0, "cert_total_value": 250000, "cert_val_year": ""}
    normalized = _normalize_collin_row(row)
    assert normalized["tot_val"] == 250000
    assert normalized["total_value_source"] == "prior_year_cert"


def test_fallback_with_none_current():
    """Current is None (not 0): fallback fires normally."""
    row = {"total_value": None, "cert_total_value": 175000, "cert_val_year": "2023"}
    normalized = _normalize_collin_row(row)
    assert normalized["tot_val"] == 175000
    assert normalized["total_value_source"] == "prior_year_cert_2023"


def test_fallback_skipped_when_cert_zero():
    """Cert is 0 or None: no fallback even if current is 0."""
    row = {"total_value": 0, "cert_total_value": 0, "cert_val_year": "2024"}
    normalized = _normalize_collin_row(row)
    assert normalized["tot_val"] == 0
    assert normalized["total_value_source"] is None


def test_fallback_skipped_when_cert_none():
    """Cert is None: no fallback."""
    row = {"total_value": 0, "cert_total_value": None, "cert_val_year": "2024"}
    normalized = _normalize_collin_row(row)
    assert normalized["tot_val"] == 0
    assert normalized["total_value_source"] is None


def test_all_normalized_fields_present():
    """Ensure all renamed/added fields are in normalized output."""
    row = {
        "total_value": 0,
        "cert_total_value": 250000,
        "cert_val_year": "2024",
        "state_cd": "1234",
        "land_value": 50000,
        "improvement_value": 100000,
        "account_num": "A123",
        "parcel_key": "P123",
        "geo_id": "G123",
        "owner_name": "John Doe",
        "owner_address": "123 Main",
        "owner_city": "City",
        "owner_state": "TX",
        "owner_zip": "75001",
        "property_address": "123 Main St",
        "property_zip": "75001",
        "subdivision": "Sub1",
        "legal_descr": "Legal Desc",
        "school_code": "ISD1",
        "year_built": 2000,
        "living_area": 2000,
        "zoning": "R1",
        "_lat": 32.5,
        "_lng": -96.5,
    }
    normalized = _normalize_collin_row(row)
    assert "tot_val" in normalized
    assert "total_value_source" in normalized
    assert normalized["division_cd"] == "COLLIN"
