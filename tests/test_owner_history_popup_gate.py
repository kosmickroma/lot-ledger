"""Tests for `_owner_history_for_popup` — gate + reconcile logic for the
parcel-detail popup's Owner History section.

Per Mike's 2026-06-01 ask:
  * Display is gated to superusers (developer / owner / power_user).
  * The chain is reconciled against the LIVE CAD owner: if the snapshot's
    "current" owner differs from the live parcel-row owner (e.g. a sale
    happened post-roll), the live owner is promoted to "Current Owner" +
    "Acquired" and the snapshot's "current" demotes to the first
    "Previously" entry. Resolves the off-by-one in the "Prior Owner N"
    labels that surfaced in the original popup design.
"""
import datetime as dt

import api.main as main


def _stub_hist_fetch(monkeypatch, *, hist_by_key=None):
    """Replace _fetch_ownership_history with a static-dict stub so we can
    test the gate in isolation without touching the DB."""
    by_key = hist_by_key or {}

    def _fake(account_keys):
        return {k: v for k, v in by_key.items() if k in account_keys}

    monkeypatch.setattr(main, "_fetch_ownership_history", _fake)


# ---------------------------------------------------------------------------
# Role gate
# ---------------------------------------------------------------------------

def test_gate_blocks_member_role(monkeypatch):
    """A member-level user gets None even though the parcel has history."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2024: "ACME"},
            "deed_dates": {2024: dt.date(2024, 5, 1)},
            "acquired": dt.date(2024, 5, 1),
        }
    })
    assert main._owner_history_for_popup(
        "dcad", "12345", {"role": "member"}, {"owner_name": "ACME"}
    ) is None


def test_gate_blocks_user_role(monkeypatch):
    """A regular user role also gets None."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {"owners": {2024: "ACME"}, "deed_dates": {}, "acquired": None}
    })
    assert main._owner_history_for_popup(
        "dcad", "12345", {"role": "user"}, {"owner_name": "ACME"}
    ) is None


def test_gate_allows_power_user(monkeypatch):
    """A power_user is part of the superuser set."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2024: "ACME"},
            "deed_dates": {2024: dt.date(2024, 5, 1)},
            "acquired": dt.date(2024, 5, 1),
        }
    })
    block = main._owner_history_for_popup(
        "dcad", "12345", {"role": "power_user"}, {"owner_name": "ACME"}
    )
    assert block is not None
    assert block["current_owner"] == "ACME"
    assert block["current_acquired"] == "05/01/2024"
    assert block["previous"] == []


def test_gate_allows_owner_role(monkeypatch):
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2024: "ACME"},
            "deed_dates": {2024: dt.date(2024, 5, 1)},
            "acquired": dt.date(2024, 5, 1),
        }
    })
    block = main._owner_history_for_popup(
        "dcad", "12345", {"role": "owner"}, {"owner_name": "ACME"}
    )
    assert block is not None
    assert block["current_owner"] == "ACME"


def test_gate_allows_developer_role(monkeypatch):
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("collin", "99"): {
            "owners": {2025: "SMITH JOHN"},
            "deed_dates": {2025: dt.date(2020, 1, 1)},
            "acquired": dt.date(2020, 1, 1),
        }
    })
    block = main._owner_history_for_popup(
        "collin", "99", {"role": "developer"}, {"owner_name": "SMITH JOHN"}
    )
    assert block is not None
    assert block["current_owner"] == "SMITH JOHN"


# ---------------------------------------------------------------------------
# County + data guards
# ---------------------------------------------------------------------------

def test_gate_returns_none_for_unsupported_county(monkeypatch):
    """An unsupported county (hypothetical Ellis) returns None even for a
    developer — protects against accidental routing bugs."""
    _stub_hist_fetch(monkeypatch)
    assert main._owner_history_for_popup(
        "ellis", "1", {"role": "developer"}, {"owner_name": "ANY"}
    ) is None


def test_gate_returns_none_when_no_history_rows(monkeypatch):
    """A superuser viewing a parcel that simply isn't in
    ownership_snapshots (new construction, exempt, etc.) gets None so
    the popup section is hidden rather than rendering a blank card."""
    _stub_hist_fetch(monkeypatch, hist_by_key={})
    assert main._owner_history_for_popup(
        "dcad", "12345", {"role": "developer"}, {"owner_name": "ANY"}
    ) is None


def test_gate_returns_none_when_all_owners_blank(monkeypatch):
    """A history record where every year's owner_name is blank distills
    to nothing — return None rather than promoting a blank live owner."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2024: "", 2025: "  "},
            "deed_dates": {2024: None, 2025: None},
            "acquired": None,
        }
    })
    assert main._owner_history_for_popup(
        "dcad", "12345", {"role": "developer"}, {"owner_name": "ALVAREZ"}
    ) is None


