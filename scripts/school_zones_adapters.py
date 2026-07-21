"""Source adapters for the DB-backed school-zones ingest
(docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §4).

Pure file parsers -- NO network calls anywhere in this module. Per §4, the
*download* of a district's source data is a separate manual operator step
(browser export, curl, whatever the source requires); this module only
reads already-downloaded files under a dated snapshot directory
(ingest/schools/<YYYY-MM-DD>/<district>/...), mirroring scripts/
ingest_flood_zones.py's `_read_county_polygons`, which reads local
shapefiles rather than fetching NFHL live.

Each adapter normalizes its source to the same row shape, one dict per
campus-level zone:
    {
        "level": "elementary"|"middle"|"high",
        "district_tea_id": str,
        "district_name": str | None,
        "campus_tea_id": str | None,   # NULL if unresolved -- never guess
        "campus_name": str,
        "geom": {...GeoJSON Polygon/MultiPolygon dict, WGS84...},
        "boundary_vintage": str | None,
        "source_url": str | None,
        "source_kind": "arcgis" | "kml" | "district_boundary" | "pilot_snapshot",
        "retrieved_at": date,
    }

⚠️ campus_tea_id derivation: the pilot's DISD-specific trick (zero-padded
SLN + district prefix reconstructs the TEA CAMPUS_ID) was empirically
verified ONLY against DISD's ~187 zones (docs/AI/SCHOOL_PILOT_DISD_FINDINGS_
2026-07-20.md). It must NOT be silently assumed for another district's
local-id field -- a wrong campus_tea_id would silently attach the wrong
TEA rating to a zone, which is worse than no rating (never fabricate).
Each district's config therefore states its own verified id strategy
explicitly (`campus_id_field` naming a source property that already IS the
TEA id, or omitted entirely -- campus_tea_id stays None, "unresolved," until
a per-district join is verified and the config updated).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Any

_LEVELS = ("elementary", "middle", "high")

_KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}


def _validate_level(level: str) -> str:
    if level not in _LEVELS:
        raise ValueError(f"unknown level {level!r}, must be one of {_LEVELS}")
    return level


def _is_valid_geom(geom: dict[str, Any] | None) -> bool:
    if not isinstance(geom, dict):
        return False
    if geom.get("type") not in ("Polygon", "MultiPolygon"):
        return False
    coords = geom.get("coordinates")
    return bool(coords)


# --- Adapter 1: ArcGIS REST GeoJSON (e.g. Irving) ---------------------------

def adapter_arcgis_geojson(
    snapshot_dir: Path,
    level_config: dict[str, dict[str, Any]],
    district_tea_id: str,
    district_name: str | None,
    boundary_vintage: str | None,
) -> list[dict[str, Any]]:
    """level_config: {level: {"file": "elementary.geojson", "name_field": ...,
    "campus_id_field": ... (optional, omit if unverified), "source_url": ...}}
    Each file is the operator-downloaded `f=geojson` FeatureCollection
    response, already paginated/assembled to completion (§3a #5's
    exceededTransferLimit assertion is the operator's fetch-time
    responsibility; this adapter asserts the flag is absent/false on every
    feature's containing response as a defensive re-check when present)."""
    rows: list[dict[str, Any]] = []
    for level, cfg in level_config.items():
        _validate_level(level)
        path = snapshot_dir / cfg["file"]
        doc = json.loads(path.read_text())
        if doc.get("exceededTransferLimit"):
            raise ValueError(
                f"{path}: exceededTransferLimit is set -- this snapshot is a "
                "cut-off page, not a complete district fetch. Re-download "
                "with pagination before ingesting."
            )
        name_field = cfg["name_field"]
        id_field = cfg.get("campus_id_field")
        for feat in doc.get("features", []):
            props = feat.get("properties") or {}
            campus_name = props.get(name_field)
            geom = feat.get("geometry")
            if not campus_name or not _is_valid_geom(geom):
                continue
            rows.append({
                "level": level,
                "district_tea_id": district_tea_id,
                "district_name": district_name,
                "campus_tea_id": str(props[id_field]) if id_field and props.get(id_field) else None,
                "campus_name": campus_name,
                "geom": geom,
                "boundary_vintage": boundary_vintage,
                "source_url": cfg.get("source_url"),
                "source_kind": "arcgis",
                "retrieved_at": date.today(),
            })
    return rows


# --- Adapter 2: Google MyMaps KML (e.g. Mesquite, DeSoto) -------------------

def _kml_coords_to_ring(text: str) -> list[list[float]]:
    """KML <coordinates> is "lon,lat[,alt] lon,lat[,alt] ..." -- always
    WGS84 per the KML spec (no reprojection needed, unlike ArcGIS's
    sometimes-Mercator storage CRS)."""
    ring = []
    for tup in text.split():
        parts = tup.split(",")
        lng, lat = float(parts[0]), float(parts[1])
        ring.append([lng, lat])
    return ring


def _kml_polygon_to_geom(polygon_el: ET.Element) -> dict[str, Any] | None:
    outer_el = polygon_el.find(".//kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", _KML_NS)
    if outer_el is None or not outer_el.text:
        return None
    rings = [_kml_coords_to_ring(outer_el.text)]
    for hole_el in polygon_el.findall(".//kml:innerBoundaryIs/kml:LinearRing/kml:coordinates", _KML_NS):
        if hole_el.text:
            rings.append(_kml_coords_to_ring(hole_el.text))
    return {"type": "Polygon", "coordinates": rings}


def _kml_placemark_geom(placemark_el: ET.Element) -> dict[str, Any] | None:
    polygons = placemark_el.findall(".//kml:Polygon", _KML_NS)
    parts = [g for p in polygons if (g := _kml_polygon_to_geom(p)) is not None]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"type": "MultiPolygon", "coordinates": [p["coordinates"] for p in parts]}


