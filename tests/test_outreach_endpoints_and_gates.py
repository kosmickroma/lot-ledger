"""Inspection-based regression guards for the Mailer + Phone Tracking feature.

Mirrors the existing AST/string-based test pattern (see
test_csv_export_dcad_owner_fields.py, test_tad_property_zip_zcta.py).
No DB, no network, no test client.

Guards against:
  - Outreach endpoints accidentally removed or unregistered
  - Role gate widening (e.g. someone hand-removes the _user_can_see_outreach
    call before a return)
  - Schema migration losing the pgcrypto extension (which the data DB
    doesn't install by default — verified 2026-06-03)
  - Per-county PK mapping breaking (DCAD uses account_num; others parcel_key)
  - Frontend role gate using _canDownloadCsv() instead of _isPowerUserOrAbove()
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "api" / "main.py"
MIGRATION_PY = ROOT / "scripts" / "setup_outreach_schema.py"
MAP_JS = ROOT / "frontend" / "map.js"
INDEX_HTML = ROOT / "frontend" / "index.html"


def _read(path: Path) -> str:
    return path.read_text()


def test_outreach_endpoints_registered() -> None:
    src = _read(MAIN_PY)
    assert "@app.put(\"/api/parcels/outreach\")" in src, (
        "PUT /api/parcels/outreach endpoint missing"
    )
    assert "@app.post(\"/api/parcels/outreach/import\")" in src, (
        "POST /api/parcels/outreach/import endpoint missing"
    )
    assert "@app.get(\"/api/parcels/outreach/imports\")" in src, (
        "GET /api/parcels/outreach/imports endpoint missing"
    )


def test_outreach_role_gate_is_stricter_than_csv_gate() -> None:
    """The outreach gate must NOT include 'member' or 'user' — only
    power_user / owner / developer. The broader CSV gate at main.py:4513
    intentionally lets 'member' through; that's WRONG for outreach.
    """
    src = _read(MAIN_PY)
    # The set literal must be exactly {developer, owner, power_user}.
    m = re.search(
        r"_OUTREACH_ALLOWED_ROLES\s*=\s*\{([^}]+)\}",
        src,
    )
    assert m, "_OUTREACH_ALLOWED_ROLES set literal not found"
    roles_text = m.group(1)
    parsed = {tok.strip().strip("\"'") for tok in roles_text.split(",") if tok.strip()}
    expected = {"developer", "owner", "power_user"}
    assert parsed == expected, (
        f"_OUTREACH_ALLOWED_ROLES must be exactly {expected}; got {parsed}. "
        f"Including 'user' or 'member' would leak PII outreach data."
    )


def test_outreach_strip_called_at_all_feature_response_sites() -> None:
    """Every endpoint that returns parcel features should call the strip
    helper before serialization (defense-in-depth, even though the frontend
    also hides the section)."""
    src = _read(MAIN_PY)
    # The strip helper should be referenced from at least 3 distinct call
    # sites (one per main return path). Today: /api/parcel/near (×4 per-county
    # branches), /api/parcel/{county}/{account_num}, /api/analyze,
    # _build_features_from_rows.
    strip_call_count = src.count("_strip_outreach_from_feature(")
    assert strip_call_count >= 4, (
        f"Expected at least 4 _strip_outreach_from_feature call sites "
        f"(per-county branches in /api/parcel/near + /api/parcel/{{...}}). "
        f"Found {strip_call_count}."
    )
    list_strip_count = src.count("_strip_outreach_from_features(")
    # The list variant counts every reference: the def itself + at least one
    # invocation. Two refs = 1 invocation; we want 1+ invocations.
    assert list_strip_count >= 3, (
        f"Expected at least 3 references to _strip_outreach_from_features "
        f"(def + invocations in _build_features_from_rows and /api/analyze). "
        f"Found {list_strip_count}."
    )


def test_per_county_pk_mapping_is_correct() -> None:
    """DCAD uses account_num, the others use parcel_key. Verified globally
    unique via cross-county collision query 2026-06-03."""
    src = _read(MAIN_PY)
    m = re.search(
        r"_OUTREACH_PARCEL_TABLES\s*=\s*\{([^}]+)\}",
        src,
        re.DOTALL,
    )
    assert m, "_OUTREACH_PARCEL_TABLES dict literal not found"
    body = m.group(1)
    assert '"dcad":   ("parcels",         "account_num")' in body, (
        "DCAD must map to (parcels, account_num)"
    )
    for county in ("tad", "collin", "denton"):
        assert f'"{county}":' in body and "parcel_key" in body, (
            f"{county} must use parcel_key as the PK"
        )


def test_migration_includes_pgcrypto_extension() -> None:
    """gen_random_uuid() requires pgcrypto, and the data DB does NOT install
    it by default (verified 2026-06-03 — only postgis is present). Without
    the explicit CREATE EXTENSION, the migration would fail."""
    src = _read(MIGRATION_PY)
    assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in src, (
        "scripts/setup_outreach_schema.py must include "
        "CREATE EXTENSION IF NOT EXISTS pgcrypto — gen_random_uuid() "
        "requires it on the data DB."
    )


def test_migration_creates_all_three_tables() -> None:
    src = _read(MIGRATION_PY)
    for table in ("parcel_outreach_notes", "outreach_import_log", "csv_export_log"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in src, (
            f"Migration script missing CREATE TABLE for {table}"
        )


def test_frontend_outreach_gate_is_stricter_than_csv_gate() -> None:
    """Frontend Outreach section render + Import button visibility must
    check _isPowerUserOrAbove() — NOT _canDownloadCsv() (which lets member
    through)."""
    src = _read(MAP_JS)
    # Outreach popup section guard.
    m = re.search(
        r"function _buildPanelOutreachHtml\(p\) \{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert m, "_buildPanelOutreachHtml function not found"
    body = m.group(1)
    assert "_isPowerUserOrAbove()" in body, (
        "_buildPanelOutreachHtml must guard on _isPowerUserOrAbove()"
    )
    assert "_canDownloadCsv()" not in body, (
        "_buildPanelOutreachHtml must NOT use _canDownloadCsv() — that's looser"
    )


def test_frontend_import_button_in_index_html() -> None:
    src = _read(INDEX_HTML)
    assert 'id="btn-import-outreach"' in src, (
        "Import from CRM button missing from sidebar (index.html)"
    )
    assert 'id="outreach-import-file"' in src, (
        "Hidden file input for outreach upload missing from index.html"
    )


def test_csv_outreach_columns_present_in_header() -> None:
    src = _read(MAIN_PY)
    # The 4 new column headers must appear in the CSV header literal.
    for col in ("\"Parcel ID\"", "\"Phone Number\"", "\"Mailer Sent\"", "\"Mailer Date\""):
        assert col in src, f"CSV header missing column literal: {col}"
