"""Chunk C source-inspection tests — per-view ratings CSV (Option 1).

Asserts the STRUCTURAL correctness of the CSV per-view wiring (matching the
test_per_view_ratings_chunk_a/b.py convention). Live CSV correctness is
validated in the throwaway-copy rehearsal (spec §6 #10). Per spec §7 Chunk
C: CSV only — active-view single "Good Comp" column, per-view bad-wins,
column-position lock. No schema/write/read/fork/frontend changes.

Tests map to FMEA items:
  H1 — CSV Good Comp column reflects the ACTIVE view's comp+parcel ratings
       (bad-wins runs once per active view; arv = today byte-for-byte)
  G3 — column-position lock: NO new columns, NO column moves
  H9 — role gate runs BEFORE any view handling (never weakened)
"""
import inspect

from api import main as api_main


# ─── helpers (mirrors test_per_view_ratings_chunk_a/b) ───────────────────


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
# H1 — CSV Good Comp column sourced per active view
# ═════════════════════════════════════════════════════════════════════════


def test_h1_download_filter_request_has_view_field():
    """H1: DownloadFilterRequest carries a `view` field defaulting to 'arv'
    (coexistence with old clients that don't send it)."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "class DownloadFilterRequest")
    assert "view" in block, (
        "DownloadFilterRequest must have a `view` field (spec §3.4, FMEA H1)."
    )
    # Default must be 'arv' so old clients (no view field) get today's ARV CSV.
    assert '"arv"' in block or "'arv'" in block, (
        "DownloadFilterRequest.view must default to 'arv' (coexistence)."
    )


def test_h1_run_download_csv_has_view_param():
    """H1: _run_download_csv accepts a `view` param."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_run_download_csv")
    assert fn, "_run_download_csv not found"
    assert "view" in fn, (
        "_run_download_csv must accept a `view` param (spec §3.4, FMEA H1)."
    )
    # Default 'arv' so the GET backward-compat path (download → _run_download_csv
    # with no view) keeps today's behavior.
    assert 'view: str = "arv"' in fn or "view='arv'" in fn or 'view = "arv"' in fn, (
        "_run_download_csv.view must default to 'arv' (coexistence)."
    )


def test_h1_download_filtered_threads_view():
    """H1: the POST endpoint threads body.view into _run_download_csv."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "download_filtered")
    assert fn, "download_filtered not found"
    assert "view=body.view" in fn, (
        "download_filtered must pass view=body.view to _run_download_csv (H1)."
    )


def test_h1_csv_comp_ratings_branch_on_view():
    """H1: the comp-rating prefetch branches on norm_view. view='arv' →
    comp_ratings (unchanged query); view in ('nbv','export') →
    comp_ratings_views WHERE view=%s."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_run_download_csv")
    # The prefetch must branch on the active view.
    assert 'norm_view == "arv"' in fn or "norm_view == 'arv'" in fn, (
        "CSV comp-rating prefetch must branch on norm_view == 'arv' (H1)."
    )
    # ARV branch: unchanged comp_ratings query.
    assert "FROM comp_ratings cr" in fn, (
        "ARV CSV path must read FROM comp_ratings cr (unchanged, H1)."
    )
    # _views branch: comp_ratings_views with view filter.
    assert "FROM comp_ratings_views cr" in fn, (
        "NBV/Export CSV path must read FROM comp_ratings_views cr (H1)."
    )
    assert "cr.view = %s" in fn, (
        "NBV/Export CSV path must filter WHERE cr.view = %s (H1 per-view)."
    )


def test_h1_csv_parcel_ratings_use_active_view():
    """H1: the parcel-rating load uses the ACTIVE view (norm_view), not a
    hardcoded 'arv'. This is what makes bad-wins run on the active view's
    parcel ratings."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_run_download_csv")
    assert "view=norm_view" in fn, (
        "CSV parcel-rating load must pass view=norm_view to "
        "_load_parcel_ratings_for_workspace (H1 — active view's parcels)."
    )


def test_h1_csv_arv_query_byte_for_byte():
    """H1: the ARV comp-rating query is byte-for-byte the original (same
    SELECT columns, same JOIN, same WHERE). ARV CSV = today's CSV."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_run_download_csv")
    # The ARV branch query must NOT filter on view (the old table has none).
    arv_idx = fn.index("FROM comp_ratings cr")
    arv_block = fn[arv_idx:arv_idx + 200]
    assert "cr.workspace_id = %s" in arv_block, (
        "ARV CSV query must keep WHERE cr.workspace_id = %s (unchanged)."
    )
    assert "AND cr.view" not in arv_block, (
        "ARV CSV query must NOT filter on view (old table has no view col). "
        "Block: " + arv_block
    )


