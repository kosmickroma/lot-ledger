"""Tests for the duplexes bucket in `classify_parcel` (DCAD v1, 2026-06-01).

Locked rule: `units` (joined from `res_detail.num_units`) drives the new
duplex bucket; SPTD is used only to gate eligibility. Condos (A13/A14)
and apartments (B11) stay multifamily by DCAD's classification regardless
of unit count. F10 commercial stays commercial. See
docs/DUPLEXES_PROPERTY_TYPE_SPEC.md for the audit data that drove the
shift from "trust SPTD" to "trust units, gate by SPTD."
"""
from api.counties.dcad import classify_parcel


def _row(*, sptd, units=None, owner_name="", tot_val=100_000):
    """Build a minimal parcel row that classify_parcel reads."""
    return {
        "account_num": "X1",
        "sptd_code": sptd,
        "owner_name": owner_name,
        "tot_val": tot_val,
        "units": units,
    }


# ---------------------------------------------------------------------------
# Happy path — actual duplexes (B12 with 2-4 units)
# ---------------------------------------------------------------------------

def test_b12_with_two_units_is_duplexes():
    assert classify_parcel(_row(sptd="B12", units=2), set()) == "duplexes"


def test_b12_with_three_units_is_duplexes():
    assert classify_parcel(_row(sptd="B12", units=3), set()) == "duplexes"


def test_b12_with_four_units_is_duplexes():
    assert classify_parcel(_row(sptd="B12", units=4), set()) == "duplexes"


# ---------------------------------------------------------------------------
# B12 fall-through — single-unit / no-unit B12 is single_family
# ---------------------------------------------------------------------------

def test_b12_with_one_unit_is_single_family():
    """60% of DCAD's B12 parcels are actually 1-unit per the 2026-06-01
    audit. DCAD's "Duplexes" SPTD label is unreliable for these; we trust
    units instead and let them fall through to single_family."""
    assert classify_parcel(_row(sptd="B12", units=1), set()) == "single_family"


def test_b12_with_null_units_is_single_family():
    """No units recorded → can't confirm 2-4 → falls through to
    single_family (safer default than the old `B12 → multifamily`
    behavior, which would have lumped these with apartments)."""
    assert classify_parcel(_row(sptd="B12", units=None), set()) == "single_family"


def test_b12_with_zero_units_is_single_family():
    assert classify_parcel(_row(sptd="B12", units=0), set()) == "single_family"


def test_b12_with_five_units_is_single_family():
    """5+ unit B12 is an anomaly (only 2 such parcels in DCAD per audit).
    Falls through to single_family — not multifamily, since DCAD's
    apartment/condo SPTDs are the multifamily anchors."""
    assert classify_parcel(_row(sptd="B12", units=5), set()) == "single_family"


# ---------------------------------------------------------------------------
# DCAD-labeled condos and apartments stay multifamily REGARDLESS of units
# ---------------------------------------------------------------------------

def test_a13_condo_with_two_units_stays_multifamily():
    """A13 = Condominium per DCAD's SPTD label. 20 A13 parcels have
    `units` in 2-4 (per audit), but they're still condos — they share
    parcel polygons with other units (99.96% stacking rate)."""
    assert classify_parcel(_row(sptd="A13", units=2), set()) == "multifamily"


def test_a13_condo_with_one_unit_is_multifamily():
    assert classify_parcel(_row(sptd="A13", units=1), set()) == "multifamily"


def test_a14_defensive_dead_code_with_two_units_stays_multifamily():
    """A14 isn't in DCAD's live data per the spec, but the classifier
    keeps it as defensive dead-code for any future DCAD drift."""
    assert classify_parcel(_row(sptd="A14", units=2), set()) == "multifamily"


def test_b11_apartment_with_four_units_stays_multifamily():
    """988 B11 parcels have `units` in 2-4 (per audit), but DCAD's
    classification says they're apartments — we trust the SPTD label
    over the unit count for B11."""
    assert classify_parcel(_row(sptd="B11", units=4), set()) == "multifamily"


def test_b11_apartment_with_null_units_is_multifamily():
    """77% of B11 has NULL units (per audit). DCAD says apartment;
    we keep it as multifamily."""
    assert classify_parcel(_row(sptd="B11", units=None), set()) == "multifamily"


# ---------------------------------------------------------------------------
# Single-family SPTDs (A11/A12/A20) — converted-SFR cases
# ---------------------------------------------------------------------------

