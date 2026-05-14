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