def parse_kml_placemarks(path: Path) -> list[dict[str, Any]]:
    """[{"name": str, "geom": GeoJSON dict}, ...] -- one per <Placemark>
    that carries at least one Polygon. Placemarks with no polygon (e.g. a
    MyMaps label/pin) are skipped, not fabricated into zero-area geometry."""
    tree = ET.parse(path)
    out = []
    for pm in tree.getroot().findall(".//kml:Placemark", _KML_NS):
        name_el = pm.find("kml:name", _KML_NS)
        name = name_el.text.strip() if name_el is not None and name_el.text else None
        geom = _kml_placemark_geom(pm)
        if name and geom:
            out.append({"name": name, "geom": geom})
    return out


def adapter_mymaps_kml(
    snapshot_dir: Path,
    level_config: dict[str, dict[str, Any]],
    district_tea_id: str,
    district_name: str | None,
    boundary_vintage: str | None,
) -> list[dict[str, Any]]:
    """level_config: {level: {"file": "elementary.kml", "campus_id_map":
    {placemark_name: campus_tea_id} (optional, omit if unverified),
    "source_url": ...}}. MyMaps placemark names are whatever the district
    typed in when drawing the map (verified per-district, not derived)."""
    rows: list[dict[str, Any]] = []
    for level, cfg in level_config.items():
        _validate_level(level)
        path = snapshot_dir / cfg["file"]
        id_map = cfg.get("campus_id_map") or {}
        for placemark in parse_kml_placemarks(path):
            if not _is_valid_geom(placemark["geom"]):
                continue
            rows.append({
                "level": level,
                "district_tea_id": district_tea_id,
                "district_name": district_name,
                "campus_tea_id": id_map.get(placemark["name"]),
                "campus_name": placemark["name"],
                "geom": placemark["geom"],
                "boundary_vintage": boundary_vintage,
                "source_url": cfg.get("source_url"),
                "source_kind": "kml",
                "retrieved_at": date.today(),
            })
    return rows


# --- Adapter 3: single-campus -> district boundary (e.g. Sunnyvale) ---------

