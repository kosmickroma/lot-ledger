"""Tests for the Denton CAD ownership-history ingest script.

Sibling of test_dcad_ownership_history_ingest.py and
test_collin_ownership_history_ingest.py — same shape, but Denton's source is
a fixed-width PACS "Appraisal Export" (byte-position-defined columns, no
headers, ~4 GB per year, variable row length). See
docs/superpowers/specs/2026-06-01-denton-ownership-history-design.md for the
locked field offsets, the multi-owner dedup rule, and the date-format
matrix.
"""
import contextlib
import datetime as dt
import io
from pathlib import Path

import pytest

from scripts.ownership_history.build_denton_ownership_history import (
    MIN_ROW_LEN,
    _check_propyear_matches_folder,
    _find_year_files,
    _parse_pacs_date,
    _row_to_record,
    _stream_dedup_records,
)


# ---------------------------------------------------------------------------
# Synthetic-row helper. Builds a fixed-width PACS-shape line at the exact
# offsets locked in the spec. Defaults yield a "happy path" row that
# survives every skip filter; override per-test to exercise edge cases.
# ---------------------------------------------------------------------------

def _row(
    *,
    prop_id: str = "000000111625",      # 12 chars (zero-padded numeric)
    prop_val_yr: str = "2022 ",          # 5 chars
    sup_num: str = "000000000000",       # 12 chars (certified == all zeros)
    py_owner_id: str = "000000123456",   # 12 chars
    py_owner_name: str = "JOHN DOE",     # ≤70 chars (left-padded with spaces)
    deed_dt: str = "12141993",           # ≤25 chars
    row_len: int = MIN_ROW_LEN,
) -> str:
    """Construct a synthetic fixed-width Denton row.

    `row_len` may be set BELOW MIN_ROW_LEN to test the short-row skip path.
    Each field is placed at its exact 0-based-exclusive offset and padded
    with spaces to its declared width; longer values are truncated.
    """
    buf = list(" " * row_len)

    def put(start: int, end: int, value: str) -> None:
        width = end - start
        if start >= row_len:
            return
        end_clamped = min(end, row_len)
        width_clamped = end_clamped - start
        padded = value.ljust(width)[:width_clamped]
        buf[start:end_clamped] = list(padded)

    put(0, 12, prop_id)
    put(17, 22, prop_val_yr)
    put(22, 34, sup_num)
    put(596, 608, py_owner_id)
    put(608, 678, py_owner_name)
    put(2033, 2058, deed_dt)
    return "".join(buf)


def _write_file(path: Path, *lines: str) -> Path:
    """Write a CRLF-joined file at `path`. Final line has no trailing CRLF
    unless caller appends one explicitly — useful for the
    missing-final-CRLF tolerance test."""
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("latin-1"))
    return path


# ---------------------------------------------------------------------------
# _find_year_files
# ---------------------------------------------------------------------------

def test_find_year_files_flat_recursive_layout(tmp_path: Path):
    """Recursive search finds the TXT inside Denton's nested subfolders."""
    nested = tmp_path / "2022" / "CertifiedData-All Property" / "deep"
    nested.mkdir(parents=True)
    target = nested / "2022-09-09_006820_APPRAISAL_INFO.TXT"
    target.write_text("dummy\n", encoding="latin-1")

    flat = tmp_path / "2024" / "APPRAISAL_INFO.TXT"
    flat.parent.mkdir()
    flat.write_text("dummy\n", encoding="latin-1")

    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2022, 2024}
    assert found[2022].endswith("2022-09-09_006820_APPRAISAL_INFO.TXT")
    assert found[2024].endswith("APPRAISAL_INFO.TXT")


def test_find_year_files_zero_match_skips_year(tmp_path: Path, capsys):
    """Year folder with no APPRAISAL_INFO.TXT prints a warning and skips."""
    empty = tmp_path / "2023"
    empty.mkdir()
    (empty / "README.md").write_text("not a roll", encoding="latin-1")

    found = _find_year_files(str(tmp_path))
    captured = capsys.readouterr()
    assert found == {}
    assert "WARNING" in captured.out
    assert "no *APPRAISAL_INFO.TXT" in captured.out


