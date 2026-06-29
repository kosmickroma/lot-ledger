"""Chunk B source-inspection tests — per-view ratings READ paths (Alt-D).

Asserts the STRUCTURAL correctness of the read-side changes (matching the
test_multiuser_collab_sprint1_*_schema.py / test_per_view_ratings_chunk_a.py
convention). Live DB validation lives in the manual preview smoke matrix
(per-view ratings spec §6 MUST-PASS). Per spec §7 Chunk B: READ paths only
(§3.2 Alt-D hydrate, §3.3 load_archived_comps reconciliation, H2 parcel
loader view-aware). No write/schema/CSV/fork/frontend changes.

Tests map to FMEA items:
  C3 — dual-source hydrate (ARV user_rating + additive ratings_by_view)
  C4 — every _views JOIN has workspace_id (cross-tenant leak guard)
  H2 — _load_parcel_ratings_for_workspace view-aware (arv→old, nbv/export→new)
  H5 — COALESCE('{}') empty→{} not NULL + GROUP BY every non-aggregated column
"""
import inspect

from api import main as api_main
from api.propelio import archive as propelio_archive


# ─── helpers (mirrors test_per_view_ratings_chunk_a) ─────────────────────


def _extract_fn_source(module_src: str, fn_name: str) -> str:
    """Crude function-source extractor: returns the source from `def <fn_name>`
    up to the next top-level `def ` (sibling). Good enough for inspection."""
    start_marker = f"def {fn_name}"
    if start_marker not in module_src:
        return ""
    start = module_src.index(start_marker)
    rest = module_src[start + len(start_marker):]
    next_def = rest.find("\ndef ")
    if next_def == -1:
        return module_src[start:]
    return module_src[start:start + len(start_marker) + next_def]


def _extract_block(module_src: str, anchor: str, span: int = 2000) -> str:
    """Return source from `anchor` onward for `span` chars."""
    idx = module_src.index(anchor)
    return module_src[idx:idx + span]


# ═════════════════════════════════════════════════════════════════════════
# C3 — Alt-D dual-source hydrate (load_comps_by_polygon)
# ═════════════════════════════════════════════════════════════════════════


def test_c3_load_comps_by_polygon_has_ratings_by_view():
    """C3: load_comps_by_polygon returns ratings_by_view (additive per-view
    blob) alongside the unchanged user_rating (ARV)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_comps_by_polygon")
    assert fn, "load_comps_by_polygon not found"
    assert "ratings_by_view" in fn, (
        "load_comps_by_polygon must add ratings_by_view (spec §3.2 Alt-D, FMEA C3)."
    )


def test_c3_load_comps_by_polygon_keeps_arv_user_rating():
    """C3: the ARV user_rating column from comp_ratings is UNCHANGED — it
    stays as its own column (not folded into ratings_by_view)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_comps_by_polygon")
    assert "cr.rating AS user_rating" in fn, (
        "ARV user_rating must still come from cr.rating (comp_ratings), unchanged."
    )
    assert "LEFT JOIN comp_ratings cr" in fn, (
        "The existing comp_ratings LEFT JOIN must remain (ARV source, unchanged)."
    )


def test_c3_load_comps_by_polygon_adds_views_join():
    """C3: a SECOND LEFT JOIN on comp_ratings_views supplies the per-view
    blob (nbv/export)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_comps_by_polygon")
    assert "LEFT JOIN comp_ratings_views crv" in fn, (
        "load_comps_by_polygon must LEFT JOIN comp_ratings_views crv (spec §3.2)."
    )


def test_c3_ratings_by_view_set_on_comp_dict():
    """C3: each comp dict in the result carries ratings_by_view."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_comps_by_polygon")
    assert 'comp["ratings_by_view"]' in fn, (
        "load_comps_by_polygon must set comp['ratings_by_view'] on each result."
    )


# ═════════════════════════════════════════════════════════════════════════
# C4 — cross-tenant leak guard: every _views JOIN has workspace_id
# ═════════════════════════════════════════════════════════════════════════


