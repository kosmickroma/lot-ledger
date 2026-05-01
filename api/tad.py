# api/tad.py
#
# TAD (Tarrant Appraisal District) parcel query module.
# Queries tad_parcels (PostGIS), applies exact polygon filtering, and returns
# rows in the same normalized shape that build_feature / classify_parcel /
# summarize_counts in dcad.py already understand.
#
# Connects to:
#   api/config.py  — shared DB connection helpers
#   api/geo.py     — polygon_bbox, point_in_polygon
#   api/dcad.py    — ParcelQueryResult (shared dataclass), build_feature,
#                    classify_parcel, summarize_counts
#   api/main.py    — called from the analyze endpoint when the polygon bbox
#                    intersects Tarrant County (Phase 4)

from __future__ import annotations

import json
from typing import Any

from api.config import get_conn, release_conn
from api.dcad import ParcelQueryResult, _clean_text, _safe_float, _safe_int
from api.geo import point_in_polygon, polygon_bbox


# TAD state-use code → human-readable label (mirrors SPTD_LABELS in dcad.py)
TAD_STATE_USE_LABELS: dict[str, str] = {
    "A1": "Single Family Residences",
    "A2": "Townhouses",
    "A3": "Condominiums",
    "A4": "Mobile Homes",
    "B1": "Multifamily (2-4 units)",
    "B2": "Multifamily (5+ units)",
    "C1": "Vacant Residential Lots",
    "C2": "Vacant Commercial Lots",
    "D1": "Qualified Agricultural Land",
    "E1": "Rural Residential/Agricultural",
    "F1": "Commercial Improvements",
    "F2": "Industrial Improvements",
    "G1": "Oil and Gas",
    "J": "Utilities",
    "L1": "Commercial Personal Property",
    "X": "Totally Exempt",
}

# State-use codes treated as residential single-family for classification
_SFR_CODES = {"A1", "A2", "A4"}
_CONDO_CODES = {"A3"}
_MF_CODES = {"B1", "B2"}
_VACANT_CODES = {"C1"}
_COMMERCIAL_CODES = {"C2", "F1", "F2", "L1"}
_EXEMPT_CODES = {"D1", "X", "G1", "J"}

_GOV_KEYWORDS = [
    "CITY OF ", "TARRANT COUNTY", "DALLAS COUNTY", "STATE OF TEXAS",
    "UNITED STATES", "TXDOT", " ISD", "FORT WORTH ISD", "DART",
]
_HOA_KEYWORDS = ["HOMEOWNER", "OWNERS ASSOC", " HOA", "CIVIC ASSOC", "PROPERTY OWNERS"]


