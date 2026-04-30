# api/redfin.py
#
# Async Redfin listing pull for a bounding box using grid-cell requests.
# Returns a dict mapping normalized address → listing metadata (price, url, beds,
# baths, sqft, days_on_market) for DCAD matching and popup/CSV enrichment.
#
# Connects to:
#   api/main.py  - called by analyze endpoint; returns dict[str, dict]
#   api/dcad.py  - no direct import; listing dict passed via main.py to build_feature

from __future__ import annotations

import asyncio
import io
from typing import Any, Iterable

import httpx
import numpy as np
import pandas as pd


REDFIN_CITY_URL = "https://www.redfin.com/city/30794/TX/Dallas"
REDFIN_GIS_CSV_URL = "https://www.redfin.com/stingray/api/gis-csv"
DEFAULT_CELL_SIZE = 0.003
DEFAULT_TIMEOUT = 20.0
DEFAULT_CONCURRENCY = 20

# USPS suffix abbreviation table — maps abbreviated tokens to canonical full forms.
# Applied to every address token so e.g. "MEADOW CRST" expands to "MEADOW CREST".
_SUFFIX_MAP: dict[str, str] = {
    "ALY": "ALLEY",
    "AVE": "AVENUE",
    "BND": "BEND",
    "BLVD": "BOULEVARD",
    "CIR": "CIRCLE",
    "CMN": "COMMON",
    "CRES": "CRESCENT",
    "CRST": "CREST",
    "CT": "COURT",
    "CV": "COVE",
    "DR": "DRIVE",
    "ESTS": "ESTATES",
    "EXPY": "EXPRESSWAY",
    "FLDS": "FIELDS",
    "FWY": "FREEWAY",
    "GLN": "GLEN",
    "GRV": "GROVE",
    "HOLW": "HOLLOW",
    "HTS": "HEIGHTS",
    "HWY": "HIGHWAY",
    "KNL": "KNOLL",
    "KNLS": "KNOLLS",
    "LN": "LANE",
    "MDW": "MEADOW",
    "MDWS": "MEADOWS",
    "MNR": "MANOR",
    "PKWY": "PARKWAY",
    "PL": "PLACE",
    "RD": "ROAD",
    "RDG": "RIDGE",
    "SQ": "SQUARE",
    "ST": "STREET",
    "TER": "TERRACE",
    "TERR": "TERRACE",
    "TRL": "TRAIL",
    "VLY": "VALLEY",
    "VW": "VIEW",
    "XING": "CROSSING",
}

# After expanding abbreviations, strip the trailing token when it is a pure
# transport suffix (DRIVE, LANE, etc.) so that addresses like "MEADOW CREST DR"
# (DCAD) and "MEADOW CRST" (Redfin, no trailing type) resolve to the same key.
_STRIP_SUFFIXES: frozenset[str] = frozenset({
    "ALLEY", "AVENUE", "BOULEVARD", "CIRCLE", "COMMON", "COURT", "COVE",
    "CROSSING", "DRIVE", "EXPRESSWAY", "FREEWAY", "HIGHWAY", "LANE",
    "PARKWAY", "PLACE", "ROAD", "SQUARE", "STREET", "TERRACE", "TRAIL", "WAY",
})


def normalize_addr_key(raw: str) -> str:
    """Normalize a property address for cross-source (DCAD ↔ Redfin) matching.

    Steps: uppercase → strip unit suffix (#N) → expand each token via the USPS
    abbreviation table → drop trailing transport-type token (DRIVE, LANE …) so
    that "6006 MEADOW CREST DR" and "6006 MEADOW CRST" both resolve to the
    same key: "6006 MEADOW CREST".
    """
    text = str(raw or "").upper().strip().split("#")[0].strip()
    if not text:
        return ""
    tokens = [_SUFFIX_MAP.get(t, t) for t in text.split()]
    if tokens and tokens[-1] in _STRIP_SUFFIXES:
        tokens = tokens[:-1]
    return " ".join(tokens)


