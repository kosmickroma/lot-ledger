# api/counties/collin.py
#
# Collin CAD county query and classification module.
# Queries collin_parcels (PostGIS), applies exact polygon filtering, and returns
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
import re
from typing import Any

from api.config import get_conn, release_conn
from api.counties.dcad import ParcelQueryResult, _clean_text, _safe_float, _safe_int
from api.geo import point_in_polygon, polygon_bbox


# Collin uses PTAD-style state category codes.
#
# Duplexes (2026-06-01): the Texas Comptroller's PTAD code set is
# semantically precise — B2/B3/B4 = Duplex/Triplex/Quadplex by
# definition — so the duplex bucket trusts the code directly. No
# `units`-field check is needed (or possible): a data audit showed
# Collin's `units` field is 99% NULL/0 across all the multifamily-side
# codes, so it can't serve as a tie-breaker the way it does for DCAD.
# Both sets are kept explicit at the module level so a future code
# addition to _COLLIN_MF_CODES (e.g. if PTAD ever issues a new B-series
# code) forces an explicit decision about which bucket it belongs in,
# instead of silently routing to duplexes via a set subtraction.
_COLLIN_DUPLEX_CODES = {"B2", "B3", "B4"}
_COLLIN_MF_ONLY_CODES = {"A3", "B1", "B6", "B9"}
_COLLIN_MF_CODES = _COLLIN_DUPLEX_CODES | _COLLIN_MF_ONLY_CODES
_COLLIN_VACANT_CODES = {"C1", "C2", "C3", "C4", "C5", "C6", "O"}
_COLLIN_COMMERCIAL_CODES = {"F1", "F2", "F3", "F4", "F6", "F7", "F9", "M1", "M2", "M4", "M5"}
_COLLIN_EXEMPT_CODES = {"D1", "D2", "D6", "J1A", "J2A", "J3A", "J4A", "J5", "J6A"}

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


def _collin_bbox_filter(min_lat: float, min_lng: float, max_lat: float, max_lng: float) -> list[dict[str, Any]]:
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
                    state_cd_name,
                    class_cd,
                    subdivision,
                    legal_descr,
                    school_code,
                    city_code,
                    zoning,
                    land_sqft,
                    land_acres,
                    living_area,
                    year_built,
                    land_value,
                    improvement_value,
                    total_value,
                    cert_total_value,
                    curr_market_value,
                    curr_assessed_value,
                    curr_ag_use_value,
                    curr_ag_market_value,
                    curr_ag_loss_value,
                    deed_num,
                    deed_type,
                    deed_date,
                    land_type_code,
                    land_type_name,
                    prop_use_code,
                    prop_use_name,
                    prop_type,
                    prop_sub_type,
                    commercial_flag,
                    pool_flag,
                    beds,
                    baths,
                    stories,
                    units,
                    protest_code,
                    entity_codes,
                    exemptions,
                    exempt_homestead,
                    tax_agent_id,
                    tax_agent_name,
                    tax_agent_auth_protest,
                    tax_agent_auth_resolve,
                    tax_agent_mailings,
                    permit_count,
                    latest_permit_date,
                    latest_permit_type,
                    latest_permit_value,
                    protest_case_count,
                    latest_protest_year,
                    latest_protest_status,
                    latest_protest_final_market,
                    protest_active,
                    ag_type,
                    ag_acres,
                    ag_value,
                    ag_market_value,
                    ST_Area(ST_OrientedEnvelope(geom)::geography) / NULLIF(ST_Area(geom::geography), 0) AS envelope_ratio,
                    ST_Perimeter(ST_OrientedEnvelope(geom)::geography) * 3.28084 AS envelope_perim_ft,
                    ST_Area(ST_OrientedEnvelope(geom)::geography) * 10.763910416709722 AS envelope_area_sqft,
                    ST_Area(geom::geography) * 10.763910416709722 AS geom_sqft,
                    ST_AsGeoJSON(geom)::json AS polygon_geojson,
                    ST_AsGeoJSON(centroid)::json AS centroid_json
                FROM collin_parcels
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


def _split_exemption_tokens(value: Any) -> set[str]:
    text = _clean_text(value).upper()
    if not text:
        return set()
    # Collin stores comma-delimited tokens (HS,OV65,EX-XV...).
    tokens = [t.strip() for t in re.split(r"[,;|/]", text) if t.strip()]
    return set(tokens)


