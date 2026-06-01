"""Tests for the v2 distinct-owner distillation helpers.

See docs/superpowers/specs/2026-06-01-dcad-ownership-history-v2-design.md
for the full spec these tests verify against.
"""
import api.main as main


# ---------- _normalize_owner_name ----------

def test_normalize_blank_returns_empty():
    assert main._normalize_owner_name("") == ""
    assert main._normalize_owner_name("   ") == ""
    assert main._normalize_owner_name(None) == ""


def test_normalize_trim_upper_collapse_whitespace():
    assert main._normalize_owner_name("  Wilson  Lois ") == "WILSON LOIS"


def test_normalize_strips_punctuation():
    assert main._normalize_owner_name("Sumeer Homes, Inc.") == "SUMEER HOMES INC"
    assert main._normalize_owner_name("O'Reilly Auto Parts") == "OREILLY AUTO PARTS"
    assert main._normalize_owner_name("(GREENBRIER LLC)") == "GREENBRIER LLC"


def test_normalize_ampersand_to_and():
    assert main._normalize_owner_name("Smith & Jones") == "SMITH AND JONES"
    assert main._normalize_owner_name("Smith&Jones") == "SMITH AND JONES"
    assert main._normalize_owner_name("Smith and Jones") == "SMITH AND JONES"


def test_normalize_keeps_business_and_trust_suffixes():
    # LLC, INC, TR, TRUST, EST are real ownership flips per the spike memory.
    assert main._normalize_owner_name("WILSON LOIS TR") == "WILSON LOIS TR"
    assert main._normalize_owner_name("WILSON LOIS") != main._normalize_owner_name("WILSON LOIS TR")
    assert main._normalize_owner_name("ACME LLC") != main._normalize_owner_name("ACME")
    assert main._normalize_owner_name("SMITH FAMILY TRUST") != main._normalize_owner_name("SMITH FAMILY")


# ---------- _distill_distinct_owners ----------

def test_distill_empty_input():
    assert main._distill_distinct_owners({}) == []


def test_distill_all_blank_names():
    assert main._distill_distinct_owners({2021: "", 2022: "   ", 2023: None}) == []


def test_distill_single_year_single_owner():
    assert main._distill_distinct_owners({2023: "WILSON LOIS"}) == [("WILSON LOIS", 2023)]


def test_distill_single_owner_across_all_years():
    per_year = {y: "WILSON LOIS" for y in [2021, 2022, 2023, 2024, 2025]}
    assert main._distill_distinct_owners(per_year) == [("WILSON LOIS", 2021)]


def test_distill_two_owners_one_flip():
    per_year = {
        2021: "SUMEER HOMES INC",
        2022: "WILSON LOIS",
        2023: "WILSON LOIS",
        2024: "WILSON LOIS",
        2025: "WILSON LOIS",
    }
    assert main._distill_distinct_owners(per_year) == [
        ("WILSON LOIS", 2022),
        ("SUMEER HOMES INC", 2021),
    ]


def test_distill_five_distinct_owners_fills_all_slots():
    per_year = {2021: "A", 2022: "B", 2023: "C", 2024: "D", 2025: "E"}
    assert main._distill_distinct_owners(per_year) == [
        ("E", 2025),
        ("D", 2024),
        ("C", 2023),
        ("B", 2022),
        ("A", 2021),
    ]


def test_distill_six_plus_distinct_owners_returns_all_periods():
    # Helper itself returns ALL distinct periods; caller truncates to the
    # 5 slots that fit the CSV columns. Overflow handling is deferred.
    per_year = {
        1999: "A", 2003: "B", 2008: "C", 2013: "D",
        2018: "E", 2022: "F", 2025: "G",
    }
    result = main._distill_distinct_owners(per_year)
    assert [name for name, _ in result] == ["G", "F", "E", "D", "C", "B", "A"]
    assert [yr for _, yr in result] == [2025, 2022, 2018, 2013, 2008, 2003, 1999]


def test_distill_gap_year_splits_run():
    # 2024=A and 2022=A with no 2023 entry — gap breaks the run; two entries.
    # We can't know whether A actually owned through 2023 or whether the
    # 2023 record is just missing from our data, so we preserve correctness
    # by NOT merging.
    per_year = {2024: "A", 2022: "A"}
    assert main._distill_distinct_owners(per_year) == [
        ("A", 2024),
        ("A", 2022),
    ]


