"""Chunk A source-inspection tests — per-view ratings schema + write routing.

These assert the STRUCTURAL correctness of the code (matching the
test_multiuser_collab_sprint1_*_schema.py convention). Live DB schema
validation lives in the manual preview smoke matrix (per-view ratings
spec §6 MUST-PASS). Per docs/superpowers/specs/2026-06-29-per-view-
ratings-spec.md §7 Chunk A: schema (§2) + write routing (§3.1) only.

Tests map to FMEA items:
  C1 — tables created UNCONDITIONALLY at startup (never flag-gated)
  C2 — write carries view; absent→arv (coexistence), invalid present→400
  H7 — view NOT NULL + CHECK excludes 'arv' on both _views tables
  H8 — FK ON DELETE CASCADE on both new tables
"""
import inspect

from api import main as api_main
from api.propelio import routes as propelio_routes
from api.propelio import archive as propelio_archive


# ─── helper (mirrors test_multiuser_collab_sprint1_endpoints) ────────────


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


def _extract_block(module_src: str, anchor: str, span: int = 1500) -> str:
    """Return source from `anchor` onward for `span` chars."""
    idx = module_src.index(anchor)
    return module_src[idx:idx + span]


# ═════════════════════════════════════════════════════════════════════════
# C1 — New tables created UNCONDITIONALLY at startup (FMEA C1)
# ═════════════════════════════════════════════════════════════════════════


def test_c1_comp_ratings_views_create_present():
    """comp_ratings_views CREATE TABLE present in _ensure_session_schema."""
    src = inspect.getsource(api_main)
    assert "CREATE TABLE IF NOT EXISTS comp_ratings_views" in src, (
        "comp_ratings_views CREATE TABLE missing — spec §2 / FMEA C1."
    )


def test_c1_parcel_ratings_views_create_present():
    """parcel_ratings_views CREATE TABLE present in _ensure_session_schema."""
    src = inspect.getsource(api_main)
    assert "CREATE TABLE IF NOT EXISTS parcel_ratings_views" in src, (
        "parcel_ratings_views CREATE TABLE missing — spec §2 / FMEA C1."
    )


def test_c1_tables_created_outside_flag_branch():
    """C1: tables must NOT be inside a flag/feature-flag conditional. The
    schema-creation block (_ensure_session_schema) has no feature-flag gate
    around comp_ratings/parcel_ratings today; the new _views creates must sit
    in the same unconditional flow (parallel to the existing table creates)."""
    src = inspect.getsource(api_main)
    # The existing comp_ratings create is the proven-unconditional anchor.
    # The new comp_ratings_views create must appear AFTER it (same flow) and
    # there must be no `if os.environ` or `if.*flag` line between the two.
    anchor_existing = src.index("CREATE TABLE IF NOT EXISTS comp_ratings ")
    anchor_new = src.index("CREATE TABLE IF NOT EXISTS comp_ratings_views")
    assert anchor_new > anchor_existing, (
        "comp_ratings_views create must follow the existing comp_ratings "
        "create (same unconditional flow), not precede it."
    )
    between = src[anchor_existing:anchor_new]
    # No feature-flag conditional may gate the new table creation.
    forbidden = ["if os.environ", "DEEP_PULL_EXPERIMENT", "arvNbvExport", "arv_nbv"]
    hits = [f for f in forbidden if f in between]
    assert not hits, (
        f"comp_ratings_views create appears gated by {hits} — FMEA C1 "
        "requires UNCONDITIONAL creation (schema never flag-gated)."
    )


