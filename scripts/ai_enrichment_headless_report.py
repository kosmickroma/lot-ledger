#!/usr/bin/env python3
"""Headless verification for Task A (enrichment) + Task B (comparator).

See docs/AI/CODER_SPEC_FACTS_2026-07-14.md §B.3 — "Verify it HEADLESSLY
before any UI consumes it." Read-only against the live DB (no writes; nothing
in Task A/B stores anything either).

Part 1 — corpus-wide coverage, matching the §1 expected-coverage table
(subdivision / garbage / ISD per county, calling enrich_comps() directly).

Part 2 — real saved areas: enrich the subject, load its comps through the
SAME load_comps_by_polygon() path the product uses (so this also exercises
the Task A.5 wiring), run compare_to_subject() on each pair, and report
same_subdivision / same_isd rates plus a spot-check sample for KK to eyeball.

Usage: python3 scripts/ai_enrichment_headless_report.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.ai.comparator import compare_to_subject  # noqa: E402
from api.ai.enrichment import _is_garbage_subdivision, enrich_comps  # noqa: E402
from api.config import get_conn, get_session_conn, release_conn, release_session_conn  # noqa: E402
from api.counties.dcad import _clean_text, _dcad_subdivision_from_legal  # noqa: E402
from api.counties.tad import _tad_subdivision_from_legal  # noqa: E402
from api.propelio.archive import load_comps_by_polygon  # noqa: E402

COUNTIES = ["dcad", "tad", "collin", "denton"]
_BATCH = 5000
_AREA_SAMPLE_SIZE = 30


def _readonly(conn):
    """Belt-and-suspenders: this script only ever issues SELECTs (as does
    every function it calls — enrich_comps/load_comps_by_polygon), but per
    project convention for ad-hoc queries against the live prod DB, force
    the session read-only so a mistake here can't write anything."""
    with conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = on")
    return conn