def adapter_district_boundary(
    snapshot_dir: Path,
    boundary_file: str,
    campuses: dict[str, dict[str, Any]],
    district_tea_id: str,
    district_name: str | None,
    boundary_vintage: str | None,
    source_url: str | None = None,
) -> list[dict[str, Any]]:
    """For a district small enough to have exactly one campus per level
    (spec §4's "single-campus -> district-boundary" kind, e.g. Sunnyvale
    ISD, registry class "partial"/"ALL-FREE"): the WHOLE district polygon
    IS that campus's zone at every level it has one for -- there's no
    separate attendance sub-boundary to draw.
    campuses: {level: {"campus_name": ..., "campus_tea_id": ... (optional)}}
    boundary_file: a .geojson (Polygon/MultiPolygon Feature or bare
    geometry) or .kml (single Placemark) district-boundary file."""
    path = snapshot_dir / boundary_file
    if path.suffix.lower() == ".kml":
        placemarks = parse_kml_placemarks(path)
        geom = placemarks[0]["geom"] if placemarks else None
    else:
        doc = json.loads(path.read_text())
        geom = doc.get("geometry") if doc.get("type") == "Feature" else doc
    if not _is_valid_geom(geom):
        return []
    rows = []
    for level, campus_cfg in campuses.items():
        _validate_level(level)
        rows.append({
            "level": level,
            "district_tea_id": district_tea_id,
            "district_name": district_name,
            "campus_tea_id": campus_cfg.get("campus_tea_id"),
            "campus_name": campus_cfg["campus_name"],
            "geom": geom,
            "boundary_vintage": boundary_vintage,
            "source_url": source_url,
            "source_kind": "district_boundary",
            "retrieved_at": date.today(),
        })
    return rows


# --- Adapter 4: the pilot's own baked snapshot (DISD, pinned re-ingest) -----
#
# docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md "Gap 4" -- the equivalence
# check (scripts/verify_school_zones_equivalence.py) must compare the DB
# path against the EXACT data the live static path serves today, not a
# fresh FeatureServer pull that may have drifted since the pilot's 2026-07-20
# build. data/school_pilot/{elementary,middle,high,ratings}.json are those
# exact baked files (scripts/build_school_pilot_data.py's output, already
# loaded by api/school_pilot/zones.py at runtime) -- this adapter reads them
# as a pinned, repeatable, snapshot-sourced re-ingest of DISD, through the
# SAME scoped-delete ingest path and guards as every other adapter (no
# bypass: run_guards' zero-feature/50%/registry checks all still apply).

def adapter_pilot_snapshot(
    pilot_data_dir: Path,
    district_tea_id: str,
    district_name: str | None,
) -> list[dict[str, Any]]:
    """Reads data/school_pilot/{elementary,middle,high}.json (the pilot
    build's own output). Each zone's "parts" (list of polygons; each
    polygon = list of rings, ring[0]=outer) IS already a GeoJSON
    MultiPolygon's `coordinates` array verbatim -- scripts/
    build_school_pilot_data.py normalized it to WGS84 at build time, so no
    reprojection happens here (validate_rows()'s is_already_wgs84 check
    downstream is a no-op confirmation, not a transform)."""
    rows: list[dict[str, Any]] = []
    for level in _LEVELS:
        path = pilot_data_dir / f"{level}.json"
        doc = json.loads(path.read_text())
        vintage = doc["meta"].get("boundary_vintage")
        source_url = doc["meta"].get("source_url")
        for zone in doc.get("zones", []):
            campus_name = zone.get("campus_name")
            parts = zone.get("parts")
            if not campus_name or not parts:
                continue
            rows.append({
                "level": level,
                "district_tea_id": district_tea_id,
                "district_name": district_name,
                "campus_tea_id": zone.get("tea_campus_id"),
                "campus_name": campus_name,
                "geom": {"type": "MultiPolygon", "coordinates": parts},
                "boundary_vintage": vintage,
                "source_url": source_url,
                "source_kind": "pilot_snapshot",
                "retrieved_at": date.today(),
            })
    return rows


def pilot_ratings_to_ingest_shape(pilot_ratings_path: Path) -> tuple[int, dict[str, dict[str, Any]]]:
    """data/school_pilot/ratings.json uses the pilot's own key name "grade"
    (see api/school_pilot/zones.py's assign()); scripts/ingest_school_zones.
    py's generic ingest_ratings() expects "letter" (the DB column name) --
    this is the one explicit conversion point between the two, so the
    generic ratings-ingest contract stays clean for future non-pilot
    sources. Returns (rating_year, {campus_tea_id: {letter, score,
    achievement, growth}}) -- rating_year comes from the file's own meta,
    never a hand-typed --rating-year that could drift from the data."""
    doc = json.loads(pilot_ratings_path.read_text())
    year = doc["meta"]["tea_year"]
    out = {}
    for campus_tea_id, info in doc.get("ratings", {}).items():
        out[campus_tea_id] = {
            "letter": info.get("grade"),
            "score": info.get("score"),
            "achievement": info.get("achievement"),
            "growth": info.get("growth"),
        }
    return year, out
