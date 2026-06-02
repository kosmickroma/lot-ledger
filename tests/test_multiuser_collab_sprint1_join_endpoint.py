"""Inspection-based assertions for the new endpoint
POST /api/areas/by-share-id/{share_id}/join introduced in Sprint 1.

Per docs/MULTIUSER_COLLAB_SPRINT1_SPEC.md v2.1 §4.1.
"""
import inspect

from api import main as api_main


def _extract_fn_source(module_src: str, fn_name: str) -> str:
    start_marker = f"def {fn_name}"
    if start_marker not in module_src:
        return ""
    start = module_src.index(start_marker)
    rest = module_src[start + len(start_marker):]
    next_def = rest.find("\ndef ")
    if next_def == -1:
        return module_src[start:]
    return module_src[start:start + len(start_marker) + next_def]


def test_join_endpoint_decorator_exists():
    src = inspect.getsource(api_main)
    assert '@app.post("/api/areas/by-share-id/{share_id}/join")' in src, (
        "New join endpoint not registered (spec §4.1)."
    )


def test_join_endpoint_csrf_protected():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "join_saved_area_via_share_id")
    assert "require_csrf(req)" in fn_src, (
        "Join endpoint must call require_csrf (CSRF parity with other mutators)."
    )


def test_join_endpoint_resolves_share_id():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "join_saved_area_via_share_id")
    assert "SELECT area_id, user_id FROM saved_areas" in fn_src
    assert "WHERE share_id = %s" in fn_src
    assert "404" in fn_src


def test_join_endpoint_owner_no_op():
    """Caller IS the owner → no editor INSERT happens; existing role returned."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "join_saved_area_via_share_id")
    assert "owner_id == int(user" in fn_src, (
        "Join endpoint must short-circuit when caller is the area owner."
    )
    assert "already_member" in fn_src


def test_join_endpoint_inserts_editor_membership():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "join_saved_area_via_share_id")
    assert "INSERT INTO saved_area_members" in fn_src
    assert "'editor', 'share_link'" in fn_src
    assert "ON CONFLICT (area_id, user_id) DO NOTHING" in fn_src


def test_join_endpoint_returns_actual_role_on_already_member():
    """Copilot SQ-2: if INSERT was no-op (user already had a membership row),
    return the ACTUAL role from saved_area_members, not hardcoded 'editor'."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "join_saved_area_via_share_id")
    assert fn_src.count("SELECT role FROM saved_area_members") >= 1, (
        "Already-member path must SELECT actual role (Copilot SQ-2)."
    )
