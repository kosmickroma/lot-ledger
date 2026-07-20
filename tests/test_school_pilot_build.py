"""Build-script pure-function tests for the Dallas ISD school-ratings pilot.
See docs/AI/SCHOOL_RATINGS_PILOT_PLAN_2026-07-20.md Task 1.

No network, no DB — reprojection, geometry normalization, and the campus
crosswalk are all pure functions tested in isolation.
"""
from __future__ import annotations

import pyproj

from scripts.build_school_pilot_data import (
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
