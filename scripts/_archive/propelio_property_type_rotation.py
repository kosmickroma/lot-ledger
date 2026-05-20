"""Propelio property-type rotation probe (READ-ONLY).

Tests whether rotating `propertyTypePresets` yields fresh, disjoint
comp slices AND whether it improves pending capture (KK's original
"missing pendings" concern).

Test plan
---------
Same lead/cma/months/range across all calls. Only `propertyTypePresets`
changes between calls. Compare:
  - Baseline:           []                   (no filter)
  - Single family:      ["SINGLE_FAMILY"]    (also tries fallback names)
  - Condo:              ["CONDO"]
  - Townhouse:          ["TOWNHOUSE"]
  - Multi-family:       ["MULTI_PLEX"]       (confirmed valid in XHR)

For each call, report:
  - HTTP status, cap sentinel
  - Returned `params` echo (does server acknowledge the preset?)
  - sales_returned
  - Status distribution (active / pending / sold / other)
  - Pending count specifically
  - Jaccard overlap with baseline (no-filter) call
  - Unique-to-this-preset comp count

Then a cumulative analysis: how many unique source_ids and how many
unique pendings do we get by UNIONing all the calls?

8 calls total (baseline + 4 confirmed presets + 3 candidate names for
single family). 3s spacing. No DB writes.
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter
from typing import Any

import requests

from api.propelio.scraper import (
    PROPELIO_API_BASE,
    PropelioScraperError,
    login_propelio,
)

LEAD_ID = "8347103"
CMA_ID = "1755174"
DELAY_S = 3.0

# Filter constants (same across all calls)
MONTHS = 24
RANGE_MI = "1"

# Property-type presets to test. We KNOW MULTI_PLEX works (seen in XHR).
# For single family we try several candidate names since we haven't
# confirmed the exact string Propelio uses.
PROBES: list[tuple[str, list[str]]] = [
    ("baseline (no property type filter)", []),
    ("SINGLE_FAMILY (canonical guess)", ["SINGLE_FAMILY"]),
    ("SFR (short form)", ["SFR"]),
    ("SINGLE_FAMILY_RESIDENCE (verbose)", ["SINGLE_FAMILY_RESIDENCE"]),
    ("CONDO", ["CONDO"]),
    ("TOWNHOUSE", ["TOWNHOUSE"]),
    ("MULTI_PLEX (confirmed valid)", ["MULTI_PLEX"]),
]

logging.basicConfig(level=logging.WARNING)


def _post(session: requests.Session, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{PROPELIO_API_BASE}/legacy/cma/search/{LEAD_ID}/{CMA_ID}"
    response = session.post(url, json=body, timeout=90)
    if response.status_code >= 400:
        raise PropelioScraperError(
            f"POST body={body!r} -> HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    raw = response.json()
    return raw[0] if isinstance(raw, list) and raw else raw


def _summarize(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    sales = data.get("sales") or []
    ids: set[str] = set()
    pendings: set[str] = set()
    status_counts: Counter[str] = Counter()
    property_categories: Counter[str] = Counter()
    for s in sales:
        sid = s.get("source_id")
        if sid is None:
            continue
        sid = str(sid)
        ids.add(sid)
        status = str(s.get("status") or "").lower()
        status_counts[status] += 1
        property_categories[str(s.get("property_category") or "unknown")] += 1
        if status == "pending":
            pendings.add(sid)
    return {
        "echoed_params": payload.get("params"),
        "sales_count": payload.get("sales_count"),
        "sales_returned": len(sales),
        "source_ids": ids,
        "pendings": pendings,
        "status_counts": status_counts,
        "property_categories": property_categories,
    }


def main() -> int:
    print("=" * 78)
    print("Propelio property-type rotation probe (READ-ONLY)")
    print(f"  lead_id={LEAD_ID}  cma_id={CMA_ID}")
    print(f"  fixed filters: months={MONTHS}, range={RANGE_MI}")
    print(f"  rotating: propertyTypePresets across {len(PROBES)} values")
    print(f"  delay: {DELAY_S}s between calls")
    print("=" * 78)

    client = login_propelio()
    session = client.session

    results: dict[str, dict[str, Any]] = {}
    for idx, (label, presets) in enumerate(PROBES):
        if idx > 0:
            time.sleep(DELAY_S)
        body = {
            "months": MONTHS,
            "range": RANGE_MI,
            "propertyTypePresets": presets,
        }
        s = _summarize(_post(session, body))
        results[label] = s
        echoed_pp = (s.get("echoed_params") or {}).get(
            "propertyTypePresets"
        )
        ack = (
            "ACK" if echoed_pp == presets else f"STRIPPED (echoed={echoed_pp})"
        )
        pcats = ", ".join(
            f"{k}={v}"
            for k, v in s["property_categories"].most_common(3)
        )
        print(f"\n[{label}]")
        print(f"  preset_sent={presets}  server_{ack}")
        print(
            f"  sales_returned={s['sales_returned']}  "
            f"sales_count={s['sales_count']}  "
            f"pendings={len(s['pendings'])}"
        )
        print(f"  status_distribution={dict(s['status_counts'])}")
        print(f"  top_property_categories: {pcats}")

    # --- Overlap analysis vs baseline ----------------------------------------
    baseline_ids = results["baseline (no property type filter)"]["source_ids"]
    baseline_pendings = results["baseline (no property type filter)"]["pendings"]

    print("\n" + "=" * 78)
    print("OVERLAP vs BASELINE (no property type filter)")
    print("=" * 78)
    for label, s in results.items():
        if label.startswith("baseline"):
            continue
        ids = s["source_ids"]
        overlap = len(ids & baseline_ids)
        unique_to_this = len(ids - baseline_ids)
        new_pendings = len(s["pendings"] - baseline_pendings)
        jac = len(ids & baseline_ids) / max(len(ids | baseline_ids), 1)
        print(
            f"  {label[:50]:50s}  "
            f"jaccard={jac:.3f}  "
            f"unique_vs_baseline={unique_to_this:3d}  "
            f"new_pendings={new_pendings}"
        )

    # --- Cumulative coverage -------------------------------------------------
    all_ids: set[str] = set()
    all_pendings: set[str] = set()
    for s in results.values():
        all_ids |= s["source_ids"]
        all_pendings |= s["pendings"]
    print("\n" + "=" * 78)
    print("CUMULATIVE COVERAGE across all 7 probes (1mi/24mo, type-rotated)")
    print("=" * 78)
    print(
        f"  total unique comps: {len(all_ids)}  "
        f"(vs {len(baseline_ids)} from baseline alone)"
    )
    print(
        f"  total unique pendings: {len(all_pendings)}  "
        f"(vs {len(baseline_pendings)} from baseline alone)"
    )
    lift = (
        ((len(all_ids) - len(baseline_ids)) / len(baseline_ids) * 100)
        if baseline_ids
        else 0
    )
    pending_lift = (
        ((len(all_pendings) - len(baseline_pendings))
         / max(len(baseline_pendings), 1) * 100)
        if baseline_pendings
        else 0
    )
    print(f"  comp lift from rotation:    +{lift:.0f}%")
    print(f"  pending lift from rotation: +{pending_lift:.0f}%")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PropelioScraperError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        sys.exit(1)
