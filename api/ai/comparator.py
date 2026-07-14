# Task B — the comparator. See docs/AI/CODER_SPEC_FACTS_2026-07-14.md §B.
#
# A PURE FUNCTION: no DOM, no filter writes, no map.js globals. Data in,
# facts out. This + enrich_comps (Task A) is the Phase-2 grade engine's data
# layer — keep both free of UI concerns or the product's core gets built
# twice and the two copies drift.
from __future__ import annotations

from typing import Any

from api.geo import haversine_miles


def compare_to_subject(subject: dict[str, Any], comp: dict[str, Any]) -> dict[str, Any]:
    """{same_subdivision, same_isd, distance_mi} for one subject/comp pair.

    Both sides must already carry `cad_subdivision` + `isd` from the SAME
    `enrich_comps()` call (§A.2b) — plain equality only, no fuzzy matching,
    no abbreviation dictionary, no token similarity. If a normalizer looks
    necessary here, the enrichment is wrong, not the comparator (§B.1).
    """
    return {
        "same_subdivision": _compare_optional_eq(subject.get("cad_subdivision"), comp.get("cad_subdivision")),
        "same_isd": _compare_optional_eq(subject.get("isd"), comp.get("isd")),
        "distance_mi": _distance_mi(subject, comp),
    }


def _compare_optional_eq(subject_value: str | None, comp_value: str | None) -> bool | None:
    # §B.2 — unknown is not a mismatch. Missing on either side returns None,
    # never False: False renders as "different," a lie when the fact simply
    # isn't there (e.g. every Tarrant/Collin comp with no subdivision parse).
    if subject_value is None or comp_value is None:
        return None
    return subject_value == comp_value


def _distance_mi(subject: dict[str, Any], comp: dict[str, Any]) -> float | None:
    slat, slng = subject.get("lat"), subject.get("lng")
    clat, clng = comp.get("lat"), comp.get("lng")
    if slat is None or slng is None or clat is None or clng is None:
        return None
    return haversine_miles(float(slat), float(slng), float(clat), float(clng))