def test_c4_load_comps_by_polygon_views_join_has_workspace_id():
    """C4 (CATASTROPHIC): the comp_ratings_views JOIN MUST include
    crv.workspace_id = %(workspace_id)s — without it, two areas rating the
    same comp_id cross-contaminate (cross-tenant leak)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_comps_by_polygon")
    # The _views JOIN block must contain a workspace_id predicate.
    join_idx = fn.index("LEFT JOIN comp_ratings_views crv")
    join_block = fn[join_idx:join_idx + 300]
    assert "crv.workspace_id" in join_block, (
        "comp_ratings_views JOIN MUST scope on crv.workspace_id (FMEA C4 — "
        "without it, ratings leak between areas sharing a comp_id). Block: "
        + join_block
    )


def test_c4_load_archived_comps_views_join_has_workspace_id():
    """C4: same guard in load_archived_comps (the reconciled fallback path)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_archived_comps")
    join_idx = fn.index("LEFT JOIN comp_ratings_views crv")
    join_block = fn[join_idx:join_idx + 300]
    assert "crv.workspace_id" in join_block, (
        "load_archived_comps _views JOIN MUST scope on crv.workspace_id (FMEA C4). "
        "Block: " + join_block
    )


def test_c4_load_archived_comps_arv_join_has_workspace_id():
    """C4: the ARV comp_ratings JOIN in load_archived_comps is also
    workspace-scoped (pa.saved_area_id is the workspace_id for that area)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_archived_comps")
    join_idx = fn.index("LEFT JOIN comp_ratings cr")
    join_block = fn[join_idx:join_idx + 300]
    assert "cr.workspace_id" in join_block, (
        "load_archived_comps ARV comp_ratings JOIN MUST scope on workspace_id "
        "(FMEA C4). Block: " + join_block
    )


def test_c4_no_unscoped_views_join():
    """C4: there must be NO comp_ratings_views JOIN lacking a workspace_id
    predicate anywhere in archive.py. Catches a future edit that adds an
    unscoped join (the catastrophic cross-tenant leak)."""
    src = inspect.getsource(propelio_archive)
    # Every occurrence of "comp_ratings_views crv" must be followed (within
    # 300 chars) by a "crv.workspace_id" predicate.
    import re
    joins = [m.start() for m in re.finditer(r"comp_ratings_views crv", src)]
    assert joins, "expected at least one comp_ratings_views crv JOIN"
    for j in joins:
        block = src[j:j + 300]
        assert "crv.workspace_id" in block, (
            f"Unscoped comp_ratings_views JOIN at offset {j} — missing "
            f"crv.workspace_id (FMEA C4 cross-tenant leak). Block: {block}"
        )


# ═════════════════════════════════════════════════════════════════════════
# H5 — COALESCE('{}') + GROUP BY (no NULL, no row duplication)
# ═════════════════════════════════════════════════════════════════════════


def test_h5_load_comps_by_polygon_coalesce_empty_to_object():
    """H5: COALESCE(..., '{}') so an unrated comp → ratings_by_view={} (never
    SQL NULL — frontend Object.keys(null) would crash)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_comps_by_polygon")
    assert "COALESCE(" in fn and "'{}'" in fn, (
        "ratings_by_view must be COALESCE(..., '{}') so empty → {} not NULL (H5)."
    )
    assert "json_object_agg(crv.view, crv.rating)" in fn, (
        "ratings_by_view must use json_object_agg(crv.view, crv.rating)."
    )
    assert "FILTER (WHERE crv.view IN ('nbv','export'))" in fn, (
        "json_object_agg must FILTER on view IN ('nbv','export') (excludes any "
        "future 'arv' leak — though CHECK already forbids it)."
    )


def test_h5_load_comps_by_polygon_has_group_by():
    """H5: GROUP BY every non-aggregated SELECT column (json_object_agg
    requires it; without it, a comp with 2 _views rows duplicates)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_comps_by_polygon")
    assert "GROUP BY" in fn, (
        "load_comps_by_polygon must GROUP BY (H5 — json_object_agg requires it "
        "or comp rows duplicate when a comp has both nbv+export ratings)."
    )
    # GROUP BY must cover the non-aggregated columns. Check a representative
    # set (parsed_payload, comp_address_key, cr.rating, is_outside_polygon).
    # Use the LAST "GROUP BY" — a docstring may mention "GROUP BY (H5)"; the
    # real SQL GROUP BY is later in the function body.
    group_idx = fn.rindex("GROUP BY")
    group_block = fn[group_idx:group_idx + 400]
    for col in ("pc.parsed_payload", "pc.comp_address_key", "cr.rating", "is_outside_polygon"):
        assert col in group_block, (
            f"GROUP BY must include {col} (H5 — every non-aggregated SELECT "
            f"column). Group block: {group_block}"
        )


def test_h5_load_archived_comps_coalesce_empty_to_object():
    """H5: same COALESCE('{}') guard in load_archived_comps."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_archived_comps")
    assert "COALESCE(" in fn and "'{}'" in fn, (
        "load_archived_comps ratings_by_view must COALESCE(..., '{}') (H5)."
    )
    assert "json_object_agg(crv.view, crv.rating)" in fn


