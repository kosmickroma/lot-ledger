# School Ratings — DB-backed runtime query path.
# docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §5.
#
# Dark, additive alongside the pilot's static path (api/school_pilot/
# zones.py) -- selected only when SCHOOL_SOURCE=db (api/school_pilot/
# routes.py). Never removes/touches the static path.
#
# Data-DB pool only (api.config.get_conn/release_conn -- the `lotledger`
# parcels/flood DB), always released via `finally`. Never the sessions
# pool. Bounded SELECT (SET LOCAL statement_timeout), GiST-indexed
# ST_Contains, DISTINCT ON + ST_Area tie-break (mirrors the flood runtime
# join, api/main.py's _hydrate_flood_for_rows, collapsed to a single point
# since this is one lat/lng per call, never a batch -- §5 explicitly bans
# folding school hydration into any _hydrate_*_for_rows-style batch).
#
# On timeout/error: return an all-null, district_status=None response --
# the frontend's never-fabricate contract renders NOTHING for that (a
# genuine backend failure is not the same as an honest "not loaded yet"
# business state -- see this module's own docstring on _ERROR_RESULT).
from __future__ import annotations

import logging
from typing import Any

from api.config import get_conn, release_conn
# ⚠️ Static, first-party runtime data — NEVER import from scripts/ here (docs/
# + scripts/ are dockerignored; a scripts import 500s every request in the
# deployed container -- packaging bug 2026-07-21). See api/school_pilot/districts.py.
from api.school_pilot.districts import (
    DALLAS_ISD_NAME_TO_TEA_ID,
    OPEN_ENROLLMENT_DISTRICT_TEA_IDS,
)

logger = logging.getLogger(__name__)

_LEVELS = ("elementary", "middle", "high")


def _district_status_result() -> dict[str, Any]:
    return {"district_status": None, "elementary": None, "middle": None, "high": None}


# A hard query error/timeout is NOT the same as the honest "not loaded yet"
# business state -- §5's "timeout/error -> nulls -> render nothing" and
# §6's 3-state contract are reconciled here by giving errors their own
# internal signal (district_status=None) distinct from the 3 real states
# ("ingested" | "not_loaded" | "open_enrollment"). The frontend treats a
# missing/None district_status as "render nothing," same as the pilot's
# original miss behavior; it treats the 3 enum values as the new honest
# per-district messaging. See build report for why this split exists.
_ERROR_RESULT = _district_status_result


def _resolve_district_tea_id_from_isd(isd: str | None) -> str | None:
    """`isd` is the parcel's CAD-derived string, e.g. "dcad:DALLAS ISD" or
    "tad:905" (api/ai/enrichment.py's `_qualify_isd`). Only Dallas-county
    (`dcad:`) names can be resolved today -- Tarrant/Collin ship numeric
    codes with no name map yet (spec §6, §11: deferred)."""
    if not isd or ":" not in isd:
        return None
    cad_key, raw = isd.split(":", 1)
    if cad_key != "dcad":
        return None
    return DALLAS_ISD_NAME_TO_TEA_ID.get(raw.strip().upper())


def _district_display_name(isd: str | None) -> str | None:
    """Human-facing district name for the "not loaded yet"/"open enrollment"
    messages -- ONLY for `dcad:` (Dallas county, text names). Tarrant/Collin
    ship numeric codes with no name map (spec §6, §11: deferred) -- None
    here means the frontend falls back to generic wording, never a guess."""
    if not isd or ":" not in isd:
        return None
    cad_key, raw = isd.split(":", 1)
    if cad_key != "dcad":
        return None
    return raw.strip() or None


def _district_has_any_rows(cur, district_tea_id: str) -> bool:
    cur.execute(
        "SELECT 1 FROM school_attendance_zones WHERE district_tea_id = %s LIMIT 1",
        (district_tea_id,),
    )
    return cur.fetchone() is not None


def _district_status(cur, district_tea_id: str | None, any_level_resolved: bool) -> str:
    if any_level_resolved:
        return "ingested"
    if district_tea_id in OPEN_ENROLLMENT_DISTRICT_TEA_IDS:
        return "open_enrollment"
    if district_tea_id and _district_has_any_rows(cur, district_tea_id):
        # §6 case 4, district-wide: ingested, but this exact point missed
        # every level (edge parcel / boundary gap). Still "ingested" --
        # each per-level None is rendered as an omitted card, never a
        # "not loaded yet" district-level lie.
        return "ingested"
    return "not_loaded"


def assign_db(lat: float, lng: float, isd: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {lvl: None for lvl in _LEVELS}
    # get_conn() itself can raise (pool exhaustion, DB unreachable) -- caught
    # here too, not just query errors, or a connection failure would escape
    # this "never fabricate, never propagate" boundary uncaught (found live
    # during the §9.6 rehearsal against an intentionally unreachable port).
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '3s'")
            cur.execute(
                """
                SELECT DISTINCT ON (z.level)
                       z.level, z.campus_name, z.campus_tea_id,
                       z.boundary_vintage, z.district_tea_id
                FROM school_attendance_zones z
                WHERE z.level = ANY(%s)
                  AND ST_Contains(z.geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                ORDER BY z.level, ST_Area(z.geom) DESC
                """,
                (list(_LEVELS), lng, lat),
            )
            zone_rows = cur.fetchall()

            resolved_district: str | None = None
            for level, campus_name, campus_tea_id, vintage, district_tea_id in zone_rows:
                resolved_district = district_tea_id
                rating_letter = rating_score = achievement = growth = rating_year = None
                if campus_tea_id:
                    cur.execute(
                        """
                        SELECT letter, score, achievement, growth, rating_year
                        FROM school_campus_ratings
                        WHERE campus_tea_id = %s
                        ORDER BY rating_year DESC LIMIT 1
                        """,
                        (campus_tea_id,),
                    )
                    r = cur.fetchone()
                    if r:
                        rating_letter, rating_score, achievement, growth, rating_year = r
                out[level] = {
                    "name": campus_name,
                    "rating": rating_letter,
                    "score": rating_score,
                    "achievement": achievement,
                    "growth": growth,
                    "rating_year": rating_year,
                    "boundary_vintage": vintage,
                }

            district_tea_id = resolved_district or _resolve_district_tea_id_from_isd(isd)
            status = _district_status(cur, district_tea_id, bool(zone_rows))
            out["district_status"] = status
            if status != "ingested":
                out["district_name"] = _district_display_name(isd)
        return out
    except Exception:
        # Log, don't just swallow. A swallowed error and an honest empty
        # state were visually identical for a whole debugging session
        # (2026-07-21) -- the response contract stays (never fabricate), but
        # the silence goes.
        logger.exception("school_pilot.assign_db failed lat=%s lng=%s isd=%s", lat, lng, isd)
        return _ERROR_RESULT()
    finally:
        # Read-only (SELECT-only) -- rollback just discards the implicit
        # transaction cleanly either way, success or error, so the pooled
        # connection never goes back "idle in transaction" (a failed
        # statement otherwise leaves it aborted for the NEXT borrower).
        # conn can be None here if get_conn() itself failed above.
        if conn is not None:
            conn.rollback()
            release_conn(conn)
