"""tests/test_flood_zones_phase2.py
Role: Inspection + unit guards for Phase 2 — backend enrichment + endpoint.

Asserts:
  - GET /api/flood-zones registered
  - Bbox parser rejects malformed input with HTTPException(400)
  - Severity-first ORDER BY in endpoint SQL (spec decision #18)
  - LIMIT 5000 cap on response
  - Each county adapter has its _fetch_flood_lookup_<county> helper
  - Each adapter wires flood enrichment into row assembly
  - CSV header includes "Flood Zone" at right edge
  - _flood_zone_csv_cell formatter handles AE/X/FLOODWAY/BFE/empty cases

Connects to: api/main.py, api/counties/{dcad,tad,collin,denton}.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from api.main import _flood_zone_csv_cell, _parse_flood_bbox


ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "api" / "main.py"
DCAD_PY = ROOT / "api" / "counties" / "dcad.py"
TAD_PY = ROOT / "api" / "counties" / "tad.py"
COLLIN_PY = ROOT / "api" / "counties" / "collin.py"
DENTON_PY = ROOT / "api" / "counties" / "denton.py"


def _read(path: Path) -> str:
    return path.read_text()


# ---- Bbox parser ----------------------------------------------------------


def test_bbox_parser_accepts_valid_input() -> None:
    """Standard DFW-ish bbox must parse cleanly."""
    assert _parse_flood_bbox("-97.6,32.4,-96.4,33.4") == (-97.6, 32.4, -96.4, 33.4)


def test_bbox_parser_rejects_missing() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_flood_bbox(None)
    assert exc.value.status_code == 400


def test_bbox_parser_rejects_wrong_count() -> None:
    for bad in ("1,2,3", "1,2,3,4,5", ""):
        with pytest.raises(HTTPException) as exc:
            _parse_flood_bbox(bad)
        assert exc.value.status_code == 400


def test_bbox_parser_rejects_non_floats() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_flood_bbox("a,b,c,d")
    assert exc.value.status_code == 400


def test_bbox_parser_rejects_non_finite() -> None:
    for bad in ("nan,32.4,-96.4,33.4", "-97.6,32.4,inf,33.4"):
        with pytest.raises(HTTPException) as exc:
            _parse_flood_bbox(bad)
        assert exc.value.status_code == 400


def test_bbox_parser_rejects_out_of_range_lng() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_flood_bbox("-200,32.4,-96.4,33.4")
    assert exc.value.status_code == 400


def test_bbox_parser_rejects_out_of_range_lat() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_flood_bbox("-97.6,-91,-96.4,33.4")
    assert exc.value.status_code == 400


def test_bbox_parser_rejects_min_greater_or_equal_max() -> None:
    for bad in ("-96.4,32.4,-97.6,33.4", "-97.6,33.4,-96.4,32.4", "-97.6,32.4,-97.6,33.4"):
        with pytest.raises(HTTPException) as exc:
            _parse_flood_bbox(bad)
        assert exc.value.status_code == 400


# ---- Endpoint SQL shape ---------------------------------------------------


def test_endpoint_registered() -> None:
    src = _read(MAIN_PY)
    assert '@app.get("/api/flood-zones")' in src
    assert "async def flood_zones(bbox:" in src


def test_endpoint_severity_first_ordering() -> None:
    """Spec decision #18 — narrow floodways must stay visible at low zoom."""
    src = _read(MAIN_PY)
    pat = re.compile(
        r"WHEN zone_subty = 'FLOODWAY' THEN 5.*?"
        r"WHEN fld_zone IN \('AE','A','AH','AO','V','VE'\) THEN 4.*?"
        r"WHEN zone_subty = '0\.2 PCT ANNUAL CHANCE FLOOD HAZARD' THEN 3.*?"
        r"WHEN fld_zone = 'X' THEN 2",
        re.DOTALL,
    )
    assert pat.search(src), "Severity-first ORDER BY missing from /api/flood-zones SQL"


def test_endpoint_caps_at_5000() -> None:
    src = _read(MAIN_PY)
    pat = re.compile(r"LIMIT 5000", re.IGNORECASE)
    # Just ensure LIMIT 5000 appears in the flood_zones endpoint area
    assert pat.search(src), "LIMIT 5000 cap missing from /api/flood-zones SQL"


def test_endpoint_uses_gist_bbox_prefilter() -> None:
    """ST_MakeEnvelope + geom && operator is the GIST-friendly bbox filter."""
    src = _read(MAIN_PY)
    assert "geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)" in src


# ---- Per-county adapter wiring -------------------------------------------


def test_dcad_has_flood_lookup_helper_and_wires_it() -> None:
    src = _read(DCAD_PY)
    assert "def _fetch_flood_lookup_dcad(account_nums:" in src
    assert "_fetch_flood_lookup_dcad(" in src
    assert "f.source_county = 'dallas'" in src


def test_tad_has_flood_lookup_helper_and_wires_it() -> None:
    src = _read(TAD_PY)
    assert "def _fetch_flood_lookup_tad(parcel_keys:" in src
    assert "_fetch_flood_lookup_tad(" in src
    assert "f.source_county = 'tarrant'" in src