def _is_full_exempt(row: dict[str, Any]) -> bool:
    state_cd = _clean_text(row.get("sptd_code")).upper()
    if state_cd.startswith("EX") or state_cd in _COLLIN_EXEMPT_CODES:
        return True

    for token in _split_exemption_tokens(row.get("exemptions")):
        if token.startswith("EX"):
            return True
    return False


def _classify_collin(row: dict[str, Any]) -> str:
    """Classify a Collin parcel.

    Reads from the NORMALIZED row produced by `_normalize_collin_row`,
    not the raw SQL row — `row.get("sptd_code")` here is the normalized
    alias for what comes off the wire as `state_cd`. If a future call
    site passes the raw SQL row, the classifier will silently return
    "single_family" for every parcel.

    Duplexes (2026-06-01): PTAD codes B2 (Duplex), B3 (Triplex), and
    B4 (Quadplex) route to the duplexes bucket. B1 (Multi-family),
    A3 (Condo), B6/B9 (other multifamily) stay multifamily even if a
    rare row happens to carry `units` 2-4 — DCAD-precedent "the
    appraisal district's classification wins over the count" applies
    here too, and Collin's `units` field is 99% NULL/0 anyway so it
    can't be used as a tie-breaker.
    """
    code = _clean_text(row.get("sptd_code")).upper()
    owner_up = _clean_text(row.get("owner_name")).upper()
    tot_val = _safe_float(row.get("tot_val")) or 0.0

    gov_match = any(kw in owner_up for kw in _GOV_KEYWORDS)
    hoa_match = any(kw in owner_up for kw in _HOA_KEYWORDS)
    non_target_owner = gov_match or (hoa_match and code not in {"A1", "A2", "A4", "A6", "A9"})
    is_nominal = tot_val <= 500 and code in _COLLIN_VACANT_CODES

    if _is_full_exempt(row) or non_target_owner or is_nominal:
        return "exempt"
    if code in _COLLIN_DUPLEX_CODES:
        return "duplexes"
    if code in _COLLIN_MF_ONLY_CODES:
        return "multifamily"
    if code in _COLLIN_VACANT_CODES:
        return "vacant"
    if code in _COLLIN_COMMERCIAL_CODES:
        return "commercial"
    return "single_family"


