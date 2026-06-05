"""tests/test_outreach_csv_empty_cell_semantics.py
Role: Regression guard for "empty cell = no change" semantics on outreach
CSV imports.

Mike bug 2026-06-05 (post-LPAD-fix): the LPAD-to-17 leading-zero fix made
parcel 424978000000 match correctly, but the import still wrote False/NULL
for contact_info_retrieved/mailer_date even though Mike's CSV cell had
"Yes" / "6/4/2026". Root cause: Mike's saved-area CSV duplicates each
parcel (once in the parcel section with outreach cells filled in, once
in the comp section with cells empty). The old dedup picked the
higher-row_idx row (comp, empty), and the empty cells parsed as False/
NULL, silently overwriting Mike's entries.

Three coordinated fixes were applied; this file asserts each:
  1. _parse_outreach_yes_no_cell("") returns None (was: False).
  2. Per-row *_set flags computed from cell content, not column existence.
  3. Dedup ORDER BY prefers rows with the most set flags before falling
     back to row_idx DESC, so the data-bearing row wins.
  4. UPSERT uses LEFT JOIN existing to preserve untouched fields without
     CASE/EXCLUDED gymnastics.

Connects to: api/main.py:_parse_outreach_yes_no_cell + import_parcel_outreach
"""
from __future__ import annotations

import re
from pathlib import Path

from api.main import _parse_outreach_yes_no_cell


MAIN_PY = Path(__file__).resolve().parent.parent / "api" / "main.py"


def _read() -> str:
    return MAIN_PY.read_text()


def test_yes_no_empty_string_returns_none() -> None:
    """Empty cell must mean 'no change', not 'clear to False'. Mike's
    saved-area CSV duplicates each parcel — one row has the Yes filled
    in, the other (a comp row) has it empty. Empty-as-False silently
    wiped Mike's data on dedup."""
    assert _parse_outreach_yes_no_cell("") is None
    assert _parse_outreach_yes_no_cell(None) is None
    assert _parse_outreach_yes_no_cell("   ") is None


def test_yes_no_explicit_clear_still_works() -> None:
    """Explicit 'no'/'false'/'0' must still mean 'clear to False' so Mike
    can intentionally un-mark a parcel via CSV if he ever needs to."""
    assert _parse_outreach_yes_no_cell("no") is False
    assert _parse_outreach_yes_no_cell("No") is False
    assert _parse_outreach_yes_no_cell("false") is False
    assert _parse_outreach_yes_no_cell("0") is False
    assert _parse_outreach_yes_no_cell("not contacted") is False


def test_yes_no_truthy_still_works() -> None:
    """'Yes'/'true'/'1' etc. must still mean True."""
    assert _parse_outreach_yes_no_cell("Yes") is True
    assert _parse_outreach_yes_no_cell("yes") is True
    assert _parse_outreach_yes_no_cell("Y") is True
    assert _parse_outreach_yes_no_cell("true") is True
    assert _parse_outreach_yes_no_cell("1") is True


def test_staging_uses_per_row_set_flags() -> None:
    """The staging INSERT must compute *_set flags from per-row cell
    content (cir is not None / md is not None), NOT from whether the
    CSV column exists. A column-level flag plus row_idx-DESC dedup is
    what caused the original silent overwrite."""
    src = _read()
    # Should have the per-row computation in the execute_values argument list
    pat = re.compile(
        r"execute_values\(.*?contact_info_retrieved_set, mailer_date_set\).*?"
        r"cir is not None,\s*md is not None,",
        re.DOTALL,
    )
    assert pat.search(src), (
        "Per-row *_set flag computation missing. Staging INSERT must use "
        "'cir is not None' / 'md is not None' so empty cells get set=False "
        "(no change) instead of set=True (clear to False)."
    )


def test_dedup_prefers_most_set_flags_first() -> None:
    """The DISTINCT ON dedup must ORDER BY (set_count DESC, row_idx DESC)
    so the parcel-section row (with cells filled in) wins over the
    comp-section row (empty cells) for the same parcel."""
    src = _read()
    pat = re.compile(
        r"DISTINCT ON \(s\.matched_county, s\.parcel_id\).*?"
        r"ORDER BY s\.matched_county, s\.parcel_id,\s*"
        r"\(s\.contact_info_retrieved_set::int \+ s\.mailer_date_set::int\) DESC,\s*"
        r"s\.row_idx DESC",
        re.DOTALL,
    )
    assert pat.search(src), (
        "Dedup ORDER BY must prioritize rows with the most set flags "
        "before falling back to row_idx DESC. Old logic picked empty "
        "comp-section rows over data-bearing parcel-section rows."
    )


def test_dedup_skips_no_op_rows() -> None:
    """Rows where neither flag is set must be skipped entirely so a CSV
    row with all-empty outreach cells doesn't touch parcel_outreach_notes
    (no creation of empty rows, no last_updated_at churn)."""
    src = _read()
    pat = re.compile(
        r"AND \(s\.contact_info_retrieved_set OR s\.mailer_date_set\)",
        re.DOTALL,
    )
    assert pat.search(src), (
        "Dedup CTE must filter out rows with no set flags. Otherwise the "
        "UPSERT churns last_updated_at on every parcel in the CSV even "
        "when the import didn't actually change anything for that parcel."
    )


def test_upsert_uses_left_join_existing_pattern() -> None:
    """The UPSERT must use a LEFT JOIN to fetch existing values and
    compute the target value in the SELECT, rather than the previous
    CASE/subquery-in-ON-CONFLICT pattern (which could pick the wrong
    staging row when multiple rows existed per parcel)."""
    src = _read()
    # The CTE name + JOIN clause are the structural fingerprint
    assert "existing AS (" in src, "Missing existing CTE for fetching current values"
    assert re.search(
        r"LEFT JOIN existing e ON e\.county = d\.matched_county",
        src,
    ), "Missing LEFT JOIN to existing values in the INSERT SELECT"
    # The CASE expressions must use d.*_set (the picked deduped row's flag)
    # and fall back to e.old_cir / e.old_md
    assert "COALESCE(e.old_cir, false)" in src, (
        "Missing fallback to existing contact_info_retrieved when not set in CSV"
    )
    assert "ELSE e.old_md END" in src, (
        "Missing fallback to existing mailer_date when not set in CSV"
    )


def test_on_conflict_no_longer_has_set_subquery() -> None:
    """The previous CASE WHEN (SELECT s2.contact_info_retrieved_set FROM
    outreach_staging s2 ... LIMIT 1) THEN EXCLUDED... pattern is now
    obsolete and must be removed — the set-flag logic moved into the
    SELECT FROM deduped d block."""
    src = _read()
    # The unordered LIMIT 1 subquery was the source of pick-wrong-row bugs
    assert "SELECT s2.contact_info_retrieved_set FROM outreach_staging s2" not in src, (
        "Stale ON CONFLICT set-flag subquery still present — must be removed."
    )
    assert "SELECT s2.mailer_date_set FROM outreach_staging s2" not in src, (
        "Stale ON CONFLICT mailer_date set-flag subquery still present."
    )
