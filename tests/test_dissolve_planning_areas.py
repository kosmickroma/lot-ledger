"""Unit tests for scripts/dissolve_planning_areas.py.
docs/AI/SPEC_MCKINNEY_ZONES_INGEST_2026-08-26.md deliverable 2.
"""
from __future__ import annotations

import pytest

from scripts.dissolve_planning_areas import dissolve_by_field


def _square(x0: float, y0: float, x1: float, y1: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
    }


def _feature(name: str, geom: dict) -> dict:
    return {"type": "Feature", "properties": {"NAME": name}, "geometry": geom}


def test_dissolves_adjacent_same_name_polygons_into_one() -> None:
    # Two adjacent 1x1 squares sharing an edge, same group -> one 2x1 polygon.
    doc = {
        "type": "FeatureCollection",
        "features": [
            _feature("A", _square(0, 0, 1, 1)),
            _feature("A", _square(1, 0, 2, 1)),
        ],
    }
    out = dissolve_by_field(doc, "NAME")
    assert out["properties"]["output_feature_count"] == 1
    assert out["features"][0]["properties"]["NAME"] == "A"
    assert out["features"][0]["properties"]["source_planning_areas"] == 2


def test_distinct_names_stay_separate_features() -> None:
    doc = {
        "type": "FeatureCollection",
        "features": [
            _feature("A", _square(0, 0, 1, 1)),
            _feature("B", _square(5, 5, 6, 6)),
            _feature("B", _square(6, 5, 7, 6)),
        ],
    }
    out = dissolve_by_field(doc, "NAME")
    assert out["properties"]["output_feature_count"] == 2
    names = {f["properties"]["NAME"] for f in out["features"]}
    assert names == {"A", "B"}


def test_non_adjacent_same_name_polygons_become_multipolygon() -> None:
    doc = {
        "type": "FeatureCollection",
        "features": [
            _feature("A", _square(0, 0, 1, 1)),
            _feature("A", _square(10, 10, 11, 11)),  # far away, not touching
        ],
    }
    out = dissolve_by_field(doc, "NAME")
    assert out["properties"]["output_feature_count"] == 1
    assert out["features"][0]["geometry"]["type"] == "MultiPolygon"


def test_area_is_conserved() -> None:
    doc = {
        "type": "FeatureCollection",
        "features": [
            _feature("A", _square(0, 0, 1, 1)),   # area 1
            _feature("A", _square(1, 0, 2, 1)),   # area 1
            _feature("B", _square(5, 5, 6, 7)),   # area 2
        ],
    }
    out = dissolve_by_field(doc, "NAME")
    assert out["properties"]["input_area"] == pytest.approx(4.0)
    assert out["properties"]["output_area"] == pytest.approx(4.0, rel=1e-6)


def test_missing_group_field_raises() -> None:
    doc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": _square(0, 0, 1, 1)},
        ],
    }
    with pytest.raises(ValueError, match="missing"):
        dissolve_by_field(doc, "NAME")


def test_invalid_geometry_raises() -> None:
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]],
    }
    doc = {"type": "FeatureCollection", "features": [_feature("A", bowtie)]}
    with pytest.raises(ValueError, match="invalid geometry"):
        dissolve_by_field(doc, "NAME")
