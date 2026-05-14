#!/usr/bin/env python3
# scripts/strip_runner.py
#
# Strip Runner — runs Propelio comp pulls against a hand-curated address list.
# See docs/propelio/STRIP_RUNNER_SPEC.md (v1.3) for design rationale.
#
# Coupled to api/propelio/deep_pull.py — keep in sync if Propelio response shape changes.
#
# MUST be invoked from the repo root so api.propelio.* imports resolve:
#     cd /path/to/lot-ledger
#     python scripts/strip_runner.py --addresses scripts/strip_runner_addresses/<file>.txt

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


FILTERS: list[tuple[int, float]] = [
    # 24-month band
    (24, 5.0), (24, 2.0), (24, 1.0), (24, 0.5), (24, 0.25),
    # 12-month band
    (12, 5.0), (12, 2.0), (12, 1.0), (12, 0.5), (12, 0.25),
    # 6-month band
    (6, 5.0), (6, 2.0), (6, 1.0), (6, 0.5), (6, 0.25),
    # 3-month band
    (3, 1.0), (3, 0.5), (3, 0.25),
    # 1-month band
    (1, 1.0), (1, 0.5), (1, 0.25),
]
assert len(FILTERS) == 21, "FILTERS must have exactly 21 entries per spec §5"


def load_addresses(path: str) -> list[str]:
    """Read an address list file and return the stripped non-blank, non-comment lines.

    Raises FileNotFoundError if the file does not exist, ValueError if the
    file exists but contains no addresses after stripping comments and blanks.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"address file not found: {path}")

    # utf-8-sig auto-strips the UTF-8 BOM if present, so a BOM-prefixed first
    # line doesn't get parsed as a malformed address. Plain utf-8 leaves the BOM.
    with file_path.open("r", encoding="utf-8-sig") as fh:
        lines = fh.readlines()

    addresses: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        addresses.append(stripped)

    if not addresses:
        raise ValueError(f"address file is empty (no non-comment, non-blank lines): {path}")

    return addresses


def fmt_ts(now: datetime | None = None) -> str:
    """Return a bracketed [HH:MM:SS] timestamp prefix. `now` is injectable for tests."""
    if now is None:
        now = datetime.now()
    return now.strftime("[%H:%M:%S]")


def log_addr_header(now: datetime, *, idx: int, total: int, address: str) -> str:
    return f"{fmt_ts(now)} address {idx}/{total}: {address}"


def log_setup_ok(now: datetime, *, cma_id: str, elapsed_s: float) -> str:
    return f"{fmt_ts(now)}   setup: add_cma ok   cma_id={cma_id}   ({elapsed_s:.1f}s)"


def log_setup_fail(now: datetime, *, error_summary: str) -> str:
    return f"{fmt_ts(now)}   setup: add_cma failed — {error_summary}; skipping address"


def log_pass(
    now: datetime,
    *,
    pass_num: int,
    pass_total: int,
    months: int,
    range_mi: float,
    returned: int,
    new: int,
    addr_total: int,
) -> str:
    # Column widths chosen to match spec §9 sample: pass 2-digit, months 2-digit,
    # range_mi 4-char (e.g. "2.0" or "0.25"), returned/new/addr_total 3-digit padded.
    # Use a custom formatter: preserve at least one decimal digit (2.0 not 2, 0.25 stays 0.25).
    raw = f"{range_mi:g}"
    range_str = (raw if "." in raw else raw + ".0") + "mi"
    months_str = f"{months}mo"
    return (
        f"{fmt_ts(now)}   pass {pass_num:>2}/{pass_total}   "
        f"{months_str:>4} / {range_str:<6}  "
        f"returned {returned:>3}   new {new:>3}   addr_total {addr_total}"
    )


def log_pass_error(
    now: datetime,
    *,
    pass_num: int,
    pass_total: int,
    months: int,
    range_mi: float,
    error_summary: str,
) -> str:
    raw = f"{range_mi:g}"
    range_str = (raw if "." in raw else raw + ".0") + "mi"
    months_str = f"{months}mo"
    return (
        f"{fmt_ts(now)}   pass {pass_num:>2}/{pass_total}   "
        f"{months_str:>4} / {range_str:<6}  "
        f"ERROR {error_summary} — continuing"
    )


def log_addr_done(
    now: datetime,
    *,
    filters_ok: int,
    filters_total: int,
    filters_errored: int,
    addr_net_new: int,
) -> str:
    if filters_errored == 0:
        return f"{fmt_ts(now)}   address done: {filters_ok}/{filters_total} filters ok, {addr_net_new} net-new comps to cache"
    return (
        f"{fmt_ts(now)}   address done: {filters_ok}/{filters_total} filters ok, "
        f"{filters_errored} errored, {addr_net_new} net-new comps to cache"
    )


def log_addr_skipped(now: datetime, *, reason: str) -> str:
    return f"{fmt_ts(now)}   address skipped: {reason}"


# --- Pacing (spec §7) -------------------------------------------------------
#
# Band A: 80% of inter-pull pauses, uniform(15, 30) — floor raised from 10s
#         per Copilot v1 review #3 for conservative-first-run.
# Band B: 20% of inter-pull pauses, uniform(30, 60).
# Setup→first-pull: uniform(3, 5) — closes immediate-burst gap on same CMA.
# Inter-address: uniform(15, 45).
#
# Future-tuning principle (KK): speed up gradually but always preserve the
# non-uniform two-band shape — don't fully look like a bot.


INTER_PULL_BAND_A_PROB = 0.80
INTER_PULL_BAND_A_MIN = 15.0
INTER_PULL_BAND_A_MAX = 30.0
INTER_PULL_BAND_B_MIN = 30.0
INTER_PULL_BAND_B_MAX = 60.0

INTER_ADDRESS_MIN = 15.0
INTER_ADDRESS_MAX = 45.0

SETUP_TO_FIRST_PULL_MIN = 3.0
SETUP_TO_FIRST_PULL_MAX = 5.0


def inter_pull_sleep_seconds() -> float:
    """Two-band jitter pause between filter pulls within an address."""
    if random.random() < INTER_PULL_BAND_A_PROB:
        return random.uniform(INTER_PULL_BAND_A_MIN, INTER_PULL_BAND_A_MAX)
    return random.uniform(INTER_PULL_BAND_B_MIN, INTER_PULL_BAND_B_MAX)


def inter_address_sleep_seconds() -> float:
    """Pause between addresses."""
    return random.uniform(INTER_ADDRESS_MIN, INTER_ADDRESS_MAX)


def setup_to_first_pull_sleep_seconds() -> float:
    """Fixed-range pause between add_cma (CMA setup) and the first search_cma."""
    return random.uniform(SETUP_TO_FIRST_PULL_MIN, SETUP_TO_FIRST_PULL_MAX)


# --- Mock client (smoke tests only) -----------------------------------------
#
# Mimics the four PropelioClient methods that strip_runner actually calls.
# Deterministic comp counts so smoke assertions are stable.
#
# Trigger addresses for error path coverage in smoke tests:
#   "POISON_LEAD 0000"  -> find_lead_id raises RuntimeError
#   "POISON_CMA 0000"   -> add_cma raises RuntimeError
#   "POISON_FILTER 0000"-> every search_cma raises RuntimeError (for burst guard)
#   "POISON_AUTH 0000"  -> raises an exception whose str() includes "401" (auth-class)


class MockPropelioClient:
    def __init__(self) -> None:
        self._logged_in = False
        self._call_counter = 0

    def login(self) -> None:
        self._logged_in = True

    def find_lead_id(self, address: str) -> tuple[str, float, dict[str, Any]]:
        if not self._logged_in:
            raise RuntimeError("MockPropelioClient: must call login() first")
        if address.startswith("POISON_LEAD"):
            raise RuntimeError("MockPropelioClient: poisoned lead")
        if address.startswith("POISON_AUTH"):
            raise RuntimeError("Mock 401 unauthorized")
        # Deterministic lead_id derived from address hash
        h = abs(hash(address)) % 10_000_000
        return (f"lead_{h:07d}", 7500.0, {"confirmation_key": f"ck_{h:07d}"})

    def add_cma(
        self, lead_id: str, confirmation_key: str, months: int, range_mi: float
    ) -> dict[str, Any]:
        if not self._logged_in:
            raise RuntimeError("MockPropelioClient: must call login() first")
        # NOTE: Task 7 will refactor this whole class to use explicit address-prefix
        # triggers (POISON_CMA, POISON_FILTER, POISON_BURST) routed through lead_id
        # encoding. The current hash-based "if lead_id == 'lead_0000001'" trigger is
        # inert in Task 5's selftests (they don't exercise the poison path here) but
        # serves as scaffolding for the Task 7 refactor.
        if lead_id == "lead_0000001":
            raise RuntimeError("MockPropelioClient: poisoned cma setup")
        return {
            "id": f"cma_{abs(hash(lead_id)) % 10_000_000:07d}",
            "data": {"sales": [{"address_full": "stale comp ignored"}]},
        }

    def search_cma(
        self, lead_id: str, cma_id: str, months: int, range_mi: float
    ) -> dict[str, Any]:
        if not self._logged_in:
            raise RuntimeError("MockPropelioClient: must call login() first")
        # Task 7 trigger scaffolding — currently inert (see add_cma note).
        if lead_id == "lead_0000002":
            raise RuntimeError("MockPropelioClient: poisoned filter")
        # Deterministic comp count: more comps for larger range_mi, fewer for tighter
        # range. months tweaks the count slightly so different filter combos produce
        # different (but predictable) comp sets.
        base = int(20 * range_mi) + months
        comps = []
        for i in range(base):
            comps.append(
                {
                    "address_full": f"{lead_id}_{i:03d}_comp_addr",
                    "comp_address_key": f"{lead_id}_comp_{i:03d}",
                    "months": months,
                    "range_mi": range_mi,
                }
            )
        return {"data": {"sales": comps}}


from dataclasses import dataclass


@dataclass(frozen=True)
class AddressOutcome:
    """Per-address result returned by run_address.

    status:
        "complete"  - all 21 filters fired without error
        "partial"   - some filters errored but address ran to completion
        "skipped"   - address-level failure (lead lookup, cma setup, or burst guard)
    skip_reason:
        Present when status == "skipped". One of:
            "lead lookup failed"
            "cma setup failed"
            "3 consecutive filter errors"
    """
    status: str  # "complete" | "partial" | "skipped"
    filters_ok: int
    filters_errored: int
    addr_net_new: int
    propelio_returned: int  # raw comps returned across all successful pulls
    cma_id: str | None
    skip_reason: str | None = None


def _persist_pull_mock(addr_seen_keys: set[str], comps: list[dict[str, Any]]) -> tuple[int, int]:
    """Mock persistence: count returned + net-new based on a per-address dedup set."""
    returned = len(comps)
    new = 0
    for c in comps:
        key = c.get("comp_address_key") or c.get("address_full")
        if not key:
            continue
        if key not in addr_seen_keys:
            new += 1
            addr_seen_keys.add(key)
    return returned, new


def _persist_pull_real(comps: list[dict[str, Any]]) -> tuple[int, int]:
    """Real persistence path via api.propelio.archive.merge_comps_into_global.

    Mirrors the flow in api/propelio/deep_pull.py: _parse_property -> dict via
    asdict() -> match_comps_to_parcels (with WARNING fallback) -> merge.
    """
    from dataclasses import asdict
    from api.propelio.scraper import _parse_property
    from api.propelio.parcel_match import match_comps_to_parcels
    from api.propelio.archive import merge_comps_into_global

    parsed = []
    for raw in comps:
        if not isinstance(raw, dict):
            continue
        try:
            parsed.append(asdict(_parse_property(raw)))
        except Exception:
            continue
    if not parsed:
        return 0, 0

    try:
        matched = match_comps_to_parcels(parsed)
    except Exception as exc:
        # Spec §6 / §8: log WARNING, fall back to unmatched merge
        print(f"  [warn] parcel_match failed (non-fatal): {exc}", file=sys.stderr)
        matched = parsed

    merge_result = merge_comps_into_global(matched, source="strip_runner")
    returned = len(comps)
    new = int(merge_result.get("inserted", 0) or 0)
    return returned, new


def run_address(
    *,
    client: Any,
    address: str,
    idx: int,
    total: int,
    mock: bool = False,
) -> AddressOutcome:
    """Run the full 21-filter sweep against one address.

    Happy path only in this task. Error handling is added in Task 7.
    """
    now = datetime.now()
    print(log_addr_header(now, idx=idx, total=total, address=address))

    # Step 1: lead lookup
    lead_id, _subject_sqft, parcel_bundle = client.find_lead_id(address)
    confirmation_key = parcel_bundle.get("confirmation_key") if isinstance(parcel_bundle, dict) else None

    # Step 2: CMA setup (comps discarded)
    setup_start = time.monotonic()
    envelope = client.add_cma(lead_id, confirmation_key, months=FILTERS[0][0], range_mi=FILTERS[0][1])
    cma_id = _extract_cma_id_from_envelope(envelope)
    setup_elapsed = time.monotonic() - setup_start
    print(log_setup_ok(datetime.now(), cma_id=cma_id, elapsed_s=setup_elapsed))

    # Setup -> first-pull pause (closes immediate-burst gap on same CMA)
    time.sleep(setup_to_first_pull_sleep_seconds())

    # Step 3: 21 filter pulls via search_cma
    addr_seen_keys: set[str] = set()
    addr_total_new = 0
    propelio_returned_total = 0
    filters_ok = 0

    for pass_num, (months, range_mi) in enumerate(FILTERS, start=1):
        if pass_num > 1:
            time.sleep(inter_pull_sleep_seconds())
        envelope = client.search_cma(lead_id, cma_id, months=months, range_mi=range_mi)
        comps = _parse_cma_envelope_sales(envelope)
        if mock:
            returned, new = _persist_pull_mock(addr_seen_keys, comps)
        else:
            returned, new = _persist_pull_real(comps)
        propelio_returned_total += returned
        addr_total_new += new
        filters_ok += 1
        print(
            log_pass(
                datetime.now(),
                pass_num=pass_num,
                pass_total=len(FILTERS),
                months=months,
                range_mi=range_mi,
                returned=returned,
                new=new,
                addr_total=addr_total_new,
            )
        )

    print(
        log_addr_done(
            datetime.now(),
            filters_ok=filters_ok,
            filters_total=len(FILTERS),
            filters_errored=0,
            addr_net_new=addr_total_new,
        )
    )
    return AddressOutcome(
        status="complete",
        filters_ok=filters_ok,
        filters_errored=0,
        addr_net_new=addr_total_new,
        propelio_returned=propelio_returned_total,
        cma_id=cma_id,
    )


# Local re-implementations of the small helpers from api/propelio/deep_pull.py
# (the underscore-prefixed ones). Importing them directly would trigger a full
# scraper module load on smoke runs, so we reimplement here. Drift risk covered
# by the _is_auth_class equivalence selftest in Task 7.
def _extract_cma_id_from_envelope(envelope: dict[str, Any]) -> str:
    if not isinstance(envelope, dict):
        raise ValueError(f"add_cma envelope is not a dict: type={type(envelope)}")
    cma_id = str(envelope.get("id") or "").strip()
    if not cma_id:
        raise ValueError(f"could not extract cma_id from envelope keys={list(envelope.keys())}")
    return cma_id


def _parse_cma_envelope_sales(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(envelope, dict):
        return []
    data = envelope.get("data")
    if not isinstance(data, dict):
        return []
    sales = data.get("sales")
    if not isinstance(sales, list):
        return []
    return [item for item in sales if isinstance(item, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Strip Runner — hand-curated Propelio comp sweep")
    parser.add_argument(
        "--addresses",
        required=True,
        help="Path to address list file (one address per line, # for comments).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the in-process MockPropelioClient instead of real Propelio. For smoke testing.",
    )
    args = parser.parse_args()

    print("strip_runner.py scaffolded; main loop not implemented yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
