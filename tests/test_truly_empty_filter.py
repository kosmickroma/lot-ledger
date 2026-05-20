"""Tests for truly-empty parcel filter (Phase 1, Phase 3)."""
import pytest
from api.main import _is_truly_empty_parcel


def test_truly_empty_parcel_excluded():
    """All four conditions empty: should be excluded."""
    row = {
        "property_address": "",
        "owner_name": "",
        "legal1": "",
        "tot_val": 0,
        "cert_total_value": 0,
    }
    assert _is_truly_empty_parcel(row) is True


def test_sparse_but_has_owner_kept():
    """Has owner: not truly empty, should be kept."""
    row = {
        "property_address": "",
        "owner_name": "JOHN DOE",
        "legal1": "",
        "tot_val": 0,
        "cert_total_value": 0,
    }
    assert _is_truly_empty_parcel(row) is False


def test_sparse_but_has_address_kept():
    """Has address: not truly empty, should be kept."""
    row = {
        "property_address": "123 MAIN ST",
        "owner_name": "",
        "legal1": "",
        "tot_val": 0,
        "cert_total_value": 0,
    }
    assert _is_truly_empty_parcel(row) is False


def test_sparse_but_has_legal_kept():
    """Has legal description: not truly empty, should be kept."""
    row = {
        "property_address": "",
        "owner_name": "",
        "legal1": "LOT 1 BLOCK 2",
        "tot_val": 0,
        "cert_total_value": 0,
    }
    assert _is_truly_empty_parcel(row) is False


def test_sparse_but_has_current_value_kept():
    """Has current value (>0): not truly empty, should be kept."""
    row = {
        "property_address": "",
        "owner_name": "",
        "legal1": "",
        "tot_val": 100000,
        "cert_total_value": 0,
    }
    assert _is_truly_empty_parcel(row) is False


def test_sparse_but_has_cert_value_kept():
    """Has cert value (>0) only: not truly empty (fallback available)."""
    row = {
        "property_address": "",
        "owner_name": "",
        "legal1": "",
        "tot_val": 0,
        "cert_total_value": 150000,
    }
    assert _is_truly_empty_parcel(row) is False


def test_truly_empty_with_none_values():
    """None values treated as empty: should be excluded."""
    row = {
        "property_address": None,
        "owner_name": None,
        "legal1": None,
        "tot_val": None,
        "cert_total_value": None,
    }
    assert _is_truly_empty_parcel(row) is True


def test_truly_empty_with_whitespace():
    """Whitespace-only strings treated as empty via _clean_text: excluded."""
    row = {
        "property_address": "   ",
        "owner_name": "  ",
        "legal1": "\t",
        "tot_val": 0,
        "cert_total_value": 0,
    }
    assert _is_truly_empty_parcel(row) is True


def test_filter_uses_either_address_or_property_address():
    """Filter checks both 'property_address' and 'addr' fields (fallback)."""
    row = {
        "addr": "123 MAIN ST",  # Uses 'addr' instead of 'property_address'
        "owner_name": "",
        "legal1": "",
        "tot_val": 0,
        "cert_total_value": 0,
    }
    assert _is_truly_empty_parcel(row) is False


def test_filter_uses_either_legal_descr_or_legal1():
    """Filter checks both 'legal_descr' and 'legal1' fields (fallback)."""
    row = {
        "property_address": "",
        "owner_name": "",
        "legal_descr": "LOT 1 BLOCK 2",  # Uses 'legal_descr' instead of 'legal1'
        "tot_val": 0,
        "cert_total_value": 0,
    }
    assert _is_truly_empty_parcel(row) is False
