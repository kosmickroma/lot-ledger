"""Propelio pending-capture probe (READ-ONLY).

Question being answered
-----------------------
Does rotating `months` (the time window) materially improve pending capture?
Hypothesis: months=24 is dominated by recent SOLD listings (the ranker
favors recency × relevance, and 2 years of closed sales swamp the
in-flight pendings). Tighter windows (months=1, 2, 3) should surface
proportionally more pendings because there ARE fewer sold deals to
compete with them.

Test plan against KK's real test subject (2451 Crest Ridge Dr, Dallas
TX, lead_id=8345711). We use the existing CMA on this lead — no
fresh credit burn, no new lead created.

1. GET /legacy/cma/8345711  →  extract cma_id from response.id
2. Run 7 POST /search calls with these param sets (range=1, preset=
   SINGLE_FAMILY for all):
       months: 1, 2, 3, 6, 12, 24, plus a baseline (no preset, months=24)
3. For each: status distribution, pending count, fresh source_ids vs
   prior calls.
4. Cumulative: how many unique pendings did we catch across rotation
   vs from the single baseline call?

8 calls total. 3s spacing. Read-only.
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter
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

# Fixed for all POSTs
RANGE_MI = "1"
PRESET_SFR = ["SINGLE_FAMILY"]

# (label, months, propertyTypePresets)
ROTATIONS: list[tuple[str, int, list[str]]] = [
    ("baseline (24mo, no type filter)", 24, []),
    ("months=1, SFR", 1, PRESET_SFR),
    ("months=2, SFR", 2, PRESET_SFR),
    ("months=3, SFR", 3, PRESET_SFR),
    ("months=6, SFR", 6, PRESET_SFR),
    ("months=12, SFR", 12, PRESET_SFR),
    ("months=24, SFR", 24, PRESET_SFR),
]

logging.basicConfig(level=logging.WARNING)


def _get_baseline(session: requests.Session) -> dict[str, Any]:
    url = CMA_URL_TEMPLATE.format(lead_id=LEAD_ID)
    response = session.get(url, timeout=60)
    if response.status_code >= 400:
        raise PropelioScraperError(
            f"GET {url} -> HTTP {response.status_code}: {response.text[:200]}"
        )
    raw = response.json()
    return raw[0] if isinstance(raw, list) and raw else raw


def _post_search(
    session: requests.Session, cma_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    url = f"{PROPELIO_API_BASE}/legacy/cma/search/{LEAD_ID}/{cma_id}"
    response = session.post(url, json=body, timeout=90)
    if response.status_code >= 400:
        raise PropelioScraperError(
            f"POST -> HTTP {response.status_code}: {response.text[:200]}"
        )
    raw = response.json()
    return raw[0] if isinstance(raw, list) and raw else raw


def _summarize(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    sales = data.get("sales") or []
    ids: set[str] = set()
    pending_ids: set[str] = set()
    status_counts: Counter[str] = Counter()
    for s in sales:
        sid = s.get("source_id")
        if sid is None:
            continue
        sid = str(sid)
        ids.add(sid)
        status = str(s.get("status") or "").lower()
        status_counts[status] += 1
        if status == "pending":
            pending_ids.add(sid)
    return {
        "sales_count": payload.get("sales_count"),
        "sales_returned": len(sales),
        "source_ids": ids,
        "pending_ids": pending_ids,
        "status_counts": status_counts,
    }


def main() -> int:
    print("=" * 78)
    print("Propelio pending-capture probe — months rotation")
    print(f"  lead_id={LEAD_ID}  (2451 Crest Ridge Dr, Dallas TX)")
    print(f"  fixed: range={RANGE_MI}mi")
    print("=" * 78)

    client = login_propelio()
    session = client.session

    # Step 1: get cma_id from existing CMA
    print("\nGET /legacy/cma/{lead_id} → extracting cma_id ...")
    baseline_cma = _get_baseline(session)
    cma_id = baseline_cma.get("id") or baseline_cma.get("cma_id")
    if not cma_id:
        print(
            f"ABORT: no cma_id in GET response. Top-level keys: "
            f"{list(baseline_cma.keys())}"
        )
        return 1
    print(f"  cma_id={cma_id}")
    baseline_params = baseline_cma.get("params")
    print(f"  baseline echoed params: {baseline_params}")

    # Step 2: run rotation
    results: list[tuple[str, dict[str, Any]]] = []
    for idx, (label, months, presets) in enumerate(ROTATIONS):
        if idx > 0:
            time.sleep(DELAY_S)
        body = {
            "months": months,
            "range": RANGE_MI,
            "propertyTypePresets": presets,
        }
        s = _summarize(_post_search(session, str(cma_id), body))
        results.append((label, s))
        pending_share = (
            len(s["pending_ids"]) / s["sales_returned"] * 100
            if s["sales_returned"]
            else 0
        )
        print(f"\n[{label}]")
        print(
            f"  returned={s['sales_returned']:3d}  "
            f"cap_sentinel={s['sales_count']}  "
            f"pendings={len(s['pending_ids']):2d}  "
            f"pending_share={pending_share:.1f}%"
        )
        # Compact status breakdown
        sc = s["status_counts"]
        print(
            f"  statuses: "
            f"sold={sc.get('sold', 0)}  "
            f"active={sc.get('active', 0)}  "
            f"pending={sc.get('pending', 0)}  "
            f"other={sum(v for k, v in sc.items() if k not in ('sold', 'active', 'pending'))}"
        )

    # Step 3: cumulative analysis
    print("\n" + "=" * 78)
    print("CUMULATIVE COVERAGE (across all 7 rotations)")
    print("=" * 78)
    all_ids: set[str] = set()
    all_pendings: set[str] = set()
    for label, s in results:
        all_ids |= s["source_ids"]
        all_pendings |= s["pending_ids"]
    baseline_pendings = results[0][1]["pending_ids"]
    baseline_comps = results[0][1]["source_ids"]
    print(
        f"  total unique comps:    {len(all_ids)}  "
        f"(vs {len(baseline_comps)} from baseline single call)"
    )
    print(
        f"  total unique pendings: {len(all_pendings)}  "
        f"(vs {len(baseline_pendings)} from baseline single call)"
    )

    # Where did each pending come from?
    print("\nPendings discovered by rotation pass (excluding baseline):")
    seen_pendings: set[str] = set(baseline_pendings)
    for label, s in results[1:]:
        new = s["pending_ids"] - seen_pendings
        if new:
            print(f"  + {len(new):2d} new pending(s) from [{label}]")
            seen_pendings |= new
        else:
            print(f"    0 new pendings from [{label}]")
    print(
        f"\n  net new pendings from months-rotation: "
        f"{len(all_pendings - baseline_pendings)}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PropelioScraperError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        sys.exit(1)
