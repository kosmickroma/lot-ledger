"""Tests for DCAD appraisal jurisdiction backfill (v4a §2.2.b)."""
from scripts.build_dcad_appraisal_jurisdiction_backfill import (
    APPRAISAL_JURIS_COLUMN_DEFS,
    _build_load_row,
    _to_numeric_or_none,
)


def test_eleven_columns_defined():
    assert len(APPRAISAL_JURIS_COLUMN_DEFS) == 11


def test_column_names_and_csv_headers_match_spec():
    expected = {
        ("city_jurisdiction_desc",         "CITY_JURIS_DESC",         "text"),
        ("county_jurisdiction_desc",       "COUNTY_JURIS_DESC",       "text"),
        ("hospital_jurisdiction_desc",     "HOSPITAL_JURIS_DESC",     "text"),
        ("college_jurisdiction_desc",      "COLLEGE_JURIS_DESC",      "text"),
        ("special_dist_jurisdiction_desc", "SPECIAL_DIST_JURIS_DESC", "text"),
        ("city_taxable_val",               "CITY_TAXABLE_VAL",        "numeric"),
        ("county_taxable_val",             "COUNTY_TAXABLE_VAL",      "numeric"),
        ("isd_taxable_val",                "ISD_TAXABLE_VAL",         "numeric"),
        ("hospital_taxable_val",           "HOSPITAL_TAXABLE_VAL",    "numeric"),
        ("college_taxable_val",            "COLLEGE_TAXABLE_VAL",     "numeric"),
        ("special_dist_taxable_val",       "SPECIAL_DIST_TAXABLE_VAL","numeric"),
    }
    got = {(c["db"], c["csv"], c["sql_type"]) for c in APPRAISAL_JURIS_COLUMN_DEFS}
    assert got == expected


def test_to_numeric_or_none_handles_dcad_formats():
    assert _to_numeric_or_none("0") == 0
    assert _to_numeric_or_none("217760") == 217760
    assert _to_numeric_or_none(".00") == 0
    assert _to_numeric_or_none("123.45") == 123.45
    assert _to_numeric_or_none("") is None
    assert _to_numeric_or_none(None) is None
    assert _to_numeric_or_none("   ") is None


def test_build_load_row_maps_correctly():
    src = {
        "ACCOUNT_NUM": "00000617260000000",
        "CITY_JURIS_DESC": "DALLAS",
        "COUNTY_JURIS_DESC": "DALLAS COUNTY",
        "HOSPITAL_JURIS_DESC": "PARKLAND HOSPITAL",
        "COLLEGE_JURIS_DESC": "DALLAS COLLEGE",
        "SPECIAL_DIST_JURIS_DESC": "UNASSIGNED",
        "CITY_TAXABLE_VAL": "217760",
        "COUNTY_TAXABLE_VAL": "217760",
        "ISD_TAXABLE_VAL": "217760",
        "HOSPITAL_TAXABLE_VAL": "217760",
        "COLLEGE_TAXABLE_VAL": "217760",
        "SPECIAL_DIST_TAXABLE_VAL": "0",
    }
    out = _build_load_row(src)
    assert out["account_num"] == "00000617260000000"
    assert out["city_jurisdiction_desc"] == "DALLAS"
    assert out["special_dist_jurisdiction_desc"] == "UNASSIGNED"
    assert out["city_taxable_val"] == 217760
    assert out["special_dist_taxable_val"] == 0


def test_build_load_row_skips_blank_account():
    assert _build_load_row({"ACCOUNT_NUM": ""}) is None
