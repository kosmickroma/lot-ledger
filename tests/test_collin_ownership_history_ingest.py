"""Tests for the Collin CAD ownership-history ingest script.

Sibling of tests/test_dcad_ownership_history_ingest.py — same shape, but
Collin's layout is flatter (single CSV per year folder, no nested
'CERTIFIED' subfolder) and the CSV uses different column names + UTF-8
encoding.
"""
import datetime as dt
from pathlib import Path

from scripts.ownership_history.build_collin_ownership_history import (
    _find_year_files,
    _parse_deed_date,
    _row_to_record,
    _stream_record_batches,
)


def test_find_year_files_flat_layout(tmp_path: Path):
    """Collin layout: <year>/collin_<year>.csv directly (no nested subfolder
    like DCAD's CERTIFIED_<date> path)."""
    d20 = tmp_path / "2020"
    d20.mkdir()
    (d20 / "collin_2020.csv").write_text("propYear,propID\n2020,1\n")
    d22 = tmp_path / "2022"
    d22.mkdir()
    (d22 / "collin_2022.csv").write_text("propYear,propID\n2022,1\n")

    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2020, 2022}
    assert found[2020].endswith("collin_2020.csv")
    assert found[2022].endswith("collin_2022.csv")


def test_find_year_files_ignores_non_year_subfolders(tmp_path: Path):
    """A non-4-digit-numeric subfolder must be skipped (KK already had a
    'cad/' archive folder under ingest/counties/collin/ before this ingest
    pipeline existed)."""
    d20 = tmp_path / "2020"
    d20.mkdir()
    (d20 / "collin_2020.csv").write_text("propYear,propID\n2020,1\n")

    # Pre-existing non-year folder — must be ignored, no crash.
    cad = tmp_path / "cad"
    cad.mkdir()
    (cad / "legacy_export.csv").write_text("something else\n")

    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2020}


def test_find_year_files_case_insensitive_filename(tmp_path: Path):
    """Tolerant of future filename-case drift (e.g. COLLIN_2024.csv)."""
    d24 = tmp_path / "2024"
    d24.mkdir()
    (d24 / "COLLIN_2024.csv").write_text("propYear,propID\n2024,1\n")
    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2024}
    assert found[2024].endswith("COLLIN_2024.csv")


def test_find_year_files_missing_dir_returns_empty(tmp_path: Path):
    assert _find_year_files(str(tmp_path / "nope")) == {}


def test_parse_deed_date():
    # Same MM/DD/YYYY format as DCAD; same edge cases.
    assert _parse_deed_date("04/17/2019") == dt.date(2019, 4, 17)
    assert _parse_deed_date("") is None
    assert _parse_deed_date(None) is None
    assert _parse_deed_date("00/00/0000") is None
    assert _parse_deed_date("GARBAGE") is None


def test_row_to_record_shape_and_tolerance():
    """Collin column names: propID / ownerName / deedEffDate."""
    row = {
        "propID": " 95649 ",
        "ownerName": " SANTA CRUZ RICHARD & ",
        "deedEffDate": "04/17/2019",
    }
    rec = _row_to_record(row, 2022, "collin_2022.csv")
    assert rec == (
        "collin",
        "95649",
        2022,
        "SANTA CRUZ RICHARD &",
        dt.date(2019, 4, 17),
        "collin_2022.csv",
    )

    # Missing optional columns tolerated; owner_name None, deed None.
    rec2 = _row_to_record({"propID": "X1"}, 2020, "f.csv")
    assert rec2 == ("collin", "X1", 2020, None, None, "f.csv")

    # Blank propID → None (caller skips).
    assert _row_to_record({"propID": "  "}, 2021, "f.csv") is None


def test_row_to_record_ignores_addtl_owner():
    """ownerNameAddtl (co-owner) is intentionally NOT folded into owner_name —
    co-ownership is the separate 'Owner 2 Name / %' CSV-export feature."""
    row = {
        "propID": "100",
        "ownerName": "BENNETT RODGER PRINCE &",
        "ownerNameAddtl": "DEBORA ANN CHARLES",
        "deedEffDate": "10/22/2019",
    }
    rec = _row_to_record(row, 2022, "collin_2022.csv")
    # owner_name field is ownerName ONLY, not concatenated with ownerNameAddtl.
    assert rec is not None
    assert rec[3] == "BENNETT RODGER PRINCE &"


def test_row_to_record_uses_deed_eff_not_file_date():
    """deedEffDate (effective) is what we record, not deedFileDate
    (administrative). Even when deedFileDate is present and effDate is blank,
    we keep deed_txfr_date as None."""
    row = {
        "propID": "100",
        "ownerName": "OWNER",
        "deedEffDate": "",
        "deedFileDate": "01/02/2020",
    }
    rec = _row_to_record(row, 2022, "f.csv")
    assert rec[4] is None  # deed_txfr_date = None even though deedFileDate exists


def test_stream_record_batches_bounded_skips_blank_and_preserves_order(tmp_path: Path):
    """Bounded-batch streaming with Collin column names + UTF-8 encoding.
    Row 2 has a blank propID and must be skipped. Embedded commas inside
    quoted fields ('SANTA CRUZ RICHARD &, LEA') must NOT confuse parsing —
    this is a frequent shape in Collin's CSV."""
    csv_text = (
        'propID,ownerName,deedEffDate,deedFileDate\n'
        'A1,OWNER A,01/02/2020,01/05/2020\n'
        ',OWNER SKIP,03/04/2021,03/05/2021\n'
        'A2,OWNER B,00/00/0000,00/00/0000\n'
        'A3,OWNER C,,\n'
        'A4,,05/06/2022,05/06/2022\n'
        'A5,"SANTA CRUZ, RICHARD & LEA",07/08/2023,07/09/2023\n'
    )
    csv_path = tmp_path / "collin_2024.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    batches = list(
        _stream_record_batches(str(csv_path), 2024, "collin_2024.csv", batch_size=2)
    )

    # Memory-safety: every batch is bounded by batch_size.
    assert all(len(b) <= 2 for b in batches)
    # 5 valid records (blank-propID row dropped) → [2, 2, 1].
    assert [len(b) for b in batches] == [2, 2, 1]

    flat = [rec for b in batches for rec in b]
    assert flat == [
        ("collin", "A1", 2024, "OWNER A", dt.date(2020, 1, 2), "collin_2024.csv"),
        ("collin", "A2", 2024, "OWNER B", None, "collin_2024.csv"),
        ("collin", "A3", 2024, "OWNER C", None, "collin_2024.csv"),
        ("collin", "A4", 2024, None, dt.date(2022, 5, 6), "collin_2024.csv"),
        # Embedded comma inside quoted ownerName must be preserved.
        ("collin", "A5", 2024, "SANTA CRUZ, RICHARD & LEA", dt.date(2023, 7, 8), "collin_2024.csv"),
    ]
