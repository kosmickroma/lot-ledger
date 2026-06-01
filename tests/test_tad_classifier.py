"""Tests for `_classify_tad` — TAD (Tarrant) parcel classifier.

Duplexes (2026-06-01): TAD's State Use Codes B2/B3/B4 (Duplex / Triplex /
Quadplex per TAD's own naming) route to the new duplex bucket. B1
(Multi-Family / 5+ apartments) and A3 (Condominium) stay multifamily.
M1/M2 (mobile home / personal property aircraft) stay multifamily per
existing TAD quirk — explicitly out of scope to touch.

This file is the FIRST classifier-side test file for TAD (prior tests
covered only city resolution normalization). Pins below match Copilot's
required coverage matrix from the 2026-06-01 critique.
"""
from api.counties.tad import _classify_tad


def _row(*, code, owner_name="JANE DOE", tot_val=100_000):
    """Minimal normalized TAD row that the classifier reads.

    Note: `sptd_code` is the normalized alias for raw `property_class`.
    """
    return {
        "account_num": "00000051",
        "sptd_code": code,
        "owner_name": owner_name,
        "tot_val": tot_val,
    }


# ---------------------------------------------------------------------------
# B2 / B3 / B4 — the new duplex bucket
# ---------------------------------------------------------------------------

def test_b2_duplex_is_duplexes():
    """TAD B2 = Residential Duplex per TAD's own State Use Code reference.
    ~7,558 parcels per the 2026-06-01 audit."""
    assert _classify_tad(_row(code="B2")) == "duplexes"


def test_b3_triplex_is_duplexes():
    """TAD B3 = Residential Triplex. ~1,095 parcels per audit."""
    assert _classify_tad(_row(code="B3")) == "duplexes"


def test_b4_quadplex_is_duplexes():
    """TAD B4 = Residential Quadplex. 0 parcels in current data but
    pinned defensively in case TAD ever uses the code."""
    assert _classify_tad(_row(code="B4")) == "duplexes"


# ---------------------------------------------------------------------------
# B1 / A3 stay multifamily — apartments and condos by TAD label
# ---------------------------------------------------------------------------

def test_b1_multifamily_stays_multifamily():
    """TAD B1 = Multi-Family Residential (apartments / 5+ unit). 0
    parcels in current data; pinned to lock the "code wins" rule."""
    assert _classify_tad(_row(code="B1")) == "multifamily"


def test_a3_condominium_stays_multifamily():
    """TAD A3 = Condominium (~7,442 parcels). Routed via _CONDO_CODES
    OR'd with _MF_ONLY_CODES in the multifamily branch — should NOT
    leak into duplexes regardless of unit-count semantics."""
    assert _classify_tad(_row(code="A3")) == "multifamily"


# ---------------------------------------------------------------------------
# M1 / M2 mobile-home quirk — explicitly out of scope
# ---------------------------------------------------------------------------

def test_m1_mobile_home_stays_multifamily_out_of_scope():
    """TAD's M1 = Mobile Home (~10,319 parcels). Functionally single-
    unit residential, but TAD's existing classifier puts it in
    multifamily — this is a pre-existing TAD quirk that the duplex PR
    intentionally does NOT touch. Pin the current behavior so a future
    duplex-related change can't silently reclassify mobile homes."""
    assert _classify_tad(_row(code="M1")) == "multifamily"


def test_m2_personal_property_aircraft_stays_multifamily_out_of_scope():
    """TAD's M2 = Personal Property Aircraft (~131 parcels). Same as
    M1 — existing quirk pinned for safety; a separate taxonomy
    cleanup would handle this."""
    assert _classify_tad(_row(code="M2")) == "multifamily"


# ---------------------------------------------------------------------------
# Other buckets — unchanged baseline
# ---------------------------------------------------------------------------

def test_a1_sfr_is_single_family():
    """A1 = Single-Family Residential — TAD's largest cohort (~564k).
    Baseline check that the SFR default path still works."""
    assert _classify_tad(_row(code="A1")) == "single_family"


def test_bc_multi_family_commercial_is_commercial():
    """BC = Multi-Family Commercial. ~2,172 parcels. Sits in
    _COMMERCIAL_CODES — must NOT leak into duplexes via the B-prefix."""
    assert _classify_tad(_row(code="BC")) == "commercial"


def test_generic_b_falls_through_to_single_family_pre_existing_behavior():
    """Generic 'B' (36 parcels) is not in any TAD code set — falls
    through to single_family. This is a pre-existing bug (TAD's
    generic 'B' label means Multi-Family Residential per the doc) and
    is INTENTIONALLY left untouched in the duplex PR — a separate
    cleanup ticket would handle it. Pin so duplex changes can't
    accidentally fix it as a side effect."""
    assert _classify_tad(_row(code="B")) == "single_family"


# ---------------------------------------------------------------------------
# Exempt path short-circuits — gov ownership trumps the duplex check
# ---------------------------------------------------------------------------

def test_government_owned_b2_is_exempt_not_duplexes():
    """City ownership trips `non_target_owner` BEFORE the duplex check.
    B2 is not in the SFR-bypass set, so this routes to exempt."""
    assert _classify_tad(_row(code="B2", owner_name="CITY OF FORT WORTH")) == "exempt"


def test_county_owned_b3_is_exempt_not_duplexes():
    assert _classify_tad(_row(code="B3", owner_name="TARRANT COUNTY")) == "exempt"
