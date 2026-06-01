import datetime as dt
from unittest.mock import MagicMock

import api.main as main


def test_fetch_ownership_history_pivots_and_picks_latest_deed(monkeypatch):
    # Mock cursor returns (account_num, snapshot_year, owner_name, deed_txfr_date)
    # for a single-county (dcad) lookup — one cursor.execute call → one fetchall.
    rows = [
        ("A1", 2021, "SHARPE SARA", dt.date(2010, 1, 1)),
        ("A1", 2022, "SHARPE SARA", dt.date(2010, 1, 1)),
        ("A1", 2023, "PARK JU YONG", dt.date(2023, 3, 14)),
        ("A2", 2024, "WILSON LOIS", dt.date(2024, 8, 2)),
    ]
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__ = lambda s: cur
    cur.__exit__ = lambda *a: False
    conn = MagicMock()
    conn.cursor.return_value = cur
    monkeypatch.setattr(main, "get_conn", lambda: conn)
    monkeypatch.setattr(main, "release_conn", lambda c: None)

    out = main._fetch_ownership_history({("dcad", "A1"), ("dcad", "A2")})
    assert out[("dcad", "A1")]["owners"] == {
        2021: "SHARPE SARA", 2022: "SHARPE SARA", 2023: "PARK JU YONG",
    }
    assert out[("dcad", "A1")]["acquired"] == dt.date(2023, 3, 14)
    # v2 (2026-06-01): per-year deed_dates map; the v2 caller picks the date
    # matching whichever year anchors the Current Owner after blank-skip.
    assert out[("dcad", "A1")]["deed_dates"] == {
        2021: dt.date(2010, 1, 1),
        2022: dt.date(2010, 1, 1),
        2023: dt.date(2023, 3, 14),
    }
    assert out[("dcad", "A2")]["owners"] == {2024: "WILSON LOIS"}
    assert out[("dcad", "A2")]["acquired"] == dt.date(2024, 8, 2)
    assert out[("dcad", "A2")]["deed_dates"] == {2024: dt.date(2024, 8, 2)}


def test_fetch_ownership_history_keys_isolated_per_county(monkeypatch):
    """v2.1 (2026-06-01): two counties sharing the same account_num string
    must NOT bleed ownership timelines into each other — keying is
    (county, account_num), so DCAD's account_num='100' and Collin's
    account_num='100' resolve to two distinct records.

    Set-iteration order is non-deterministic, so we route fetchall via the
    actual `cur.execute(sql, (county, accts))` params rather than a
    positional `side_effect` list — that makes the test order-independent
    and matches how the real lookup works (one query per county)."""
    rows_by_county = {
        "dcad":   [("100", 2024, "DCAD OWNER",   dt.date(2024, 1, 1))],
        "collin": [("100", 2024, "COLLIN OWNER", dt.date(2024, 6, 1))],
    }
    cur = MagicMock()
    cur.__enter__ = lambda s: cur
    cur.__exit__ = lambda *a: False

    def execute_side_effect(_sql, params):
        county = params[0]
        cur.fetchall.return_value = rows_by_county[county]
    cur.execute.side_effect = execute_side_effect

    conn = MagicMock()
    conn.cursor.return_value = cur
    monkeypatch.setattr(main, "get_conn", lambda: conn)
    monkeypatch.setattr(main, "release_conn", lambda c: None)

    out = main._fetch_ownership_history({("dcad", "100"), ("collin", "100")})
    assert out[("dcad", "100")]["owners"] == {2024: "DCAD OWNER"}
    assert out[("collin", "100")]["owners"] == {2024: "COLLIN OWNER"}
    # Two distinct cursor.execute calls — one per county. If we ever
    # collapse back to a single query, this assertion catches the
    # cross-county collision risk again.
    assert cur.execute.call_count == 2


def test_fetch_ownership_history_empty_input_skips_query(monkeypatch):
    monkeypatch.setattr(main, "get_conn", lambda: (_ for _ in ()).throw(AssertionError("should not connect")))
    assert main._fetch_ownership_history(set()) == {}


def test_fetch_ownership_history_swallows_db_error(monkeypatch):
    def boom():
        raise RuntimeError("table missing")
    monkeypatch.setattr(main, "get_conn", boom)
    assert main._fetch_ownership_history({("dcad", "A1")}) == {}