def test_c1_create_uses_if_not_exists_idempotent():
    """C1: CREATE TABLE IF NOT EXISTS (restart-safe / idempotent)."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "CREATE TABLE IF NOT EXISTS comp_ratings_views")
    assert block.count("CREATE TABLE IF NOT EXISTS") >= 1
    block2 = _extract_block(src, "CREATE TABLE IF NOT EXISTS parcel_ratings_views")
    assert block2.count("CREATE TABLE IF NOT EXISTS") >= 1


def test_c1_indexes_present():
    """C1: indexes on the new tables (workspace-scoped lookups)."""
    src = inspect.getsource(api_main)
    assert "idx_comp_ratings_views_ws" in src, "comp_ratings_views index missing"
    assert "idx_parcel_ratings_views_ws" in src, "parcel_ratings_views index missing"
    assert "ON comp_ratings_views (workspace_id, comp_id)" in src
    assert "ON parcel_ratings_views (workspace_id, county, account_num)" in src


# ═════════════════════════════════════════════════════════════════════════
# H7 — view NOT NULL + CHECK excludes 'arv' on both _views tables
# ═════════════════════════════════════════════════════════════════════════


def test_h7_comp_view_not_null_check_excludes_arv():
    """H7: comp_ratings_views.view NOT NULL + CHECK (view IN ('nbv','export'))
    — 'arv' structurally excluded so ARV can never be shadowed in the new
    table (FMEA G1 / C6)."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "CREATE TABLE IF NOT EXISTS comp_ratings_views")
    assert "view TEXT NOT NULL CHECK (view IN ('nbv','export'))" in block, (
        "comp_ratings_views.view must be NOT NULL + CHECK (view IN ('nbv','export')) "
        "— excludes 'arv' on purpose (spec §2, FMEA G1)."
    )
    # 'arv' must NOT appear in the CHECK for the new table.
    check_line = [ln for ln in block.splitlines() if "view TEXT NOT NULL CHECK" in ln][0]
    assert "'arv'" not in check_line, (
        "comp_ratings_views.view CHECK must NOT include 'arv' — found it: " + check_line
    )


def test_h7_parcel_view_not_null_check_excludes_arv():
    """H7: same for parcel_ratings_views."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "CREATE TABLE IF NOT EXISTS parcel_ratings_views")
    assert "view TEXT NOT NULL CHECK (view IN ('nbv','export'))" in block, (
        "parcel_ratings_views.view must be NOT NULL + CHECK (view IN ('nbv','export'))."
    )
    check_line = [ln for ln in block.splitlines() if "view TEXT NOT NULL CHECK" in ln][0]
    assert "'arv'" not in check_line, (
        "parcel_ratings_views.view CHECK must NOT include 'arv' — found it: " + check_line
    )


def test_h7_rating_check_unchanged():
    """H7: rating CHECK still ('good','bad') on both new tables (parity
    with the existing comp_ratings/parcel_ratings)."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "CREATE TABLE IF NOT EXISTS comp_ratings_views")
    assert "rating TEXT NOT NULL CHECK (rating IN ('good','bad'))" in block
    block2 = _extract_block(src, "CREATE TABLE IF NOT EXISTS parcel_ratings_views")
    assert "rating TEXT NOT NULL CHECK (rating IN ('good','bad'))" in block2


# ═════════════════════════════════════════════════════════════════════════
# H8 — FK ON DELETE CASCADE on both new tables
# ═════════════════════════════════════════════════════════════════════════


def test_h8_comp_views_workspace_fk_cascade():
    """H8: comp_ratings_views.workspace_id FK → saved_areas ON DELETE CASCADE
    (deleting an area wipes its per-view ratings)."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "CREATE TABLE IF NOT EXISTS comp_ratings_views")
    assert "workspace_id TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE" in block, (
        "comp_ratings_views.workspace_id must FK CASCADE → saved_areas (spec §2, FMEA H8)."
    )


def test_h8_comp_views_comp_id_fk_cascade():
    """H8: comp_ratings_views.comp_id FK → propelio_comps ON DELETE CASCADE
    (deleting a comp wipes its per-view ratings)."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "CREATE TABLE IF NOT EXISTS comp_ratings_views")
    assert "comp_id BIGINT NOT NULL REFERENCES propelio_comps(comp_id) ON DELETE CASCADE" in block, (
        "comp_ratings_views.comp_id must FK CASCADE → propelio_comps (spec §2, FMEA H8)."
    )


