"""Tests for `_classify_denton` — Denton CAD parcel classifier.

Duplexes (2026-06-01): PTAD code B2 (Duplex) and its variant OB2 route to
the new duplex bucket. B1 (Multi-family / 5+ apartments) stays
multifamily. Denton has no `units` column, so the rule is entirely
code-driven — there's no `units`-based tie-breaker available.

This file is the FIRST classifier-side test file for Denton. The 9 pins
below match the minimum coverage matrix from the 2026-06-01 Copilot
critique, plus the multi-code comma-precedence cases.
"""
from api.counties.denton import _classify_denton


def _row(*, code, owner_name="JANE DOE", tot_val=100_000):
    """Minimal normalized Denton row that the classifier reads."""
    return {
        "account_num": "111625",
        "sptd_code": code,
        "owner_name": owner_name,
        "tot_val": tot_val,
        "is_exempt": False,
    }


# ---------------------------------------------------------------------------
# B2 / OB2 — the new duplex bucket
# ---------------------------------------------------------------------------

def test_b2_duplex_is_duplexes():
    """The change: B2 was in `_DENTON_MF_CODES → multifamily`; now it
    routes to the new duplexes bucket. ~2,606 Denton parcels per audit."""
    assert _classify_denton(_row(code="B2")) == "duplexes"


def test_ob2_other_duplex_is_duplexes():
    """OB2 (8 parcels) currently falls through to single_family — it's
    not in ANY existing code set. Moving it to duplexes is the symmetric
    move with DCAD's defensive A14 treatment of "Other" variants."""
    assert _classify_denton(_row(code="OB2")) == "duplexes"


# ---------------------------------------------------------------------------
# B1 stays multifamily — apartments-by-code, not duplex
# ---------------------------------------------------------------------------

def test_b1_multifamily_stays_multifamily():
    """PTAD B1 = Multi-family Residence (apartments / 5+ unit). Not in
    the duplex bucket — and unlike Collin, there are no edge cases here
    because Denton has no `units` column to expose any 2-4 unit B1
    parcels in the first place."""
    assert _classify_denton(_row(code="B1")) == "multifamily"


# ---------------------------------------------------------------------------
# Exempt path short-circuits — gov / HOA ownership of B2 still hits exempt
# ---------------------------------------------------------------------------

def test_government_owned_b2_is_exempt_not_duplexes():
    """City ownership trips `non_target_owner` BEFORE the duplex check —
    Denton's denton.py:188-193 short-circuits on gov ownership. B2 is
    not in the gov-bypass set, so this must route to exempt."""
    assert _classify_denton(_row(code="B2", owner_name="CITY OF DENTON")) == "exempt"


def test_hoa_owned_b2_is_exempt_not_duplexes():
    """HOA ownership trips non_target_owner ONLY for codes outside the
    A1-A6/A9 bypass set. B2 is NOT in that set, so an HOA-owned B2
    routes to exempt. This pins the existing HOA-bypass contract."""
    assert _classify_denton(_row(code="B2", owner_name="THE PRESERVE HOA")) == "exempt"


# ---------------------------------------------------------------------------
# Single-family + variants — baseline unchanged
# ---------------------------------------------------------------------------

def test_a1_sfr_is_single_family():
    """A1 = Single Family Residence — Denton's largest cohort (~281k).
    Baseline check that the default path still works post-duplex split."""
    assert _classify_denton(_row(code="A1")) == "single_family"


def test_oa1_other_sfr_is_single_family():
    """OA1 = "Other A1" variant — existing Denton convention treats it
    as single_family. Verify the duplex change doesn't accidentally
    poach this."""
    assert _classify_denton(_row(code="OA1")) == "single_family"


# ---------------------------------------------------------------------------
# Comma-joined multi-code first-wins contract
# ---------------------------------------------------------------------------

def test_comma_joined_b2_first_is_duplexes():
    """Multi-code rows: `_primary_code` splits on `,` and takes the
    first. `'B2,A1'` means the dominant improvement is a duplex with a
    secondary SFR structure on the parcel — duplex classification wins.
    Locks the first-wins contract that every other code-check branch in
    `_classify_denton` already follows."""
    assert _classify_denton(_row(code="B2,A1")) == "duplexes"


def test_comma_joined_a1_first_is_single_family():
    """The mirror case — `'A1,B2'` means dominant improvement is SFR
    with a secondary duplex structure. SFR wins. Same first-wins
    contract; this row should NOT route to duplexes just because B2
    appears later in the string."""
    assert _classify_denton(_row(code="A1,B2")) == "single_family"
