"""Inspection-based assertions for endpoint refactors in Sprint 1.

Tasks 4, 5, 7 of the implementation plan (see
docs/superpowers/plans/2026-06-02-multiuser-collab-sprint1.md):
- READ endpoints (GET list, GET single, GET stored-value) move from
  owner-only to membership-based gating.
- MUTATION endpoints (POST create, PUT, DELETE, PUT stored-value)
  get tiered auth and the B-1 WHERE-clause fix.
- Cross-cutting (POST /api/parcels bonded gate, /api/parcels/rate,
  /api/propelio/comp/rate) migrate to membership-based.

Per docs/MULTIUSER_COLLAB_SPRINT1_SPEC.md v2.1 §3, §3.1, §3.2.
"""
import inspect

from api import main as api_main


def _extract_fn_source(module_src: str, fn_name: str) -> str:
    """Crude function-source extractor: returns the source from `def <fn_name>`
    up to the next top-level `def ` (sibling function). Good enough for
    inspection assertions; not a parser."""
    start_marker = f"def {fn_name}"
    if start_marker not in module_src:
        return ""
    start = module_src.index(start_marker)
    rest = module_src[start + len(start_marker):]
    next_def = rest.find("\ndef ")
    if next_def == -1:
        return module_src[start:]
    return module_src[start:start + len(start_marker) + next_def]


# ─── Task 4: READ endpoints ──────────────────────────────────────────────


def test_list_areas_filters_by_membership():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "list_saved_areas")
    assert "saved_area_members" in fn_src, (
        "GET /api/areas must filter by saved_area_members (spec §3 row 1). "
        "Refactor still uses saved_areas.user_id directly."
    )


def test_list_areas_returns_role_per_row():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "list_saved_areas")
    assert "role" in fn_src.lower(), (
        "GET /api/areas response must include 'role' per row (spec §3 row 1, §3.5)."
    )


def test_get_single_area_uses_member_check():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "get_saved_area")
    assert "_assert_user_is_area_member" in fn_src, (
        "GET /api/areas/{area_id} must use _assert_user_is_area_member, not "
        "the old `if row != user[id]` owner check (spec §3 row 3)."
    )


def test_get_single_area_returns_role():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "get_saved_area")
    assert "_user_area_role" in fn_src or '"role"' in fn_src, (
        "GET /api/areas/{area_id} response must include caller's role."
    )


def test_get_stored_value_uses_member_check():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "get_area_stored_value")
    assert "_assert_user_is_area_member" in fn_src or "saved_area_members" in fn_src, (
        "GET /api/areas/{id}/stored-value must use membership check (spec §3 row 4)."
    )
