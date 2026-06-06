"""tests/test_duplexes_default_on_migration.py
Role: Regression guard for KK's "Duplexes ON by default" follow-up
2026-06-06.

Earlier today we set DEFAULT_FILTERS.duplexes = true and added the
`checked` attribute to the duplexes HTML input. Both shipped. But
existing users' localStorage at FILTER_STORAGE_KEY = "lotledger.map.
filters.v1" still has `duplexes: false` from the previous default.
loadFilters does `{ ...DEFAULT_FILTERS, ...parsed }` so the stored
false wins on every load — defeating the new default.

Fix: one-shot migration. On first load after this change, force-apply
DEFAULT_FILTERS.duplexes and set a marker in localStorage. After the
marker exists, future loads honor whatever the user has explicitly
chosen — Mike's not stuck with duplexes always ON if he turns it off
later.

Connects to: frontend/map.js loadFilters
"""
from __future__ import annotations

import re
from pathlib import Path


MAP_JS = Path(__file__).resolve().parent.parent / "frontend" / "map.js"


def _read() -> str:
    return MAP_JS.read_text()


def test_migration_marker_constant_defined() -> None:
    src = _read()
    assert (
        'const FILTER_DUPLEXES_DEFAULT_ON_MIGRATION_KEY = '
        '"lotledger.map.filters.v1.duplexes_default_on_migration"'
    ) in src, (
        "Migration marker constant must be defined so the migration "
        "runs at most once per browser."
    )


def test_migration_runs_only_when_marker_absent() -> None:
    """The migration must be GATED on the marker check — otherwise it
    would run every load and override a user's explicit off toggle."""
    src = _read()
    pat = re.compile(
        r"if \(!localStorage\.getItem\(FILTER_DUPLEXES_DEFAULT_ON_MIGRATION_KEY\)\) \{\s*"
        r"parsed\.duplexes = DEFAULT_FILTERS\.duplexes;\s*"
        r'localStorage\.setItem\(FILTER_DUPLEXES_DEFAULT_ON_MIGRATION_KEY, "1"\);',
        re.DOTALL,
    )
    assert pat.search(src), (
        "Migration must (a) check the marker, (b) only apply the new "
        "default when missing, (c) set the marker after applying."
    )


def test_migration_inside_try_block_so_localstorage_failure_is_nonblocking() -> None:
    """If localStorage.setItem throws (private mode, quota), the
    migration must not nuke the rest of loadFilters."""
    src = _read()
    # The marker manipulation should be inside a try/catch (the inner one
    # within loadFilters).
    pat = re.compile(
        r"try \{\s*"
        r"if \(!localStorage\.getItem\(FILTER_DUPLEXES_DEFAULT_ON_MIGRATION_KEY\)\).*?"
        r"\} catch \(_\) \{ /\* fall through — non-blocking \*/ \}",
        re.DOTALL,
    )
    assert pat.search(src), (
        "Migration must be wrapped in a try/catch so localStorage errors "
        "in private mode or under quota don't break loadFilters."
    )


def test_migration_runs_before_spread_into_filterstate() -> None:
    """If the migration mutates `parsed` AFTER the spread into filterState,
    the new default wouldn't actually land. Verify ordering."""
    src = _read()
    pat = re.compile(
        r"parsed\.duplexes = DEFAULT_FILTERS\.duplexes;.*?"
        r"filterState = \{ \.\.\.DEFAULT_FILTERS, \.\.\.parsed \};",
        re.DOTALL,
    )
    assert pat.search(src), (
        "Migration must mutate `parsed` BEFORE the spread into filterState "
        "so the new default actually applies."
    )


def test_default_filters_duplexes_still_true() -> None:
    """The migration is meaningless if DEFAULT_FILTERS.duplexes isn't
    actually true. Guard against accidental revert."""
    src = _read()
    pat = re.compile(
        r"const DEFAULT_FILTERS = \{.*?"
        r"duplexes: true,.*?"
        r"\};",
        re.DOTALL,
    )
    assert pat.search(src), (
        "DEFAULT_FILTERS.duplexes must be true for the migration to do "
        "the right thing."
    )
