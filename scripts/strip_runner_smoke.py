#!/usr/bin/env python3
# scripts/strip_runner_smoke.py
#
# Inline assertion-based selftests for scripts/strip_runner.py.
# Follows the repo's existing _smoke.py convention (see scripts/propelio_refresh_smoke.py,
# scripts/marathon_campaign/smoke_phase4a_runner.py).
#
# MUST be invoked from the repo root:
#     cd /path/to/lot-ledger
#     python scripts/strip_runner_smoke.py

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _assert_eq(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def _assert_in(needle: object, haystack: object, label: str) -> None:
    if needle not in haystack:  # type: ignore[operator]
        raise AssertionError(f"{label}: expected {needle!r} in {haystack!r}")


def _assert_true(cond: bool, label: str) -> None:
    if not cond:
        raise AssertionError(f"{label}: expected True")


SELFTESTS: list[tuple[str, Callable[[], None]]] = []


def selftest(label: str):
    """Decorator to register a selftest function."""
    def _decorate(fn):
        SELFTESTS.append((label, fn))
        return fn
    return _decorate


# Selftest registrations get added by subsequent tasks. Empty for now.

from scripts.strip_runner import FILTERS, load_addresses
import tempfile


@selftest("FILTERS has 21 entries in spec-defined order")
def _test_filters_shape():
    _assert_eq(len(FILTERS), 21, "FILTERS length")
    _assert_eq(FILTERS[0], (24, 5.0), "FILTERS[0]")
    _assert_eq(FILTERS[20], (1, 0.25), "FILTERS[20]")
    # 24-month band: 5 entries
    band_24 = [f for f in FILTERS if f[0] == 24]
    _assert_eq(len(band_24), 5, "24-month band length")
    # 1-month band: 3 entries
    band_1 = [f for f in FILTERS if f[0] == 1]
    _assert_eq(len(band_1), 3, "1-month band length")


@selftest("load_addresses strips comments, blanks, whitespace")
def _test_load_addresses_happy():
    text = "# header comment\n1234 Main St, Dallas TX 75201\n\n  5678 Oak Ave, Dallas TX 75223  \n# trailing comment\n9012 Pine Rd, Dallas TX 75228\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(text)
        path = fh.name
    addresses = load_addresses(path)
    _assert_eq(len(addresses), 3, "address count")
    _assert_eq(addresses[0], "1234 Main St, Dallas TX 75201", "first address")
    _assert_eq(addresses[1], "5678 Oak Ave, Dallas TX 75223", "second address whitespace-stripped")
    _assert_eq(addresses[2], "9012 Pine Rd, Dallas TX 75228", "third address")


@selftest("load_addresses raises on empty after strip")
def _test_load_addresses_empty():
    text = "# only comments\n\n# nothing else\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        load_addresses(path)
    except ValueError as exc:
        _assert_in("empty", str(exc).lower(), "empty error message")
        return
    raise AssertionError("expected ValueError on empty address file")


@selftest("load_addresses raises on missing file")
def _test_load_addresses_missing():
    try:
        load_addresses("/tmp/__nonexistent_strip_runner_test_file__.txt")
    except (FileNotFoundError, ValueError):
        return
    raise AssertionError("expected FileNotFoundError or ValueError on missing file")


@selftest("load_addresses strips UTF-8 BOM from first line")
def _test_load_addresses_bom():
    # UTF-8 BOM (0xEF 0xBB 0xBF) prefix on first line; `strip()` does not remove it,
    # so encoding="utf-8-sig" must be used in load_addresses.
    text = "﻿# header comment\n1234 Main St, Dallas TX 75201\n5678 Oak Ave, Dallas TX 75223\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        path = fh.name
    addresses = load_addresses(path)
    _assert_eq(len(addresses), 2, "BOM file address count")
    _assert_eq(addresses[0], "1234 Main St, Dallas TX 75201", "first address (no BOM bleed)")
    _assert_true("﻿" not in addresses[0], "no BOM remnant in first address")


from scripts.strip_runner import (
    fmt_ts,
    log_addr_header,
    log_setup_ok,
    log_setup_fail,
    log_pass,
    log_pass_error,
    log_addr_done,
    log_addr_skipped,
)
from datetime import datetime


_FIXED_TS = datetime(2026, 5, 14, 12, 5, 14)


@selftest("fmt_ts produces HH:MM:SS bracketed prefix")
def _test_fmt_ts():
    _assert_eq(fmt_ts(_FIXED_TS), "[12:05:14]", "fmt_ts at fixed time")


@selftest("log_addr_header format matches spec §9")
def _test_log_addr_header():
    line = log_addr_header(_FIXED_TS, idx=4, total=25, address="1234 Main St, Dallas TX 75201")
    _assert_eq(line, "[12:05:14] address 4/25: 1234 Main St, Dallas TX 75201", "addr header")


@selftest("log_setup_ok format matches spec §9")
def _test_log_setup_ok():
    line = log_setup_ok(_FIXED_TS, cma_id="cma_a1b2c3d4", elapsed_s=3.8)
    _assert_eq(line, "[12:05:14]   setup: add_cma ok   cma_id=cma_a1b2c3d4   (3.8s)", "setup ok")


@selftest("log_setup_fail format matches spec §9")
def _test_log_setup_fail():
    line = log_setup_fail(_FIXED_TS, error_summary="HTTPError 503")
    _assert_eq(line, "[12:05:14]   setup: add_cma failed — HTTPError 503; skipping address", "setup fail")


@selftest("log_pass format matches spec §9 (aligned columns)")
def _test_log_pass():
    line = log_pass(
        _FIXED_TS,
        pass_num=2,
        pass_total=21,
        months=24,
        range_mi=2.0,
        returned=142,
        new=18,
        addr_total=305,
    )
    _assert_eq(
        line,
        "[12:05:14]   pass  2/21   24mo / 2.0mi   returned 142   new  18   addr_total 305",
        "pass line",
    )


@selftest("log_pass_error format matches spec §9")
def _test_log_pass_error():
    line = log_pass_error(
        _FIXED_TS,
        pass_num=12,
        pass_total=21,
        months=6,
        range_mi=2.0,
        error_summary="HTTPError 502",
    )
    _assert_eq(
        line,
        "[12:05:14]   pass 12/21    6mo / 2.0mi   ERROR HTTPError 502 — continuing",
        "pass error line",
    )


@selftest("log_addr_done format matches spec §9 (all filters ok)")
def _test_log_addr_done_clean():
    line = log_addr_done(_FIXED_TS, filters_ok=21, filters_total=21, filters_errored=0, addr_net_new=487)
    _assert_eq(
        line,
        "[12:05:14]   address done: 21/21 filters ok, 487 net-new comps to cache",
        "addr done clean",
    )


@selftest("log_addr_done format matches spec §9 (some errors)")
def _test_log_addr_done_partial():
    line = log_addr_done(_FIXED_TS, filters_ok=20, filters_total=21, filters_errored=1, addr_net_new=423)
    _assert_eq(
        line,
        "[12:05:14]   address done: 20/21 filters ok, 1 errored, 423 net-new comps to cache",
        "addr done partial",
    )


@selftest("log_addr_skipped format matches spec §9")
def _test_log_addr_skipped():
    line = log_addr_skipped(_FIXED_TS, reason="lead lookup failed")
    _assert_eq(line, "[12:05:14]   address skipped: lead lookup failed", "addr skipped")


from scripts.strip_runner import (
    inter_pull_sleep_seconds,
    inter_address_sleep_seconds,
    setup_to_first_pull_sleep_seconds,
)


@selftest("inter_pull_sleep_seconds returns values in [15, 60]")
def _test_inter_pull_range():
    # Run 500 samples; all must fall in band A [15, 30] or band B [30, 60]
    for _ in range(500):
        v = inter_pull_sleep_seconds()
        _assert_true(15.0 <= v <= 60.0, f"inter_pull sample {v} out of [15, 60]")


@selftest("inter_pull_sleep_seconds favors band A (~80%)")
def _test_inter_pull_band_ratio():
    samples = [inter_pull_sleep_seconds() for _ in range(2000)]
    band_a = sum(1 for v in samples if v <= 30.0)
    ratio = band_a / len(samples)
    # Expect ~80% in band A; allow generous slack for randomness (0.70..0.90)
    _assert_true(0.70 <= ratio <= 0.90, f"band A ratio {ratio:.2f} outside [0.70, 0.90]")


@selftest("inter_address_sleep_seconds returns values in [15, 45]")
def _test_inter_address_range():
    for _ in range(500):
        v = inter_address_sleep_seconds()
        _assert_true(15.0 <= v <= 45.0, f"inter_address sample {v} out of [15, 45]")


@selftest("setup_to_first_pull_sleep_seconds returns values in [3, 5]")
def _test_setup_pause_range():
    for _ in range(500):
        v = setup_to_first_pull_sleep_seconds()
        _assert_true(3.0 <= v <= 5.0, f"setup pause sample {v} out of [3, 5]")


def main() -> int:
    print(f"strip_runner_smoke: running {len(SELFTESTS)} selftest(s)")
    failures: list[tuple[str, str]] = []
    for label, fn in SELFTESTS:
        try:
            fn()
            print(f"  [ok]   {label}")
        except Exception as exc:
            failures.append((label, f"{type(exc).__name__}: {exc}"))
            print(f"  [FAIL] {label}: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    if failures:
        print(f"\n{len(failures)} selftest(s) failed")
        return 1

    print(f"\nall {len(SELFTESTS)} selftest(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
