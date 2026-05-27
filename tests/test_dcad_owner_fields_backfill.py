"""Tests for DCAD owner+property fields backfill (v4a §2.2.a)."""
from scripts.build_dcad_owner_fields_backfill import (
    OWNER_FIELDS_COLUMN_DEFS,
    _build_load_row,
)


def test_eleven_columns_defined():
    # Spec v4a.2 §2.2.a lists exactly 11 new columns. Lock the count.
    assert len(OWNER_FIELDS_COLUMN_DEFS) == 11


def test_column_names_match_spec():
    expected = {
        "owner_name2",
        "biz_name",
        "owner_address_line1",
        "owner_address_line2",
        "owner_address_line3",
        "owner_address_line4",
        "owner_country",
        "street_half_num",
        "bldg_id",
        "unit_id",
        "mapsco",
    }
    assert {c["db"] for c in OWNER_FIELDS_COLUMN_DEFS} == expected


def test_build_load_row_maps_source_headers_to_db_columns():
    src = {
        "ACCOUNT_NUM": "00000123456789012",
        "OWNER_NAME2": "JANE DOE",
        "BIZ_NAME": "ACME LLC",
        "OWNER_ADDRESS_LINE1": "PARK TAE SOON & YOUNG MI",
        "OWNER_ADDRESS_LINE2": "5412 LACEY DR",
        "OWNER_ADDRESS_LINE3": "",
        "OWNER_ADDRESS_LINE4": "",
        "OWNER_COUNTRY": "",
        "STREET_HALF_NUM": "1/2",
        "BLDG_ID": "B",
        "UNIT_ID": "204",
        "MAPSCO": "44G",
    }
    out = _build_load_row(src)
    assert out["account_num"] == "00000123456789012"
    assert out["owner_address_line1"] == "PARK TAE SOON & YOUNG MI"
    assert out["owner_address_line2"] == "5412 LACEY DR"
    assert out["biz_name"] == "ACME LLC"
    assert out["mapsco"] == "44G"
    # Blank strings load as None so the bulk UPDATE writes SQL NULL,
    # not the empty string (so re-runs clear stale values).
    assert out["owner_country"] is None
    assert out["owner_address_line3"] is None


def test_build_load_row_skips_when_account_num_blank():
    assert _build_load_row({"ACCOUNT_NUM": "", "OWNER_NAME2": "X"}) is None
    assert _build_load_row({"ACCOUNT_NUM": "   "}) is None
