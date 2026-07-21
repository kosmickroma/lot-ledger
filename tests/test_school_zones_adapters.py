"""Tests for scripts/school_zones_adapters.py -- the 3 source adapters
(ArcGIS REST GeoJSON, Google MyMaps KML, single-campus->district-boundary).
See docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §4.

Pure file-parsing, no network -- every adapter reads only from
tests/fixtures/school_zones/, mirroring how the adapters themselves read
only from an operator-downloaded snapshot directory, never the network.
"""
from __future__ import annotations

from pathlib import Path

from scripts.school_zones_adapters import (
    adapter_arcgis_geojson,
    adapter_district_boundary,
    adapter_mymaps_kml,
    parse_kml_placemarks,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "school_zones"


def test_arcgis_adapter_normalizes_rows() -> None:
    rows = adapter_arcgis_geojson(
        FIXTURES,
        {"elementary": {"file": "irving_elementary.geojson", "name_field": "CAMPUS_NAME", "campus_id_field": "CAMPUS_ID"}},
        district_tea_id="057912", district_name="IRVING ISD", boundary_vintage="2025-26",
    )
    names = {r["campus_name"] for r in rows}
    assert names == {"Bowie Elementary", "Lively Elementary"}  # null-named feature dropped


def test_arcgis_adapter_campus_id_only_when_field_present() -> None:
    rows = adapter_arcgis_geojson(
        FIXTURES,
        {"elementary": {"file": "irving_elementary.geojson", "name_field": "CAMPUS_NAME", "campus_id_field": "CAMPUS_ID"}},
        district_tea_id="057912", district_name="IRVING ISD", boundary_vintage="2025-26",
    )
    by_name = {r["campus_name"]: r for r in rows}
    assert by_name["Bowie Elementary"]["campus_tea_id"] == "057912101"
    assert by_name["Lively Elementary"]["campus_tea_id"] is None  # no CAMPUS_ID on this feature -- unresolved, never guessed


def test_arcgis_adapter_omits_campus_id_field_entirely_when_unverified() -> None:
    # A district whose config never names a campus_id_field at all (no
    # verified join strategy yet) -- every row's campus_tea_id is None.
    rows = adapter_arcgis_geojson(
        FIXTURES,
        {"elementary": {"file": "irving_elementary.geojson", "name_field": "CAMPUS_NAME"}},
        district_tea_id="057912", district_name="IRVING ISD", boundary_vintage="2025-26",
    )
    assert all(r["campus_tea_id"] is None for r in rows)


def test_arcgis_adapter_sets_level_and_source_kind() -> None:
    rows = adapter_arcgis_geojson(
        FIXTURES,
        {"elementary": {"file": "irving_elementary.geojson", "name_field": "CAMPUS_NAME"}},
        district_tea_id="057912", district_name="IRVING ISD", boundary_vintage="2025-26",
    )
    assert all(r["level"] == "elementary" for r in rows)
    assert all(r["source_kind"] == "arcgis" for r in rows)


def test_arcgis_adapter_raises_on_exceeded_transfer_limit(tmp_path) -> None:
    bad = tmp_path / "truncated.geojson"
    bad.write_text('{"exceededTransferLimit": true, "features": []}')
    try:
        adapter_arcgis_geojson(
            tmp_path, {"elementary": {"file": "truncated.geojson", "name_field": "X"}},
            district_tea_id="1", district_name=None, boundary_vintage=None,
        )
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_parse_kml_placemarks_skips_non_polygon() -> None:
    placemarks = parse_kml_placemarks(FIXTURES / "mesquite_elementary.kml")
    names = {p["name"] for p in placemarks}
    assert names == {"Shaw Elementary", "Ligon Elementary"}  # "Just A Label" (a Point) dropped


def test_parse_kml_placemarks_keeps_hole_ring() -> None:
    placemarks = parse_kml_placemarks(FIXTURES / "mesquite_elementary.kml")
    ligon = next(p for p in placemarks if p["name"] == "Ligon Elementary")
    assert len(ligon["geom"]["coordinates"]) == 2  # outer + 1 hole


def test_mymaps_kml_adapter_maps_campus_id_by_name() -> None:
    rows = adapter_mymaps_kml(
        FIXTURES,
        {"elementary": {"file": "mesquite_elementary.kml", "campus_id_map": {"Shaw Elementary": "057914050"}}},
        district_tea_id="057914", district_name="MESQUITE ISD", boundary_vintage="2025-26",
    )
    by_name = {r["campus_name"]: r for r in rows}
    assert by_name["Shaw Elementary"]["campus_tea_id"] == "057914050"
    assert by_name["Ligon Elementary"]["campus_tea_id"] is None  # not in the map -- unresolved, never guessed


def test_district_boundary_adapter_replicates_geometry_per_level() -> None:
    rows = adapter_district_boundary(
        FIXTURES, "sunnyvale_boundary.geojson",
        {
            "elementary": {"campus_name": "Sunnyvale Elementary", "campus_tea_id": "057919001"},
            "middle": {"campus_name": "Sunnyvale Middle"},
            "high": {"campus_name": "Sunnyvale High", "campus_tea_id": "057919001"},
        },
        district_tea_id="057919", district_name="SUNNYVALE ISD", boundary_vintage="2025-26",
    )
    assert {r["level"] for r in rows} == {"elementary", "middle", "high"}
    assert all(r["geom"] == rows[0]["geom"] for r in rows)  # same district polygon at every level
    assert all(r["source_kind"] == "district_boundary" for r in rows)
