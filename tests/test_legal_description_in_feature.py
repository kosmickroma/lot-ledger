"""Tests for the `legal_description` field added to `build_feature` (2026-06-01).

The popup's "Neighborhood" cell now renders a second-line tail with the
Block/Lot detail from the full legal description. Each county's
normalized row stuffs the legal parts into legal1..legal5:

  * DCAD:    spreads across all five (e.g. "WINSLOW HEIGHTS" / "BLK A" / "LOT 7")
  * TAD:     full string in legal1, "" in legal2-5
  * Collin:  same as TAD
  * Denton:  same as TAD

`build_feature` (api/counties/dcad.py, called by all 4 counties via
api/main.py) joins them with spaces. The popup helper
`_neighborhoodCellHtml` then strips the subdivision prefix to compute
the tail.
"""
from api.counties.dcad import build_feature


def _minimal_row(**overrides):
    """Minimal row dict that satisfies build_feature's required keys.
    Just enough fields to not blow up on attribute access; the test only
    cares about legal_description on the resulting feature properties.
    """
    base = {
        "account_num": "00000051",
        "lat": 32.7,
        "lng": -97.0,
        "sptd_code": "A1",
        "state_code": "A",
        "owner_name": "JANE DOE",
    }
    base.update(overrides)
    return base


def test_legal_description_joins_dcad_style_without_admin_markers_passes_through():
    """DCAD splits across legal1..legal5 — join with single spaces, in
    the order legal1..legal5. When no admin markers (INT###, DD###,
    CO-DC) appear, the trim is a no-op and the ACS / acreage portion
    stays in the joined string."""
    row = _minimal_row(
        legal1="WINSLOW HEIGHTS",
        legal2="BLK A",
        legal3="LOT 7",
        legal4="ACS .15",
        legal5="",
    )
    feature = build_feature(row, "single_family", False, None)
    assert feature["properties"]["legal_description"] == "WINSLOW HEIGHTS BLK A LOT 7 ACS .15"


def test_legal_description_tad_style_full_in_legal1():
    """TAD stuffs the full legal_descr into legal1 and leaves the rest
    empty — the join returns the legal1 value as-is, no trailing spaces."""
    row = _minimal_row(
        legal1="WESTPOINT ADDITION (FT WORTH) Block 6 Lot 5",
        legal2="",
        legal3="",
        legal4="",
        legal5="",
    )
    feature = build_feature(row, "single_family", False, None)
    assert (
        feature["properties"]["legal_description"]
        == "WESTPOINT ADDITION (FT WORTH) Block 6 Lot 5"
    )


def test_legal_description_collin_denton_shape_matches_tad():
    """Collin and Denton normalize identically to TAD (full string in
    legal1, empties in 2-5). Single test pinning the shape since both
    counties use the same pattern."""
    row = _minimal_row(
        legal1="THE OAKS PHASE 2 BLK 4 LOT 12",
        legal2="",
        legal3="",
        legal4="",
        legal5="",
    )
    feature = build_feature(row, "single_family", False, None)
    assert feature["properties"]["legal_description"] == "THE OAKS PHASE 2 BLK 4 LOT 12"


def test_legal_description_empty_when_no_legal_fields():
    """A row with no legal1-5 keys at all returns an empty string —
    backward-compatible with anything that previously skipped legal
    population (e.g. older cached_jobs rows). The popup helper then
    falls back to subdivision-only rendering."""
    row = _minimal_row()  # no legal1..legal5 keys
    feature = build_feature(row, "single_family", False, None)
    assert feature["properties"]["legal_description"] == ""


def test_legal_description_skips_blank_parts_dcad_with_gaps():
    """DCAD parcels with some legal slots blank should produce a clean
    single-space join — no double spaces or trailing whitespace."""
    row = _minimal_row(
        legal1="HARWOOD HEIGHTS",
        legal2="",
        legal3="LOT 22",  # legal2 empty between legal1 and legal3
        legal4="",
        legal5="ACS .25",
    )
    feature = build_feature(row, "single_family", False, None)
    assert feature["properties"]["legal_description"] == "HARWOOD HEIGHTS LOT 22 ACS .25"


