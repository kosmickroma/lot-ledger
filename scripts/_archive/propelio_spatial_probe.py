"""Propelio CMA cap-bypass probe (READ-ONLY, NO DB WRITES).

Purpose
-------
Tests whether Propelio's ``POST /legacy/cma/search/{lead_id}/{cma_id}`` endpoint
accepts any undocumented parameters that would let us pull more than ~100
comps per call in dense areas. The endpoint is hard-capped at 100 sales + 100
leases per response, and filter rotation (months/range) hits diminishing
returns when 2000+ comps exist in the search radius.

Six probes, run sequentially with polite spacing:

    A. Baseline GET /legacy/cma/{lead_id}                  (current behavior)
    B. POST /search with the minimal body we already send  (current behavior)
    C. POST /search with `address` override -> can we re-center?
    D. POST /search with `offset: 100`     -> hidden pagination?
    E. POST /search with `exclude_ids: [...]` -> tell-me-what-I-don't-have?
    F. POST /search with `page: 2`         -> hidden pagination, variant?

For each probe we report HTTP status, returned `sales_count`, the server's
echo of `params`, and a hash of which `source_id`s we saw — so we can tell
whether each variant actually changed the result set.

Constraints
-----------
- READ-ONLY: no DB writes, no lead creation, no CMA creation. Reuses an
  existing lead_id/cma_id pair (8347103/1755174) that's already in
  Propelio's system, so this burns zero new credits.
- POLITE: 2.5s between calls, single thread, reuses one authenticated
  session (so cookies + TLS fingerprint stay stable -- matches what
  Propelio's web UI does).
- SAFE: prints to stdout only. If anything 4xx/5xx, abort the rest of the
  run.

Usage
-----
    python -m scripts.propelio_spatial_probe

Reads PROPELIO_USERNAME and PROPELIO_PASSWORD from .env via the existing
api.propelio.config module. No CLI flags -- this is a single-shot
investigation, not a production tool.
"""

from __future__ import annotations

import hashlib
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

# Hardcoded subject -- already exists in Propelio (no fresh credit burn).
# Captured from KK's dev-tools XHRs 2026-05-13.
LEAD_ID = "8347103"
CMA_ID = "1755174"

# Politeness: 2.5s between calls. Single user behaving normally.
INTER_CALL_DELAY_S = 2.5

