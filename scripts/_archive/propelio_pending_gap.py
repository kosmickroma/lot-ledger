"""Propelio pending-gap probe for 2451 Crest Ridge Dr (READ-ONLY).

KK's screenshots show ~6-8 pendings (blue) in Propelio's UI for the same
drawn polygon where LL shows only 2. Two hypotheses to test:

H1: Aggressive filter rotation (months × range) recovers more pendings
    than a single months=1/range=1 call.

H2: Propelio's server uses the stored geojson polygon as the search
    constraint. POSTing /search with that geojson explicitly may unlock
    different/more results (especially pendings near the polygon edge
    that fall outside the circular range).

Plan
----
1. GET /legacy/cma/8345711  →  extract cma_id AND the stored geojson.
2. Rotation sweep (12 calls, range×months grid): record every pending
   source_id, address, list price, lat/lon.
3. Geojson experiment (2 calls):
     - POST /search with {months:1, range:1}              (no geojson)
     - POST /search with {months:1, range:1, geojson:X}   (X = stored polygon)
   Compare result sets — does adding geojson change the response?
4. Report: total unique pendings captured, where each pending sits
   geographically, and whether geojson changed anything.

Read-only. Reuses cma_id 1754163 (already created). 14 calls total,
3s spacing, ~45s wall time.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

import requests

from api.propelio.scraper import (
    CMA_URL_TEMPLATE,
    PROPELIO_API_BASE,
    PropelioScraperError,
    login_propelio,
)

LEAD_ID = "8345711"  # 2451 Crest Ridge Dr, Dallas TX
DELAY_S = 3.0

# Rotation grid for H1
ROTATIONS_H1: list[tuple[int, str]] = [
    (1, "0.5"), (1, "1"), (1, "2"), (1, "5"),
    (2, "0.5"), (2, "1"), (2, "2"),
    (3, "0.5"), (3, "1"), (3, "2"),
    (6, "1"), (12, "1"),
]

logging.basicConfig(level=logging.WARNING)


def _get_baseline(session: requests.Session) -> dict[str, Any]:
    url = CMA_URL_TEMPLATE.format(lead_id=LEAD_ID)
    r = session.get(url, timeout=60)
    if r.status_code >= 400:
        raise PropelioScraperError(
            f"GET {url} -> HTTP {r.status_code}: {r.text[:200]}"
        )
    raw = r.json()
    return raw[0] if isinstance(raw, list) and raw else raw


def _post(
    session: requests.Session, cma_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    url = f"{PROPELIO_API_BASE}/legacy/cma/search/{LEAD_ID}/{cma_id}"
    r = session.post(url, json=body, timeout=90)
    if r.status_code >= 400:
        raise PropelioScraperError(
            f"POST body={body!r} -> HTTP {r.status_code}: {r.text[:200]}"
        )
    raw = r.json()
    return raw[0] if isinstance(raw, list) and raw else raw


def _pending_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull pending sales with their key fields for reporting."""
    sales = (payload.get("data") or {}).get("sales") or []
    out = []
    for s in sales:
        if str(s.get("status") or "").lower() != "pending":
            continue
        addr = s.get("address") or {}
        out.append({
            "source_id": str(s.get("source_id") or ""),
            "address": addr.get("line1") or "",
            "city": addr.get("city") or "",
            "lat": addr.get("lat"),
            "lon": addr.get("lon"),
            "list_price": s.get("list_price"),
            "dom": s.get("dom") or s.get("calculated_dom"),
        })
    return out


