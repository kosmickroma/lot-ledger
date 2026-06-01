"""Tests for the Tarrant Appraisal District (TAD) ownership-history ingest.

Sibling of test_collin_ownership_history_ingest.py and the DCAD/Denton tests.
TAD's format is pipe-delimited (|) with a header row — same csv.DictReader
shape as Collin's CSV, but with three TAD-specific filters: RP in {R,C}
(skip Personal Property + Minerals), Record_Type == AAAA (skip LOCA
location rows), and Account_Num zero-strip.
"""
import datetime as dt
from pathlib import Path

import pytest

from scripts.ownership_history.build_tad_ownership_history import (
    _check_appraisal_year_matches_folder,
    _find_year_files,
    _parse_deed_date,
    _row_to_record,
    _stream_record_batches,
)


HEADER = (
    "RP|Appraisal_Year|Account_Num|Record_Type|Sequence_No|PIDN|Owner_Name|"
    "Owner_Address|Owner_CityState|Owner_Zip|Owner_Zip4|Owner_CRRT|"
    "Situs_Address|Property_Class|TAD_Map|MAPSCO|Exemption_Code|"
    "State_Use_Code|LegalDescription|Notice_Date|County|City|School|"
    "Num_Special_Dist|Spec1|Spec2|Spec3|Spec4|Spec5|Deed_Date|Deed_Book|"
    "Deed_Page|Land_Value|Improvement_Value|Total_Value|Garage_Capacity|"
    "Num_Bedrooms|Num_Bathrooms|Year_Built|Living_Area|Swimming_Pool_Ind|"
    "ARB_Indicator|Ag_Code|Land_Acres|Land_SqFt|Ag_Acres|Ag_Value|"
    "Central_Heat_Ind|Central_Air_Ind|Structure_Count|From_Accts|"
    "Appraisal_Date|Appraised_Value|GIS_Link|Instrument_No|Overlap_Flag"
)
# Index of each pipe-delim field we care about — keeps _row helper readable.
_FIELD_NAMES = HEADER.split("|")


def _row(**overrides: str) -> str:
    """Build one synthetic pipe-delimited row at the actual TAD header
    layout. Defaults to a happy-path Residential AAAA row; override
    individual fields by name."""
    base = {name: "" for name in _FIELD_NAMES}
    defaults = {
        "RP": "R",
        "Appraisal_Year": "2022",
        "Account_Num": "00000051",
        "Record_Type": "AAAA",
        "Sequence_No": "000",
        "Owner_Name": "FORT WORTH CITY OF",
        "Deed_Date": "04/17/2019",
    }
    base.update(defaults)
    base.update(overrides)
    return "|".join(base[name] for name in _FIELD_NAMES)


def _write_file(path: Path, *data_rows: str, header: str = HEADER) -> Path:
    """Write a CRLF-joined pipe-delimited file with the standard TAD header."""
    lines = [header, *data_rows]
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    return path


# ---------------------------------------------------------------------------
# _find_year_files
# ---------------------------------------------------------------------------

def test_find_year_files_nested_layout(tmp_path: Path):
    """TAD layout: <year>/PropertyData_<year>(Certified)/PropertyData_<year>.txt"""
    nested = tmp_path / "2022" / "PropertyData_2022(Certified)"
    nested.mkdir(parents=True)
    target = nested / "PropertyData_2022.txt"
    target.write_text("RP|...\n", encoding="utf-8")

    flat = tmp_path / "2024"
    flat.mkdir()
    (flat / "PropertyData_2024.txt").write_text("RP|...\n", encoding="utf-8")

    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2022, 2024}
    assert found[2022].endswith("PropertyData_2022.txt")
    assert found[2024].endswith("PropertyData_2024.txt")


def test_find_year_files_zero_match_skips_year(tmp_path: Path, capsys):
    empty = tmp_path / "2023"
    empty.mkdir()
    (empty / "README.md").write_text("not the data file", encoding="utf-8")
    assert _find_year_files(str(tmp_path)) == {}
    assert "WARNING" in capsys.readouterr().out


