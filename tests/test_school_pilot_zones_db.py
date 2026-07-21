"""Tests for api/school_pilot/zones_db.py -- the DB-backed runtime query
path. See docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §5, §6.

Pure-function tests for the isd-string parsing / district-status decision
logic (fake cursor, no live DB) plus source-inspection guards (statement
timeout, data-DB pool only, own endpoint -- never folded into a batch
hydration). The actual ST_Contains/GiST query correctness is exercised
against a live PostGIS instance in the mandatory rehearsal (§9.6), not here.
"""
from __future__ import annotations

from pathlib import Path

from api.school_pilot.zones_db import (
    OPEN_ENROLLMENT_DISTRICT_TEA_IDS,
    _district_display_name,
    _district_status,
    _resolve_district_tea_id_from_isd,
)

SRC_PATH = Path(__file__).resolve().parent.parent / "api" / "school_pilot" / "zones_db.py"
ROUTES_PATH = Path(__file__).resolve().parent.parent / "api" / "school_pilot" / "routes.py"


def _read(p: Path) -> str:
    return p.read_text()


# --- _resolve_district_tea_id_from_isd (uses the real registry crosswalk) ---

def test_resolve_district_from_dcad_isd_string() -> None:
    assert _resolve_district_tea_id_from_isd("dcad:DALLAS ISD") == "057905"


def test_resolve_district_returns_none_for_tarrant_numeric_code() -> None:
    # §6/§11 -- Tarrant/Collin numeric codes have no name map yet (deferred).
    assert _resolve_district_tea_id_from_isd("tad:905") is None


def test_resolve_district_returns_none_for_missing_isd() -> None:
    assert _resolve_district_tea_id_from_isd(None) is None
    assert _resolve_district_tea_id_from_isd("") is None
    assert _resolve_district_tea_id_from_isd("garbage-no-colon") is None


def test_resolve_district_returns_none_for_unknown_dallas_name() -> None:
    assert _resolve_district_tea_id_from_isd("dcad:NOT A REAL ISD") is None


# --- _district_display_name ---------------------------------------------------

def test_district_display_name_from_dcad_isd() -> None:
    assert _district_display_name("dcad:GARLAND ISD") == "GARLAND ISD"


def test_district_display_name_none_for_tarrant_code() -> None:
    assert _district_display_name("tad:905") is None


def test_district_display_name_none_for_missing_isd() -> None:
    assert _district_display_name(None) is None


# --- _district_status (fake cursor) ------------------------------------------

class _FakeCursor:
    def __init__(self, has_rows: bool):
        self._has_rows = has_rows

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (1,) if self._has_rows else None


def test_district_status_ingested_when_any_level_resolved() -> None:
    assert _district_status(_FakeCursor(has_rows=False), "999999", any_level_resolved=True) == "ingested"


def test_district_status_open_enrollment_for_garland() -> None:
    garland = next(iter(OPEN_ENROLLMENT_DISTRICT_TEA_IDS))
    assert _district_status(_FakeCursor(has_rows=False), garland, any_level_resolved=False) == "open_enrollment"


def test_district_status_ingested_when_district_has_rows_but_point_missed_every_level() -> None:
    # §6 case 4 generalized to all 3 levels missing at once: still "ingested"
    # at the district level -- never a district-wide "not loaded yet" lie.
    assert _district_status(_FakeCursor(has_rows=True), "057905", any_level_resolved=False) == "ingested"


def test_district_status_not_loaded_when_district_unknown_and_no_rows() -> None:
    assert _district_status(_FakeCursor(has_rows=False), "999999", any_level_resolved=False) == "not_loaded"


def test_district_status_not_loaded_when_district_tea_id_is_none() -> None:
    assert _district_status(_FakeCursor(has_rows=False), None, any_level_resolved=False) == "not_loaded"


# --- source-inspection: §5, §8 ------------------------------------------------

def test_bounds_the_select_with_statement_timeout() -> None:
    src = _read(SRC_PATH)
    assert "SET LOCAL statement_timeout" in src


def test_uses_distinct_on_and_st_area_tiebreak() -> None:
    src = _read(SRC_PATH)
    assert "DISTINCT ON (z.level)" in src
    assert "ST_Area(z.geom)" in src


def test_uses_data_db_pool_with_finally() -> None:
    src = _read(SRC_PATH)
    assert "from api.config import get_conn, release_conn" in src
    assert "get_session_conn" not in src
    fn_start = src.index("def assign_db")
    body = src[fn_start:]
    assert "finally:" in body
    assert "release_conn(conn)" in body


def test_no_write_statements_in_runtime_path() -> None:
    src = _read(SRC_PATH).upper()
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "TRUNCATE", "ALTER "):
        assert verb not in src


def test_error_path_returns_null_district_status_not_a_business_state() -> None:
    src = _read(SRC_PATH)
    assert '"district_status": None' in src


def test_school_lookup_stays_its_own_endpoint() -> None:
    # §5 -- never folded into /api/parcel or a _hydrate_*_for_rows batch.
    src = _read(ROUTES_PATH)
    assert '@router.get("/api/school-pilot/assign")' in src
    assert "_hydrate_" not in src


def test_routes_toggle_reads_school_source_env_directly() -> None:
    src = _read(ROUTES_PATH)
    assert 'os.getenv("SCHOOL_SOURCE", "static")' in src
