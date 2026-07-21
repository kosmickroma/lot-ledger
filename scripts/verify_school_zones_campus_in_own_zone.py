#!/usr/bin/env python3
# scripts/verify_school_zones_campus_in_own_zone.py
#
# docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md "recurring-cadence
# safeguards" Safeguard 3 -- campus-in-own-zone post-check.
#
# ⚠️ STUB, HONESTLY BLOCKED -- do not fake this. Every other safeguard in
# this feature validates COUNTS/SCOPE/SCHEMA (registry count floor, <50%
# tripwire, TEA column drift, boundary vintage). None of them can catch a
# specific wrong/mislabeled polygon -- e.g. an adapter's name-matching
# attached "Bowie Elementary"'s zone shape to "Lively Elementary"'s row.
# The only way to catch THAT is to check each campus's own physical point
# location against its own attendance-zone polygon.
#
# This repo has NOT ingested campus point locations (lat/lng per campus).
# TEA's accountability XLSX (scripts/build_school_pilot_data.py's own data
# source) carries no coordinates. The standard free source would be NCES's
# Common Core of Data (CCD) public school locations file -- but its NCES
# School ID is NOT the same identifier as TEA's CAMPUS_ID; a new,
# independently-verified crosswalk would be needed first (mirroring
# derive_tea_campus_id()'s "verify before trust" precedent in
# scripts/build_school_pilot_data.py -- do not assume a naive join works).
#
# Per instruction: do NOT invent point data to unblock this. Flagged back
# so campus-point sourcing can be scheduled as its own piece of work.
from __future__ import annotations

from typing import Any


def verify_campus_in_own_zone(conn: Any) -> list[tuple[str, str]]:
    """For every ingested zone row with a resolved campus_tea_id, checks
    that campus's OWN physical point location falls inside its OWN
    attendance-zone polygon (ST_Contains). A False result means the
    polygon is wrong or mislabeled for that campus -- a data-quality bug
    no other guard in this feature can see, since every other check
    validates counts/scope, never "is this specific shape actually where
    this specific campus is."

    Returns [(campus_tea_id, campus_name)] for every campus whose own
    point falls OUTSIDE its own zone; an empty list means every located
    campus checked out.

    BLOCKED on campus point-location data (see module docstring for why
    and the proposed NCES CCD approach + required crosswalk work). Once a
    campus_tea_id -> (lat, lng) source exists (proposed: an additive
    `school_campus_locations` table, its own migration + scoped-write
    ingest path mirroring school_attendance_zones' discipline -- never a
    shortcut through this feature's existing tables), replace this
    NotImplementedError with:
        SELECT z.campus_tea_id, z.campus_name
        FROM school_attendance_zones z
        JOIN school_campus_locations loc ON loc.campus_tea_id = z.campus_tea_id
        WHERE NOT ST_Contains(z.geom, ST_SetSRID(ST_MakePoint(loc.lng, loc.lat), 4326))
    """
    raise NotImplementedError(
        "Safeguard 3 is blocked on campus point-location data (NCES/TEA), "
        "which this repo has not ingested. Do NOT fake this check with "
        "invented coordinates. Source real campus points first (see this "
        "function's docstring for the proposed NCES Common Core of Data "
        "approach and the required TEA-CAMPUS_ID crosswalk), then "
        "implement the ST_Contains check described above."
    )
