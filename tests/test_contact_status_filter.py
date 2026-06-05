"""tests/test_contact_status_filter.py
Role: Inspection-based regression guards for the Contact Status filter.
Connects to: frontend/index.html, frontend/map.js, frontend/style.css, api/main.py
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "api" / "main.py"
MAP_JS = ROOT / "frontend" / "map.js"
INDEX_HTML = ROOT / "frontend" / "index.html"


def _read(path: Path) -> str:
    return path.read_text()


def test_default_filter_and_input_id_wired() -> None:
    src = _read(MAP_JS)
    assert re.search(r"contact_status:\s*false", src), (
        "DEFAULT_FILTERS.contact_status must exist and default to false"
    )
    assert 'contact_status: "filter-contact-status"' in src, (
        "FILTER_INPUT_IDS.contact_status must target filter-contact-status"
    )


def test_overlay_helpers_exist() -> None:
    src = _read(MAP_JS)
    assert "function _maybeAddOutreachOverlay(parcel, feature)" in src, (
        "_maybeAddOutreachOverlay helper missing from frontend/map.js"
    )
    assert "function _rebuildOutreachOverlays()" in src, (
        "_rebuildOutreachOverlays helper missing from frontend/map.js"
    )
    assert "outreachOverlayGeomSeen" in src, (
        "outreachOverlayGeomSeen Set must exist for condo/shared-geometry dedup"
    )


def test_render_features_clears_and_rebuilds_outreach_layer() -> None:
    src = _read(MAP_JS)
    assert "outreachOverlayLayer.clearLayers();" in src, (
        "renderFeatures must clear outreachOverlayLayer at the top of each pass"
    )
    assert "outreachOverlayLayerByKey.clear();" in src, (
        "renderFeatures must clear outreachOverlayLayerByKey at the top of each pass"
    )
    assert "outreachOverlayGeomSeen.clear();" in src, (
        "renderFeatures must clear outreachOverlayGeomSeen each render/rebuild"
    )
    assert re.search(
        r"geojson\.features\.forEach\(\(feature\) => \{\s*const p = feature\?\.properties;\s*if \(!p\) return;\s*_maybeAddOutreachOverlay\(p, feature\);",
        src,
        re.DOTALL,
    ), "renderFeatures must rebuild outreach overlays in the post-loop pass"


def test_generic_checkbox_listener_rebuilds_contact_status_overlay() -> None:
    src = _read(MAP_JS)
    assert re.search(
        r"Object\.entries\(FILTER_INPUT_IDS\)\.forEach\(\(\[key, id\]\) => \{.*?_filterSaveQueueSave\(\);\s*if \(key === \"contact_status\"\) \{\s*_rebuildOutreachOverlays\(\);\s*\}",
        src,
        re.DOTALL,
    ), "Generic checkbox listener must branch on contact_status and rebuild overlays"
    assert re.search(
        r"document\.getElementById\(\"btn-filters-reset\"\)\?\.addEventListener\("
        r"change|click",
        src,
    ) or "document.getElementById(\"btn-filters-reset\")?.addEventListener(\"click\", () => {" in src
    assert "_rebuildOutreachOverlays();" in src, (
        "Filter reset path must rebuild outreach overlays"
    )


def test_sse_and_outreach_mutation_paths_rebuild_overlays() -> None:
    src = _read(MAP_JS)
    assert re.search(
        r"function _writeFilterFieldDirect\(fieldKey, value\) \{.*?applyMapVisibilityFilters\(\); } catch \(_\) \{\}\s*try \{ _rebuildOutreachOverlays\(\); } catch \(_\) \{\}",
        src,
        re.DOTALL,
    ), "SSE apply path must rebuild outreach overlays after visibility refresh"
    assert re.search(
        r"async function _putOutreachField\(county, parcelId, field, value\) \{.*?_updateLocalFeatureOutreach\(county, parcelId, field, value\);\s*_rebuildOutreachOverlays\(\);",
        src,
        re.DOTALL,
    ), "_putOutreachField must rebuild overlays after updating cached outreach data"
    assert re.search(
        r"applyMapVisibilityFilters\(\);\s*\}\s*catch \(_\) \{\}\s*try \{\s*_rebuildOutreachOverlays\(\);\s*\} catch \(_\) \{\}",
        src,
        re.DOTALL,
    ), "CSV import commit success path must rebuild outreach overlays"


def test_index_html_contains_power_user_contact_status_row_and_divider() -> None:
    src = _read(INDEX_HTML)
    assert '<label class="filter-row power-user-only">' in src, (
        "Contact Status row must be power-user-only"
    )
    assert 'id="filter-contact-status"' in src, (
        "Contact Status checkbox input missing from frontend/index.html"
    )
    assert '<hr class="filter-section-divider power-user-only">' in src, (
        "Contact Status divider must be power-user-only"
    )


def test_backend_allowlist_includes_contact_status() -> None:
    src = _read(MAIN_PY)
    assert '"checkboxes.contact_status"' in src, (
        "api/main.py _FILTER_FIELD_KEYS must allowlist checkboxes.contact_status"
    )