# Compact logging -- one line per probe so the report is readable.
logging.basicConfig(
    level=logging.WARNING,  # silence the scraper's chatty INFO logs
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _summarize(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the bits we care about from a CMA response.

    Returns server-echoed params, sales/leases counts (the cap sentinel),
    and a stable fingerprint of the comp ids returned so we can compare
    probes for actual set differences.
    """
    data = payload.get("data") or {}
    sales = data.get("sales") or []
    source_ids = sorted(
        str(s.get("source_id"))
        for s in sales
        if s.get("source_id") is not None
    )
    fingerprint = hashlib.sha1(
        ",".join(source_ids).encode("utf-8")
    ).hexdigest()[:10]
    return {
        "echoed_params": payload.get("params"),
        "sales_count": payload.get("sales_count"),
        "leases_count": payload.get("leases_count"),
        "sales_returned": len(sales),
        "source_id_fingerprint": fingerprint,
        "first_source_id": source_ids[0] if source_ids else None,
        "last_source_id": source_ids[-1] if source_ids else None,
        "source_id_set": set(source_ids),
    }


def _report(name: str, summary: dict[str, Any], baseline: set[str] | None) -> None:
    """One-line(ish) report per probe."""
    overlap_info = ""
    if baseline is not None:
        their_set: set[str] = summary["source_id_set"]
        if their_set:
            in_baseline = len(their_set & baseline)
            not_in_baseline = len(their_set - baseline)
            overlap_info = (
                f"  overlap_with_baseline={in_baseline}/{len(their_set)}"
                f"  fresh_vs_baseline={not_in_baseline}"
            )
    print(f"\n[{name}]")
    print(f"  echoed_params={json.dumps(summary['echoed_params'])}")
    print(
        f"  sales_count={summary['sales_count']}"
        f"  leases_count={summary['leases_count']}"
        f"  sales_returned={summary['sales_returned']}"
    )
    print(
        f"  fingerprint={summary['source_id_fingerprint']}"
        f"  first={summary['first_source_id']}"
        f"  last={summary['last_source_id']}"
        f"{overlap_info}"
    )


def _post_search(
    session: requests.Session, body: dict[str, Any]
) -> dict[str, Any]:
    """POST /legacy/cma/search/{lead}/{cma} with an arbitrary body.

    Raises PropelioScraperError on any non-2xx so the run aborts before
    burning more calls into a possibly-flagging error path.
    """
    url = f"{PROPELIO_API_BASE}/legacy/cma/search/{LEAD_ID}/{CMA_ID}"
    response = session.post(url, json=body, timeout=90)
    if response.status_code >= 400:
        raise PropelioScraperError(
            f"POST {url} body={body!r} -> HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    raw = response.json()
    if isinstance(raw, list):
        return raw[0] if raw else {}
    return raw


def _get_baseline(session: requests.Session) -> dict[str, Any]:
    """GET /legacy/cma/{lead_id} -- the unfiltered baseline."""
    url = CMA_URL_TEMPLATE.format(lead_id=LEAD_ID)
    response = session.get(url, timeout=60)
    if response.status_code >= 400:
        raise PropelioScraperError(
            f"GET {url} -> HTTP {response.status_code}: {response.text[:300]}"
        )
    raw = response.json()
    if isinstance(raw, list):
        return raw[0] if raw else {}
    return raw


def main() -> int:
    print("=" * 70)
    print("Propelio spatial / pagination probe (READ-ONLY)")
    print(f"  lead_id={LEAD_ID}  cma_id={CMA_ID}")
    print(f"  inter-call delay: {INTER_CALL_DELAY_S}s")
    print("=" * 70)

    client = login_propelio()
    session = client.session

    # --- A. Baseline GET ----------------------------------------------------
    a_payload = _get_baseline(session)
    a = _summarize(a_payload)
    _report("A. Baseline GET /legacy/cma/{lead_id}", a, baseline=None)
    baseline = a["source_id_set"]

    # We need the subject's lat/lon for the address-override probe (C).
    subj = (a_payload.get("data") or {}).get("subject") or {}
    subj_lat = subj.get("address_lat") or subj.get("lat")
    subj_lon = subj.get("address_lon") or subj.get("lon")
    if not (subj_lat and subj_lon):
        # Fallback: borrow from the first comp (approximate; only matters
        # for the address-override probe).
        first_comp = ((a_payload.get("data") or {}).get("sales") or [{}])[0]
        addr = first_comp.get("address") or {}
        subj_lat = addr.get("lat")
        subj_lon = addr.get("lon")
    print(f"\nSubject anchor for probe C: lat={subj_lat}, lon={subj_lon}")

    time.sleep(INTER_CALL_DELAY_S)

    # --- B. Baseline POST /search (no extra params) -------------------------
    body_b = {"months": 24, "range": "10"}
    b = _summarize(_post_search(session, body_b))
    _report("B. POST /search baseline (months=24, range=10)", b, baseline)
    time.sleep(INTER_CALL_DELAY_S)

    # --- C. POST with address override --------------------------------------
    # Offset the center ~3 miles north. 1 degree latitude ~= 69 mi,
    # so 3 mi north ~= +0.0435 degrees lat.
    if subj_lat and subj_lon:
        offset_lat = float(subj_lat) + 0.0435
        offset_lon = float(subj_lon)
        body_c = {
            "months": 24,
            "range": "10",
            "address": {"lat": offset_lat, "lon": offset_lon},
        }
        c = _summarize(_post_search(session, body_c))
        _report(
            f"C. POST /search with address override (~3mi N of subject)",
            c,
            baseline,
        )
    else:
        print("\n[C. POST /search with address override] SKIPPED — no anchor")
    time.sleep(INTER_CALL_DELAY_S)

    # --- D. POST with offset: 100 -------------------------------------------
    body_d = {"months": 24, "range": "10", "offset": 100}
    d = _summarize(_post_search(session, body_d))
    _report("D. POST /search with offset=100", d, baseline)
    time.sleep(INTER_CALL_DELAY_S)

    # --- E. POST with exclude_ids -------------------------------------------
    # Hand it the 10 most-common baseline source_ids and ask it to skip
    # them. If exclude works, we'd expect 10 *new* comps to appear in slots
    # that were occupied by those 10.
    sample_excludes = sorted(baseline)[:10] if baseline else []
    body_e = {
        "months": 24,
        "range": "10",
        "exclude_ids": sample_excludes,
        "exclude_source_ids": sample_excludes,  # double-tap: try both names
    }
    e = _summarize(_post_search(session, body_e))
    _report("E. POST /search with exclude_ids/exclude_source_ids", e, baseline)
    time.sleep(INTER_CALL_DELAY_S)

    # --- F. POST with page: 2 -----------------------------------------------
    body_f = {"months": 24, "range": "10", "page": 2}
    f_ = _summarize(_post_search(session, body_f))
    _report("F. POST /search with page=2", f_, baseline)

    # --- Verdict ------------------------------------------------------------
    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    print(
        "Compare each probe's `fingerprint` to (A) and (B). Same fingerprint\n"
        "means the param was ignored. Different fingerprint with mostly-\n"
        "fresh source_ids means the param actually shifted the result set --\n"
        "that's the bypass mechanism we want."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PropelioScraperError as exc:
        print(f"\nABORT: {exc}", file=sys.stderr)
        sys.exit(1)