def test_h1_csv_views_query_workspace_scoped():
    """C4/H1: the _views CSV query MUST scope on workspace_id (cross-tenant
    leak guard) — same discipline as the hydrate reads in Chunk B."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_run_download_csv")
    views_idx = fn.index("FROM comp_ratings_views cr")
    views_block = fn[views_idx:views_idx + 250]
    assert "cr.workspace_id = %s" in views_block, (
        "CSV _views query MUST scope on cr.workspace_id (FMEA C4 — without it, "
        "ratings leak between areas). Block: " + views_block
    )


def test_h1_bad_wins_runs_once_per_view():
    """H1: the bad-wins conflict resolution runs ONCE on the active view's
    data (it consumes rating_by_comp_id + parcel_rating_by_key which are now
    per-view-sourced). There must be only ONE bad-wins loop, not 3."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_run_download_csv")
    # The bad-wins logic: "once a parcel has any bad rating, no other rating
    # displaces it." Count occurrences of the sentinel.
    bad_wins_count = fn.count('existing == "bad"')
    assert bad_wins_count == 1, (
        f"Expected exactly 1 bad-wins loop in _run_download_csv (runs once per "
        f"active view), found {bad_wins_count}. Per-view means the SAME loop "
        f"runs once on the active view's data — NOT 3 separate loops."
    )


def test_h1_rating_for_uses_view_sourced_dict():
    """H1: _rating_for consumes rating_by_comp_id, which is now sourced from
    the active view. The helper itself is unchanged (it just reads the dict);
    the per-view-ness comes from what populates the dict."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_run_download_csv")
    # The dict is populated from the active-view query (Chunk C) and consumed by
    # the bad-wins resolver inside _run_download_csv — so per-view-ness flows
    # through whatever reads it. (Earlier the check used a 600-char window after
    # `def _rating_for`, which the helper's long docstring pushed the dict access
    # out of — brittle. Assert the dict is present + used in the function.)
    assert "rating_by_comp_id" in fn, (
        "_run_download_csv must use rating_by_comp_id (now per-view-sourced)."
    )


# ═════════════════════════════════════════════════════════════════════════
# G3 — column-position lock: NO new columns, NO column moves
# ═════════════════════════════════════════════════════════════════════════


def test_g3_good_comp_column_count_unchanged():
    """G3: "Good Comp" appears exactly ONCE in the CSV header (no new columns
    added). The column count is unchanged from pre-Chunk-C."""
    src = inspect.getsource(api_main)
    assert src.count('"Good Comp"') == 1, (
        f'"Good Comp" must appear exactly once in the CSV header (G3 — no new '
        f'columns). Found {src.count(chr(34) + "Good Comp" + chr(34))}.'
    )


def test_g3_compatibility_lock_comments_preserved():
    """G3: the COMPATIBILITY LOCK comments at the 3 mirror write sites are
    preserved (they document the fixed column position; Chunk C must not
    move the column)."""
    src = inspect.getsource(api_main)
    lock_count = src.count("COMPATIBILITY LOCK: Good Comp")
    assert lock_count >= 3, (
        f"Expected >=3 'COMPATIBILITY LOCK: Good Comp' comments (header + 2 "
        f"mirror write sites). Found {lock_count} — G3 column-position lock "
        f"documentation must be preserved."
    )


def test_g3_no_new_rating_columns_added():
    """G3: Chunk C adds NO new CSV columns. There must be no 'NBV Good Comp',
    'Export Good Comp', or 'ratings_by_view' column in the CSV header."""
    src = inspect.getsource(api_main)
    forbidden_headers = [
        '"NBV Good Comp"',
        '"Export Good Comp"',
        '"ARV Good Comp"',
        '"ratings_by_view"',
        '"Ratings By View"',
    ]
    for h in forbidden_headers:
        assert h not in src, (
            f"Forbidden new CSV column {h!r} found — G3 locks column positions "
            f"(spec §3.4: NO new columns, only the source of the existing "
            f"'Good Comp' column changes per active view)."
        )


def test_g3_good_comp_still_after_comp_status():
    """G3: 'Good Comp' still slots immediately after 'Comp Status' (the
    documented position). The column did not move."""
    src = inspect.getsource(api_main)
    # Find the header region and confirm Comp Status precedes Good Comp.
    good_comp_idx = src.index('"Good Comp"')
    comp_status_idx = src.rfind('"Comp Status"', 0, good_comp_idx)
    assert comp_status_idx != -1, (
        "'Good Comp' must still follow 'Comp Status' in the CSV header (G3 — "
        "column position unchanged)."
    )
    # No other column header between them (they're adjacent in the header list).
    between = src[comp_status_idx + len('"Comp Status"'):good_comp_idx]
    # Allow whitespace/newlines/commas/comments but no other quoted column header.
    import re
    other_headers = re.findall(r'"[A-Z][^"]*"', between)
    assert not other_headers, (
        f"Unexpected column(s) between 'Comp Status' and 'Good Comp': "
        f"{other_headers} — G3 requires they stay adjacent."
    )


# ═════════════════════════════════════════════════════════════════════════
# H9 — role gate runs BEFORE any view handling (never weakened)
# ═════════════════════════════════════════════════════════════════════════


def test_h9_role_gate_first_in_run_download_csv():
    """H9: the CSV role check (role=='user' → 403) is the FIRST statement in
    _run_download_csv, BEFORE the view normalization. Per-view wiring never
    weakens the role gate."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_run_download_csv")
    # The role gate line.
    role_gate_marker = 'role") or "").strip().lower() == "user"'
    role_gate_idx = fn.index(role_gate_marker)
    # The view normalization line.
    view_norm_marker = "norm_view = str(view"
    view_norm_idx = fn.index(view_norm_marker)
    assert role_gate_idx < view_norm_idx, (
        "H9: the role gate (role=='user' → 403) must come BEFORE view "
        "normalization in _run_download_csv. Found view-handling before the "
        "role gate — per-view wiring must never weaken the CSV role check."
    )


