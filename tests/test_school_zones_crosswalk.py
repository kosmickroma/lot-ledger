"""Tests for scripts/school_zones_crosswalk.py -- the generalized zone->
TEA-rating crosswalk (multi-district ratings foundation, Part B). See that
module's own docstring for why the tier structure here is a documented
RECONSTRUCTION (the spec text arrived truncated twice) rather than a
literal spec quote past "Tier 1 -- exact-normalized match... stopword-strip
both sides (EL/ELEMENTARY/MIDDLE/JUNIOR/H S/HIGH/SC[HOOL?])."

Pure-function tests, no DB, no network -- covers the acceptance criteria's
5 named scenarios (exact hit, token hit, tie->null, duplicate->both null,
no-match->null) plus the adapter-already-resolved passthrough and the
district/TEA-School-Type scoping itself.
"""
from __future__ import annotations

from scripts.school_zones_crosswalk import print_crosswalk_table, resolve_district_crosswalk


def _zone(level="elementary", campus_name="X Elementary", campus_tea_id=None):
    return {
        "level": level, "district_tea_id": "057912", "district_name": "IRVING ISD",
        "campus_tea_id": campus_tea_id, "campus_name": campus_name,
        "geom": {"type": "MultiPolygon", "coordinates": []},
        "boundary_vintage": "2025-26", "source_url": None, "source_kind": "arcgis",
        "retrieved_at": None,
    }


def _candidate(name, school_type="Elementary", district_number="057912"):
    return {"name": name, "type": school_type, "district_number": district_number}


# --- exact hit -----------------------------------------------------------------

def test_exact_hit() -> None:
    zones = [_zone(campus_name="Bowie Elementary")]
    index = {"057912101": _candidate("Bowie Elementary School")}
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] == "057912101"
    assert results[0]["match_method"] == "exact"


def test_exact_hit_is_stopword_insensitive_both_sides() -> None:
    zones = [_zone(campus_name="Travis El")]
    index = {"057912050": _candidate("Travis Elementary")}
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] == "057912050"
    assert results[0]["match_method"] == "exact"


def test_exact_hit_strips_junior_extra_stopword() -> None:
    # "JUNIOR" is the one word the surviving spec fragment explicitly
    # named that isn't already in build_school_pilot_data's _STOPWORDS.
    zones = [_zone(level="middle", campus_name="Travis Junior High")]
    index = {"057912060": _candidate("Travis Middle", school_type="Middle School")}
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] == "057912060"
    assert results[0]["match_method"] == "exact"


# --- token hit -------------------------------------------------------------------

def test_token_hit_when_no_exact_match() -> None:
    # "Lively Elem" tokenizes to {"LIVELY"} (ELEM is a stopword). The real
    # TEA name carries extra non-stopword tokens (the honoree's full name),
    # so the token sets are NOT equal -- Tier 1 finds nothing -- but Tier 2's
    # overlap score (LIVELY, shared) is nonzero and uniquely best.
    zones = [_zone(campus_name="Lively Elem")]
    index = {
        "057912102": _candidate("Kathlyn Joy Gilbert Lively Elementary School"),
        "057912999": _candidate("Farine Elementary School"),  # unrelated, zero overlap
    }
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] == "057912102"
    assert results[0]["match_method"] == "token"


# --- tie -> null -----------------------------------------------------------------

def test_tier1_exact_tie_resolves_to_null() -> None:
    # Two DIFFERENT TEA campuses share the identical token set as the zone
    # -- a real, if rare, ambiguity. Never guess.
    zones = [_zone(campus_name="North Elementary")]
    index = {
        "057912201": _candidate("North Elementary"),
        "057912202": _candidate("North Elementary"),  # genuine duplicate TEA-side name
    }
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] is None
    assert results[0]["match_method"] is None


def test_tier2_token_overlap_tie_resolves_to_null() -> None:
    zones = [_zone(campus_name="Park Elementary")]
    index = {
        "057912301": _candidate("Central Park Elementary"),
        "057912302": _candidate("Forest Park Elementary"),
    }
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] is None
    assert results[0]["match_method"] is None


# --- duplicate source name -> both null -------------------------------------------

def test_duplicate_source_campus_name_resolves_both_to_null() -> None:
    # The district's OWN zone list has "Travis Elementary" twice at the
    # same level -- a source-data quality issue, not a TEA ambiguity.
    # Neither should silently claim the one real TEA campus.
    zones = [_zone(campus_name="Travis Elementary"), _zone(campus_name="Travis Elementary")]
    index = {"057912050": _candidate("Travis Elementary")}
    results = resolve_district_crosswalk(zones, "057912", index)
    assert all(r["campus_tea_id"] is None for r in results)
    assert all(r["match_method"] is None for r in results)


def test_duplicate_guard_is_case_and_whitespace_insensitive() -> None:
    zones = [_zone(campus_name="Travis Elementary"), _zone(campus_name="  travis   elementary  ")]
    index = {"057912050": _candidate("Travis Elementary")}
    results = resolve_district_crosswalk(zones, "057912", index)
    assert all(r["campus_tea_id"] is None for r in results)


