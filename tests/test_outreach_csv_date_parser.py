"""tests/test_outreach_csv_date_parser.py
Role: Regression guard for _parse_outreach_csv_date. Mike re-imports
CSVs that Excel has silently reformatted from ISO YYYY-MM-DD into
US M/D/YYYY. The strict _parse_iso_date used to silently drop those
dates → mailer_date was being written as NULL.

Connects to: api/main.py:_parse_outreach_csv_date
"""
from __future__ import annotations

import datetime as date_module

from api.main import _parse_outreach_csv_date


def test_iso_format_round_trips() -> None:
    """LotLedger's own export format must still work."""
    assert _parse_outreach_csv_date("2026-06-04") == date_module.date(2026, 6, 4)


def test_excel_us_format_single_digit_month_and_day() -> None:
    """Mike bug 2026-06-05: Excel auto-formats ISO into M/D/YYYY when the
    user opens + saves the export. Must accept the mangled form."""
    assert _parse_outreach_csv_date("6/4/2026") == date_module.date(2026, 6, 4)


def test_excel_us_format_zero_padded() -> None:
    """Excel sometimes emits MM/DD/YYYY when the cell was explicitly
    formatted as a date."""
    assert _parse_outreach_csv_date("06/04/2026") == date_module.date(2026, 6, 4)


def test_iso_with_slashes() -> None:
    """Defensive: some operating systems / locale settings produce
    YYYY/MM/DD when Excel re-saves."""
    assert _parse_outreach_csv_date("2026/06/04") == date_module.date(2026, 6, 4)


def test_end_of_month() -> None:
    """Edge: M/D/YYYY with values requiring two-digit interpretation."""
    assert _parse_outreach_csv_date("12/31/2025") == date_module.date(2025, 12, 31)


def test_january_first() -> None:
    assert _parse_outreach_csv_date("1/1/2026") == date_module.date(2026, 1, 1)


def test_empty_returns_none() -> None:
    """Empty cells must return None — the *_set flags decide whether the
    UPSERT writes NULL ('explicit clear') vs. preserves the existing value
    ('no change')."""
    assert _parse_outreach_csv_date("") is None
    assert _parse_outreach_csv_date(None) is None
    assert _parse_outreach_csv_date("   ") is None


def test_unparseable_returns_none_not_raise() -> None:
    """Garbage input must not crash the import loop; return None so the
    row gets dropped from the staging table cleanly."""
    assert _parse_outreach_csv_date("not a date") is None
    assert _parse_outreach_csv_date("13/45/2026") is None
    assert _parse_outreach_csv_date("2026") is None
    assert _parse_outreach_csv_date("June 4, 2026") is None


def test_invalid_month_day_ranges() -> None:
    """Out-of-range components must return None (no silent rollover)."""
    assert _parse_outreach_csv_date("13/1/2026") is None
    assert _parse_outreach_csv_date("0/4/2026") is None
    assert _parse_outreach_csv_date("6/32/2026") is None
    assert _parse_outreach_csv_date("6/0/2026") is None


def test_two_digit_year_rejected() -> None:
    """Excel sometimes emits MM/DD/YY. Refuse to guess the century; return
    None so Mike sees the row gets cleared rather than mis-dated to year
    0026 or 2026 ambiguously."""
    assert _parse_outreach_csv_date("6/4/26") is None


def test_iso_format_with_invalid_calendar_date() -> None:
    """Feb 30 etc. — ISO parser already rejects; confirm we don't fall
    through into the M/D/YYYY branch and accidentally accept."""
    assert _parse_outreach_csv_date("2026-02-30") is None
    assert _parse_outreach_csv_date("2/30/2026") is None
