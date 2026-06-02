"""Inspection-based assertions for the new PATCH endpoint
PATCH /api/areas/{area_id}/filter-fields/{field_key}.

Per docs/MULTIUSER_COLLAB_SPRINT2_SPEC.md v2 §3.
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


def test_patch_endpoint_decorator_exists():
    src = inspect.getsource(api_main)
    assert '@app.patch("/api/areas/{area_id}/filter-fields/{field_key}")' in src, (
        "PATCH /api/areas/{id}/filter-fields/{field_key} endpoint missing (spec §3)."
    )


def test_patch_endpoint_csrf_protected():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    assert "require_csrf(req" in fn_src or "require_csrf(request" in fn_src, (
        "PATCH endpoint must call require_csrf."
    )


def test_patch_endpoint_uses_membership_check():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    assert "_assert_user_is_area_member" in fn_src, (
        "PATCH endpoint must use Sprint 1 _assert_user_is_area_member helper "
        "(editor-tier permits filter changes, spec §3)."
    )


def test_patch_endpoint_validates_field_key():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    assert "_FILTER_FIELD_KEYS" in fn_src
    assert "400" in fn_src


def test_patch_endpoint_does_lazy_backfill():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    assert "SELECT 1 FROM saved_area_filter_fields WHERE area_id = %s LIMIT 1" in fn_src
    assert "jsonb_typeof(sa.filter_state) = 'object'" in fn_src
    assert "jsonb_typeof(sa.filter_state -> 'checkboxes') = 'object'" in fn_src


def test_patch_endpoint_upsert_uses_client_seq_guard():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    assert "ON CONFLICT (area_id, field_key) DO UPDATE" in fn_src
    assert "WHERE saved_area_filter_fields.client_seq < EXCLUDED.client_seq" in fn_src


def test_patch_endpoint_cache_update_uses_safe_pattern():
    """Copilot B-2: cache update uses CASE WHEN jsonb_typeof guard."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    assert "CASE" in fn_src
    # The parent-typeof check
    assert "jsonb_typeof(COALESCE(filter_state, '{}'::jsonb) ->" in fn_src


def test_patch_endpoint_returns_value_and_seq():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    assert "RETURNING value, client_seq, updated_at, updated_by_user_id" in fn_src


def test_patch_endpoint_stale_write_refetch():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    assert "if row is None" in fn_src
    assert "SELECT value, client_seq, updated_at, updated_by_user_id" in fn_src
    # Must appear at least twice — once in UPSERT RETURNING, once in re-fetch
    assert fn_src.count("client_seq, updated_at, updated_by_user_id") >= 2


def test_patch_request_model_shape():
    src = inspect.getsource(api_main)
    assert "class FilterFieldPatchRequest" in src
    idx = src.index("class FilterFieldPatchRequest")
    block = src[idx:idx + 500]
    assert "value:" in block
    assert "client_seq: int" in block


def test_patch_response_model_shape():
    src = inspect.getsource(api_main)
    assert "class FilterFieldPatchResponse" in src
    idx = src.index("class FilterFieldPatchResponse")
    block = src[idx:idx + 500]
    for field in ("area_id", "field_key", "value", "client_seq", "updated_at", "updated_by_user_id"):
        assert field in block
