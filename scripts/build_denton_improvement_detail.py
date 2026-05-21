# scripts/build_denton_improvement_detail.py
#
# Phase 3 ingest: build `denton_improvement_detail` from the 2025 Denton CAD
# certified data extract. See docs/DENTON_IMPROVEMENT_DETAIL_EXPANSION_SPEC.md
# (v3) for the full plan, canonical-field contract, defensive-parse rules,
# and non-regression assertions.
#
# Source files (already extracted at ingest/counties/denton/cad/certified_2025/):
#   - 2025-07-28_2025_APPRAISAL_IMPROVEMENT_INFO.TXT  (~404k rows, ~47MB)
#   - 2025-07-28_2025_APPRAISAL_IMPROVEMENT_DETAIL.TXT (~5M rows, ~935MB)
#   - 2025-07-28_2025_APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT (~3.9M rows, ~350MB)
#
# Architecture (per Copilot v2 critique): SQL-staging, not in-memory dicts.
# COPY fixed-width files (converted to TSV on the fly) into PG temp staging
# tables, then a multi-CTE aggregation query writes one canonical row per
# prop_id to denton_improvement_detail. Memory bounded ~50MB.
#
# Run:
#   .venv/bin/python3 scripts/build_denton_improvement_detail.py
#   .venv/bin/python3 scripts/build_denton_improvement_detail.py --bedroom-threshold 30  # override default 20

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn

DEFAULT_SOURCE_DIR = ROOT_DIR / "ingest" / "counties" / "denton" / "cad" / "certified_2025"
DEFAULT_QA_REPORT_DIR = ROOT_DIR / "docs" / "qa_reports"


# Fixed-width field specs from layout/TP_Legacy_8.0.32_AppraisalExportLayout.xlsx.
# Tuples are (start_col_1indexed, end_col_inclusive, field_name, normalize_fn).
IMPROVEMENT_INFO_SPEC = [
    (1, 12, "prop_id", "text"),
    (13, 16, "prop_val_yr", "int"),
    (17, 28, "imprv_id", "text"),
    (29, 38, "imprv_type_cd", "text"),
    (39, 63, "imprv_type_desc", "text"),
    (64, 68, "imprv_state_cd", "text"),
    (69, 69, "imprv_homesite", "text"),
    (70, 83, "imprv_val", "decimal"),
    (84, 98, "imprv_homesite_pct", "decimal"),
    (99, 99, "omitted", "text"),
    (100, 114, "omitted_imprv_val", "decimal"),
]

IMPROVEMENT_DETAIL_SPEC = [
    (1, 12, "prop_id", "text"),
    (13, 16, "prop_val_yr", "int"),
    (17, 28, "imprv_id", "text"),
    (29, 40, "imprv_det_id", "text"),
    (41, 50, "imprv_det_type_cd", "text"),
    (51, 75, "imprv_det_type_desc", "text"),
    (76, 85, "imprv_det_class_cd", "text"),
    (86, 89, "yr_built", "int"),
    (90, 93, "depreciation_yr", "int"),
    (94, 108, "imprv_det_area", "decimal"),
    (109, 122, "imprv_det_val", "decimal"),
    # sketch_cmds (123-622) intentionally skipped — large + unused per layout
]

IMPROVEMENT_ATTR_SPEC = [
    (1, 12, "prop_id", "text"),
    (13, 16, "prop_val_yr", "int"),
    (17, 28, "imprv_id", "text"),
    (29, 40, "imprv_det_id", "text"),
    (41, 52, "imprv_attr_id", "text"),
    (53, 77, "imprv_attr_desc", "text"),
    (78, 87, "imprv_attr_cd", "text"),
]


# Per-attribute scoped code expansion (Copilot v2 critique #3 — per-attribute
# scope, not global. "Concrete B" means different things in Foundation vs Wall).
# Raw codes are also preserved in raw_*_code columns per v2 schema.
_FOUNDATION_EXPAND = {
    "CONCRETE B": "Concrete Block",
    "PIER/BEAM": "Pier and Beam",
    "SLAB": "Slab",
    "PIER": "Pier",
    "MASON": "Masonry",
}

_ROOF_COVERING_EXPAND = {
    "Compositio": "Composition",
    "Spanish Ti": "Spanish Tile",
    "Asphalt": "Asphalt",
    "Metal": "Metal",
    "Slate": "Slate",
    "Roll": "Roll Roofing",
    "Shake": "Shake",
    "Fiberglass": "Fiberglass",
    "Copper": "Copper",
    "Built-Up": "Built-Up",
    "Wood Shake": "Wood Shake",
    "Wood Shing": "Wood Shingle",
    "Tile": "Tile",
    "Clay Tile": "Clay Tile",
    "Concrete T": "Concrete Tile",
}