def test_dcad_admin_tail_trimmed_with_lt_abbreviation():
    """DCAD inconsistently uses both LOT and LT (an abbreviation) to
    label the lot value. KK hit this on preview (BRYAN PLACE REV 2 BLK
    B/333 LT 18 INT...). Trim by admin-marker position, NOT by the LOT
    keyword position — so either abbreviation gets handled."""
    row = _minimal_row(
        legal1="BRYAN PLACE REV 2",
        legal2="BLK B/333",
        legal3="LT 18",
        legal4="INT202500021195 DD01312025",
        legal5="CO-DC 0333 00B 01800 1000333 00B",
    )
    feature = build_feature(row, "single_family", False, None)
    assert (
        feature["properties"]["legal_description"]
        == "BRYAN PLACE REV 2 BLK B/333 LT 18"
    )


def test_dcad_subdivision_containing_inst_not_falsely_trimmed():
    """Many DCAD subdivisions have "INST" in their name (e.g.
    "BUCKNER TERRACE 1ST INST" = First Installment). The admin-marker
    regex `\\bINT\\d` requires a digit immediately after, so "INST" with
    no digit doesn't trigger a false cut. Confirms the regex is
    well-scoped."""
    row = _minimal_row(
        legal1="BUCKNER TERRACE 1ST INST",
        legal2="BLK A",
        legal3="LOT 1",
        legal4="",
        legal5="",
    )
    feature = build_feature(row, "single_family", False, None)
    assert (
        feature["properties"]["legal_description"]
        == "BUCKNER TERRACE 1ST INST BLK A LOT 1"
    )


def test_dcad_admin_tail_trimmed_after_lot():
    """DCAD parcels frequently append deed instrument number + deed date
    + internal CO-DC indices after the LOT token. Those are operational
    metadata, not analyst-relevant — trim them so the popup's
    Neighborhood cell stays scannable. Real example from a live DCAD
    parcel (4727 ASHBROOK RD)."""
    row = _minimal_row(
        legal1="BUCKNER TERRACE 8TH SEC 1ST INST",
        legal2="BLK 21/6129",
        legal3="LOT 7",
        legal4="INT202500184503 DD08292025",
        legal5="CO-DC 6129 021 00700 3DA6129 021",
    )
    feature = build_feature(row, "single_family", False, None)
    assert (
        feature["properties"]["legal_description"]
        == "BUCKNER TERRACE 8TH SEC 1ST INST BLK 21/6129 LOT 7"
    )


def test_tad_clean_legal_unchanged_by_trim():
    """TAD's legal strings already end at the lot number — the DCAD
    admin-tail trim must be a no-op for them (regex matches the whole
    string and returns it unchanged)."""
    row = _minimal_row(
        legal1="WESTPOINT ADDITION (FT WORTH) Block 6 Lot 5",
        legal2="", legal3="", legal4="", legal5="",
    )
    feature = build_feature(row, "single_family", False, None)
    assert (
        feature["properties"]["legal_description"]
        == "WESTPOINT ADDITION (FT WORTH) Block 6 Lot 5"
    )


def test_legal_without_lot_token_passes_through():
    """Commercial / agricultural / vacant parcels without a LOT token
    in the legal description get returned unchanged — they don't carry
    the admin tail anyway, and stripping ACS / TRACT etc. would lose
    real info."""
    row = _minimal_row(
        legal1="ACME COMMERCIAL TRACT",
        legal2="ACS 12.5",
        legal3="", legal4="", legal5="",
    )
    feature = build_feature(row, "commercial", False, None)
    assert feature["properties"]["legal_description"] == "ACME COMMERCIAL TRACT ACS 12.5"


def test_legal_description_strips_whitespace_in_parts():
    """_clean_text trims each part before joining — leading/trailing
    spaces on individual legal fields don't leak into the result."""
    row = _minimal_row(
        legal1="  THE OAKS  ",
        legal2="\tLOT 8\t",
        legal3="",
        legal4="",
        legal5="",
    )
    feature = build_feature(row, "single_family", False, None)
    assert feature["properties"]["legal_description"] == "THE OAKS LOT 8"
