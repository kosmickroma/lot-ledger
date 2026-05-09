# api/propelio/routes.py
#
# FastAPI router for Propelio per-address comp fetches.
# Handles cache lookup/write, quota logging, and response normalization.
#
# Connects to:
#   api/propelio/scraper.py  - synchronous Propelio client call
#   api/propelio/cache.py    - address cache and quota log helpers

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.config import get_conn, get_session_conn, release_conn, release_session_conn
from api.geo import haversine_miles, point_in_polygon, polygon_centroid
from api.redfin import normalize_addr_key

from .archive import load_archived_comps, merge_comps_into_archive
from .parcel_match import match_comps_to_parcels
from . import cache as cache_mod
from . import scraper as scraper_mod


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/propelio", tags=["propelio"])


class PolygonRequest(BaseModel):
    polygon: list[list[float]]
    months: int = 24
    range_override_mi: float | None = None
    saved_area_id: str | None = None


class RefreshRequest(BaseModel):
    saved_area_id: str
    months: int = 24
    range_override_mi: float | None = None


def _extract_balance(subject_extra: dict[str, Any]) -> int | None:
    if not isinstance(subject_extra, dict):
        return None

    def _as_int(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    search_roots = [
        subject_extra,
        subject_extra.get("valuation") if isinstance(subject_extra.get("valuation"), dict) else None,
        subject_extra.get("raw") if isinstance(subject_extra.get("raw"), dict) else None,
        subject_extra.get("withaddress") if isinstance(subject_extra.get("withaddress"), dict) else None,
    ]

    for root in search_roots:
        if not isinstance(root, dict):
            continue
        for key in ("balance", "remaining", "remainingConsumables", "consumablesRemaining"):
            got = _as_int(root.get(key))
            if got is not None:
                return got
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _first(*vals: Any) -> Any:
    for v in vals:
        if v not in (None, "", 0):
            return v
    return None


def _build_payload(subject: Any, comps_list: list[Any]) -> dict[str, Any]:
    subject_dict = asdict(subject)
    subject_extra = subject_dict.pop("extra", {}) if isinstance(subject_dict.get("extra"), dict) else {}
    parcel_enrichment = subject_extra.get("parcel_enrichment") or {}
    valuation = subject_extra.get("valuation") or {}
    return {
        "fetched_at": _now_iso(),
        "balance": _extract_balance(subject_extra),
        "cma_settings": {
            "params": subject_extra.get("cma_params"),
            "arv": subject_extra.get("cma_arv"),
            "arv_type": subject_extra.get("cma_arvType"),
            "as_of_dt": subject_extra.get("cma_as_of_dt"),
            "start_dt": subject_extra.get("cma_start_dt"),
            "sales_count": subject_extra.get("cma_sales_count"),
            "leases_count": subject_extra.get("cma_leases_count"),
            "cma_id": subject_extra.get("cma_id"),
        },
        "subject": {
            "address": subject_dict.get("address"),
            "lot_size": _first(subject_dict.get("lot_size"), parcel_enrichment.get("lot_size")),
            "sqft": _first(subject_dict.get("sqft"), parcel_enrichment.get("sqft")),
            "year_built": _first(subject_dict.get("year_built"), parcel_enrichment.get("year_built")),
            "neighborhood": _first(subject_dict.get("neighborhood"), parcel_enrichment.get("subdivision")),
            "lat": _first(subject_extra.get("lat"), parcel_enrichment.get("lat")),
            "lon": _first(subject_extra.get("lon"), parcel_enrichment.get("lon")),
            "parcel_enrichment": parcel_enrichment or None,
            "valuation": valuation or None,
            "transfer_history": subject_extra.get("transfer_history") or subject_extra.get("transfers"),
            "raw": subject_extra.get("raw"),
        },
        "comps": [asdict(comp) for comp in comps_list],
    }


def _validate_polygon(points: list[list[float]]) -> list[list[float]]:
    if len(points) < 3:
        raise HTTPException(status_code=400, detail="Polygon must contain at least three points")

    normalized: list[list[float]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise HTTPException(status_code=400, detail="Polygon points must be [lng, lat] pairs")
        lng = float(point[0])
        lat = float(point[1])
        if not -180.0 <= lng <= 180.0:
            raise HTTPException(status_code=400, detail="Polygon lng must be between -180 and 180")
        if not -90.0 <= lat <= 90.0:
            raise HTTPException(status_code=400, detail="Polygon lat must be between -90 and 90")
        normalized.append([lng, lat])
    return normalized


def _polygon_cache_key(polygon: list[list[float]], months: int, range_mi: float) -> str:
    canonical = json.dumps({"v": 2, "polygon": polygon, "months": months, "range_mi": round(float(range_mi), 6)}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _nearest_subject_parcel(lat: float, lng: float) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH point AS (
                    SELECT ST_SetSRID(ST_Point(%s, %s), 4326) AS geom
                ),
                candidates AS (
                    SELECT
                        'dcad'::text AS county,
                        p.account_num,
                        p.property_address AS address,
                        ST_Distance(p.centroid::geography, point.geom::geography) AS distance_m
                    FROM parcels p, point
                    WHERE p.centroid IS NOT NULL
                      AND p.property_address IS NOT NULL
                      AND p.property_address <> ''

                    UNION ALL

                    SELECT
                        'tad'::text AS county,
                        t.account_num,
                        t.situs_addr AS address,
                        ST_Distance(t.centroid::geography, point.geom::geography) AS distance_m
                    FROM tad_parcels t, point
                    WHERE t.centroid IS NOT NULL
                      AND t.situs_addr IS NOT NULL
                      AND t.situs_addr <> ''

                    UNION ALL

                    SELECT
                        'collin'::text AS county,
                        c.account_num,
                        c.property_address AS address,
                        ST_Distance(c.centroid::geography, point.geom::geography) AS distance_m
                    FROM collin_parcels c, point
                    WHERE c.centroid IS NOT NULL
                      AND c.property_address IS NOT NULL
                      AND c.property_address <> ''

                    UNION ALL

                    SELECT
                        'denton'::text AS county,
                        d.account_num,
                        d.property_address AS address,
                        ST_Distance(d.centroid::geography, point.geom::geography) AS distance_m
                    FROM denton_parcels d, point
                    WHERE d.centroid IS NOT NULL
                      AND d.property_address IS NOT NULL
                      AND d.property_address <> ''
                )
                SELECT county, account_num, address, distance_m
                FROM candidates
                ORDER BY distance_m ASC
                LIMIT 1
                """,
                (lng, lat),
            )
            row = cur.fetchone()
            if row is None:
                return None
            distance_m = float(row[3] or 0.0)
            if distance_m > 1609.344:
                return None
            return {
                "county": str(row[0]),
                "account_num": str(row[1]),
                "address": str(row[2]),
                "distance_m": distance_m,
                "address_key": normalize_addr_key(row[2]),
            }
    finally:
        release_conn(conn)


def _comp_point(comp: Any) -> tuple[float | None, float | None]:
    extra = comp.extra if isinstance(getattr(comp, "extra", None), dict) else {}
    lat_raw = extra.get("lat")
    lng_raw = extra.get("lon", extra.get("lng"))
    try:
        lat = float(lat_raw)
        lng = float(lng_raw)
    except (TypeError, ValueError):
        return None, None
    return lat, lng


def _load_saved_area_polygon(saved_area_id: str) -> list[list[float]]:
    normalized = str(saved_area_id or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="saved_area_id is required")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT polygon
                FROM saved_areas
                WHERE area_id = %s
                LIMIT 1
                """,
                (normalized,),
            )
            row = cur.fetchone()
    finally:
        release_session_conn(conn)

    if row is None:
        raise HTTPException(status_code=404, detail="saved_area_id not found")

    polygon = row[0]
    if not isinstance(polygon, list):
        raise HTTPException(status_code=422, detail="Saved area polygon is invalid")
    return _validate_polygon(polygon)


async def _run_by_polygon(request: PolygonRequest, *, use_cache: bool) -> dict[str, Any]:
    polygon = _validate_polygon(request.polygon)
    months = int(request.months)
    saved_area_id = str(request.saved_area_id or "").strip() or None
    centroid_lat, centroid_lng = polygon_centroid(polygon)
    circumradius_mi = max(
        haversine_miles(centroid_lat, centroid_lng, point_lat, point_lng)
        for point_lng, point_lat in polygon
    )
    computed_range_mi = min(circumradius_mi * 1.05, 10.0)
    if request.range_override_mi is None:
        range_mi = computed_range_mi
    else:
        range_mi = min(max(float(request.range_override_mi), 0.01), 10.0)

    cache_key = _polygon_cache_key(polygon, months, range_mi)
    if use_cache:
        cached = cache_mod.get_cached(cache_key)
        if cached is not None:
            cached_payload = dict(cached)
            cached_payload["comps"] = match_comps_to_parcels(cached_payload.get("comps") or [])
            polygon_meta = cached_payload.get("polygon_meta")
            if isinstance(polygon_meta, dict):
                comps_pulled = polygon_meta.get("comps_pulled")
                if not isinstance(comps_pulled, int):
                    comps_pulled = len(cached_payload.get("comps") or [])
                comps_in_polygon = polygon_meta.get("comps_in_polygon")
                if not isinstance(comps_in_polygon, int):
                    comps_in_polygon = 0
                polygon_meta["comps_outside_polygon"] = max(comps_pulled - comps_in_polygon, 0)
            if saved_area_id:
                cached_payload["archive_meta"] = merge_comps_into_archive(saved_area_id, cached_payload.get("comps") or [])
            return {"cached": True, **cached_payload}

    subject_parcel = _nearest_subject_parcel(centroid_lat, centroid_lng)
    if subject_parcel is None:
        raise HTTPException(status_code=404, detail="No parcel found near polygon centroid")

    try:
        subject, comps_list = await asyncio.to_thread(
            scraper_mod.search_properties,
            subject_parcel["address"],
            months=months,
            range_mi=range_mi,
        )
    except scraper_mod.PropelioScraperError as exc:
        msg = str(exc)
        if "empty list" in msg.lower():
            empty_payload = {
                "fetched_at": _now_iso(),
                "balance": None,
                "cma_settings": {
                    "params": None,
                    "arv": None,
                    "arv_type": None,
                    "as_of_dt": None,
                    "start_dt": None,
                    "sales_count": 0,
                    "leases_count": None,
                    "cma_id": None,
                },
                "subject": {
                    "address": subject_parcel["address"],
                    "lot_size": None,
                    "sqft": None,
                    "year_built": None,
                    "neighborhood": None,
                    "lat": None,
                    "lon": None,
                    "parcel_enrichment": None,
                    "valuation": None,
                    "transfer_history": None,
                    "raw": None,
                },
                "comps": [],
                "polygon_meta": {
                    "centroid": {"lat": centroid_lat, "lng": centroid_lng},
                    "circumradius_mi": circumradius_mi,
                    "subject_parcel": {
                        "address": subject_parcel["address"],
                        "county": subject_parcel["county"],
                        "account_num": subject_parcel["account_num"],
                    },
                    "comps_pulled": 0,
                    "comps_in_polygon": 0,
                    "comps_outside_polygon": 0,
                },
                "warning": "Propelio resolved the centroid parcel but returned 0 comps for the polygon pull under the current filter settings.",
            }
            if saved_area_id:
                empty_payload["archive_meta"] = merge_comps_into_archive(saved_area_id, [])
            return {"cached": False, **empty_payload}
        return JSONResponse(
            status_code=503,
            content={"detail": "Propelio service unavailable", "error": msg},
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Address not resolvable on Propelio") from exc
    except Exception as exc:
        logger.exception("Unexpected Propelio by-polygon error")
        return JSONResponse(
            status_code=500,
            content={"detail": "Unexpected error", "error": str(exc)},
        )

    comps_in_polygon = 0
    for comp in comps_list:
        lat, lng = _comp_point(comp)
        if lat is None or lng is None:
            continue
        if point_in_polygon(lat, lng, polygon):
            comps_in_polygon += 1

    payload = _build_payload(subject, comps_list)
    payload["comps"] = match_comps_to_parcels(payload.get("comps") or [])
    comps_pulled = len(comps_list)
    payload["polygon_meta"] = {
        "centroid": {"lat": centroid_lat, "lng": centroid_lng},
        "circumradius_mi": circumradius_mi,
        "subject_parcel": {
            "address": subject_parcel["address"],
            "county": subject_parcel["county"],
            "account_num": subject_parcel["account_num"],
        },
        "comps_pulled": comps_pulled,
        "comps_in_polygon": comps_in_polygon,
        "comps_outside_polygon": max(comps_pulled - comps_in_polygon, 0),
    }

    if saved_area_id:
        payload["archive_meta"] = merge_comps_into_archive(saved_area_id, payload.get("comps") or [])

    if use_cache:
        cache_payload = dict(payload)
        cache_payload.pop("archive_meta", None)
        cache_mod.log_quota(payload.get("balance"), cache_key)
        cache_mod.put_cached(cache_key, cache_payload)
    return {"cached": False, **payload}


@router.get("/by-address")
async def get_by_address(
    address: str = Query(..., min_length=3),
    months: int = Query(24, ge=1, le=60),
    range: float = Query(1.0, gt=0.0, le=10.0),
) -> dict[str, Any]:
    address_key_base = cache_mod.normalize_address_key(address)
    if not address_key_base:
        raise HTTPException(status_code=400, detail="Address is required")

    # Cache key includes filter params so different settings don't collide.
    # Only fresh CMA generations honor these params — existing CMAs return
    # cached data — but cache key still distinguishes for clarity.
    address_key = f"{address_key_base}|m{months}|r{range}"

    cached = cache_mod.get_cached(address_key)
    if cached is not None:
        cached_payload = dict(cached)
        cached_payload["comps"] = match_comps_to_parcels(cached_payload.get("comps") or [])
        return {"cached": True, **cached_payload}

    try:
        subject, comps_list = await asyncio.to_thread(
            scraper_mod.search_properties,
            address,
            months=months,
            range_mi=range,
        )
    except scraper_mod.PropelioScraperError as exc:
        msg = str(exc)
        # Empty CMA / lead-details lists are not real failures — they mean
        # Propelio resolved the address but has no nearby comps in the time
        # window. Return 200 with empty comps so the frontend can show
        # "no comps for this address" rather than an error toast.
        if "empty list" in msg.lower():
            logger.info("Propelio returned empty pool for %r — returning 200 with no comps", address)
            empty_payload = {
                "fetched_at": _now_iso(),
                "balance": None,
                "cma_settings": {
                    "params": None, "arv": None, "arv_type": None,
                    "as_of_dt": None, "start_dt": None,
                    "sales_count": 0, "leases_count": None, "cma_id": None,
                },
                "subject": {"address": address, "neighborhood": None, "lot_size": None,
                            "sqft": None, "year_built": None, "lat": None, "lon": None,
                            "parcel_enrichment": None, "valuation": None,
                            "transfer_history": None, "raw": None},
                "comps": [],
                "warning": "Propelio resolved this address but returned 0 comps under their default filter (typically 0.5mi · 6mo · similar lot size). Try a higher-activity neighborhood, or wait for the filter-widen feature.",
            }
            return {"cached": False, **empty_payload}
        return JSONResponse(
            status_code=503,
            content={"detail": "Propelio service unavailable", "error": msg},
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Address not resolvable on Propelio") from exc
    except Exception as exc:
        logger.exception("Unexpected Propelio by-address error")
        return JSONResponse(
            status_code=500,
            content={"detail": "Unexpected error", "error": str(exc)},
        )

    payload = _build_payload(subject, comps_list)
    payload["comps"] = match_comps_to_parcels(payload.get("comps") or [])

    cache_mod.log_quota(payload.get("balance"), address_key)
    cache_mod.put_cached(address_key, payload)
    return {"cached": False, **payload}


@router.post("/by-polygon")
async def get_by_polygon(
    request: PolygonRequest,
    saved_area_id: str | None = Query(None),
) -> dict[str, Any]:
    body_saved_area_id = str(request.saved_area_id or "").strip() or None
    query_saved_area_id = str(saved_area_id or "").strip() or None
    effective_saved_area_id = query_saved_area_id or body_saved_area_id

    effective_request = PolygonRequest(
        polygon=request.polygon,
        months=request.months,
        range_override_mi=request.range_override_mi,
        saved_area_id=effective_saved_area_id,
    )
    return await _run_by_polygon(effective_request, use_cache=True)


@router.post("/refresh")
async def refresh_by_saved_area(request: RefreshRequest) -> dict[str, Any]:
    saved_area_id = str(request.saved_area_id or "").strip()
    if not saved_area_id:
        raise HTTPException(status_code=400, detail="saved_area_id is required")

    polygon = _load_saved_area_polygon(saved_area_id)
    by_poly_request = PolygonRequest(
        polygon=polygon,
        months=int(request.months),
        range_override_mi=request.range_override_mi,
        saved_area_id=saved_area_id,
    )
    response = await _run_by_polygon(by_poly_request, use_cache=False)
    response["comps"] = load_archived_comps(saved_area_id)
    return response


@router.get("/by-saved-area")
async def get_by_saved_area(saved_area_id: str = Query(..., min_length=1)) -> dict[str, Any]:
    normalized = str(saved_area_id or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="saved_area_id is required")
    return {"comps": load_archived_comps(normalized)}
