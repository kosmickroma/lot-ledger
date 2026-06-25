"""Allowlist drift regression — every key emitted by captureFilterState()
in frontend/map.js must be present in _FILTER_FIELD_KEYS on the backend.

If a future feature adds a new filter field to the frontend UI but
forgets the allowlist, the PATCH endpoint will 400 on that field and
production users will see autosave errors. This test catches that.

Per docs/MULTIUSER_COLLAB_SPRINT2_SPEC.md v2 §2.1.A + §7.1.
"""
import re
from pathlib import Path

from api import main as api_main


_EXPECTED_KEYS = frozenset({
    # checkboxes (9)
    "checkboxes.active", "checkboxes.sold", "checkboxes.off_market",
    "checkboxes.vacant", "checkboxes.multifamily", "checkboxes.duplexes",
    "checkboxes.commercial", "checkboxes.exempt",
    "checkboxes.contact_status",
    # numeric Property Filters (8)
    "numeric.lot_sqft_min", "numeric.lot_sqft_max",
    "numeric.appr_val_min", "numeric.appr_val_max",
    "numeric.yr_built_min", "numeric.yr_built_max",
    "numeric.sqft_min",     "numeric.sqft_max",
    # sold (5)
    "sold.maxDaysAgo",  "sold.minPrice", "sold.maxPrice",
    "sold.minYearBuilt", "sold.maxYearBuilt",
    # comp Comp Filters (8)
    "comp.lot_sqft_min", "comp.lot_sqft_max",
    "comp.appr_val_min", "comp.appr_val_max",
    "comp.yr_built_min", "comp.yr_built_max",
    "comp.sqft_min",     "comp.sqft_max",
    # propelio (23) — includes the 6 parcelType* mirrors (Sprint 2 smoke catch).
    "propelio.months",  "propelio.range",
    "propelio.statusSold", "propelio.statusActive", "propelio.statusPending",
    "propelio.showOutsideArea", "propelio.soldWithinDays",
    "propelio.lotMin",  "propelio.lotMax",
    "propelio.sqftMin", "propelio.sqftMax",
    "propelio.yearMin", "propelio.yearMax",
    "propelio.priceMin", "propelio.priceMax",
    "propelio.sortMode",
    "propelio.neighborhood",
    "propelio.parcelTypeMultifamily", "propelio.parcelTypeDuplexes",
    "propelio.parcelTypeCommercial",  "propelio.parcelTypeVacant",
    "propelio.parcelTypeExempt",      "propelio.parcelTypeOffMarket",
})


def test_filter_field_keys_constant_exists():
    assert hasattr(api_main, "_FILTER_FIELD_KEYS"), (
        "_FILTER_FIELD_KEYS constant missing from api.main (spec §2.1.A)."
    )


def test_filter_field_keys_has_all_43_expected():
    actual = getattr(api_main, "_FILTER_FIELD_KEYS")
    missing = _EXPECTED_KEYS - set(actual)
    assert not missing, f"_FILTER_FIELD_KEYS missing keys: {sorted(missing)}"


def test_filter_field_keys_no_unknown():
    actual = getattr(api_main, "_FILTER_FIELD_KEYS")
    extras = set(actual) - _EXPECTED_KEYS
    assert not extras, (
        f"_FILTER_FIELD_KEYS contains unexpected keys: {sorted(extras)}. "
        f"If you added a new filter, also update _EXPECTED_KEYS in this test."
    )


def test_filter_field_keys_count_matches_spec():
    actual = getattr(api_main, "_FILTER_FIELD_KEYS")
    # 9 checkboxes + 8 numeric + 5 sold + 8 comp + 22 propelio = 52.
    # Sprint 2 smoke catch 2026-06-02: readPropelioFiltersFromUI() spreads
    # 6 additional parcelType* keys into the propelio object at capture
    # time (mirrors of the parcel-side checkboxes). They must be in the
    # allowlist or PATCH 400s on every checkbox toggle.
    assert len(actual) == 53, (
        f"Sprint 2 captures 53 distinct filter fields; constant has {len(actual)}."
    )


def test_capture_filter_state_leaves_present_in_map_js():
    """Coarse drift check: every leaf in _EXPECTED_KEYS should appear as
    a property/identifier in frontend/map.js. Catches the case where the
    backend allowlist drifts ahead of the frontend (less common) and the
    case where someone adds to _EXPECTED_KEYS without the leaf existing.
    """
    map_js = Path(__file__).resolve().parent.parent / "frontend" / "map.js"
    src = map_js.read_text(encoding="utf-8")

    for key in _EXPECTED_KEYS:
        _, _, leaf = key.partition(".")
        # Identifier pattern — must appear somewhere in map.js
        pattern = rf"\b{re.escape(leaf)}\b"
        assert re.search(pattern, src), (
            f"_EXPECTED_KEYS includes {key!r} but leaf {leaf!r} not found "
            f"in frontend/map.js — drift or stale test data."
        )
