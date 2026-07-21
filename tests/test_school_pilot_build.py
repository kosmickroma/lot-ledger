"""Build-script pure-function tests for the Dallas ISD school-ratings pilot.
See docs/AI/SCHOOL_RATINGS_PILOT_PLAN_2026-07-20.md Task 1.

No network, no DB — reprojection, geometry normalization, and the campus
crosswalk are all pure functions tested in isolation.
"""
from __future__ import annotations

import pyproj
import pytest

from scripts.build_school_pilot_data import (
    EXPECTED_TEA_HEADER,
    TeaSchemaDriftError,
    assert_tea_header_matches,
    derive_tea_campus_id,
    is_already_wgs84,
    match_campus_to_tea,
    normalize_geometry,
    reproject_ring,
)


def test_reproject_ring_3857_to_4326_degrees() -> None:
    # A Web Mercator point near Dallas (~ -96.8, 32.8) in meters
    t = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    ring = [[-10775000.0, 3866000.0]]
    out = reproject_ring(ring, t)
    lng, lat = out[0]
    assert -97.5 < lng < -96.0 and 32.0 < lat < 33.5  # degrees, not meters


def test_reproject_ring_identity_transform_is_a_noop() -> None:
    identity = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:4326", always_xy=True)
    ring = [[-96.8, 32.8]]
    out = reproject_ring(ring, identity)
    lng, lat = out[0]
    assert lng == -96.8 and lat == 32.8


def test_normalize_geometry_multipolygon_keeps_parts_and_holes() -> None:
    identity = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:4326", always_xy=True)
    geom = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [0, 2], [2, 2], [2, 0], [0, 0]], [[0.5, 0.5], [0.5, 1], [1, 1], [1, 0.5], [0.5, 0.5]]],
        [[[5, 5], [5, 6], [6, 6], [6, 5], [5, 5]]]]}
    parts = normalize_geometry(geom, identity)
    assert len(parts) == 2            # two polygons
    assert len(parts[0]) == 2         # outer + one hole
    assert len(parts[1]) == 1         # outer only


def test_normalize_geometry_plain_polygon_single_part() -> None:
    identity = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:4326", always_xy=True)
    geom = {"type": "Polygon", "coordinates": [[[0, 0], [0, 2], [2, 2], [2, 0], [0, 0]]]}
    parts = normalize_geometry(geom, identity)
    assert len(parts) == 1
    assert len(parts[0]) == 1


def test_is_already_wgs84_true_for_degree_range_coords() -> None:
    # docs/AI/SCHOOL_PILOT_DISD_FINDINGS_2026-07-20.md §2 -- ArcGIS's f=geojson
    # output is already WGS84 regardless of the layer's declared storage CRS;
    # verify by magnitude rather than trust the declared spatialReference blindly.
    geom = {"type": "Polygon", "coordinates": [[[-96.8, 32.8], [-96.8, 32.9], [-96.7, 32.9], [-96.7, 32.8], [-96.8, 32.8]]]}
    assert is_already_wgs84(geom) is True


def test_is_already_wgs84_false_for_web_mercator_meters() -> None:
    geom = {"type": "Polygon", "coordinates": [[[-10781538.0, 3850850.0], [-10781000.0, 3850850.0], [-10781000.0, 3851000.0], [-10781538.0, 3851000.0], [-10781538.0, 3850850.0]]]}
    assert is_already_wgs84(geom) is False


def test_match_campus_prefers_number_then_name() -> None:
    idx = {"057905101": "057905101", "BOWIE": "057905101"}
    assert match_campus_to_tea("Bowie Elementary", "057905101", idx) == "057905101"
    assert match_campus_to_tea("Bowie Elementary", None, idx) == "057905101"
    assert match_campus_to_tea("Nonexistent", None, idx) is None


def test_match_campus_token_overlap_handles_verbose_tea_names() -> None:
    # docs/AI/SCHOOL_PILOT_DISD_FINDINGS_2026-07-20.md §3 -- DISD's own zone
    # names are short ("Bowie"); TEA's official names are verbose ("JAMES
    # BOWIE EL"). A plain normalized-exact-match index (as a naive crosswalk
    # would build) never contains "BOWIE" as a key, only "JAMES BOWIE EL" --
    # so the index here is keyed on the REAL TEA name, and match_campus_to_tea
    # must still resolve "Bowie Elementary" against it via token overlap.
    idx = {"057905101": "057905101", "JAMES BOWIE EL": "057905101"}
    assert match_campus_to_tea("Bowie Elementary", None, idx) == "057905101"