def test_h5_load_archived_comps_has_group_by():
    """H5: GROUP BY in load_archived_comps (the reconciled fallback path)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_archived_comps")
    assert "GROUP BY" in fn, (
        "load_archived_comps must GROUP BY (H5 — json_object_agg requires it)."
    )
    # Use the LAST "GROUP BY" — a docstring may mention "GROUP BY (H5)"; the
    # real SQL GROUP BY is later in the function body.
    group_idx = fn.rindex("GROUP BY")
    group_block = fn[group_idx:group_idx + 400]
    for col in ("pa.comp_data", "pa.comp_address_key", "cr.rating"):
        assert col in group_block, (
            f"load_archived_comps GROUP BY must include {col} (H5). "
            f"Group block: {group_block}"
        )


def test_h5_ratings_by_view_never_null_in_python():
    """H5: the Python hydration defensively sets ratings_by_view to {} (never
    None) even if the DB returns NULL — guards against a driver/config that
    bypasses COALESCE."""
    src = inspect.getsource(propelio_archive)
    for fn_name in ("load_comps_by_polygon", "load_archived_comps"):
        fn = _extract_fn_source(src, fn_name)
        # The else branch must assign {} (not None) as the fallback.
        assert 'comp["ratings_by_view"] = {}' in fn, (
            f"{fn_name} must default ratings_by_view to {{}} (never None) in "
            f"the else branch (H5 defensive)."
        )


# ═════════════════════════════════════════════════════════════════════════
# §3.3 — load_archived_comps reconciliation (latent desync fix)
# ═════════════════════════════════════════════════════════════════════════


def test_load_archived_comps_reads_authoritative_arv_store():
    """§3.3 gap-hunt catch: load_archived_comps must source user_rating from
    comp_ratings (the canonical ARV store), NOT the legacy
    propelio_comp_archive.user_rating column."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_archived_comps")
    assert "LEFT JOIN comp_ratings cr" in fn, (
        "load_archived_comps must JOIN comp_ratings for ARV user_rating "
        "(spec §3.3 — was reading the stale legacy archive column)."
    )
    assert "cr.rating AS user_rating" in fn, (
        "load_archived_comps user_rating must come from cr.rating (comp_ratings)."
    )


def test_load_archived_comps_no_longer_reads_archive_user_rating_column():
    """§3.3: the SELECT must NOT read propelio_comp_archive.user_rating as the
    rating source (it may still be a column on the table, but not SELECTed as
    user_rating). This is the latent-desync fix — set_comp_rating stopped
    writing that column, so reading it showed stale ratings."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_archived_comps")
    # The SELECT list must not have a bare `user_rating` column from the
    # archive table. (pa.user_rating / propelio_comp_archive.user_rating as a
    # selected column = the stale-source bug.)
    select_idx = fn.index("SELECT")
    from_idx = fn.index("FROM propelio_comp_archive")
    select_block = fn[select_idx:from_idx]
    assert "pa.user_rating" not in select_block, (
        "load_archived_comps must NOT SELECT pa.user_rating (the stale legacy "
        "column) — spec §3.3 reconciliation. Select block: " + select_block
    )


def test_load_archived_comps_bridges_via_propelio_comps():
    """§3.3: the archive → comp_ratings bridge goes through propelio_comps
    (archive has comp_address_key; comp_ratings needs comp_id)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_archived_comps")
    assert "LEFT JOIN propelio_comps pc" in fn, (
        "load_archived_comps must JOIN propelio_comps pc to bridge "
        "comp_address_key → comp_id (archive lacks comp_id)."
    )
    assert "pc.comp_address_key = pa.comp_address_key" in fn, (
        "Bridge JOIN must be on comp_address_key."
    )