def test_gate_returns_none_when_user_none():
    """Defensive: anonymous request reaches the helper with user=None."""
    assert main._owner_history_for_popup(
        "dcad", "12345", None, {"owner_name": "ANY"}
    ) is None


def test_gate_handles_blank_account_num(monkeypatch):
    _stub_hist_fetch(monkeypatch)
    assert main._owner_history_for_popup(
        "dcad", "  ", {"role": "developer"}, {"owner_name": "ANY"}
    ) is None
    assert main._owner_history_for_popup(
        "dcad", "", {"role": "developer"}, {"owner_name": "ANY"}
    ) is None


# ---------------------------------------------------------------------------
# MATCH case: snapshot's "current" matches the live CAD owner
# ---------------------------------------------------------------------------

def test_match_case_passes_snapshot_through_unchanged(monkeypatch):
    """When live owner equals snapshot's "current" owner (after
    normalization), the block reads straight from the snapshot — same
    six values the CSV exports."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2021: "OLD OWNER", 2024: "SIBLEY BRADLEY L", 2025: "SIBLEY BRADLEY L"},
            "deed_dates": {
                2021: dt.date(2010, 5, 1),
                2024: dt.date(2016, 1, 19),
                2025: dt.date(2016, 1, 19),
            },
            "acquired": dt.date(2016, 1, 19),
        }
    })
    block = main._owner_history_for_popup(
        "dcad", "12345",
        {"role": "developer"},
        {"owner_name": "SIBLEY BRADLEY L"},
    )
    assert block is not None
    assert block["current_owner"] == "SIBLEY BRADLEY L"
    assert block["current_acquired"] == "01/19/2016"
    assert block["previous"] == ["OLD OWNER (~2021)"]


def test_match_case_tolerates_punctuation_and_case_diff(monkeypatch):
    """Normalization handles trim/upper/punctuation/'&'->AND so the
    match check doesn't trip on cosmetic differences."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2025: "ACME LLC & SONS"},
            "deed_dates": {2025: dt.date(2020, 1, 1)},
            "acquired": dt.date(2020, 1, 1),
        }
    })
    # Live owner: same name with mixed case, extra spaces, no '&'.
    block = main._owner_history_for_popup(
        "dcad", "12345",
        {"role": "developer"},
        {"owner_name": "  acme,  LLC AND  Sons  "},
    )
    assert block is not None
    # Match → use snapshot's stored form, not the live spelling.
    assert block["current_owner"] == "ACME LLC & SONS"


def test_match_case_when_live_owner_missing(monkeypatch):
    """If parcel_props lacks an owner_name (or it's blank), treat the
    snapshot as the source of truth — don't fabricate a mismatch."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2025: "ACME"},
            "deed_dates": {2025: dt.date(2020, 1, 1)},
            "acquired": dt.date(2020, 1, 1),
        }
    })
    block = main._owner_history_for_popup(
        "dcad", "12345", {"role": "developer"}, {}
    )
    assert block is not None
    assert block["current_owner"] == "ACME"
    block_none = main._owner_history_for_popup(
        "dcad", "12345", {"role": "developer"}, None
    )
    assert block_none is not None
    assert block_none["current_owner"] == "ACME"


# ---------------------------------------------------------------------------
# MISMATCH case: a sale happened post-roll; live owner differs from snapshot
# ---------------------------------------------------------------------------

def test_mismatch_promotes_live_and_demotes_snapshot(monkeypatch):
    """The Mike-flagged case: ALVAREZ (live) ≠ SIBLEY (snapshot 2025).
    ALVAREZ promotes to Current Owner; SIBLEY demotes into Previously."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2021: "OLD", 2024: "SIBLEY BRADLEY L", 2025: "SIBLEY BRADLEY L"},
            "deed_dates": {
                2021: dt.date(2010, 5, 1),
                2024: dt.date(2016, 1, 19),
                2025: dt.date(2016, 1, 19),
            },
            "acquired": dt.date(2016, 1, 19),
        }
    })
    block = main._owner_history_for_popup(
        "dcad", "12345",
        {"role": "developer"},
        {"owner_name": "ALVAREZ ROBERTO CARLOS"},  # post-roll sale
    )
    assert block is not None
    assert block["current_owner"] == "ALVAREZ ROBERTO CARLOS"
    # DCAD parcel rows have no live deed_date — Acquired is blank for now.
    assert block["current_acquired"] == ""
    assert block["previous"] == [
        "SIBLEY BRADLEY L (~2016)",  # acquired-year suffix from the snapshot's deed
        "OLD (~2021)",                # snapshot's existing prior
    ]


