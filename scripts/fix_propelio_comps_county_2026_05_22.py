"""
One-shot migration: backfill parcel_county on propelio_comps rows that
came out of the 2026-05-04 + 2026-05-11 archive→global migrations.

Background:
  The original backfill (scripts/backfill_propelio_comps.py) ran twice
  in May 2026 to migrate workspace-scoped propelio_comp_archive rows
  into the new global propelio_comps table. The source archive table
  didn't carry parcel_county, so line 131 of that script hardcoded
  None. Result: 826 (originally 867) rows had parcel_account_num set
  but parcel_county NULL — breaking the frontend bucket lookup at
  frontend/map.js:5047 → those comps fell into the off_market bucket
  instead of their real category (multifamily for the BANDERA AVE
  condo investigation that surfaced the bug).

  Live writes since 2026-05-10 (commit bd398c1) all set parcel_county
  correctly via match_comps_to_parcels, so this is a one-shot fix for
  the tailings.

Strategy (see docs/PROPELIO_COMPS_COUNTY_BACKFILL_SPEC.md):
  Phase 1 — 817 unambiguous account_nums get their county set via a
            straight per-county UPDATE that joins propelio_comps to
            each county's parcel table.
  Phase 2 — 9 known Collin↔Denton collisions get resolved via PostGIS
            ST_Contains on the comp's lat/lng against each county's
            polygon (whichever polygon contains the point wins).
  Phase 3 — verify zero residual rows with parcel_account_num set but
            parcel_county NULL.
  Phase 4 — add CHECK constraint so the bug class can't recur:
            (parcel_account_num IS NULL AND parcel_county IS NULL) OR
            (parcel_account_num IS NOT NULL AND parcel_county IS NOT NULL)

Pre-requisite:
  scripts/backfill_propelio_comps.py must be patched FIRST (call
  match_comps_to_parcels on each batch before _extract_comp_fields)
  so a concurrent re-run can't overwrite our fixes via ON CONFLICT
  DO UPDATE.

Usage:
  python3 scripts/fix_propelio_comps_county_2026_05_22.py [--dry-run]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import (
    get_conn,
    get_session_conn,
    release_conn,
    release_session_conn,
)


# The 9 known Collin↔Denton collision account_nums discovered during the
# 2026-05-22 audit. Each exists in BOTH collin_parcels AND denton_parcels.
# Phase 1 skips these; Phase 2 resolves them via geo-containment.
KNOWN_COLLISIONS = (
    "1086620", "22308", "1088101", "267311", "1082508",
    "1081340", "215751", "112933", "269435",
)

COUNTY_TABLES = (
    ("dcad",   "parcels"),
    ("tad",    "tad_parcels"),
    ("collin", "collin_parcels"),
    ("denton", "denton_parcels"),
)


def _count_missing(cur) -> int:
    cur.execute("""
        SELECT COUNT(*) FROM propelio_comps
        WHERE parcel_account_num IS NOT NULL AND parcel_county IS NULL
    """)
    return int(cur.fetchone()[0])


def _phase1_unambiguous(session_cur, main_cur) -> dict[str, int]:
    """For each county, pull matching account_nums from MAIN DB then UPDATE
    propelio_comps in SESSION DB (parcel tables live in main, propelio_comps
    lives in session — no cross-DB JOIN possible). Skips the 9 known
    collisions; they get geo-resolved in Phase 2.
    """
    results: dict[str, int] = {}
    collision_set = set(KNOWN_COLLISIONS)

    # First: pull the set of affected account_nums from session DB
    session_cur.execute("""
        SELECT DISTINCT parcel_account_num FROM propelio_comps
        WHERE parcel_account_num IS NOT NULL AND parcel_county IS NULL
    """)
    affected = [r[0] for r in session_cur.fetchall()]
    affected_non_collision = [a for a in affected if a not in collision_set]
    print(f"  Phase 1 input: {len(affected)} affected account_nums, "
          f"{len(affected_non_collision)} after excluding {len(collision_set)} known collisions")

    for county, table in COUNTY_TABLES:
        # Pull matching account_nums from main DB
        main_cur.execute(
            f"SELECT account_num FROM {table} WHERE account_num = ANY(%s)",
            (affected_non_collision,),
        )
        matching = [r[0] for r in main_cur.fetchall()]
        if not matching:
            print(f"  Phase 1 — {county:<8} no matches")
            results[county] = 0
            continue
        # UPDATE session DB. WHERE clause re-checks parcel_county IS NULL so
        # we don't overwrite rows fixed earlier in the same script run (e.g.
        # a later county finding a row already touched by an earlier county).
        session_cur.execute(
            """
            UPDATE propelio_comps
               SET parcel_county = %s
             WHERE parcel_county IS NULL
               AND parcel_account_num = ANY(%s)
            """,
            (county, matching),
        )
        results[county] = session_cur.rowcount
        print(f"  Phase 1 — {county:<8} {session_cur.rowcount} rows updated "
              f"(from {len(matching)} matching account_nums)")
    return results


def _geo_resolve_county(main_cur, account_num: str, lat: float, lng: float) -> str | None:
    """For a Collin↔Denton collision, test the comp's lat/lng against each
    candidate parcel's polygon. Returns 'collin' or 'denton', or None if
    neither polygon contains the point (e.g. lat/lng missing or drift).
    """
    if lat is None or lng is None:
        return None
    main_cur.execute(
        """
        SELECT 1 FROM collin_parcels
         WHERE account_num = %s
           AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
         LIMIT 1
        """,
        (account_num, lng, lat),
    )
    if main_cur.fetchone():
        return "collin"
    main_cur.execute(
        """
        SELECT 1 FROM denton_parcels
         WHERE account_num = %s
           AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
         LIMIT 1
        """,
        (account_num, lng, lat),
    )
    if main_cur.fetchone():
        return "denton"
    return None


def _phase2_collisions(session_cur, main_cur) -> tuple[int, int]:
    """Geo-resolve the 9 known Collin↔Denton collisions via PostGIS."""
    collision_list = list(KNOWN_COLLISIONS)
    session_cur.execute(
        """
        SELECT comp_id, parcel_account_num, lat, lng
          FROM propelio_comps
         WHERE parcel_county IS NULL
           AND parcel_account_num IS NOT NULL
           AND parcel_account_num = ANY(%s)
        """,
        (collision_list,),
    )
    collision_rows = session_cur.fetchall()
    print(f"  Phase 2 — {len(collision_rows)} collision rows to resolve via geo")

    resolved = 0
    unresolved = 0
    for comp_id, acct, lat, lng in collision_rows:
        lat_f = float(lat) if lat is not None else None
        lng_f = float(lng) if lng is not None else None
        county = _geo_resolve_county(main_cur, acct, lat_f, lng_f)
        if county is None:
            unresolved += 1
            print(f"    {acct} @ ({lat_f},{lng_f}) → UNRESOLVED (leaving NULL)")
            continue
        session_cur.execute(
            "UPDATE propelio_comps SET parcel_county = %s WHERE comp_id = %s",
            (county, comp_id),
        )
        resolved += 1
        print(f"    {acct} @ ({lat_f:.5f},{lng_f:.5f}) → {county}")
    return resolved, unresolved


def _phase4_check_constraint(session_cur) -> None:
    """Add CHECK constraint preventing the bug class from recurring."""
    # Idempotent: only add if not already present.
    session_cur.execute(
        """
        SELECT 1 FROM pg_constraint
         WHERE conname = 'parcel_attrs_paired'
           AND conrelid = 'propelio_comps'::regclass
        """
    )
    if session_cur.fetchone():
        print("  Phase 4 — CHECK constraint 'parcel_attrs_paired' already exists, skipping")
        return
    session_cur.execute(
        """
        ALTER TABLE propelio_comps
        ADD CONSTRAINT parcel_attrs_paired CHECK (
            (parcel_account_num IS NULL AND parcel_county IS NULL)
            OR
            (parcel_account_num IS NOT NULL AND parcel_county IS NOT NULL)
        )
        """
    )
    print("  Phase 4 — CHECK constraint added: bug class can't recur")


def run_fix(*, dry_run: bool = False) -> None:
    session_conn = get_session_conn()
    main_conn = get_conn()
    try:
        with session_conn.cursor() as session_cur, main_conn.cursor() as main_cur:
            before = _count_missing(session_cur)
            print(f"Before: {before} rows missing parcel_county")
            if before == 0:
                print("Nothing to do.")
                return

            print()
            print("--- Phase 1: Unambiguous matches (817 expected) ---")
            phase1_results = _phase1_unambiguous(session_cur, main_cur)
            phase1_total = sum(phase1_results.values())
            print(f"  Phase 1 total: {phase1_total} rows updated")

            print()
            print("--- Phase 2: Geo-resolve Collin↔Denton collisions (9 expected) ---")
            resolved, unresolved = _phase2_collisions(session_cur, main_cur)
            print(f"  Phase 2 resolved: {resolved}, unresolved: {unresolved}")

            print()
            print("--- Phase 3: Verify ---")
            after = _count_missing(session_cur)
            print(f"After: {after} rows still missing parcel_county")
            print(f"Fixed: {before - after} rows total")

            print()
            print("--- Phase 4: Schema guardrail ---")
            if after == 0:
                _phase4_check_constraint(session_cur)
            else:
                print(f"  Skipping CHECK constraint — {after} rows still missing parcel_county")

            if dry_run:
                print()
                print("DRY-RUN: rolling back all changes")
                session_conn.rollback()
            else:
                session_conn.commit()
                print()
                print("Committed.")
    finally:
        release_session_conn(session_conn)
        release_conn(main_conn)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the work in transactions but rollback at the end.")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    run_fix(dry_run=bool(args.dry_run))


if __name__ == "__main__":
    main()
