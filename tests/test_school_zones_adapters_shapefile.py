"""Tests for scripts/school_zones_adapters.adapter_shapefile -- the
PIA-delivered ESRI shapefile source kind (Azle, Community; batch
docs/AI/SPEC_PIA_BATCH1_ZONES_2026-08-31.md).

Unlike the other three adapters, this one owns its own reprojection, because
a shapefile's CRS is whatever the district's GIS staff had their ArcMap
project set to -- it is NOT knowable in advance the way KML (WGS84 by spec)
or an ArcGIS REST `outSR=4326` response is. These tests therefore centre on
the CRS contract: the file's own .prj decides, a geographic file passes
through untouched, and a file with NO .prj is refused rather than assumed.

Fixtures are written with pyshp at test time (rather than committed binary
.shp/.dbf/.prj triples) so each test's CRS is visible in the test itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import shapefile

from scripts.school_zones_adapters import adapter_shapefile

WEB_MERCATOR_WKT = (
    'PROJCS["WGS_1984_Web_Mercator_Auxiliary_Sphere",GEOGCS["GCS_WGS_1984",'
    'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.017453292519943295]],'
    'PROJECTION["Mercator_Auxiliary_Sphere"],PARAMETER["False_Easting",0.0],'
    'PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",0.0],'
    'PARAMETER["Standard_Parallel_1",0.0],PARAMETER["Auxiliary_Sphere_Type",0.0],'
    'UNIT["Meter",1.0]]'
)
NAD83_GEOGRAPHIC_WKT = (
    'GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",'
    'SPHEROID["GRS_1980",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],'
    'UNIT["Degree",0.0174532925199433]]'
)
# NAD83 / Texas North Central (ftUS) -- a projected CRS that is NOT Web
# Mercator. The whole point of reading the .prj: ingest_school_zones.
# normalize_geom_to_wgs84 would run this through a 3857->4326 transform and
# land the zones in the wrong place without erroring.
TX_NORTH_CENTRAL_EPSG = "EPSG:2276"


def _write_shapefile(directory: Path, stem: str, prj_wkt: str | None, records, fields=(("ZONE_NAME", "C", 50, 0),)):
    path = directory / stem
    with shapefile.Writer(str(path)) as w:
        for name, ftype, size, dec in fields:
            w.field(name, ftype, size, dec)
        for parts, values in records:
            w.poly(parts)
            w.record(*values)
    if prj_wkt is not None:
        (directory / f"{stem}.prj").write_text(prj_wkt)
    return path


def _square(x, y, d):
    """One closed clockwise ring -- pyshp's outer-ring winding."""
    return [[[x, y], [x, y + d], [x + d, y + d], [x + d, y], [x, y]]]


def test_web_mercator_is_reprojected_to_wgs84(tmp_path: Path) -> None:
    # A square around Community ISD's real extent, in Web Mercator metres.
    _write_shapefile(tmp_path, "zones", WEB_MERCATOR_WKT, [(_square(-10740000, 3893000, 1000), ["NeSmith"])])
    rows = adapter_shapefile(
        tmp_path, {"elementary": {"file": "zones.shp", "name_field": "ZONE_NAME"}},
        district_tea_id="043918", district_name="COMMUNITY ISD", boundary_vintage="2024-25",
    )
    assert len(rows) == 1
    lng, lat = rows[0]["geom"]["coordinates"][0][0]
    assert -97.0 < lng < -96.0, lng   # Collin County longitudes, not raw metres
    assert 32.0 < lat < 34.0, lat


def test_geographic_crs_passes_through_untouched(tmp_path: Path) -> None:
    # Azle's real .prj is geographic NAD83 -- already lon/lat. Reprojecting
    # it would be a no-op at best; the adapter must not touch the numbers.
    ring = _square(-97.55, 32.87, 0.01)
    _write_shapefile(tmp_path, "zones", NAD83_GEOGRAPHIC_WKT, [(ring, ["AZLE"])])
    rows = adapter_shapefile(
        tmp_path, {"elementary": {"file": "zones.shp", "name_field": "ZONE_NAME"}},
        district_tea_id="220915", district_name="AZLE ISD", boundary_vintage="2026-27",
    )
    lng, lat = rows[0]["geom"]["coordinates"][0][0]
    assert lng == pytest.approx(-97.55, abs=1e-9)
    assert lat == pytest.approx(32.87, abs=1e-9)


