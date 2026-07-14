"""Task B — compare_to_subject() pure-function tests.

See docs/AI/CODER_SPEC_FACTS_2026-07-14.md §B. No DB, no DOM — pure data in,
facts out.
"""
from __future__ import annotations

from api.ai.comparator import compare_to_subject


def _pair(subject_extra=None, comp_extra=None):
    subject = {"cad_subdivision": "HIGHLAND PARK", "isd": "dcad:HIGHLAND PARK ISD", "lat": 32.8, "lng": -96.8}
    comp = {"cad_subdivision": "HIGHLAND PARK", "isd": "dcad:HIGHLAND PARK ISD", "lat": 32.81, "lng": -96.81}
    subject.update(subject_extra or {})
    comp.update(comp_extra or {})
    return subject, comp


def test_same_subdivision_true_on_exact_match() -> None:
    subject, comp = _pair()
    assert compare_to_subject(subject, comp)["same_subdivision"] is True


def test_same_subdivision_false_on_different_names() -> None:
    subject, comp = _pair(comp_extra={"cad_subdivision": "PRESTON HOLLOW"})
    assert compare_to_subject(subject, comp)["same_subdivision"] is False


def test_same_subdivision_no_fuzzy_matching() -> None:
    # Plain equality only — a near-miss (extra whitespace-normalized token,
    # abbreviation, case) must NOT count as a match. §B.1.
    subject, comp = _pair(
        subject_extra={"cad_subdivision": "HIGHLAND PARK"},
        comp_extra={"cad_subdivision": "Highland Park"},
    )
    assert compare_to_subject(subject, comp)["same_subdivision"] is False


def test_same_subdivision_null_when_subject_missing() -> None:
    subject, comp = _pair(subject_extra={"cad_subdivision": None})
    assert compare_to_subject(subject, comp)["same_subdivision"] is None


def test_same_subdivision_null_when_comp_missing() -> None:
    subject, comp = _pair(comp_extra={"cad_subdivision": None})
    assert compare_to_subject(subject, comp)["same_subdivision"] is None


def test_same_subdivision_null_never_false_on_unknown() -> None:
    # §B.2 — unknown must never render as a mismatch.
    subject, comp = _pair(subject_extra={"cad_subdivision": None}, comp_extra={"cad_subdivision": None})
    result = compare_to_subject(subject, comp)["same_subdivision"]
    assert result is None
    assert result is not False


def test_same_isd_true_on_exact_county_qualified_match() -> None:
    subject, comp = _pair()
    assert compare_to_subject(subject, comp)["same_isd"] is True


def test_same_isd_false_across_different_districts_same_county() -> None:
    subject, comp = _pair(comp_extra={"isd": "dcad:PLANO ISD"})
    assert compare_to_subject(subject, comp)["same_isd"] is False


def test_same_isd_false_across_counties_with_colliding_raw_codes() -> None:
    # §A.3b — Tarrant "905" and Collin "905" are different districts. The
    # county-qualified key must keep them from matching.
    subject, comp = _pair(subject_extra={"isd": "tad:905"}, comp_extra={"isd": "collin:905"})
    assert compare_to_subject(subject, comp)["same_isd"] is False


def test_same_isd_null_when_either_side_missing() -> None:
    subject, comp = _pair(subject_extra={"isd": None})
    assert compare_to_subject(subject, comp)["same_isd"] is None
    subject, comp = _pair(comp_extra={"isd": None})
    assert compare_to_subject(subject, comp)["same_isd"] is None


def test_distance_mi_computed_from_lat_lng() -> None:
    subject, comp = _pair()
    distance = compare_to_subject(subject, comp)["distance_mi"]
    assert distance is not None
    assert 0 < distance < 5  # these two points are close together


def test_distance_mi_null_when_either_side_missing_coords() -> None:
    subject, comp = _pair(subject_extra={"lat": None})
    assert compare_to_subject(subject, comp)["distance_mi"] is None
    subject, comp = _pair(comp_extra={"lng": None})
    assert compare_to_subject(subject, comp)["distance_mi"] is None


def test_price_never_enters_the_comparator() -> None:
    # §2.3 — price must never enter the comparator, sort, or tie-break, not
    # even implicitly. Adding a price field to either side must not change
    # any fact.
    subject, comp = _pair()
    baseline = compare_to_subject(subject, comp)
    subject["price"] = 100_000
    comp["price"] = 900_000
    assert compare_to_subject(subject, comp) == baseline
