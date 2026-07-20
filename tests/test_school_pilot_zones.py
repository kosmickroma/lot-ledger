"""In-memory zone loader + point-in-polygon assignment tests for the school
pilot. See docs/AI/SCHOOL_RATINGS_PILOT_PLAN_2026-07-20.md Task 2.

No network, no DB. MultiPolygon/hole PIP is tested against synthetic
fixtures (Task 0 confirmed today's real DISD data has zero holes, so this
is the only way to actually exercise that path).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.school_pilot.zones import assign, load_zones, point_in_parts

SQUARE_WITH_HOLE = [[  # one polygon: outer + hole
    [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]],
    [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]],
]]


def test_point_inside_outer_not_in_hole() -> None:
    assert point_in_parts(2, 2, SQUARE_WITH_HOLE) is True


def test_point_in_hole_is_outside() -> None:
    assert point_in_parts(5, 5, SQUARE_WITH_HOLE) is False


def test_point_outside_all() -> None:
    assert point_in_parts(20, 20, SQUARE_WITH_HOLE) is False


def test_point_in_parts_checks_multiple_polygon_parts() -> None:
    # A MultiPolygon: two disjoint squares. A point in the second part must
    # still resolve True (not just the first part tested).
    two_parts = [
        [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
        [[[20, 20], [20, 21], [21, 21], [21, 20], [20, 20]]],
    ]
    assert point_in_parts(20.5, 20.5, two_parts) is True
    assert point_in_parts(0.5, 0.5, two_parts) is True
    assert point_in_parts(50, 50, two_parts) is False


# --- load_zones() / assign() against a tiny fixture -------------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "school_pilot"


@pytest.fixture()
def fixture_zones(monkeypatch):
    """Point zones.py's data directory at tests/fixtures/school_pilot and
    clear the lru_cache singleton so each test starts fresh."""
    monkeypatch.setattr("api.school_pilot.zones._DATA", FIXTURES)
    load_zones.cache_clear()
    yield
    load_zones.cache_clear()


def test_assign_returns_matched_campus_and_grade_inside_zone(fixture_zones) -> None:
    # Fixture squares are centered around (lat=32.8, lng=-96.8), each 0.01
    # degrees wide -- see tests/fixtures/school_pilot/*.json.
    result = assign(32.805, -96.805)
    assert result["elementary"] == {
        "name": "Fixture Elementary", "rating": "B",
        "rating_year": 2025, "boundary_vintage": "2025-26",
    }
    assert result["middle"]["name"] == "Fixture Middle"
    assert result["high"]["name"] == "Fixture High"


def test_assign_returns_none_outside_all_zones(fixture_zones) -> None:
    result = assign(40.0, -100.0)
    assert result == {"elementary": None, "middle": None, "high": None}


def test_load_zones_tolerates_missing_files(monkeypatch) -> None:
    # §5.1 -- a missing data file must never raise; assign() degrades to
    # all-nulls, never a broken app.
    monkeypatch.setattr("api.school_pilot.zones._DATA", FIXTURES / "does-not-exist")
    load_zones.cache_clear()
    try:
        result = assign(32.8, -96.8)
    finally:
        load_zones.cache_clear()
    assert result == {"elementary": None, "middle": None, "high": None}