def test_projected_non_mercator_crs_reprojects_correctly(tmp_path: Path) -> None:
    # State-plane feet: the case that silently breaks if the .prj is ignored.
    _write_shapefile(tmp_path, "zones", None, [(_square(2400000, 7000000, 500), ["HILLTOP"])])
    rows = adapter_shapefile(
        tmp_path,
        {"elementary": {"file": "zones.shp", "name_field": "ZONE_NAME", "crs": TX_NORTH_CENTRAL_EPSG}},
        district_tea_id="220915", district_name="AZLE ISD", boundary_vintage="2026-27",
    )
    lng, lat = rows[0]["geom"]["coordinates"][0][0]
    assert -98.0 < lng < -96.0, lng
    assert 32.0 < lat < 34.0, lat


def test_missing_prj_is_refused_never_assumed_wgs84(tmp_path: Path) -> None:
    _write_shapefile(tmp_path, "zones", None, [(_square(2400000, 7000000, 500), ["HILLTOP"])])
    with pytest.raises(ValueError, match="no sidecar .prj"):
        adapter_shapefile(
            tmp_path, {"elementary": {"file": "zones.shp", "name_field": "ZONE_NAME"}},
            district_tea_id="220915", district_name="AZLE ISD", boundary_vintage="2026-27",
        )


def test_row_shape_level_and_source_kind(tmp_path: Path) -> None:
    _write_shapefile(tmp_path, "zones", NAD83_GEOGRAPHIC_WKT, [(_square(-97.55, 32.87, 0.01), ["AZLE"])])
    rows = adapter_shapefile(
        tmp_path, {"elementary": {"file": "zones.shp", "name_field": "ZONE_NAME",
                                  "source_url": "https://www.azleisd.net/"}},
        district_tea_id="220915", district_name="AZLE ISD", boundary_vintage="2026-27",
    )
    row = rows[0]
    assert row["level"] == "elementary"
    assert row["source_kind"] == "shapefile"          # never mislabelled "arcgis"
    assert row["district_tea_id"] == "220915"
    assert row["district_name"] == "AZLE ISD"
    assert row["boundary_vintage"] == "2026-27"
    assert row["source_url"] == "https://www.azleisd.net/"
    assert row["campus_tea_id"] is None                # no campus_id_field configured
    assert row["geom"]["type"] in ("Polygon", "MultiPolygon")


def test_campus_id_field_used_only_when_configured(tmp_path: Path) -> None:
    fields = (("ZONE_NAME", "C", 50, 0), ("TEA_ID", "C", 15, 0))
    _write_shapefile(tmp_path, "zones", NAD83_GEOGRAPHIC_WKT,
                     [(_square(-97.55, 32.87, 0.01), ["AZLE", "220915104"])], fields=fields)
    without = adapter_shapefile(
        tmp_path, {"elementary": {"file": "zones.shp", "name_field": "ZONE_NAME"}},
        district_tea_id="220915", district_name="AZLE ISD", boundary_vintage="2026-27",
    )
    assert without[0]["campus_tea_id"] is None
    with_id = adapter_shapefile(
        tmp_path, {"elementary": {"file": "zones.shp", "name_field": "ZONE_NAME", "campus_id_field": "TEA_ID"}},
        district_tea_id="220915", district_name="AZLE ISD", boundary_vintage="2026-27",
    )
    assert with_id[0]["campus_tea_id"] == "220915104"


def test_multipart_polygon_becomes_multipolygon(tmp_path: Path) -> None:
    # Community's McClendon/Edge/Community zones are genuinely multipart.
    parts = _square(-97.55, 32.87, 0.01) + _square(-97.50, 32.90, 0.01)
    _write_shapefile(tmp_path, "zones", NAD83_GEOGRAPHIC_WKT, [(parts, ["McClendon"])])
    rows = adapter_shapefile(
        tmp_path, {"elementary": {"file": "zones.shp", "name_field": "ZONE_NAME"}},
        district_tea_id="043918", district_name="COMMUNITY ISD", boundary_vintage="2024-25",
    )
    assert len(rows) == 1
    assert rows[0]["geom"]["type"] == "MultiPolygon"
    assert len(rows[0]["geom"]["coordinates"]) == 2


