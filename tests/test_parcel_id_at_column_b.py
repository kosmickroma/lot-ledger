"""Mike request 2026-06-03 PM: Parcel ID moved from the right-edge outreach
block to column B (position 2 in the CSV header + every writerow), between
Row Type (A) and County Source (now C).

This test pins:
  - The header literal has "Parcel ID" immediately after "Row Type"
  - The parcel writerow has _outreach_parcel_id_cell(row) at index 1
    (Python 0-indexed → CSV column 2 / spreadsheet column B)
  - The orphan/comp writerow has _outreach_parcel_id_cell(_cad) at index 1
  - "Parcel ID" is NOT in the right-edge outreach block any more
  - _outreach_csv_cells star-expansion is 2 cells (was 3)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "api" / "main.py"


def _read() -> str:
    return MAIN_PY.read_text()


def test_parcel_id_appears_at_position_2_in_header() -> None:
    """The header literal should be: Row Type, Parcel ID, County Source, ..."""
    src = _read()
    # Find "Row Type" anchor + the next two string literals after it.
    # Accept any whitespace / comments between the literals.
    m = re.search(
        r'"Row Type",\s*(?:#[^\n]*\n\s*)?'
        r'"(?P<col2>[^"]+)",\s*(?:#[^\n]*\n\s*)?'
        r'"(?P<col3>[^"]+)"',
        src,
    )
    assert m, "couldn't locate Row Type + next two header cells"
    col2 = m.group("col2")
    col3 = m.group("col3")
    assert col2 == "Parcel ID", (
        f'Column B (header position 2) must be "Parcel ID". Got: {col2!r}'
    )
    assert col3 == "County Source", (
        f'Column C (header position 3) must be "County Source". Got: {col3!r}'
    )


def test_parcel_writerow_emits_parcel_id_at_index_1() -> None:
    """The parcel writerow literal must call _outreach_parcel_id_cell(row)
    immediately after the "Parcel" row-type marker (index 0)."""
    src = _read()
    # Find the parcel writerow — starts with "Parcel" literal.
    m = re.search(
        r'writer\.writerow\(\s*\[\s*"Parcel",\s*([_a-zA-Z][_a-zA-Z0-9]*\(row\))',
        src,
    )
    assert m, "couldn't locate parcel writerow literal start"
    second_cell_call = m.group(1)
    assert "_outreach_parcel_id_cell(row)" == second_cell_call, (
        f"Parcel writerow's 2nd cell (column B) must be _outreach_parcel_id_cell(row). "
        f"Found: {second_cell_call!r}"
    )


def test_orphan_writerow_emits_parcel_id_at_index_1() -> None:
    """The orphan/comp writerow must call _outreach_parcel_id_cell(_cad)
    immediately after the "Comp" row-type marker."""
    src = _read()
    # "Comp" → optional `#...` inline comment → `_outreach_parcel_id_cell(_cad)`
    # next line.
    m = re.search(
        r'"Comp",[^\n]*\n\s*_outreach_parcel_id_cell\(_cad\)',
        src,
    )
    assert m, (
        "couldn't find _outreach_parcel_id_cell(_cad) immediately after "
        "the 'Comp' marker in the orphan writerow."
    )


def test_parcel_id_not_in_right_edge_block_any_more() -> None:
    """The right-edge outreach block should no longer contain "Parcel ID"
    (it moved to column B)."""
    src = _read()
    # Walk header. After the v3 comment block, only 2 cells: Contact Info + Last Mailer Sent.
    m = re.search(
        r'# v3.*?Parcel ID moved out.*?(\n\s*"[^"]+",?){1,5}',
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate v3 right-edge outreach block in header"
    block = m.group(0)
    assert '"Parcel ID"' not in block, (
        "v3 right-edge outreach header block must NOT contain 'Parcel ID'. "
        "Parcel ID lives at column B now (position 2), not the right edge."
    )


def test_outreach_csv_cells_returns_two_tuple() -> None:
    """_outreach_csv_cells should return a 2-tuple (Contact Info, Last
    Mailer Sent). v1 returned 4-tuple, v2 returned 3-tuple, v3 returns 2."""
    src = _read()
    # Find the function signature + return type annotation.
    m = re.search(
        r"def _outreach_csv_cells\(row[^)]+\)\s*->\s*tuple\[([^\]]+)\]",
        src,
    )
    assert m, "couldn't locate _outreach_csv_cells return type annotation"
    return_type = m.group(1)
    type_args = [t.strip() for t in return_type.split(",")]
    assert len(type_args) == 2, (
        f"_outreach_csv_cells must return tuple[str, str] (2-tuple). "
        f"Got {len(type_args)}-tuple. Type args: {type_args}"
    )


def test_outreach_parcel_id_cell_helper_exists() -> None:
    """The new helper for emitting Parcel ID at column B."""
    src = _read()
    assert re.search(r"def _outreach_parcel_id_cell\(row[^)]+\)", src), (
        "_outreach_parcel_id_cell helper missing — required for Parcel ID "
        "at column B (Mike request 2026-06-03 PM)"
    )
