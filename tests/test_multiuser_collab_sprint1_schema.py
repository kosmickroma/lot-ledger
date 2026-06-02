"""Inspection-based assertions that the saved_area_members migration step
landed in api/main.py's _run_schema_steps invocation.

Live DB schema validation lives in the manual preview smoke matrix
(spec §7.1). These tests confirm the migration code itself is present.

Per docs/MULTIUSER_COLLAB_SPRINT1_SPEC.md v2.1 §2.1, §2.2.
"""
import inspect

from api import main as api_main


def test_saved_area_members_table_create_present():
    src = inspect.getsource(api_main)
    assert "CREATE TABLE IF NOT EXISTS saved_area_members" in src, (
        "Migration step for saved_area_members table not found. "
        "Sprint 1 spec §2.1 requires this in the startup-hook schema steps."
    )


def test_saved_area_members_columns_present():
    src = inspect.getsource(api_main)
    members_idx = src.index("CREATE TABLE IF NOT EXISTS saved_area_members")
    members_block = src[members_idx:members_idx + 1500]
    for col in ("area_id", "user_id", "role", "added_via", "added_at"):
        assert col in members_block, (
            f"saved_area_members column {col!r} not referenced inside the "
            f"CREATE TABLE block."
        )


def test_saved_area_members_role_check_constraint():
    src = inspect.getsource(api_main)
    assert "role IN ('owner', 'editor')" in src or 'role IN ("owner", "editor")' in src, (
        "saved_area_members.role CHECK constraint missing. Spec §2.1 "
        "requires role IN ('owner', 'editor')."
    )


def test_saved_area_members_added_via_check_constraint():
    src = inspect.getsource(api_main)
    assert "added_via IN ('owner', 'share_link', 'developer_bypass', 'backfill')" in src, (
        "saved_area_members.added_via CHECK constraint missing or different. "
        "Spec §2.1 requires these 4 values."
    )


def test_saved_area_members_cascade_delete_on_both_fks():
    src = inspect.getsource(api_main)
    members_idx = src.index("CREATE TABLE IF NOT EXISTS saved_area_members")
    members_block = src[members_idx:members_idx + 1500]
    # Both area_id and user_id FKs must cascade.
    assert "REFERENCES saved_areas(area_id) ON DELETE CASCADE" in members_block
    assert "REFERENCES users(id)" in members_block
    assert members_block.count("ON DELETE CASCADE") >= 2, (
        "saved_area_members must cascade on BOTH FK references (area_id, user_id)."
    )


def test_saved_area_members_index_on_user_id():
    src = inspect.getsource(api_main)
    assert "idx_saved_area_members_user_id" in src, (
        "Index idx_saved_area_members_user_id missing. Spec §2.1 requires it "
        "for 'list areas I'm a member of' query performance."
    )
    assert "ON saved_area_members(user_id)" in src, (
        "Index must target saved_area_members(user_id) column specifically."
    )


def test_saved_area_members_backfill_idempotent():
    src = inspect.getsource(api_main)
    assert "INSERT INTO saved_area_members" in src
    assert "ON CONFLICT (area_id, user_id) DO NOTHING" in src, (
        "Backfill must be idempotent via ON CONFLICT DO NOTHING (spec §2.2)."
    )
    assert "'owner', 'backfill'" in src, (
        "Backfill must set role='owner' and added_via='backfill' for existing rows."
    )