def test_load_archived_comps_has_ratings_by_view():
    """§3.3: load_archived_comps also returns ratings_by_view (Alt-D shape
    parity with load_comps_by_polygon)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_archived_comps")
    assert "ratings_by_view" in fn, (
        "load_archived_comps must return ratings_by_view (Alt-D parity, §3.3)."
    )


def test_load_archived_comps_sets_comp_address_key():
    """§3.3 parity: load_archived_comps now sets comp_address_key on the comp
    dict (parity with load_comps_by_polygon) so popup rating buttons work on
    the fallback path too. The archive row always has it (NOT NULL)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_archived_comps")
    assert 'comp["comp_address_key"]' in fn, (
        "load_archived_comps must set comp['comp_address_key'] (parity with "
        "load_comps_by_polygon; needed for popup rating buttons)."
    )


# ═════════════════════════════════════════════════════════════════════════
# H2 — _load_parcel_ratings_for_workspace view-aware
# ═════════════════════════════════════════════════════════════════════════


def test_h2_parcel_loader_has_view_param():
    """H2: _load_parcel_ratings_for_workspace signature has a view param
    defaulting to 'arv' (today's behavior; Chunk B only makes it capable)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_load_parcel_ratings_for_workspace")
    assert fn, "_load_parcel_ratings_for_workspace not found"
    assert "view" in fn, (
        "_load_parcel_ratings_for_workspace must accept a `view` param (H2)."
    )
    # Default must be 'arv' so existing callers (Chunk B keeps them at arv)
    # behave identically to today.
    assert 'view: str = "arv"' in fn or "view='arv'" in fn or 'view = "arv"' in fn, (
        "_load_parcel_ratings_for_workspace.view must default to 'arv' (H2 — "
        "today's behavior unless a caller passes a non-arv view)."
    )


def test_h2_parcel_loader_arv_reads_old_table():
    """H2: view='arv' → EXISTING parcel_ratings table (unchanged query)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_load_parcel_ratings_for_workspace")
    assert "FROM parcel_ratings WHERE workspace_id = %s" in fn, (
        "ARV path must read the EXISTING parcel_ratings table (unchanged query, H2)."
    )
    # The ARV query must NOT filter on view (the old table has no view column).
    arv_query_idx = fn.index("FROM parcel_ratings WHERE workspace_id = %s")
    arv_query_block = fn[arv_query_idx:arv_query_idx + 200]
    assert "AND view" not in arv_query_block, (
        "ARV parcel query must NOT filter on view (old table has no view col). "
        "Block: " + arv_query_block
    )


def test_h2_parcel_loader_views_reads_new_table():
    """H2: view in ('nbv','export') → parcel_ratings_views WHERE view=%s."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_load_parcel_ratings_for_workspace")
    assert "FROM parcel_ratings_views" in fn, (
        "NBV/Export path must read parcel_ratings_views (H2)."
    )
    assert "AND view = %s" in fn, (
        "NBV/Export path must filter WHERE view = %s (H2 — per-view scoping)."
    )


def test_h2_parcel_loader_routes_on_view():
    """H2: the function branches on norm_view == 'arv' vs the _views path."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_load_parcel_ratings_for_workspace")
    assert 'norm_view == "arv"' in fn or "norm_view == 'arv'" in fn, (
        "_load_parcel_ratings_for_workspace must branch on norm_view == 'arv' (H2)."
    )


