"""Rerun-yield curve at KK's proposed tight filter (READ-ONLY).

Same shape as propelio_rerun_curve.py but with the filter combo KK
wants to test for a real LL button: 1 mile radius, 2 year window.

If the API hits the 101-cap at this radius, reruns should add comps
(same pattern we saw at range=10). If the API returns <100 because
the actual pool is smaller than the cap, reruns will return ~identical
sets and we've learned that tight filters don't benefit from reruns.

Either result is useful info for the deep-pull engine redesign.
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
N_REPS = 4
DELAY_S = 3.0
BODY = {"months": 24, "range": "1"}  # KK's proposed filter: 1mi / 2yr

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


def _ids_and_meta(payload: dict[str, Any]) -> tuple[set[str], int]:
    sales = (payload.get("data") or {}).get("sales") or []
    ids = {
        str(s["source_id"]) for s in sales if s.get("source_id") is not None
    }
    sales_count = payload.get("sales_count") or 0
    return ids, sales_count


def main() -> int:
    print("=" * 70)
    print(f"Propelio tight-filter rerun curve — {N_REPS} identical calls")
    print(f"  body={BODY}   (1mi radius, 2-year window)")
    print(f"  lead_id={LEAD_ID}  cma_id={CMA_ID}  delay={DELAY_S}s")
    print("=" * 70)

    client = login_propelio()
    session = client.session

    cumulative: set[str] = set()
    per_call: list[set[str]] = []
    sales_counts: list[int] = []

    for i in range(1, N_REPS + 1):
        if i > 1:
            time.sleep(DELAY_S)
        payload = _post(session, BODY)
        ids, sc = _ids_and_meta(payload)
        before = len(cumulative)
        cumulative |= ids
        added = len(cumulative) - before
        per_call.append(ids)
        sales_counts.append(sc)
        print(
            f"  call {i}: returned={len(ids):3d}  sales_count={sc:4d}  "
            f"new_to_set={added:3d}  cumulative={len(cumulative):3d}"
        )

    cap_hit = any(sc >= 101 for sc in sales_counts)
    print()
    print(f"Cap sentinel (sales_count>=101) hit? {cap_hit}")
    print()
    print("Pairwise jaccard:")
    for i in range(N_REPS):
        for j in range(i + 1, N_REPS):
            a, b = per_call[i], per_call[j]
            jac = len(a & b) / max(len(a | b), 1)
            print(f"  call {i+1} vs call {j+1}: jaccard={jac:.3f}  "
                  f"overlap={len(a & b)}/{len(a | b)}")

    print()
    print("=" * 70)
    one_call = len(per_call[0]) if per_call else 0
    multiplier = (len(cumulative) / one_call) if one_call else 0
    print(
        f"VERDICT: {N_REPS} reruns at 1mi/24mo -> "
        f"{len(cumulative)} unique vs {one_call} from a single call "
        f"({multiplier:.2f}x)"
    )
    print("=" * 70)

    if not cap_hit:
        print(
            "  -> Cap was NOT hit. Tight filter returned the full pool;\n"
            "     reruns can't add anything beyond natural variation.\n"
            "     IMPLICATION: rerun strategy doesn't help at tight radii.\n"
            "     Save reruns for wider passes where the cap fires."
        )
    elif multiplier > 1.3:
        print(
            "  -> Cap WAS hit AND reruns added substantial fresh comps.\n"
            "     IMPLICATION: rerun multiplier works at this tightness too.\n"
            "     Safe to wire a 1mi/24mo + Nx-rerun button."
        )
    else:
        print(
            "  -> Cap hit but rerun gain was modest. Investigate further\n"
            "     before committing to engine changes."
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PropelioScraperError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        sys.exit(1)