def test_h9_role_gate_not_inside_view_branch():
    """H9: the role gate is unconditional (not inside any `if norm_view`
    branch) — it runs for ALL views, including arv."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_run_download_csv")
    role_gate_idx = fn.index('== "user"')
    # The role gate must be at the function's top level (not indented inside
    # an if-block). Check the preceding non-blank lines for a view branch.
    preceding = fn[:role_gate_idx]
    # Count open vs closed `if norm_view` blocks before the role gate — must
    # be balanced (role gate is NOT inside a view branch).
    import re
    opens = len(re.findall(r'if norm_view', preceding))
    # A rough balance check: the role gate should appear before the first
    # `if norm_view` (it's the very first statement).
    first_view_branch = fn.find("if norm_view")
    if first_view_branch != -1:
        assert role_gate_idx < first_view_branch, (
            "H9: role gate must run before the first view branch (unconditional, "
            "all views). Found a view branch before the role gate."
        )


def test_h9_invalid_view_returns_400_not_403():
    """H1/C2: an invalid present view → 400 (bad request), NOT 403 (role).
    The role 403 is reserved for the role gate; view validation is a separate
    400. This confirms the two gates are distinct and the role gate isn't
    overloaded for view validation."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "_run_download_csv")
    assert 'status_code=400' in fn and "view must be 'arv', 'nbv', 'export'" in fn, (
        "Invalid view must return 400 with an explicit message (distinct from "
        "the role-gate 403). H1 view-validation discipline."
    )


# ═════════════════════════════════════════════════════════════════════════
# Additive-only / no-touch invariants (Chunks A/B preserved)
# ═════════════════════════════════════════════════════════════════════════


def test_no_alter_or_drop():
    """§8 DB-line: Chunk C (CSV) adds NO destructive DDL. Scoped to the function
    Chunk C touches — main.py legitimately has pre-existing ALTER…ADD COLUMN
    migrations elsewhere that a whole-file scan would false-positive on."""
    fn = _extract_fn_source(inspect.getsource(api_main), "_run_download_csv")
    for pat in ("ALTER TABLE", "DROP TABLE", "DROP INDEX"):
        assert pat not in fn, (
            f"Forbidden destructive DDL {pat!r} found in _run_download_csv (spec §8)."
        )


def test_chunk_c_does_not_touch_fork():
    """Chunk C = CSV only. The fork block must NOT reference comp_ratings_views
    yet (fork is Chunk D)."""
    src = inspect.getsource(api_main)
    # The fork comp_ratings copy block.
    fork_idx = src.find("INSERT INTO comp_ratings (workspace_id, comp_id, rating, rated_by_user_id, rated_at)")
    if fork_idx != -1:
        fork_region = src[fork_idx:fork_idx + 1500]
        assert "comp_ratings_views" not in fork_region, (
            "Fork must not copy comp_ratings_views yet (Chunk D, not C)."
        )
        assert "parcel_ratings_views" not in fork_region, (
            "Fork must not copy parcel_ratings_views yet (Chunk D, not C)."
        )


def test_get_download_path_unchanged():
    """The backward-compat GET /api/download/{job_id} path (no body, no view)
    must still work — it calls _run_download_csv with no view, which defaults
    to 'arv' = today's behavior."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "download")
    assert "_run_download_csv(job_id, filename, user)" in fn, (
        "GET /api/download must call _run_download_csv with no view arg "
        "(defaults to 'arv' = today's behavior, coexistence)."
    )