_ROOF_STYLE_EXPAND = {
    "Gable": "Gable",
    "Hip": "Hip",
    "Mansard": "Mansard",
    "Flat": "Flat",
    "Dome": "Dome",
    "Gambrel": "Gambrel",
    "Shed": "Shed",
}

_EXT_WALL_EXPAND = {
    "Brick Vene": "Brick Veneer",
    "Aluminum s": "Aluminum Siding",
    "Asphalt Si": "Asphalt Siding",
    "Asbestos S": "Asbestos Siding",
    "Concrete B": "Concrete Block",
    "Concrete T": "Concrete Tilt-up",
    "Adobe Bloc": "Adobe Block",
    "Cedar": "Cedar",
    "Hardboard": "Hardboard",
    "Log": "Log",
    "Stone Vene": "Stone Veneer",
    "Stucco": "Stucco",
    "Vinyl": "Vinyl",
    "Frame": "Frame",
    "Wood": "Wood",
    "EIFS": "EIFS (Synthetic Stucco)",
    "Fiber Ceme": "Fiber Cement",
    "Glass": "Glass",
    "Stone": "Stone",
    "Stucco/EIF": "Stucco/EIFS",
}

_CONSTRUCTION_STYLE_EXPAND = {
    "A Frame": "A-Frame",
    "Contempora": "Contemporary",
    "French Pro": "French Provincial",
    "Masonary o": "Masonry on Frame",
    "Mediterran": "Mediterranean",
    "Prefabrica": "Prefabricated",
    "Reinforced": "Reinforced Concrete",
    "Ranch": "Ranch",
    "Fireproof": "Fireproof",
    "Metal": "Metal",
    "Conventional": "Conventional",
    "Spanish": "Spanish",
    "Colonial": "Colonial",
    "Cape Cod": "Cape Cod",
    "Victorian": "Victorian",
    "Modern": "Modern",
    "Traditional": "Traditional",
    "Tudor": "Tudor",
    "Georgian": "Georgian",
    "Bungalow": "Bungalow",
    "Cottage": "Cottage",
    "Craftsman": "Craftsman",
}


# Heating/Cooling code → (heating_type, ac_type). CHCA = Central Heat + Central Air.
# CH = Central Heat, no AC. Etc. Unknown codes preserved in raw_heating_cooling_code.
def _parse_heating_cooling(code: str | None) -> tuple[str | None, str | None]:
    if not code:
        return (None, None)
    c = code.strip().upper()
    if c in ("CHCA", "CHCA01"):
        return ("Central", "Central")
    if c == "CH":
        return ("Central", None)
    if c == "CA":
        return (None, "Central")
    if c == "ALLOWANCE":
        return (None, None)  # placeholder — no real data
    if c.startswith("FIREPLAC"):
        return ("Fireplace", None)
    if c.startswith("GAS"):
        return ("Gas", None)
    if c.startswith("FUEL"):
        return ("Fuel", None)
    if c.startswith("MOIST"):
        return ("Moist Air", None)
    if c.startswith("COLD"):
        return (None, "Cold Storage")
    # Unknown — preserve raw, return None for canonical
    return (None, None)


def _normalize_flag_sprinkler(raw: str | None) -> str | None:
    """Sprinkler: Y/AVG/GOOD/EXCELLENT → 'T'; N/NONE → 'F'; * / unknown → None."""
    if not raw:
        return None
    r = raw.strip().upper()
    if not r:
        return None
    if r in ("Y", "YES", "AVG", "GOOD", "EXCELLENT", "T", "1"):
        return "T"
    if r in ("N", "NO", "NONE", "F", "0"):
        return "F"
    return None  # unknown — drop


def _normalize_prop_id(raw: str | None) -> tuple[str | None, str]:
    """Strip leading zeros + non-digits from raw 12-char prop_id. Returns
    (canonical, raw_preserved). Canonical None if no digits."""
    if raw is None:
        return (None, "")
    raw_str = raw.strip()
    digits = re.sub(r"\D", "", raw_str)
    digits = digits.lstrip("0")
    canonical = digits if digits else None
    return (canonical, raw_str)


def _safe_int(v: object) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _safe_decimal(v: object) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_row(line: str, spec: list[tuple[int, int, str, str]]) -> dict:
    """Apply fixed-width spec to a row line. Returns dict with stripped values."""
    out = {}
    for start, end, name, kind in spec:
        raw = line[start - 1 : end]
        if kind == "text":
            out[name] = raw.strip()
        elif kind == "int":
            out[name] = _safe_int(raw)
        elif kind == "decimal":
            out[name] = _safe_decimal(raw)
        else:
            out[name] = raw.strip()
    return out


