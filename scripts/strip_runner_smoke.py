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
