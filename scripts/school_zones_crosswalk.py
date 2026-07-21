"""Generalized zone -> TEA-rating crosswalk (multi-district ratings
foundation, Part B). Given a district's parsed zones (any adapter's output
-- campus_name + level) plus that district's TEA district id and the
all-Texas campus index (scripts.build_school_pilot_data.
load_all_tea_campus_index), resolves each zone's campus_tea_id -- the join
key api/school_pilot/zones_db.py needs to attach a real TEA rating to a
real campus, not just a name.

⚠️ Provenance: this task's Part B spec text arrived cut short identically
twice -- both times stopping mid-sentence right after "Tier 1 --
exact-normalized match, scoped to that district's TEA rows AND the
matching TEA School Type: stopword-strip both sides (EL/ELEMENTARY/MIDDLE/
JUNIOR/H S/HIGH/SC[HOOL?])" and resuming at "Record the match method per
campus (exact / token / null)." The tier structure below (Tier 1 exact,
Tier 2 token-overlap, a duplicate-source-name guard) is RECONSTRUCTED from
those surviving fragments plus the acceptance criteria's five named test
cases (exact hit / token hit / tie->null / duplicate->both null /
no-match->null) and the already-tested precedent this generalizes
(build_school_pilot_data.py's match_campus_to_tea/_tokens/_STOPWORDS,
which already implements exact-then-token-overlap-then-null for DISD
alone). Flagged explicitly in the build report -- correct if this diverges
from the actual intended tiers.

Tier 1 (exact) -- stopword-strip + tokenize both the zone's campus_name
  and every TEA candidate's name (candidates scoped to this district_tea_id
  AND the TEA School Type matching this zone's level); an EXACT token-set
  match to exactly one candidate -> "exact". A tie (>1 candidate sharing
  the identical token set) -> null, never a guess.
Tier 2 (token) -- only tried when Tier 1 found nothing: token-SET-OVERLAP
  scoring against the same scoped candidates (mirrors match_campus_to_
  tea's already-tested algorithm). Zero overlap, or a scoring tie between
  >1 different candidates -> null.
Duplicate source-name guard -- if the DISTRICT'S OWN zone list has the
  SAME campus_name (case/whitespace-normalized) appearing more than once
  at the same level, every zone sharing that name resolves to null --
  attaching one TEA rating to two ambiguous same-named zones would be
  worse than attaching none. This is a source-data-quality check, run
  BEFORE either tier, independent of what TEA has on file.

Stopword list extends build_school_pilot_data.py's already-tested
_STOPWORDS (tuned against DISD's specific naming) with "JUNIOR" -- the one
additional word the surviving spec fragment explicitly named that isn't
already in that set (which has "JR" but not the spelled-out form a fresh
district's names might use).
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from scripts.build_school_pilot_data import TEA_TYPE_BY_LEVEL, _STOPWORDS, _tokens

_EXTRA_STOPWORDS = {"JUNIOR"}


def _crosswalk_tokens(name: str | None) -> set[str]:
    return _tokens(name) - _EXTRA_STOPWORDS


def _normalize_source_name(name: str | None) -> str:
    return " ".join(str(name or "").strip().upper().split())


def _candidates_for(district_tea_id: str, level: str, tea_campus_index: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    tea_type = TEA_TYPE_BY_LEVEL[level]
    return {
        campus_tea_id: info
        for campus_tea_id, info in tea_campus_index.items()
        if info.get("district_number") == district_tea_id and info.get("type") == tea_type
    }


def _tier1_exact_hits(zone_tokens: set[str], candidates: dict[str, dict[str, Any]]) -> list[str]:
    return [
        campus_tea_id for campus_tea_id, info in candidates.items()
        if _crosswalk_tokens(info["name"]) == zone_tokens
    ]


def _tier2_token_overlap_match(zone_tokens: set[str], candidates: dict[str, dict[str, Any]]) -> str | None:
    best_score = 0
    best_ids: set[str] = set()
    for campus_tea_id, info in candidates.items():
        score = len(zone_tokens & _crosswalk_tokens(info["name"]))
        if score == 0:
            continue
        if score > best_score:
            best_score = score
            best_ids = {campus_tea_id}
        elif score == best_score:
            best_ids.add(campus_tea_id)
    if best_score == 0 or len(best_ids) != 1:
        return None  # zero overlap, or ambiguous tie -- never guess
    return next(iter(best_ids))


def resolve_district_crosswalk(
    zones: list[dict[str, Any]],
    district_tea_id: str,
    tea_campus_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Returns one row per input zone, SAME ORDER, each the original zone
    dict plus "match_method" ("adapter" | "exact" | "token" | None) and
    "campus_tea_id" filled in where resolved. A zone that already carries
    a campus_tea_id (an adapter's own verified direct-ID field) is left
    untouched and tagged "adapter" -- this crosswalk only fills GAPS,
    never overrides an adapter's own resolution."""
    name_counts = Counter(
        (z["level"], _normalize_source_name(z["campus_name"])) for z in zones
    )
    duplicated_keys = {key for key, count in name_counts.items() if count > 1}

    results: list[dict[str, Any]] = []
    for zone in zones:
        if zone.get("campus_tea_id"):
            results.append({**zone, "match_method": "adapter"})
            continue

        level = zone["level"]
        campus_name = zone["campus_name"]
        key = (level, _normalize_source_name(campus_name))
        if key in duplicated_keys:
            results.append({**zone, "campus_tea_id": None, "match_method": None})
            continue

        candidates = _candidates_for(district_tea_id, level, tea_campus_index)
        zone_tokens = _crosswalk_tokens(campus_name)

        exact_hits = _tier1_exact_hits(zone_tokens, candidates)
        if len(exact_hits) == 1:
            results.append({**zone, "campus_tea_id": exact_hits[0], "match_method": "exact"})
            continue
        if len(exact_hits) > 1:
            # A tie -- >1 TEA candidate shares the identical token set.
            # Never guess; falls to null, not Tier 2.
            results.append({**zone, "campus_tea_id": None, "match_method": None})
            continue

        token_id = _tier2_token_overlap_match(zone_tokens, candidates)
        if token_id:
            results.append({**zone, "campus_tea_id": token_id, "match_method": "token"})
        else:
            results.append({**zone, "campus_tea_id": None, "match_method": None})

    # Bidirectional uniqueness (Fable's mandatory rule -- dropped in the spec
    # relay, added post-review 2026-07-21). If two zones resolve to the SAME
    # campus_tea_id -- even with DIFFERENT names -- a campus was consumed
    # twice, which is always a matching error; NULL all of them, flagged.
    # The name-based `duplicated_keys` guard above only catches identical
    # source names; this catches different names token-matching one campus
    # (confirmed gap: "Zavala Middle" + "Lorenzo Academy" both matching one
    # campus -> one would carry a wrong rating). A missing grade is free; a
    # wrong grade is the product failure. Do NOT null an adapter-verified
    # direct-ID match here -- only crosswalk-resolved (exact/token) ones.
    resolved_id_counts = Counter(
        r["campus_tea_id"]
        for r in results
        if r.get("campus_tea_id") and r.get("match_method") in ("exact", "token")
    )
    over_consumed = {cid for cid, n in resolved_id_counts.items() if n > 1}
    if over_consumed:
        for r in results:
            if r.get("campus_tea_id") in over_consumed and r.get("match_method") in ("exact", "token"):
                r["campus_tea_id"] = None
                r["match_method"] = None

    return results


def print_crosswalk_table(district_tea_id: str, results: list[dict[str, Any]]) -> None:
    """The printed table + method counts ARE the registry evidence -- a
    human eyeballs the whole table (15-45 rows per district), not just
    the residuals that failed to resolve."""
    print(f"[crosswalk] district {district_tea_id} -- {len(results)} zones")
    print(f"{'level':<12} {'campus_name':<38} {'campus_tea_id':<15} method")
    for r in results:
        print(f"{r['level']:<12} {str(r['campus_name'])[:38]:<38} {str(r['campus_tea_id']):<15} {r['match_method'] or 'null'}")
    counts = Counter(r["match_method"] or "null" for r in results)
    print(f"[crosswalk] method counts for district {district_tea_id}: {dict(counts)}")
