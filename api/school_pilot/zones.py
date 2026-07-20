# School Ratings — Dallas ISD Pilot — in-memory zone loader + assignment.
# docs/AI/SCHOOL_RATINGS_PILOT_SPEC_2026-07-20.md §5.1
# docs/AI/SCHOOL_RATINGS_PILOT_PLAN_2026-07-20.md Task 2
#
# No DB, no per-request I/O after the first call. Loads the three baked zone
# files + the ratings file (produced by scripts/build_school_pilot_data.py)
# into memory once (module-level lru_cache singleton), then answers
# lat/lng -> {elementary, middle, high} via pure-Python point-in-polygon
# (api/geo.py, no shapely). Tolerates missing files: a data outage degrades
# to all-null assignments, never a raised exception (§8: the pilot must
# never break the app over a school-data hiccup).
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from api.geo import point_in_polygon  # existing ray-casting, geo.py:72

_DATA = Path(__file__).resolve().parent.parent.parent / "data" / "school_pilot"

_LEVELS = ("elementary", "middle", "high")


def point_in_parts(lng: float, lat: float, parts: list[list[list[list[float]]]]) -> bool:
    """True iff (lng,lat) is inside an outer ring of any polygon part and NOT
    inside any of that part's holes. `parts` = list of polygons; each polygon
    = list of rings, ring[0]=outer, ring[1:]=holes (Task 1's zone-file
    schema). geo.py's point_in_polygon takes a single ring and is winding-
    direction-agnostic (ray-casting parity) — it has no hole concept itself,
    hence the explicit exclude-if-in-hole check here."""
    for rings in parts:
        if not rings:
            continue
        outer = rings[0]
        if not point_in_polygon(lat, lng, outer):
            continue
        in_hole = any(point_in_polygon(lat, lng, hole) for hole in rings[1:])
        if not in_hole:
            return True
    return False


@functools.lru_cache(maxsize=1)
def load_zones() -> dict[str, Any]:
    """Load baked data; tolerate missing files (return an empty set so
    assign() returns all-nulls, never raise). Lazy + memoized: the first
    request pays the load cost, every later request in this process reuses
    it (module-level singleton, ~190 polygons / <=5 MB — see spec §5.1)."""
    def _read(name: str):
        p = _DATA / name
        return json.loads(p.read_text()) if p.exists() else None

    ratings_doc = _read("ratings.json")
    ratings = ratings_doc["ratings"] if ratings_doc else {}
    year = ratings_doc["meta"]["tea_year"] if ratings_doc else None
    levels = {}
    for level in _LEVELS:
        doc = _read(f"{level}.json")
        levels[level] = (
            {"vintage": doc["meta"]["boundary_vintage"], "zones": doc["zones"]}
            if doc else {"vintage": None, "zones": []}
        )
    return {"levels": levels, "ratings": ratings, "year": year}


def assign(lat: float, lng: float) -> dict[str, dict[str, Any] | None]:
    """For each level, the first zone whose polygon contains (lat,lng) ->
    {name, rating, rating_year, boundary_vintage}; None where no zone
    contains it (no fallback, no guess — spec §6.4/§6.7: a miss renders
    nothing)."""
    data = load_zones()
    out: dict[str, dict[str, Any] | None] = {}
    for level in _LEVELS:
        bundle = data["levels"][level]
        hit = None
        for z in bundle["zones"]:
            if point_in_parts(lng, lat, z["parts"]):
                rating = (data["ratings"].get(z.get("tea_campus_id")) or {}).get("grade")
                hit = {
                    "name": z["campus_name"],
                    "rating": rating,
                    "rating_year": data["year"],
                    "boundary_vintage": bundle["vintage"],
                }
                break
        out[level] = hit
    return out
