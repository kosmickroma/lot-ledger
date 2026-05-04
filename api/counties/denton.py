# api/counties/denton.py
#
# Denton CAD county query and classification module.
# Queries denton_parcels (PostGIS), applies exact polygon filtering, and returns
# rows in the normalized shape expected by build_feature and CSV export.
#
# Connects to:
#   api/config.py         - shared DB connection helpers
#   api/geo.py            - polygon_bbox, point_in_polygon
#   api/counties/dcad.py  - ParcelQueryResult and helper converters
#   api/main.py           - called from analyze and parcel detail routes

from __future__ import annotations

import json
import math
from typing import Any

from api.config import get_conn, release_conn
from api.counties.dcad import ParcelQueryResult, _clean_text, _safe_float, _safe_int
from api.geo import point_in_polygon, polygon_bbox


_DENTON_MF_CODES = {"B1", "B2"}
_DENTON_VACANT_CODES = {"C1", "C2", "C3", "C5"}
_DENTON_COMMERCIAL_CODES = {"F1", "F2", "F3", "J3", "J5", "OC1"}
_DENTON_EXEMPT_CODES = {"D1", "D2", "E1", "E4"}
_DENTON_SFR_CODES = {"A1", "A2", "A3", "A4", "A5", "A6", "OA1", "OA5"}

_GOV_KEYWORDS = [
    "CITY OF ",
    "COUNTY",
    "STATE OF TEXAS",
    "UNITED STATES",
    " ISD",
    "INDEPENDENT SCHOOL DISTRICT",
    "COLLIN COLLEGE",
]
_HOA_KEYWORDS = ["HOMEOWNER", "OWNERS ASSOC", " HOA", "CIVIC ASSOC", "PROPERTY OWNERS"]


def _primary_code(state_cd: str | None) -> str:
    if not state_cd:
        return ""
    return state_cd.split(",")[0].strip().upper()


