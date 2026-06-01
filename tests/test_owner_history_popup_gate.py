"""Tests for `_owner_history_for_popup` — the gate that decides whether
the parcel-detail endpoint exposes the 6 owner-history cells to a given
viewer.

Per Mike's 2026-06-01 ask: popup display is gated to superusers
(developer / owner / power_user). Anyone else gets None and the popup
section is suppressed entirely; the v2 CSV columns remain available
to everyone since those have always been the public surface.
"""
import datetime as dt
from unittest.mock import MagicMock

import api.main as main


def _stub_hist_fetch(monkeypatch, *, hist_by_key=None):
    """Replace _fetch_ownership_history with a static-dict stub so we can
    test the gate in isolation without touching the DB."""
    by_key = hist_by_key or {}

    def _fake(account_keys):
        return {k: v for k, v in by_key.items() if k in account_keys}

    monkeypatch.setattr(main, "_fetch_ownership_history", _fake)


def test_gate_blocks_member_role(monkeypatch):
    """A member-level user gets None even though the parcel has history."""
    _stub_hist_fetch(
        monkeypatch,
        hist_by_key={
            ("dcad", "12345"): {
                "owners": {2024: "ACME"},
                "deed_dates": {2024: dt.date(2024, 5, 1)},
                "acquired": dt.date(2024, 5, 1),
            }
        },
    )
    assert main._owner_history_for_popup("dcad", "12345", {"role": "member"}) is None


def test_gate_blocks_user_role(monkeypatch):
    """A regular user role also gets None."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {"owners": {2024: "ACME"}, "deed_dates": {}, "acquired": None}
    })
    assert main._owner_history_for_popup("dcad", "12345", {"role": "user"}) is None


def test_gate_allows_power_user(monkeypatch):
    """A power_user is part of the superuser set."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2024: "ACME"},
            "deed_dates": {2024: dt.date(2024, 5, 1)},
            "acquired": dt.date(2024, 5, 1),
        }
    })
    cells = main._owner_history_for_popup("dcad", "12345", {"role": "power_user"})
    assert cells is not None
    assert cells[0] == "ACME"
    assert len(cells) == 6


def test_gate_allows_owner_role(monkeypatch):
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2024: "ACME"},
            "deed_dates": {2024: dt.date(2024, 5, 1)},
            "acquired": dt.date(2024, 5, 1),
        }
    })
    cells = main._owner_history_for_popup("dcad", "12345", {"role": "owner"})
    assert cells is not None
    assert cells[0] == "ACME"


def test_gate_allows_developer_role(monkeypatch):
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("collin", "99"): {
            "owners": {2025: "SMITH JOHN"},
            "deed_dates": {2025: dt.date(2020, 1, 1)},
            "acquired": dt.date(2020, 1, 1),
        }
    })
    cells = main._owner_history_for_popup("collin", "99", {"role": "developer"})
    assert cells is not None
    assert cells[0] == "SMITH JOHN"


def test_gate_returns_none_for_unsupported_county(monkeypatch):
    """A county not yet in the supported set (e.g. a hypothetical Ellis)
    must return None even for a developer — protects against accidental
    exposure if a routing bug ever sends us an unsupported county_key."""
    _stub_hist_fetch(monkeypatch)
    assert main._owner_history_for_popup("ellis", "1", {"role": "developer"}) is None


def test_gate_returns_none_when_no_history_rows(monkeypatch):
    """A superuser viewing a parcel that simply isn't in
    ownership_snapshots (new construction, exempt, etc.) gets None so
    the popup section is hidden rather than rendering 6 blanks."""
    _stub_hist_fetch(monkeypatch, hist_by_key={})  # empty lookup
    assert main._owner_history_for_popup("dcad", "12345", {"role": "developer"}) is None


def test_gate_returns_none_when_all_cells_blank(monkeypatch):
    """A history record that distills to 6 empty cells (every year's
    owner_name is blank — degenerate data) gets None for the same
    reason: don't render an empty section."""
    _stub_hist_fetch(monkeypatch, hist_by_key={
        ("dcad", "12345"): {
            "owners": {2024: "", 2025: "  "},
            "deed_dates": {2024: None, 2025: None},
            "acquired": None,
        }
    })
    assert main._owner_history_for_popup("dcad", "12345", {"role": "developer"}) is None


def test_gate_returns_none_when_user_none():
    """Defensive: anonymous request reaches the helper with user=None."""
    # No fetch should occur — the gate short-circuits before the DB lookup.
    assert main._owner_history_for_popup("dcad", "12345", None) is None


def test_gate_handles_blank_account_num(monkeypatch):
    _stub_hist_fetch(monkeypatch)
    assert main._owner_history_for_popup("dcad", "  ", {"role": "developer"}) is None
    assert main._owner_history_for_popup("dcad", "", {"role": "developer"}) is None
