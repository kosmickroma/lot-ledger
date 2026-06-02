"""Inspection-based assertions for the two new auth helpers introduced
in Sprint 1: _assert_user_is_area_member and _user_area_role.

Both helpers must include the defensive lazy-backfill fallback per
spec §2.3 (Copilot S-5 catch — survives rolling-deploy windows where
an area is created on a pre-Sprint-1 Cloud Run instance and lacks an
explicit owner membership row).

Per docs/MULTIUSER_COLLAB_SPRINT1_SPEC.md v2.1 §2.3.
"""
import inspect

from api import main as api_main


def _extract_fn_source(module_src: str, fn_name: str) -> str:
    """Crude function-source extractor: returns the source from `def <fn_name>`
    up to the next top-level `def ` (sibling function). Good enough for
    inspection assertions; not a parser."""
    start_marker = f"def {fn_name}"
    assert start_marker in module_src, f"function {fn_name!r} not defined"
    start = module_src.index(start_marker)
    rest = module_src[start + len(start_marker):]
    next_def = rest.find("\ndef ")
    if next_def == -1:
        return module_src[start:]
    return module_src[start:start + len(start_marker) + next_def]


def test_assert_user_is_area_member_exists():
    src = inspect.getsource(api_main)
    assert "def _assert_user_is_area_member" in src, (
        "Helper _assert_user_is_area_member is required by spec §2.3."
    )


def test_user_area_role_exists():
    src = inspect.getsource(api_main)
    assert "def _user_area_role" in src, (
        "Helper _user_area_role is required by spec §2.3."
    )


def test_assert_user_is_area_member_queries_members_table():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "_assert_user_is_area_member")
    assert "SELECT 1 FROM saved_area_members" in fn_src
    assert "area_id = %s AND user_id = %s" in fn_src
    assert "403" in fn_src  # raises 403 on no-access


def test_assert_user_is_area_member_has_lazy_backfill_fallback():
    """Copilot S-5: if no membership row exists but saved_areas.user_id
    matches the caller, lazily insert the owner row and proceed."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "_assert_user_is_area_member")
    # Must query saved_areas as a fallback
    assert "FROM saved_areas" in fn_src, (
        "Lazy-backfill fallback must SELECT from saved_areas when no "
        "membership row found (spec §2.3 Copilot S-5)."
    )
    # Must INSERT 'owner' / 'backfill' on the fallback path
    assert "INSERT INTO saved_area_members" in fn_src
    assert "'owner', 'backfill'" in fn_src
    assert "ON CONFLICT (area_id, user_id) DO NOTHING" in fn_src


def test_user_area_role_returns_string_or_none():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "_user_area_role")
    assert "SELECT role FROM saved_area_members" in fn_src
    assert "return None" in fn_src
    assert "return str(row[0])" in fn_src or "str(row[0])" in fn_src


def test_user_area_role_has_lazy_backfill():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "_user_area_role")
    assert "FROM saved_areas" in fn_src, (
        "_user_area_role must also include lazy-backfill (spec §2.3)."
    )
    assert "'owner', 'backfill'" in fn_src
