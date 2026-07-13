# AI module — POST /api/ai/read-comps (§A.6).
#
# Handler order is load-bearing (spec: "do not reorder"):
#   1. CSRF  2. auth  3. role gate (403)  4. AI_ENABLED guard (404)
#   5. payload validation (400)  6. server-authoritative gate (§A.4)
#   7. cache split  8. truncate to AI_MAX_COMPS  9. extract (502 / partial)
#   10. merge + respond + log
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.ai.config import (
    AI_ENABLED,
    AI_MAX_COMPS,
    AI_MODEL,
    AI_PRICE_IN_PER_M,
    AI_PRICE_OUT_PER_M,
)
from api.ai.extractor import extract
from api.ai.gate import fetch_permitted_comps
from api.ai.rubric import CONDITION_ORDER, PROMPT_VERSION
from api.auth import get_current_user, require_csrf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])


class ReadCompsRequest(BaseModel):
    area_id: str
    comp_keys: list[str]   # comp_address_key values (§0 — the client has these, not comp_id)


# --- Per-process cache (§A.7) -------------------------------------------------
# Keyed (comp_id, PROMPT_VERSION) -> validated per-comp result. Bounded FIFO;
# only ever holds post-quote-check results, never a failure. Per-instance —
# a miss on another Cloud Run instance is correct, not a bug.
_CACHE_MAX = 5000
_cache: "OrderedDict[tuple[int, str], dict[str, Any]]" = OrderedDict()
_cache_lock = threading.Lock()

# --- In-flight de-dup guard (§A.7.1) ------------------------------------------
_inflight: set[tuple[str, int]] = set()
_inflight_lock = threading.Lock()


def _cache_get(comp_id: int) -> dict[str, Any] | None:
    with _cache_lock:
        return _cache.get((comp_id, PROMPT_VERSION))


def _cache_put_many(results: dict[int, dict[str, Any]]) -> None:
    with _cache_lock:
        for comp_id, result in results.items():
            _cache[(comp_id, PROMPT_VERSION)] = result
            if len(_cache) > _CACHE_MAX:
                _cache.popitem(last=False)


