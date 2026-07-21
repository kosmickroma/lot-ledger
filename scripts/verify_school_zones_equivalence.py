#!/usr/bin/env python3
# scripts/verify_school_zones_equivalence.py
#
# docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md "Gap 1" -- the acceptance
# gate before SCHOOL_SOURCE is ever flipped to "db" anywhere. Proves the DB
# path (api/school_pilot/zones_db.py::assign_db) resolves the SAME zoned
# schools -- name, rating, score -- as the live static path
# (api/school_pilot/zones.py::assign) does today, over a coordinate set.
#
# Calls both functions directly, in-process -- no SCHOOL_SOURCE env flip
# anywhere, so this can run safely while prod is still on "static" (the
# committed default). A clean run is evidence the flip is safe; it is not
# itself the flip.
#
# Run:
#   .venv/bin/python3 scripts/verify_school_zones_equivalence.py
#   .venv/bin/python3 scripts/verify_school_zones_equivalence.py --points-file extra.json
#   .venv/bin/python3 scripts/verify_school_zones_equivalence.py --parcel-sample 200
#
# ⚠️ --parcel-sample queries the real parcels/appraisal tables (a read-only
# SELECT, same data DB the static/DB school paths already share) -- correct
# and ready for the supervised prod window (spec §10), but NOT exercised
# during a throwaway-DB rehearsal, since the rehearsal DB never has a real
# parcels table. Omit the flag (default 0) for a rehearsal or CI run.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn  # noqa: E402
from api.school_pilot.zones import assign as assign_static  # noqa: E402
from api.school_pilot.zones_db import assign_db  # noqa: E402

_LEVELS = ("elementary", "middle", "high")

# The pilot's 3 independently-verified smoke addresses
# (tests/test_school_pilot_smoke.py) -- duplicated here rather than
# imported, since tests/ is a pytest tree, not a package meant to be
# imported by a production script. Keep in sync by hand; each address's
# provenance (own-campus-address, Census-geocoded, HAR.com cross-checked)
# is documented in that test file, not repeated here.
SMOKE_POINTS: list[tuple[str, float, float]] = [
    ("Woodrow Wilson High School — 100 S Glasgow Dr, Dallas, TX 75214", 32.805200632373, -96.750980585861),
    ("James Bowie Elementary — 330 N Marsalis Ave, Dallas, TX 75203", 32.751708978938, -96.815564405173),
    ("Zan Wesley Holmes Jr Middle — 2939 St Rita Dr, Dallas, TX 75233", 32.711497894182, -96.866196156033),
]


def sample_disd_parcel_centroids(conn, n: int) -> list[tuple[str, float, float]]:
    """Real DCAD parcel centroids inside Dallas ISD, read-only
    (parcels.centroid / appraisal.isd_desc -- the same columns api/main.py's
    existing DCAD address-search and enrichment queries already use).
    Broadens the comparison past the 3 smoke addresses. See module
    docstring: not run during a throwaway-DB rehearsal (no real parcels
    table there)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.account_num, ST_Y(p.centroid), ST_X(p.centroid)
            FROM parcels p
            JOIN appraisal a ON a.account_num = p.account_num
            WHERE a.isd_desc = 'DALLAS ISD' AND p.centroid IS NOT NULL
            ORDER BY random()
            LIMIT %s
            """,
            (n,),
        )
        return [(f"parcel {acct}", float(lat), float(lng)) for acct, lat, lng in cur.fetchall()]


def _level_signature(result: dict[str, Any] | None, level: str) -> tuple[Any, Any, Any]:
    s = result.get(level) if result else None
    if not s:
        return (None, None, None)
    return (s.get("name"), s.get("rating"), s.get("score"))


def diff_point(lat: float, lng: float) -> list[tuple[str, tuple, tuple]]:
    """Per spec: assert elementary/middle/high campus name + rating + score
    match between the two paths. Returns a list of (level, static_sig,
    db_sig) for every level that disagrees -- empty list means equivalent."""
    static_result = assign_static(lat, lng)
    db_result = assign_db(lat, lng)
    diffs = []
    for level in _LEVELS:
        static_sig = _level_signature(static_result, level)
        db_sig = _level_signature(db_result, level)
        if static_sig != db_sig:
            diffs.append((level, static_sig, db_sig))
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points-file", help="Optional JSON list of {label, lat, lng} to add to the smoke-address set")
    parser.add_argument("--parcel-sample", type=int, default=0, help="Also sample N random DCAD parcel centroids inside Dallas ISD (real prod read -- see module docstring)")
    args = parser.parse_args()

    points: list[tuple[str, float, float]] = list(SMOKE_POINTS)
    if args.points_file:
        extra = json.loads(Path(args.points_file).read_text())
        points.extend((p["label"], p["lat"], p["lng"]) for p in extra)

    if args.parcel_sample:
        conn = get_conn()
        try:
            points.extend(sample_disd_parcel_centroids(conn, args.parcel_sample))
        finally:
            release_conn(conn)

    compared = 0
    mismatched = 0
    for label, lat, lng in points:
        compared += 1
        diffs = diff_point(lat, lng)
        if diffs:
            mismatched += 1
            print(f"[verify_school_zones_equivalence] MISMATCH at {label} ({lat}, {lng}):")
            for level, static_sig, db_sig in diffs:
                print(f"    {level}: static(name,rating,score)={static_sig}  db(name,rating,score)={db_sig}")

    matched = compared - mismatched
    print(f"[verify_school_zones_equivalence] {compared} compared, {matched} matched, {mismatched} mismatched")
    return 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main())