def test_mismatch_uses_live_deed_date_when_available(monkeypatch):
    """Collin and Denton parcel rows DO carry a live deed_date. When
    present, it populates the Acquired cell."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("collin", "99"): {
            "owners": {2024: "PRIOR OWNER", 2025: "PRIOR OWNER"},
            "deed_dates": {2024: dt.date(2020, 6, 1), 2025: dt.date(2020, 6, 1)},
            "acquired": dt.date(2020, 6, 1),
        }
    })
    # Live owner mismatch with a YYYY-MM-DD string deed (Collin's format).
    block = main._owner_history_for_popup(
        "collin", "99",
        {"role": "developer"},
        {"owner_name": "NEW BUYER LLC", "deed_date": "2025-11-15"},
    )
    assert block is not None
    assert block["current_owner"] == "NEW BUYER LLC"
    assert block["current_acquired"] == "11/15/2025"
    assert block["previous"] == ["PRIOR OWNER (~2020)"]


def test_mismatch_handles_datetime_date_deed(monkeypatch):
    """Denton's parcel rows return deed_date as a datetime.date object."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("denton", "1111"): {
            "owners": {2025: "OLD"},
            "deed_dates": {2025: dt.date(2018, 3, 1)},
            "acquired": dt.date(2018, 3, 1),
        }
    })
    block = main._owner_history_for_popup(
        "denton", "1111",
        {"role": "developer"},
        {"owner_name": "NEW", "deed_date": dt.date(2026, 1, 5)},
    )
    assert block is not None
    assert block["current_owner"] == "NEW"
    assert block["current_acquired"] == "01/05/2026"


def test_mismatch_caps_previous_at_four_entries(monkeypatch):
    """When a long-history parcel sells post-roll, the chain prepends the
    demoted snapshot current to the priors. Cap total previous entries
    at 4 — drop the oldest snapshot prior to stay within the cap.

    Realistic data shape: each owner's recorded deed_txfr_date is the
    same across every year of their run (one deed = one date, even when
    re-reported on the next certified roll). E acquired in 2024 and
    held through 2025, so deed_dates[2024] = deed_dates[2025] = 2024-01-01.
    """
    deed_by_owner = {"A": 2020, "B": 2021, "C": 2022, "D": 2023, "E": 2024}
    owners = {
        2020: "A", 2021: "B", 2022: "C", 2023: "D",
        2024: "E", 2025: "E",
    }
    deed_dates = {
        yr: dt.date(deed_by_owner[name], 1, 1) for yr, name in owners.items()
    }
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": owners,
            "deed_dates": deed_dates,
            "acquired": dt.date(2024, 1, 1),
        }
    })
    block = main._owner_history_for_popup(
        "dcad", "12345",
        {"role": "developer"},
        {"owner_name": "F"},  # never seen in snapshot — full mismatch
    )
    assert block is not None
    assert block["current_owner"] == "F"
    # Chain: demoted E (snapshot current) + previously-snapshotted priors D/C/B.
    # The oldest prior (A) is dropped to keep the cap at 4 total entries.
    assert block["previous"] == [
        "E (~2024)",
        "D (~2023)",
        "C (~2022)",
        "B (~2021)",
    ]


def test_format_live_deed_for_popup_handles_each_shape():
    """Format helper tolerates the deed_date shapes the four counties
    actually return (ISO string, MM/DD/YYYY string, datetime.date, None,
    empty)."""
    assert main._format_live_deed_for_popup(dt.date(2024, 5, 1)) == "05/01/2024"
    assert main._format_live_deed_for_popup("2024-05-01") == "05/01/2024"
    assert main._format_live_deed_for_popup("05/01/2024") == "05/01/2024"
    assert main._format_live_deed_for_popup("05-01-2024") == "05/01/2024"
    assert main._format_live_deed_for_popup(None) == ""
    assert main._format_live_deed_for_popup("") == ""
    assert main._format_live_deed_for_popup("   ") == ""
    # Garbage falls through to the raw string so unknown future formats
    # don't silently get swallowed; operator can spot the oddball.
    assert main._format_live_deed_for_popup("not-a-date") == "not-a-date"
