"""Backward-compatibility: PUT /api/areas/{id} with `filter_state` blob
still works, and ALSO explodes into per-field rows transactionally.
Covers (a) old frontend talking to new backend during rolling deploy,
(b) developer-bypass curl/Postman flows, (c) any caller that posts a
full filter_state blob.

Per docs/MULTIUSER_COLLAB_SPRINT2_SPEC.md v2 §4.
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


def test_update_saved_area_still_accepts_filter_state_blob():
    """Regression: existing blob cache write must keep working.

    ARV·NBV·Export feature (2026-06-29): the whole-blob PUT now MERGES
    (`COALESCE(filter_state,'{}') || %s::jsonb`) instead of overwriting, so a
    flat-section PUT can never silently wipe the additive `_views` data written
    by per-field PATCH. Still writes filter_state to the blob — just via merge."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "update_saved_area")
    assert "filter_state" in fn_src
    assert 'update_cols.append("filter_state = COALESCE(filter_state,\'{}\') || %s::jsonb")' in fn_src


def test_update_saved_area_explodes_blob_into_per_field():
    """Spec §4: when filter_state is in the PUT body, the handler must
    ALSO write per-field rows (UPSERT with client_seq=0 so per-field
    PATCHes always win on subsequent traffic)."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "update_saved_area")
    assert "INSERT INTO saved_area_filter_fields" in fn_src, (
        "PUT blob path must explode filter_state into per-field rows (spec §4)."
    )
    assert "ON CONFLICT (area_id, field_key)" in fn_src


def test_update_saved_area_blob_explode_uses_safe_typeof_guards():
    """Blob explode must use jsonb_typeof guards (Copilot B-1)."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "update_saved_area")
    if "INSERT INTO saved_area_filter_fields" in fn_src:
        assert "jsonb_typeof" in fn_src, (
            "PUT blob explode must use jsonb_typeof safety guards."
        )


def test_update_saved_area_blob_explode_runs_in_same_transaction():
    """Spec §4: blob explode must be inside the same with conn.cursor()
    block as the main UPDATE so cache + per-field rows commit atomically."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "update_saved_area")
    # Find the order of markers: main UPDATE, blob explode INSERT, conn.commit
    update_idx = fn_src.find("UPDATE saved_areas")
    explode_idx = fn_src.find("INSERT INTO saved_area_filter_fields")
    commit_idx = fn_src.find("conn.commit()")
    assert update_idx > -1 and explode_idx > -1 and commit_idx > -1, (
        "Missing markers in update_saved_area"
    )
    # UPDATE first, then explode, then commit
    assert update_idx < explode_idx < commit_idx, (
        f"Wrong order: UPDATE@{update_idx} explode@{explode_idx} commit@{commit_idx}"
    )