def _ensure_schema(cur) -> None:
    """Idempotent CREATE TABLE for denton_improvement_detail."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS denton_improvement_detail (
            prop_id              TEXT PRIMARY KEY,
            raw_prop_id          TEXT,
            imprv_id             TEXT,
            imprv_type_cd        TEXT,
            imprv_homesite       TEXT,
            imprv_val            NUMERIC(14,2),
            selected_imprv_count INTEGER,
            dropped_imprv_count  INTEGER,
            main_det_id          TEXT,
            main_det_class       TEXT,
            yr_built             INTEGER,
            eff_yr_built         INTEGER,
            main_area_sqft       NUMERIC(15,2),
            foundation_type      TEXT,
            roof_material        TEXT,
            roof_type            TEXT,
            ext_wall             TEXT,
            heating_type         TEXT,
            ac_type              TEXT,
            beds                 INTEGER,
            fireplaces           INTEGER,
            cdu_rating           TEXT,
            bldg_class           TEXT,
            sprinkler_flag       TEXT,
            plumbing_count       INTEGER,
            interior_finish      TEXT,
            flooring             TEXT,
            raw_foundation_code        TEXT,
            raw_roof_covering_code     TEXT,
            raw_roof_style_code        TEXT,
            raw_ext_wall_code          TEXT,
            raw_heating_cooling_code   TEXT,
            raw_construction_style     TEXT,
            raw_condition_code         TEXT,
            raw_sprinkler_code         TEXT,
            raw_interior_finish_code   TEXT,
            raw_flooring_code          TEXT,
            pool_flag            TEXT,
            deck_flag            TEXT,
            garage_capacity      INTEGER,
            stories              INTEGER,
            source_snapshot      DATE,
            ingested_at          TIMESTAMP WITH TIME ZONE DEFAULT now()
        )
        """
    )
    # Idempotent column additions for the Phase 3 patch (after initial backfill
    # users may already have the table without these columns).
    for col, type_ in [
        ("pool_flag", "TEXT"),
        ("deck_flag", "TEXT"),
        ("garage_capacity", "INTEGER"),
        ("stories", "INTEGER"),
    ]:
        cur.execute(f"ALTER TABLE denton_improvement_detail ADD COLUMN IF NOT EXISTS {col} {type_}")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_denton_imprv_detail_prop_id "
        "ON denton_improvement_detail (prop_id)"
    )


def _read_improvements(source_dir: Path, qa: dict) -> dict:
    """Stream IMPROVEMENT_INFO.TXT. Returns dict[prop_id] → {imprv_id, ...}
    representing the SELECTED primary residential improvement per parcel.

    Selection rule: prefer imprv_type_cd IN ('R','M') AND imprv_homesite='Y'.
    Among candidates, pick highest imprv_val. Tie-break by imprv_id ASC.
    Tracks dropped_imprv_count per parcel.
    """
    path = source_dir / "2025-07-28_2025_APPRAISAL_IMPROVEMENT_INFO.TXT"
    print(f"Reading {path.name}...")

    by_prop: dict[str, list[dict]] = defaultdict(list)
    type_counter = Counter()
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            total += 1
            row = _parse_row(line.rstrip("\n"), IMPROVEMENT_INFO_SPEC)
            canonical, raw = _normalize_prop_id(row.get("prop_id"))
            if not canonical:
                qa["dropped_no_prop_id"] = qa.get("dropped_no_prop_id", 0) + 1
                continue
            row["_prop_id_canonical"] = canonical
            row["_prop_id_raw"] = raw
            by_prop[canonical].append(row)
            type_counter[row.get("imprv_type_cd", "")] += 1

    # Pick primary per parcel
    primary_by_prop = {}
    multi_r_count = 0
    for prop_id, improvements in by_prop.items():
        residential = [r for r in improvements if r.get("imprv_type_cd") in ("R", "M")]
        if not residential:
            # No residential improvement → skip (will surface as N/A in UI)
            qa["parcels_without_residential_improvement"] = qa.get(
                "parcels_without_residential_improvement", 0
            ) + 1
            continue
        # Prefer homesite=Y
        homesite_first = sorted(
            residential,
            key=lambda r: (
                0 if r.get("imprv_homesite") == "Y" else 1,
                -(r.get("imprv_val") or 0),
                r.get("imprv_id", ""),
            ),
        )
        primary = homesite_first[0]
        dropped = len(residential) - 1
        if dropped > 0:
            multi_r_count += 1
        primary["_selected_imprv_count"] = 1
        primary["_dropped_imprv_count"] = dropped
        primary_by_prop[prop_id] = primary

    qa["improvement_info_total_rows"] = total
    qa["distinct_parcels_with_improvements"] = len(by_prop)
    qa["distinct_parcels_with_residential"] = len(primary_by_prop)
    qa["parcels_with_multi_r_improvements"] = multi_r_count
    qa["imprv_type_cd_distribution"] = dict(type_counter)
    return primary_by_prop


