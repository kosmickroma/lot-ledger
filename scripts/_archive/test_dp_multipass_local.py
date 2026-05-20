# scripts/test_dp_multipass_local.py
#
# Local terminal-only stealth probe for Propelio deep-pull multipass behavior.
# Exercises one authenticated client across six jittered passes with no DB writes.
#
# Connects to:
#   api/propelio/config.py   - Propelio credentials from env
#   api/propelio/scraper.py  - PropelioClient API calls
#   api/propelio/archive.py  - _comp_address_key dedup key helper

from __future__ import annotations

import logging
import random
import sys
import time

from api.propelio.archive import _comp_address_key
from api.propelio.config import PROPELIO_PASSWORD, PROPELIO_USERNAME
from api.propelio.scraper import PropelioClient

PASSES = [
    {"months": 24, "range_mi": 0.25, "label": "tightest"},
    {"months": 24, "range_mi": 0.5, "label": "blocks"},
    {"months": 24, "range_mi": 1.0, "label": "neighborhood"},
    {"months": 24, "range_mi": 2.0, "label": "broader"},
    {"months": 24, "range_mi": 5.0, "label": "wider"},
    {"months": 24, "range_mi": 10.0, "label": "rural_fallback"},
]
SATURATION_MIN_PASSES = 3
SATURATION_THRESHOLD = 0.05


def _jittered_pass_sleep_seconds() -> float:
    """Inter-pass sleep with heavy-tailed mixed distribution. Mirrors
    api/propelio/deep_pull.py:_jittered_pass_sleep_seconds. 80% in 15-45s,
    20% in 45-120s — avoids uniform-distribution fingerprinting."""
    if random.random() < 0.8:
        return random.uniform(15, 45)
    return random.uniform(45, 120)


def main(address: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("test_dp_multipass_local")

    client = PropelioClient(username=PROPELIO_USERNAME, password=PROPELIO_PASSWORD)
    client.login()
    logger.info("login OK")

    lead_id, subject_lot_sqft, parcel_bundle = client.find_lead_id(address)
    confirmation_key = parcel_bundle.get("confirmation_key") if isinstance(parcel_bundle, dict) else None
    logger.info(
        "find_lead_id -> lead_id=%s subject_lot=%s has_ck=%s",
        lead_id,
        subject_lot_sqft,
        bool(confirmation_key),
    )

    envelope = client.add_cma(
        lead_id,
        confirmation_key,
        months=PASSES[0]["months"],
        range_mi=PASSES[0]["range_mi"],
    )
    cma_id = str(envelope.get("id") or "")
    if not cma_id:
        logger.error("FAILED: could not extract cma_id from envelope. keys=%s", list(envelope.keys()))
        return 1

    pass_1_comps = (envelope.get("data") or {}).get("sales") or []
    logger.info("Pass 1 (add_cma): cma_id=%s, comps in envelope=%d", cma_id, len(pass_1_comps))

    seen: set[str] = set()
    per_pass: list[dict[str, int | str]] = []

    pass_1_new = 0
    for comp in pass_1_comps:
        key = _comp_address_key(comp if isinstance(comp, dict) else {})
        if key and key not in seen:
            seen.add(key)
            pass_1_new += 1
    per_pass.append({"pass": 1, "label": PASSES[0]["label"], "returned": len(pass_1_comps), "new": pass_1_new})

    total_unique = len(seen)

    for pass_num in range(2, len(PASSES) + 1):
        sleep_s = _jittered_pass_sleep_seconds()
        logger.info("sleep %.1fs before pass %d", sleep_s, pass_num)
        time.sleep(sleep_s)

        cfg = PASSES[pass_num - 1]
        logger.info("Pass %d (%s): months=%d range=%.2fmi", pass_num, cfg["label"], cfg["months"], cfg["range_mi"])
        try:
            response = client.search_cma(
                lead_id,
                cma_id,
                months=cfg["months"],
                range_mi=cfg["range_mi"],
            )
        except Exception as exc:
            logger.error("Pass %d FAILED with %s: %s", pass_num, type(exc).__name__, exc)
            logger.error("STOPPING - possible stealth detection. DO NOT retry without investigation.")
            return 1

        comps = (response.get("data") or {}).get("sales") or []
        new_count = 0
        for comp in comps:
            key = _comp_address_key(comp if isinstance(comp, dict) else {})
            if key and key not in seen:
                seen.add(key)
                new_count += 1

        total_unique = len(seen)
        per_pass.append({"pass": pass_num, "label": cfg["label"], "returned": len(comps), "new": new_count})
        logger.info(
            "Pass %d done: returned=%d new=%d total_unique=%d",
            pass_num,
            len(comps),
            new_count,
            total_unique,
        )

        if pass_num >= SATURATION_MIN_PASSES and (new_count / max(total_unique, 1)) < SATURATION_THRESHOLD:
            logger.info("SATURATED after pass %d (%.1f%% new)", pass_num, 100 * new_count / max(total_unique, 1))
            break

    print("\n=== SUMMARY ===")
    print(f"{'Pass':<5} {'Label':<18} {'Returned':<10} {'New':<8}")
    for row in per_pass:
        print(f"{row['pass']:<5} {row['label']:<18} {row['returned']:<10} {row['new']:<8}")
    print(f"\nTotal unique comps: {total_unique}")
    print(f"Passes completed: {len(per_pass)}")
    print("PASS - no Propelio errors. Strategy looks viable.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: python -m scripts.test_dp_multipass_local "<address>"', file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