def _to_int(val: Any) -> int | None:
    """Convert a CSV value to int, returning None for blanks or non-numeric literals."""
    try:
        v = str(val or "").replace(",", "").strip()
        return int(float(v)) if v and v.lower() not in ("nan", "none", "") else None
    except (ValueError, TypeError):
        return None


REDFIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": REDFIN_CITY_URL,
}


def _build_cell_polygon(min_lng: float, min_lat: float, max_lng: float, max_lat: float) -> str:
    return (
        f"{min_lng} {min_lat},"
        f"{max_lng} {min_lat},"
        f"{max_lng} {max_lat},"
        f"{min_lng} {max_lat},"
        f"{min_lng} {min_lat}"
    )


def _normalize_addresses(df: pd.DataFrame) -> dict[str, dict]:
    """Return a mapping of normalized address → listing metadata dict."""
    address_col = "ADDRESS" if "ADDRESS" in df.columns else "Address" if "Address" in df.columns else None
    if not address_col:
        return {}

    # URL column header includes a long parenthetical — match by prefix.
    url_col = next((c for c in df.columns if c.startswith("URL")), None)

    result: dict[str, dict] = {}
    for _, row in df.iterrows():
        raw_addr = str(row.get(address_col) or "").strip()
        if not raw_addr or raw_addr.upper() in ("NAN", ""):
            continue
        normalized = normalize_addr_key(raw_addr)
        if not normalized:
            continue
        result[normalized] = {
            "price": _to_int(row.get("PRICE")),
            "url": str(row.get(url_col) or "").strip() or None if url_col else None,
            "beds": _to_int(row.get("BEDS")),
            "baths": _to_int(row.get("BATHS")),
            "sqft": _to_int(row.get("SQUARE FEET")),
            "dom": _to_int(row.get("DAYS ON MARKET")),
        }
    return result


async def _fetch_cell(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    min_lng: float,
    min_lat: float,
    max_lng: float,
    max_lat: float,
) -> dict[str, dict]:
    params = {
        "al": "1",
        "market": "dallas",
        "mpt": "99",
        "num_homes": "350",
        "sf": "1,2,3,5,6,7",
        "start": "0",
        "status": "1",
        "uipt": "1,2,3,4,5,6,7",
        "v": "8",
        "poly": _build_cell_polygon(min_lng, min_lat, max_lng, max_lat),
    }

    async with semaphore:
        response = await client.get(REDFIN_GIS_CSV_URL, params=params)

    if response.status_code != 200 or len(response.text) <= 200:
        return {}

    dataframe = pd.read_csv(io.StringIO(response.text))
    if dataframe.empty:
        return {}

    return _normalize_addresses(dataframe)


async def pull_grid(
    min_lng: float,
    min_lat: float,
    max_lng: float,
    max_lat: float,
    cell_size: float = DEFAULT_CELL_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict[str, dict]:
    """Fetch active Redfin listing addresses for all cells in the supplied bounding box."""
    lngs = np.arange(min_lng, max_lng, cell_size)
    lats = np.arange(min_lat, max_lat, cell_size)

    semaphore = asyncio.Semaphore(max(1, concurrency))
    timeout = httpx.Timeout(DEFAULT_TIMEOUT)

    async with httpx.AsyncClient(headers=REDFIN_HEADERS, timeout=timeout, follow_redirects=True) as client:
        # Prime cookies/session once, mirroring the reference script behavior.
        try:
            await client.get(REDFIN_CITY_URL)
        except httpx.HTTPError:
            pass

        tasks: list[asyncio.Task[dict[str, dict]]] = []
        for lng in lngs:
            for lat in lats:
                cell_max_lng = min(float(lng + cell_size), max_lng)
                cell_max_lat = min(float(lat + cell_size), max_lat)
                tasks.append(
                    asyncio.create_task(
                        _fetch_cell(
                            client,
                            semaphore,
                            float(lng),
                            float(lat),
                            cell_max_lng,
                            cell_max_lat,
                        )
                    )
                )

        if not tasks:
            return {}

        results: Iterable[dict[str, dict]] = await asyncio.gather(*tasks, return_exceptions=False)

    combined: dict[str, dict] = {}
    for addr_dict in results:
        combined.update(addr_dict)
    return combined