_RAW_SUBDIVISION_SOURCE = {
    "dcad": ("parcels", "legal1", lambda t: _dcad_subdivision_from_legal(t)),
    "tad": ("tad_parcels", "legal_descr", lambda t: _tad_subdivision_from_legal(_clean_text(t))),
    "collin": ("collin_parcels", "subdivision", lambda t: t),
    "denton": ("denton_parcels", "subdivision", lambda t: t),
}


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _load_comp_accounts_by_county() -> dict[str, list[str]]:
    conn = _readonly(get_session_conn())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT parcel_county, parcel_account_num
                FROM propelio_comps
                WHERE parcel_county IS NOT NULL AND parcel_account_num IS NOT NULL
                """
            )
            rows = cur.fetchall()
    finally:
        release_session_conn(conn)

    by_county: dict[str, list[str]] = {c: [] for c in COUNTIES}
    for county, account_num in rows:
        if county in by_county:
            by_county[county].append(account_num)
    return by_county


def _raw_subdivision_text(county: str, account_nums: list[str]) -> dict[str, str]:
    """Pre-garbage-filter subdivision text, keyed by account_num — used only
    to separate 'garbage, correctly rejected' from 'source data genuinely blank'."""
    table, column, parse = _RAW_SUBDIVISION_SOURCE[county]
    out: dict[str, str] = {}
    conn = _readonly(get_conn())
    try:
        with conn.cursor() as cur:
            for batch in _chunks(account_nums, _BATCH):
                cur.execute(f"SELECT account_num, {column} FROM {table} WHERE account_num = ANY(%s)", (batch,))
                for account_num, raw_value in cur.fetchall():
                    out[account_num] = _clean_text(parse(raw_value))
    finally:
        release_conn(conn)
    return out


def part1_corpus_coverage() -> None:
    print("=" * 78)
    print("PART 1 — corpus-wide coverage (compare against §1's expected table)")
    print("=" * 78)

    by_county = _load_comp_accounts_by_county()
    total = sum(len(v) for v in by_county.values())
    print(
        f"Loaded {total} parcel-matched comps across {len(COUNTIES)} counties.\n"
        "NOTE: this denominator is COMPS (parcel-matched propelio_comps rows), not\n"
        "all rows in the county parcel tables — comps land on populated parcels by\n"
        "definition, so this will read a few points higher than a coverage measure\n"
        "taken over the full parcels table (e.g. Tarrant ISD: ~100% here vs ~99.1%\n"
        "over all tad_parcels, where ~7,069 rows have a blank school_code).\n"
    )

    header = f"{'county':<8} {'comps':>7} {'subdivision':>16} {'garbage':>9} {'isd':>16}"
    print(header)
    print("-" * len(header))

    grand_sub = grand_garbage = grand_isd = 0

    for county in COUNTIES:
        account_nums = by_county[county]
        n = len(account_nums)
        if n == 0:
            print(f"{county:<8} {0:>7}")
            continue

        enriched: dict[str, dict] = {}
        for batch in _chunks(account_nums, _BATCH):
            enriched.update(enrich_comps(county, batch))

        raw_sub = _raw_subdivision_text(county, account_nums)

        sub_hit = sum(1 for a in account_nums if enriched.get(a, {}).get("cad_subdivision"))
        garbage = sum(
            1
            for a in account_nums
            if not enriched.get(a, {}).get("cad_subdivision")
            and raw_sub.get(a)
            and _is_garbage_subdivision(raw_sub[a])
        )
        isd_hit = sum(1 for a in account_nums if enriched.get(a, {}).get("isd"))

        grand_sub += sub_hit
        grand_garbage += garbage
        grand_isd += isd_hit

        print(
            f"{county:<8} {n:>7} {sub_hit:>7} ({sub_hit/n:>5.1%}) {garbage:>9} "
            f"{isd_hit:>7} ({isd_hit/n:>5.1%})"
        )

    print("-" * len(header))
    if total:
        print(
            f"{'TOTAL':<8} {total:>7} {grand_sub:>7} ({grand_sub/total:>5.1%}) {grand_garbage:>9} "
            f"{grand_isd:>7} ({grand_isd/total:>5.1%})"
        )
    print()


def part2_real_saved_areas() -> None:
    print("=" * 78)
    print("PART 2 — real saved areas: subject vs. comp, via compare_to_subject()")
    print("=" * 78)

    conn = _readonly(get_session_conn())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT area_id, polygon, originator_parcel_county, originator_parcel_account_num
                FROM saved_areas
                WHERE originator_parcel_county IS NOT NULL
                  AND originator_parcel_account_num IS NOT NULL
                  AND polygon IS NOT NULL
                """
            )
            areas = cur.fetchall()
    finally:
        release_session_conn(conn)

    total_qualifying = len(areas)
    if total_qualifying > _AREA_SAMPLE_SIZE:
        random.seed(20260714)
        areas = random.sample(areas, _AREA_SAMPLE_SIZE)
    print(
        f"Found {total_qualifying} saved areas with a subject set — "
        f"sampling {len(areas)} of them (each area's comp load takes "
        f"~10-15s from this environment; a full sweep is a separate,\n"
        f"longer-running exercise, not needed to verify the comparator).\n"
    )

    same_sub_true = same_sub_false = same_sub_null = 0
    same_isd_true = same_isd_false = same_isd_null = 0
    total_pairs = 0
    spot_checks: list[tuple[str, str, dict]] = []

    for area_id, polygon, subject_county, subject_account in areas:
        subject_county = str(subject_county or "").strip().lower()
        subject_account = str(subject_account or "").strip()
        if not subject_county or not subject_account or not isinstance(polygon, list) or len(polygon) < 3:
            continue

        subject_enriched = enrich_comps(subject_county, [subject_account]).get(subject_account) or {
            "cad_subdivision": None,
            "isd": None,
        }
        # lat/lng for distance_mi isn't needed for the subdivision/ISD report —
        # subject coordinates aren't loaded here to keep this script Task-A/B
        # scoped; distance is exercised implicitly (returns None cleanly).
        subject = {**subject_enriched, "lat": None, "lng": None}

        comps = load_comps_by_polygon(polygon, area_id)
        for comp in comps:
            fact = compare_to_subject(subject, comp)
            total_pairs += 1

            if fact["same_subdivision"] is True:
                same_sub_true += 1
            elif fact["same_subdivision"] is False:
                same_sub_false += 1
            else:
                same_sub_null += 1

            if fact["same_isd"] is True:
                same_isd_true += 1
            elif fact["same_isd"] is False:
                same_isd_false += 1
            else:
                same_isd_null += 1

            spot_checks.append((area_id, subject_county, {
                "subject_account": subject_account,
                "subject_subdivision": subject_enriched["cad_subdivision"],
                "subject_isd": subject_enriched["isd"],
                "comp_key": comp.get("comp_address_key"),
                "comp_subdivision": comp.get("cad_subdivision"),
                "comp_isd": comp.get("isd"),
                "fact": fact,
            }))

    print(f"Compared {total_pairs} subject/comp pairs across {len(areas)} areas.\n")
    if total_pairs:
        print(
            f"same_subdivision: True={same_sub_true} ({same_sub_true/total_pairs:.1%})  "
            f"False={same_sub_false} ({same_sub_false/total_pairs:.1%})  "
            f"Unknown={same_sub_null} ({same_sub_null/total_pairs:.1%})"
        )
        print(
            f"same_isd:         True={same_isd_true} ({same_isd_true/total_pairs:.1%})  "
            f"False={same_isd_false} ({same_isd_false/total_pairs:.1%})  "
            f"Unknown={same_isd_null} ({same_isd_null/total_pairs:.1%})"
        )

    print("\n--- 20 spot-check pairs for KK to eyeball ---")
    random.seed(20260714)
    sample = random.sample(spot_checks, min(20, len(spot_checks)))
    for area_id, county, row in sample:
        print(
            f"  area={area_id[:8]} county={county:<7} "
            f"subject={row['subject_subdivision']!r} / {row['subject_isd']!r}   "
            f"comp={row['comp_subdivision']!r} / {row['comp_isd']!r}   "
            f"-> {row['fact']}"
        )
    print()


def main() -> None:
    part1_corpus_coverage()
    part2_real_saved_areas()


if __name__ == "__main__":
    main()
