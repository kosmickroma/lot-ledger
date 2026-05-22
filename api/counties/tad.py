# api/counties/tad.py
#
# Tarrant Appraisal District (TAD) parcel query module.
# Queries tad_parcels (PostGIS), applies exact polygon filtering, and returns
# rows in the same normalized shape that build_feature and classify_parcel expect.
#
# Connects to:
#   api/config.py         — shared DB connection helpers
#   api/geo.py            — polygon_bbox, point_in_polygon
#   api/counties/dcad.py  — ParcelQueryResult dataclass, build_feature, classify_parcel
#   api/main.py           — called from the analyze endpoint

from __future__ import annotations

import json
import math
from typing import Any

from api.config import get_conn, release_conn
from api.counties.dcad import ParcelQueryResult, _clean_text, _safe_float, _safe_int
from api.geo import point_in_polygon, polygon_bbox


# TAD state-use code → human-readable label (mirrors SPTD_LABELS in dcad.py)
# property_class → human-readable label (mirrors SPTD_LABELS in dcad.py).
# TAD stores a broad state_use_code ("A", "B", "C"…) AND a detailed property_class
# ("A1", "A2", "C1", "F1"…).  property_class is what we use for both classification
# and display labels.
TAD_CLASS_LABELS: dict[str, str] = {
    "A":   "Single Family Residences",
    "A1":  "Single Family Residences",
    "A2":  "Townhouses",
    "A3":  "Condominiums",
    "A4":  "Mobile Homes",
    "B1":  "Multifamily (2–4 units)",
    "B2":  "Multifamily (5+ units)",
    "B3":  "Multifamily (5+ units)",
    "B4":  "Multifamily (5+ units)",
    "BC":  "Commercial/Mixed-Use",
    "C1":  "Vacant Residential Lots",
    "C1C": "Vacant Residential Lots",
    "C2":  "Vacant Commercial Lots",
    "C2C": "Vacant Commercial Lots",
    "D1":  "Qualified Agricultural Land",
    "D2":  "Timberland",
    "E1":  "Rural Residential/Agricultural",
    "EC":  "Rural Residential/Agricultural",
    "F1":  "Commercial Improvements",
    "F2":  "Industrial Improvements",
    "G1":  "Oil and Gas",
    "G3":  "Minerals",
    "J1":  "Water/Gas Utilities",
    "J2":  "Electric Utilities",
    "J3":  "Radio/TV Cable Utilities",
    "J4":  "Telephone Utilities",
    "J5":  "Railroad Corridor",
    "L1":  "Commercial Personal Property",
    "L2":  "Industrial Personal Property",
    "M1":  "Mobile Homes (Leased Land)",
    "M2":  "Mobile Homes (Other)",
    "O1":  "Residential Inventory (Vacant)",
    "O2":  "Residential Inventory (Improved)",
    "ROC": "Right of Way/Common Area",
    "S":   "Special Inventory",
    "AC":  "Agricultural Common Area",
    "X":   "Totally Exempt",
}

# property_class sets for classification
_SFR_CODES     = {"A", "A1", "A2", "A4", "E1", "EC"}
_CONDO_CODES   = {"A3"}
_MF_CODES      = {"B1", "B2", "B3", "B4", "M1", "M2"}
_VACANT_CODES  = {"C1", "C1C", "O1"}
_COMMERCIAL_CODES = {"C2", "C2C", "BC", "F1", "F2", "L1", "L2", "S"}
_EXEMPT_CODES  = {"D1", "D2", "G1", "G2", "G3", "G4", "J1", "J2", "J3", "J4", "J5",
                  "ROC", "AC", "X"}

_GOV_KEYWORDS = [
    "CITY OF ", "TARRANT COUNTY", "DALLAS COUNTY", "STATE OF TEXAS",
    "UNITED STATES", "TXDOT", " ISD", "FORT WORTH ISD", "DART",
]
_HOA_KEYWORDS = ["HOMEOWNER", "OWNERS ASSOC", " HOA", "CIVIC ASSOC", "PROPERTY OWNERS"]


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


def _tad_subdivision_from_legal(legal_descr: str) -> str:
    """Best-effort subdivision extractor from ParcelView legal description text."""
    text = _clean_text(legal_descr)
    if not text:
        return ""

    upper = text.upper()
    cut_tokens = [" LOT", " BLK", " BLOCK", " TRACT", " ACRES", " AC"]
    cut_idx = len(text)
    for token in cut_tokens:
        idx = upper.find(token)
        if idx != -1:
            cut_idx = min(cut_idx, idx)

    candidate = text[:cut_idx].strip(" ,.-")
    if not candidate:
        return ""
    return candidate[:64]


