# api/propelio/parcel_match.py
#
# Batch comp-to-parcel matcher for Propelio payload enrichment.
# Adds parcel footprint geometry + account_num for comps when address keys
# match county parcel tables.
#
# Strategy: bbox-fetch raw parcel rows from each county within the comp
# pool's bounding box, then apply Python normalize_addr_key (the SAME
# normalizer Propelio comp addresses go through) to BOTH sides. This
# avoids the SQL/Python normalization-mismatch bug that an earlier
# all-SQL approach hit (USPS abbreviation expansion like RD->ROAD,
# LN->LANE etc. is too painful to fully replicate in REGEXP, and
# partial replication causes silent match failures).
#
# Connects to:
#   api/config.py  - get_conn/release_conn DB helpers
#   api/redfin.py  - normalize_addr_key canonical address-key function

from __future__ import annotations

import logging
from typing import Any, Iterable

from api.config import get_conn, release_conn
from api.redfin import normalize_addr_key


logger = logging.getLogger(__name__)


# Each tuple: (table, account_col, address_col, geom_select_sql)
# DCAD's parcels table stores its polygon as the pre-baked GeoJSON column
# `polygon_geojson`; the other counties store native geom and we cast at
# select time via ST_AsGeoJSON.
_COUNTY_TABLES: dict[str, tuple[str, str, str, str]] = {
    "dcad":   ("parcels",         "account_num", "property_address", "polygon_geojson::json"),
    "tad":    ("tad_parcels",     "account_num", "situs_addr",       "ST_AsGeoJSON(geom)::json"),
    "collin": ("collin_parcels",  "account_num", "property_address", "ST_AsGeoJSON(geom)::json"),
    "denton": ("denton_parcels",  "account_num", "property_address", "ST_AsGeoJSON(geom)::json"),
}


def _comp_lat_lng(comp: dict[str, Any]) -> tuple[float | None, float | None]:
    extra = comp.get("extra") if isinstance(comp.get("extra"), dict) else {}
    lat_raw = extra.get("lat")
    lng_raw = extra.get("lon", extra.get("lng"))
    try:
        return float(lat_raw), float(lng_raw)
    except (TypeError, ValueError):
        return None, None


def _comps_bbox(comps: Iterable[dict[str, Any]], slack_deg: float = 0.001) -> tuple[float, float, float, float] | None:
    """Return (min_lng, min_lat, max_lng, max_lat) covering all comp points,
    expanded by `slack_deg` (~110m at TX latitude) so border parcels aren't
    accidentally excluded by floating-point edge cases.
    Returns None when no comp has usable coordinates.
    """
    lats: list[float] = []
    lngs: list[float] = []
    for c in comps:
        lat, lng = _comp_lat_lng(c)
        if lat is not None and lng is not None:
            lats.append(lat)
            lngs.append(lng)
    if not lats:
        return None
    return (min(lngs) - slack_deg, min(lats) - slack_deg, max(lngs) + slack_deg, max(lats) + slack_deg)


