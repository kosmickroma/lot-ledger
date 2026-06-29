"""Chunk D source-inspection tests — per-view ratings fork copy.

Asserts the STRUCTURAL correctness of the fork copy wiring (matching the
test_per_view_ratings_chunk_a/b/c.py convention). Live fork correctness is
validated in the throwaway-copy rehearsal (spec §6 #9). Per spec §7 Chunk
D: fork copies the _views tables in-txn, parallel to the existing
comp_ratings/parcel_ratings copies. No schema/write/read/CSV/frontend changes.

Tests map to FMEA items:
  C5 — fork copies _views tables atomically (same txn as the ARV copies)
  G9 — fork INSERTs inside the existing txn before commit
"""
import inspect
import re

from api import main as api_main


# ─── helpers (mirrors test_per_view_ratings_chunk_a/b/c) ─────────────────


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


# ═════════════════════════════════════════════════════════════════════════
# C5 — fork copies both _views tables (INSERT ... SELECT ... FROM _views)
# ═════════════════════════════════════════════════════════════════════════


def test_c5_fork_copies_comp_ratings_views():
    """C5: fork_saved_area contains an INSERT INTO comp_ratings_views ...
    SELECT ... FROM comp_ratings_views so the fork inherits the source's
    per-view comp ratings (nbv/export)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    assert fn, "fork_saved_area not found"
    assert "INSERT INTO comp_ratings_views" in fn, (
        "fork_saved_area must INSERT INTO comp_ratings_views (spec §3.3, FMEA C5)."
    )
    assert "FROM comp_ratings_views" in fn, (
        "The comp_ratings_views fork copy must SELECT FROM comp_ratings_views."
    )


def test_c5_fork_copies_parcel_ratings_views():
    """C5: fork_saved_area contains an INSERT INTO parcel_ratings_views ...
    SELECT ... FROM parcel_ratings_views so the fork inherits the source's
    per-view parcel ratings (nbv/export)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    assert "INSERT INTO parcel_ratings_views" in fn, (
        "fork_saved_area must INSERT INTO parcel_ratings_views (spec §3.3, FMEA C5)."
    )
    assert "FROM parcel_ratings_views" in fn, (
        "The parcel_ratings_views fork copy must SELECT FROM parcel_ratings_views."
    )


def test_c5_fork_views_copies_use_source_workspace_filter():
    """C5: both _views fork copies filter WHERE workspace_id = %s (the source
    area) so only the source's per-view ratings are copied — not every area's."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    # Each _views INSERT block must have a WHERE workspace_id = %s.
    for table in ("comp_ratings_views", "parcel_ratings_views"):
        insert_idx = fn.index(f"INSERT INTO {table}")
        # Find the matching FROM ... WHERE within the next ~400 chars.
        block = fn[insert_idx:insert_idx + 400]
        assert "WHERE workspace_id = %s" in block, (
            f"{table} fork copy must filter WHERE workspace_id = %s (only the "
            f"source's ratings). Block: {block}"
        )


def test_c5_fork_views_copies_use_row0_as_new_workspace():
    """C5: both _views fork copies SELECT %s (the new fork area_id = row[0])
    as the destination workspace_id — same pattern as the ARV copies."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    for table in ("comp_ratings_views", "parcel_ratings_views"):
        insert_idx = fn.index(f"INSERT INTO {table}")
        block = fn[insert_idx:insert_idx + 400]
        assert "SELECT %s" in block, (
            f"{table} fork copy must SELECT %s (new fork area_id) as the "
            f"destination workspace_id. Block: {block}"
        )
        # Params must be (row[0], source_area_id) — same as the ARV copies.
        assert "row[0]" in block and "source_area_id" in block, (
            f"{table} fork copy params must be (row[0], source_area_id) — "
            f"same as the ARV rating copies. Block: {block}"
        )


# ═════════════════════════════════════════════════════════════════════════
# C5 — `view` column copied verbatim; `rating_id` excluded (BIGSERIAL)
# ═════════════════════════════════════════════════════════════════════════


