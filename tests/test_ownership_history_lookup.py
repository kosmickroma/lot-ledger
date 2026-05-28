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
    assert out["A2"]["owners"] == {2024: "WILSON LOIS"}
    assert out["A2"]["acquired"] == dt.date(2024, 8, 2)


def test_fetch_ownership_history_empty_input_skips_query(monkeypatch):
    monkeypatch.setattr(main, "get_conn", lambda: (_ for _ in ()).throw(AssertionError("should not connect")))
    assert main._fetch_ownership_history(set()) == {}


def test_fetch_ownership_history_swallows_db_error(monkeypatch):
    def boom():
        raise RuntimeError("table missing")
    monkeypatch.setattr(main, "get_conn", boom)
    assert main._fetch_ownership_history({"A1"}) == {}
