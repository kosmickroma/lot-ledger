"""Pure Markdown-table parsing tests for scripts/school_zones_registry.py.
See docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §3a #3 (registry count
floor) and §6/zones_db.py (Dallas ISD-name -> TEA-id crosswalk).

No network, no DB. Uses tests/fixtures/school_zones/registry_sample.md (a
small excerpt matching the real registry's exact column format) plus the
real docs/AI/SCHOOL_ZONES_COVERAGE_REGISTRY.md file itself for the
absent-file-degrades-gracefully case.
"""
from __future__ import annotations

from pathlib import Path

from scripts.school_zones_registry import (
    dallas_isd_name_to_tea_id,
    expected_counts_for_district,
    load_registry,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "school_zones" / "registry_sample.md"


def test_load_registry_parses_expected_counts() -> None:
    reg = load_registry(FIXTURE)
    assert reg["057905"] == {"name": "DALLAS ISD", "elementary": 158, "middle": 39, "high": 42}
    assert reg["057909"] == {"name": "GARLAND ISD", "elementary": 46, "middle": 12, "high": 9}


def test_load_registry_skips_non_data_rows() -> None:
    reg = load_registry(FIXTURE)
    # header/separator rows never produce a 6-digit TEA# key
    assert all(len(k) == 6 and k.isdigit() for k in reg)


def test_load_registry_missing_file_returns_empty_dict() -> None:
    assert load_registry(Path("/no/such/file.md")) == {}


def test_expected_counts_for_district_known() -> None:
    assert expected_counts_for_district("057905", FIXTURE) == {
        "elementary": 158, "middle": 39, "high": 42,
    }


def test_expected_counts_for_district_unknown_returns_none() -> None:
    # None (not zero-counts) -- callers must skip the guard, never treat an
    # unknown district as "expected zero campuses."
    assert expected_counts_for_district("999999", FIXTURE) is None


def test_dallas_isd_name_to_tea_id_crosswalk() -> None:
    crosswalk = dallas_isd_name_to_tea_id(FIXTURE)
    assert crosswalk["DALLAS ISD"] == "057905"
    assert crosswalk["GARLAND ISD"] == "057909"


def test_dallas_isd_name_to_tea_id_excludes_other_counties() -> None:
    crosswalk = dallas_isd_name_to_tea_id(FIXTURE)
    assert "FORT WORTH ISD" not in crosswalk  # Tarrant County section in the fixture


def test_real_registry_file_parses_without_raising() -> None:
    # The actual docs/ file is gitignored -- this environment may or may not
    # have it. Either way must never raise; an absent file degrades to {}.
    reg = load_registry()
    assert isinstance(reg, dict)