def test_ownership_history_cells_none_is_six_blanks():
    assert main._ownership_history_cells(None) == ["", "", "", "", "", ""]


def test_ownership_history_cells_populated():
    """v2 (2026-06-01): cells are [Current Owner, Current Owner Acquired,
    Prior Owner 1..4]. SHARPE SARA (2021) and PARK JU YONG (2023) are
    NOT calendar-consecutive (gap at 2022), so two distinct runs."""
    hist = {
        "owners": {2021: "SHARPE SARA", 2023: "PARK JU YONG"},
        "deed_dates": {2021: dt.date(2010, 1, 1), 2023: dt.date(2023, 3, 14)},
        "acquired": dt.date(2023, 3, 14),
    }
    assert main._ownership_history_cells(hist) == [
        "PARK JU YONG", "03/14/2023", "SHARPE SARA (~2021)", "", "", "",
    ]


def test_ownership_history_cells_blank_latest_year_anchor_walks_back():
    """v2: when the latest year has a blank owner_name, the anchor walks
    back to the latest non-blank year and the Acquired cell follows it
    (not the max snapshot year's date)."""
    hist = {
        "owners": {2025: "", 2024: "WILSON LOIS", 2023: "WILSON LOIS"},
        "deed_dates": {2025: None, 2024: dt.date(2022, 5, 1), 2023: dt.date(2010, 1, 1)},
        "acquired": None,  # max year (2025) has null deed
    }
    # Anchor = 2024 (latest non-blank). Acquired = deed_dates[2024].
    assert main._ownership_history_cells(hist) == [
        "WILSON LOIS", "05/01/2022", "", "", "", "",
    ]


def test_ownership_history_cells_null_deed_on_anchor_blank_acquired():
    """v2: when the anchor year has a null deed_txfr_date, Acquired cell
    is blank but Current Owner still populates."""
    hist = {
        "owners": {2024: "WILSON LOIS"},
        "deed_dates": {2024: None},
        "acquired": None,
    }
    assert main._ownership_history_cells(hist) == [
        "WILSON LOIS", "", "", "", "", "",
    ]


def test_ownership_history_cells_overflow_truncates_to_four_priors():
    """v2: more than 5 distinct owners → 1 current + 4 priors kept, the
    5th-and-older entries dropped silently (overflow handling deferred
    until 1999-2020 owner data lands)."""
    hist = {
        "owners": {
            1999: "A", 2003: "B", 2008: "C", 2013: "D",
            2018: "E", 2022: "F", 2025: "G",
        },
        "deed_dates": {y: None for y in [1999, 2003, 2008, 2013, 2018, 2022, 2025]},
        "acquired": None,
    }
    assert main._ownership_history_cells(hist) == [
        "G", "", "F (~2022)", "E (~2018)", "D (~2013)", "C (~2008)",
    ]


def test_ownership_history_cells_all_blank_owners_six_blanks():
    """v2: every year has a blank/whitespace owner_name → 6 blank cells
    (distill returns empty list)."""
    hist = {
        "owners": {2025: "", 2024: "  ", 2023: None},
        "deed_dates": {
            2025: dt.date(2025, 1, 1),
            2024: dt.date(2024, 1, 1),
            2023: dt.date(2023, 1, 1),
        },
        "acquired": dt.date(2025, 1, 1),
    }
    assert main._ownership_history_cells(hist) == ["", "", "", "", "", ""]


def test_fetch_ownership_history_records_null_deed_dates(monkeypatch):
    """v2 (2026-06-01): deed_dates carries None for years with no recorded
    deed_txfr_date so the caller can decide blank vs fallback per-row."""
    rows = [
        ("B1", 2021, "OWNER A", None),
        ("B1", 2022, "OWNER A", dt.date(2022, 5, 1)),
        ("B1", 2023, "OWNER B", None),
    ]
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__ = lambda s: cur
    cur.__exit__ = lambda *a: False
    conn = MagicMock()
    conn.cursor.return_value = cur
    monkeypatch.setattr(main, "get_conn", lambda: conn)
    monkeypatch.setattr(main, "release_conn", lambda c: None)

    out = main._fetch_ownership_history({("dcad", "B1")})
    assert out[("dcad", "B1")]["deed_dates"] == {
        2021: None,
        2022: dt.date(2022, 5, 1),
        2023: None,
    }
    # 'acquired' = deed_date of latest year, which is None here.
    assert out[("dcad", "B1")]["acquired"] is None
