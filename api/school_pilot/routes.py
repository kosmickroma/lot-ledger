# School Ratings — Dallas ISD Pilot — GET /api/school-pilot/assign
# docs/AI/SCHOOL_RATINGS_PILOT_SPEC_2026-07-20.md §5.1
# docs/AI/SCHOOL_RATINGS_PILOT_PLAN_2026-07-20.md Task 3
#
# No SQL of its own — answers entirely from the in-memory zone polygons
# (api/school_pilot/zones.py) loaded from baked JSON files. The only DB touch
# is the shared get_current_user login gate every other endpoint already
# uses (same session lookup every authenticated request incurs — this adds
# no new query load). Mounted only when SCHOOL_PILOT is truthy (api/main.py).
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.auth import get_current_user
from api.school_pilot.zones import assign

router = APIRouter()


@router.get("/api/school-pilot/assign")
async def school_pilot_assign(
    lat: float = Query(...),
    lng: float = Query(...),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return assign(lat, lng)
