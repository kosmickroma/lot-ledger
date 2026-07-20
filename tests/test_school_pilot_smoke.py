"""CRS/geometry smoke gate — docs/AI/SCHOOL_RATINGS_PILOT_PLAN_2026-07-20.md
Task 7 / spec §6.3. Skips if data/school_pilot/ is absent (not baked, or
this environment never ran scripts/build_school_pilot_data.py).

Methodology: rather than "publicly known school assignments" for arbitrary
residential addresses (hard to verify without an interactive locator tool),
each case is a DISD campus's OWN physical street address, independently
geocoded via the free US Census Bureau geocoder (geocoding.geo.census.gov —
a federal data source wholly unrelated to DISD's attendance-zone
FeatureServers), then checked against api/school_pilot/zones.assign(). A
school's own building is virtually always inside its own attendance zone, so
"does this independently-geocoded point resolve to the SAME school" is a
strong, verifiable CRS/geometry correctness check: a lon/lat swap, a wrong
CRS, or a broken point-in-polygon would put these points in the WRONG zone
(or no zone) even though the coordinates are unambiguously real and correct.

Addresses + the TEA campus IDs were cross-verified independently against
HAR.com's per-campus listing pages (which key by TEA CAMPUS_ID in the URL,
e.g. har.com/school/057905022/woodrow-wilson-high-school) — those IDs match
this build's derive_tea_campus_id() output exactly (057905 + zero-padded
SLN), a second independent confirmation of the join alongside this
CRS/geometry check.

A failure here means CRS or geometry is wrong — stop and fix before
trusting the pilot (spec §6.3).
"""
from __future__ import annotations

from pathlib import Path

import pytest

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "school_pilot"
pytestmark = pytest.mark.skipif(
    not (_DATA_DIR / "elementary.json").exists(),
    reason="data/school_pilot/ not baked in this environment "
           "(run scripts/build_school_pilot_data.py first)",
)

# (label, lat, lng, level, expected campus name substring, source)
CASES = [
    (
        "Woodrow Wilson High School — 100 S Glasgow Dr, Dallas, TX 75214",
        32.805200632373, -96.750980585861, "high", "Wilson",
    ),
    (
        "James Bowie Elementary — 330 N Marsalis Ave, Dallas, TX 75203",
        32.751708978938, -96.815564405173, "elementary", "Bowie",
    ),
    (
        "Zan Wesley Holmes Jr Middle — 2939 St Rita Dr, Dallas, TX 75233",
        32.711497894182, -96.866196156033, "middle", "Holmes",
    ),
]


def test_known_addresses_resolve_correct_campus() -> None:
    from api.school_pilot.zones import assign

    failures = []
    for label, lat, lng, level, expected_substr in CASES:
        result = assign(lat, lng)
        got = result.get(level)
        name = got["name"] if got else None
        if not got or expected_substr.lower() not in name.lower():
            failures.append(f"{label}: expected {level} to contain {expected_substr!r}, got {name!r}")
    assert not failures, "CRS or geometry is wrong:\n" + "\n".join(failures)


def test_known_addresses_have_a_rating() -> None:
    # Each of these campuses is TEA-rated in 2025 -- a missing rating here
    # would point at a crosswalk regression, not a CRS/geometry one.
    from api.school_pilot.zones import assign

    for label, lat, lng, level, _expected_substr in CASES:
        result = assign(lat, lng)
        got = result.get(level)
        assert got is not None, f"{label}: {level} did not resolve to any zone"
        assert got["rating"] in ("A", "B", "C", "D", "F"), f"{label}: missing/invalid rating {got['rating']!r}"