def test_find_year_files_multi_match_fails_year(tmp_path: Path):
    """Two PropertyData_<year>.txt files under one year folder → SystemExit."""
    y = tmp_path / "2021"
    a, b = y / "first", y / "second"
    a.mkdir(parents=True)
    b.mkdir()
    (a / "PropertyData_2021.txt").write_text("x", encoding="utf-8")
    (b / "PropertyData_2021.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _find_year_files(str(tmp_path))
    assert "Multiple PropertyData_2021.txt" in str(exc.value)


def test_find_year_files_excludes_macosx_sidecars(tmp_path: Path):
    """__MACOSX/._...PropertyData...txt sidecars must NOT cause a spurious
    multi-match failure when only one real file exists."""
    y = tmp_path / "2025"
    real = y / "PropertyData_2025(Certified)"
    macos = y / "__MACOSX" / "PropertyData_2025(Certified)"
    real.mkdir(parents=True)
    macos.mkdir(parents=True)
    (real / "PropertyData_2025.txt").write_text("x", encoding="utf-8")
    (macos / "._PropertyData_2025.txt").write_text("x", encoding="utf-8")
    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2025}
    assert "__MACOSX" not in found[2025]


def test_find_year_files_rejects_year_outside_1900_2100(tmp_path: Path):
    bad = tmp_path / "0000"
    bad.mkdir()
    (bad / "PropertyData_0000.txt").write_text("x", encoding="utf-8")
    good = tmp_path / "2024"
    good.mkdir()
    (good / "PropertyData_2024.txt").write_text("x", encoding="utf-8")
    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2024}


def test_find_year_files_case_insensitive_filename(tmp_path: Path):
    """Tolerant of filename-case drift (PROPERTYDATA_2024.TXT etc.)."""
    d = tmp_path / "2024"
    d.mkdir()
    (d / "PROPERTYDATA_2024.TXT").write_text("x", encoding="utf-8")
    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2024}


# ---------------------------------------------------------------------------
# _parse_deed_date
# ---------------------------------------------------------------------------

def test_parse_deed_date_happy_path():
    assert _parse_deed_date("04/17/2019") == dt.date(2019, 4, 17)
    assert _parse_deed_date("01/01/2020") == dt.date(2020, 1, 1)


def test_parse_deed_date_blank_zero_garbage():
    assert _parse_deed_date("") is None
    assert _parse_deed_date(None) is None
    assert _parse_deed_date("   ") is None
    assert _parse_deed_date("00/00/0000") is None
    assert _parse_deed_date("garbage") is None
    assert _parse_deed_date("99/99/9999") is None  # invalid month/day


def test_parse_deed_date_year_out_of_range():
    # 1900-2100 inclusive
    assert _parse_deed_date("01/01/1900") == dt.date(1900, 1, 1)
    assert _parse_deed_date("12/31/2100") == dt.date(2100, 12, 31)
    # outside range
    assert _parse_deed_date("01/01/1899") is None
    assert _parse_deed_date("01/01/2101") is None
    assert _parse_deed_date("01/01/5000") is None


# ---------------------------------------------------------------------------
# _row_to_record (shape + skip conditions)
# ---------------------------------------------------------------------------

def test_row_to_record_happy_path_residential():
    """A normal Residential AAAA row → upsert tuple with account_num
    lstripped of leading zeros."""
    row = {
        "RP": "R",
        "Appraisal_Year": "2022",
        "Account_Num": "00000051",
        "Record_Type": "AAAA",
        "Owner_Name": "FORT WORTH CITY OF",
        "Deed_Date": "04/17/2019",
    }
    assert _row_to_record(row, 2022, "PropertyData_2022.txt") == (
        "tad",
        "51",
        2022,
        "FORT WORTH CITY OF",
        dt.date(2019, 4, 17),
        "PropertyData_2022.txt",
    )


