"""Inspection-based assertions for NOTIFY firing in PATCH + PUT paths.

Per docs/MULTIUSER_COLLAB_SPRINT3_SPEC.md v1 §3.2 + §3.3.
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


def test_patch_fires_pg_notify():
    """Spec §3.2: PATCH must call pg_notify on saved_area_filter_changes."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    assert "pg_notify" in fn_src
    assert "saved_area_filter_changes" in fn_src


def test_patch_notify_only_on_winning_write():
    """Agent C catch C-4: only fire NOTIFY when UPSERT RETURNING produced
    a row (winning write). Stale writes must NOT broadcast phantom events."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    notify_idx = fn_src.find("pg_notify('saved_area_filter_changes'")
    assert notify_idx > -1, "pg_notify call not found"
    before_notify = fn_src[:notify_idx]
    last_chunk = before_notify[-800:]
    assert "patch_won" in last_chunk or "if row is not None" in last_chunk, (
        "NOTIFY must be guarded by winning-write check — Agent C catch C-4."
    )


def test_patch_notify_payload_includes_session_id():
    """Spec §3.1: payload must include by_session_id for self-echo filtering."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    assert '"by_session_id"' in fn_src or "'by_session_id'" in fn_src
    assert "x-session-id" in fn_src.lower() or "X-Session-Id" in fn_src


def test_patch_notify_payload_includes_required_fields():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "patch_area_filter_field")
    for required in ("area_id", "field_key", "value", "client_seq",
                     "by_user_id", "by_session_id", "updated_at"):
        assert f'"{required}"' in fn_src or f"'{required}'" in fn_src, (
            f"Payload missing {required!r}"
        )


def test_blob_explode_fires_single_notify():
    """Spec §3.3: blob-explode path fires a single 'blob_explode' NOTIFY,
    not N per-field NOTIFYs."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "update_saved_area")
    assert "pg_notify" in fn_src
    assert '"blob_explode"' in fn_src or "'blob_explode'" in fn_src