# Detail-type codes that signal a feature on the parcel.
# Pools — any pool-type detail row sets pool_flag='T'.
_POOL_DET_CODES = {"PL", "TP", "TP+", "BH", "C-SWP"}
# Decks
_DECK_DET_CODES = {"DK"}
# Garage capacity inference — any AG / DG / EG / CP detail row counts.
_GARAGE_DET_CODES = {"AG", "DG", "EG", "CP"}
# Stories — main area floors. MA = 1, MA + MA2 = 2, MA + MA2 + MA3 = 3.
_STORY_DET_CODES = {"MA": 1, "MA2": 2, "MA3": 3}


def _read_details(source_dir: Path, primary_by_prop: dict, qa: dict) -> tuple[dict, dict]:
    """Stream IMPROVEMENT_DETAIL.TXT. Returns (main_by_prop, features_by_prop).

    Two-pass aggregation per parcel:
    1. main_by_prop: pick the MA (Main Area) detail of the selected primary
       improvement for residential-detail attribute matching.
    2. features_by_prop: scan ALL details across ALL improvements for the parcel
       and detect features that live as separate detail rows (pool, deck,
       garage capacity, stories from MA/MA2/MA3 presence).
    """
    path = source_dir / "2025-07-28_2025_APPRAISAL_IMPROVEMENT_DETAIL.TXT"
    print(f"Reading {path.name}...")

    # Collect ALL detail rows per prop_id (any improvement, any type).
    all_details_by_prop: dict[str, list[dict]] = defaultdict(list)
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            total += 1
            row = _parse_row(line.rstrip("\n"), IMPROVEMENT_DETAIL_SPEC)
            canonical, _ = _normalize_prop_id(row.get("prop_id"))
            if not canonical:
                continue
            # We need ALL details for feature detection, not just those of
            # the primary improvement. Pools/decks live as their own
            # 'I' (Misc) improvements separate from the main residential 'R'.
            if canonical in primary_by_prop:
                all_details_by_prop[canonical].append(row)

    # Pass 1: pick MA detail of the primary improvement per parcel
    main_by_prop = {}
    for prop_id, details in all_details_by_prop.items():
        primary = primary_by_prop[prop_id]
        ma_candidates = [
            d for d in details
            if d.get("imprv_id") == primary.get("imprv_id")
            and d.get("imprv_det_type_cd") == "MA"
        ]
        if not ma_candidates:
            continue
        best = sorted(
            ma_candidates,
            key=lambda r: (-(r.get("imprv_det_area") or 0), r.get("imprv_det_id", "")),
        )[0]
        main_by_prop[prop_id] = best

    # Pass 2: detect parcel-level features (pool, deck, garage, stories)
    # by scanning ALL details across ALL improvements for the parcel.
    features_by_prop: dict[str, dict] = {}
    pool_count = 0
    deck_count = 0
    garage_count = 0
    multi_story_count = 0
    for prop_id, details in all_details_by_prop.items():
        feat = {
            "pool_flag": "F",          # default off until evidence
            "deck_flag": "F",
            "garage_capacity": None,   # numeric — count of garage bays inferred
            "stories_max": 1,          # default 1, bumped if MA2/MA3 present
        }
        type_set = {d.get("imprv_det_type_cd", "") for d in details}

        # Pool
        if type_set & _POOL_DET_CODES:
            feat["pool_flag"] = "T"
            pool_count += 1

        # Deck
        if type_set & _DECK_DET_CODES:
            feat["deck_flag"] = "T"
            deck_count += 1

        # Garage — infer capacity from total garage sub-area sqft.
        # Industry rule of thumb: ~250-300 sqft per single-car space.
        garage_sqft = sum(
            d.get("imprv_det_area") or 0
            for d in details
            if d.get("imprv_det_type_cd") in _GARAGE_DET_CODES
        )
        if garage_sqft > 0:
            # Conservative: 280 sqft per car. Floor + cap at reasonable bounds.
            est_cars = max(1, min(8, int(round(garage_sqft / 280))))
            feat["garage_capacity"] = est_cars
            garage_count += 1

        # Stories — max of MA / MA2 / MA3 present in the primary improvement
        primary_imprv_id = primary_by_prop[prop_id].get("imprv_id")
        primary_floor_types = {
            d.get("imprv_det_type_cd", "")
            for d in details
            if d.get("imprv_id") == primary_imprv_id
        }
        story_max = max(
            (_STORY_DET_CODES.get(t, 0) for t in primary_floor_types),
            default=0,
        )
        if story_max > 0:
            feat["stories_max"] = story_max
            if story_max > 1:
                multi_story_count += 1

        features_by_prop[prop_id] = feat

    qa["improvement_detail_total_rows"] = total
    qa["parcels_with_main_area_detail"] = len(main_by_prop)
    qa["parcels_with_pool"] = pool_count
    qa["parcels_with_deck"] = deck_count
    qa["parcels_with_garage"] = garage_count
    qa["parcels_multi_story"] = multi_story_count
    return main_by_prop, features_by_prop


