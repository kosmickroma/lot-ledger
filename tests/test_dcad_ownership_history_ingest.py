import datetime as dt
from pathlib import Path

from scripts.ownership_history.build_dcad_ownership_history import (
    _find_year_files,
    _parse_deed_date,
    _row_to_record,
    _stream_record_batches,
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


def test_stream_record_batches_bounded_skips_blank_and_preserves_order(tmp_path: Path):
    # Header + 6 data rows; row 2 has a blank ACCOUNT_NUM and must be skipped.
    csv_text = (
        "ACCOUNT_NUM,OWNER_NAME1,DEED_TXFR_DATE,JUNK\n"
        "A1,OWNER A,01/02/2020,x\n"
        ",OWNER SKIP,03/04/2021,x\n"
        "A2,OWNER B,00/00/0000,x\n"
        "A3,OWNER C,,x\n"
        "A4,,05/06/2022,x\n"
        "A5,OWNER E,07/08/2023,x\n"
    )
    csv_path = tmp_path / "account_info.csv"
    csv_path.write_text(csv_text, encoding="latin-1")

    batches = list(
        _stream_record_batches(str(csv_path), 2024, "account_info.csv", batch_size=2)
    )

    # Memory-safety property: every batch is bounded by batch_size.
    assert all(len(b) <= 2 for b in batches)
    # 5 valid records (blank-account row dropped) → [2, 2, 1].
    assert [len(b) for b in batches] == [2, 2, 1]

    flat = [rec for b in batches for rec in b]
    assert flat == [
        ("dcad", "A1", 2024, "OWNER A", dt.date(2020, 1, 2), "account_info.csv"),
        ("dcad", "A2", 2024, "OWNER B", None, "account_info.csv"),
        ("dcad", "A3", 2024, "OWNER C", None, "account_info.csv"),
        ("dcad", "A4", 2024, None, dt.date(2022, 5, 6), "account_info.csv"),
        ("dcad", "A5", 2024, "OWNER E", dt.date(2023, 7, 8), "account_info.csv"),
    ]
