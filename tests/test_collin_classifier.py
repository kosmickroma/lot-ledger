"""Tests for `_classify_collin` — Collin CAD parcel classifier.

Duplexes (2026-06-01): PTAD codes B2/B3/B4 route to the new duplex
bucket. B1 (Multi-family / 5+ apartment), A3 (Condo), B6/B9 (other
multifamily) stay multifamily — "the appraisal district's classification
wins over the count," matching DCAD's locked rule.

This file is the FIRST classifier-side test file for Collin — prior tests
only covered `_normalize_collin_row`. The cases here pin the
post-duplexes behavior so a future refactor can't silently regress B2 to
multifamily or promote B1 to duplexes.
"""
from api.counties.collin import _classify_collin


def _row(*, code, owner_name="JANE DOE", tot_val=100_000, units=None):
    """Minimal normalized Collin row that the classifier reads."""
    return {
        "account_num": "R-1234-005A",
        "sptd_code": code,
        "owner_name": owner_name,
        "tot_val": tot_val,
        "units": units,
        # Fields _is_full_exempt may read — all defaults make it return False.
        "is_exempt": False,
    }


# ---------------------------------------------------------------------------
# B2 / B3 / B4 — the new duplex bucket
# ---------------------------------------------------------------------------

def test_b2_duplex_is_duplexes():
    """PTAD B2 = Duplex by Texas Comptroller definition."""
    assert _classify_collin(_row(code="B2")) == "duplexes"


def test_b3_triplex_is_duplexes():
    """PTAD B3 = Triplex (in the 2-4 unit small-multi bucket)."""
    assert _classify_collin(_row(code="B3")) == "duplexes"


def test_b4_quadplex_is_duplexes():
    """PTAD B4 = Quadplex (in the 2-4 unit small-multi bucket)."""
    assert _classify_collin(_row(code="B4")) == "duplexes"


# ---------------------------------------------------------------------------
# B1 / A3 / B6 / B9 — multifamily stays multifamily ("code wins")
# ---------------------------------------------------------------------------

def test_b1_apartments_stays_multifamily():
    """PTAD B1 = Multi-family Residence (apartments / 5+ unit)."""
    assert _classify_collin(_row(code="B1")) == "multifamily"


def test_b1_with_units_2_to_4_still_multifamily():
    """Locks in the "code wins over units" rule for Collin's 25 B1
    parcels with units 2-4 (per the 2026-06-01 audit). These look like
    duplexes by unit count but Collin's PTAD label says apartment —
    DCAD-precedent applies: the appraisal district's classification
    is authoritative, and Collin's `units` field is 99% NULL/0 anyway."""
    assert _classify_collin(_row(code="B1", units=2)) == "multifamily"
    assert _classify_collin(_row(code="B1", units=3)) == "multifamily"
    assert _classify_collin(_row(code="B1", units=4)) == "multifamily"


def test_a3_condo_stays_multifamily():
    """A3 = Condominium — never a duplex. Audit showed 100% of A3 have
    units=0, but even if they had units 2-4, the condo classification
    would win (shared parcel polygons, distinct investment thesis)."""
    assert _classify_collin(_row(code="A3")) == "multifamily"


def test_b6_b9_other_multifamily_stays_multifamily():
    """B6 / B9 are other PTAD multifamily codes (mobile home park /
    other multi-family). Tiny cohorts in Collin but verify they stay
    in multifamily and don't fall through to single_family."""
    assert _classify_collin(_row(code="B6")) == "multifamily"
    assert _classify_collin(_row(code="B9")) == "multifamily"


# ---------------------------------------------------------------------------
# Existing classification rules unchanged
# ---------------------------------------------------------------------------

def test_a1_sfr_is_single_family():
    """A1 = Single Family Residence (the largest cohort by far in Collin
    — 326k parcels). Baseline check that the existing default path
    still works post-duplexes split."""
    assert _classify_collin(_row(code="A1")) == "single_family"


def test_c1_vacant_lot_is_vacant():
    """Existing vacant rule unchanged."""
    assert _classify_collin(_row(code="C1")) == "vacant"


def test_f1_commercial_is_commercial():
    """Existing commercial rule unchanged."""
    assert _classify_collin(_row(code="F1")) == "commercial"


def test_f1_commercial_with_units_2_to_4_stays_commercial():
    """31 Collin F1 parcels have units 2-4 per the audit — likely
    small mixed-use buildings. Commercial classification wins;
    they're not residential duplexes."""
    assert _classify_collin(_row(code="F1", units=3)) == "commercial"


# ---------------------------------------------------------------------------
# Exempt path short-circuits — gov ownership, full exempt, nominal value
# ---------------------------------------------------------------------------

def test_government_owned_b2_is_exempt_not_duplexes():
    """A city-owned B2 (e.g. municipal-housing duplex) hits the
    non_target_owner gate BEFORE the B2 → duplexes branch fires.
    Important: this is the test that proves the duplex check doesn't
    accidentally leapfrog the exempt path."""
    assert _classify_collin(_row(code="B2", owner_name="CITY OF PLANO")) == "exempt"


def test_county_owned_b3_is_exempt_not_duplexes():
    assert _classify_collin(_row(code="B3", owner_name="COLLIN COUNTY")) == "exempt"


def test_state_owned_b4_is_exempt_not_duplexes():
    assert _classify_collin(_row(code="B4", owner_name="STATE OF TEXAS")) == "exempt"


def test_nominal_value_c1_is_exempt():
    """Existing nominal-value rule unaffected by duplex change."""
    assert _classify_collin(_row(code="C1", tot_val=100)) == "exempt"