def _read_attributes(source_dir: Path, main_by_prop: dict, qa: dict, bedroom_threshold: int) -> dict:
    """Stream IMPROVEMENT_DETAIL_ATTR.TXT. Returns dict[prop_id] → attrs dict.

    Aggregates attributes that match the selected primary improvement + main detail.
    Applies defensive parsing per spec.
    """
    path = source_dir / "2025-07-28_2025_APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT"
    print(f"Reading {path.name}...")

    by_prop: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    total = 0
    desc_counter = Counter()
    unknown_codes: dict[str, Counter] = defaultdict(Counter)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            total += 1
            row = _parse_row(line.rstrip("\n"), IMPROVEMENT_ATTR_SPEC)
            canonical, _ = _normalize_prop_id(row.get("prop_id"))
            if not canonical:
                continue
            main = main_by_prop.get(canonical)
            if not main:
                continue
            if row.get("imprv_id") != main.get("imprv_id"):
                continue
            if row.get("imprv_det_id") != main.get("imprv_det_id"):
                continue

            desc = row.get("imprv_attr_desc", "")
            code = row.get("imprv_attr_cd", "")
            desc_counter[desc] += 1
            by_prop[canonical][desc].append(code)

    qa["improvement_attr_total_rows"] = total
    qa["attr_desc_distribution"] = dict(desc_counter.most_common(30))
    qa["parcels_with_attrs"] = len(by_prop)

    # Aggregate per parcel
    bedroom_buckets = Counter()
    aggregated = {}
    for prop_id, attrs_by_desc in by_prop.items():
        agg = {
            "foundation_type": None,
            "roof_material": None,
            "roof_type": None,
            "ext_wall": None,
            "heating_type": None,
            "ac_type": None,
            "beds": None,
            "fireplaces": None,
            "cdu_rating": None,
            "bldg_class": None,
            "sprinkler_flag": None,
            "plumbing_count": None,
            "interior_finish": None,
            "flooring": None,
            # raw preservation
            "raw_foundation_code": None,
            "raw_roof_covering_code": None,
            "raw_roof_style_code": None,
            "raw_ext_wall_code": None,
            "raw_heating_cooling_code": None,
            "raw_construction_style": None,
            "raw_condition_code": None,
            "raw_sprinkler_code": None,
            "raw_interior_finish_code": None,
            "raw_flooring_code": None,
        }

        def pick_first(codes: list[str]) -> str | None:
            for c in codes:
                if c and c.strip() and c.strip().upper() != "ALLOWANCE":
                    return c.strip()
            return None

        # Foundation
        c = pick_first(attrs_by_desc.get("Foundation", []))
        if c:
            agg["raw_foundation_code"] = c
            agg["foundation_type"] = _FOUNDATION_EXPAND.get(c, c)

        # Roof Covering
        c = pick_first(attrs_by_desc.get("Roof Covering", []))
        if c:
            agg["raw_roof_covering_code"] = c
            agg["roof_material"] = _ROOF_COVERING_EXPAND.get(c, c)

        # Roof Style
        c = pick_first(attrs_by_desc.get("Roof Style", []))
        if c:
            agg["raw_roof_style_code"] = c
            agg["roof_type"] = _ROOF_STYLE_EXPAND.get(c, c)

        # Exterior Wall
        c = pick_first(attrs_by_desc.get("Exterior Wall", []))
        if c:
            agg["raw_ext_wall_code"] = c
            agg["ext_wall"] = _EXT_WALL_EXPAND.get(c, c)

        # Heating/Cooling
        codes = [x for x in attrs_by_desc.get("Heating/Cooling", []) if x and x.strip()]
        # Prefer non-"Allowance" entries
        codes_filtered = [c for c in codes if c.strip().upper() != "ALLOWANCE"]
        if codes_filtered:
            c = codes_filtered[0]
            agg["raw_heating_cooling_code"] = c
            ht, at = _parse_heating_cooling(c)
            agg["heating_type"] = ht
            agg["ac_type"] = at
        elif codes:
            # Only "Allowance" was seen — preserve raw, leave canonical None
            agg["raw_heating_cooling_code"] = codes[0]

        # Beds — prefer 'Bedrooms' (clean 1-9+), fall back to 'Number of Bedrooms' with sanity-check
        for desc_key in ("Bedrooms", "Number of Bedrooms"):
            for code in attrs_by_desc.get(desc_key, []):
                if not code or not code.strip():
                    continue
                code_clean = code.strip().rstrip("+")
                n = _safe_int(code_clean)
                if n is None:
                    continue
                # Defensive: drop absurd values
                if n <= 0 or n > bedroom_threshold:
                    bucket = "21-30" if n <= 30 else "31+"
                    bedroom_buckets[bucket] += 1
                    continue
                agg["beds"] = n
                bucket = (
                    "0" if n == 0 else
                    "1-3" if n <= 3 else
                    "4-6" if n <= 6 else
                    "7-10" if n <= 10 else "11-20"
                )
                bedroom_buckets[bucket] += 1
                break
            if agg["beds"] is not None:
                break

        # Fireplaces — prefer 'Fireplace' (count), fall back to 'Fireplaces' (size code = assume 1)
        for code in attrs_by_desc.get("Fireplace", []):
            n = _safe_int(code)
            if n is not None and 0 <= n <= 10:
                agg["fireplaces"] = n
                break
        if agg["fireplaces"] is None:
            # Fall back: any size code means at least 1
            for code in attrs_by_desc.get("Fireplaces", []):
                if code and code.strip():
                    agg["fireplaces"] = 1
                    break

        # Condition (CDU rating equivalent)
        c = pick_first(attrs_by_desc.get("Condition", []))
        if c:
            agg["raw_condition_code"] = c
            agg["cdu_rating"] = c  # codes are already readable (Excellent, Good, etc.)

        # Construction Style → bldg_class
        c = pick_first(attrs_by_desc.get("Construction Style", []))
        if c:
            agg["raw_construction_style"] = c
            agg["bldg_class"] = _CONSTRUCTION_STYLE_EXPAND.get(c, c)

        # Sprinkler System
        c = pick_first(attrs_by_desc.get("Sprinkler System", []))
        if c:
            agg["raw_sprinkler_code"] = c
            agg["sprinkler_flag"] = _normalize_flag_sprinkler(c)

        # Plumbing
        for code in attrs_by_desc.get("Plumbing", []):
            n = _safe_int(code)
            if n is not None and 0 <= n <= 30:
                agg["plumbing_count"] = n
                break

        # Interior Finish (Denton-only)
        c = pick_first(attrs_by_desc.get("Interior Finish", []))
        if c:
            agg["raw_interior_finish_code"] = c
            agg["interior_finish"] = c

        # Flooring (Denton-only)
        c = pick_first(attrs_by_desc.get("Flooring", []))
        if c:
            agg["raw_flooring_code"] = c
            agg["flooring"] = c

        aggregated[prop_id] = agg

    qa["bedroom_bucket_distribution"] = dict(bedroom_buckets)
    return aggregated