def test_a11_sfr_with_two_units_is_duplexes():
    """200 A11 parcels have `units` in 2-4 (per audit) — these are
    SFRs that have been converted to 2-4 unit dwellings. Mike's
    investment thesis treats them as duplexes."""
    assert classify_parcel(_row(sptd="A11", units=2), set()) == "duplexes"


def test_a11_sfr_with_three_units_is_duplexes():
    assert classify_parcel(_row(sptd="A11", units=3), set()) == "duplexes"


def test_a11_sfr_with_one_unit_stays_single_family():
    assert classify_parcel(_row(sptd="A11", units=1), set()) == "single_family"


def test_a12_townhouse_with_two_units_is_duplexes():
    """58 A12 townhouse parcels have `units` in 2-4 (per audit)."""
    assert classify_parcel(_row(sptd="A12", units=2), set()) == "duplexes"


def test_a20_mobile_home_with_two_units_is_duplexes():
    """Edge case — A20 = mobile home on owner's land. Very small cohort
    but we include it in the eligible set for symmetry with the existing
    single-family-residential bucket the spec defines."""
    assert classify_parcel(_row(sptd="A20", units=2), set()) == "duplexes"


# ---------------------------------------------------------------------------
# Commercial + vacant — duplex check does NOT poach these
# ---------------------------------------------------------------------------

def test_f10_commercial_with_two_units_stays_commercial():
    """2 F10 commercial parcels have `units` in 2-4 (per audit). DCAD's
    commercial label trumps — these aren't residential duplexes."""
    assert classify_parcel(_row(sptd="F10", units=2), set()) == "commercial"


def test_c11_vacant_with_any_units_stays_vacant():
    """C11 is a vacant SFR lot. Units field is unlikely to be populated
    on vacant lots but verify it doesn't accidentally promote."""
    assert classify_parcel(_row(sptd="C11", units=None), set()) == "vacant"


def test_c12_vacant_commercial_with_two_units_stays_commercial():
    assert classify_parcel(_row(sptd="C12", units=2), set()) == "commercial"


# ---------------------------------------------------------------------------
# Existing rules unaffected by the new duplex check
# ---------------------------------------------------------------------------

def test_exempt_account_is_exempt_even_when_duplex_eligible():
    """Exempt-account match (e.g., parcel is in exempt_accounts list)
    short-circuits BEFORE the duplex check — exemption takes priority."""
    row = _row(sptd="B12", units=2)
    assert classify_parcel(row, exempt_set={"X1"}) == "exempt"


def test_x11_totally_exempt_short_circuits_duplex():
    """X11 is totally-exempt; duplex check doesn't run."""
    assert classify_parcel(_row(sptd="X11", units=2), set()) == "exempt"


def test_d10_ag_short_circuits_duplex():
    assert classify_parcel(_row(sptd="D10", units=2), set()) == "exempt"


def test_nominal_value_c11_stays_exempt():
    """Existing $≤500 nominal-value rule for C11/C12 unaffected."""
    row = _row(sptd="C11", units=None, tot_val=100)
    assert classify_parcel(row, set()) == "exempt"


def test_a11_sfr_normal_returns_single_family():
    """Sanity check that the existing single_family default is untouched."""
    assert classify_parcel(_row(sptd="A11", units=None), set()) == "single_family"


def test_units_value_is_string_coerces_correctly():
    """The `_safe_int` helper coerces numeric strings — guard against
    DB driver returning Decimal or string-form numbers without a crash."""
    assert classify_parcel(_row(sptd="B12", units="2"), set()) == "duplexes"
    assert classify_parcel(_row(sptd="B12", units="1"), set()) == "single_family"


def test_units_value_is_decimal_coerces_correctly():
    """psycopg2 may return Decimal for numeric DB columns."""
    from decimal import Decimal
    assert classify_parcel(_row(sptd="B12", units=Decimal("3")), set()) == "duplexes"


def test_units_value_is_float_with_no_fraction_coerces_correctly():
    """A clean float like 2.0 should be coerced to int 2 and match."""
    assert classify_parcel(_row(sptd="B12", units=2.0), set()) == "duplexes"


def test_units_value_is_garbage_string_falls_through_safely():
    """Non-numeric `units` value should be treated as missing (None
    semantics), not crash. The row falls through to single_family."""
    assert classify_parcel(_row(sptd="B12", units="garbage"), set()) == "single_family"
