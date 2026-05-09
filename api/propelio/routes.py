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
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from . import cache as cache_mod
from . import scraper as scraper_mod


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/propelio", tags=["propelio"])


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


@router.get("/by-address")
async def get_by_address(
    address: str = Query(..., min_length=3),
    months: int = Query(6, ge=1, le=60),
    range: float = Query(0.5, gt=0.0, le=10.0),
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
        return {"cached": True, **cached}

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

    subject_dict = asdict(subject)
    subject_extra = subject_dict.pop("extra", {}) if isinstance(subject_dict.get("extra"), dict) else {}

    # Subject's top-level fields can be sparse when the CMA didn't carry them
    # directly. Parcel-detail enrichment usually has them — fall back so the
    # frontend doesn't have to drill into nested objects for routine display.
    parcel_enrichment = subject_extra.get("parcel_enrichment") or {}
    valuation = subject_extra.get("valuation") or {}

    def _first(*vals: Any) -> Any:
        for v in vals:
            if v not in (None, "", 0):
                return v
        return None

    payload: dict[str, Any] = {
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

    cache_mod.log_quota(payload.get("balance"), address_key)
    cache_mod.put_cached(address_key, payload)
    return {"cached": False, **payload}