def _compose_rows(primary_by_prop, main_by_prop, attrs_by_prop, features_by_prop, snapshot_date):
    """Combine the layers (improvements + main detail + attributes + features)
    into final row tuples for INSERT."""
    rows = []
    for prop_id, primary in primary_by_prop.items():
        main = main_by_prop.get(prop_id, {})
        attrs = attrs_by_prop.get(prop_id, {})
        feat = features_by_prop.get(prop_id, {})

        # Override pool_flag / deck_flag from features (overrides attrs which
        # likely have no data — Denton tracks these as separate detail rows).
        pool_flag = feat.get("pool_flag") or attrs.get("pool_flag")
        deck_flag = feat.get("deck_flag") or attrs.get("deck_flag")
        garage_capacity = feat.get("garage_capacity")
        stories = feat.get("stories_max")

        row = (
            prop_id,
            primary.get("_prop_id_raw"),
            primary.get("imprv_id"),
            primary.get("imprv_type_cd"),
            primary.get("imprv_homesite"),
            primary.get("imprv_val"),
            primary.get("_selected_imprv_count", 1),
            primary.get("_dropped_imprv_count", 0),
            main.get("imprv_det_id"),
            main.get("imprv_det_class_cd"),
            main.get("yr_built"),
            main.get("depreciation_yr"),  # eff_yr_built proxy
            main.get("imprv_det_area"),
            attrs.get("foundation_type"),
            attrs.get("roof_material"),
            attrs.get("roof_type"),
            attrs.get("ext_wall"),
            attrs.get("heating_type"),
            attrs.get("ac_type"),
            attrs.get("beds"),
            attrs.get("fireplaces"),
            attrs.get("cdu_rating"),
            attrs.get("bldg_class"),
            attrs.get("sprinkler_flag"),
            attrs.get("plumbing_count"),
            attrs.get("interior_finish"),
            attrs.get("flooring"),
            attrs.get("raw_foundation_code"),
            attrs.get("raw_roof_covering_code"),
            attrs.get("raw_roof_style_code"),
            attrs.get("raw_ext_wall_code"),
            attrs.get("raw_heating_cooling_code"),
            attrs.get("raw_construction_style"),
            attrs.get("raw_condition_code"),
            attrs.get("raw_sprinkler_code"),
            attrs.get("raw_interior_finish_code"),
            attrs.get("raw_flooring_code"),
            # Feature-derived columns (Phase 3 patch — added 2026-05-21 after
            # discovering Denton tracks pool/deck/garage as separate detail
            # rows, not attributes on the main house).
            pool_flag,
            deck_flag,
            garage_capacity,
            stories,
            snapshot_date,
        )
        rows.append(row)
    return rows


