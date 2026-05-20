"""Propelio rerun-yield curve probe (READ-ONLY).

Tests the ranker-nondeterminism hypothesis directly: fire the literal
same query body 5 times in a row and measure how many unique comps
we accumulate after each call.

If the API is deterministic (KK's gut), all 5 calls return the same
100 comps -> cumulative stays flat at 100.

If the ranker samples randomly from a larger pool (what probe v2
suggested), cumulative climbs and asymptotes near the true pool size.

5 calls, 2.5s spacing, ~15s total, no DB writes.
"""

from __future__ import annotations

import logging
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
N_REPS = 5
DELAY_S = 2.5
BODY = {"months": 24, "range": "10"}

logging.basicConfig(level=logging.WARNING)


def _post(session: requests.Session, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{PROPELIO_API_BASE}/legacy/cma/search/{LEAD_ID}/{CMA_ID}"
    response = session.post(url, json=body, timeout=90)
    if response.status_code >= 400:
        raise PropelioScraperError(
            f"POST -> HTTP {response.status_code}: {response.text[:200]}"
        )
    raw = response.json()
    return raw[0] if isinstance(raw, list) and raw else raw


def _ids(payload: dict[str, Any]) -> set[str]:
    sales = (payload.get("data") or {}).get("sales") or []
    return {
        str(s["source_id"]) for s in sales if s.get("source_id") is not None
    }


def main() -> int:
    print("=" * 70)
    print(f"Propelio rerun-yield curve — {N_REPS} identical calls")
    print(f"  body={BODY}")
    print(f"  lead_id={LEAD_ID}  cma_id={CMA_ID}  delay={DELAY_S}s")
    print("=" * 70)

    client = login_propelio()
    session = client.session

    cumulative: set[str] = set()
    per_call: list[set[str]] = []

    for i in range(1, N_REPS + 1):
        if i > 1:
            time.sleep(DELAY_S)
        payload = _post(session, BODY)
        ids = _ids(payload)
        before = len(cumulative)
        cumulative |= ids
        added = len(cumulative) - before
        per_call.append(ids)
        print(
            f"  call {i}: returned={len(ids):3d}  new_to_set={added:3d}  "
            f"cumulative_unique={len(cumulative):3d}"
        )

    print()
    print("Pairwise jaccard between calls (1.0 = identical, 0.0 = disjoint):")
    for i in range(N_REPS):
        for j in range(i + 1, N_REPS):
            a, b = per_call[i], per_call[j]
            jac = len(a & b) / max(len(a | b), 1)
            print(f"  call {i+1} vs call {j+1}: jaccard={jac:.3f}  "
                  f"overlap={len(a & b)}/{len(a | b)}")

    print()
    print("=" * 70)
    print(f"VERDICT: {N_REPS} identical calls yielded {len(cumulative)} "
          f"unique comps (vs 100 from a single call)")
    print("=" * 70)
    multiplier = len(cumulative) / 100.0
    print(f"  effective multiplier: {multiplier:.2f}x")
    if multiplier > 1.5:
        print("  -> ranker is nondeterministic. Reruns are exploitable.")
    elif multiplier > 1.1:
        print("  -> some randomness, modest yield. Worth 2-3 reruns.")
    else:
        print("  -> essentially deterministic. Reruns won't help.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PropelioScraperError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        sys.exit(1)