def test_h2_parcel_loader_validates_view():
    """H2: invalid view falls back to 'arv' (defensive — this is a read
    helper, not a request handler, so no 400; just arv fallback)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_load_parcel_ratings_for_workspace")
    assert "norm_view" in fn, (
        "_load_parcel_ratings_for_workspace must normalize view (H2)."
    )
    # Invalid view → arv (defensive fallback).
    assert 'norm_view = "arv"' in fn or "norm_view = 'arv'" in fn, (
        "Invalid/empty view must fall back to 'arv' (H2 defensive)."
    )


def test_h2_callers_pass_explicit_view():
    """H2 (post-Chunk-C): every caller passes an EXPLICIT view — none is left
    unscoped. The analyze hydrate paths still pass view='arv' (active-view
    analyze is Chunk E); the CSV caller now passes the active view
    (view=norm_view) — that is Chunk C's per-view export. The invariant under
    test is 'no caller silently relies on the default', which keeps every read
    intentionally scoped on the shared prod DB."""
    src = inspect.getsource(api_main)
    # Find all call sites.
    call_sites = []
    marker = "_load_parcel_ratings_for_workspace("
    start = 0
    while True:
        idx = src.find(marker, start)
        if idx == -1:
            break
        # Skip the `def ` definition itself.
        if not src[max(0, idx - 4):idx].strip().endswith("def"):
            call_sites.append(src[idx:idx + 120])
        start = idx + 1
    # Expect 3 call sites (analyze hydrate ×2 + CSV).
    assert len(call_sites) >= 3, (
        f"Expected >=3 _load_parcel_ratings_for_workspace call sites, found {len(call_sites)}."
    )
    arv_sites = 0
    view_var_sites = 0
    for site in call_sites:
        is_arv = 'view="arv"' in site or "view='arv'" in site
        # Chunk C's CSV caller passes the normalized active view.
        is_view_var = "view=norm_view" in site
        assert is_arv or is_view_var, (
            f"Every caller must pass an explicit view — either view='arv' "
            f"(analyze) or view=norm_view (CSV per-view, Chunk C). Site: {site}"
        )
        arv_sites += 1 if is_arv else 0
        view_var_sites += 1 if is_view_var else 0
    # Analyze hydrate (×2) still defaults to arv; CSV (×1) is now view-aware.
    assert arv_sites >= 2, (
        f"Expected the 2 analyze hydrate callers to still pass view='arv' "
        f"(active-view analyze is Chunk E); found {arv_sites}."
    )
    assert view_var_sites >= 1, (
        f"Expected the CSV caller to pass view=norm_view (Chunk C per-view "
        f"export); found {view_var_sites}."
    )


# ═════════════════════════════════════════════════════════════════════════
# ARV read-paths byte-for-byte invariant
# ═════════════════════════════════════════════════════════════════════════


def test_arv_comp_ratings_join_unchanged_in_polygon():
    """The existing comp_ratings LEFT JOIN in load_comps_by_polygon keeps its
    workspace_id scoping (the ARV read path is unchanged; ratings_by_view is
    ADDITIVE alongside it)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "load_comps_by_polygon")
    join_idx = fn.index("LEFT JOIN comp_ratings cr")
    join_block = fn[join_idx:join_idx + 200]
    assert "cr.workspace_id" in join_block, (
        "ARV comp_ratings JOIN must keep cr.workspace_id scoping (unchanged). "
        "Block: " + join_block
    )


def test_no_alter_or_drop_anywhere():
    """§8 DB-line: Chunk B (read paths) adds NO destructive DDL. Scoped to the
    THREE functions Chunk B touches — main.py legitimately has pre-existing
    ALTER…ADD COLUMN migrations elsewhere that Chunk B never modified, so a
    whole-file scan would false-positive on them."""
    changed = [
        (propelio_archive, "load_comps_by_polygon"),
        (propelio_archive, "load_archived_comps"),
        (api_main, "_load_parcel_ratings_for_workspace"),
    ]
    for mod, fn_name in changed:
        fn = _extract_fn_source(inspect.getsource(mod), fn_name)
        for pat in ("ALTER TABLE", "DROP TABLE", "DROP INDEX"):
            assert pat not in fn, (
                f"Forbidden destructive DDL {pat!r} found in {mod.__name__}.{fn_name} (spec §8)."
            )


def test_no_csv_or_fork_changes_in_chunk_b():
    """Chunk B = READ paths only. The CSV export block (main.py ~5100+) and
    the fork block (main.py ~7600+) must NOT reference comp_ratings_views yet
    — those are Chunks C and D respectively."""
    src = inspect.getsource(api_main)
    # Fork: the comp_ratings copy block must not yet copy _views.
    fork_idx = src.find("INSERT INTO comp_ratings (workspace_id, comp_id, rating, rated_by_user_id, rated_at)")
    if fork_idx != -1:
        # Look at the fork region (next ~1500 chars) — no _views INSERT there yet.
        fork_region = src[fork_idx:fork_idx + 1500]
        assert "comp_ratings_views" not in fork_region, (
            "Fork must not copy comp_ratings_views yet (Chunk D, not B)."
        )
        assert "parcel_ratings_views" not in fork_region, (
            "Fork must not copy parcel_ratings_views yet (Chunk D, not B)."
        )
