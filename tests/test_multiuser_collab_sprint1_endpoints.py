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


# ─── Task 7: cross-cutting (parcels rate, comp rate, parcels POST B-2 gate) ──


def test_parcels_rate_uses_member_check():
    """POST /api/parcels/rate must let editors rate parcels on shared areas."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "rate_parcel")
    assert fn_src, "rate_parcel handler not found"
    assert "_assert_user_is_area_member" in fn_src, (
        "/api/parcels/rate must use _assert_user_is_area_member (spec §3.2)."
    )


def test_propelio_comp_rate_uses_member_check():
    """Copilot B-4: /comp/rate lives in api/propelio/routes.py, not main.py.
    Must switch from _assert_user_owns_area to _assert_user_is_area_member."""
    from api.propelio import routes as propelio_routes
    src = inspect.getsource(propelio_routes)
    assert "_assert_user_is_area_member" in src, (
        "api/propelio/routes.py must import + call _assert_user_is_area_member "
        "(Copilot B-4 catch — file is outside main.py and was missed in the "
        "initial endpoint enumeration)."
    )


def test_parcels_post_bonded_copy_membership_check():
    """Copilot B-2 blocking catch: the bonded-copy SQL at the original
    line 6684 (in create_saved_parcel) silently skipped non-owners.
    Replace with membership-aware logic so editors can save parcels into
    shared areas."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "create_saved_parcel")
    assert fn_src, "create_saved_parcel handler not found"
    # The old gate was:
    #   SELECT 1 FROM saved_areas WHERE area_id = %s AND user_id = %s LIMIT 1
    # Post-Sprint-1 must NOT exist in this shape.
    assert "_assert_user_is_area_member" in fn_src or "_user_area_role" in fn_src, (
        "POST /api/parcels bonded-copy gate must permit editors "
        "(Copilot B-2 blocking catch). Replace owner-only SELECT with "
        "membership-aware helper call."
    )


# ─── Leave shared area (2026-07-26) ──────────────────────────────────────
# Per docs/lot-ledger/SPEC_leave_shared_area_2026-07-26.md §7. This is a new,
# additive endpoint — DELETE /api/areas/{area_id}/membership — that removes
# only the caller's own saved_area_members row. It must never touch
# DELETE /api/areas/{area_id} (delete_saved_area, tested above) or its
# "Only the owner can delete" 403 message.


def test_leave_saved_area_shape_and_guards():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "leave_saved_area")
    assert fn_src, "leave_saved_area handler not found"
    assert "require_csrf" in fn_src, (
        "DELETE /api/areas/{id}/membership must call require_csrf (spec §4)."
    )
    assert 'app_role not in ("owner", "developer")' in fn_src, (
        "leave_saved_area must gate on app-role owner/developer only (spec §4)."
    )
    # S1 (Fable critique): creator hard-guard, defense in depth against the
    # lazy backfill resurrecting the area as 'owner' after a Leave.
    assert "FROM saved_areas WHERE area_id = %s AND user_id = %s" in fn_src, (
        "leave_saved_area must carry the S1 creator hard-guard SELECT "
        "(spec §4 S1) before allowing a Leave."
    )
    assert 'role != "editor"' in fn_src, (
        "leave_saved_area must reject non-editor roles — a creator's own "
        "area must never be leaveable (spec §4)."
    )
    assert "DELETE FROM saved_area_members" in fn_src, (
        "leave_saved_area's DELETE must target saved_area_members, not "
        "saved_areas (spec §4 — this endpoint is purely additive)."
    )
    assert "DELETE FROM saved_areas" not in fn_src, (
        "leave_saved_area must NEVER issue DELETE FROM saved_areas — that is "
        "the hard-delete cascade this feature deliberately does not touch."
    )


def test_leave_saved_area_csrf_before_authorization():
    """N3 (Fable critique): require_csrf must run before the app-role gate,
    not just be present somewhere, so a future refactor can't leave the CSRF
    check behind the authorization gates."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "leave_saved_area")
    assert fn_src, "leave_saved_area handler not found"
    csrf_idx = fn_src.index("require_csrf")
    role_gate_idx = fn_src.index('app_role not in')
    assert csrf_idx < role_gate_idx, (
        f"require_csrf@{csrf_idx} must precede the app_role gate@{role_gate_idx}."
    )


def test_delete_from_saved_areas_does_not_grow_a_third_site():
    """Cheap tripwire (spec §7, adjusted): the spec assumed `DELETE FROM
    saved_areas` appears exactly once (in delete_saved_area). That's stale —
    it's already 2 on this branch's base, pre-existing and unrelated to this
    feature: delete_saved_area's own-area delete, plus the admin account-purge
    cascade (delete_user, ~api/main.py:3021, `WHERE user_id = %s` — wipes a
    deleted user's own areas). Baseline kept at 2; the guard is that
    leave_saved_area must not add a third."""
    src = inspect.getsource(api_main)
    assert src.count("DELETE FROM saved_areas") == 2, (
        "DELETE FROM saved_areas count changed from the known baseline of 2 "
        "(delete_saved_area + admin delete_user cascade). If this is "
        "leave_saved_area growing a new hard-delete path, that violates the "
        "spec's core non-goal — Leave must only ever "
        "DELETE FROM saved_area_members."
    )
