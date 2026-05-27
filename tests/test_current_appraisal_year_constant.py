"""Verifies CURRENT_APPRAISAL_YEAR is wired in api.config."""
from api.config import CURRENT_APPRAISAL_YEAR


def test_current_appraisal_year_is_int():
    assert isinstance(CURRENT_APPRAISAL_YEAR, int)


def test_current_appraisal_year_is_2026():
    # Locked to 2026 per spec v4a.2 §2.6 — config-driven only, no MAX() drift.
    assert CURRENT_APPRAISAL_YEAR == 2026