def test_collin_has_flood_lookup_helper_and_wires_it() -> None:
    src = _read(COLLIN_PY)
    assert "def _fetch_flood_lookup_collin(parcel_keys:" in src
    assert "_fetch_flood_lookup_collin(" in src
    assert "f.source_county = 'collin'" in src


def test_denton_has_flood_lookup_helper_and_wires_it() -> None:
    src = _read(DENTON_PY)
    assert "def _fetch_flood_lookup_denton(parcel_keys:" in src
    assert "_fetch_flood_lookup_denton(" in src
    assert "f.source_county = 'denton'" in src


def test_all_adapters_use_centroid_not_polygon() -> None:
    """KK 2026-06-06 decision (post-Phase-1 discovery that DCAD has no
    polygon column): centroid-based ST_Contains for ALL 4 counties so the
    SQL pattern stays uniform."""
    for path in (DCAD_PY, TAD_PY, COLLIN_PY, DENTON_PY):
        src = _read(path)
        assert "ST_Contains(f.geom, p.centroid)" in src, (
            f"{path.name} must use ST_Contains(f.geom, p.centroid) — "
            f"centroid-based join per locked decision."
        )


def test_all_adapters_inject_flood_fields_into_rows() -> None:
    """Each adapter must set row['flood_zone'], row['flood_zone_subtype'],
    and row['flood_bfe'] — defaulting to empty/None when no match."""
    for path in (DCAD_PY, TAD_PY, COLLIN_PY, DENTON_PY):
        src = _read(path)
        assert 'row["flood_zone"]' in src, f"{path.name} missing flood_zone wiring"
        assert 'row["flood_zone_subtype"]' in src, f"{path.name} missing flood_zone_subtype wiring"
        assert 'row["flood_bfe"]' in src, f"{path.name} missing flood_bfe wiring"


# ---- CSV column at right edge --------------------------------------------


def test_csv_header_includes_flood_zone_at_right_edge() -> None:
    """Header sequence: ...Contact Info Retrieved, Last Mailer Sent, Flood Zone."""
    src = _read(MAIN_PY)
    pat = re.compile(
        r'"Contact Info Retrieved",\s*"Last Mailer Sent",\s*"Flood Zone",',
        re.DOTALL,
    )
    assert pat.search(src), (
        "CSV header must end with the sequence ...Contact Info Retrieved, "
        "Last Mailer Sent, Flood Zone."
    )


def test_csv_row_builder_emits_flood_cell_after_outreach() -> None:
    """Both parcel-row and orphan-row builders must call _flood_zone_csv_cell
    right after the outreach cells."""
    src = _read(MAIN_PY)
    pat = re.compile(
        r"\*_outreach_csv_cells\([^)]+\),\s*_flood_zone_csv_cell\([^)]+\),",
    )
    matches = pat.findall(src)
    assert len(matches) >= 2, (
        f"Expected _flood_zone_csv_cell call in both parcel + orphan row "
        f"builders (2+ sites). Found {len(matches)}."
    )


# ---- CSV cell formatter --------------------------------------------------


def test_flood_csv_cell_empty_when_no_zone() -> None:
    assert _flood_zone_csv_cell(None) == ""
    assert _flood_zone_csv_cell({}) == ""
    assert _flood_zone_csv_cell({"flood_zone": ""}) == ""
    assert _flood_zone_csv_cell({"flood_zone": "   "}) == ""


def test_flood_csv_cell_ae_with_bfe() -> None:
    cell = _flood_zone_csv_cell({"flood_zone": "AE", "flood_bfe": 502.3, "flood_zone_subtype": ""})
    assert cell == "AE (BFE 502.3 ft)"


def test_flood_csv_cell_ae_without_bfe() -> None:
    cell = _flood_zone_csv_cell({"flood_zone": "AE", "flood_bfe": None, "flood_zone_subtype": ""})
    assert cell == "AE"


def test_flood_csv_cell_floodway_overrides_bfe_format() -> None:
    """FLOODWAY subtype gets a distinct, attention-grabbing label."""
    cell = _flood_zone_csv_cell({
        "flood_zone": "AE", "flood_bfe": 502.3, "flood_zone_subtype": "FLOODWAY",
    })
    assert cell == "AE — FLOODWAY (no build)"


def test_flood_csv_cell_x_shaded_500yr() -> None:
    cell = _flood_zone_csv_cell({
        "flood_zone": "X", "flood_bfe": None,
        "flood_zone_subtype": "0.2 PCT ANNUAL CHANCE FLOOD HAZARD",
    })
    assert cell == "X — 500-yr floodplain"


def test_flood_csv_cell_x_unshaded_minimal() -> None:
    cell = _flood_zone_csv_cell({
        "flood_zone": "X", "flood_bfe": None,
        "flood_zone_subtype": "AREA OF MINIMAL FLOOD HAZARD",
    })
    assert cell == "X — minimal risk"


def test_flood_csv_cell_x_bare_no_subtype() -> None:
    cell = _flood_zone_csv_cell({"flood_zone": "X", "flood_bfe": None, "flood_zone_subtype": ""})
    assert cell == "X"