def test_find_year_files_multi_match_fails_year(tmp_path: Path):
    """Two APPRAISAL_INFO.TXT files under one year folder → SystemExit."""
    y = tmp_path / "2021"
    a = y / "first"
    b = y / "second"
    a.mkdir(parents=True)
    b.mkdir()
    (a / "APPRAISAL_INFO.TXT").write_text("x", encoding="latin-1")
    (b / "2021-09-01_999999_APPRAISAL_INFO.TXT").write_text("x", encoding="latin-1")

    with pytest.raises(SystemExit) as exc:
        _find_year_files(str(tmp_path))
    msg = str(exc.value)
    assert "Multiple APPRAISAL_INFO.TXT" in msg
    assert "first" in msg and "second" in msg


def test_find_year_files_excludes_macosx_sidecars(tmp_path: Path):
    """__MACOSX/._...APPRAISAL_INFO.TXT sidecars (added when zips are opened
    on macOS) must NOT count as a real match — that would otherwise cause a
    spurious multi-match SystemExit when only one real file exists."""
    y = tmp_path / "2025"
    real_dir = y / "CertifiedData-All Property"
    macos_dir = y / "__MACOSX" / "CertifiedData-All Property"
    real_dir.mkdir(parents=True)
    macos_dir.mkdir(parents=True)
    (real_dir / "APPRAISAL_INFO.TXT").write_text("real", encoding="latin-1")
    (macos_dir / "._APPRAISAL_INFO.TXT").write_text("sidecar", encoding="latin-1")

    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2025}
    assert "__MACOSX" not in found[2025]


def test_find_year_files_rejects_year_outside_1900_2100(tmp_path: Path):
    """Accidental '0000' or '9999' folder must not produce a row."""
    bad = tmp_path / "0000"
    bad.mkdir()
    (bad / "APPRAISAL_INFO.TXT").write_text("x", encoding="latin-1")
    good = tmp_path / "2024"
    good.mkdir()
    (good / "APPRAISAL_INFO.TXT").write_text("x", encoding="latin-1")

    found = _find_year_files(str(tmp_path))
    assert set(found.keys()) == {2024}


# ---------------------------------------------------------------------------
# _parse_pacs_date
# ---------------------------------------------------------------------------

def test_parse_pacs_date_mmddyyyy_no_sep():
    assert _parse_pacs_date("12141993") == dt.date(1993, 12, 14)
    assert _parse_pacs_date("01012020") == dt.date(2020, 1, 1)


def test_parse_pacs_date_mm_dd_yyyy_dashes():
    assert _parse_pacs_date("12-14-1993") == dt.date(1993, 12, 14)
    assert _parse_pacs_date("01-01-2020") == dt.date(2020, 1, 1)


def test_parse_pacs_date_blank_and_zero_and_garbage():
    assert _parse_pacs_date("") is None
    assert _parse_pacs_date(None) is None
    assert _parse_pacs_date("   ") is None
    assert _parse_pacs_date("00000000") is None
    assert _parse_pacs_date("00-00-0000") is None
    assert _parse_pacs_date("garbage") is None
    assert _parse_pacs_date("99999999") is None  # 99/99/9999 — invalid month/day


def test_parse_pacs_date_year_out_of_range():
    """Year 5000 placeholder must NOT be returned (would poison sorts)."""
    # 5000 is parseable as a date but outside 1900-2100 — must return None.
    assert _parse_pacs_date("01015000") is None
    assert _parse_pacs_date("01-01-5000") is None
    # Boundary: 1900 and 2100 inclusive.
    assert _parse_pacs_date("01011900") == dt.date(1900, 1, 1)
    assert _parse_pacs_date("12312100") == dt.date(2100, 12, 31)
    # 1899 and 2101 rejected.
    assert _parse_pacs_date("01011899") is None
    assert _parse_pacs_date("01012101") is None


# ---------------------------------------------------------------------------
# _row_to_record (shape and shared skip-condition mapper)
# ---------------------------------------------------------------------------

def test_row_to_record_returns_correct_tuple_shape():
    """Happy-path row → (record_tuple, py_owner_id_int).
    Record shape matches the UPSERT columns:
        (county, account_num, snapshot_year, owner_name, deed, source_file)
    """
    line = _row(
        prop_id="000000111625",
        py_owner_id="000000999999",
        py_owner_name="SMITH JOHN AND JANE",
        deed_dt="04171999",
    )
    out = _row_to_record(line, 2022, "APPRAISAL_INFO.TXT")
    assert out is not None
    record, py_owner_id = out
    assert record == (
        "denton",
        "111625",
        2022,
        "SMITH JOHN AND JANE",
        dt.date(1999, 4, 17),
        "APPRAISAL_INFO.TXT",
    )
    assert py_owner_id == 999999


