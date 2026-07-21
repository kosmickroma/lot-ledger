"""Tests for scripts/ingest_school_zones.py.
See docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §3, §3a, §8.

Two kinds of coverage:
  - Pure-function tests (validate_rows, levels_in, run_guards) against a
    minimal fake cursor/connection -- these guards are plain Python logic,
    fully exercisable without a live DB.
  - Source-inspection guards (blast radius, §8) matching the repo's
    established convention (tests/test_flood_zones_loader.py) for the parts
    that genuinely need a live Postgres (the actual scoped-delete
    transaction, kill -9 mid-insert, CONCURRENTLY index build) -- those are
    exercised in the mandatory manual rehearsal (§9) against a throwaway
    PostGIS instance, not here.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from scripts.ingest_school_zones import (
    IngestAbort,
    levels_in,
    normalize_geom_to_wgs84,
    run_guards,
    validate_rows,
)

SRC_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ingest_school_zones.py"


def _read() -> str:
    return SRC_PATH.read_text()


def _row(level="elementary", campus_name="X Elementary", district_tea_id="999999", geom=None):
    return {
        "level": level, "district_tea_id": district_tea_id, "district_name": "IRVING ISD",
        "campus_tea_id": None, "campus_name": campus_name,
        "geom": geom or {"type": "Polygon", "coordinates": [[[-96.9, 32.8], [-96.9, 32.81], [-96.89, 32.81], [-96.89, 32.8], [-96.9, 32.8]]]},
        "boundary_vintage": "2025-26", "source_url": None, "source_kind": "arcgis", "retrieved_at": date(2026, 7, 21),
    }


# --- validate_rows -----------------------------------------------------------

def test_validate_rows_drops_missing_level() -> None:
    bad = _row(); bad["level"] = "elem"  # typo, not one of the 3
    assert validate_rows([bad]) == []


def test_validate_rows_drops_missing_name() -> None:
    bad = _row(campus_name="")
    assert validate_rows([bad]) == []


def test_validate_rows_drops_invalid_geometry() -> None:
    bad = _row(geom={"type": "Point", "coordinates": [-96.9, 32.8]})
    assert validate_rows([bad]) == []


def test_validate_rows_keeps_a_good_row() -> None:
    assert len(validate_rows([_row()])) == 1


def test_validate_rows_reprojects_web_mercator_geometry() -> None:
    mercator_row = _row(geom={
        "type": "Polygon",
        "coordinates": [[[-10781538.0, 3850850.0], [-10781000.0, 3850850.0], [-10781000.0, 3851000.0], [-10781538.0, 3851000.0], [-10781538.0, 3850850.0]]],
    })
    out = validate_rows([mercator_row])
    assert len(out) == 1
    lng, lat = out[0]["geom"]["coordinates"][0][0]
    assert -180 <= lng <= 180 and -90 <= lat <= 90  # degrees now, not meters


def test_normalize_geom_to_wgs84_is_noop_for_already_wgs84() -> None:
    geom = {"type": "Polygon", "coordinates": [[[-96.9, 32.8], [-96.9, 32.81], [-96.89, 32.81], [-96.89, 32.8], [-96.9, 32.8]]]}
    assert normalize_geom_to_wgs84(geom) == geom


# --- levels_in ----------------------------------------------------------------

def test_levels_in_derived_from_rows_not_config() -> None:
    # §3's last over-delete guard: even though a caller might have INTENDED
    # E/M/H, only the levels that actually made it through validation count.
    rows = [_row(level="elementary"), _row(level="elementary")]
    assert levels_in(rows) == ["elementary"]


def test_levels_in_sorted_and_deduped() -> None:
    rows = [_row(level="high"), _row(level="elementary"), _row(level="high")]
    assert levels_in(rows) == ["elementary", "high"]


def test_levels_in_empty_for_no_rows() -> None:
    assert levels_in([]) == []


# --- run_guards (fake cursor, no live DB) -------------------------------------

class _FakeCursor:
    def __init__(self, existing_counts):
        self._existing_counts = existing_counts
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = (sql, params)

    def fetchall(self):
        return list(self._existing_counts.items())


class _FakeConn:
    def __init__(self, existing_counts):
        self._existing_counts = existing_counts

    def cursor(self):
        return _FakeCursor(self._existing_counts)


def test_run_guards_aborts_on_zero_rows() -> None:
    with pytest.raises(IngestAbort):
        run_guards(_FakeConn({}), "999999", [], force=False)


def test_run_guards_passes_with_no_existing_rows() -> None:
    levels = run_guards(_FakeConn({}), "999999", [_row()], force=False)
    assert levels == ["elementary"]


def test_run_guards_refuses_under_50pct_of_existing_without_force() -> None:
    # 10 existing elementary rows, only 3 new ones parsed -- under 50%.
    rows = [_row() for _ in range(3)]
    with pytest.raises(IngestAbort):
        run_guards(_FakeConn({"elementary": 10}), "999999", rows, force=False)


def test_run_guards_force_bypasses_the_50pct_tripwire() -> None:
    rows = [_row() for _ in range(3)]
    levels = run_guards(_FakeConn({"elementary": 10}), "999999", rows, force=True)
    assert levels == ["elementary"]


def test_run_guards_allows_over_50pct_of_existing() -> None:
    rows = [_row() for _ in range(6)]  # 6/10 = 60%, over the 50% floor
    levels = run_guards(_FakeConn({"elementary": 10}), "999999", rows, force=False)
    assert levels == ["elementary"]


def test_run_guards_never_touches_levels_not_in_this_run() -> None:
    # Existing counts include "high" but this run only parsed elementary --
    # levels_in must not pull "high" into the guard/delete scope at all.
    rows = [_row(level="elementary")]
    levels = run_guards(_FakeConn({"elementary": 0, "high": 42}), "999999", rows, force=False)
    assert levels == ["elementary"]


# --- source-inspection: blast radius (§8) ------------------------------------

def test_no_truncate_anywhere() -> None:
    assert "truncate" not in _read().lower()


def test_no_schema_mutating_alter_anywhere() -> None:
    assert not re.search(r"\balter\b", _read(), re.IGNORECASE)


def test_no_sql_reference_to_other_existing_tables() -> None:
    # §8 -- "no reference to any existing table NAME" is about SQL touching
    # another table, not the module filename ingest_flood_zones.py (which
    # legitimately appears in a comment citing the precedent this script's
    # transaction hygiene is modeled on -- containing the substring
    # "flood_zones" without ever querying that table).
    src = _read()
    for other_table in ("parcels", "saved_areas", "propelio_comps", "comp_ratings", "stored_value_entries"):
        assert other_table not in src
    for clause in ("FROM flood_zones", "INTO flood_zones", "UPDATE flood_zones", "TABLE flood_zones"):
        assert clause not in src


def test_uses_data_db_pool_not_sessions_pool() -> None:
    src = _read()
    assert "from api.config import get_conn, release_conn" in src
    assert "get_session_conn" not in src


def test_delete_is_scoped_to_district_and_level() -> None:
    src = _read()
    assert "DELETE FROM school_attendance_zones WHERE district_tea_id = %s AND level = ANY(%s)" in src


def test_ratings_delete_scoped_by_year_only() -> None:
    src = _read()
    assert "DELETE FROM school_campus_ratings WHERE rating_year = %s" in src


def test_scoped_delete_and_insert_share_one_transaction() -> None:
    # ingest_district must issue exactly one conn.commit() -- proving the
    # delete + all insert batches ride in the same transaction (a kill -9
    # mid-batch then rolls back to the pre-run state, never half-loaded).
    src = _read()
    fn_start = src.index("def ingest_district")
    fn_end = src.index("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end]
    assert body.count("conn.commit()") == 1
    assert body.index("DELETE FROM school_attendance_zones") < body.index("conn.commit()")
    assert body.index("execute_values") < body.index("conn.commit()")


def test_force_flag_exists_on_cli() -> None:
    src = _read()
    assert '"--force"' in src
