"""Inspection-based tests for the TAD property_zip ZCTA hotfix (spec v3).

Catches regressions on:
  * api/counties/tad.py — owner_zip is NOT the source of property_zip
  * tad.py SELECT pulls property_zip column
  * api/main.py:_fetch_tad_parcel_by_account SELECT pulls property_zip
  * _google_maps_link has NO owner_* fallbacks (3 separate fallbacks removed)
  * All 4 county normalizers truncate property_zip via [:5]
  * Backfill script has correct per-county PK mapping (DCAD differs)

These tests parse source files as text + AST and assert structure. No DB.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_tad_normalizer_does_not_use_owner_zip_as_property_zip() -> None:
    """The original bug — 'property_zip': owner_zip — must not return."""
    src = _read("api/counties/tad.py")
    # Search the _normalize_tad_row return-dict region. Anchor on the
    # property_zip key inside the return literal.
    assert '"property_zip": owner_zip' not in src, (
        "Regression: tad.py is using owner_zip as property_zip again. "
        "Must read raw.get('property_zip') with [:5]."
    )


def test_tad_normalizer_reads_property_zip_from_raw_row() -> None:
    """tad.py must read property_zip from the SQL row, not owner_zip."""
    src = _read("api/counties/tad.py")
    assert 'property_zip = _clean_text(raw.get("property_zip"))[:5]' in src, (
        "tad.py _normalize_tad_row must populate property_zip via "
        '_clean_text(raw.get("property_zip"))[:5]'
    )


def test_tad_bbox_select_includes_property_zip() -> None:
    """_tad_bbox_filter SELECT must include property_zip column."""
    src = _read("api/counties/tad.py")
    # Find the bbox SELECT body
    m = re.search(
        r"_tad_bbox_filter.*?SELECT(.*?)FROM tad_parcels",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate _tad_bbox_filter SELECT block"
    select_body = m.group(1)
    assert "property_zip" in select_body, (
        "_tad_bbox_filter SELECT must include property_zip column"
    )


def test_fetch_tad_parcel_by_account_select_includes_property_zip() -> None:
    """_fetch_tad_parcel_by_account SELECT must include property_zip column."""
    src = _read("api/main.py")
    m = re.search(
        r"def _fetch_tad_parcel_by_account.*?SELECT(.*?)FROM tad_parcels",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate _fetch_tad_parcel_by_account SELECT block"
    select_body = m.group(1)
    assert "property_zip" in select_body, (
        "_fetch_tad_parcel_by_account SELECT must include property_zip — "
        "otherwise direct-account lookups read None while bbox lookups read real value"
    )


def test_google_maps_link_has_no_owner_fallbacks() -> None:
    """_google_maps_link must NOT fall back to owner_* for city/state/zip."""
    src = _read("api/main.py")
    m = re.search(
        r"def _google_maps_link\(.*?\n(.*?)return \"https://maps\.google\.com\"\n",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate _google_maps_link body"
    body = m.group(1)
    # Each of these 3 strings was a fallback we removed.
    assert "owner_city" not in body, (
        "_google_maps_link still falls back to owner_city — must be removed"
    )
    assert "owner_state" not in body, (
        "_google_maps_link still falls back to owner_state — must be removed"
    )
    assert "owner_zip" not in body, (
        "_google_maps_link still falls back to owner_zip — must be removed"
    )


def test_all_four_county_normalizers_truncate_property_zip_to_5() -> None:
    """All 4 county normalizers must emit property_zip via [:5] truncation."""
    # tad.py is handled by a different test (the bug-specific one). Here we
    # check the other 3 counties + tad as a uniform display contract.
    cases = [
        ("api/counties/tad.py",    '_clean_text(raw.get("property_zip"))[:5]'),
        ("api/counties/collin.py", '_clean_text(raw.get("property_zip"))[:5]'),
        ("api/counties/denton.py", '_clean_text(raw.get("property_zip"))[:5]'),
        ("api/counties/dcad.py",   '_clean_text(row.get("property_zip"))[:5]'),
    ]
    for path, snippet in cases:
        src = _read(path)
        assert snippet in src, (
            f"{path} must emit property_zip with [:5] truncation. "
            f"Expected substring: {snippet}"
        )
    # DCAD has two normalizer return sites; both must truncate.
    dcad_src = _read("api/counties/dcad.py")
    occurrences = dcad_src.count('_clean_text(row.get("property_zip"))[:5]')
    assert occurrences >= 2, (
        f"dcad.py must truncate property_zip at both return sites (655 + 993). "
        f"Got {occurrences} occurrences."
    )


def test_backfill_script_has_correct_per_county_pk() -> None:
    """backfill_property_zip_from_zcta.py must use account_num for DCAD, parcel_key for others."""
    src = _read("scripts/backfill_property_zip_from_zcta.py")
    # Parse the COUNTY_TABLES dict literal.
    tree = ast.parse(src)
    found: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        # Handle both `COUNTY_TABLES = {...}` (Assign) and
        # `COUNTY_TABLES: dict[...] = {...}` (AnnAssign).
        target_name = None
        value_node = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value_node = node.value
        if target_name != "COUNTY_TABLES":
            continue
        if not isinstance(value_node, ast.Dict):
            continue
        for k, v in zip(value_node.keys, value_node.values):
            if isinstance(k, ast.Constant) and isinstance(v, ast.Tuple) and len(v.elts) == 2:
                table = v.elts[0].value if isinstance(v.elts[0], ast.Constant) else None
                pk = v.elts[1].value if isinstance(v.elts[1], ast.Constant) else None
                if table and pk:
                    found[k.value] = (table, pk)

    assert found.get("tad")    == ("tad_parcels",    "parcel_key")
    assert found.get("collin") == ("collin_parcels", "parcel_key")
    assert found.get("denton") == ("denton_parcels", "parcel_key")
    assert found.get("dcad")   == ("parcels",        "account_num"), (
        "DCAD's PK is account_num, not parcel_key. Batching against parcel_key "
        "would seq-scan the parcels table."
    )


def test_backfill_uses_st_covers_not_st_contains() -> None:
    """Spatial join must use ST_Covers (boundary-inclusive), not ST_Contains."""
    src = _read("scripts/backfill_property_zip_from_zcta.py")
    assert "ST_Covers(z.geom, p.centroid)" in src
    assert "ST_Contains" not in src, (
        "Spatial join must use ST_Covers (boundary-inclusive). ST_Contains "
        "is boundary-exclusive and was rejected in spec v3."
    )


def test_ingest_zcta_creates_audit_ledger() -> None:
    """ingest_zcta_polygons.py must create backfill_audit_rows ledger.

    Required for run-id-scoped rollback (NOT updated_at-scoped, which would
    clobber unrelated process updates in the same window).
    """
    src = _read("scripts/ingest_zcta_polygons.py")
    assert "backfill_audit_rows" in src
    assert "run_id" in src
    # Concurrent index requirement (non-blocking on shared Cloud SQL).
    assert "CREATE INDEX CONCURRENTLY" in src


def test_ingest_zcta_uses_2025_tiger_vintage() -> None:
    """Confirm TIGER 2025 (latest vintage), not 2024 from earlier spec draft."""
    src = _read("scripts/ingest_zcta_polygons.py")
    assert "TIGER2025" in src
    assert "tl_2025_us_zcta520" in src
