"""Task A — enrich_comps() pure-logic tests (garbage rejection, ISD keying,
fail-open shape). See docs/AI/CODER_SPEC_FACTS_2026-07-14.md §A.

DB-touching paths (_enrich_dcad/_tad/_collin/_denton) are covered by
scripts/ai_enrichment_headless_report.py against the real corpus, per §B.3
("verify it headlessly before any UI consumes it") — not re-mocked here.
"""
from __future__ import annotations

from api.ai.enrichment import (
    _clean_subdivision,
    _is_garbage_subdivision,
    _qualify_isd,
    enrich_comps,
)


def test_garbage_rejects_blank() -> None:
    assert _is_garbage_subdivision("") is True


def test_garbage_rejects_purely_numeric() -> None:
    assert _is_garbage_subdivision("449") is True


def test_garbage_rejects_admin_first_words() -> None:
    for value in ["BLK 449", "LOT 12", "BLOCK 1/287", "TR 5A1", "TRACT 5", "ABST 123"]:
        assert _is_garbage_subdivision(value) is True, value


def test_garbage_does_not_reject_names_that_merely_start_with_the_same_letters() -> None:
    # A raw character-prefix check would wrongly reject real DFW subdivision
    # names for starting with "TR" or "LOT"-ish substrings. Must check the
    # first WORD only.
    for value in ["TRAVIS RANCH", "TRAILWOOD ESTATES", "TRINITY MEADOWS", "LOTUS ESTATES"]:
        assert _is_garbage_subdivision(value) is False, value


def test_garbage_does_not_reject_real_subdivision_name() -> None:
    assert _is_garbage_subdivision("BRYAN PLACE PHASE V SEC 1 & 2") is False


def test_clean_subdivision_returns_none_for_garbage() -> None:
    assert _clean_subdivision("BLK 449") is None


def test_clean_subdivision_returns_none_for_blank() -> None:
    assert _clean_subdivision(None) is None
    assert _clean_subdivision("") is None


def test_clean_subdivision_passes_through_real_name() -> None:
    assert _clean_subdivision("BRYAN PLACE PHASE V SEC 1 & 2") == "BRYAN PLACE PHASE V SEC 1 & 2"


def test_qualify_isd_county_prefixes_the_key() -> None:
    assert _qualify_isd("dcad", "HIGHLAND PARK ISD") == "dcad:HIGHLAND PARK ISD"
    assert _qualify_isd("tad", "905") == "tad:905"


def test_qualify_isd_none_when_blank() -> None:
    assert _qualify_isd("collin", None) is None
    assert _qualify_isd("collin", "") is None


def test_qualify_isd_keeps_same_raw_code_distinct_across_counties() -> None:
    # §A.3b — the exact hazard the corrected spec calls out: Tarrant "905"
    # and Collin "905" must never produce the same key.
    assert _qualify_isd("tad", "905") != _qualify_isd("collin", "905")


def test_enrich_comps_empty_account_list_returns_empty_dict_no_db_call() -> None:
    assert enrich_comps("dcad", []) == {}


def test_enrich_comps_unknown_county_fails_open_to_nulls_no_db_call() -> None:
    result = enrich_comps("harris", ["12345", "67890"])
    assert result == {
        "12345": {"cad_subdivision": None, "isd": None},
        "67890": {"cad_subdivision": None, "isd": None},
    }


def test_enrich_comps_dedupes_and_drops_falsy_account_nums_no_db_call() -> None:
    # Only exercises the pre-DB dedup/filter path — reaches an unknown county
    # so no real connection is attempted.
    result = enrich_comps("harris", ["A", "A", "", None])
    assert result == {"A": {"cad_subdivision": None, "isd": None}}
