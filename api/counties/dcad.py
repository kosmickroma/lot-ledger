# api/counties/dcad.py
#
# Dallas Central Appraisal District (DCAD) query and classification module.
# Queries parcels/appraisal/detail tables, applies exact polygon filtering,
# classifies each parcel by type, and builds GeoJSON features for the API response.
#
# Connects to:
#   api/config.py        — shared DB connection helpers
#   api/geo.py           — polygon_bbox, point_in_polygon
#   api/redfin.py        — normalize_addr_key for address matching
#   api/counties/tad.py  — imports ParcelQueryResult, _clean_text, _safe_float, _safe_int
#   api/main.py          — imports build_feature, classify_parcel, query_parcels

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

import pandas as pd

from api.config import get_conn, release_conn
from api.geo import point_in_polygon, polygon_bbox
from api.redfin import normalize_addr_key


DIVISION_CODES = ["RES", "COM"]
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


def _dcad_subdivision_from_legal(legal_descr: str | None) -> str:
    """Best-effort subdivision name extractor from DCAD legal description.

    DCAD legal1 text follows the same convention as TAD ParcelView: the
    subdivision name appears at the front, followed by BLK / LOT / ACS
    qualifiers. e.g., "BRYAN PLACE PHASE V SEC 1 & 2 BLK 1/287 PT LT 1C
    ACS 0.9432" → "BRYAN PLACE PHASE V SEC 1 & 2". Mirrors
    api/counties/tad.py:_tad_subdivision_from_legal — kept local instead
    of factoring to a shared helper module per project convention.
    """
    text = _clean_text(legal_descr)
    if not text:
        return ""
    upper = text.upper()
    cut_tokens = [" LOT", " BLK", " BLOCK", " TRACT", " ACRES", " AC", " ACS"]
    cut_idx = len(text)
    for token in cut_tokens:
        idx = upper.find(token)
        if idx != -1:
            cut_idx = min(cut_idx, idx)
    candidate = text[:cut_idx].strip(" ,.-")
    if not candidate:
        return ""
    return candidate[:64]


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