def _fetch_county_parcels_in_bbox(county: str, bbox: tuple[float, float, float, float]) -> list[tuple[str, str, Any, float, float]]:
    """Pull (account_num, raw_address, parcel_geom, centroid_lat, centroid_lng)
    tuples from one county where the parcel's centroid is inside the bbox.

    Returning centroid lat/lng enables a lat/lon proximity fallback when
    address-key matching fails (handles spacing differences like
    "Preston Crest" vs "Prestoncrest" or street-name misspellings).
    """
    spec = _COUNTY_TABLES.get(county)
    if not spec:
        return []
    table, account_col, address_col, geom_expr = spec
    min_lng, min_lat, max_lng, max_lat = bbox
    sql = f"""
        SELECT
            {account_col} AS account_num,
            {address_col} AS raw_address,
            {geom_expr}   AS parcel_geom,
            ST_Y(centroid) AS centroid_lat,
            ST_X(centroid) AS centroid_lng
        FROM {table}
        WHERE {address_col} IS NOT NULL
          AND {address_col} <> ''
          AND centroid IS NOT NULL
          AND ST_Intersects(
              centroid,
              ST_MakeEnvelope(%s, %s, %s, %s, 4326)
          )
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (min_lng, min_lat, max_lng, max_lat))
            return [
                (
                    str(r[0] or ""),
                    str(r[1] or ""),
                    r[2],
                    float(r[3]) if r[3] is not None else None,
                    float(r[4]) if r[4] is not None else None,
                )
                for r in cur.fetchall() or []
            ]
    finally:
        release_conn(conn)


def _street_only(raw: str) -> str:
    """Strip multi-line + city/state suffix from a parcel address.

    Several county parcel tables store addresses as multi-line strings
    like ``'4400 BARWYN LN \\r\\nPLANO, TX 75093'`` (Collin) or with city
    appended after a comma. Without this trim, ``normalize_addr_key``
    tokenizes the city/state/zip into the address key and matching
    silently fails. Mirrors the pattern in
    ``api/main.py:_parcel_addr_match_key``.
    """
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    street = first_line.split(",", 1)[0].strip()
    return street or first_line or text


_DIRECTION_PREFIX = frozenset({"N", "S", "E", "W", "NE", "NW", "SE", "SW"})

# Mirrors api/redfin.py:_STRIP_SUFFIXES — kept local so we can reference
# it without coupling to redfin's internal symbol. Used to detect a
# street-type word that's second-to-last when the last token is a
# trailing direction suffix.
_STREET_TYPE_TOKENS = frozenset({
    "ALLEY", "AVENUE", "BOULEVARD", "CIRCLE", "COMMON", "COURT", "COVE",
    "CROSSING", "DRIVE", "EXPRESSWAY", "FREEWAY", "HIGHWAY", "LANE",
    "PARKWAY", "PLACE", "ROAD", "SQUARE", "STREET", "TERRACE", "TRAIL",
    "WAY",
})


def _propelio_match_key(raw: str) -> str:
    """Tighter address-key normalization for Propelio comp <-> parcel matching.

    Wraps :func:`api.redfin.normalize_addr_key` after :func:`_street_only`,
    then applies three additional transforms that fix common
    Propelio-vs-parcel mismatches found in production testing:

    1. **Optional leading direction prefix** stripped after the street
       number. Propelio sometimes drops it ("3316 Malcolm X") while
       parcel records keep it ("3316 S MALCOLM X"). Both sides now
       normalize to "3316 MALCOLM X".
    2. **Trailing direction suffix** with embedded street type stripped.
       Fort Worth has many streets like "Bellaire Drive S" / "Bellaire
       Dr S" — different layouts of the same address. After
       normalize_addr_key the suffix-stripping only fires when the
       trailing word is a type ("DRIVE"); when a direction follows the
       type, normalize_addr_key leaves both in. We detect that case
       here and drop the type, keeping just the direction.
    3. **Consecutive single-letter tokens collapsed** so "J B JACKSON"
       and "JB JACKSON" both become "JB JACKSON". Common for streets
       named after people with multiple initials.
    """
    base = normalize_addr_key(_street_only(raw)).strip().upper()
    if not base:
        return ""
    tokens = base.split()
    # 1. Leading direction prefix after street number
    if len(tokens) >= 3 and tokens[1] in _DIRECTION_PREFIX:
        tokens = [tokens[0]] + tokens[2:]
    # 2. Trailing direction with second-to-last street type
    if (
        len(tokens) >= 3
        and tokens[-1] in _DIRECTION_PREFIX
        and tokens[-2] in _STREET_TYPE_TOKENS
    ):
        tokens = tokens[:-2] + [tokens[-1]]
    # 3. Collapse consecutive single-letter tokens
    collapsed: list[str] = []
    buffer: list[str] = []
    for t in tokens:
        if len(t) == 1 and t.isalpha():
            buffer.append(t)
        else:
            if buffer:
                collapsed.append("".join(buffer))
                buffer = []
            collapsed.append(t)
    if buffer:
        collapsed.append("".join(buffer))
    return " ".join(collapsed)


def _build_addr_key_index(rows: Iterable[tuple[str, str, Any, float | None, float | None]], county: str) -> dict[str, dict[str, Any]]:
    """Apply Python normalize_addr_key to each row's raw_address and index
    by the resulting key. First-seen key wins on duplicates.
    """
    index: dict[str, dict[str, Any]] = {}
    for account_num, raw_address, parcel_geom, _clat, _clng in rows:
        key = _propelio_match_key(raw_address)
        if not key:
            continue
        if key in index:
            continue
        index[key] = {
            "parcel_account_num": account_num,
            "parcel_geom": parcel_geom,
            "source_county": county,
        }
    return index


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine distance in meters. Inlined for Python-side proximity lookup."""
    import math
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.asin(math.sqrt(a))