def test_row_to_record_happy_path_commercial():
    """Commercial (RP='C') is also kept."""
    row = {
        "RP": "C",
        "Account_Num": "12345678",
        "Record_Type": "AAAA",
        "Owner_Name": "ACME LLC",
        "Deed_Date": "",
    }
    rec = _row_to_record(row, 2024, "f.txt")
    assert rec is not None
    assert rec[3] == "ACME LLC"
    assert rec[4] is None  # blank deed_date → None


def test_row_to_record_skips_personal_property_and_minerals():
    """RP='P' (Personal Property) and RP='M' (Minerals) are out of scope —
    we only track real-property ownership history."""
    for rp in ("P", "M"):
        row = {"RP": rp, "Account_Num": "100", "Record_Type": "AAAA",
               "Owner_Name": "ANY", "Deed_Date": ""}
        assert _row_to_record(row, 2022, "f.txt") is None


def test_row_to_record_skips_non_aaaa_records():
    """LOCA (location) rows carry blank/garbage Owner_Name; the doc says
    AAAA is the primary real-estate record. Only AAAA counts."""
    row = {"RP": "R", "Account_Num": "100", "Record_Type": "LOCA",
           "Owner_Name": "GARBAGE", "Deed_Date": ""}
    assert _row_to_record(row, 2022, "f.txt") is None


def test_row_to_record_strips_account_leading_zeros():
    """TAD stores Account_Num as 8-char zero-padded; the rest of LotLedger
    uses the unpadded form."""
    row = {"RP": "R", "Account_Num": "00000051", "Record_Type": "AAAA",
           "Owner_Name": "X", "Deed_Date": ""}
    rec = _row_to_record(row, 2022, "f.txt")
    assert rec is not None
    assert rec[1] == "51"

    # All-zeros account → empty after lstrip → skipped.
    row["Account_Num"] = "00000000"
    assert _row_to_record(row, 2022, "f.txt") is None


def test_row_to_record_skips_blank_owner_name():
    row = {"RP": "R", "Account_Num": "100", "Record_Type": "AAAA",
           "Owner_Name": "   ", "Deed_Date": ""}
    assert _row_to_record(row, 2022, "f.txt") is None


def test_row_to_record_rp_and_record_type_case_insensitive():
    """Defensive: lowercase 'r' / 'aaaa' should still match (the doc's all-
    caps form is what's shipped, but a future case-drift mustn't drop rows)."""
    row = {"RP": "r", "Account_Num": "100", "Record_Type": "aaaa",
           "Owner_Name": "OWNER", "Deed_Date": ""}
    assert _row_to_record(row, 2022, "f.txt") is not None


# ---------------------------------------------------------------------------
# _check_appraisal_year_matches_folder
# ---------------------------------------------------------------------------

def test_check_appraisal_year_warns_on_mismatch(capsys):
    _check_appraisal_year_matches_folder({"Appraisal_Year": "2021"}, 2022, "f.txt")
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "Appraisal_Year=2021" in out
    assert "year=2022" in out


def test_check_appraisal_year_silent_on_missing_blank_garbage_match(capsys):
    _check_appraisal_year_matches_folder({}, 2022, "f.txt")
    _check_appraisal_year_matches_folder({"Appraisal_Year": ""}, 2022, "f.txt")
    _check_appraisal_year_matches_folder({"Appraisal_Year": "abc"}, 2022, "f.txt")
    _check_appraisal_year_matches_folder({"Appraisal_Year": "2022"}, 2022, "f.txt")
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# _stream_record_batches (end-to-end on tmp files)
# ---------------------------------------------------------------------------

