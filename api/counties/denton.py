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


# Duplexes (2026-06-01): Denton uses fewer PTAD B-codes than Collin —
# only B1 (Multi-family, 5+ unit) and B2 (Duplex). No B3/B4 codes exist
# in Denton's data; the district apparently lumps any 3-4 unit residential
# into B2. `OB2` ("Other B2", 8 parcels) currently falls through to
# single_family and is brought into the duplex bucket here as the
# symmetric move with DCAD's defensive A14 inclusion. Denton has NO
# `units` column in `denton_parcels`, so the rule is entirely
# code-driven — no fall-back tie-breaker is possible.
_DENTON_DUPLEX_CODES = {"B2", "OB2"}
_DENTON_MF_ONLY_CODES = {"B1"}
# Documentation superset — the full "what Denton classifies as B-class
# multifamily" catalog. Not consulted by `_classify_denton` directly
# (it splits via _DENTON_DUPLEX_CODES + _DENTON_MF_ONLY_CODES); kept so a
# future PTAD code addition prompts an explicit bucket decision.
_DENTON_MF_CODES = _DENTON_DUPLEX_CODES | _DENTON_MF_ONLY_CODES
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
                    ST_Area(ST_OrientedEnvelope(p_outer.geom)::geography) / NULLIF(ST_Area(p_outer.geom::geography), 0) AS envelope_ratio,
                    ST_Perimeter(ST_OrientedEnvelope(p_outer.geom)::geography) * 3.28084 AS envelope_perim_ft,
                    ST_Area(ST_OrientedEnvelope(p_outer.geom)::geography) * 10.763910416709722 AS envelope_area_sqft,
                    ST_Area(p_outer.geom::geography) * 10.763910416709722 AS geom_sqft,
                    ST_AsGeoJSON(p_outer.geom)::json AS polygon_geojson,
                    ST_AsGeoJSON(p_outer.centroid)::json AS centroid_json,
                    -- v3 residential detail expansion (Phase 3, 2026-05-21):
                    -- LEFT JOIN denton_improvement_detail provides canonical
                    -- residential keys (foundation, roof, beds, etc.) sourced
                    -- from Denton's 2025 certified data extract. See
                    -- docs/DENTON_IMPROVEMENT_DETAIL_EXPANSION_SPEC.md v3.
                    -- Frontend popup + Subject Property card already consume
                    -- these via row.get('foundation_type') etc. (Phase 1).
                    d.foundation_type    AS foundation_type,
                    d.roof_material      AS roof_material,
                    d.roof_type          AS roof_type,
                    d.ext_wall           AS ext_wall,
                    d.heating_type       AS heating_type,
                    d.ac_type            AS ac_type,
                    d.beds               AS beds,
                    d.fireplaces         AS fireplaces,
                    d.cdu_rating         AS cdu_rating,
                    d.bldg_class         AS bldg_class,
                    d.sprinkler_flag     AS sprinkler_flag,
                    d.plumbing_count     AS plumbing_count,
                    d.interior_finish    AS interior_finish,
                    d.flooring           AS flooring,
                    d.eff_yr_built       AS eff_yr_built,
                    d.main_area_sqft     AS denton_main_area_sqft,
                    d.dropped_imprv_count AS dropped_imprv_count,
                    d.raw_heating_cooling_code AS raw_heating_cooling_code,
                    -- Feature-derived columns from sub-area details (Phase 3 patch 2026-05-21)
                    d.pool_flag          AS pool_flag,
                    d.deck_flag          AS deck_flag,
                    d.garage_capacity    AS garage_capacity,
                    d.stories            AS stories,
                    -- Phase 3 patch v3 (Plumbing-as-baths reinterpretation + rooms + outdoor FP + end unit)
                    d.baths              AS baths,
                    d.full_baths         AS full_baths,
                    d.half_baths         AS half_baths,
                    d.total_rooms        AS total_rooms,
                    d.outdoor_fireplaces AS outdoor_fireplaces,
                    d.end_unit           AS end_unit,
                    pon.contact_info_retrieved AS outreach_contact_info_retrieved,
                    pon.mailer_date            AS outreach_mailer_date
                FROM denton_parcels p_outer
                LEFT JOIN LATERAL (
                    -- Single-row pick per parcel — prop_id is the canonical
                    -- digits-only key (matches denton_parcels.account_num).
                    -- LATERAL ensures we never multiply rows even if any
                    -- future schema change introduces multiple detail rows
                    -- per parcel (defensive — today PK constraint guarantees
                    -- one row).
                    SELECT *
                    FROM denton_improvement_detail
                    WHERE prop_id = p_outer.account_num
                    LIMIT 1
                ) d ON TRUE
                LEFT JOIN parcel_outreach_notes pon
                       ON pon.county = 'denton' AND pon.parcel_id = p_outer.parcel_key
                WHERE ST_Intersects(p_outer.centroid, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
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
    """Classify a Denton parcel.

    Reads from the NORMALIZED row produced by `_normalize_denton_row` —
    `row.get("sptd_code")` is the aliased form of the raw column
    `state_cd`, which may carry a comma-joined multi-code value
    (e.g. `'B2,A1'`). `_primary_code` splits on the first `,` and
    upper-cases. If a future call site passes the raw SQL row, every
    classification will silently fall through to "single_family".

    Duplexes (2026-06-01): PTAD code B2 (Duplex) and the variant OB2
    route to the duplexes bucket. B1 (Multi-family, 5+ apartments)
    stays multifamily. Denton has no `units` column, so the rule is
    entirely code-driven — there's no tie-breaker available.

    Multi-code first-wins precedence is intentional and consistent with
    every other code-check branch in this function: `'B2,A1'` → primary
    is B2 → duplexes; `'A1,B2'` → primary is A1 → single_family. The
    district lists the dominant improvement type first.
    """
    code = _primary_code(_clean_text(row.get("sptd_code")))

    if code in _DENTON_EXEMPT_CODES:
        return "exempt"

    owner_up = _clean_text(row.get("owner_name")).upper()
    gov_match = any(kw in owner_up for kw in _GOV_KEYWORDS)
    hoa_match = any(kw in owner_up for kw in _HOA_KEYWORDS)
    non_target_owner = gov_match or (hoa_match and code not in {"A1", "A2", "A3", "A4", "A5", "A6", "A9"})
    if non_target_owner:
        return "exempt"

    if code in _DENTON_DUPLEX_CODES:
        return "duplexes"
    if code in _DENTON_MF_ONLY_CODES:
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
        # Display as zip5 — Denton raw stores "5-4" format (e.g. "75077-1833").
        "property_zip": _clean_text(raw.get("property_zip"))[:5],
        # outreach v2 (2026-06-03) — None when no row.
        "outreach_contact_info_retrieved": raw.get("outreach_contact_info_retrieved"),
        "outreach_mailer_date": raw.get("outreach_mailer_date"),
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
        # === Phase 3 residential detail expansion (2026-05-21) ===
        # Aliased from denton_improvement_detail via LEFT JOIN in
        # _denton_bbox_filter SELECT. Must explicitly pass through here
        # because this dict-builder enumerates output keys (same pattern
        # that caused the DCAD merged_rows leak in Phase 1 — fixed in
        # commit 8684b96). Carry the canonical keys per the contract in
        # docs/CAD_RESIDENTIAL_DETAIL_EXPANSION_SPEC.md.
        "foundation_type": _clean_text(raw.get("foundation_type")),
        "roof_material": _clean_text(raw.get("roof_material")),
        "roof_type": _clean_text(raw.get("roof_type")),
        "ext_wall": _clean_text(raw.get("ext_wall")),
        "heating_type": _clean_text(raw.get("heating_type")),
        "ac_type": _clean_text(raw.get("ac_type")),
        "beds": _safe_int(raw.get("beds")),
        "fireplaces": _safe_int(raw.get("fireplaces")),
        "cdu_rating": _clean_text(raw.get("cdu_rating")),
        "bldg_class": _clean_text(raw.get("bldg_class")),
        "sprinkler_flag": _clean_text(raw.get("sprinkler_flag")),
        "plumbing_count": _safe_int(raw.get("plumbing_count")),
        "interior_finish": _clean_text(raw.get("interior_finish")),
        "flooring": _clean_text(raw.get("flooring")),
        "eff_yr_built": _safe_int(raw.get("eff_yr_built")),
        "dropped_imprv_count": _safe_int(raw.get("dropped_imprv_count")),
        # Raw code preservation for debug + future re-decoding
        "raw_heating_cooling_code": _clean_text(raw.get("raw_heating_cooling_code")),
        # Feature-derived (Phase 3 patch — sub-area detail rows, 2026-05-21).
        # Overrides any attr-table value since Denton tracks pool/deck/garage
        # as separate IMPROVEMENT_DETAIL rows, not as attributes.
        "pool_flag": _clean_text(raw.get("pool_flag")),
        "deck_flag": _clean_text(raw.get("deck_flag")),
        "garage_capacity": _safe_int(raw.get("garage_capacity")),
        "stories": _safe_int(raw.get("stories")),
        # Phase 3 patch v3 — Plumbing-as-baths reinterpretation + new canonical fields.
        # `baths` is a decimal (e.g. 2.5 = 2 full + 1 half) from Denton's Plumbing
        # attribute. full_baths + half_baths split for compatibility with DCAD shape.
        "baths": _safe_float(raw.get("baths")),
        "full_baths": _safe_int(raw.get("full_baths")),
        "half_baths": _safe_int(raw.get("half_baths")),
        "total_rooms": _safe_int(raw.get("total_rooms")),
        "outdoor_fireplaces": _safe_int(raw.get("outdoor_fireplaces")),
        "end_unit": _clean_text(raw.get("end_unit")),
        # === end Phase 3 expansion ===
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

    flood_lookup = _fetch_flood_lookup_denton(
        [r["parcel_key"] for r in rows if r.get("parcel_key")]
    )
    for row in rows:
        flood = flood_lookup.get(row.get("parcel_key"))
        if flood:
            row["flood_zone"] = flood["flood_zone"]
            row["flood_zone_subtype"] = flood["flood_zone_subtype"]
            row["flood_bfe"] = flood["flood_bfe"]
        else:
            row["flood_zone"] = ""
            row["flood_zone_subtype"] = ""
            row["flood_bfe"] = None

    return ParcelQueryResult(parcels=rows, exempt_accounts=exempt_set)


def _fetch_flood_lookup_denton(parcel_keys: list[str]) -> dict[str, dict[str, Any]]:
    """Batch spatial join: returns
    {parcel_key: {flood_zone, flood_zone_subtype, flood_bfe}}
    for Denton parcels whose centroid falls inside a FEMA flood polygon.
    Mirrors api/counties/dcad.py:_fetch_flood_lookup_dcad."""
    if not parcel_keys:
        return {}
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (p.parcel_key)
                       p.parcel_key, f.fld_zone, f.zone_subty, f.static_bfe
                FROM denton_parcels p
                JOIN flood_zones f
                     ON f.source_county = 'denton'
                    AND ST_Contains(f.geom, p.centroid)
                WHERE p.parcel_key = ANY(%s)
                ORDER BY p.parcel_key, ST_Area(f.geom) DESC
                """,
                (parcel_keys,),
            )
            return {
                row[0]: {
                    "flood_zone": row[1] or "",
                    "flood_zone_subtype": row[2] or "",
                    "flood_bfe": float(row[3]) if row[3] is not None else None,
                }
                for row in cur.fetchall()
            }
    except Exception as exc:
        logger.warning("Denton flood zone lookup failed: %s", exc)
        return {}
    finally:
        release_conn(conn)


__all__ = ["query_denton_parcels", "_classify_denton", "_normalize_denton_row"]
