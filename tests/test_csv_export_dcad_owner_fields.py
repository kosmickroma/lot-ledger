"""Catch CSV header/writerow drift before deploy (spec v4a.2 §2.9).

This test reads api/main.py as source text and pulls out:
  * the header literal passed to the first writer.writerow(...)
  * the parcel writerow literal
  * the orphan-comp writerow literal

It counts cells (elements at top level of the list literal) and asserts
they all have the same count.

Approach: parse with `ast` to keep this resilient against formatting
changes (string-search alternatives are brittle).
"""
import ast
from pathlib import Path

import pytest

MAIN_PATH = Path(__file__).resolve().parent.parent / "api" / "main.py"


def _writerow_cell_counts() -> list[int]:
    """Return a list of element-counts for every writer.writerow([...]) call
    in the streaming CSV generator."""
    tree = ast.parse(MAIN_PATH.read_text())
    counts: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute) and func.attr == "writerow"
            and isinstance(func.value, ast.Name) and func.value.id == "writer"
        ):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if not isinstance(arg, ast.List):
            continue
        # Count elements; expanded `*foo` star-args contribute their declared
        # count if foo is a known fixed-shape helper, otherwise we skip — we
        # only want to compare integer counts. _additional_owner_cells returns
        # 4; stored_value_export_cells is constructed as [""] * 10 (10 cells).
        n = 0
        ok = True
        for elt in arg.elts:
            if isinstance(elt, ast.Starred):
                # Recognize known star-expansions by name.
                if isinstance(elt.value, ast.Name) and elt.value.id == "stored_value_export_cells":
                    n += 12  # K4 (2026-05-27): 10 original + 2 NBV cells
                elif isinstance(elt.value, ast.Call) and isinstance(elt.value.func, ast.Name) and elt.value.func.id == "_additional_owner_cells":
                    n += 4
                elif isinstance(elt.value, ast.Call) and isinstance(elt.value.func, ast.Name) and elt.value.func.id == "_outreach_csv_cells":
                    # Mailer + Phone Tracking v3 (2026-06-03 PM). 2-tuple:
                    # Contact Info Retrieved, Last Mailer Sent.
                    # (Parcel ID moved out to column B as a separate cell —
                    # _outreach_parcel_id_cell — per Mike's request.)
                    n += 2
                else:
                    ok = False
                    break
            else:
                n += 1
        if ok:
            counts.append(n)
    return counts


def test_writerow_cell_counts_match():
    counts = _writerow_cell_counts()
    # We expect at least 3 writerow sites: header, parcel, orphan-comp.
    assert len(counts) >= 3, (
        f"Expected at least 3 writer.writerow() literals; found {len(counts)}"
    )
    # All three should agree.
    assert len(set(counts)) == 1, (
        f"writerow cell counts disagree: {counts}. "
        f"Header/parcel/orphan must share the same column count "
        f"(spec v4a.2 §2.9)."
    )


def test_total_columns_is_at_least_191():
    # 151 baseline + 31 v4a + 6 ownership-history + 3 outreach v2 right-edge
    # (Parcel ID, Contact Info Retrieved, Last Mailer Sent). v2 dropped
    # Phone Number + Mailer Sent boolean (was 4, now 3).
    counts = _writerow_cell_counts()
    assert counts and counts[0] >= 191, (
        f"Expected at least 191 cells (151 baseline + 31 v4a + 6 ownership + 3 outreach v2). "
        f"Got {counts[0] if counts else 'no rows'}."
    )
