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


# ─── Task 5: MUTATION endpoints (POST create, PUT, DELETE, PUT stored-value) ──


def test_create_area_inserts_owner_membership():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "create_saved_area")
    assert "INSERT INTO saved_area_members" in fn_src, (
        "POST /api/areas must insert an owner membership row (spec §3 row 2)."
    )
    assert "'owner', 'owner'" in fn_src, (
        "Membership row from create must use role='owner', added_via='owner'."
    )


def test_create_area_membership_insert_after_release_savepoint():
    """Copilot S-4: membership INSERT must be after RELEASE SAVEPOINT
    sp_create_saved_area (outside the share_id retry loop) and before
    conn.commit()."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "create_saved_area")
    last_release = fn_src.rfind("RELEASE SAVEPOINT sp_create_saved_area")
    member_insert_idx = fn_src.find("INSERT INTO saved_area_members")
    commit_idx = fn_src.find("conn.commit()")
    assert last_release > -1 and member_insert_idx > -1 and commit_idx > -1, (
        "All three markers must exist in create_saved_area."
    )
    assert last_release < member_insert_idx < commit_idx, (
        f"Order violation: RELEASE@{last_release} INSERT@{member_insert_idx} commit@{commit_idx}"
    )


def test_put_area_two_tier_auth():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "update_saved_area")
    # Editor tier for filter_state
    assert "_assert_user_is_area_member" in fn_src, (
        "PUT /api/areas/{id} must use _assert_user_is_area_member for "
        "filter_state-only updates (spec §3.1 tier (a))."
    )
    # Owner tier for name / polygon / originator
    assert "_assert_user_owns_area" in fn_src, (
        "PUT /api/areas/{id} must keep _assert_user_owns_area for "
        "owner-only mutations (spec §3.1 tier (b))."
    )


def test_put_area_where_clause_drops_user_id_for_non_developers():
    """Copilot B-1 blocking catch: UPDATE WHERE clause must drop
    `AND user_id = %s` for non-developer paths. Pre-check IS the gate."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "update_saved_area")
    # The plain "WHERE area_id = %s" string should appear as the non-developer
    # where_clause assignment, not "WHERE area_id = %s AND user_id = %s".
    assert 'where_clause = "WHERE area_id = %s"' in fn_src, (
        "PUT UPDATE WHERE clause must be `WHERE area_id = %s` for the "
        "non-developer auth path post-Sprint-1 (Copilot B-1)."
    )


def test_delete_area_pre_check_403_for_non_owner():
    """Copilot S-3 catch: editors must get 403, not 404, on DELETE."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "delete_saved_area")
    assert "_user_area_role" in fn_src, (
        "DELETE /api/areas/{id} must call _user_area_role and explicitly "
        "return 403 for non-owners (spec §3 DELETE row, Copilot S-3)."
    )
    assert "Only the owner can delete" in fn_src, (
        "Editor-blocking 403 message expected ('Only the owner can delete')."
    )


def test_put_stored_value_uses_member_check():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "put_area_stored_value")
    assert "_assert_user_is_area_member" in fn_src, (
        "PUT /api/areas/{id}/stored-value must use membership check (spec §3 row 5)."
    )
