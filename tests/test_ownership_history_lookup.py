import datetime as dt
from unittest.mock import MagicMock

import api.main as main


def test_fetch_ownership_history_pivots_and_picks_latest_deed(monkeypatch):
    # Mock cursor returns (account_num, snapshot_year, owner_name, deed_txfr_date)
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

    out = main._fetch_ownership_history({"A1", "A2"})
    assert out["A1"]["owners"] == {2021: "SHARPE SARA", 2022: "SHARPE SARA", 2023: "PARK JU YONG"}
    assert out["A1"]["acquired"] == dt.date(2023, 3, 14)   # latest year's deed
    # v2 (2026-06-01): per-year deed_dates map; the v2 caller picks the date
    # matching whichever year anchors the Current Owner after blank-skip.
    assert out["A1"]["deed_dates"] == {
        2021: dt.date(2010, 1, 1),
        2022: dt.date(2010, 1, 1),
        2023: dt.date(2023, 3, 14),
    }
    assert out["A2"]["owners"] == {2024: "WILSON LOIS"}
    assert out["A2"]["acquired"] == dt.date(2024, 8, 2)
    assert out["A2"]["deed_dates"] == {2024: dt.date(2024, 8, 2)}


def test_fetch_ownership_history_empty_input_skips_query(monkeypatch):
    monkeypatch.setattr(main, "get_conn", lambda: (_ for _ in ()).throw(AssertionError("should not connect")))
    assert main._fetch_ownership_history(set()) == {}


def test_fetch_ownership_history_swallows_db_error(monkeypatch):
    def boom():
        raise RuntimeError("table missing")
    monkeypatch.setattr(main, "get_conn", boom)
    assert main._fetch_ownership_history({"A1"}) == {}


def test_ownership_history_cells_none_is_six_blanks():
    assert main._ownership_history_cells(None) == ["", "", "", "", "", ""]


def test_ownership_history_cells_populated():
    hist = {"owners": {2021: "SHARPE SARA", 2023: "PARK JU YONG"},
            "acquired": dt.date(2023, 3, 14)}
    # OWNERSHIP_HISTORY_YEARS = [2021, 2022, 2023, 2024, 2025]
    assert main._ownership_history_cells(hist) == [
        "SHARPE SARA", "", "PARK JU YONG", "", "", "03/14/2023",
    ]


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

    out = main._fetch_ownership_history({"B1"})
    assert out["B1"]["deed_dates"] == {
        2021: None,
        2022: dt.date(2022, 5, 1),
        2023: None,
    }
    # 'acquired' = deed_date of latest year, which is None here.
    assert out["B1"]["acquired"] is None
