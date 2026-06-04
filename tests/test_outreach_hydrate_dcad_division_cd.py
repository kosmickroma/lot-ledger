"""Regression guard for the 2026-06-03 PM CSV-blank bug.

KK reported that toggling "Contact Info Retrieved" in the popup did NOT
make it to the downloaded CSV (cell stayed blank), even though the DB
had contact_info_retrieved=true for the parcel.

Root cause: _row_outreach_key did a raw `(source_county or division_cd).lower()`
and checked the result against {dcad,tad,collin,denton}. Cached_jobs rows
cache DCAD's raw division_cd as "RES" (Residential) or "COM" (Commercial)
— NOT "DCAD". Raw lowercase of "RES" → "res" → not in the set → returns
None → hydrate skips → CSV cell blank.

Fresh /api/analyze responses worked because the bbox SELECT result already
has source_county='dcad'. The bug only manifested for cached job reads
(CSV exports, session restore, share-link opens).

Fix: route through _csv_county_source which already knows the
RES/COM → DCAD mapping (see _CSV_COUNTY_SOURCE_MAP), then lowercase.

These tests load api/main.py as a module + simulate the helper directly.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_api_main():
    """Load api/main.py as a module without running app startup."""
    spec = importlib.util.spec_from_file_location("api_main_for_tests", ROOT / "api" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    # Avoid side-effects by NOT actually executing the FastAPI app routes —
    # but we DO need the module-level constants + helpers. Just import it;
    # app instantiation happens at import time but the tests don't hit it.
    sys.modules.setdefault("api_main_for_tests", mod)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        # Startup-time DB connection may fail in CI; that's fine — we just
        # need the helpers, and they get defined before any connection use.
        if "DB_" not in str(e) and "database" not in str(e).lower():
            raise
    return mod


def test_dcad_row_with_division_cd_res_resolves_to_dcad() -> None:
    """The exact cached-row shape that broke the CSV in production."""
    mod = _load_api_main()
    row = {
        "account_num": "00000733108000000",
        "parcel_key": "00000733108000000",
        "source_county": "",          # ← cached job has this empty
        "division_cd": "RES",          # ← DCAD-RES, not "DCAD"
    }
    key = mod._row_outreach_key(row)
    assert key is not None, (
        "DCAD row with division_cd='RES' must resolve to a valid outreach "
        "key. Got None → hydrate would skip → CSV cells would be blank."
    )
    county, parcel_id = key
    assert county == "dcad", f"Expected county='dcad', got {county!r}"
    assert parcel_id == "00000733108000000", (
        f"Expected parcel_id='00000733108000000', got {parcel_id!r}"
    )


def test_dcad_row_with_division_cd_com_resolves_to_dcad() -> None:
    """Same for the COM (Commercial) division_cd variant."""
    mod = _load_api_main()
    row = {
        "account_num": "00000111222333000",
        "source_county": "",
        "division_cd": "COM",
    }
    key = mod._row_outreach_key(row)
    assert key is not None
    county, _ = key
    assert county == "dcad"


def test_tad_row_with_source_county_lowercase_resolves() -> None:
    """TAD rows have source_county='tad' populated. Should still work
    (the fix shouldn't regress the simple case)."""
    mod = _load_api_main()
    row = {
        "parcel_key": "03482197:000",
        "source_county": "tad",
        "division_cd": "TAD",
    }
    key = mod._row_outreach_key(row)
    assert key is not None
    county, parcel_id = key
    assert county == "tad"
    assert parcel_id == "03482197:000"


def test_unknown_division_cd_returns_none() -> None:
    """A row with no recognizable county should return None — not panic."""
    mod = _load_api_main()
    row = {"account_num": "x", "division_cd": "????"}
    assert mod._row_outreach_key(row) is None