def _tad_bbox_filter(
    min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT parcel_key, account_num, sequence_num, taxpin, pidn,
                       owner_name, owner_addr, owner_city,
                       situs_addr, property_class, state_use_code, legal_descr,
                       county_code, city_code, school_code,
                       acres, land_sqft,
                       year_built, living_area,
                       land_value, improvement_value, total_value,
                       ST_AsGeoJSON(geom)::json  AS polygon_geojson,
                       ST_AsGeoJSON(centroid)::json AS centroid_json
                FROM tad_parcels
                WHERE ST_Intersects(centroid, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
                """,
                (min_lng, min_lat, max_lng, max_lat),
            )
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        release_conn(conn)


def _centroid_from_geojson(centroid_json: Any) -> tuple[float | None, float | None]:
    """Parse ST_AsGeoJSON Point result → (lat, lng)."""
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
            pass
    return None, None


def _classify_tad(row: dict[str, Any]) -> str:
    """TAD-specific classification → same bucket labels as dcad.classify_parcel."""
    code = _clean_text(row.get("state_use_code")).upper()
    owner_up = _clean_text(row.get("owner_name")).upper()
    tot_val = _safe_float(row.get("total_value")) or 0.0

    gov_match = any(kw in owner_up for kw in _GOV_KEYWORDS)
    hoa_match = any(kw in owner_up for kw in _HOA_KEYWORDS)
    non_target_owner = gov_match or (hoa_match and code not in _SFR_CODES)
    is_nominal = tot_val <= 500 and code in _COMMERCIAL_CODES

    if code in _EXEMPT_CODES or non_target_owner or is_nominal:
        return "exempt"
    if code in _MF_CODES or code in _CONDO_CODES:
        return "multifamily"
    if code in _VACANT_CODES:
        return "vacant"
    if code in _COMMERCIAL_CODES:
        return "commercial"
    return "single_family"


def _normalize_tad_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Map tad_parcels columns → the unified row shape dcad.build_feature expects."""
    state_use_code = _clean_text(raw.get("state_use_code"))
    land_val = _safe_float(raw.get("land_value"))
    tot_val = _safe_float(raw.get("total_value"))
    area_size = _safe_float(raw.get("land_sqft"))
    if area_size is None:
        acres = _safe_float(raw.get("acres"))
        area_size = acres * 43560 if acres is not None else None

    property_address = _clean_text(raw.get("situs_addr")).upper()

    # Mimic the SPTD→label lookup in dcad.py via TAD_STATE_USE_LABELS
    state_label = TAD_STATE_USE_LABELS.get(state_use_code, state_use_code) if state_use_code else ""

    polygon_geojson = raw.get("polygon_geojson")
    if isinstance(polygon_geojson, str):
        try:
            polygon_geojson = json.loads(polygon_geojson)
        except Exception:
            polygon_geojson = None

    return {
        # identity
        "account_num": _clean_text(raw.get("account_num")),
        "parcel_key": _clean_text(raw.get("parcel_key")),
        "gis_parcel_id": _clean_text(raw.get("taxpin")),
        # owner
        "owner_name": _clean_text(raw.get("owner_name")),
        "owner_address": _clean_text(raw.get("owner_addr")),
        "owner_city": _clean_text(raw.get("owner_city")),
        "owner_state": "TX",
        "owner_zip": "",
        # address
        "street_num": "",
        "full_street_name": "",
        "property_address": property_address,
        "property_zip": "",
        # classification
        "division_cd": "TAD",
        "sptd_code": state_use_code,
        "nbhd_cd": "",
        "legal1": _clean_text(raw.get("legal_descr")),
        "legal2": "", "legal3": "", "legal4": "", "legal5": "",
        # spatial
        "lat": _safe_float(raw.get("_lat")),
        "lng": _safe_float(raw.get("_lng")),
        "polygon_geojson": polygon_geojson,
        # financials
        "land_val": land_val,
        "impr_val": _safe_float(raw.get("improvement_value")),
        "tot_val": tot_val,
        "isd_desc": _clean_text(raw.get("school_code")),
        # improvements
        "yr_built": _safe_int(raw.get("year_built")),
        "tot_living_area": _safe_float(raw.get("living_area")),
        "tot_main_sf": _safe_float(raw.get("living_area")),
        # land dims (TAD doesn't carry frontage/depth in ParcelView)
        "zoning": "",
        "front_dim": None,
        "depth_dim": None,
        "area_size": area_size,
        "area_uom": "SQFT",
        # derived
        "state_code": state_label,
        "land_pct": round((land_val / tot_val) * 100, 1) if land_val and tot_val else None,
        "hoa_name": "",
        "hoa_url": "",
        "verified_vacant": "",
        "potential_target": "",
        "county": "Tarrant",
    }


def query_tad_parcels(polygon: list[list[float]]) -> ParcelQueryResult:
    """
    Spatial bbox pre-filter on tad_parcels centroid, then exact point-in-polygon.
    Returns a ParcelQueryResult with the same row shape as dcad.query_parcels.
    """
    min_lat, min_lng, max_lat, max_lng = polygon_bbox(polygon)
    candidates = _tad_bbox_filter(min_lat, min_lng, max_lat, max_lng)

    rows: list[dict[str, Any]] = []
    for raw in candidates:
        lat, lng = _centroid_from_geojson(raw.get("centroid_json"))
        if lat is None or lng is None:
            continue
        if not point_in_polygon(lat, lng, polygon):
            continue
        raw["_lat"] = lat
        raw["_lng"] = lng
        rows.append(_normalize_tad_row(raw))

    # TAD has no separate exempt-accounts table; classification is code-based
    return ParcelQueryResult(parcels=rows, exempt_accounts=set())