def test_c5_fork_comp_views_includes_view_column():
    """C5: the comp_ratings_views INSERT column list includes `view` (copied
    verbatim from the source — the per-view dimension travels with the fork)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    insert_idx = fn.index("INSERT INTO comp_ratings_views")
    # The INSERT column list is on the same line / next few lines.
    insert_block = fn[insert_idx:insert_idx + 300]
    assert "view" in insert_block, (
        "comp_ratings_views INSERT must include the `view` column (copied "
        "verbatim). Block: " + insert_block
    )
    # And the SELECT must also reference view (so it's copied, not defaulted).
    select_idx = insert_block.index("SELECT")
    select_block = insert_block[select_idx:select_idx + 200]
    assert "view" in select_block, (
        "comp_ratings_views SELECT must copy `view` verbatim. Block: " + select_block
    )


def test_c5_fork_parcel_views_includes_view_column():
    """C5: same for parcel_ratings_views — `view` in both INSERT + SELECT."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    insert_idx = fn.index("INSERT INTO parcel_ratings_views")
    insert_block = fn[insert_idx:insert_idx + 300]
    assert "view" in insert_block, (
        "parcel_ratings_views INSERT must include the `view` column. Block: "
        + insert_block
    )
    select_idx = insert_block.index("SELECT")
    select_block = insert_block[select_idx:select_idx + 200]
    assert "view" in select_block, (
        "parcel_ratings_views SELECT must copy `view` verbatim. Block: "
        + select_block
    )


def test_c5_fork_views_exclude_rating_id():
    """C5: rating_id is NOT in either _views INSERT column list (it's
    BIGSERIAL PRIMARY KEY — must auto-generate, not be copied). Copying it
    would collide with existing rows / break the sequence."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    for table in ("comp_ratings_views", "parcel_ratings_views"):
        insert_idx = fn.index(f"INSERT INTO {table}")
        # The column list runs from the INSERT to the SELECT.
        select_idx_in_block = fn.index("SELECT", insert_idx)
        col_list = fn[insert_idx:select_idx_in_block]
        assert "rating_id" not in col_list, (
            f"{table} fork INSERT must NOT include rating_id (BIGSERIAL — let "
            f"it auto-generate). Column list: {col_list}"
        )


def test_c5_fork_views_column_lists_match_schema():
    """C5: the INSERT column lists match the Chunk A schema exactly (minus
    rating_id). comp_ratings_views: (workspace_id, comp_id, view, rating,
    rated_by_user_id, rated_at). parcel_ratings_views: (workspace_id, county,
    account_num, view, rating, rated_by_user_id, rated_at)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")

    # comp_ratings_views column list.
    comp_idx = fn.index("INSERT INTO comp_ratings_views")
    comp_select = fn.index("SELECT", comp_idx)
    comp_cols = fn[comp_idx:comp_select]
    for col in ("workspace_id", "comp_id", "view", "rating", "rated_by_user_id", "rated_at"):
        assert col in comp_cols, (
            f"comp_ratings_views INSERT must include {col!r} (Chunk A schema). "
            f"Column list: {comp_cols}"
        )

    # parcel_ratings_views column list.
    parcel_idx = fn.index("INSERT INTO parcel_ratings_views")
    parcel_select = fn.index("SELECT", parcel_idx)
    parcel_cols = fn[parcel_idx:parcel_select]
    for col in ("workspace_id", "county", "account_num", "view", "rating", "rated_by_user_id", "rated_at"):
        assert col in parcel_cols, (
            f"parcel_ratings_views INSERT must include {col!r} (Chunk A schema). "
            f"Column list: {parcel_cols}"
        )


# ═════════════════════════════════════════════════════════════════════════
# C5/G9 — same transaction (atomic): no separate commit/conn before the copies
# ═════════════════════════════════════════════════════════════════════════


def test_c5_fork_views_inside_same_transaction():
    """C5/G9: the _views copies are inside the SAME try-block / transaction
    as the ARV comp_ratings + parcel_ratings copies. There must be NO
    conn.commit() between the ARV rating copies and the _views copies, and
    NO new connection (get_session_conn) opened for them."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    # Anchor on the ARV comp_ratings copy — the _views copies must come after.
    arv_idx = fn.index("INSERT INTO comp_ratings (workspace_id, comp_id, rating, rated_by_user_id, rated_at)")
    views_idx = fn.index("INSERT INTO comp_ratings_views")
    assert views_idx > arv_idx, (
        "comp_ratings_views fork copy must come AFTER the ARV comp_ratings copy."
    )
    # Between the ARV copy and the _views copy: no commit, no new conn.
    between = fn[arv_idx:views_idx]
    assert "conn.commit()" not in between, (
        "G9: no conn.commit() between the ARV rating copy and the _views copy "
        "— they must be in the same transaction (atomic)."
    )
    assert "get_session_conn" not in between, (
        "C5: no new connection opened for the _views copy — same cursor + txn."
    )


def test_c5_fork_views_before_commit():
    """C5/G9: the _views copies come BEFORE the conn.commit() that finalizes
    the fork transaction (so they commit atomically with the rest)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    parcel_views_idx = fn.index("INSERT INTO parcel_ratings_views")
    # The commit that ends the try-block.
    commit_idx = fn.index("conn.commit()", parcel_views_idx)
    assert commit_idx > parcel_views_idx, (
        "G9: the _views fork copies must come BEFORE conn.commit() (atomic "
        "with the rest of the fork transaction)."
    )
    # And no intermediate commit between the _views copy and the final commit.
    between = fn[parcel_views_idx:commit_idx]
    # The stored_value_entries copy legitimately sits between — that's fine.
    # But no conn.commit() there.
    assert "conn.commit()" not in between, (
        "G9: no intermediate commit between the _views copy and the fork's "
        "final conn.commit() — atomic."
    )


