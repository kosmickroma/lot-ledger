"""Tests for DCAD multi_owner per-year full sync (v4a §2.3 + §2.4)."""
from scripts.build_dcad_multi_owner import (
    _build_load_row,
    _delete_stale_sql,
    _create_table_sql,
)


def test_build_load_row_normalizes_types():
    src = {
        "APPRAISAL_YR": "2026",
        "ACCOUNT_NUM": "00000123456789012",
        "OWNER_SEQ_NUM": "2",
        "OWNER_NAME": "PARK YOUNG MI",
        "OWNERSHIP_PCT": "50.00",
    }
    out = _build_load_row(src)
    assert out == {
        "appraisal_yr": 2026,
        "account_num": "00000123456789012",
        "owner_seq_num": 2,
        "owner_name": "PARK YOUNG MI",
        "ownership_pct": 50.00,
    }


def test_build_load_row_handles_blank_ownership_pct():
    src = {
        "APPRAISAL_YR": "2026",
        "ACCOUNT_NUM": "00000123456789012",
        "OWNER_SEQ_NUM": "1",
        "OWNER_NAME": "DOE JOHN",
        "OWNERSHIP_PCT": "",
    }
    out = _build_load_row(src)
    assert out["ownership_pct"] is None


def test_build_load_row_skips_blank_account_or_seq():
    base = {"APPRAISAL_YR": "2026", "OWNER_NAME": "X"}
    assert _build_load_row({**base, "ACCOUNT_NUM": "", "OWNER_SEQ_NUM": "1"}) is None
    assert _build_load_row({**base, "ACCOUNT_NUM": "X", "OWNER_SEQ_NUM": ""}) is None


def test_create_table_sql_has_composite_pk():
    sql = _create_table_sql()
    assert "PRIMARY KEY (appraisal_yr, account_num, owner_seq_num)" in sql
    assert "CREATE TABLE IF NOT EXISTS multi_owner" in sql


def test_delete_stale_sql_is_not_exists_anti_join():
    # v4a.2 §2.4 — must use NOT EXISTS anti-join, not NOT IN.
    sql = _delete_stale_sql(2026)
    assert "NOT EXISTS" in sql
    assert "NOT IN" not in sql
    assert "_multi_owner_load" in sql
    assert "appraisal_yr = 2026" in sql or "appraisal_yr = %s" in sql