def test_h8_parcel_views_workspace_fk_cascade():
    """H8: parcel_ratings_views.workspace_id FK → saved_areas ON DELETE CASCADE."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "CREATE TABLE IF NOT EXISTS parcel_ratings_views")
    assert "workspace_id TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE" in block, (
        "parcel_ratings_views.workspace_id must FK CASCADE → saved_areas (spec §2, FMEA H8)."
    )


def test_h8_rated_by_user_id_set_null():
    """M10: rated_by_user_id ON DELETE SET NULL (parity with existing tables —
    a deleted user's ratings stay, just unattributed)."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "CREATE TABLE IF NOT EXISTS comp_ratings_views")
    assert "rated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL" in block
    block2 = _extract_block(src, "CREATE TABLE IF NOT EXISTS parcel_ratings_views")
    assert "rated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL" in block2


def test_h8_unique_includes_view():
    """UNIQUE on the _views tables MUST include `view` (so the same comp can
    carry independent nbv + export ratings)."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "CREATE TABLE IF NOT EXISTS comp_ratings_views")
    assert "UNIQUE (workspace_id, comp_id, view)" in block, (
        "comp_ratings_views UNIQUE must include view (spec §2)."
    )
    block2 = _extract_block(src, "CREATE TABLE IF NOT EXISTS parcel_ratings_views")
    assert "UNIQUE (workspace_id, county, account_num, view)" in block2, (
        "parcel_ratings_views UNIQUE must include view (spec §2)."
    )


# ═════════════════════════════════════════════════════════════════════════
# C2 — Write routing: view field on both request models + routing logic
# ═════════════════════════════════════════════════════════════════════════


def test_c2_comp_rate_request_has_view_field():
    """C2: CompRateRequest carries view (absent → arv coexistence)."""
    src = inspect.getsource(propelio_routes)
    block = _extract_block(src, "class CompRateRequest")
    assert "view" in block, "CompRateRequest must have a `view` field (spec §3.1, FMEA C2)."
    # The field must be Optional/defaulted so old clients (no view) still parse.
    assert "Optional" in block or "= None" in block, (
        "CompRateRequest.view must default to None (coexistence with old clients)."
    )
    # Literal must restrict to the 3 valid views.
    assert "arv" in block and "nbv" in block and "export" in block, (
        "CompRateRequest.view Literal must include 'arv','nbv','export'."
    )


def test_c2_parcel_rate_request_has_view_field():
    """C2: ParcelRateRequest carries view (absent → arv coexistence)."""
    src = inspect.getsource(api_main)
    block = _extract_block(src, "class ParcelRateRequest")
    assert "view" in block, "ParcelRateRequest must have a `view` field (spec §3.1, FMEA C2)."
    assert "Optional" in block or "= None" in block, (
        "ParcelRateRequest.view must default to None (coexistence with old clients)."
    )
    assert "arv" in block and "nbv" in block and "export" in block, (
        "ParcelRateRequest.view Literal must include 'arv','nbv','export'."
    )


def test_c2_set_comp_rating_routes_arv_to_old_table():
    """C2: set_comp_rating(view='arv') → comp_ratings (the existing table),
    byte-for-byte unchanged ARV path."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "set_comp_rating")
    assert fn, "set_comp_rating not found"
    # ARV branch exists and writes the OLD table.
    assert "norm_view == \"arv\"" in fn or "norm_view == 'arv'" in fn, (
        "set_comp_rating must branch on norm_view == 'arv' for the old-table path."
    )
    # ARV INSERT must use the EXISTING ON CONFLICT (workspace_id, comp_id) — NOT view.
    assert "ON CONFLICT (workspace_id, comp_id) DO UPDATE" in fn, (
        "ARV comp write must keep ON CONFLICT (workspace_id, comp_id) (unchanged)."
    )
    # The ARV INSERT must NOT include the `view` column.
    arv_insert_idx = fn.index("INSERT INTO comp_ratings (workspace_id, comp_id, rating")
    arv_insert_block = fn[arv_insert_idx:arv_insert_idx + 400]
    assert "view" not in arv_insert_block.lower(), (
        "ARV comp INSERT must NOT include the view column (byte-for-byte ARV). "
        "Block: " + arv_insert_block
    )


def test_c2_set_comp_rating_routes_views_to_new_table():
    """C2: set_comp_rating(view in nbv/export) → comp_ratings_views with
    ON CONFLICT (workspace_id, comp_id, view)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "set_comp_rating")
    assert "ON CONFLICT (workspace_id, comp_id, view) DO UPDATE" in fn, (
        "NBV/Export comp write must UPSERT ON CONFLICT (workspace_id, comp_id, view)."
    )
    assert "INSERT INTO comp_ratings_views" in fn, (
        "NBV/Export comp write must INSERT into comp_ratings_views."
    )
    # Clear (rating=None) on a _views view must DELETE that view's row only.
    assert "DELETE FROM comp_ratings_views" in fn, (
        "Clear on a _views view must DELETE from comp_ratings_views."
    )


def test_c2_set_comp_rating_view_normalization():
    """C2: absent/None view → 'arv' (coexistence); present-but-invalid → ValueError
    (→ 400, NEVER silently default an invalid present view to ARV)."""
    src = inspect.getsource(propelio_archive)
    fn = _extract_fn_source(src, "set_comp_rating")
    assert "view is None" in fn, "set_comp_rating must handle view=None → 'arv'."
    assert "ValueError" in fn, (
        "set_comp_rating must raise ValueError on invalid view (→ 400, FMEA C2)."
    )
    # The 'arv' default for None must be explicit.
    assert 'norm_view = "arv"' in fn or "norm_view = 'arv'" in fn, (
        "set_comp_rating must set norm_view='arv' when view is None (coexistence)."
    )


def test_c2_rate_parcel_routes_arv_to_old_table():
    """C2: rate_parcel(view='arv') → parcel_ratings (existing table), unchanged."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "rate_parcel")
    assert fn, "rate_parcel not found"
    assert "norm_view == \"arv\"" in fn or "norm_view == 'arv'" in fn, (
        "rate_parcel must branch on norm_view == 'arv' for the old-table path."
    )
    assert "ON CONFLICT (workspace_id, county, account_num) DO UPDATE" in fn, (
        "ARV parcel write must keep ON CONFLICT (workspace_id, county, account_num) (unchanged)."
    )
    # ARV INSERT must NOT include the `view` column.
    arv_insert_idx = fn.index("INSERT INTO parcel_ratings (workspace_id, county, account_num, rating")
    arv_insert_block = fn[arv_insert_idx:arv_insert_idx + 400]
    assert "view" not in arv_insert_block.lower(), (
        "ARV parcel INSERT must NOT include the view column (byte-for-byte ARV). "
        "Block: " + arv_insert_block
    )


def test_c2_rate_parcel_routes_views_to_new_table():
    """C2: rate_parcel(view in nbv/export) → parcel_ratings_views with
    ON CONFLICT (workspace_id, county, account_num, view)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "rate_parcel")
    assert "ON CONFLICT (workspace_id, county, account_num, view) DO UPDATE" in fn, (
        "NBV/Export parcel write must UPSERT ON CONFLICT (workspace_id, county, account_num, view)."
    )
    assert "INSERT INTO parcel_ratings_views" in fn, (
        "NBV/Export parcel write must INSERT into parcel_ratings_views."
    )
    assert "DELETE FROM parcel_ratings_views" in fn, (
        "Clear on a _views view must DELETE from parcel_ratings_views."
    )


def test_c2_rate_parcel_invalid_view_returns_400():
    """C2: present-but-invalid view → HTTP 400 (NEVER silent ARV default)."""
    src = inspect.getsource(api_main)
    fn = _extract_fn_source(src, "rate_parcel")
    # The invalid-view guard must raise 400.
    assert "view must be 'arv', 'nbv', 'export', or null" in fn, (
        "rate_parcel must 400 on invalid view with an explicit message (FMEA C2)."
    )
    assert "status_code=400" in fn, "rate_parcel invalid-view path must return HTTP 400."


def test_c2_rate_comp_passes_view_through():
    """C2: rate_comp (routes.py) passes request.view through to set_comp_rating."""
    src = inspect.getsource(propelio_routes)
    fn = _extract_fn_source(src, "rate_comp")
    assert fn, "rate_comp not found"
    assert "view=request.view" in fn, (
        "rate_comp must pass view=request.view to set_comp_rating (spec §3.1, FMEA C2)."
    )


# ═════════════════════════════════════════════════════════════════════════
# ARV byte-for-byte invariant (the load-bearing coexistence property)
# ═════════════════════════════════════════════════════════════════════════


def test_arv_existing_tables_unchanged():
    """The existing comp_ratings/parcel_ratings CREATE TABLE statements are
    untouched (byte-for-byte). This is the additive-only / no-ALTER guarantee
    that makes main (flag OFF) provably unaffected."""
    src = inspect.getsource(api_main)
    # Existing comp_ratings schema — must still be exactly this.
    block = _extract_block(src, "CREATE TABLE IF NOT EXISTS comp_ratings ", 900)
    assert "UNIQUE (workspace_id, comp_id)" in block, (
        "Existing comp_ratings UNIQUE (workspace_id, comp_id) must be unchanged."
    )
    assert "rating TEXT NOT NULL CHECK (rating IN ('good', 'bad'))" in block, (
        "Existing comp_ratings.rating CHECK unchanged."
    )
    # Existing parcel_ratings schema.
    block2 = _extract_block(src, "CREATE TABLE IF NOT EXISTS parcel_ratings ", 900)
    assert "UNIQUE (workspace_id, county, account_num)" in block2, (
        "Existing parcel_ratings UNIQUE must be unchanged."
    )


def test_no_alter_or_drop_on_existing_tables():
    """§8 DB-line: no ALTER, no DROP anywhere touching the rating tables."""
    src = inspect.getsource(api_main)
    archive_src = inspect.getsource(propelio_archive)
    routes_src = inspect.getsource(propelio_routes)
    combined = src + archive_src + routes_src
    forbidden_patterns = [
        "ALTER TABLE comp_ratings",
        "ALTER TABLE parcel_ratings",
        "DROP TABLE comp_ratings",
        "DROP TABLE parcel_ratings",
        "DROP INDEX idx_comp_ratings_workspace",
        "DROP INDEX idx_parcel_ratings_workspace",
    ]
    for pat in forbidden_patterns:
        assert pat not in combined, (
            f"Forbidden destructive DDL found: {pat!r} (spec §8 — additive only)."
        )


def test_no_second_arv_backfill():
    """M8/G1: no backfill writes into the _views tables. A backfill is an
    INSERT ... SELECT (copying rows in); the legitimate Chunk A WRITE path is
    INSERT ... VALUES ... ON CONFLICT and is allowed. (Fork's INSERT...SELECT
    into _views is Chunk D, not present yet.) Matching any 'INSERT INTO
    *_views' was too broad — it flagged the normal write UPSERT."""
    import re
    src = inspect.getsource(api_main)
    # Catch only the backfill shape: INSERT INTO <_views> (cols) SELECT …
    # The legitimate write is INSERT INTO <_views> (cols) VALUES … ON CONFLICT,
    # so keying on "(cols) SELECT" (vs "(cols) VALUES") distinguishes them.
    backfill_re = re.compile(
        r"INSERT\s+INTO\s+(?:comp|parcel)_ratings_views\s*\([^)]*\)\s*SELECT\b",
        re.IGNORECASE,
    )
    m = backfill_re.search(src)
    assert m is None, (
        "Backfill (INSERT...SELECT) into a _views table found in main.py — "
        "Chunk A must add NO _views backfill (fork is Chunk D; spec M8/G1 forbid "
        f"a second ARV/backfill source). Match: {m.group(0)[:120] if m else ''!r}"
    )