def test_stream_record_batches_filters_and_emits(tmp_path: Path, capsys):
    """End-to-end: a mix of R/C (kept), P/M (skipped), LOCA (skipped),
    blank-account (skipped), blank-owner (skipped). Summary counters
    record every skip reason."""
    txt = tmp_path / "PropertyData_2022.txt"
    _write_file(
        txt,
        _row(RP="R", Account_Num="00000051", Owner_Name="OWNER R", Deed_Date="01/02/2020"),
        _row(RP="C", Account_Num="00000099", Owner_Name="OWNER C", Deed_Date="03/04/2021"),
        _row(RP="P", Account_Num="00000123", Owner_Name="PERSONAL"),       # skipped
        _row(RP="M", Account_Num="00000124", Owner_Name="MINERAL"),        # skipped
        _row(RP="R", Account_Num="00000125", Record_Type="LOCA",           # skipped
             Owner_Name="LOCA_GARBAGE"),
        _row(RP="R", Account_Num="00000000", Owner_Name="ALL ZEROS"),      # skipped
        _row(RP="R", Account_Num="00000126", Owner_Name=""),               # skipped
    )
    batches = list(_stream_record_batches(str(txt), 2022, txt.name, batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert sorted(r[1] for r in flat) == ["51", "99"]
    out = capsys.readouterr().out
    assert "scanned=7" in out
    assert "non_real(PP+M)=2" in out
    assert "non_aaaa=1" in out
    assert "blank_acct=1" in out
    assert "blank_owner=1" in out
    assert "emitted=2" in out


def test_stream_record_batches_handles_utf8_bom(tmp_path: Path):
    """A BOM-prefixed file would otherwise make the first header column
    '\\ufeffRP' and silently drop every row. encoding='utf-8-sig' must
    transparently strip it."""
    txt = tmp_path / "PropertyData_2024.txt"
    body = (
        HEADER + "\r\n"
        + _row(RP="R", Account_Num="00000001", Owner_Name="A") + "\r\n"
    )
    txt.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    batches = list(_stream_record_batches(str(txt), 2024, txt.name, batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert len(flat) == 1
    assert flat[0][1] == "1"
    assert flat[0][3] == "A"


def test_stream_record_batches_warns_on_appraisal_year_mismatch(
    tmp_path: Path, capsys
):
    """First-row Appraisal_Year disagrees with folder year → warning,
    ingest writes the row under folder year (not the row's year)."""
    txt = tmp_path / "PropertyData_2025.txt"
    _write_file(
        txt,
        _row(RP="R", Appraisal_Year="2023", Account_Num="00000001",
             Owner_Name="OWNER"),
    )
    batches = list(_stream_record_batches(str(txt), 2025, txt.name, batch_size=10))
    out = capsys.readouterr().out
    flat = [rec for b in batches for rec in b]
    assert "WARNING" in out
    assert "Appraisal_Year=2023" in out
    assert "year=2025" in out
    assert flat[0][2] == 2025  # folder year wins


def test_stream_record_batches_bounded(tmp_path: Path):
    """Yield in chunks of `batch_size` (last batch may be smaller). Lock
    in so a refactor can't accidentally yield the full file as one batch."""
    txt = tmp_path / "PropertyData_2022.txt"
    rows = [
        _row(RP="R", Account_Num=f"0000010{i}", Owner_Name=f"O{i}")
        for i in range(7)
    ]
    _write_file(txt, *rows)
    batches = list(_stream_record_batches(str(txt), 2022, txt.name, batch_size=3))
    assert [len(b) for b in batches] == [3, 3, 1]


def test_stream_record_batches_embedded_comma_in_owner(tmp_path: Path):
    """Owner names with embedded commas (' SANTA CRUZ, RICHARD') must NOT
    confuse parsing — pipe delimiter ignores commas, no quoting needed."""
    txt = tmp_path / "PropertyData_2022.txt"
    _write_file(
        txt,
        _row(RP="R", Account_Num="00000051",
             Owner_Name="SANTA CRUZ, RICHARD & LEA", Deed_Date="01/01/2020"),
    )
    batches = list(_stream_record_batches(str(txt), 2022, txt.name, batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert flat[0][3] == "SANTA CRUZ, RICHARD & LEA"
