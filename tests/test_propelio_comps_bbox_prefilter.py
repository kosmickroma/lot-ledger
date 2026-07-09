"""Envelope superset test for the load_comps_by_polygon bbox prefilter
(docs/PROPELIO_COMPS_BBOX_PREFILTER_SPEC_2026-07-09.md).

Pure-Python, no DB: proves the understated-constant envelope computed by
load_comps_by_polygon's prefilter is always >= an envelope built from
accurate meters-per-degree constants, for a handful of polygons/radii. If
this holds, AND-ing `pc.geom && envelope` can never drop a qualifying row
(the geometric argument in the spec's "Why it works" section).
"""
from __future__ import annotations

import math

from api.geo import polygon_bbox

# Mirrors the constant used in load_comps_by_polygon's prefilter.
UNDERSTATED_M_PER_DEG = 110000.0
# Accurate constants (spec §Why it works): true is >=110574 lat, <=111320*cos(phi) lng.
ACCURATE_LAT_M_PER_DEG = 110574.0
ACCURATE_LNG_M_PER_DEG = 111320.0


def _envelope(polygon_latlngs, centroid_lat, centroid_lng, circumradius_meters, lat_m_per_deg, lng_m_per_deg_base):
    p_min_lat, p_min_lng, p_max_lat, p_max_lng = polygon_bbox(polygon_latlngs)
    dlat = circumradius_meters / lat_m_per_deg
    max_abs_lat = min(
        max(abs(p_min_lat), abs(p_max_lat), abs(centroid_lat) + dlat),
        89.0,
    )
    dlng = circumradius_meters / (lng_m_per_deg_base * math.cos(math.radians(max_abs_lat)))
    env_min_lat = max(min(p_min_lat, centroid_lat - dlat), -90.0)
    env_max_lat = min(max(p_max_lat, centroid_lat + dlat), 90.0)
    env_min_lng = max(min(p_min_lng, centroid_lng - dlng), -180.0)
    env_max_lng = min(max(p_max_lng, centroid_lng + dlng), 180.0)
    return env_min_lat, env_min_lng, env_max_lat, env_max_lng


def _understated_envelope(polygon_latlngs, centroid_lat, centroid_lng, circumradius_meters):
    return _envelope(
        polygon_latlngs, centroid_lat, centroid_lng, circumradius_meters,
        UNDERSTATED_M_PER_DEG, UNDERSTATED_M_PER_DEG,
    )


def _accurate_envelope(polygon_latlngs, centroid_lat, centroid_lng, circumradius_meters):
    return _envelope(
        polygon_latlngs, centroid_lat, centroid_lng, circumradius_meters,
        ACCURATE_LAT_M_PER_DEG, ACCURATE_LNG_M_PER_DEG,
    )


def _assert_superset(understated, accurate):
    u_min_lat, u_min_lng, u_max_lat, u_max_lng = understated
    a_min_lat, a_min_lng, a_max_lat, a_max_lng = accurate
    assert u_min_lat <= a_min_lat
    assert u_min_lng <= a_min_lng
    assert u_max_lat >= a_max_lat
    assert u_max_lng >= a_max_lng


# Dallas-area small polygon (~2mi floor radius applies).
_DFW_TIGHT = [
    [-96.8000, 32.9000],
    [-96.7990, 32.9000],
    [-96.7990, 32.9010],
    [-96.8000, 32.9010],
]

# Denton-area polygon further north (higher |lat|).
_DENTON_MEDIUM = [
    [-97.1000, 33.2100],
    [-97.0800, 33.2100],
    [-97.0800, 33.2300],
    [-97.1000, 33.2300],
]

# A wider polygon forcing the 10mi radius cap.
_COLLIN_WIDE = [
    [-96.7000, 33.1500],
    [-96.5000, 33.1500],
    [-96.5000, 33.3500],
    [-96.7000, 33.3500],
]


def test_understated_envelope_is_superset_for_tight_dfw_polygon():
    circumradius_meters = 2.0 * 1609.344  # 2mi floor
    centroid_lat, centroid_lng = 32.9005, -96.7995
    understated = _understated_envelope(_DFW_TIGHT, centroid_lat, centroid_lng, circumradius_meters)
    accurate = _accurate_envelope(_DFW_TIGHT, centroid_lat, centroid_lng, circumradius_meters)
    _assert_superset(understated, accurate)


def test_understated_envelope_is_superset_for_denton_medium_polygon():
    circumradius_meters = 3.5 * 1609.344
    centroid_lat, centroid_lng = 33.2200, -97.0900
    understated = _understated_envelope(_DENTON_MEDIUM, centroid_lat, centroid_lng, circumradius_meters)
    accurate = _accurate_envelope(_DENTON_MEDIUM, centroid_lat, centroid_lng, circumradius_meters)
    _assert_superset(understated, accurate)


def test_understated_envelope_is_superset_at_10mi_radius_cap():
    circumradius_meters = 10.0 * 1609.344  # radius cap
    centroid_lat, centroid_lng = 33.2500, -96.6000
    understated = _understated_envelope(_COLLIN_WIDE, centroid_lat, centroid_lng, circumradius_meters)
    accurate = _accurate_envelope(_COLLIN_WIDE, centroid_lat, centroid_lng, circumradius_meters)
    _assert_superset(understated, accurate)


def test_understated_envelope_is_superset_across_radius_sweep():
    centroid_lat, centroid_lng = 33.0000, -96.9000
    for radius_mi in (2.0, 2.5, 4.0, 6.3, 10.0):
        circumradius_meters = radius_mi * 1609.344
        understated = _understated_envelope(_DENTON_MEDIUM, centroid_lat, centroid_lng, circumradius_meters)
        accurate = _accurate_envelope(_DENTON_MEDIUM, centroid_lat, centroid_lng, circumradius_meters)
        _assert_superset(understated, accurate)