def test_derive_tea_campus_id_zero_pads_and_prefixes_district() -> None:
    # docs/AI/SCHOOL_PILOT_DISD_FINDINGS_2026-07-20.md §3 UPDATE -- DISD's SLN
    # zero-padded to 3 digits and prefixed with the district number IS the
    # TEA CAMPUS_ID (verified against all 187 real zones, 100% agreement with
    # independent name-token-overlap matching).
    assert derive_tea_campus_id(235) == "057905235"
    assert derive_tea_campus_id(1) == "057905001"


def test_derive_tea_campus_id_none_for_non_int() -> None:
    assert derive_tea_campus_id(None) is None
    assert derive_tea_campus_id("not-a-number") is None


def test_match_campus_does_not_conflate_adams_and_adamson() -> None:
    # The exact collision Task 0 checked for by hand: "Adams" (Bryan Adams HS)
    # must never match "W H ADAMSON H S" -- different whole tokens after
    # stopword-stripping, even though "ADAMS" is a character-substring of
    # "ADAMSON".
    idx = {
        "057905001": "057905001",
        "BRYAN ADAMS H S LEADERSHIP ACADEMY": "057905001",
        "057905002": "057905002",
        "W H ADAMSON H S": "057905002",
    }
    assert match_campus_to_tea("Adams", None, idx) == "057905001"


# --- Safeguard 1 (docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md
# "recurring-cadence safeguards") -- TEA schema-drift assert ------------------

def _valid_header(overrides: dict[int, str] | None = None) -> tuple:
    # Cells wrap across lines in the real file (e.g. "District\nNumber") --
    # verified against the live 2025-26 XLSX's actual header row.
    row = [None] * 25
    row[0] = "District\nNumber"
    row[2] = "Campus\nNumber"
    row[6] = "School\nType"
    row[13] = "Overall\nRating"
    row[14] = "Overall\nScore"
    row[15] = "Student\nAchievement\nRating"
    row[16] = "Student\nAchievement\nScore"
    row[19] = "Academic\nGrowth\nRating"
    row[20] = "Academic\nGrowth\nScore"
    for idx, val in (overrides or {}).items():
        row[idx] = val
    return tuple(row)


def test_assert_tea_header_matches_passes_on_the_real_layout() -> None:
    assert_tea_header_matches(_valid_header())  # must not raise


def test_assert_tea_header_matches_refuses_on_swapped_columns() -> None:
    # The exact scenario this safeguard exists for: TEA swaps two adjacent
    # columns (Overall Rating <-> Overall Score) in a future year's file.
    reshuffled = _valid_header({13: "Overall\nScore", 14: "Overall\nRating"})
    with pytest.raises(TeaSchemaDriftError, match="index 13"):
        assert_tea_header_matches(reshuffled)


def test_assert_tea_header_matches_refuses_on_a_renamed_column() -> None:
    reshuffled = _valid_header({6: "Campus Category"})
    with pytest.raises(TeaSchemaDriftError, match="index 6"):
        assert_tea_header_matches(reshuffled)


def test_assert_tea_header_matches_error_names_both_expected_and_actual() -> None:
    reshuffled = _valid_header({14: "Something Else"})
    with pytest.raises(TeaSchemaDriftError, match=r"expected 'Overall Score', got 'Something Else'"):
        assert_tea_header_matches(reshuffled)


def test_assert_tea_header_matches_refuses_on_missing_trailing_column() -> None:
    # A shorter header row (fewer columns than expected) must refuse, not
    # index out of range or silently skip the check.
    short_row = _valid_header()[:18]
    with pytest.raises(TeaSchemaDriftError, match="index 19"):
        assert_tea_header_matches(short_row)


def test_assert_tea_header_matches_is_whitespace_insensitive() -> None:
    # A different wrap point for the SAME words is not a real drift.
    reworded = _valid_header({0: "District Number", 6: "  School   Type  "})
    assert_tea_header_matches(reworded)  # must not raise


def test_expected_tea_header_covers_every_index_load_tea_ratings_reads() -> None:
    assert set(EXPECTED_TEA_HEADER.keys()) == {0, 2, 6, 13, 14, 15, 16, 19, 20}


def test_load_tea_ratings_checks_header_before_reading_any_data_row() -> None:
    # Source-level guarantee: the assert call must precede the data-row
    # loop inside load_tea_ratings(), not follow it.
    import inspect

    from scripts.build_school_pilot_data import load_tea_ratings
    src = inspect.getsource(load_tea_ratings)
    assert src.index("assert_tea_header_matches(") < src.index("for row in ws.iter_rows(min_row=2")
