"""Inspection-based assertions that the saved_area_filter_fields migration
landed in api/main.py's _run_schema_steps invocation.

Per docs/MULTIUSER_COLLAB_SPRINT2_SPEC.md v2 §2.1, §2.2.

Live DB schema validation lives in the manual preview smoke matrix (§7.2).
"""
import inspect

from api import main as api_main


def test_saved_area_filter_fields_table_create_present():
    src = inspect.getsource(api_main)
    assert "CREATE TABLE IF NOT EXISTS saved_area_filter_fields" in src, (
        "saved_area_filter_fields CREATE TABLE missing (spec §2.1)."
    )


def test_saved_area_filter_fields_columns_present():
    src = inspect.getsource(api_main)
    idx = src.index("CREATE TABLE IF NOT EXISTS saved_area_filter_fields")
    block = src[idx:idx + 2000]
    for col in ("area_id", "field_key", "value", "client_seq",
                "updated_by_user_id", "updated_at"):
        assert col in block, f"saved_area_filter_fields column {col!r} missing"


def test_saved_area_filter_fields_pk():
    src = inspect.getsource(api_main)
    idx = src.index("CREATE TABLE IF NOT EXISTS saved_area_filter_fields")
    block = src[idx:idx + 2000]
    assert "PRIMARY KEY (area_id, field_key)" in block, (
        "saved_area_filter_fields PK missing or wrong shape (spec §2.1)."
    )


def test_saved_area_filter_fields_fk_cascades():
    src = inspect.getsource(api_main)
    idx = src.index("CREATE TABLE IF NOT EXISTS saved_area_filter_fields")
    block = src[idx:idx + 2000]
    assert "REFERENCES saved_areas(area_id) ON DELETE CASCADE" in block
    assert "REFERENCES users(id) ON DELETE SET NULL" in block, (
        "updated_by_user_id FK should ON DELETE SET NULL per spec §2.1."
    )


def test_saved_area_filter_fields_index_updated_at():
    src = inspect.getsource(api_main)
    assert "idx_saved_area_filter_fields_updated_at" in src, (
        "Index for Sprint 3 replay query missing (spec §2.1)."
    )
    assert "(area_id, updated_at DESC)" in src


def test_backfill_uses_jsonb_typeof_object_guard():
    """Copilot B-1: backfill must use jsonb_typeof(...) = 'object', NOT
    IS NOT NULL. JSON null passes IS NOT NULL but raises inside jsonb_each."""
    src = inspect.getsource(api_main)
    idx = src.find("SELECT 'checkboxes' AS section")
    assert idx > -1, "Eager backfill SELECT not found"
    block = src[max(0, idx - 200):idx + 2500]
    # The section guards must use jsonb_typeof
    assert "jsonb_typeof(sa.filter_state -> 'checkboxes') = 'object'" in block
    assert "jsonb_typeof(sa.filter_state -> 'numeric') = 'object'" in block
    assert "jsonb_typeof(sa.filter_state -> 'sold') = 'object'" in block
    assert "jsonb_typeof(sa.filter_state -> 'comp') = 'object'" in block
    assert "jsonb_typeof(sa.filter_state -> 'propelio') = 'object'" in block
    # And the outer guard
    assert "jsonb_typeof(sa.filter_state) = 'object'" in block
    # No bare IS NOT NULL guards on filter_state sections inside this block
    bad = "sa.filter_state -> 'propelio' IS NOT NULL"
    assert bad not in block, (
        "Backfill still uses bare IS NOT NULL guard — Copilot B-1 not fixed."
    )


def test_backfill_idempotent():
    src = inspect.getsource(api_main)
    assert "saved_area_filter_fields_backfill" in src
    assert "ON CONFLICT (area_id, field_key) DO NOTHING" in src
