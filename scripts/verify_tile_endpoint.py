#!/usr/bin/env python3
"""scripts/verify_tile_endpoint.py

G1 gate (docs/CODER_SPEC_TILE_ENDPOINT_2026-08-05.md) — the load-bearing
byte-compare. Picks >=25 tiles across zooms 12-16 (dense Dallas coverage +
points outside the archive's bbox, to exercise the empty-tile path) and
asserts the NEW endpoint's decoded payload is byte-identical to the OLD
path's, after normalizing compression.

OLD path = direct ranged reads via the official pmtiles.reader.Reader
against the same upstream archive URL (_TILES_UPSTREAM_URL) that the
existing /tiles/parcels.pmtiles proxy (api/main.py:tiles_proxy) forwards
Range requests to verbatim — the proxy performs zero byte transformation
(see its source: it streams GCS's response back untouched), so exercising
the upstream URL directly is equivalent to exercising it through the proxy.

NEW path = api.main._pmtiles_lookup_tile(z, x, y), the exact function the
new GET /tiles/parcels/{z}/{x}/{y}.mvt endpoint calls. Called in-process
(no HTTP server needed) so this script has zero dependency on the app's
DB/session config — the tile endpoint touches neither.

Run: source .venv/bin/activate && python scripts/verify_tile_endpoint.py
"""
from __future__ import annotations

import asyncio
import gzip
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from pmtiles.reader import Reader

import api.main as main

# Dense Dallas anchor points — cover downtown, uptown, Oak Cliff, Richardson,
# Arlington, so the sample includes both dense-parcel and sparser tiles.
DALLAS_POINTS = [
    (32.7767, -96.7970),  # downtown
    (32.8140, -96.7712),  # uptown / Knox-Henderson
    (32.7357, -96.8235),  # Oak Cliff
    (32.9370, -96.6989),  # Richardson
    (32.6668, -97.0713),  # Arlington
]
# Well outside the archive's bbox (min/max lon/lat from the header) — these
# must resolve to no tile in the archive, exercising the empty-tile path.
OUTSIDE_POINTS = [
    (40.7128, -74.0060),  # NYC
    (29.7604, -95.3698),  # Houston
]
ZOOMS = [12, 13, 14, 15, 16]


def deg2tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    lat_rad = math.radians(lat)
    n = 2.0**zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int(
        (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    )
    return x, y


def build_sample() -> list[tuple[int, int, int]]:
    sample: set[tuple[int, int, int]] = set()
    for z in ZOOMS:
        for lat, lon in DALLAS_POINTS:
            x, y = deg2tile(lat, lon, z)
            sample.add((z, x, y))
        for lat, lon in OUTSIDE_POINTS:
            x, y = deg2tile(lat, lon, z)
            sample.add((z, x, y))
    return sorted(sample)


def normalize(raw: bytes | None, compression) -> bytes | None:
    if raw is None:
        return None
    if compression.name == "GZIP":
        return gzip.decompress(raw)
    if compression.name == "NONE":
        return raw
    raise ValueError(f"Unhandled compression in verify script: {compression}")


async def main_async() -> None:
    sample = build_sample()
    print(f"Sample size: {len(sample)} distinct (z,x,y) tuples across zooms {ZOOMS}\n")

    old_client = httpx.Client(timeout=30.0)

    def old_get_bytes(offset: int, length: int) -> bytes:
        r = old_client.get(
            main._TILES_UPSTREAM_URL,
            headers={"Range": f"bytes={offset}-{offset + length - 1}"},
        )
        r.raise_for_status()
        return r.content

    old_reader = Reader(old_get_bytes)
    archive_compression = old_reader.header()["tile_compression"]

    mismatches = []
    empty_count = 0
    checked = 0
    for z, x, y in sample:
        old_raw = old_reader.get(z, x, y)
        new_raw, new_compression = await main._pmtiles_lookup_tile(z, x, y)

        if new_raw is not None and new_compression != archive_compression:
            mismatches.append((z, x, y, "compression-mismatch"))

        old_decoded = normalize(old_raw, archive_compression)
        new_decoded = normalize(new_raw, new_compression)
        checked += 1

        if old_decoded is None and new_decoded is None:
            empty_count += 1
            status = "OK(empty)"
        elif old_decoded == new_decoded:
            status = "OK"
        else:
            status = "MISMATCH"
            mismatches.append((z, x, y, "byte-mismatch"))

        old_len = len(old_raw) if old_raw else 0
        new_len = len(new_raw) if new_raw else 0
        print(f"{status:10s} z={z:2d} x={x:6d} y={y:6d}  old={old_len:7d}B new={new_len:7d}B")

    old_client.close()
    if main._TILE_HTTP_CLIENT is not None:
        await main._TILE_HTTP_CLIENT.aclose()

    print()
    print(
        f"Checked {checked} tiles ({empty_count} empty, {checked - empty_count} with data). "
        f"Mismatches: {len(mismatches)}"
    )
    if mismatches:
        print("FAILED:", mismatches)
        sys.exit(1)
    print("PASS — byte-identical (after compression normalization) on every sampled tile.")


if __name__ == "__main__":
    asyncio.run(main_async())
