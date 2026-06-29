"""Chunk E (backend half) source-inspection tests — per-view PARCEL hydration.

Comps already carry `ratings_by_view` (Chunk B, load_comps_by_polygon /
load_archived_comps). This is the parcel analogue: the two parcel hydration
sites must additionally stamp `feature.properties.ratings_by_view` so the
frontend can project the active view's parcel marks without ARV bleeding
across views. ARV stays in `user_rating` (unchanged). Additive + backward-
compatible (flag-off clients ignore the extra property).

Per-view ratings spec §4 (Chunk E parcel display). Source-inspection style,
matching test_per_view_ratings_chunk_a/b/c/d.py. Live correctness lives in the
manual preview smoke + the throwaway-copy rehearsal.
"""
import inspect

from api import main as api_main
from api.propelio import archive as propelio_archive


# ─── helper (mirrors the other chunk test files) ─────────────────────────


def _extract_fn_source(module_src: str, fn_name: str) -> str:
    """Source from `def <fn_name>` up to the next top-level `def ` (sibling)."""
    start_marker = f"def {fn_name}"
    if start_marker not in module_src:
        return ""
    start = module_src.index(start_marker)
    rest = module_src[start + len(start_marker):]
    next_def = rest.find("\ndef ")
    if next_def == -1:
        return module_src[start:]
    return module_src[start:start + len(start_marker) + next_def]


# ═════════════════════════════════════════════════════════════════════════
# The per-view parcel loader
# ═════════════════════════════════════════════════════════════════════════


def test_per_view_parcel_loader_exists():
    """The additive per-view parcel loader exists and is callable."""
    assert hasattr(api_main, "_load_parcel_ratings_by_view_for_workspace"), (
        "_load_parcel_ratings_by_view_for_workspace must exist (Chunk E parcel)."
    )
    assert callable(api_main._load_parcel_ratings_by_view_for_workspace)


def test_per_view_parcel_loader_queries_views_table_scoped():
    """The loader reads parcel_ratings_views WHERE workspace_id AND view IN
    ('nbv','export') — workspace-scoped (C4 cross-tenant guard), ARV excluded
    (ARV lives in the separate user_rating path)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_load_parcel_ratings_by_view_for_workspace")
    assert fn, "loader not found"
    assert "FROM parcel_ratings_views" in fn, (
        "loader must read the additive parcel_ratings_views table."
    )
    assert "WHERE workspace_id = %s" in fn, (
        "loader must scope to workspace_id = %s (cross-tenant guard, C4)."
    )
    assert "view IN ('nbv', 'export')" in fn or "view IN ('nbv','export')" in fn, (
        "loader must restrict to the non-ARV views (ARV is the user_rating path)."
    )
    # Reads only — never mutates.
    for pat in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP"):
        assert pat not in fn.upper().replace("ACCOUNT", ""), (
            f"loader must be read-only; found {pat!r}."
        )


def test_per_view_parcel_loader_empty_workspace_returns_empty():
    """No workspace → {} (guard, parallels _load_parcel_ratings_for_workspace)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_load_parcel_ratings_by_view_for_workspace")
    assert "if not workspace_id:" in fn and "return {}" in fn, (
        "loader must short-circuit to {} when workspace_id is falsy."
    )
    # Runtime check: None in → {} out (no DB hit).
    assert api_main._load_parcel_ratings_by_view_for_workspace(None) == {}
    assert api_main._load_parcel_ratings_by_view_for_workspace("") == {}


# ═════════════════════════════════════════════════════════════════════════
# Both hydration sites attach ratings_by_view
# ═════════════════════════════════════════════════════════════════════════


def _assert_site_hydrates(fn_name: str):
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, fn_name)
    assert fn, f"{fn_name} not found"
    # ARV path unchanged: still loads user_rating via the arv loader.
    assert '_load_parcel_ratings_for_workspace(area_id, view="arv")' in fn, (
        f"{fn_name} must still load ARV user_rating unchanged (view='arv')."
    )
    # Per-view path: load the per-view map + attach it to each feature.
    assert "_load_parcel_ratings_by_view_for_workspace(area_id)" in fn, (
        f"{fn_name} must load the per-view parcel ratings map (Chunk E)."
    )
    assert 'feature["properties"]["ratings_by_view"]' in fn, (
        f"{fn_name} must attach ratings_by_view to each feature."
    )
    # Default to {} per feature (never None) — the COALESCE-equivalent.
    assert ".get(_rating_key, {})" in fn, (
        f"{fn_name} must default ratings_by_view to {{}} when a parcel has none."
    )


def test_cached_jobs_site_hydrates_ratings_by_view():
    """_build_features_from_rows (cached-jobs / share-open replay) attaches
    ratings_by_view, ARV user_rating unchanged."""
    _assert_site_hydrates("_build_features_from_rows")


def test_analyze_site_hydrates_ratings_by_view():
    """analyze (/api/analyze live) attaches ratings_by_view, ARV unchanged."""
    _assert_site_hydrates("analyze")


def test_arv_user_rating_attach_still_present_both_sites():
    """Additive guarantee: the existing user_rating attach is untouched at
    both sites (ARV behavior byte-identical)."""
    src = inspect.getsource(api_main)
    for fn_name in ("_build_features_from_rows", "analyze"):
        fn = _extract_fn_source(src, fn_name)
        assert 'feature["properties"]["user_rating"] = parcel_ratings_map.get(_rating_key)' in fn, (
            f"{fn_name} must keep the existing ARV user_rating attach (additive)."
        )


# ═════════════════════════════════════════════════════════════════════════
# _ratingArv leak guard (Chunk E review catch — adversarial verifier #1)
# ═════════════════════════════════════════════════════════════════════════
# The frontend projection stamps a client-internal transient `_ratingArv` onto
# comp objects. The attach-to-area path POSTs those objects, so the archive
# blob must NOT persist `_ratingArv` (baking a stale copy would survive reload
# and poison the client's lazy-capture → silent ARV-rating desync). Stripped on
# BOTH write (merge_comps_into_archive) and read (load_archived_comps).


def _extract_fn(module, fn_name):
    src = inspect.getsource(module)
    start_marker = f"def {fn_name}"
    start = src.index(start_marker)
    rest = src[start + len(start_marker):]
    nxt = rest.find("\ndef ")
    return src if nxt == -1 else src[start:start + len(start_marker) + nxt]


def test_merge_strips_rating_arv_before_persist():
    """merge_comps_into_archive must drop _ratingArv before Json(comp) so the
    client transient never enters the comp_data blob."""
    fn = _extract_fn(propelio_archive, "merge_comps_into_archive")
    assert 'comp.pop("_ratingArv"' in fn, (
        "merge_comps_into_archive must strip _ratingArv before serializing "
        "(else it bakes a stale ARV canonical into the blob — verifier #1)."
    )
    # The strip must occur before the INSERT that serializes the comp.
    strip_idx = fn.index('comp.pop("_ratingArv"')
    insert_idx = fn.index("INSERT INTO propelio_comp_archive")
    assert strip_idx < insert_idx, (
        "_ratingArv strip must run BEFORE the comp_data is serialized/inserted."
    )


def test_load_archived_comps_strips_rating_arv_defensively():
    """load_archived_comps must defensively drop _ratingArv from any blob that
    pre-dates the write-side strip (belt-and-suspenders)."""
    fn = _extract_fn(propelio_archive, "load_archived_comps")
    assert 'comp.pop("_ratingArv"' in fn, (
        "load_archived_comps must strip _ratingArv when reconstructing the comp "
        "(defensive against already-baked blobs — verifier #1)."
    )
