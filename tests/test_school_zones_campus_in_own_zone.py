"""Safeguard 3 stub tests -- docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md
"recurring-cadence safeguards." verify_campus_in_own_zone() is honestly
blocked on campus point-location data (NCES/TEA) this repo has not
ingested -- see scripts/verify_school_zones_campus_in_own_zone.py's module
docstring. These tests exist so the blocker stays VISIBLE in test output
(a skip with a reason, plus a passing test proving today's honest
NotImplementedError behavior) rather than silently absent from the suite.
"""
from __future__ import annotations

import pytest

from scripts.verify_school_zones_campus_in_own_zone import verify_campus_in_own_zone


@pytest.mark.skip(
    reason="Safeguard 3 blocked on campus point-location data (NCES/TEA) -- "
    "not yet ingested into this repo; do not fake with invented "
    "coordinates. See verify_campus_in_own_zone()'s docstring for the "
    "proposed NCES Common Core of Data approach + required crosswalk."
)
def test_campus_in_own_zone_real_check_not_yet_implemented() -> None:
    """Placeholder for the real assertion once campus points are sourced:
    verify_campus_in_own_zone(conn) == [] against a rehearsal DB seeded
    with correct campus points, and non-empty when a zone is deliberately
    mislabeled. Replace this test's body (not just delete the skip) when
    that data lands."""
    raise AssertionError("replace this test once campus point-location data is sourced")


def test_campus_in_own_zone_raises_clearly_until_unblocked() -> None:
    # Not skipped -- documents TODAY's honest behavior: calling this
    # raises NotImplementedError naming the real blocker, never a silent
    # no-op and never a fabricated pass.
    with pytest.raises(NotImplementedError, match="campus point-location data"):
        verify_campus_in_own_zone(conn=None)