def _estimate_front_depth(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    ratio = _safe_float(raw.get("envelope_ratio")) or 999.0
    perim = _safe_float(raw.get("envelope_perim_ft")) or 0.0
    area = _safe_float(raw.get("envelope_area_sqft")) or 0.0
    if ratio > 1.25 or perim <= 0 or area <= 0:
        return None, None
    half_p = perim / 2.0
    disc = half_p * half_p - 4.0 * area
    if disc < 0:
        return None, None
    long_side = round((half_p + math.sqrt(disc)) / 2.0, 0)
    short_side = round((half_p - math.sqrt(disc)) / 2.0, 0)
    if short_side <= 0:
        return None, None
    return short_side, long_side


def _denton_bbox_filter(min_lat: float, min_lng: float, max_lat: float, max_lng: float) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    parcel_key,
                    account_num,
                    geo_id,
                    owner_name,
                    owner_address,
                    owner_city,
                    owner_state,
                    owner_zip,
                    property_address,
                    property_city,
                    property_zip,
                    state_cd,
                    exemptions,
                    land_value,
                    improvement_value,
                    total_value,
                    land_sqft,
                    land_total_sqft,
                    land_acres,
                    living_area,
                    year_built,
                    isd_desc,
                    entity_codes,
                    deed_number,
                    deed_date,
                    legal_descr,
                    subdivision,
                    zoning,
                    area_estimated,
                    ST_Area(ST_OrientedEnvelope(geom)::geography) / NULLIF(ST_Area(geom::geography), 0) AS envelope_ratio,
                    ST_Perimeter(ST_OrientedEnvelope(geom)::geography) * 3.28084 AS envelope_perim_ft,
                    ST_Area(ST_OrientedEnvelope(geom)::geography) * 10.763910416709722 AS envelope_area_sqft,
                    ST_Area(geom::geography) * 10.763910416709722 AS geom_sqft,
                    ST_AsGeoJSON(geom)::json AS polygon_geojson,
                    ST_AsGeoJSON(centroid)::json AS centroid_json
                FROM denton_parcels
                WHERE ST_Intersects(centroid, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
                """,
                (min_lng, min_lat, max_lng, max_lat),
            )
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        release_conn(conn)


def _centroid_from_geojson(centroid_json: Any) -> tuple[float | None, float | None]:
    if isinstance(centroid_json, dict):
        if centroid_json.get("type") == "Point":
            coords = centroid_json.get("coordinates", [])
            if len(coords) >= 2:
                return _safe_float(coords[1]), _safe_float(coords[0])
    if isinstance(centroid_json, str):
        try:
            obj = json.loads(centroid_json)
            return _centroid_from_geojson(obj)
        except Exception:
            return None, None
    return None, None


def _classify_denton(row: dict[str, Any]) -> str:
    code = _primary_code(_clean_text(row.get("sptd_code")))

    if code in _DENTON_EXEMPT_CODES:
        return "exempt"

    owner_up = _clean_text(row.get("owner_name")).upper()
    gov_match = any(kw in owner_up for kw in _GOV_KEYWORDS)
    hoa_match = any(kw in owner_up for kw in _HOA_KEYWORDS)
    non_target_owner = gov_match or (hoa_match and code not in {"A1", "A2", "A3", "A4", "A5", "A6", "A9"})
    if non_target_owner:
        return "exempt"

    if code in _DENTON_MF_CODES:
        return "multifamily"
    if code in _DENTON_VACANT_CODES:
        return "vacant"
    if code in _DENTON_COMMERCIAL_CODES:
        return "commercial"
    if code in _DENTON_SFR_CODES:
        return "single_family"
    return "single_family"


def _normalize_denton_row(raw: dict[str, Any]) -> dict[str, Any]:
    sptd_code = _clean_text(raw.get("state_cd"))
    land_val = _safe_float(raw.get("land_value"))
    tot_val = _safe_float(raw.get("total_value"))

    area_size = _safe_float(raw.get("land_sqft"))
    area_estimated = bool(raw.get("area_estimated"))
    if not area_size:
        land_acres = _safe_float(raw.get("land_acres"))
        if land_acres:
            area_size = land_acres * 43560
    if not area_size:
        geom_sqft = _safe_float(raw.get("geom_sqft"))
        if geom_sqft and geom_sqft > 0:
            area_size = geom_sqft
            area_estimated = True

    front_dim, depth_dim = _estimate_front_depth(raw)
    dims_estimated = front_dim is not None

    property_address = _clean_text(raw.get("property_address")).upper()
    if not property_address:
        property_address = _clean_text(raw.get("legal_descr")).upper()

    polygon_geojson = raw.get("polygon_geojson")
    if isinstance(polygon_geojson, str):
        try:
            polygon_geojson = json.loads(polygon_geojson)
        except Exception:
            polygon_geojson = None

    exempt_tokens = {t.strip().upper() for t in _clean_text(raw.get("exemptions")).split(",") if t.strip()}

    return {
        "account_num": _clean_text(raw.get("account_num")),
        "parcel_key": _clean_text(raw.get("parcel_key")),
        "gis_parcel_id": _clean_text(raw.get("geo_id")),
        "owner_name": _clean_text(raw.get("owner_name")),
        "owner_address": _clean_text(raw.get("owner_address")),
        "owner_city": _clean_text(raw.get("owner_city")),
        "owner_state": _clean_text(raw.get("owner_state")),
        "owner_zip": _clean_text(raw.get("owner_zip")),
        "street_num": "",
        "full_street_name": "",
        "property_address": property_address,
        "property_city": _clean_text(raw.get("property_city")),
        "property_zip": _clean_text(raw.get("property_zip")),
        "division_cd": "DENTON",
        "sptd_code": sptd_code,
        "nbhd_cd": _clean_text(raw.get("subdivision")),
        "legal1": _clean_text(raw.get("legal_descr")),
        "legal2": "",
        "legal3": "",
        "legal4": "",
        "legal5": "",
        "lat": _safe_float(raw.get("_lat")),
        "lng": _safe_float(raw.get("_lng")),
        "polygon_geojson": polygon_geojson,
        "land_val": land_val,
        "impr_val": _safe_float(raw.get("improvement_value")),
        "tot_val": tot_val,
        "isd_desc": _clean_text(raw.get("isd_desc")),
        "yr_built": _safe_int(raw.get("year_built")),
        "tot_living_area": _safe_float(raw.get("living_area")),
        "tot_main_sf": _safe_float(raw.get("living_area")),
        "zoning": _clean_text(raw.get("zoning")),
        "front_dim": front_dim,
        "depth_dim": depth_dim,
        "dims_estimated": dims_estimated,
        "area_size": area_size,
        "area_uom": "SF" if area_size else "",
        "area_estimated": area_estimated,
        "state_code": sptd_code,
        "land_pct": round((land_val / tot_val) * 100, 1) if land_val and tot_val else None,
        "hoa_name": "",
        "hoa_url": "",
        "verified_vacant": "",
        "potential_target": "",
        "county": "Denton",
        "exemptions": _clean_text(raw.get("exemptions")),
        "exempt_homestead": "HS" if "HS" in exempt_tokens else "",
        "entity_codes": _clean_text(raw.get("entity_codes")),
        "deed_number": _clean_text(raw.get("deed_number")),
        "deed_date": _clean_text(raw.get("deed_date")),
        "legal_descr": _clean_text(raw.get("legal_descr")),
        "subdivision": _clean_text(raw.get("subdivision")),
    }


def query_denton_parcels(polygon: list[list[float]]) -> ParcelQueryResult:
    min_lat, min_lng, max_lat, max_lng = polygon_bbox(polygon)
    candidates = _denton_bbox_filter(min_lat, min_lng, max_lat, max_lng)

    rows: list[dict[str, Any]] = []
    exempt_set: set[str] = set()

    for raw in candidates:
        lat, lng = _centroid_from_geojson(raw.get("centroid_json"))
        if lat is None or lng is None:
            continue
        if not point_in_polygon(lat, lng, polygon):
            continue
        raw["_lat"] = lat
        raw["_lng"] = lng
        normalized = _normalize_denton_row(raw)
        if _primary_code(_clean_text(normalized.get("sptd_code"))) in _DENTON_EXEMPT_CODES:
            exempt_set.add(_clean_text(normalized.get("account_num")))
        rows.append(normalized)

    return ParcelQueryResult(parcels=rows, exempt_accounts=exempt_set)


__all__ = ["query_denton_parcels", "_classify_denton", "_normalize_denton_row"]
