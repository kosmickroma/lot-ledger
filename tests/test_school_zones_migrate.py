"""Source-inspection guards for scripts/migrate_school_zones_schema.py.
See docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §1, §2, §8.

Follows the established repo convention (tests/test_flood_zones_loader.py):
source-text assertions rather than a live DB connection -- this repo has no
existing live-Postgres test fixture (confirmed via full-suite grep before
writing this). The mandatory live rehearsal (§9) is run manually against a
throwaway PostGIS instance, not as a pytest.
"""
from __future__ import annotations

from pathlib import Path

SRC_PATH = Path(__file__).resolve().parent.parent / "scripts" / "migrate_school_zones_schema.py"


def _read() -> str:
    return SRC_PATH.read_text()


def test_creates_both_tables_if_not_exists() -> None:
    src = _read()
    assert "CREATE TABLE IF NOT EXISTS school_attendance_zones" in src
    assert "CREATE TABLE IF NOT EXISTS school_campus_ratings" in src


def test_zones_table_has_level_check_constraint() -> None:
    src = _read()
    assert "CHECK (level IN ('elementary', 'middle', 'high'))" in src


def test_zones_table_geom_column_is_multipolygon_4326_not_null() -> None:
    src = _read()
    assert "GEOMETRY(MultiPolygon, 4326) NOT NULL" in src


def test_ratings_table_pk_is_campus_and_year() -> None:
    src = _read()
    assert "PRIMARY KEY (campus_tea_id, rating_year)" in src


def test_uses_set_local_lock_and_statement_timeout_for_schema() -> None:
    src = _read()
    assert "SET LOCAL lock_timeout = '5s'" in src
    assert "SET LOCAL statement_timeout = '60s'" in src


def test_indexes_created_concurrently() -> None:
    src = _read()
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_school_attendance_zones_geom" in src
    assert "USING GIST (geom)" in src
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_school_attendance_zones_district_level" in src
    assert "(district_tea_id, level)" in src


def test_index_step_flips_autocommit_for_concurrently() -> None:
    src = _read()
    assert "conn.autocommit = True" in src
    assert "conn.autocommit = False" in src


def test_detects_and_drops_invalid_index_before_rebuild() -> None:
    """The flood precedent has NO invalid-index detect/drop/retry (verified
    by reading scripts/ingest_flood_zones.py in full) -- an index left
    INVALID by a failed CONCURRENTLY build would otherwise be silently
    skipped forever by a later `IF NOT EXISTS` run. This is the fix."""
    src = _read()
    assert "indisvalid" in src
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in src


def test_schema_and_indexes_created_in_that_order() -> None:
    src = _read()
    # "create empty tables -> create indexes -> load" (§2) -- the ensure
    # function calls schema before indexes.
    fn_idx = src.index("def ensure_schema_and_indexes")
    body = src[fn_idx:src.index("\n\n\n", fn_idx)]
    assert body.index("ensure_schema_txn(conn)") < body.index("ensure_indexes_concurrent(conn)")


def test_no_truncate_anywhere() -> None:
    # §8 grep-enforced ban -- literal absence, not just "not executed."
    assert "truncate" not in _read().lower()


def test_no_schema_mutating_alter_anywhere() -> None:
    import re
    assert not re.search(r"\balter\b", _read(), re.IGNORECASE)


def test_uses_data_db_pool_not_sessions_pool() -> None:
    src = _read()
    assert "from api.config import get_conn, release_conn" in src
    assert "get_session_conn" not in src
    assert "release_session_conn" not in src
