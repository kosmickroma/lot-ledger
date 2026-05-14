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


SELFTESTS: list[tuple[str, callable]] = []


def selftest(label: str):
    """Decorator to register a selftest function."""
    def _decorate(fn):
        SELFTESTS.append((label, fn))
        return fn
    return _decorate


# Selftest registrations get added by subsequent tasks. Empty for now.


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
