import datetime as dt
from pathlib import Path

from scripts.ownership_history.build_dcad_ownership_history import (
    _find_year_files,
    _parse_deed_date,
    _row_to_record,
)


def test_find_year_files_mixed_case_and_nesting(tmp_path: Path):
    # 2021 lowercase, nested in a CERTIFIED subfolder
    d21 = tmp_path / "2021" / "DCAD2021_CERTIFIED_07232021"
    d21.mkdir(parents=True)
    (d21 / "account_info.csv").write_text("x")
    # 2023 uppercase, nested
    d23 = tmp_path / "2023" / "DCAD2023_CERTIFIED_07252023"
    d23.mkdir(parents=True)
    (d23 / "ACCOUNT_INFO.CSV").write_text("x")
    # a non-year folder is ignored
    (tmp_path / "notes").mkdir()

    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2021, 2023}
    assert found[2021].endswith("account_info.csv")
    assert found[2023].endswith("ACCOUNT_INFO.CSV")


def test_parse_deed_date():
    assert _parse_deed_date("04/05/1999") == dt.date(1999, 4, 5)
    assert _parse_deed_date("") is None
    assert _parse_deed_date(None) is None
    assert _parse_deed_date("00/00/0000") is None
    assert _parse_deed_date("GARBAGE") is None


def test_row_to_record_shape_and_tolerance():
    row = {"ACCOUNT_NUM": " 00000661740030000 ", "OWNER_NAME1": " STILLE PATRICIA M ",
           "DEED_TXFR_DATE": "03/14/2023"}
    rec = _row_to_record(row, 2023, "account_info.csv")
    assert rec == ("dcad", "00000661740030000", 2023, "STILLE PATRICIA M",
                   dt.date(2023, 3, 14), "account_info.csv")

    # Missing optional columns tolerated; owner_name None, deed None.
    rec2 = _row_to_record({"ACCOUNT_NUM": "X1"}, 1999, "f.csv")
    assert rec2 == ("dcad", "X1", 1999, None, None, "f.csv")

    # Blank account_num → None (caller skips).
    assert _row_to_record({"ACCOUNT_NUM": "  "}, 2021, "f.csv") is None