# ---------------------------------------------------------------------------
# _stream_dedup_records (end-to-end with a tmp file)
# ---------------------------------------------------------------------------

def test_stream_dedup_lowest_py_owner_id_wins_when_lower_arrives_later(tmp_path: Path):
    """Same prop_id appearing twice — the LOWER py_owner_id row wins, even
    when it arrives second in the file."""
    txt = tmp_path / "x.TXT"
    _write_file(
        txt,
        _row(
            prop_id="000000111625",
            py_owner_id="000000888888",
            py_owner_name="HIGH ID OWNER",
        ),
        _row(
            prop_id="000000111625",
            py_owner_id="000000222222",
            py_owner_name="LOW ID OWNER",
        ),
    )
    batches = list(_stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert len(flat) == 1
    assert flat[0][1] == "111625"
    assert flat[0][3] == "LOW ID OWNER"


def test_stream_dedup_same_py_owner_id_different_name_first_seen_wins_and_logs(
    tmp_path: Path, capsys
):
    """Defensive: when PACS exposes the same prop_id + py_owner_id with two
    different names (shouldn't normally happen), keep first-seen and surface
    the count in the per-file summary so anomalies are visible."""
    txt = tmp_path / "x.TXT"
    _write_file(
        txt,
        _row(
            prop_id="000000111625",
            py_owner_id="000000555555",
            py_owner_name="FIRST NAME",
        ),
        _row(
            prop_id="000000111625",
            py_owner_id="000000555555",
            py_owner_name="SECOND NAME",
        ),
    )
    batches = list(_stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert len(flat) == 1
    assert flat[0][3] == "FIRST NAME"
    out = capsys.readouterr().out
    assert "same_id_diff_name=1" in out


def test_stream_dedup_short_row_skipped_with_counter(tmp_path: Path, capsys):
    """Row shorter than MIN_ROW_LEN must be skipped and counted, not parsed
    into a half-blank record. The valid row should still come through."""
    txt = tmp_path / "x.TXT"
    short = _row(prop_id="000000999999", row_len=1000)
    valid = _row(prop_id="000000111625", py_owner_name="VALID")
    _write_file(txt, short, valid)
    batches = list(_stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert len(flat) == 1
    assert flat[0][1] == "111625"
    out = capsys.readouterr().out
    assert "short=1" in out


def test_stream_dedup_blank_prop_id_after_strip_skipped(tmp_path: Path, capsys):
    """A row whose prop_id field is all zeros lstrips to empty → skipped."""
    txt = tmp_path / "x.TXT"
    _write_file(
        txt,
        _row(prop_id="000000000000", py_owner_name="ZERO OWNER"),
        _row(prop_id="000000111625", py_owner_name="REAL OWNER"),
    )
    batches = list(_stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert len(flat) == 1
    assert flat[0][1] == "111625"
    out = capsys.readouterr().out
    assert "blank_propid=1" in out


def test_stream_dedup_blank_owner_name_skipped(tmp_path: Path, capsys):
    """A row with whitespace-only py_owner_name → skipped (would otherwise
    write a row with owner_name=''  which the v2 distinct-owners algorithm
    can't classify)."""
    txt = tmp_path / "x.TXT"
    _write_file(
        txt,
        _row(prop_id="000000111625", py_owner_name=""),
        _row(prop_id="000000222222", py_owner_name="REAL OWNER"),
    )
    batches = list(_stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert len(flat) == 1
    assert flat[0][1] == "222222"
    out = capsys.readouterr().out
    assert "blank_owner=1" in out


def test_stream_dedup_non_ascii_owner_name_passes_through_latin1(tmp_path: Path):
    """Latin-1 superset bytes (e.g. ñ at byte 0xF1) must pass through cleanly
    — encoding='latin-1' makes any byte legal and preserves the offset
    arithmetic."""
    txt = tmp_path / "x.TXT"
    _write_file(
        txt,
        _row(prop_id="000000111625", py_owner_name="MUÑOZ JUAN"),
    )
    batches = list(_stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert len(flat) == 1
    assert flat[0][3] == "MUÑOZ JUAN"


def test_stream_dedup_mixed_date_formats_within_one_file(tmp_path: Path):
    """Two rows in the same file with different date formats — both must
    parse correctly (defensive against vintage drift inside a single file)."""
    txt = tmp_path / "x.TXT"
    _write_file(
        txt,
        _row(prop_id="000000100001", py_owner_name="OWNER A", deed_dt="12141993"),
        _row(prop_id="000000100002", py_owner_name="OWNER B", deed_dt="04-17-2019"),
    )
    batches = list(_stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=10))
    flat = sorted((rec for b in batches for rec in b), key=lambda r: r[1])
    assert len(flat) == 2
    assert flat[0][4] == dt.date(1993, 12, 14)
    assert flat[1][4] == dt.date(2019, 4, 17)


def test_stream_dedup_missing_final_crlf_tolerated(tmp_path: Path):
    """A file with no trailing CRLF on the last line must still ingest that
    last row — Python's text-mode `for line in fh` handles this transparently
    but lock it in so a refactor doesn't accidentally re-introduce a final-
    line bug."""
    txt = tmp_path / "x.TXT"
    a = _row(prop_id="000000111625", py_owner_name="OWNER A")
    b = _row(prop_id="000000222222", py_owner_name="OWNER B")
    # Hand-write WITHOUT a trailing CRLF on the last line.
    txt.write_bytes((a + "\r\n" + b).encode("latin-1"))
    batches = list(_stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert {r[1] for r in flat} == {"111625", "222222"}


def test_stream_dedup_skips_non_certified_sup_num(tmp_path: Path, capsys):
    """sup_num != "000000000000" (i.e. a supplemental, not the certified
    base row) must be skipped and counted. Supplements are out-of-scope for
    this ingest by design."""
    txt = tmp_path / "x.TXT"
    _write_file(
        txt,
        _row(
            prop_id="000000111625",
            sup_num="000000000001",
            py_owner_name="SUP ROW (SKIP)",
        ),
        _row(
            prop_id="000000222222",
            sup_num="000000000000",
            py_owner_name="CERT ROW (KEEP)",
        ),
    )
    batches = list(_stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=10))
    flat = [rec for b in batches for rec in b]
    assert len(flat) == 1
    assert flat[0][1] == "222222"
    out = capsys.readouterr().out
    assert "non_cert=1" in out


def test_stream_propyear_mismatch_warns(tmp_path: Path, capsys):
    """First-row prop_val_yr disagrees with folder year → warning, ingest
    still writes the row under folder year (not the row's prop_val_yr)."""
    txt = tmp_path / "x.TXT"
    _write_file(
        txt,
        _row(prop_id="000000111625", prop_val_yr="2020 ", py_owner_name="OWNER"),
    )
    batches = list(
        _stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=10)
    )
    out = capsys.readouterr().out
    flat = [rec for b in batches for rec in b]
    assert "WARNING" in out
    assert "prop_val_yr=2020" in out
    assert "year=2022" in out
    assert flat[0][2] == 2022  # folder year wins, not row's prop_val_yr


def test_stream_dedup_bounded_batches_at_batch_size(tmp_path: Path):
    """Streaming yields in chunks of `batch_size` (the last batch may be
    smaller). Lock this in so a refactor doesn't accidentally yield the
    full dedup dict as one batch."""
    txt = tmp_path / "x.TXT"
    # Start at 1 so the first prop_id isn't all zeros (which lstrip's to
    # empty and gets skipped — that's a different test's territory).
    lines = [
        _row(prop_id=f"00000000{i:04d}", py_owner_name=f"OWNER {i}")
        for i in range(1, 8)
    ]
    _write_file(txt, *lines)
    batches = list(_stream_dedup_records(str(txt), 2022, "x.TXT", batch_size=3))
    # 7 unique parcels in batches of 3 → sizes [3, 3, 1]
    assert [len(b) for b in batches] == [3, 3, 1]


# ---------------------------------------------------------------------------
# _check_propyear_matches_folder (silent paths)
# ---------------------------------------------------------------------------

def test_check_propyear_silent_on_short_or_unparseable_or_matching():
    """Silent when row is too short, prop_val_yr is blank/non-numeric, or
    matches the folder year. Only warns on numeric mismatch."""
    # Short row (< 22 chars) — silent.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _check_propyear_matches_folder("short", 2022, "f.TXT")
    assert buf.getvalue() == ""

    # Blank prop_val_yr — silent.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _check_propyear_matches_folder(_row(prop_val_yr="     "), 2022, "f.TXT")
    assert buf.getvalue() == ""

    # Non-numeric prop_val_yr — silent.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _check_propyear_matches_folder(_row(prop_val_yr="ABCDE"), 2022, "f.TXT")
    assert buf.getvalue() == ""

    # Matching prop_val_yr — silent.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _check_propyear_matches_folder(_row(prop_val_yr="2022 "), 2022, "f.TXT")
    assert buf.getvalue() == ""
