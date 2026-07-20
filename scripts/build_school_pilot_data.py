"""Build-time script: fetch Dallas ISD attendance-zone polygons + TEA A-F
campus ratings, emit small baked JSON files for the school-ratings pilot
(api/school_pilot/zones.py loads them at runtime).

docs/AI/SCHOOL_RATINGS_PILOT_SPEC_2026-07-20.md §4
docs/AI/SCHOOL_RATINGS_PILOT_PLAN_2026-07-20.md Task 1
docs/AI/SCHOOL_PILOT_DISD_FINDINGS_2026-07-20.md -- Task 0 spike findings this
implementation follows: no state campus number on the DISD zone layers (a
normalized-name TOKEN-OVERLAP crosswalk is required, not a direct join; plain
exact-string matching scores 0% against real data); ArcGIS's f=geojson output
is already WGS84 regardless of the layer's declared storage CRS (verify by
coordinate magnitude, transform only if actually needed); today's real data
has zero holes and never exceeds maxRecordCount (no pagination fires) but
both are implemented defensively per spec.

Run: python scripts/build_school_pilot_data.py
Output: data/school_pilot/{elementary,middle,high,ratings}.json -- gitignored,
never committed (this repo is public). Re-run to refresh.

⛔ Must be non-fatal at container build time (spec §8): if the DISD
FeatureServer or the TEA download is unreachable, log and exit 0 rather than
failing an unrelated deploy. The loader (api/school_pilot/zones.py) already
tolerates missing files -- a data outage degrades to "no school rows," never
a broken build or app.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pyproj
import requests

DISD_BASE = "https://services.arcgis.com/RfrtTbYxQ8YIhjWT/arcgis/rest/services"
# level -> (FeatureServer name, campus-name field, local-id field)
LEVELS = {
    "elementary": ("Elementary_Attendance_Boundaries", "ELEM_DESC", "SLN"),
    "middle": ("Middle_Attendance_Boundaries", "MIDDLE", "MID_SLN"),
    "high": ("High_Attendance_Boundaries", "HIGH", "HIGH_SLN"),
}
BOUNDARY_VINTAGE = "2025-26"
PAGE_SIZE = 2000  # matches the FeatureServers' maxRecordCount (Task 0 §1)

# TEA 2025 A-F statewide summary (columns used: District Number, Campus
# Number, Campus, School Type, Overall Rating) -- Task 0 findings §3.
TEA_SUMMARY_URL = (
    "https://tea.texas.gov/school-and-district-leaders/accountability/"
    "academic-accountability/performance-reporting/"
    "2025-enhanced-statewide-summary-after-appeal-updated-0.xlsx"
)
TEA_YEAR = 2025
TEA_SHEET = "2025 State Summary"
DISD_DISTRICT_NUMBER = "057905"
TEA_TYPE_BY_LEVEL = {
    "elementary": "Elementary",
    "middle": "Middle School",
    "high": "High School",
}

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "school_pilot"

REQUEST_TIMEOUT_S = 30


# --- Pure helpers (unit-tested in tests/test_school_pilot_build.py, no network) --

def reproject_ring(ring: list[list[float]], transformer: "pyproj.Transformer") -> list[list[float]]:
    """[[x,y],...] -> [[lng,lat],...] via transformer.transform (always_xy=True).
    Pass an identity (EPSG:4326 -> EPSG:4326) transformer when the source is
    already WGS84 -- see is_already_wgs84()."""
    return [list(transformer.transform(x, y)) for x, y in ring]


def is_already_wgs84(geom: dict[str, Any]) -> bool:
    """Magnitude check on the first coordinate of a GeoJSON Polygon/MultiPolygon.
    ArcGIS's f=geojson output is WGS84 regardless of the layer's DECLARED
    storage spatialReference (verified empirically against the live DISD
    FeatureServers, docs/AI/SCHOOL_PILOT_DISD_FINDINGS_2026-07-20.md §2) --
    never trust the declared CRS blindly; check the actual numbers. Lon/lat
    degrees are always |x|<=180, |y|<=90; Web Mercator meters are not."""
    coords = geom.get("coordinates") or []
    gtype = geom.get("type")
    first = None
    if gtype == "Polygon" and coords and coords[0]:
        first = coords[0][0]
    elif gtype == "MultiPolygon" and coords and coords[0] and coords[0][0]:
        first = coords[0][0][0]
    if first is None:
        return True  # nothing to check -- treat as no-op-safe
    x, y = first[0], first[1]
    return abs(x) <= 180 and abs(y) <= 90


def normalize_geometry(geom: dict[str, Any], transformer: "pyproj.Transformer") -> list[list[list[list[float]]]]:
    """GeoJSON Polygon/MultiPolygon -> `parts`: list of polygons; each polygon
    is a list of rings (ring[0]=outer, ring[1:]=holes); coords [lng,lat]
    WGS84. Matches the zone-file schema api/school_pilot/zones.py consumes."""
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon":
        polygons = [coords]
    elif gtype == "MultiPolygon":
        polygons = coords
    else:
        return []
    return [[reproject_ring(ring, transformer) for ring in polygon] for polygon in polygons]


# Stripped from both DISD zone names and TEA campus names before token-overlap
# comparison -- generic school-naming noise words, not signal. Single-letter
# tokens (initials: "W H ADAMSON", "H S") are dropped separately by length,
# not listed here.
_STOPWORDS = {
    "EL", "ELEM", "ELEMENTARY", "SCHOOL", "MIDDLE", "MID", "HS", "HIGH",
    "ACADEMY", "CENTER", "LEARNING", "NEW", "TECH", "SR", "JR", "SCH", "OF",
    "AND", "THE", "AT", "STEAM", "STEM", "LEADERSHIP", "YOUNG", "MAGNET",
    "PREP", "COMMUNICATIONS", "INNOVATION", "HUMANITIES",
}


def _tokens(name: str | None) -> set[str]:
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    return {t for t in s.split() if len(t) > 1 and t not in _STOPWORDS}


def match_campus_to_tea(campus_name: str, campus_number: str | None, tea_index: dict[str, str]) -> str | None:
    """Direct lookup on campus_number if present in tea_index; else token-set
    overlap of campus_name against tea_index's NAME keys (9-digit numeric
    keys are skipped as match candidates). Returns the tea_campus_id, or None
    on zero overlap OR a tie between two DIFFERENT real campuses -- never
    guesses (docs/AI/SCHOOL_PILOT_DISD_FINDINGS_2026-07-20.md §3: plain
    normalized-exact-match scores 0% against DISD's short names vs TEA's
    verbose official names; token overlap gets ~94%, and is verified NOT to
    conflate e.g. "Adams" with "Adamson" -- different whole tokens)."""
    if campus_number and campus_number in tea_index:
        return tea_index[campus_number]
    d_tokens = _tokens(campus_name)
    if not d_tokens:
        return None
    best_score = 0
    best_ids: set[str] = set()
    for key, tea_id in tea_index.items():
        if re.fullmatch(r"\d{9}", key):
            continue  # numeric key, not a name candidate
        score = len(d_tokens & _tokens(key))
        if score == 0:
            continue
        if score > best_score:
            best_score = score
            best_ids = {tea_id}
        elif score == best_score:
            best_ids.add(tea_id)
    if best_score == 0 or len(best_ids) != 1:
        return None  # zero match, or ambiguous tie between different campuses
    return next(iter(best_ids))


# --- Network / IO (not unit-tested directly; exercised by `main()`) ---------

def _fetch_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


def fetch_all_features(service_name: str) -> list[dict[str, Any]]:
    """Page a FeatureServer's /query via resultOffset until a page returns
    fewer than PAGE_SIZE features (Task 0 §2: f=geojson responses don't
    reliably expose `exceededTransferLimit` -- verified empirically, the
    field is simply absent -- so short-page-length is the completion check,
    a standard REST-pagination pattern that doesn't depend on it)."""
    url = f"{DISD_BASE}/{service_name}/FeatureServer/0/query"
    features: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _fetch_json(url, {
            "where": "1=1", "outFields": "*", "f": "geojson",
            "resultRecordCount": PAGE_SIZE, "resultOffset": offset,
        })
        page_features = page.get("features", [])
        features.extend(page_features)
        if len(page_features) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return features


def load_tea_ratings() -> dict[str, dict[str, Any]]:
    """Download the TEA 2025 statewide summary XLSX, filter to Dallas ISD
    (district 057905), return {tea_campus_id: {"grade": ..., "name": ...,
    "type": TEA School Type}}."""
    import openpyxl

    resp = requests.get(TEA_SUMMARY_URL, timeout=REQUEST_TIMEOUT_S * 2)
    resp.raise_for_status()
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(resp.content), read_only=True, data_only=True)
    ws = wb[TEA_SHEET]
    out: dict[str, dict[str, Any]] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        district_number, _district, campus_number, campus_name, *_rest = row
        school_type = row[6]
        overall_rating = row[13]
        if district_number != DISD_DISTRICT_NUMBER or not campus_number:
            continue
        out[str(campus_number)] = {
            "grade": overall_rating if overall_rating in ("A", "B", "C", "D", "F") else None,
            "name": campus_name,
            "type": school_type,
        }
    return out