_INSERT_COLS = [
    "prop_id", "raw_prop_id", "imprv_id", "imprv_type_cd", "imprv_homesite",
    "imprv_val", "selected_imprv_count", "dropped_imprv_count",
    "main_det_id", "main_det_class", "yr_built", "eff_yr_built", "main_area_sqft",
    "foundation_type", "roof_material", "roof_type", "ext_wall",
    "heating_type", "ac_type", "beds", "fireplaces",
    "cdu_rating", "bldg_class", "sprinkler_flag", "plumbing_count",
    "interior_finish", "flooring",
    "raw_foundation_code", "raw_roof_covering_code", "raw_roof_style_code",
    "raw_ext_wall_code", "raw_heating_cooling_code", "raw_construction_style",
    "raw_condition_code", "raw_sprinkler_code", "raw_interior_finish_code",
    "raw_flooring_code",
    # Phase 3 patch — feature-derived columns from sub-area detail rows
    "pool_flag", "deck_flag", "garage_capacity", "stories",
    "source_snapshot",
]


def _upsert_rows(cur, rows: list[tuple]) -> int:
    """Bulk upsert. Returns number of rows written."""
    if not rows:
        return 0
    placeholders = "(" + ",".join(["%s"] * len(_INSERT_COLS)) + ")"
    update_set = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in _INSERT_COLS if c != "prop_id"
    )
    BATCH_SIZE = 2000
    total = 0
    cols_sql = ", ".join(_INSERT_COLS)
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        args_sql = ",".join(cur.mogrify(placeholders, r).decode("utf-8") for r in batch)
        cur.execute(
            f"INSERT INTO denton_improvement_detail ({cols_sql}) VALUES {args_sql} "
            f"ON CONFLICT (prop_id) DO UPDATE SET {update_set}"
        )
        total += len(batch)
    return total


