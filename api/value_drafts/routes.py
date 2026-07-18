# Value Drafts — POST /api/value-drafts/telemetry (spec §6, the anchoring tripwire).
#
# There is no AI in this feature and no math happens here — the draft numbers
# are computed entirely client-side (deterministic arithmetic on comps the
# user is already rating). This endpoint exists ONLY to log structured,
# de-identified telemetry so we can see if the visible draft number is
# steering ratings. NO new table, NO schema change — one server log line per
# event, same shadow-logging-v0 discipline as the AI module (§A.8).
#
# ⛔ comp_address_key is address-derived (client-only handle for a comp) and
# is NEVER logged raw — every key here is resolved to the opaque numeric
# comp_id first, exactly mirroring the existing archive.py:261 /
# api/ai/gate.py pattern, and only the resolved id is ever written to a log.
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.auth import get_current_user, require_csrf
from api.config import get_session_conn, release_session_conn
from api.value_drafts.config import VALUE_DRAFTS_ENABLED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/value-drafts", tags=["value-drafts"])

_MAX_KEPT_KEYS = 500


class DraftTelemetryRequest(BaseModel):
    event_type: Literal["rating", "draft_change", "commit"]
    area_id: str
    view: Optional[Literal["arv", "nbv"]] = None
    field: Optional[Literal["arv", "nbv"]] = None
    comp_key: Optional[str] = None                     # rating events only — resolved, never logged raw
    verdict: Optional[Literal["good", "bad", "unrated"]] = None
    draft_state: Optional[Literal["pool", "kept", "none"]] = None
    draft_value: Optional[float] = None
    from_value: Optional[float] = None
    to_value: Optional[float] = None
    cause: Optional[Literal["rating", "filter", "other"]] = None
    final_value: Optional[float] = None
    outcome: Optional[Literal["accepted_verbatim", "edited", "typed_while_draft_visible"]] = None
    kept_comp_keys: Optional[list[str]] = None          # commit events only — resolved, never logged raw
    filter_state: Optional[dict[str, Any]] = None       # commit events only — UI toggle state, no licensed content
    # Part D item 2, task-3-brief.md: the frontend has sent this since 053576f
    # (value-drafts.js:347, 393) but there was no field here to receive it, so
    # pydantic silently dropped it on every request — a no-op fix, not new
    # capture. Same shape/safety as filter_state: UI toggle state, no licensed content.
    fact_filters: Optional[dict[str, Any]] = None       # commit events only — the fact-filter lens state
    # §3, docs/AI/CODER_SPEC_AIMODE_FIX_2026-07-14.md — commit events only.
    # Nothing else in the record distinguishes a number signed under the AI
    # lens from one signed under the VA's own filters; Accept stays live
    # while AI mode is on, so this is the one place that fact gets logged.
    ai_mode: Optional[bool] = None
    # Part 4, docs/AI/CODER_SPEC_TIER1_NUMBER_2026-07-17.md — commit events
    # only. Which band-factor defaults (e.g. "bands-2026-07-18") a signed
    # number was drafted under, so before/after measurement across a band
    # tweak can tell which regime produced which number. ⛔ Must land in ALL
    # THREE of frontend sendTelemetry / this model / the commit log line, or
    # it's a silent no-op — exactly how ai_mode/fact_filters (053576f) failed
    # the first time: a model field alone with no log-line read is a no-op.
    lens_version: Optional[str] = None


def _resolve_comp_ids(keys: list[str]) -> dict[str, int]:
    if not keys:
        return {}
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comp_address_key, comp_id FROM propelio_comps WHERE comp_address_key = ANY(%s)",
                (keys,),
            )
            return {row[0]: int(row[1]) for row in cur.fetchall()}
    finally:
        release_session_conn(conn)


@router.post("/telemetry")
async def draft_telemetry(
    payload: DraftTelemetryRequest,
    req: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    require_csrf(req)

    role = str(user.get("role") or "").strip().lower()
    if role not in ("owner", "developer"):
        # Server-side, independent of the frontend gate.
        raise HTTPException(status_code=403, detail="Value drafts are not enabled for this role")

    if not VALUE_DRAFTS_ENABLED:
        # Belt-and-braces: the router shouldn't even be mounted when this is
        # false, but a 404 here means no information leak if it somehow is.
        raise HTTPException(status_code=404)

    user_id = int(user["id"])
    area_id = payload.area_id

    if payload.event_type == "rating":
        comp_id = None
        if payload.comp_key:
            comp_id = _resolve_comp_ids([payload.comp_key]).get(payload.comp_key)
        logger.info(
            "value_drafts.rating area_id=%s user=%s role=%s view=%s comp_id=%s "
            "verdict=%s draft_state=%s draft_value=%s",
            area_id, user_id, role, payload.view, comp_id,
            payload.verdict, payload.draft_state, payload.draft_value,
        )
    elif payload.event_type == "draft_change":
        logger.info(
            "value_drafts.draft_change area_id=%s user=%s role=%s field=%s from=%s to=%s cause=%s",
            area_id, user_id, role, payload.field, payload.from_value, payload.to_value, payload.cause,
        )
    else:   # "commit"
        keys = (payload.kept_comp_keys or [])[:_MAX_KEPT_KEYS]
        kept_ids = sorted(_resolve_comp_ids(keys).values())
        # filter_state is UI toggle state only (checkboxes/ranges) — no
        # licensed content, safe to log in full as the kept-set snapshot §6 asks for.
        filter_summary = payload.filter_state if isinstance(payload.filter_state, dict) else {}
        # fact_filters: same safety class as filter_state (UI toggle state).
        # Part D item 2 — this field existed on the frontend since 053576f but
        # had nowhere to land server-side; wiring it into the log line is the
        # other half of that fix (the model field alone is a no-op otherwise).
        fact_filters_summary = payload.fact_filters if isinstance(payload.fact_filters, dict) else {}
        logger.info(
            "value_drafts.commit area_id=%s user=%s role=%s field=%s draft_value=%s final_value=%s "
            "outcome=%s kept_count=%s kept_comp_ids=%s filter_state=%s fact_filters=%s ai_mode=%s "
            "lens_version=%s",
            area_id, user_id, role, payload.field, payload.draft_value, payload.final_value,
            payload.outcome, len(kept_ids), ",".join(str(i) for i in kept_ids), filter_summary,
            fact_filters_summary, bool(payload.ai_mode), payload.lens_version,
        )

    return {"ok": True}
