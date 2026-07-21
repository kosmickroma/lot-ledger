"""Tests for scripts/rollback_school_zones.sql.
See docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §7 (Gap 3) -- "written
before anything runs," a real committed file rather than a spec table row.
"""
from __future__ import annotations

import re
from pathlib import Path

SQL_PATH = Path(__file__).resolve().parent.parent / "scripts" / "rollback_school_zones.sql"


def _read() -> str:
    return SQL_PATH.read_text()


def test_file_exists() -> None:
    assert SQL_PATH.exists()


def test_drops_both_tables_if_exists() -> None:
    src = _read()
    assert "DROP TABLE IF EXISTS school_attendance_zones, school_campus_ratings" in src


def test_drops_the_restricted_role() -> None:
    src = _read()
    assert "DROP ROLE IF EXISTS school_zones_ingest" in src


def test_no_truncate_anywhere() -> None:
    assert "truncate" not in _read().lower()


def test_no_schema_mutating_alter_statement() -> None:
    # The file may legitimately DISCUSS rollback in comments without ever
    # issuing an ALTER statement -- assert no executable ALTER keyword.
    src = _read()
    assert not re.search(r"\balter\b", src, re.IGNORECASE)


def test_touches_only_school_zones_objects() -> None:
    src = _read()
    for other_table in ("parcels", "flood_zones", "saved_areas", "propelio_comps", "comp_ratings", "stored_value_entries"):
        assert other_table not in src