def test_duplicate_guard_is_scoped_per_level() -> None:
    # Same campus_name at DIFFERENT levels is not a duplicate -- a
    # district can legitimately reuse a place name across elementary/
    # middle (e.g. "Travis" the neighborhood, two different buildings).
    zones = [_zone(level="elementary", campus_name="Travis"), _zone(level="middle", campus_name="Travis")]
    index = {
        "057912050": _candidate("Travis Elementary", school_type="Elementary"),
        "057912060": _candidate("Travis Middle", school_type="Middle School"),
    }
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] == "057912050"
    assert results[1]["campus_tea_id"] == "057912060"


# --- no match -> null --------------------------------------------------------------

def test_no_match_resolves_to_null() -> None:
    zones = [_zone(campus_name="Nonexistent Elementary")]
    index = {"057912999": _candidate("Farine Elementary")}
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] is None
    assert results[0]["match_method"] is None


def test_no_candidates_in_scope_resolves_to_null() -> None:
    zones = [_zone(campus_name="Bowie Elementary")]
    index = {"057905101": _candidate("Bowie Elementary", district_number="057905")}  # different district
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] is None


# --- adapter-already-resolved passthrough -------------------------------------------

def test_adapter_resolved_campus_id_is_never_overridden() -> None:
    zones = [_zone(campus_name="Bowie Elementary", campus_tea_id="057912999")]
    index = {"057912101": _candidate("Bowie Elementary")}  # a DIFFERENT id the crosswalk would otherwise pick
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] == "057912999"  # adapter's own ID wins
    assert results[0]["match_method"] == "adapter"


# --- district + TEA School Type scoping ----------------------------------------------

def test_scoping_by_district_excludes_other_districts_same_name() -> None:
    zones = [_zone(campus_name="Bowie Elementary")]
    index = {
        "057905101": _candidate("Bowie Elementary", district_number="057905"),  # Dallas ISD -- wrong district
        "057912101": _candidate("Bowie Elementary", district_number="057912"),  # Irving ISD -- correct district
    }
    # Both have the identical name -- but only ONE is in scope for district
    # 057912, so this must be an exact hit, not a tie.
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] == "057912101"
    assert results[0]["match_method"] == "exact"


def test_scoping_by_school_type_excludes_wrong_level() -> None:
    zones = [_zone(level="elementary", campus_name="Bowie")]
    index = {
        "057912050": _candidate("Bowie", school_type="Middle School"),  # same name, wrong level
    }
    results = resolve_district_crosswalk(zones, "057912", index)
    assert results[0]["campus_tea_id"] is None


# --- output shape / ordering --------------------------------------------------------

def test_results_preserve_input_order_and_count() -> None:
    zones = [_zone(campus_name="A Elementary"), _zone(campus_name="B Elementary"), _zone(campus_name="C Elementary")]
    index = {"1": _candidate("A Elementary"), "2": _candidate("B Elementary")}
    results = resolve_district_crosswalk(zones, "057912", index)
    assert [r["campus_name"] for r in results] == ["A Elementary", "B Elementary", "C Elementary"]


def test_print_crosswalk_table_does_not_raise(capsys) -> None:
    zones = [_zone(campus_name="Bowie Elementary"), _zone(campus_name="Nonexistent")]
    index = {"057912101": _candidate("Bowie Elementary")}
    results = resolve_district_crosswalk(zones, "057912", index)
    print_crosswalk_table("057912", results)
    out = capsys.readouterr().out
    assert "057912" in out
    assert "method counts" in out


def test_bidirectional_uniqueness_two_different_names_same_campus_both_null():
    """Fable's mandatory rule (dropped in spec relay, added post-review):
    two DIFFERENTLY-named zones token-matching the SAME TEA campus = a
    matching error -> both NULL, never a wrong rating on one."""
    from scripts.school_zones_crosswalk import resolve_district_crosswalk

    tea = {"099001001": {"name": "LORENZO DE ZAVALA MIDDLE", "type": "Middle School", "district_number": "099001"}}
    zones = [
        {"level": "middle", "campus_name": "Zavala Middle"},
        {"level": "middle", "campus_name": "Lorenzo Academy"},
    ]
    res = resolve_district_crosswalk(zones, "099001", tea)
    assert all(r["campus_tea_id"] is None and r["match_method"] is None for r in res)


def test_bidirectional_uniqueness_does_not_over_null_clean_1to1_matches():
    from scripts.school_zones_crosswalk import resolve_district_crosswalk

    tea = {
        "099001001": {"name": "LINCOLN MIDDLE", "type": "Middle School", "district_number": "099001"},
        "099001002": {"name": "ROOSEVELT MIDDLE", "type": "Middle School", "district_number": "099001"},
    }
    zones = [
        {"level": "middle", "campus_name": "Lincoln Middle"},
        {"level": "middle", "campus_name": "Roosevelt Middle"},
    ]
    res = resolve_district_crosswalk(zones, "099001", tea)
    assert res[0]["campus_tea_id"] == "099001001"
    assert res[1]["campus_tea_id"] == "099001002"