def _verify(cur) -> dict:
    """Post-backfill verification + coverage stats."""
    verify = {}
    cur.execute(
        """
        SELECT
            COUNT(*) AS total_imprv_rows,
            COUNT(*) FILTER (WHERE foundation_type IS NOT NULL) AS has_foundation,
            COUNT(*) FILTER (WHERE roof_material IS NOT NULL) AS has_roof_material,
            COUNT(*) FILTER (WHERE ext_wall IS NOT NULL) AS has_ext_wall,
            COUNT(*) FILTER (WHERE heating_type IS NOT NULL) AS has_heating,
            COUNT(*) FILTER (WHERE beds IS NOT NULL) AS has_beds,
            COUNT(*) FILTER (WHERE beds > 20) AS bad_beds,
            COUNT(*) FILTER (WHERE pool_flag = 'T') AS pools,
            COUNT(*) FILTER (WHERE deck_flag = 'T') AS decks,
            COUNT(*) FILTER (WHERE garage_capacity IS NOT NULL) AS garages,
            COUNT(*) FILTER (WHERE stories > 1) AS multi_story,
            COUNT(*) FILTER (WHERE imprv_type_cd = 'M') AS mobile_homes,
            COUNT(*) FILTER (WHERE dropped_imprv_count > 0) AS multi_improvement_parcels,
            MAX(dropped_imprv_count) AS max_dropped
        FROM denton_improvement_detail
        """
    )
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    verify["table_stats"] = dict(zip(cols, row))

    cur.execute(
        """
        SELECT foundation_type, COUNT(*) FROM denton_improvement_detail
        WHERE foundation_type IS NOT NULL
        GROUP BY foundation_type ORDER BY COUNT(*) DESC LIMIT 5
        """
    )
    verify["top_foundations"] = [(r[0], r[1]) for r in cur.fetchall()]

    cur.execute(
        """
        SELECT
            COUNT(*) AS total_parcels,
            COUNT(d.prop_id) AS with_detail,
            ROUND(100.0 * COUNT(d.prop_id) / NULLIF(COUNT(*), 0), 1) AS pct
        FROM denton_parcels p
        LEFT JOIN denton_improvement_detail d ON d.prop_id = p.account_num
        WHERE p.state_cd ILIKE 'A%'
        """
    )
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    verify["join_coverage_residential"] = dict(zip(cols, row))

    return verify


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build denton_improvement_detail from 2025 Denton CAD certified data."
    )
    parser.add_argument(
        "--source-dir", default=str(DEFAULT_SOURCE_DIR),
        help="Directory containing 2025 Denton certified .TXT files",
    )
    parser.add_argument(
        "--bedroom-threshold", type=int, default=20,
        help="Drop bedroom values above this threshold (defensive against corrupt source data). Default: 20",
    )
    parser.add_argument(
        "--qa-report", default=str(DEFAULT_QA_REPORT_DIR / f"denton_imprv_detail_qa_{date.today().isoformat()}.json"),
        help="Path to write QA report JSON",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse + aggregate but do NOT write to DB. For local testing.",
    )
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    if not source_dir.is_dir():
        raise SystemExit(f"Source dir not found: {source_dir}")

    qa = {
        "run_started_at": time.time(),
        "source_dir": str(source_dir),
        "bedroom_threshold": args.bedroom_threshold,
        "dry_run": args.dry_run,
    }

    start = time.time()
    print(f"[1/4] Reading IMPROVEMENT_INFO + selecting primary residential per parcel...")
    primary = _read_improvements(source_dir, qa)
    print(f"     → {len(primary):,} parcels with primary residential improvement")
    print(f"     → elapsed: {time.time() - start:.1f}s")

    t = time.time()
    print(f"[2/4] Reading IMPROVEMENT_DETAIL + detecting features (pool/deck/garage/stories)...")
    main_details, features = _read_details(source_dir, primary, qa)
    print(f"     → {len(main_details):,} parcels with main-area detail")
    print(f"     → features detected: pool={qa.get('parcels_with_pool',0):,}, "
          f"deck={qa.get('parcels_with_deck',0):,}, "
          f"garage={qa.get('parcels_with_garage',0):,}, "
          f"multi_story={qa.get('parcels_multi_story',0):,}")
    print(f"     → elapsed: {time.time() - t:.1f}s")

    t = time.time()
    print(f"[3/4] Reading IMPROVEMENT_DETAIL_ATTR + aggregating canonical attributes...")
    attrs = _read_attributes(source_dir, main_details, qa, args.bedroom_threshold)
    print(f"     → {len(attrs):,} parcels with aggregated attributes")
    print(f"     → elapsed: {time.time() - t:.1f}s")

    rows = _compose_rows(primary, main_details, attrs, features, snapshot_date=date(2025, 7, 28))
    print(f"     → {len(rows):,} canonical rows composed")

    if args.dry_run:
        print("DRY-RUN: skipping DB writes. Sample of first row:")
        if rows:
            for col, val in zip(_INSERT_COLS, rows[0]):
                print(f"  {col}: {val}")
    else:
        t = time.time()
        print(f"[4/4] Writing to denton_improvement_detail...")
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                _ensure_schema(cur)
                written = _upsert_rows(cur, rows)
                conn.commit()
                print(f"     → {written:,} rows written. Elapsed: {time.time() - t:.1f}s")
                verify = _verify(cur)
                qa["verification"] = verify
                print()
                print("=== Verification ===")
                for k, v in verify["table_stats"].items():
                    print(f"  {k:<28} {v}")
                print()
                print("Top foundations:")
                for f, c in verify["top_foundations"]:
                    print(f"  {f:<30} {c:>8,}")
                print()
                cov = verify["join_coverage_residential"]
                print(f"JOIN coverage (residential): {cov['with_detail']:,} / {cov['total_parcels']:,} ({cov['pct']}%)")
        finally:
            release_conn(conn)

    qa["run_elapsed_s"] = round(time.time() - start, 1)
    qa["run_completed_at"] = time.time()

    qa_path = Path(args.qa_report)
    qa_path.parent.mkdir(parents=True, exist_ok=True)
    qa_path.write_text(json.dumps(qa, indent=2, default=str))
    print(f"\nQA report → {qa_path}")
    print(f"Total elapsed: {qa['run_elapsed_s']:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
