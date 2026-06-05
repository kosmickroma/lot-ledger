"""Regression guards for the 2026-06-05 hotfix bundle:

  Bug 1: CSV re-import did NOT show in the popup (date input blank,
    checkbox stayed off). Root cause: import response only returned
    summary counts; frontend had no way to update lastAnalysisGeojson
    in place, so popup re-renders read stale data from the cached
    feature properties.

  Bug 2: button label "Import from CRM" was confusing — renamed to
    just "Import".

  Bug 3: Parcel ID is now a visible row at the bottom of the CAD
    detail table for easy reference.

These tests read source files as text and assert structure.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "api" / "main.py"
MAP_JS = ROOT / "frontend" / "map.js"
INDEX_HTML = ROOT / "frontend" / "index.html"


def _read(p: Path) -> str:
    return p.read_text()


def test_import_endpoint_returns_committed_rows() -> None:
    """POST /api/parcels/outreach/import commit response includes the list of
    rows that were UPSERTed, so the frontend can refresh local state."""
    src = _read(MAIN_PY)
    # Response dict must include committed_rows
    assert '"committed_rows": committed_rows' in src, (
        "Import endpoint response missing 'committed_rows' field — "
        "frontend can't refresh feature props without it"
    )
    # The chunked UPSERT must RETURN the four fields needed.
    assert "RETURNING county, parcel_id, contact_info_retrieved, mailer_date" in src, (
        "UPSERT must RETURN all 4 fields so we can build committed_rows"
    )


def test_committed_rows_includes_outreach_keys() -> None:
    """Each row in committed_rows must carry the same key names the
    frontend reads: outreach_contact_info_retrieved + outreach_mailer_date."""
    src = _read(MAIN_PY)
    assert '"outreach_contact_info_retrieved": bool(cir)' in src
    assert '"outreach_mailer_date":' in src
    assert '"county": cnty' in src
    assert '"parcel_id": pid' in src


def test_frontend_import_handler_updates_local_features() -> None:
    """After a successful commit, the frontend iterates committed_rows
    and calls _updateLocalFeatureOutreach for each field. Without this,
    popup re-opens show stale data."""
    src = _read(MAP_JS)
    # The handler must reference commit.committed_rows
    assert "commit.committed_rows" in src
    # And it must call _updateLocalFeatureOutreach for each field
    # (contact_info_retrieved + mailer_date)
    assert '_updateLocalFeatureOutreach(cnty, pid, "contact_info_retrieved"' in src
    assert '_updateLocalFeatureOutreach(cnty, pid, "mailer_date"' in src


def test_button_renamed_to_import() -> None:
    """The sidebar button text is now just 'Import', not 'Import from CRM'."""
    html = _read(INDEX_HTML)
    # Find the button line with id="btn-import-outreach"
    m = re.search(r'<button[^>]*id="btn-import-outreach"[^>]*>([^<]+)</button>', html)
    assert m, "btn-import-outreach button not found in index.html"
    label = m.group(1).strip()
    assert label == "Import", (
        f'Button label must be "Import" (not "{label}"). KK request 2026-06-05.'
    )


def test_button_old_label_not_present() -> None:
    """The old 'Import from CRM' label should be gone everywhere it
    used to appear (button text + originalLabel fallback in map.js)."""
    html = _read(INDEX_HTML)
    assert "Import from CRM" not in html, (
        '"Import from CRM" label should be removed from index.html'
    )
    js = _read(MAP_JS)
    # The originalLabel fallback should also be just "Import"
    assert 'btn?.textContent || "Import from CRM"' not in js, (
        'map.js still has the old "Import from CRM" fallback string'
    )


def test_parcel_id_row_in_popup_cad_detail_section() -> None:
    """The CAD detail table includes a Parcel ID row near the bottom
    (KK request 2026-06-05: 'easy reference')."""
    src = _read(MAP_JS)
    # Look for the new Parcel ID row's label string in the popup-builder context
    assert '_buildParcelDetailTableRow("Parcel ID"' in src, (
        "Popup CAD detail table must include a 'Parcel ID' row"
    )
    # The Parcel ID picker must respect per-county PK (DCAD account_num,
    # others parcel_key) — same logic as _outreach_parcel_id_cell.
    m = re.search(
        r'_buildParcelDetailTableRow\("Parcel ID".{0,300}',
        src,
        re.DOTALL,
    )
    # And the surrounding context must reference dcad branch
    context_m = re.search(
        r'(\(\(\) => \{[^}]+_cnty === "dcad"[^}]+account_num[^}]+parcel_key[^}]+\}\)\(\))',
        src,
        re.DOTALL,
    )
    assert context_m, (
        "Parcel ID row must use _cnty==='dcad' ? account_num : parcel_key logic"
    )