def build_tea_index(level: str, tea_ratings: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Per-level crosswalk-lookup index: {campus_number: campus_number,
    campus_name: campus_number} restricted to this level's TEA School Type,
    for match_campus_to_tea()."""
    tea_type = TEA_TYPE_BY_LEVEL[level]
    idx: dict[str, str] = {}
    for tea_id, info in tea_ratings.items():
        if info["type"] != tea_type:
            continue
        idx[tea_id] = tea_id
        if info["name"]:
            idx[info["name"]] = tea_id
    return idx


def derive_tea_campus_id(local_id: Any) -> str | None:
    """docs/AI/SCHOOL_PILOT_DISD_FINDINGS_2026-07-20.md §3 UPDATE (superseding
    the spike's first-pass conclusion): DISD's own SLN/MID_SLN/HIGH_SLN field
    is NOT an arbitrary internal number -- zero-padded to 3 digits and
    prefixed with the district number, it IS the TEA 9-digit CAMPUS_ID.
    Verified against all 187 zones: every SLN-derived id that exists in the
    TEA index agrees 100% with an independently-computed name-token-overlap
    match (176/176 agreement, zero contradictions), and resolves every
    single zone the name-crosswalk left ambiguous or unmatched. This is the
    PREFERRED direct join the spec asked for (§6.2 option 1) -- it was just
    not an obviously-labeled field. match_campus_to_tea's name-token-overlap
    path is kept as a defensive fallback only, for a SLN that doesn't
    resolve (e.g. a campus absent from this year's TEA ratings)."""
    if not isinstance(local_id, int):
        return None
    return f"{DISD_DISTRICT_NUMBER}{local_id:03d}"


def build_level(level: str, tea_ratings: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], int, int]:
    service_name, name_field, id_field = LEVELS[level]
    features = fetch_all_features(service_name)
    tea_index = build_tea_index(level, tea_ratings)

    identity = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:4326", always_xy=True)
    mercator_to_wgs84 = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    zones = []
    matched = 0
    for feat in features:
        props = feat.get("properties") or {}
        campus_name = props.get(name_field)
        if not campus_name:
            continue
        geom = feat.get("geometry") or {}
        transformer = identity if is_already_wgs84(geom) else mercator_to_wgs84
        parts = normalize_geometry(geom, transformer)
        if not parts:
            continue
        local_id = props.get(id_field)
        derived_id = derive_tea_campus_id(local_id)
        # match_campus_to_tea already prefers a direct campus_number hit in
        # tea_index over name matching -- passing the derived id here makes
        # the direct join the primary path, name-token-overlap the fallback.
        tea_campus_id = match_campus_to_tea(campus_name, derived_id, tea_index)
        if tea_campus_id:
            matched += 1
        zones.append({
            "campus_name": campus_name,
            "tea_campus_id": tea_campus_id,
            "local_id": local_id,
            "parts": parts,
        })

    doc = {
        "meta": {
            "level": level,
            "source_url": f"{DISD_BASE}/{service_name}/FeatureServer/0",
            "retrieved": date.today().isoformat(),
            "boundary_vintage": BOUNDARY_VINTAGE,
            "crs": "EPSG:4326",
        },
        "zones": zones,
    }
    return doc, len(zones), matched


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # §8: non-fatal at build time -- a data-source outage must never fail an
    # unrelated deploy. The loader tolerates missing files, so skipping here
    # degrades to "no school rows," never a broken build.
    try:
        tea_ratings = load_tea_ratings()
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
        print(f"[school-pilot-build] TEA ratings fetch failed, skipping: {exc}", file=sys.stderr)
        return 0

    ratings_out = {tid: {"grade": info["grade"]} for tid, info in tea_ratings.items() if info["grade"]}
    (OUT_DIR / "ratings.json").write_text(json.dumps({
        "meta": {"tea_year": TEA_YEAR, "district": DISD_DISTRICT_NUMBER},
        "ratings": ratings_out,
    }, indent=None))

    total_zones = 0
    total_matched = 0
    for level in LEVELS:
        try:
            doc, count, matched = build_level(level, tea_ratings)
        except Exception as exc:  # noqa: BLE001 -- non-fatal per §8
            print(f"[school-pilot-build] {level} fetch failed, skipping: {exc}", file=sys.stderr)
            continue
        (OUT_DIR / f"{level}.json").write_text(json.dumps(doc, indent=None))
        total_zones += count
        total_matched += matched
        rate = (matched / count * 100) if count else 0.0
        print(f"[school-pilot-build] {level}: {count} zones, {matched} matched to a TEA rating ({rate:.1f}%)")

    overall_rate = (total_matched / total_zones * 100) if total_zones else 0.0
    print(f"[school-pilot-build] total: {total_zones} zones, crosswalk match rate {overall_rate:.1f}%")
    print(f"[school-pilot-build] wrote files to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