def _fetch_hoa_lookup(parcels: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """
    Batch spatial join: returns {account_num: {hoa_name, hoa_url}} for any
    parcel centroid that falls inside an HOA boundary polygon.
    Returns an empty dict gracefully if the hoa_boundaries table does not exist.
    """
    candidates = [
        (r["account_num"], r["lng"], r["lat"])
        for r in parcels
        if r.get("lat") is not None and r.get("lng") is not None and r.get("account_num")
    ]
    if not candidates:
        return {}

    account_nums = [c[0] for c in candidates]
    lngs = [c[1] for c in candidates]
    lats = [c[2] for c in candidates]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (p.account_num)
                       p.account_num,
                       h.asso_name,
                       h.asso_web
                FROM (
                    SELECT unnest(%s::varchar[]) AS account_num,
                           unnest(%s::float8[])  AS lng,
                           unnest(%s::float8[])  AS lat
                ) p
                LEFT JOIN hoa_boundaries h
                       ON ST_Within(
                              ST_SetSRID(ST_MakePoint(p.lng, p.lat), 4326),
                              h.geom
                          )
                WHERE h.objectid IS NOT NULL
                """,
                (account_nums, lngs, lats),
            )
            return {
                row[0]: {"hoa_name": row[1] or "", "hoa_url": row[2] or ""}
                for row in cur.fetchall()
            }
    except Exception as exc:
        logger.warning("HOA boundary lookup failed (table missing or query error): %s", exc)
        return {}
    finally:
        release_conn(conn)


def query_parcels(polygon: list[list[float]]) -> ParcelQueryResult:
    """
    Single-query DCAD fetch: bbox spatial filter + all account table JOINs in one round trip,
    then exact point-in-polygon filtering in Python.
    Returns merged parcel rows along with the exempt account-number set.
    """
    min_lat, min_lng, max_lat, max_lng = polygon_bbox(polygon)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (p.account_num)
                    p.account_num, p.parcel_key, p.gis_parcel_id,
                    p.owner_name, p.owner_address, p.owner_city, p.owner_state, p.owner_zip,
                    p.street_num, p.full_street_name, p.property_address, p.property_city, p.property_zip,
                    p.division_cd, p.nbhd_cd,
                    p.legal1, p.legal2, p.legal3, p.legal4, p.legal5,
                    p.polygon_geojson,
                    ST_AsGeoJSON(p.centroid)::json AS centroid,
                    CASE
                      WHEN p.polygon_geojson IS NOT NULL
                          AND (p.polygon_geojson::json)->>'type' IN ('Polygon', 'MultiPolygon')
                      THEN ST_Area(ST_OrientedEnvelope(ST_SetSRID(ST_GeomFromGeoJSON(p.polygon_geojson::text), 4326))::geography)
                          / NULLIF(ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(p.polygon_geojson::text), 4326)::geography), 0)
                      ELSE NULL
                    END AS envelope_ratio,
                    CASE
                      WHEN p.polygon_geojson IS NOT NULL
                          AND (p.polygon_geojson::json)->>'type' IN ('Polygon', 'MultiPolygon')
                      THEN ST_Perimeter(ST_OrientedEnvelope(ST_SetSRID(ST_GeomFromGeoJSON(p.polygon_geojson::text), 4326))::geography) * 3.28084
                      ELSE NULL
                    END AS envelope_perim_ft,
                    CASE
                      WHEN p.polygon_geojson IS NOT NULL
                          AND (p.polygon_geojson::json)->>'type' IN ('Polygon', 'MultiPolygon')
                      THEN ST_Area(ST_OrientedEnvelope(ST_SetSRID(ST_GeomFromGeoJSON(p.polygon_geojson::text), 4326))::geography) * 10.763910416709722
                      ELSE NULL
                    END AS envelope_area_sqft,
                    COALESCE(a.sptd_code, p.sptd_code) AS sptd_code,
                    a.land_val, a.impr_val, a.tot_val, a.isd_desc,
                    r.yr_built, r.tot_living_area, r.tot_main_sf,
                    l.zoning, l.front_dim, l.depth_dim, l.area_size, l.area_uom, l.area_estimated,
                    (e.account_num IS NOT NULL) AS is_exempt
                FROM parcels p
                LEFT JOIN appraisal a ON a.account_num = p.account_num
                LEFT JOIN res_detail r ON r.account_num = p.account_num
                LEFT JOIN land_detail l ON l.account_num = p.account_num
                LEFT JOIN exempt_accounts e ON e.account_num = p.account_num
                WHERE p.division_cd IN ('RES', 'COM')
                    AND ST_Intersects(p.centroid, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
                """,
                (min_lng, min_lat, max_lng, max_lat),
            )
            cols = [desc[0] for desc in cur.description]
            candidate_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        release_conn(conn)

    exact_rows: list[dict[str, Any]] = []
    exempt_accounts: set[str] = set()
    for row in candidate_rows:
        lat, lng = _extract_centroid(row.get("centroid"))
        if lat is None or lng is None:
            continue
        if not point_in_polygon(lat, lng, polygon):
            continue
        row["lat"] = lat
        row["lng"] = lng
        if row.get("is_exempt"):
            exempt_accounts.add(_clean_text(row.get("account_num")))
        exact_rows.append(row)

    merged_rows: list[dict[str, Any]] = []
    for row in exact_rows:
        account_num = _clean_text(row.get("account_num"))
        sptd_code = _clean_text(row.get("sptd_code"))
        land_val = _safe_float(row.get("land_val"))
        tot_val = _safe_float(row.get("tot_val"))
        area_size = _safe_float(row.get("area_size"))
        front_dim = _safe_float(row.get("front_dim"))
        depth_dim = _safe_float(row.get("depth_dim"))
        dims_estimated = False
        if front_dim in (None, 0.0) or depth_dim in (None, 0.0):
            est_front, est_depth = _estimate_front_depth(row)
            if est_front is not None and est_depth is not None:
                front_dim = est_front
                depth_dim = est_depth
            dims_estimated = True
        # DCAD flat-price lots store AREA_SIZE=0 but carry frontage+depth.
        # Rows from updated DBs have area_estimated stored; older rows fall back to runtime derivation.
        area_estimated = bool(row.get("area_estimated"))
        if (area_size is None or area_size <= 0) and front_dim and front_dim > 0 and depth_dim and depth_dim > 0:
            area_size = front_dim * depth_dim
            area_estimated = True

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
                "subdivision": _dcad_subdivision_from_legal(row.get("legal1")),
                "legal1": _clean_text(row.get("legal1")),
                "legal2": _clean_text(row.get("legal2")),
                "legal3": _clean_text(row.get("legal3")),
                "legal4": _clean_text(row.get("legal4")),
                "legal5": _clean_text(row.get("legal5")),
                "lat": _safe_float(row.get("lat")),
                "lng": _safe_float(row.get("lng")),
                "polygon_geojson": row.get("polygon_geojson"),
                "land_val": land_val,
                "impr_val": _safe_float(row.get("impr_val")),
                "tot_val": tot_val,
                "isd_desc": _clean_text(row.get("isd_desc")),
                "yr_built": _safe_int(row.get("yr_built")),
                "tot_living_area": _safe_float(row.get("tot_living_area")),
                "tot_main_sf": _safe_float(row.get("tot_main_sf")),
                "zoning": _clean_text(row.get("zoning")),
                "front_dim": front_dim,
                "depth_dim": depth_dim,
                "dims_estimated": dims_estimated,
                "area_size": area_size,
                "area_uom": _clean_text(row.get("area_uom")),
                "area_estimated": area_estimated,
                "state_code": SPTD_LABELS.get(sptd_code, sptd_code),
                "land_pct": round((land_val / tot_val) * 100, 1) if land_val is not None and tot_val not in (None, 0) else None,
                "hoa_name": "",
                "hoa_url": "",
            }
        )

    hoa_lookup = _fetch_hoa_lookup(merged_rows)
    for row in merged_rows:
        hoa = hoa_lookup.get(row["account_num"])
        if hoa:
            row["hoa_name"] = hoa["hoa_name"]
            row["hoa_url"] = hoa["hoa_url"]

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

    if account_num in exempt_set or sptd in {"X11", "D10"} or non_target_owner or is_nominal:
        return "exempt"
    if sptd in {"B11", "B12", "A14", "A13"}:
        return "multifamily"
    if sptd == "C11":
        return "vacant"
    if sptd in {"C12", "C13", "F10", "F20"}:
        return "commercial"
    return "single_family"