def test_c5_fork_views_grouped_with_rating_copies():
    """C5: the _views copies are grouped with the rating copies (after
    parcel_ratings, before stored_value_entries) — not scattered."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    parcel_arv_idx = fn.index("INSERT INTO parcel_ratings (workspace_id, county, account_num, rating, rated_by_user_id, rated_at)")
    comp_views_idx = fn.index("INSERT INTO comp_ratings_views")
    parcel_views_idx = fn.index("INSERT INTO parcel_ratings_views")
    stored_idx = fn.index("INSERT INTO stored_value_entries")
    # Order: parcel_ratings (ARV) < comp_ratings_views < parcel_ratings_views < stored_value_entries
    assert parcel_arv_idx < comp_views_idx < parcel_views_idx < stored_idx, (
        "C5: the _views copies must be grouped between the ARV parcel_ratings "
        "copy and the stored_value_entries copy (all rating copies together)."
    )


# ═════════════════════════════════════════════════════════════════════════
# Additive-only: existing ARV copies untouched
# ═════════════════════════════════════════════════════════════════════════


def test_arv_comp_ratings_copy_still_present():
    """The existing ARV comp_ratings fork copy is untouched (additive only)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    assert "INSERT INTO comp_ratings (workspace_id, comp_id, rating, rated_by_user_id, rated_at)" in fn, (
        "The existing ARV comp_ratings fork copy must remain (additive only)."
    )
    assert "FROM comp_ratings\n" in fn or "FROM comp_ratings " in fn, (
        "ARV comp_ratings fork copy must still SELECT FROM comp_ratings."
    )


def test_arv_parcel_ratings_copy_still_present():
    """The existing ARV parcel_ratings fork copy is untouched (additive only)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    assert "INSERT INTO parcel_ratings (workspace_id, county, account_num, rating, rated_by_user_id, rated_at)" in fn, (
        "The existing ARV parcel_ratings fork copy must remain (additive only)."
    )


def test_no_alter_or_drop():
    """§8 DB-line: Chunk D introduces NO destructive DDL. Scoped to
    fork_saved_area (Chunk D's only edit) — a whole-file scan false-positives
    on api/main.py's pre-existing legit migrations that contain ALTER/DROP."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    assert fn, "fork_saved_area not found"
    for pat in ("ALTER TABLE", "DROP TABLE", "DROP INDEX"):
        assert pat not in fn, (
            f"Forbidden destructive DDL {pat!r} found in fork_saved_area "
            f"(spec §8 — Chunk D must be additive-only)."
        )


def test_fork_views_do_not_touch_arv_tables():
    """C6/§8: the _views fork copies must NOT write into the ARV tables
    (comp_ratings / parcel_ratings). The `view` CHECK excludes 'arv' in the
    _views tables, and the ARV tables must not receive per-view rows."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "fork_saved_area")
    # The comp_ratings_views INSERT must SELECT FROM comp_ratings_views, NOT
    # FROM comp_ratings (no ARV→_views cross-contamination, and no
    # _views→ARV writes).
    comp_views_idx = fn.index("INSERT INTO comp_ratings_views")
    comp_views_block = fn[comp_views_idx:comp_views_idx + 400]
    assert "FROM comp_ratings_views" in comp_views_block, (
        "comp_ratings_views fork copy must SELECT FROM comp_ratings_views "
        "(not the ARV comp_ratings table)."
    )
    parcel_views_idx = fn.index("INSERT INTO parcel_ratings_views")
    parcel_views_block = fn[parcel_views_idx:parcel_views_idx + 400]
    assert "FROM parcel_ratings_views" in parcel_views_block, (
        "parcel_ratings_views fork copy must SELECT FROM parcel_ratings_views "
        "(not the ARV parcel_ratings table)."
    )