def _normalize_collin_row(raw: dict[str, Any]) -> dict[str, Any]:
    sptd_code = _clean_text(raw.get("state_cd"))
    land_val = _safe_float(raw.get("land_value"))
    tot_val_current = _safe_float(raw.get("total_value"))
    cert_tot_val = _safe_float(raw.get("cert_total_value"))

    # Prior-year fallback: if current is null/0 but cert is real, use cert.
    # `cert_total_value` represents Collin's last final certified value, baked
    # into the shapefile DBF — when GIS staff have drawn a polygon but the
    # appraisal team hasn't entered this year's value yet, this carries the
    # last-known-good value forward. Tag with provenance for UX surfacing.
    total_value_source: str | None = None
    if (tot_val_current is None or tot_val_current <= 0) and cert_tot_val and cert_tot_val > 0:
        tot_val = cert_tot_val
        cert_year = _clean_text(raw.get("cert_val_year")) or ""
        total_value_source = f"prior_year_cert_{cert_year}" if cert_year else "prior_year_cert"
    else:
        tot_val = tot_val_current

    area_size = _safe_float(raw.get("land_sqft"))
    area_estimated = False
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
        "property_zip": _clean_text(raw.get("property_zip")),
        "division_cd": "COLLIN",
        "sptd_code": sptd_code,
        "nbhd_cd": _clean_text(raw.get("subdivision")) or _clean_text(raw.get("city_code")),
        "subdivision": _clean_text(raw.get("subdivision")) or "",
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
        "isd_desc": _clean_text(raw.get("school_code")),
        "yr_built": _safe_int(raw.get("year_built")),
        "tot_living_area": _safe_float(raw.get("living_area")),
        "tot_main_sf": _safe_float(raw.get("living_area")),
        "zoning": _clean_text(raw.get("zoning")),
        "front_dim": front_dim,
        "depth_dim": depth_dim,
        "dims_estimated": dims_estimated,
        "area_size": area_size,
        "area_uom": "SQFT" if area_size else "",
        "area_estimated": area_estimated,
        "state_code": _clean_text(raw.get("state_cd_name")) or sptd_code,
        "land_pct": round((land_val / tot_val) * 100, 1) if land_val and tot_val else None,
        "hoa_name": "",
        "hoa_url": "",
        "verified_vacant": "",
        "potential_target": "",
        "county": "Collin",
        "property_city": _clean_text(raw.get("property_city")),
        "city_code": _clean_text(raw.get("city_code")),
        "class_cd": _clean_text(raw.get("class_cd")),
        "prop_use_code": _clean_text(raw.get("prop_use_code")),
        "prop_use_name": _clean_text(raw.get("prop_use_name")),
        "land_type_code": _clean_text(raw.get("land_type_code")),
        "land_type_name": _clean_text(raw.get("land_type_name")),
        "prop_type": _clean_text(raw.get("prop_type")),
        "prop_sub_type": _clean_text(raw.get("prop_sub_type")),
        "commercial_flag": _clean_text(raw.get("commercial_flag")),
        "pool_flag": _clean_text(raw.get("pool_flag")),
        "beds": _safe_float(raw.get("beds")),
        "baths": _safe_float(raw.get("baths")),
        "stories": _safe_float(raw.get("stories")),
        "units": _safe_float(raw.get("units")),
        "deed_num": _clean_text(raw.get("deed_num")),
        "deed_type": _clean_text(raw.get("deed_type")),
        "deed_date": _clean_text(raw.get("deed_date")),
        "entity_codes": _clean_text(raw.get("entity_codes")),
        "exemptions": _clean_text(raw.get("exemptions")),
        "exempt_homestead": _clean_text(raw.get("exempt_homestead")),
        "protest_code": _clean_text(raw.get("protest_code")),
        "tax_agent_id": _clean_text(raw.get("tax_agent_id")),
        "tax_agent_name": _clean_text(raw.get("tax_agent_name")),
        "tax_agent_auth_protest": _clean_text(raw.get("tax_agent_auth_protest")),
        "tax_agent_auth_resolve": _clean_text(raw.get("tax_agent_auth_resolve")),
        "tax_agent_mailings": _clean_text(raw.get("tax_agent_mailings")),
        "permit_count": _safe_int(raw.get("permit_count")),
        "latest_permit_date": _clean_text(raw.get("latest_permit_date")),
        "latest_permit_type": _clean_text(raw.get("latest_permit_type")),
        "latest_permit_value": _safe_float(raw.get("latest_permit_value")),
        "protest_case_count": _safe_int(raw.get("protest_case_count")),
        "latest_protest_year": _safe_int(raw.get("latest_protest_year")),
        "latest_protest_status": _clean_text(raw.get("latest_protest_status")),
        "latest_protest_final_market": _safe_float(raw.get("latest_protest_final_market")),
        "protest_active": _clean_text(raw.get("protest_active")),
        "ag_type": _clean_text(raw.get("ag_type")),
        "ag_acres": _safe_float(raw.get("ag_acres")),
        "ag_value": _safe_float(raw.get("ag_value")),
        "ag_market_value": _safe_float(raw.get("ag_market_value")),
        "curr_market_value": _safe_float(raw.get("curr_market_value")),
        "curr_assessed_value": _safe_float(raw.get("curr_assessed_value")),
        "curr_ag_use_value": _safe_float(raw.get("curr_ag_use_value")),
        "curr_ag_market_value": _safe_float(raw.get("curr_ag_market_value")),
        "curr_ag_loss_value": _safe_float(raw.get("curr_ag_loss_value")),
        "cert_total_value": _safe_float(raw.get("cert_total_value")),
        "total_value_source": total_value_source,  # None when current-year is used
    }


def query_collin_parcels(polygon: list[list[float]]) -> ParcelQueryResult:
    min_lat, min_lng, max_lat, max_lng = polygon_bbox(polygon)
    candidates = _collin_bbox_filter(min_lat, min_lng, max_lat, max_lng)

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
        normalized = _normalize_collin_row(raw)
        if _is_full_exempt(normalized):
            exempt_set.add(_clean_text(normalized.get("account_num")))
        rows.append(normalized)

    return ParcelQueryResult(parcels=rows, exempt_accounts=exempt_set)


__all__ = ["query_collin_parcels", "_classify_collin", "_normalize_collin_row"]