def test_blank_named_feature_is_dropped_never_fabricated(tmp_path: Path) -> None:
    _write_shapefile(tmp_path, "zones", NAD83_GEOGRAPHIC_WKT, [
        (_square(-97.55, 32.87, 0.01), ["AZLE"]),
        (_square(-97.50, 32.90, 0.01), [""]),
    ])
    rows = adapter_shapefile(
        tmp_path, {"elementary": {"file": "zones.shp", "name_field": "ZONE_NAME"}},
        district_tea_id="220915", district_name="AZLE ISD", boundary_vintage="2026-27",
    )
    assert [r["campus_name"] for r in rows] == ["AZLE"]


def test_campus_name_is_whitespace_stripped(tmp_path: Path) -> None:
    # dbf character fields are space-padded; a trailing space would break the
    # crosswalk's normalized-name match and the duplicate-name guard.
    _write_shapefile(tmp_path, "zones", NAD83_GEOGRAPHIC_WKT, [(_square(-97.55, 32.87, 0.01), ["  HOOVER  "])])
    rows = adapter_shapefile(
        tmp_path, {"elementary": {"file": "zones.shp", "name_field": "ZONE_NAME"}},
        district_tea_id="220915", district_name="AZLE ISD", boundary_vintage="2026-27",
    )
    assert rows[0]["campus_name"] == "HOOVER"


def test_multiple_levels_in_one_call(tmp_path: Path) -> None:
    for stem, name in (("Elementary_Zones", "NeSmith"), ("Middle_Zones", "Edge"), ("High_Zones", "Community")):
        _write_shapefile(tmp_path, stem, WEB_MERCATOR_WKT, [(_square(-10740000, 3893000, 1000), [name])])
    rows = adapter_shapefile(
        tmp_path,
        {
            "elementary": {"file": "Elementary_Zones.shp", "name_field": "ZONE_NAME"},
            "middle": {"file": "Middle_Zones.shp", "name_field": "ZONE_NAME"},
            "high": {"file": "High_Zones.shp", "name_field": "ZONE_NAME"},
        },
        district_tea_id="043918", district_name="COMMUNITY ISD", boundary_vintage="2024-25",
    )
    assert {r["level"] for r in rows} == {"elementary", "middle", "high"}
    assert {r["campus_name"] for r in rows} == {"NeSmith", "Edge", "Community"}


def test_unknown_level_is_rejected(tmp_path: Path) -> None:
    _write_shapefile(tmp_path, "zones", NAD83_GEOGRAPHIC_WKT, [(_square(-97.55, 32.87, 0.01), ["AZLE"])])
    with pytest.raises(ValueError, match="unknown level"):
        adapter_shapefile(
            tmp_path, {"elem": {"file": "zones.shp", "name_field": "ZONE_NAME"}},
            district_tea_id="220915", district_name="AZLE ISD", boundary_vintage="2026-27",
        )


def test_per_level_boundary_vintage_overrides_the_district_wide_one(tmp_path: Path) -> None:
    # Community ISD: E/M drawn for 2024-25, but the district stated its one
    # high-school zone "has been set since the founding of the district."
    # Stamping 2024-25 on that row would invent a date the district never gave.
    for stem, name in (("Elementary_Zones", "NeSmith"), ("High_Zones", "Community")):
        _write_shapefile(tmp_path, stem, WEB_MERCATOR_WKT, [(_square(-10740000, 3893000, 1000), [name])])
    rows = adapter_shapefile(
        tmp_path,
        {
            "elementary": {"file": "Elementary_Zones.shp", "name_field": "ZONE_NAME"},
            "high": {"file": "High_Zones.shp", "name_field": "ZONE_NAME",
                     "boundary_vintage": "unchanged since district founding"},
        },
        district_tea_id="043918", district_name="COMMUNITY ISD", boundary_vintage="2024-25",
    )
    by_level = {r["level"]: r for r in rows}
    assert by_level["elementary"]["boundary_vintage"] == "2024-25"
    assert by_level["high"]["boundary_vintage"] == "unchanged since district founding"
