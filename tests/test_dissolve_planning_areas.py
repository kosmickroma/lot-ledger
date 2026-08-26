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


# --- --exclude-value (docs/AI/SPEC_COPPELL_ZONES_INGEST_2026-08-26.md, gate
# G-LAKE: "zero rows named LAKE reach any DB") -------------------------------

def test_excluded_value_produces_no_output_feature() -> None:
    doc = {
        "type": "FeatureCollection",
        "features": [
            _feature("A", _square(0, 0, 1, 1)),
            _feature("A", _square(1, 0, 2, 1)),
            _feature("LAKE", _square(5, 5, 6, 6)),
        ],
    }
    out = dissolve_by_field(doc, "NAME", {"LAKE"})
    names = [f["properties"]["NAME"] for f in out["features"]]
    assert names == ["A"]
    assert out["properties"]["excluded_values"] == {"LAKE": 1}
    assert out["properties"]["excluded_feature_count"] == 1
    assert out["properties"]["input_feature_count"] == 3
    assert out["properties"]["output_feature_count"] == 1


def test_excluded_area_is_not_counted_as_lost_area() -> None:
    # The excluded square is 1x1; the kept group is 2x1. Area accounting must
    # compare kept-in vs kept-out (2.0 == 2.0), not 3.0 vs 2.0 -- otherwise
    # every exclusion would trip the conservation check.
    doc = {
        "type": "FeatureCollection",
        "features": [
            _feature("A", _square(0, 0, 1, 1)),
            _feature("A", _square(1, 0, 2, 1)),
            _feature("LAKE", _square(5, 5, 6, 6)),
        ],
    }
    out = dissolve_by_field(doc, "NAME", {"LAKE"})
    assert out["properties"]["input_area"] == pytest.approx(2.0)
    assert out["properties"]["output_area"] == pytest.approx(2.0)


def test_exclusion_that_matches_nothing_raises() -> None:
    # A drop filter that silently matches nothing is the failure G-LAKE exists
    # to catch: a renamed placeholder would otherwise be ingested as a campus.
    doc = {
        "type": "FeatureCollection",
        "features": [_feature("A", _square(0, 0, 1, 1))],
    }
    with pytest.raises(ValueError, match="matched no feature"):
        dissolve_by_field(doc, "NAME", {"LAKE"})


def test_no_exclusions_is_unchanged_behaviour() -> None:
    doc = {
        "type": "FeatureCollection",
        "features": [
            _feature("A", _square(0, 0, 1, 1)),
            _feature("B", _square(5, 5, 6, 6)),
        ],
    }
    out = dissolve_by_field(doc, "NAME")
    assert [f["properties"]["NAME"] for f in out["features"]] == ["A", "B"]
    assert out["properties"]["excluded_values"] == {}
    assert out["properties"]["excluded_feature_count"] == 0


# --- --make-valid (Argyle GIBSON: one ring self-intersection) ---------------

def _bowtie() -> dict:
    # A self-intersecting "bowtie" ring: invalid, but make_valid recovers two
    # triangles whose combined area equals what the ring encloses.
    return {
        "type": "Polygon",
        "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]],
    }


def test_invalid_geometry_raises_by_default() -> None:
    doc = {"type": "FeatureCollection", "features": [_feature("A", _bowtie())]}
    with pytest.raises(ValueError, match="invalid geometry"):
        dissolve_by_field(doc, "NAME")


def test_invalid_geometry_error_names_the_defect_and_the_flag() -> None:
    doc = {"type": "FeatureCollection", "features": [_feature("A", _bowtie())]}
    with pytest.raises(ValueError, match="Self-intersection"):
        dissolve_by_field(doc, "NAME")
    with pytest.raises(ValueError, match="--make-valid"):
        dissolve_by_field(doc, "NAME")


def test_make_valid_repairs_and_reports_the_repair() -> None:
    doc = {"type": "FeatureCollection", "features": [_feature("A", _bowtie())]}
    out = dissolve_by_field(doc, "NAME", repair_invalid=True)
    assert out["properties"]["output_feature_count"] == 1
    assert out["properties"]["repaired_input_geometries"] == ["A"]
    assert out["features"][0]["properties"]["NAME"] == "A"


def test_valid_input_reports_no_repairs_even_with_the_flag_on() -> None:
    doc = {
        "type": "FeatureCollection",
        "features": [_feature("A", _square(0, 0, 1, 1))],
    }
    out = dissolve_by_field(doc, "NAME", repair_invalid=True)
    assert out["properties"]["repaired_input_geometries"] == []


def test_repair_flag_does_not_change_valid_geometry_output() -> None:
    doc = {
        "type": "FeatureCollection",
        "features": [
            _feature("A", _square(0, 0, 1, 1)),
            _feature("A", _square(1, 0, 2, 1)),
            _feature("B", _square(5, 5, 6, 6)),
        ],
    }
    plain = dissolve_by_field(doc, "NAME")
    repaired = dissolve_by_field(doc, "NAME", repair_invalid=True)
    assert plain["features"] == repaired["features"]
    assert plain["properties"]["output_area"] == repaired["properties"]["output_area"]