def main() -> int:
    print("=" * 78)
    print("Propelio pending-gap probe — lead 8345711 (2451 Crest Ridge Dr)")
    print("=" * 78)

    client = login_propelio()
    session = client.session

    # ---- Step 1: get cma_id + stored geojson --------------------------------
    baseline = _get_baseline(session)
    cma_id = baseline.get("id") or baseline.get("cma_id")
    stored_params = baseline.get("params") or {}
    stored_geojson = stored_params.get("geojson")
    print(f"\ncma_id = {cma_id}")
    print(f"stored params keys = {list(stored_params.keys())}")
    if stored_geojson:
        feats = stored_geojson.get("features") or []
        coords_count = 0
        for f in feats:
            coords = (f.get("geometry") or {}).get("coordinates") or []
            for ring in coords:
                coords_count += len(ring) if isinstance(ring, list) else 0
        print(
            f"stored geojson: {len(feats)} feature(s), "
            f"{coords_count} polygon vertices"
        )
    else:
        print("stored geojson: NONE")

    # ---- Step 2: H1 rotation sweep ------------------------------------------
    print("\n" + "=" * 78)
    print("H1: filter rotation — months × range grid")
    print("=" * 78)
    all_pendings: dict[str, dict[str, Any]] = {}  # source_id -> record
    pending_by_combo: dict[str, set[str]] = {}
    for idx, (months, range_mi) in enumerate(ROTATIONS_H1):
        if idx > 0:
            time.sleep(DELAY_S)
        body = {"months": months, "range": range_mi}
        payload = _post(session, str(cma_id), body)
        pendings = _pending_records(payload)
        combo_label = f"m{months}/r{range_mi}"
        pending_by_combo[combo_label] = {p["source_id"] for p in pendings}
        for p in pendings:
            sid = p["source_id"]
            if sid not in all_pendings:
                all_pendings[sid] = p
        sales_count = payload.get("sales_count")
        print(
            f"  {combo_label:12s}  "
            f"sales_count={sales_count:>4}  "
            f"pendings={len(pendings):2d}  "
            f"unique_so_far={len(all_pendings):2d}"
        )

    # ---- Step 3: H2 geojson experiment --------------------------------------
    print("\n" + "=" * 78)
    print("H2: geojson polygon experiment")
    print("=" * 78)
    if stored_geojson is None:
        print("SKIPPED — no stored geojson to send")
    else:
        time.sleep(DELAY_S)
        baseline_body = {"months": 1, "range": "1"}
        baseline_payload = _post(session, str(cma_id), baseline_body)
        baseline_ids = {
            str(s.get("source_id"))
            for s in (baseline_payload.get("data") or {}).get("sales") or []
            if s.get("source_id") is not None
        }
        baseline_pendings = {p["source_id"] for p in _pending_records(baseline_payload)}
        baseline_echo_geojson = (baseline_payload.get("params") or {}).get("geojson")
        print(
            f"\n  [no geojson]                "
            f"sales={len(baseline_ids):3d}  "
            f"pendings={len(baseline_pendings):2d}  "
            f"server_echoed_geojson={'yes' if baseline_echo_geojson else 'no'}"
        )

        time.sleep(DELAY_S)
        geojson_body = {"months": 1, "range": "1", "geojson": stored_geojson}
        geojson_payload = _post(session, str(cma_id), geojson_body)
        geojson_ids = {
            str(s.get("source_id"))
            for s in (geojson_payload.get("data") or {}).get("sales") or []
            if s.get("source_id") is not None
        }
        geojson_pendings = {p["source_id"] for p in _pending_records(geojson_payload)}
        geojson_echo_geojson = (geojson_payload.get("params") or {}).get("geojson")
        print(
            f"  [with stored geojson]       "
            f"sales={len(geojson_ids):3d}  "
            f"pendings={len(geojson_pendings):2d}  "
            f"server_echoed_geojson={'yes' if geojson_echo_geojson else 'no'}"
        )

        # also add these pendings to the cumulative set
        for p in _pending_records(geojson_payload):
            if p["source_id"] not in all_pendings:
                all_pendings[p["source_id"]] = p

        jac = (
            len(baseline_ids & geojson_ids) / max(len(baseline_ids | geojson_ids), 1)
        )
        print(f"  jaccard(no_geojson vs with_geojson) = {jac:.3f}")
        if jac >= 0.95:
            print(
                "  -> sending geojson made ~no difference. Server already\n"
                "     applies the stored polygon, or geojson is ignored.\n"
                "     We can't use it as a custom-area override from this call."
            )
        elif jac < 0.6:
            print(
                "  -> sending geojson MEANINGFULLY changed the result set.\n"
                "     The polygon param works -> we can send LL's drawn\n"
                "     polygon for true area-bounded searches."
            )
        else:
            print("  -> partial effect, investigate further.")

    # ---- Step 4: pending dossier --------------------------------------------
    print("\n" + "=" * 78)
    print(f"PENDING DOSSIER — {len(all_pendings)} unique pendings discovered")
    print("=" * 78)
    for sid, rec in sorted(all_pendings.items()):
        price = (
            f"${int(rec['list_price']):,}"
            if isinstance(rec.get("list_price"), (int, float)) and rec["list_price"]
            else "n/a"
        )
        lat = rec.get("lat")
        lon = rec.get("lon")
        coord_str = f"({lat:.4f},{lon:.4f})" if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) else "(no coords)"
        print(
            f"  {sid:>12s}  {rec['address']:30s}  "
            f"price={price:>10s}  dom={rec.get('dom', '?')!s:>3s}  {coord_str}"
        )

    # ---- Step 5: which combo found each pending? ----------------------------
    print("\n" + "=" * 78)
    print("Coverage by filter combo (which combos surfaced each pending)")
    print("=" * 78)
    for sid in sorted(all_pendings.keys()):
        combos = [c for c, ids in pending_by_combo.items() if sid in ids]
        addr = all_pendings[sid].get("address", "")[:24]
        print(f"  {sid:>12s} {addr:24s}  found_in: {','.join(combos) if combos else '(only geojson)'}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PropelioScraperError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        sys.exit(1)