def test_distill_blank_middle_year_skipped_then_breaks_continuity():
    # Blank years are dropped first, so {2025:A, 2024:'', 2023:A} becomes
    # {2025:A, 2023:A}, which is NOT calendar-consecutive (the 2024 gap
    # remains a gap) → two entries for A. This is the locked behavior:
    # blank-skip happens BEFORE consecutive-year detection.
    per_year = {2025: "A", 2024: "", 2023: "A"}
    assert main._distill_distinct_owners(per_year) == [
        ("A", 2025),
        ("A", 2023),
    ]


def test_distill_latest_year_blank_anchor_walks_back():
    # 2025 blank, 2024 non-blank → anchor moves to 2024.
    per_year = {2025: "", 2024: "WILSON LOIS", 2023: "WILSON LOIS"}
    assert main._distill_distinct_owners(per_year) == [
        ("WILSON LOIS", 2023),
    ]


def test_distill_punctuation_variant_merges():
    # 'SUMEER HOMES, INC' and 'SUMEER HOMES INC' should merge.
    per_year = {2021: "SUMEER HOMES, INC", 2022: "SUMEER HOMES INC"}
    result = main._distill_distinct_owners(per_year)
    assert len(result) == 1
    # Display uses the latest year's stored form.
    assert result[0] == ("SUMEER HOMES INC", 2021)


def test_distill_ampersand_vs_and_merges():
    per_year = {2021: "SMITH & JONES", 2022: "SMITH AND JONES"}
    result = main._distill_distinct_owners(per_year)
    assert len(result) == 1
    assert result[0] == ("SMITH AND JONES", 2021)


def test_distill_trust_suffix_remains_distinct():
    # 'WILSON LOIS' → 'WILSON LOIS TR' is a real estate-planning flip per spike memory.
    per_year = {2022: "WILSON LOIS", 2023: "WILSON LOIS TR"}
    assert main._distill_distinct_owners(per_year) == [
        ("WILSON LOIS TR", 2023),
        ("WILSON LOIS", 2022),
    ]


def test_distill_inc_suffix_remains_distinct():
    # Same principle — adding INC is a real flip from individual to entity.
    per_year = {2021: "SUMEER HOMES", 2022: "SUMEER HOMES INC"}
    assert main._distill_distinct_owners(per_year) == [
        ("SUMEER HOMES INC", 2022),
        ("SUMEER HOMES", 2021),
    ]


def test_distill_display_name_is_latest_stored_form():
    # Same normalized owner with different stored spellings; display picks
    # the latest year's actual form.
    per_year = {2021: "wilson lois", 2022: "WILSON  LOIS", 2023: "Wilson Lois"}
    result = main._distill_distinct_owners(per_year)
    assert len(result) == 1
    assert result[0] == ("Wilson Lois", 2021)


# ---------- _is_owner_history_supported ----------
# Renamed from _is_dcad_source 2026-06-01 when Collin + Denton ingests landed.
# The supported set widens as new counties' rolls get loaded into
# ownership_snapshots; TAD is the next addition.

def test_is_owner_history_supported_dcad_aliases():
    assert main._is_owner_history_supported("DCAD") is True
    assert main._is_owner_history_supported("dcad") is True
    assert main._is_owner_history_supported("Dallas") is True
    assert main._is_owner_history_supported("dallas") is True
    assert main._is_owner_history_supported("  DCAD  ") is True


def test_is_owner_history_supported_collin_and_denton():
    """Collin + Denton joined the supported set on 2026-06-01 once their
    certified rolls were ingested into ownership_snapshots."""
    assert main._is_owner_history_supported("collin") is True
    assert main._is_owner_history_supported("Collin") is True
    assert main._is_owner_history_supported("denton") is True
    assert main._is_owner_history_supported("Denton") is True
    assert main._is_owner_history_supported("  collin  ") is True


def test_is_owner_history_supported_pending_counties():
    """TAD/Tarrant is still pending ingest — gate must keep returning False
    so the CSV emits blank cells rather than misleading '(None)' values."""
    assert main._is_owner_history_supported("Tarrant") is False
    assert main._is_owner_history_supported("TAD") is False
    assert main._is_owner_history_supported("tad") is False


def test_is_owner_history_supported_blank_and_none():
    assert main._is_owner_history_supported("") is False
    assert main._is_owner_history_supported(None) is False
    assert main._is_owner_history_supported("  ") is False