def build_feature(row: dict[str, Any], prop_type: str, on_redfin: bool, redfin_listing: dict | None = None) -> dict[str, Any]:
    """Build a GeoJSON feature + frontend popup payload for one parcel row."""
    lat = _safe_float(row.get("lat"))
    lng = _safe_float(row.get("lng"))
    if lat is None or lng is None:
        raise ValueError("Parcel row is missing centroid coordinates")

    area_size = _safe_float(row.get("area_size"))
    land_pct = _safe_float(row.get("land_pct"))
    area_estimated = bool(row.get("area_estimated"))
    est_tag = " (est.)" if area_estimated else ""
    dims_est_tag = " (est.)" if bool(row.get("dims_estimated")) else ""
    # D10 (Qualified Agricultural Land) carries $0 in DCAD bulk exports — market
    # value is withheld; display a label rather than misleading "$0".
    ag_zero = (
        _clean_text(row.get("sptd_code")) == "D10"
        and (_safe_float(row.get("land_val")) or 0) == 0
    )

    props = {
        "on_redfin": bool(on_redfin),
        "prop_type": prop_type,
        "account_num": _clean_text(row.get("account_num")),
        "addr": _clean_text(row.get("property_address")),
        "city": _clean_text(row.get("property_city")),
        "owner": _clean_text(row.get("owner_name")),
        "land_val": "Ag-exempt" if ag_zero else (f"${row['land_val']:,.0f}" if _safe_float(row.get("land_val")) is not None else "N/A"),
        "tot_val": "Ag-exempt" if ag_zero else (f"${row['tot_val']:,.0f}" if _safe_float(row.get("tot_val")) is not None else "N/A"),
        "total_value_source": _clean_text(row.get("total_value_source")),  # "" when not set, or "prior_year_cert_YYYY"
        "land_pct": f"{land_pct:.1f}%" if land_pct is not None else "N/A",
        "lot_sqft": f"{int(area_size):,} sf{est_tag}" if area_size is not None and area_size > 0 else "N/A",
        "lot_acres": f"{area_size / 43560:.2f} ac{est_tag}" if area_size is not None and area_size > 0 else "N/A",
        "frontage": f"{int(row['front_dim'])} ft{dims_est_tag}" if _safe_float(row.get("front_dim")) not in (None, 0.0) else "N/A",
        "depth": f"{int(row['depth_dim'])} ft{dims_est_tag}" if _safe_float(row.get("depth_dim")) not in (None, 0.0) else "N/A",
        "state_code": _clean_text(row.get("state_code")) or "N/A",
        "zoning": _clean_text(row.get("zoning")) or "N/A",
        "school": _clean_text(row.get("isd_desc")) or "N/A",
        "subdivision": _clean_text(row.get("subdivision")) or _dcad_subdivision_from_legal(row.get("legal1")),
        "yr_built": str(row.get("yr_built")) if row.get("yr_built") else "N/A",
        "sqft": f"{int(float(row['tot_living_area'])):,}" if _safe_float(row.get("tot_living_area")) not in (None, 0.0) else "N/A",
        "verified_vacant": _clean_text(row.get("verified_vacant")),
        "potential_target": _clean_text(row.get("potential_target")),
        "redfin_price": f"${redfin_listing['price']:,}" if redfin_listing and redfin_listing.get("price") else None,
        "redfin_url": redfin_listing.get("url") if redfin_listing else None,
        "lat": lat,
        "lng": lng,
    }

    geometry = row.get("polygon_geojson")
    if isinstance(geometry, dict) and geometry.get("type") in {"Polygon", "MultiPolygon"}:
        feature_geometry = geometry
    else:
        feature_geometry = {"type": "Point", "coordinates": [lng, lat]}

    return {
        "type": "Feature",
        "geometry": feature_geometry,
        "properties": props,
    }
