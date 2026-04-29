# api/dcad.py
#
# DCAD query and parcel classification module for analysis responses.
# Pulls parcel/appraisal/detail rows from Supabase, applies exact polygon filtering,
# and builds feature-ready parcel dictionaries with the proven business logic.
#
# Connects to:
#   api/config.py  - uses shared database connection helpers
#   api/geo.py     - uses polygon bbox and point-in-polygon helpers
#   api/main.py    - imported and used by analyze endpoint (Phase 4)

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from api.config import get_conn, release_conn
from api.geo import polygon_bbox


DIVISION_CODES = ["RES", "COM"]
ACCOUNT_CHUNK_SIZE = 500

GOV_KEYWORDS = [
    "CITY OF DALLAS",
    "DALLAS COUNTY",
    "STATE OF TEXAS",
    "UNITED STATES",
    "TXDOT",
    "TX DEPT",
    " ISD",
    "DISD",
    "DART ",
    "NTTA",
]

HOA_KEYWORDS = [
    "HOMEOWNER",
    "OWNERS ASSOC",
    " HOA",
    "CIVIC ASSOC",
    "COMMUNITY ASSOC",
    "PROPERTY OWNERS",
]

SPTD_LABELS = {
    "A11": "Single Family Residences",
    "A12": "Townhouses",
    "A13": "Condominiums",
    "A20": "Mobile Home on Owners Land",
    "B11": "Apartments",
    "B12": "Duplexes",
    "C11": "Vacant Lots/Tracts (SFR)",
    "C12": "Vacant Lots/Tracts (Commercial)",
    "C13": "Vacant Lots/Tracts (Industrial)",
    "C14": "Rural Vacant - Under 5 Acres",
    "D10": "Qualified Agricultural Land",
    "E11": "Ranch Improvements",
    "E12": "Farm Improvements",
    "F10": "Commercial Improvements",
    "F20": "Industrial Improvements",
    "G10": "Oil, Gas and Mineral Reserves",
    "G30": "Minerals, Non-Producing",
    "J51": "Railroad Corridor",
    "L10": "Commercial BPP",
    "M31": "Mobile Homes on Leased Spaces",
    "M32": "Mobile Homes for Sale",
    "O10": "Residential Inventory (Vacant)",
    "O11": "Residential Inventory (Improved)",
    "X11": "Totally Exempt Property",
}


@dataclass
class ParcelQueryResult:
    parcels: list[dict[str, Any]]
    exempt_accounts: set[str]


def _chunked(values: list[str], chunk_size: int = ACCOUNT_CHUNK_SIZE):
    for index in range(0, len(values), chunk_size):
        yield values[index : index + chunk_size]


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "none" else text


def _extract_centroid(point_value: Any) -> tuple[float | None, float | None]:
    """Best-effort parser for PostGIS centroid values returned by PostgREST."""
    if point_value is None:
        return None, None

    if isinstance(point_value, dict):
        if point_value.get("type") == "Point" and isinstance(point_value.get("coordinates"), list):
            coordinates = point_value["coordinates"]
            if len(coordinates) >= 2:
                return _safe_float(coordinates[1]), _safe_float(coordinates[0])
        if "lat" in point_value and "lng" in point_value:
            return _safe_float(point_value.get("lat")), _safe_float(point_value.get("lng"))

    if isinstance(point_value, str):
        text = point_value.strip()
        if text.startswith("SRID=") and "POINT(" in text:
            text = text.split(";", 1)[1]
        if text.upper().startswith("POINT(") and text.endswith(")"):
            body = text[text.find("(") + 1 : -1]
            parts = body.replace(",", " ").split()
            if len(parts) >= 2:
                lng = _safe_float(parts[0])
                lat = _safe_float(parts[1])
                return lat, lng

    return None, None


