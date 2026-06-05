"""tests/test_outreach_csv_leading_zero_match.py
Role: Regression guard for the Excel-mangled DCAD account_num matching
path. DCAD parcels are stored zero-padded to 17 digits
('00000424978000000') but Excel strips leading zeros when a user opens
+ re-saves the exported CSV. Mike bug 2026-06-05: the entire re-import
wrote nothing because the row went silently to the unmatched bucket.

Connects to: api/main.py import_parcel_outreach endpoint (the second-pass
UPDATE that LPADs numeric-only ids back to 17 digits for DCAD).
"""
from __future__ import annotations

import re
from pathlib import Path


MAIN_PY = Path(__file__).resolve().parent.parent / "api" / "main.py"


def _read() -> str:
    return MAIN_PY.read_text()


def test_dcad_leading_zero_lpad_pass_exists() -> None:
    """The import endpoint must include the DCAD-only LPAD-to-17 second pass
    so Excel-stripped DCAD account numbers still match."""
    src = _read()
    assert "LPAD(s.parcel_id, 17, '0')" in src, (
        "Missing DCAD-only zero-padding match path. Excel strips leading "
        "zeros from 17-digit DCAD account numbers when the user opens an "
        "exported CSV; the import must LPAD numeric-only ids back to 17 "
        "before matching against parcels.account_num."
    )


def test_lpad_pass_gated_to_numeric_ids() -> None:
    """The LPAD pass must only fire for purely-numeric parcel_ids so it
    doesn't accidentally mangle TAD ('NNNN:NNN'), Collin
    ('NNNN:R-...'), or other non-numeric formats."""
    src = _read()
    # The regex guard '^[0-9]+$' must appear in the same logical block as
    # the LPAD predicate. Use a multi-line tolerant pattern.
    pat = re.compile(
        r"LPAD\(s\.parcel_id, 17, '0'\).*?s\.parcel_id\s*~\s*'\^\[0-9\]\+\$'",
        re.DOTALL,
    )
    assert pat.search(src), (
        "LPAD pass must be guarded by ^[0-9]+$ regex so it only fires for "
        "purely-numeric parcel_ids (DCAD shape)."
    )


def test_lpad_pass_skips_already_matched_rows() -> None:
    """The LPAD pass must skip rows that were already matched in the main
    loop — otherwise we'd risk multi-county overwrites."""
    src = _read()
    pat = re.compile(
        r"LPAD\(s\.parcel_id, 17, '0'\).*?s\.matched_county\s+IS\s+NULL",
        re.DOTALL,
    )
    assert pat.search(src), (
        "LPAD pass must include 'AND s.matched_county IS NULL' so it never "
        "overwrites a row already resolved in the primary per-county loop."
    )


def test_lpad_pass_targets_dcad_only() -> None:
    """The LPAD pass must explicitly set matched_county='dcad'. The other
    counties don't suffer from Excel leading-zero stripping because their
    canonical PKs contain non-digit characters or aren't zero-padded."""
    src = _read()
    pat = re.compile(
        r"SET matched_county\s*=\s*'dcad'.*?LPAD\(s\.parcel_id, 17, '0'\)",
        re.DOTALL,
    )
    assert pat.search(src), (
        "LPAD pass must set matched_county='dcad' explicitly — TAD/Collin/"
        "Denton don't have this problem and shouldn't be affected."
    )
