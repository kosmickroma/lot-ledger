# api/redfin.py
#
# Async Redfin listing pull for a bounding box using grid-cell requests.
# Collects active listing addresses and normalizes them for DCAD matching.
#
# Connects to:
#   api/main.py  - called by analyze endpoint (Phase 4)
#   api/dcad.py  - no direct import; output addresses are used with ON_REDFIN logic via main

from __future__ import annotations

import asyncio
import io
from typing import Iterable

import httpx
import numpy as np
import pandas as pd


REDFIN_CITY_URL = "https://www.redfin.com/city/30794/TX/Dallas"
REDFIN_GIS_CSV_URL = "https://www.redfin.com/stingray/api/gis-csv"
DEFAULT_CELL_SIZE = 0.003
DEFAULT_TIMEOUT = 20.0
DEFAULT_CONCURRENCY = 20

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


def _normalize_addresses(df: pd.DataFrame) -> set[str]:
    address_col = "ADDRESS" if "ADDRESS" in df.columns else "Address" if "Address" in df.columns else None
    if not address_col:
        return set()

    normalized = (
        df[address_col]
        .dropna()
        .astype(str)
        .str.upper()
        .str.strip()
        .str.split("#")
        .str[0]
        .str.strip()
    )
    return set(normalized[normalized != ""])


async def _fetch_cell(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    min_lng: float,
    min_lat: float,
    max_lng: float,
    max_lat: float,
) -> set[str]:
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
        return set()

    dataframe = pd.read_csv(io.StringIO(response.text))
    if dataframe.empty:
        return set()

    return _normalize_addresses(dataframe)


async def pull_grid(
    min_lng: float,
    min_lat: float,
    max_lng: float,
    max_lat: float,
    cell_size: float = DEFAULT_CELL_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> set[str]:
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

        tasks: list[asyncio.Task[set[str]]] = []
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
            return set()

        results: Iterable[set[str]] = await asyncio.gather(*tasks, return_exceptions=False)

    combined: set[str] = set()
    for address_set in results:
        combined.update(address_set)
    return combined