def _tad_bbox_filter(
    min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT parcel_key, account_num, sequence_num, taxpin, pidn,
                       owner_name, owner_addr, owner_city, owner_citystate, owner_zip, owner_zip4,
                       situs_addr, property_city, property_class, state_use_code, legal_descr,
                       county_code, city_code, school_code,
                       acres, land_acres, land_sqft,
                       year_built, living_area,
                       land_value, improvement_value, total_value,
                      ST_Area(ST_OrientedEnvelope(geom)::geography) / NULLIF(ST_Area(geom::geography), 0) AS envelope_ratio,
                      ST_Perimeter(ST_OrientedEnvelope(geom)::geography) * 3.28084 AS envelope_perim_ft,
                      ST_Area(ST_OrientedEnvelope(geom)::geography) * 10.763910416709722 AS envelope_area_sqft,
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
    code = _clean_text(row.get("sptd_code")).upper()   # sptd_code ← property_class
    owner_up = _clean_text(row.get("owner_name")).upper()
    tot_val = _safe_float(row.get("tot_val")) or 0.0

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
    # property_class is the detailed 2–3 char code (A1, C1, F1…); use it for
    # both classification (stored as sptd_code) and the human-readable label.
    property_class = _clean_text(raw.get("property_class")) or _clean_text(raw.get("state_use_code"))
    land_val = _safe_float(raw.get("land_value"))
    tot_val = _safe_float(raw.get("total_value"))
    # Best-effort area: land_sqft → land_acres*43560 → acres*43560
    area_size = _safe_float(raw.get("land_sqft")) or None
    if not area_size:
        land_acres = _safe_float(raw.get("land_acres"))
        if land_acres:
            area_size = land_acres * 43560
    if not area_size:
        acres = _safe_float(raw.get("acres"))
        if acres:
            area_size = acres * 43560

    front_dim, depth_dim = _estimate_front_depth(raw)
    dims_estimated = front_dim is not None

    property_address = _clean_text(raw.get("situs_addr")).upper()
    if not property_address:
        property_address = _clean_text(raw.get("owner_addr")).upper()

    legal_descr = _clean_text(raw.get("legal_descr"))
    subdivision = _tad_subdivision_from_legal(legal_descr)
    school_code = _clean_text(raw.get("school_code"))
    school_display = f"ISD {school_code}" if school_code else ""

    state_label = TAD_CLASS_LABELS.get(property_class, property_class) if property_class else ""

    polygon_geojson = raw.get("polygon_geojson")
    if isinstance(polygon_geojson, str):
        try:
            polygon_geojson = json.loads(polygon_geojson)
        except Exception:
            polygon_geojson = None

    owner_zip = _clean_text(raw.get("owner_zip"))

    return {
        # identity
        "account_num": _clean_text(raw.get("account_num")),
        "parcel_key": _clean_text(raw.get("parcel_key")),
        "gis_parcel_id": _clean_text(raw.get("taxpin")),
        # owner
        "owner_name": _clean_text(raw.get("owner_name")),
        "owner_address": _clean_text(raw.get("owner_addr")),
        "owner_city": _clean_text(raw.get("owner_city")) or _clean_text(raw.get("owner_citystate")),
        "owner_state": "TX",
        "owner_zip": owner_zip,
        # address
        "street_num": "",
        "full_street_name": "",
        "property_address": property_address,
        "property_city": _clean_text(raw.get("property_city")),
        "property_zip": owner_zip,
        # classification
        "division_cd": "TAD",
        "sptd_code": property_class,       # detailed code used for classify + label
        "nbhd_cd": subdivision,
        "subdivision": subdivision,
        "legal1": legal_descr,
        "legal2": "", "legal3": "", "legal4": "", "legal5": "",
        # spatial
        "lat": _safe_float(raw.get("_lat")),
        "lng": _safe_float(raw.get("_lng")),
        "polygon_geojson": polygon_geojson,
        # financials
        "land_val": land_val,
        "impr_val": _safe_float(raw.get("improvement_value")),
        "tot_val": tot_val,
        "isd_desc": school_display,
        # improvements
        "yr_built": _safe_int(raw.get("year_built")),
        "tot_living_area": _safe_float(raw.get("living_area")),
        "tot_main_sf": _safe_float(raw.get("living_area")),
        # land dims (TAD doesn't carry frontage/depth in ParcelView)
        "zoning": "",
        "front_dim": front_dim,
        "depth_dim": depth_dim,
        "dims_estimated": dims_estimated,
        "area_size": area_size,
        "area_uom": "SQFT",
        "area_estimated": False,
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
