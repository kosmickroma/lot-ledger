#!/usr/bin/env python3
# scripts/check_school_zones_freshness.py
#
# docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md "recurring-cadence
# safeguards" Safeguard 2 -- the yearly refresh treadmill has no natural
# forcing function: a district's attendance-zone boundaries can silently
# go a year (or several) stale with nothing in the app ever complaining --
# the DB just keeps serving last year's polygons as if they were current.
# This script is the alarm: it turns silent staleness into a visible,
# scriptable checklist item (exit non-zero + a STALE: line per district).
#
# Deterministic by design: the "as of" date is a REQUIRED CLI argument,
# never datetime.now()/date.today() called directly -- so a stale-flagging
# run is reproducible and testable (same --as-of always produces the same
# expected school year), and CI/a cron can pass today's date explicitly
# rather than this script silently drifting with wall-clock time.
#
# Run:
#   .venv/bin/python3 scripts/check_school_zones_freshness.py --as-of 2026-08-15
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn  # noqa: E402
from scripts.school_zones_registry import load_registry  # noqa: E402

# Texas school years run Aug -> Jun. "Current or coming" as of a date:
# from July 1 onward, districts are typically redrawing/publishing next
# year's boundaries ahead of the fall semester, so the coming year is
# already the expected vintage; before July 1, the year already in
# progress (which started the previous calendar year) is expected.
# Not a value taken from the spec verbatim -- a documented judgment call,
# flagged as such in the build report.
_SCHOOL_YEAR_ROLLOVER_MONTH = 7


def compute_expected_school_year(as_of: date) -> str:
    """"2025-26"-style string -- matches BOUNDARY_VINTAGE's format in
    scripts/build_school_pilot_data.py exactly."""
    if as_of.month >= _SCHOOL_YEAR_ROLLOVER_MONTH:
        start_year = as_of.year
    else:
        start_year = as_of.year - 1
    end_year = start_year + 1
    return f"{start_year}-{str(end_year)[-2:]}"


def ingested_vintages(conn) -> dict[str, set[str | None]]:
    """{district_tea_id: {distinct boundary_vintage values currently in
    the table for that district}} -- a read-only SELECT, no writes."""
    out: dict[str, set[str | None]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT district_tea_id, boundary_vintage FROM school_attendance_zones")
        for district_tea_id, vintage in cur.fetchall():
            out.setdefault(district_tea_id, set()).add(vintage)
    return out


def find_stale_districts(
    vintages_by_district: dict[str, set[str | None]],
    expected: str,
    registry: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[str, str, str]]:
    """[(district_tea_id, display_name, actual_vintage_repr)] for every
    ingested district that ISN'T uniformly on `expected` -- a district
    with a MIX of vintages (e.g. a half-finished re-ingest) is flagged
    too, not just a uniformly-old one."""
    registry = registry or {}
    stale = []
    for district_tea_id, vintages in vintages_by_district.items():
        if vintages == {expected}:
            continue
        name = (registry.get(district_tea_id) or {}).get("name") or district_tea_id
        actual = ", ".join(sorted(v or "(none)" for v in vintages))
        stale.append((district_tea_id, name, actual))
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="YYYY-MM-DD -- deterministic reference date, e.g. today's date passed explicitly")
    args = parser.parse_args()

    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"[check_school_zones_freshness] ERROR: --as-of must be YYYY-MM-DD, got {args.as_of!r}", file=sys.stderr)
        return 1

    expected = compute_expected_school_year(as_of)
    registry = load_registry()

    conn = get_conn()
    try:
        vintages_by_district = ingested_vintages(conn)
    finally:
        release_conn(conn)

    if not vintages_by_district:
        print(f"[check_school_zones_freshness] no districts ingested yet -- nothing to check (expected vintage: {expected})")
        return 0

    stale = find_stale_districts(vintages_by_district, expected, registry)
    if not stale:
        print(f"[check_school_zones_freshness] all {len(vintages_by_district)} ingested districts are current ({expected})")
        return 0

    print(f"[check_school_zones_freshness] expected vintage as of {args.as_of}: {expected}")
    for district_tea_id, name, actual in sorted(stale, key=lambda t: t[1]):
        print(f"STALE: {name} ({district_tea_id}) is {actual}, expected {expected}")
    print(f"[check_school_zones_freshness] {len(stale)}/{len(vintages_by_district)} ingested district(s) stale")
    return 1


if __name__ == "__main__":
    sys.exit(main())
