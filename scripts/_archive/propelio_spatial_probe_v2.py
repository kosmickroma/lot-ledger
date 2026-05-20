"""Propelio cap-bypass probe v2 — controlled for nondeterminism (READ-ONLY).

v1 told us the cap is real but couldn't distinguish "param ignored" from
"ranker rolled different dice." v2 fixes that by:

1. Running the same query twice in a row to measure the nondeterminism
   floor (probes A and B).
2. Comparing **geographic centroids** (mean lat/lon of returned comps)
   in addition to source_id sets. If a center-override field works,
   the centroid will shift by miles. If it doesn't, the centroid stays
   put up to a small noise wobble. This is a much sharper signal than
   source_id overlap.
3. Probing FIVE candidate field names for re-centering with a 20-mile
   offset (far enough that working override would produce a completely
   disjoint result set).
4. Tightening the exclude test by feeding 50 known IDs and checking
   for any intersection with the response.

Total: 9 calls, ~25s, read-only, reuses authenticated session.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from typing import Any

import requests

from api.propelio.scraper import (
    PROPELIO_API_BASE,
    PropelioScraperError,
    login_propelio,
)

LEAD_ID = "8347103"
CMA_ID = "1755174"
INTER_CALL_DELAY_S = 2.5

# Offset 20 miles north of subject. 1 deg lat ≈ 69 mi, so 20 mi ≈ 0.29 deg.
# At range=10, a working override produces a result-set circle that is
# completely disjoint from the baseline circle -> centroid moves cleanly.
OFFSET_MILES_N = 20.0
DEG_LAT_PER_MILE = 1.0 / 69.0

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _centroid(sales: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """Mean (lat, lon) of comps with usable coordinates."""
    lats: list[float] = []
    lons: list[float] = []
    for s in sales:
        addr = s.get("address") or {}
        lat = addr.get("lat") or s.get("address_lat")
        lon = addr.get("lon") or s.get("address_lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            lats.append(float(lat))
            lons.append(float(lon))
    if not lats:
        return (None, None)
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _haversine_mi(
    a: tuple[float | None, float | None],
    b: tuple[float | None, float | None],
) -> float | None:
    """Great-circle distance in miles. None if either point is None."""
    lat1, lon1 = a
    lat2, lon2 = b
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _summarize(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    sales = data.get("sales") or []
    ids = sorted(
        str(s.get("source_id"))
        for s in sales
        if s.get("source_id") is not None
    )
    return {
        "echoed_params": payload.get("params"),
        "sales_count": payload.get("sales_count"),
        "sales_returned": len(sales),
        "source_ids": set(ids),
        "centroid": _centroid(sales),
    }


def _post(session: requests.Session, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{PROPELIO_API_BASE}/legacy/cma/search/{LEAD_ID}/{CMA_ID}"
    response = session.post(url, json=body, timeout=90)
    if response.status_code >= 400:
        raise PropelioScraperError(
            f"POST body={body!r} -> HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    raw = response.json()
    if isinstance(raw, list):
        return raw[0] if raw else {}
    return raw


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(len(a | b), 1)


def _report(name: str, s: dict[str, Any], anchor_centroid: tuple) -> None:
    lat, lon = s["centroid"]
    dist = _haversine_mi(s["centroid"], anchor_centroid)
    dist_str = f"{dist:.2f}mi" if dist is not None else "n/a"
    centroid_str = (
        f"({lat:.4f},{lon:.4f})" if lat is not None else "(none)"
    )
    print(f"\n[{name}]")
    print(f"  sales_returned={s['sales_returned']}  sales_count={s['sales_count']}")
    print(f"  centroid={centroid_str}  delta_from_baseline={dist_str}")
    # Strip giant exclude lists from echoed params before printing
    ep = dict(s.get("echoed_params") or {})
    for k in ("exclude_ids", "exclude_source_ids"):
        if k in ep and isinstance(ep[k], list) and len(ep[k]) > 3:
            ep[k] = f"[{len(ep[k])} ids…]"
    print(f"  echoed_params={json.dumps(ep)}")


def main() -> int:
    print("=" * 72)
    print("Propelio cap-bypass probe v2 (READ-ONLY)")
    print(f"  lead_id={LEAD_ID}  cma_id={CMA_ID}  delay={INTER_CALL_DELAY_S}s")
    print("=" * 72)

    client = login_propelio()
    session = client.session
    base_body = {"months": 24, "range": "10"}

    # --- A. Baseline ---------------------------------------------------------
    a = _summarize(_post(session, base_body))
    baseline_centroid = a["centroid"]
    print("\n[A. Baseline POST {months:24, range:10}]")
    print(
        f"  sales_returned={a['sales_returned']}  "
        f"sales_count={a['sales_count']}"
    )
    print(f"  centroid={baseline_centroid}")
    time.sleep(INTER_CALL_DELAY_S)

    # --- B. Same query again — noise floor -----------------------------------
    b = _summarize(_post(session, base_body))
    noise_jaccard = _jaccard(a["source_ids"], b["source_ids"])
    noise_centroid_drift = _haversine_mi(a["centroid"], b["centroid"])
    print(
        "\n[B. Same query again — NOISE FLOOR]"
    )
    print(
        f"  sales_returned={b['sales_returned']}  "
        f"jaccard_with_A={noise_jaccard:.3f}  "
        f"centroid_drift={noise_centroid_drift:.2f}mi"
        if noise_centroid_drift is not None
        else f"  jaccard_with_A={noise_jaccard:.3f}"
    )
    time.sleep(INTER_CALL_DELAY_S)

    # --- C-G. Five candidate re-center field names --------------------------
    # If subject centroid is ~33.02 lat, offset target is ~33.31 lat.
    if baseline_centroid[0] is None:
        print("ABORT: no centroid from baseline; can't run offset probes.")
        return 1

    target_lat = baseline_centroid[0] + (OFFSET_MILES_N * DEG_LAT_PER_MILE)
    target_lon = baseline_centroid[1]
    print(
        f"\nTarget offset: ({target_lat:.4f}, {target_lon:.4f}) — "
        f"{OFFSET_MILES_N}mi N of baseline centroid"
    )

    field_variants = [
        ("C. top-level lat/lon", {**base_body, "lat": target_lat, "lon": target_lon}),
        ("D. center: {lat,lon}", {**base_body, "center": {"lat": target_lat, "lon": target_lon}}),
        ("E. point: {lat,lon}", {**base_body, "point": {"lat": target_lat, "lon": target_lon}}),
        ("F. subject: {lat,lon}", {**base_body, "subject": {"lat": target_lat, "lon": target_lon}}),
        (
            "G. address: full address shape",
            {
                **base_body,
                "address": {
                    "lat": target_lat,
                    "lon": target_lon,
                    "line1": "100 Main St",
                    "city": "Celina",
                    "state": "TX",
                    "zip": "75009",
                },
            },
        ),
    ]

    results: dict[str, dict[str, Any]] = {"A": a, "B": b}
    for label, body in field_variants:
        time.sleep(INTER_CALL_DELAY_S)
        s = _summarize(_post(session, body))
        _report(label, s, baseline_centroid)
        results[label.split(".")[0]] = s

    # --- H. exclude_ids tight test ------------------------------------------
    time.sleep(INTER_CALL_DELAY_S)
    # Take 50 known IDs from baseline + B union
    known = sorted(a["source_ids"] | b["source_ids"])[:50]
    body_h = {**base_body, "exclude_ids": known, "exclude_source_ids": known}
    h = _summarize(_post(session, body_h))
    leaked = h["source_ids"] & set(known)
    print(f"\n[H. exclude_ids + exclude_source_ids (50 known IDs sent)]")
    print(
        f"  sales_returned={h['sales_returned']}  "
        f"of_the_50_sent_to_exclude_we_still_saw={len(leaked)}"
    )
    print(
        f"  verdict: {'EXCLUDE HONORED' if len(leaked) == 0 else 'EXCLUDE IGNORED'}"
    )

    # --- Summary table -------------------------------------------------------
    print("\n" + "=" * 72)
    print("SUMMARY — centroid drift in miles from baseline centroid")
    print("=" * 72)
    print(f"  noise floor (A vs B same query):  "
          f"jaccard={noise_jaccard:.3f}  "
          f"drift={noise_centroid_drift:.2f}mi"
          if noise_centroid_drift is not None
          else f"  noise floor jaccard={noise_jaccard:.3f}")
    for key in ("C", "D", "E", "F", "G"):
        if key not in results:
            continue
        r = results[key]
        j = _jaccard(a["source_ids"], r["source_ids"])
        d = _haversine_mi(baseline_centroid, r["centroid"])
        d_str = f"{d:.2f}mi" if d is not None else "n/a"
        print(f"  {key}: jaccard={j:.3f}  centroid_drift={d_str}")
    print()
    print(
        "If a row's drift is >>noise floor (e.g. >5mi when noise <1mi), that\n"
        "field is the re-center primitive. If all rows look like noise, no\n"
        "field-name probe worked — we'd need scratch leads."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PropelioScraperError as exc:
        print(f"\nABORT: {exc}", file=sys.stderr)
        sys.exit(1)
