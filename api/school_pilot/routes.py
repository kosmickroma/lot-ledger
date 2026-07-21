# School Ratings — Dallas ISD Pilot — GET /api/school-pilot/assign
# docs/AI/SCHOOL_RATINGS_PILOT_SPEC_2026-07-20.md §5.1
# docs/AI/SCHOOL_RATINGS_PILOT_PLAN_2026-07-20.md Task 3
# docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §5 -- SCHOOL_SOURCE
# static|db toggle, default static. Ship the DB path dark: read the env var
# per-request (not a load-time const) so flipping it never needs a deploy.
# The only DB touch on the static path is the shared get_current_user login
# gate every other endpoint already uses; on the db path, assign_db() does
# its own bounded read query via the data-DB pool (never this router's concern).
# Mounted only when SCHOOL_PILOT is truthy (api/main.py).
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.auth import get_current_user
from api.school_pilot.zones import assign

router = APIRouter()


@router.get("/api/school-pilot/assign")
async def school_pilot_assign(
    lat: float = Query(...),
    lng: float = Query(...),
    isd: str | None = Query(None),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if os.getenv("SCHOOL_SOURCE", "static").strip().lower() == "db":
        from api.school_pilot.zones_db import assign_db

        return assign_db(lat, lng, isd)
    return assign(lat, lng)
