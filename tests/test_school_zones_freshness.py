"""Tests for scripts/check_school_zones_freshness.py -- Safeguard 2
(docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md "recurring-cadence
safeguards"). The stale-vintage bell that turns a silent yearly-refresh
treadmill into a visible, scriptable checklist item.

Pure-function tests (no DB) for the school-year math and the stale-vs-fresh
decision, plus source-inspection guards for determinism (no direct
datetime.now()/date.today() call). The actual DB read is exercised in the
mandatory throwaway-DB rehearsal, not here.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from scripts.check_school_zones_freshness import (
    compute_expected_school_year,
    find_stale_districts,
)

SRC_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_school_zones_freshness.py"


def _read() -> str:
    return SRC_PATH.read_text()


# --- compute_expected_school_year ---------------------------------------------

def test_expected_school_year_mid_school_year() -> None:
    # January -- the 2025-26 school year is already in progress.
    assert compute_expected_school_year(date(2026, 1, 15)) == "2025-26"


def test_expected_school_year_just_before_rollover() -> None:
    assert compute_expected_school_year(date(2026, 6, 30)) == "2025-26"


def test_expected_school_year_at_rollover() -> None:
    assert compute_expected_school_year(date(2026, 7, 1)) == "2026-27"


def test_expected_school_year_deep_in_fall_semester() -> None:
    assert compute_expected_school_year(date(2026, 11, 1)) == "2026-27"


def test_expected_school_year_format_matches_boundary_vintage_convention() -> None:
    # scripts/build_school_pilot_data.py's BOUNDARY_VINTAGE == "2025-26"
    result = compute_expected_school_year(date(2026, 3, 1))
    assert re.fullmatch(r"\d{4}-\d{2}", result)


# --- find_stale_districts ------------------------------------------------------

def test_find_stale_districts_clean_when_all_current() -> None:
    vintages = {"057905": {"2026-27"}, "057912": {"2026-27"}}
    assert find_stale_districts(vintages, "2026-27") == []


def test_find_stale_districts_flags_a_stale_one() -> None:
    vintages = {"057905": {"2025-26"}}
    stale = find_stale_districts(vintages, "2026-27", registry={"057905": {"name": "DALLAS ISD"}})
    assert stale == [("057905", "DALLAS ISD", "2025-26")]


def test_find_stale_districts_falls_back_to_id_when_registry_unknown() -> None:
    vintages = {"999999": {"2024-25"}}
    stale = find_stale_districts(vintages, "2026-27", registry={})
    assert stale == [("999999", "999999", "2024-25")]


def test_find_stale_districts_flags_a_mixed_vintage_district() -> None:
    # A half-finished re-ingest (e.g. only some levels re-loaded) leaves a
    # district with MORE than one distinct vintage -- must be flagged even
    # though one of them is technically current.
    vintages = {"057905": {"2025-26", "2026-27"}}
    stale = find_stale_districts(vintages, "2026-27")
    assert len(stale) == 1
    assert stale[0][0] == "057905"


def test_find_stale_districts_treats_null_vintage_as_stale() -> None:
    vintages = {"057905": {None}}
    stale = find_stale_districts(vintages, "2026-27")
    assert stale[0][2] == "(none)"


def test_find_stale_districts_ignores_districts_not_ingested() -> None:
    # A district absent from vintages_by_district entirely (never
    # ingested) is not this check's business -- that's Gap-1-style "not
    # loaded yet," a different signal.
    stale = find_stale_districts({}, "2026-27")
    assert stale == []


# --- source-inspection: determinism + safety ---------------------------------

def test_as_of_is_a_required_cli_argument() -> None:
    src = _read()
    assert '"--as-of", required=True' in src


def test_never_calls_datetime_now_or_date_today_directly() -> None:
    # The module's own header comment discusses datetime.now()/date.today()
    # BY NAME to explain why --as-of exists -- check only the executable
    # code (after the imports), not the prose above it.
    src = _read()
    code = src[src.index("from __future__"):]
    assert "datetime.now(" not in code
    assert "date.today(" not in code


def test_exits_nonzero_when_any_district_is_stale() -> None:
    src = _read()
    assert "return 1" in src


def test_uses_data_db_pool_read_only() -> None:
    src = _read()
    assert "from api.config import get_conn, release_conn" in src
    fn_start = src.index("def ingested_vintages")
    fn_end = src.index("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end].upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE", "ALTER "):
        assert verb not in body