def match_comps_to_parcels(comps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inject `parcel_geom` (GeoJSON dict | None) and `parcel_account_num`
    (str | None) onto each comp dict by joining on Python-normalized
    address keys against parcels whose centroid falls inside the comps'
    bounding box.

    Single batched query per county (4 queries total). Both sides of the
    join apply the same Python `normalize_addr_key` to ensure RD/ROAD
    style abbreviation differences don't cause silent misses.

    Mutates and returns the same list. Comps without resolvable lat/lon
    or whose normalized key doesn't appear in the parcel index simply
    get `parcel_geom=None`, and the frontend falls back to a dot.
    """
    if not comps:
        return comps

    # Initialize each comp with null values so callers can rely on the
    # fields existing.
    for c in comps:
        c["parcel_geom"] = None
        c["parcel_account_num"] = None
        c["parcel_county"] = None

    bbox = _comps_bbox(comps)
    if bbox is None:
        logger.info("match_comps_to_parcels: no comp coordinates, skipping")
        return comps

    # Precompute each comp's address key (Python side, single source of truth).
    # _propelio_match_key handles street-only extraction, normalize_addr_key,
    # direction-prefix stripping (N/S/E/W), and initials-collapse (J B -> JB).
    comp_keys: list[str | None] = []
    for c in comps:
        key = _propelio_match_key(str(c.get("address") or ""))
        comp_keys.append(key or None)

    # Fetch + normalize per county. Chain a single global key->parcel
    # index; first county to match a key wins. Also accumulate a flat
    # list of (parcel_geom, account_num, centroid_lat, centroid_lng) for
    # the lat/lon proximity fallback on address-key misses.
    global_index: dict[str, dict[str, Any]] = {}
    geo_pool: list[tuple[Any, str, str, float, float]] = []
    for county in _COUNTY_TABLES:
        rows = _fetch_county_parcels_in_bbox(county, bbox)
        county_index = _build_addr_key_index(rows, county)
        for k, v in county_index.items():
            global_index.setdefault(k, v)
        for account_num, _raw, parcel_geom, clat, clng in rows:
            if clat is not None and clng is not None and parcel_geom is not None:
                geo_pool.append((parcel_geom, account_num, county, clat, clng))

    matched_by_addr = 0
    matched_by_geo = 0
    PROXIMITY_THRESHOLD_M = 30.0  # ~98ft, same parcel by any reasonable measure

    for idx, key in enumerate(comp_keys):
        # Try address-key match first (deterministic, fast)
        if key:
            hit = global_index.get(key)
            if hit is not None:
                comps[idx]["parcel_geom"] = hit["parcel_geom"]
                comps[idx]["parcel_account_num"] = hit["parcel_account_num"]
                comps[idx]["parcel_county"] = hit["source_county"]
                matched_by_addr += 1
                continue
        # Fallback: lat/lon proximity to nearest parcel centroid.
        # Catches spacing/spelling drift like "Preston Crest" vs
        # "PRESTONCREST" where address keys diverge but the comp's
        # geocode lands inside the same parcel.
        comp = comps[idx]
        clat, clng = _comp_lat_lng(comp)
        if clat is None or clng is None or not geo_pool:
            continue
        best_dist = float("inf")
        best_geom = None
        best_account = None
        best_county = None
        for parcel_geom, account_num, county, plat, plng in geo_pool:
            d = _haversine_meters(clat, clng, plat, plng)
            if d < best_dist:
                best_dist = d
                best_geom = parcel_geom
                best_account = account_num
                best_county = county
        if best_dist <= PROXIMITY_THRESHOLD_M:
            comps[idx]["parcel_geom"] = best_geom
            comps[idx]["parcel_account_num"] = best_account
            comps[idx]["parcel_county"] = best_county
            matched_by_geo += 1

    logger.info(
        "match_comps_to_parcels: matched %d/%d (addr=%d, geo=%d) from %d parcels in bbox %s",
        matched_by_addr + matched_by_geo, len(comps), matched_by_addr, matched_by_geo,
        len(global_index), bbox,
    )
    return comps