@router.post("/read-comps")
async def read_comps(
    payload: ReadCompsRequest,
    req: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    start = time.monotonic()

    require_csrf(req)

    role = str(user.get("role") or "").strip().lower()
    if role not in ("owner", "developer"):
        # Server-side, independent of the frontend gate — VAs never reach the model.
        raise HTTPException(status_code=403, detail="AI features are not enabled for this role")

    if not AI_ENABLED:
        # Belt-and-braces: the router shouldn't even be mounted when this is
        # false, but a 404 here means no information leak if it somehow is.
        raise HTTPException(status_code=404)

    comp_keys = payload.comp_keys or []
    if not comp_keys:
        raise HTTPException(status_code=400, detail="comp_keys must be non-empty")
    if len(comp_keys) > 500:
        raise HTTPException(status_code=400, detail="comp_keys exceeds maximum of 500")

    area_id = payload.area_id
    user_id = int(user["id"])
    inflight_key = (area_id, user_id)

    with _inflight_lock:
        if inflight_key in _inflight:
            raise HTTPException(status_code=409, detail="A read is already in progress for this area")
        _inflight.add(inflight_key)

    try:
        permitted, excluded = fetch_permitted_comps(area_id, comp_keys)

        cached_results: dict[int, dict[str, Any]] = {}
        to_read: list[dict[str, Any]] = []
        for comp in permitted:
            hit = _cache_get(comp["comp_id"])
            if hit is not None:
                cached_results[comp["comp_id"]] = hit
            else:
                to_read.append(comp)

        truncated = False
        if len(to_read) > AI_MAX_COMPS:
            # First AI_MAX_COMPS by price descending (None-priced comps sort last).
            to_read = sorted(
                to_read, key=lambda c: (c["price"] is None, -(c["price"] or 0))
            )[:AI_MAX_COMPS]
            truncated = True

        fresh_results: dict[int, dict[str, Any]] = {}
        usage = {"tokens_in": 0, "tokens_out": 0}
        rejected_quotes = 0
        partial = False

        if to_read:
            try:
                fresh_results, usage, rejected_quotes, partial = extract(to_read)
            except Exception as exc:
                # Zero batches completed -> nothing to show; say so (§A.6 step 9).
                logger.info(
                    "ai.read_comps area_id=%s user=%s role=%s requested=%s permitted=%s "
                    "excluded=%s cached=%s read=0 rejected_quotes=0 truncated=%s partial=false "
                    "prompt_version=%s model=%s tokens_in=0 tokens_out=0 est_cost=0.0 "
                    "duration_ms=%s error_class=%s error_status=%s",
                    area_id, user_id, role, len(comp_keys), len(permitted), len(excluded),
                    len(cached_results), truncated, PROMPT_VERSION, AI_MODEL,
                    int((time.monotonic() - start) * 1000),
                    type(exc).__name__, getattr(exc, "code", "n/a"),
                )
                raise HTTPException(status_code=502, detail="AI read failed") from exc

            if fresh_results:
                # Cache every comp whose batch completed, even if the run overall
                # is partial (§A.7 — a *run* can be partial; a *comp's result* never is).
                _cache_put_many(fresh_results)

        all_results = {**cached_results, **fresh_results}

        comps_out: list[dict[str, Any]] = []
        mix: dict[str, int] = {c: 0 for c in CONDITION_ORDER}
        for comp in permitted:
            result = all_results.get(comp["comp_id"])
            if result is None:
                continue   # truncated away this run, or (should not happen) not yet read
            condition = result.get("condition", "unknown")
            mix[condition] = mix.get(condition, 0) + 1
            comps_out.append({
                "comp_id": comp["comp_id"],
                "comp_address_key": comp["comp_address_key"],
                "address": comp["address"],
                "price": comp["price"],
                "condition": condition,
                "condition_quote": result.get("condition_quote"),
                "flags": result.get("flags", []),
            })

        excluded_out = [
            {"comp_address_key": e["comp_address_key"], "address": e["address"], "reason": e["reason"]}
            for e in excluded
        ]

        est_cost = round(
            usage["tokens_in"] / 1e6 * AI_PRICE_IN_PER_M
            + usage["tokens_out"] / 1e6 * AI_PRICE_OUT_PER_M,
            6,
        )

        meta = {
            "prompt_version": PROMPT_VERSION,
            "model": AI_MODEL,
            "read": len(comps_out),
            # Total gate-passed pool (pre-cache, pre-truncation) — the "M" the
            # frontend needs for both "Read N of M" and "showing first N of M".
            "permitted": len(permitted),
            "excluded": len(excluded_out),
            "cached": len(cached_results),
            "truncated": truncated,
            "partial": partial,
            "rejected_quotes": rejected_quotes,
            "tokens_in": usage["tokens_in"],
            "tokens_out": usage["tokens_out"],
            "est_cost": est_cost,
        }

        logger.info(
            "ai.read_comps area_id=%s user=%s role=%s requested=%s permitted=%s excluded=%s "
            "cached=%s read=%s rejected_quotes=%s truncated=%s partial=%s prompt_version=%s "
            "model=%s tokens_in=%s tokens_out=%s est_cost=%s duration_ms=%s",
            area_id, user_id, role, len(comp_keys), len(permitted), len(excluded_out),
            len(cached_results), len(comps_out), rejected_quotes, truncated, partial,
            PROMPT_VERSION, AI_MODEL, usage["tokens_in"], usage["tokens_out"], est_cost,
            int((time.monotonic() - start) * 1000),
        )

        return {
            "mix": {k: v for k, v in mix.items() if v > 0},
            "comps": comps_out,
            "excluded": excluded_out,
            "meta": meta,
        }
    finally:
        # ⛔ Released in finally — otherwise one Vertex error permanently wedges
        # this area for this user until the instance restarts (§A.7.1).
        with _inflight_lock:
            _inflight.discard(inflight_key)