def _spatial_bbox_filter(min_lat: float, min_lng: float, max_lat: float, max_lng: float) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                    SELECT account_num, parcel_key, gis_parcel_id, owner_name,
                           owner_address, owner_city, owner_state, owner_zip,
                           street_num, full_street_name, property_address, property_zip,
                           division_cd, sptd_code, nbhd_cd, legal1, legal2, legal3,
                              legal4, legal5, polygon_geojson,
                           ST_AsGeoJSON(centroid)::json AS centroid
                    FROM parcels
                                        WHERE division_cd IN ('RES', 'COM')
                                            AND ST_Intersects(centroid, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
                """,
                (min_lng, min_lat, max_lng, max_lat),
            )
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        release_conn(conn)


def _fetch_by_account(table: str, account_nums: list[str], columns: list[str]) -> dict[str, dict[str, Any]]:
    if not account_nums:
        return {}

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            col_list = ", ".join(columns)
            cur.execute(
                f"SELECT {col_list} FROM {table} WHERE account_num = ANY(%s)",
                (list(account_nums),),
            )
            cols = [desc[0] for desc in cur.description]
            return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}
    finally:
        release_conn(conn)


def _fetch_exempt_accounts(account_nums: list[str]) -> set[str]:
    if not account_nums:
        return set()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT account_num FROM exempt_accounts WHERE account_num = ANY(%s)",
                (list(account_nums),),
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        release_conn(conn)


def query_parcels(polygon: list[list[float]]) -> ParcelQueryResult:
    """
    Query candidate parcels by bbox (POC parity behavior).

    Returns merged parcel rows along with the exempt account-number set.
    """
    min_lat, min_lng, max_lat, max_lng = polygon_bbox(polygon)
    candidate_rows = _spatial_bbox_filter(min_lat, min_lng, max_lat, max_lng)

    exact_rows: list[dict[str, Any]] = []
    for row in candidate_rows:
        lat, lng = _extract_centroid(row.get("centroid"))
        if lat is None or lng is None:
            continue
        row["lat"] = lat
        row["lng"] = lng
        exact_rows.append(row)

    account_nums = sorted({_clean_text(row.get("account_num")) for row in exact_rows if _clean_text(row.get("account_num"))})

    appraisal_lookup = _fetch_by_account(
        "appraisal",
        account_nums,
        ["account_num", "land_val", "impr_val", "tot_val", "isd_desc", "sptd_code"],
    )
    res_lookup = _fetch_by_account(
        "res_detail",
        account_nums,
        ["account_num", "yr_built", "tot_living_area", "tot_main_sf"],
    )
    land_lookup = _fetch_by_account(
        "land_detail",
        account_nums,
        ["account_num", "zoning", "front_dim", "depth_dim", "area_size", "area_uom"],
    )
    exempt_accounts = _fetch_exempt_accounts(account_nums)

    merged_rows: list[dict[str, Any]] = []
    for row in exact_rows:
        account_num = _clean_text(row.get("account_num"))
        appraisal = appraisal_lookup.get(account_num, {})
        res_detail = res_lookup.get(account_num, {})
        land_detail = land_lookup.get(account_num, {})

        sptd_code = _clean_text(appraisal.get("sptd_code") or row.get("sptd_code"))
        land_val = _safe_float(appraisal.get("land_val"))
        tot_val = _safe_float(appraisal.get("tot_val"))

        area_size = _safe_float(land_detail.get("area_size"))
        area_uom = _clean_text(land_detail.get("area_uom")).upper()
        if area_size is not None and area_uom == "ACRE":
            area_size = area_size * 43560

        property_address = _clean_text(row.get("property_address"))
        if not property_address:
            property_address = f"{_clean_text(row.get('street_num'))} {_clean_text(row.get('full_street_name'))}".strip()
        property_address = property_address.upper()

        merged_rows.append(
            {
                "account_num": account_num,
                "parcel_key": _clean_text(row.get("parcel_key")),
                "gis_parcel_id": _clean_text(row.get("gis_parcel_id")),
                "owner_name": _clean_text(row.get("owner_name")),
                "owner_address": _clean_text(row.get("owner_address")),
                "owner_city": _clean_text(row.get("owner_city")),
                "owner_state": _clean_text(row.get("owner_state")),
                "owner_zip": _clean_text(row.get("owner_zip")),
                "street_num": _clean_text(row.get("street_num")),
                "full_street_name": _clean_text(row.get("full_street_name")),
                "property_address": property_address,
                "property_zip": _clean_text(row.get("property_zip")),
                "division_cd": _clean_text(row.get("division_cd")),
                "sptd_code": sptd_code,
                "nbhd_cd": _clean_text(row.get("nbhd_cd")),
                "legal1": _clean_text(row.get("legal1")),
                "legal2": _clean_text(row.get("legal2")),
                "legal3": _clean_text(row.get("legal3")),
                "legal4": _clean_text(row.get("legal4")),
                "legal5": _clean_text(row.get("legal5")),
                "lat": _safe_float(row.get("lat")),
                "lng": _safe_float(row.get("lng")),
                "polygon_geojson": row.get("polygon_geojson"),
                "land_val": land_val,
                "impr_val": _safe_float(appraisal.get("impr_val")),
                "tot_val": tot_val,
                "isd_desc": _clean_text(appraisal.get("isd_desc")),
                "yr_built": _safe_int(res_detail.get("yr_built")),
                "tot_living_area": _safe_float(res_detail.get("tot_living_area")),
                "tot_main_sf": _safe_float(res_detail.get("tot_main_sf")),
                "zoning": _clean_text(land_detail.get("zoning")),
                "front_dim": _safe_float(land_detail.get("front_dim")),
                "depth_dim": _safe_float(land_detail.get("depth_dim")),
                "area_size": area_size,
                "area_uom": _clean_text(land_detail.get("area_uom")),
                "state_code": SPTD_LABELS.get(sptd_code, sptd_code),
                "land_pct": round((land_val / tot_val) * 100, 1) if land_val is not None and tot_val not in (None, 0) else None,
            }
        )

    return ParcelQueryResult(parcels=merged_rows, exempt_accounts=exempt_accounts)


def classify_parcel(row: dict[str, Any], exempt_set: set[str]) -> str:
    """Ported parcel type logic from the reference tool."""
    account_num = _clean_text(row.get("account_num"))
    sptd = _clean_text(row.get("sptd_code"))
    owner_up = _clean_text(row.get("owner_name")).upper()

    gov_match = any(keyword in owner_up for keyword in GOV_KEYWORDS)
    hoa_match = any(keyword in owner_up for keyword in HOA_KEYWORDS)
    residential_sptd = sptd in {"A11", "A12", "A13", "A20"}
    non_target_owner = gov_match or (hoa_match and not residential_sptd)

    tot_val = _safe_float(row.get("tot_val")) or 0.0
    is_nominal = tot_val <= 500 and sptd in {"C11", "C12"}

    if account_num in exempt_set or sptd == "X11" or non_target_owner or is_nominal:
        return "exempt"
    if sptd in {"B11", "B12", "A14"}:
        return "multifamily"
    if sptd == "C11":
        return "vacant"
    if sptd in {"C12", "C13", "F10", "F20"}:
        return "commercial"
    return "single_family"


def build_feature(row: dict[str, Any], prop_type: str, on_redfin: bool) -> dict[str, Any]:
    """Build a GeoJSON feature + frontend popup payload for one parcel row."""
    lat = _safe_float(row.get("lat"))
    lng = _safe_float(row.get("lng"))
    if lat is None or lng is None:
        raise ValueError("Parcel row is missing centroid coordinates")

    area_size = _safe_float(row.get("area_size"))
    land_pct = _safe_float(row.get("land_pct"))

    props = {
        "on_redfin": bool(on_redfin),
        "prop_type": prop_type,
        "addr": _clean_text(row.get("property_address")),
        "owner": _clean_text(row.get("owner_name")),
        "land_val": f"${row['land_val']:,.0f}" if _safe_float(row.get("land_val")) is not None else "N/A",
        "tot_val": f"${row['tot_val']:,.0f}" if _safe_float(row.get("tot_val")) is not None else "N/A",
        "land_pct": f"{land_pct:.1f}%" if land_pct is not None else "N/A",
        "lot_acres": f"{area_size / 43560:.2f} ac" if area_size is not None and area_size > 0 else "N/A",
        "frontage": f"{int(row['front_dim'])} ft" if _safe_float(row.get("front_dim")) not in (None, 0.0) else "N/A",
        "depth": f"{int(row['depth_dim'])} ft" if _safe_float(row.get("depth_dim")) not in (None, 0.0) else "N/A",
        "state_code": _clean_text(row.get("state_code")) or "N/A",
        "zoning": _clean_text(row.get("zoning")) or "N/A",
        "school": _clean_text(row.get("isd_desc")) or "N/A",
        "yr_built": str(row.get("yr_built")) if row.get("yr_built") else "N/A",
        "sqft": f"{int(float(row['tot_living_area'])):,}" if _safe_float(row.get("tot_living_area")) not in (None, 0.0) else "N/A",
        "lat": lat,
        "lng": lng,
    }

    geometry = row.get("polygon_geojson")
    if isinstance(geometry, dict) and geometry.get("type") == "Polygon":
        feature_geometry = geometry
    else:
        feature_geometry = {"type": "Point", "coordinates": [lng, lat]}

    return {
        "type": "Feature",
        "geometry": feature_geometry,
        "properties": props,
    }


def summarize_counts(rows: list[dict[str, Any]], exempt_set: set[str], on_redfin_addresses: set[str]) -> dict[str, int]:
    """Return aggregate counts by listing/category for legend and API response."""
    active = 0
    multifamily = 0
    vacant = 0
    commercial = 0
    exempt = 0

    for row in rows:
        parcel_key = _clean_text(row.get("parcel_key"))
        account_num = _clean_text(row.get("account_num"))
        direct_match = parcel_key == account_num if parcel_key else True
        on_redfin = _clean_text(row.get("property_address")).upper() in on_redfin_addresses and direct_match
        if on_redfin:
            active += 1
            continue

        prop_type = classify_parcel(row, exempt_set)
        if prop_type == "multifamily":
            multifamily += 1
        elif prop_type == "vacant":
            vacant += 1
        elif prop_type == "commercial":
            commercial += 1
        elif prop_type == "exempt":
            exempt += 1

    total = len(rows)
    off_market = total - active - multifamily - vacant - commercial - exempt
    return {
        "active": active,
        "off_market": max(off_market, 0),
        "multifamily": multifamily,
        "vacant": vacant,
        "commercial": commercial,
        "exempt": exempt,
        "total": total,
    }
