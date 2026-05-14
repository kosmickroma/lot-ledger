# Strip Runner Implementation Plan v1.1

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Changes v1 → v1.1 (Copilot plan-review findings folded in)

1. **BLOCKER (Task 6/7 TDD runtime):** Pacing helpers in `run_address` will make Task 6/7 selftests run for ~10 min per call without monkeypatching. Added a `_zero_pacing()` context manager to the smoke harness in Task 6 and reused it in Tasks 7 and 8. Selftests now run sub-second.
2. **IMPORTANT (auth-block summary, spec §10):** v1 plan's `main()` returned 2 on `AuthBlockExit` without printing the summary, violating spec §10. v1.1 wraps `run_all` in try/finally so the summary always prints, including on auth-block exit, before re-raising.
3. **IMPORTANT (burst-guard reset test):** v1 had a reset-on-success path in `run_address` but no test for it. Redefined `POISON_BURST` mock trigger to fail-fail-succeed-fail-fail-then-succeed (instead of fail-3-then-succeed which never reached the reset path because the burst guard fired first). Added an explicit reset selftest.
4. **NICE-TO-HAVE (§12 wording):** Task 6 note said `_classify_propelio_error` would be imported lazily in Task 7, but Task 7 re-implements it as `_is_auth_class`. Fixed the note and added a small equivalence selftest covering 401/403/429/throttle-text/benign inputs.
5. **NICE-TO-HAVE (BOM-prefixed address files):** v1 `load_addresses` opened with `encoding="utf-8"`; a BOM-prefixed first line would parse as an invalid address. Switched to `utf-8-sig`. Added a BOM selftest.
6. **Direct-answer add:** Inserted new Task 9 — "Pilot real-address run (2-3 addresses)" — between mock CLI rehearsal (Task 8) and final push (now Task 10). Validates the real Propelio path before KK kicks off the 25-address strip.

Total selftests grow from 27 → 31.

**Goal:** Build the Strip Runner — a single-file Python script that runs Propelio comp pulls against a hand-curated address list with a fixed 21-filter matrix per address, bypassing the marathon FSM/breaker/reconcile machinery.

**Architecture:** One script (`scripts/strip_runner.py`, ~250 lines) drives a sequential loop. It logs into Propelio once, then for each address: `find_lead_id` → `add_cma` for CMA setup (comps discarded) → 21 × `search_cma` (one per filter). Per-pull terminal logging, tight pacing with two-band jitter, log-and-continue error handling. Persistence reuses `merge_comps_into_global` from `api/propelio/archive.py`. Tests are inline assertions in a sibling smoke script (`scripts/strip_runner_smoke.py`) following the repo's existing `_smoke.py` convention. No `tests/` directory, no pytest.

**Tech Stack:** Python 3, `psycopg2` (transitive via `api.config`), existing `api/propelio/*` modules. No new dependencies.

**Spec:** `docs/propelio/STRIP_RUNNER_SPEC.md` (v1.3, build-eligible per Copilot rounds 1-3).

**Branch:** `feat/strip-runner` (already created off `develop`).

---

## File Structure

| Path | Status | Responsibility |
|---|---|---|
| `scripts/strip_runner.py` | Create | Main runner: CLI, FILTERS, address loader, log formatters, pacing helpers, MockPropelioClient (for smoke), per-address execution, main loop, summary printer |
| `scripts/strip_runner_smoke.py` | Create | Inline assertion-based selftest harness exercising the runner end-to-end with the mock client |
| `scripts/strip_runner_addresses/.gitkeep` | Create | Empty placeholder so the address directory is committed to git |
| `scripts/strip_runner_addresses/strip_dallas_south.txt` | Create later | KK pastes his address list here when ready to run for real (not part of this plan) |

The script is intentionally single-file because it's a throwaway-grade local-operator tool per the spec §1. If it grows, splitting can happen later.

---

## Task 1: Scaffold strip_runner.py and the smoke harness

**Files:**
- Create: `scripts/strip_runner.py`
- Create: `scripts/strip_runner_smoke.py`
- Create: `scripts/strip_runner_addresses/.gitkeep`

This task establishes the file skeleton, CLI parser, and the smoke-test harness pattern. Subsequent tasks add functions and grow the smoke harness's selftest list.

- [ ] **Step 1: Create the address directory placeholder**

```bash
mkdir -p scripts/strip_runner_addresses
touch scripts/strip_runner_addresses/.gitkeep
```

- [ ] **Step 2: Create `scripts/strip_runner.py` with shebang, imports, CLI stub, main() entrypoint**

```python
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
```

- [ ] **Step 3: Create `scripts/strip_runner_smoke.py` with the selftest harness skeleton**

```python
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
```

- [ ] **Step 4: Verify both files run without crashing**

Run:
```bash
cd /home/kk/projects/clients/lot-ledger
python scripts/strip_runner.py --addresses /dev/null
```
Expected: prints `strip_runner.py scaffolded; main loop not implemented yet` and exits 0.

Run:
```bash
python scripts/strip_runner_smoke.py
```
Expected: prints `strip_runner_smoke: running 0 selftest(s)` and `all 0 selftest(s) passed`, exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/strip_runner.py scripts/strip_runner_smoke.py scripts/strip_runner_addresses/.gitkeep
git commit -m "feat(strip-runner): scaffold runner script + smoke harness"
```

---

## Task 2: FILTERS constant + address-list loader

**Files:**
- Modify: `scripts/strip_runner.py`
- Modify: `scripts/strip_runner_smoke.py`

Adds the immutable filter matrix from spec §5 and a small text-file loader with the validation rules from spec §4.

- [ ] **Step 1: Write failing tests in the smoke harness**

In `scripts/strip_runner_smoke.py`, add these selftests after the `SELFTESTS` declaration:

```python
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
```

- [ ] **Step 2: Run the smoke harness and verify failures**

Run:
```bash
cd /home/kk/projects/clients/lot-ledger
python scripts/strip_runner_smoke.py
```
Expected: ImportError (`FILTERS` and `load_addresses` don't exist yet) — or, if you've stubbed them out as `None`, 4 selftests FAIL.

- [ ] **Step 3: Implement FILTERS and `load_addresses` in `strip_runner.py`**

Add to `scripts/strip_runner.py` after the `from typing import Any` import line:

```python
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
```

- [ ] **Step 4: Run smoke and verify all five selftests pass**

Run:
```bash
python scripts/strip_runner_smoke.py
```
Expected: `running 5 selftest(s)`, all `[ok]`, exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/strip_runner.py scripts/strip_runner_smoke.py
git commit -m "feat(strip-runner): FILTERS constant + address list loader (BOM-safe)"
```

---

## Task 3: Log line formatters

**Files:**
- Modify: `scripts/strip_runner.py`
- Modify: `scripts/strip_runner_smoke.py`

Per spec §9. Six small helpers that produce the exact terminal lines KK will scan during multi-hour runs. Pure functions — easy to unit-test.

- [ ] **Step 1: Write failing tests**

Append to `scripts/strip_runner_smoke.py`:

```python
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
```

- [ ] **Step 2: Run smoke and verify fails (ImportError)**

Run:
```bash
python scripts/strip_runner_smoke.py
```
Expected: ImportError for the seven new log_* helpers.

- [ ] **Step 3: Implement the log formatters in `strip_runner.py`**

Append after the `load_addresses` function:

```python
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
    range_str = f"{range_mi:g}mi"
    months_str = f"{months}mo"
    return (
        f"{fmt_ts(now)}   pass {pass_num:>2}/{pass_total}   "
        f"{months_str:>4} / {range_str:<6} "
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
    range_str = f"{range_mi:g}mi"
    months_str = f"{months}mo"
    return (
        f"{fmt_ts(now)}   pass {pass_num:>2}/{pass_total}   "
        f"{months_str:>4} / {range_str:<6} "
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
```

- [ ] **Step 4: Run smoke and verify all 13 selftests pass**

Run:
```bash
python scripts/strip_runner_smoke.py
```
Expected: `running 13 selftest(s)`, all `[ok]`, exits 0.

If a log_pass test fails on column alignment, inspect the actual vs expected output character-by-character. The spec §9 sample is the source of truth for spacing.

- [ ] **Step 5: Commit**

```bash
git add scripts/strip_runner.py scripts/strip_runner_smoke.py
git commit -m "feat(strip-runner): log line formatters per spec §9"
```

---

## Task 4: Pacing helpers

**Files:**
- Modify: `scripts/strip_runner.py`
- Modify: `scripts/strip_runner_smoke.py`

Per spec §7. Three pacing functions: inter-pull (two-band), inter-address (single band), setup-to-first-pull (fixed range).

- [ ] **Step 1: Write failing tests**

Append to `scripts/strip_runner_smoke.py`:

```python
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
```

- [ ] **Step 2: Run smoke and verify fails (ImportError)**

```bash
python scripts/strip_runner_smoke.py
```
Expected: ImportError for the three new pacing helpers.

- [ ] **Step 3: Implement pacing in `strip_runner.py`**

Append after the log formatters:

```python
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
```

- [ ] **Step 4: Run smoke and verify all 17 selftests pass**

```bash
python scripts/strip_runner_smoke.py
```
Expected: `running 17 selftest(s)`, all `[ok]`, exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/strip_runner.py scripts/strip_runner_smoke.py
git commit -m "feat(strip-runner): pacing helpers per spec §7"
```

---

## Task 5: MockPropelioClient for smoke testing

**Files:**
- Modify: `scripts/strip_runner.py`
- Modify: `scripts/strip_runner_smoke.py`

A small in-process mock that mimics the four `PropelioClient` methods strip_runner actually calls. Deterministic output so smoke tests can assert exact counts. Lives in strip_runner.py rather than a separate file — it's small, tightly coupled, and only used by the smoke harness.

- [ ] **Step 1: Write failing tests**

Append to `scripts/strip_runner_smoke.py`:

```python
from scripts.strip_runner import MockPropelioClient


@selftest("MockPropelioClient.login is a no-op")
def _test_mock_login():
    client = MockPropelioClient()
    client.login()  # must not raise
    _assert_true(client._logged_in, "logged_in flag set")


@selftest("MockPropelioClient.find_lead_id returns triple for normal address")
def _test_mock_find_lead_happy():
    client = MockPropelioClient()
    client.login()
    lead_id, sqft, bundle = client.find_lead_id("1234 Main St, Dallas TX 75201")
    _assert_true(lead_id.startswith("lead_"), "lead_id format")
    _assert_true(isinstance(sqft, (int, float)) and sqft > 0, "sqft positive")
    _assert_true(bundle.get("confirmation_key", "").startswith("ck_"), "confirmation_key format")


@selftest("MockPropelioClient.find_lead_id raises for poisoned address")
def _test_mock_find_lead_poison():
    client = MockPropelioClient()
    client.login()
    try:
        client.find_lead_id("POISON_LEAD 0000")
    except RuntimeError as exc:
        _assert_in("poisoned lead", str(exc).lower(), "poison error message")
        return
    raise AssertionError("expected RuntimeError for POISON_LEAD address")


@selftest("MockPropelioClient.add_cma returns envelope with id")
def _test_mock_add_cma():
    client = MockPropelioClient()
    client.login()
    envelope = client.add_cma("lead_x", "ck_y", months=24, range_mi=5.0)
    _assert_true(envelope.get("id", "").startswith("cma_"), "cma id format")


@selftest("MockPropelioClient.search_cma returns deterministic comp count")
def _test_mock_search_cma_deterministic():
    client = MockPropelioClient()
    client.login()
    envelope_a = client.search_cma("lead_x", "cma_y", months=24, range_mi=5.0)
    envelope_b = client.search_cma("lead_x", "cma_y", months=24, range_mi=5.0)
    sales_a = envelope_a.get("data", {}).get("sales", [])
    sales_b = envelope_b.get("data", {}).get("sales", [])
    _assert_eq(len(sales_a), len(sales_b), "same filter returns same comp count")
    _assert_true(len(sales_a) > 0, "search_cma returns at least one comp")
```

- [ ] **Step 2: Run smoke, verify fails**

```bash
python scripts/strip_runner_smoke.py
```
Expected: ImportError on `MockPropelioClient`.

- [ ] **Step 3: Implement `MockPropelioClient` in `strip_runner.py`**

Append after the pacing helpers:

```python
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
        if confirmation_key.endswith("0000000"):
            # POISON_CMA addresses hash to confirmation keys ending in 0000000
            # via the hash modulo above; harder to trigger reliably, so we also
            # check lead_id explicitly below.
            pass
        # Use lead_id as the discriminator for poison_cma trigger:
        if lead_id == "lead_0000001":
            raise RuntimeError("MockPropelioClient: poisoned cma setup")
        # Stale envelope shape — the comps inside don't matter (discarded per spec §5)
        return {
            "id": f"cma_{abs(hash(lead_id)) % 10_000_000:07d}",
            "data": {"sales": [{"address_full": "stale comp ignored"}]},
        }

    def search_cma(
        self, lead_id: str, cma_id: str, months: int, range_mi: float
    ) -> dict[str, Any]:
        if not self._logged_in:
            raise RuntimeError("MockPropelioClient: must call login() first")
        # POISON_FILTER trigger: every search_cma raises (so 3-in-a-row burst guard trips)
        if lead_id == "lead_0000002":
            raise RuntimeError("MockPropelioClient: poisoned filter")
        # Deterministic comp count: more comps for larger range_mi, fewer for tighter
        # range. months tweaks the count slightly so different filter combos produce
        # different (but predictable) comp sets.
        base = int(20 * range_mi) + months
        comps = []
        for i in range(base):
            # Keys overlap across filters within the same lead_id, so net-new
            # decreases as the matrix progresses. Each lead has its own key namespace.
            comps.append(
                {
                    "address_full": f"{lead_id}_{i:03d}_comp_addr",
                    "comp_address_key": f"{lead_id}_comp_{i:03d}",
                    "months": months,
                    "range_mi": range_mi,
                }
            )
        return {"data": {"sales": comps}}


# Convenience seeds for smoke tests
MOCK_POISON_LEAD_ADDRESS = "POISON_LEAD 0000, Dallas TX 75000"
MOCK_POISON_CMA_LEAD_ID = "lead_0000001"  # use a real address that hashes to this
MOCK_POISON_FILTER_LEAD_ID = "lead_0000002"
MOCK_POISON_AUTH_ADDRESS = "POISON_AUTH 0000, Dallas TX 75000"
```

**Note on poison-triggering reality:** the `lead_0000001` / `lead_0000002` triggers rely on specific addresses hashing to those IDs. In Task 7 we'll define dedicated trigger addresses with known hash collisions, OR refactor the mock to use an explicit injection list. We'll address that when the burst-guard test in Task 7 fails the trigger mechanism — fix it then.

- [ ] **Step 4: Run smoke, verify 22 selftests pass**

```bash
python scripts/strip_runner_smoke.py
```
Expected: `running 22 selftest(s)`, all `[ok]`, exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/strip_runner.py scripts/strip_runner_smoke.py
git commit -m "feat(strip-runner): MockPropelioClient for smoke harness"
```

---

## Task 6: `run_address` happy path

**Files:**
- Modify: `scripts/strip_runner.py`
- Modify: `scripts/strip_runner_smoke.py`

The function that drives one address through all 22 Propelio calls. This task only handles the happy path — errors are added in Task 7.

- [ ] **Step 1: Add the `_zero_pacing` context manager to the smoke harness, then write the failing test**

`run_address` calls `time.sleep()` with pacing helpers internally. Without monkeypatching, a single happy-path selftest would take ~10 minutes. Add this context manager once; Tasks 7 and 8 also use it.

Append to `scripts/strip_runner_smoke.py`:

```python
import contextlib
import scripts.strip_runner as _sr_module
from scripts.strip_runner import run_address, AddressOutcome


@contextlib.contextmanager
def _zero_pacing():
    """Monkeypatch pacing helpers to return 0 so selftests run fast.

    Restores originals on exit so other tests (e.g., Task 4's range tests)
    still see the real implementations.
    """
    saved = (
        _sr_module.inter_pull_sleep_seconds,
        _sr_module.inter_address_sleep_seconds,
        _sr_module.setup_to_first_pull_sleep_seconds,
    )
    _sr_module.inter_pull_sleep_seconds = lambda: 0.0
    _sr_module.inter_address_sleep_seconds = lambda: 0.0
    _sr_module.setup_to_first_pull_sleep_seconds = lambda: 0.0
    try:
        yield
    finally:
        (
            _sr_module.inter_pull_sleep_seconds,
            _sr_module.inter_address_sleep_seconds,
            _sr_module.setup_to_first_pull_sleep_seconds,
        ) = saved


@selftest("run_address happy path: 21 search_cma calls, all succeed, addr_total > 0")
def _test_run_address_happy():
    client = MockPropelioClient()
    client.login()
    with _zero_pacing():
        outcome = run_address(
            client=client,
            address="1234 Main St, Dallas TX 75201",
            idx=1,
            total=1,
            mock=True,
        )
    _assert_eq(outcome.status, "complete", "outcome status")
    _assert_eq(outcome.filters_ok, 21, "all 21 filters succeeded")
    _assert_eq(outcome.filters_errored, 0, "no errors")
    _assert_true(outcome.addr_net_new > 0, f"net-new > 0 (got {outcome.addr_net_new})")
    _assert_true(outcome.cma_id is not None, "cma_id captured")
```

- [ ] **Step 2: Run smoke, verify fail (ImportError on `run_address` / `AddressOutcome`)**

```bash
python scripts/strip_runner_smoke.py
```

- [ ] **Step 3: Implement `AddressOutcome` and `run_address` (happy path only)**

Append to `strip_runner.py` after the MockPropelioClient block:

```python
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
# (the underscore-prefixed ones). Importing them directly per spec §12, but
# we wrap them in module-local aliases so tests can stub if needed.
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
```

**Note (spec §12 divergence — confirmed by Copilot plan-review R1):** the spec §12 recommends importing `_classify_propelio_error`, `_parse_cma_envelope_comps`, and `_extract_cma_id` from `deep_pull.py`. We're instead re-implementing all three locally:

- `_parse_cma_envelope_sales` (this task) — replaces `_parse_cma_envelope_comps`
- `_extract_cma_id_from_envelope` (this task) — replaces `_extract_cma_id`
- `_is_auth_class` (Task 7) — replaces `_classify_propelio_error`

Justification: importing from `deep_pull.py` triggers a module-load of the entire scraper subsystem (DB connections via `api.config`, Propelio session imports, etc.) which is unnecessary for smoke-only flows and slows test runs. The functions are 3-5 lines each — drift risk is low. Task 7 adds an equivalence selftest for `_is_auth_class` against representative inputs (401/403/429/throttle-text/benign) so we can detect drift early. Copilot R1 plan-review explicitly agreed with this approach.

- [ ] **Step 4: Run smoke, verify all 23 selftests pass**

```bash
python scripts/strip_runner_smoke.py
```

Note: the happy-path selftest prints all 21 pass-log lines + the setup line — verbose but expected. With `_zero_pacing()` wrapping the call, the test runs in under a second instead of ~10 minutes.

Expected: `running 23 selftest(s)`, all `[ok]`, exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/strip_runner.py scripts/strip_runner_smoke.py
git commit -m "feat(strip-runner): run_address happy path (21-filter sweep)"
```

---

## Task 7: `run_address` error handling + burst-error guard

**Files:**
- Modify: `scripts/strip_runner.py`
- Modify: `scripts/strip_runner_smoke.py`

Per spec §8. Four error classes: auth/rate (raises to exit run), address-level lead-lookup failure, address-level CMA setup failure, filter-level search_cma failure (with 3-consecutive-error guard).

- [ ] **Step 1: Replace the poison-trigger mechanism in MockPropelioClient with explicit injection**

The Task 5 mock uses hash-based triggers which are unreliable. Replace with explicit address-prefix triggers. In `scripts/strip_runner.py`, replace the `MockPropelioClient` class with this version:

```python
class MockPropelioClient:
    """Triggers (address prefix or substring):
        "POISON_LEAD"     -> find_lead_id raises RuntimeError
        "POISON_CMA"      -> add_cma raises RuntimeError
        "POISON_FILTER"   -> every search_cma raises RuntimeError (burst guard trips)
        "POISON_AUTH"     -> find_lead_id raises with "401" in message (auth-class)
        "POISON_BURST"    -> search_cma raises on zero-indexed call indices {0,1,3,4};
                             succeeds elsewhere. Tests burst-guard RESET on success.
                             Sequence: fail-fail-succeed-fail-fail-then-succeed.
    """

    _POISON_BURST_FAIL_INDICES = frozenset({0, 1, 3, 4})

    def __init__(self) -> None:
        self._logged_in = False
        self._burst_counter: dict[str, int] = {}

    def login(self) -> None:
        self._logged_in = True

    def find_lead_id(self, address: str) -> tuple[str, float, dict[str, Any]]:
        if not self._logged_in:
            raise RuntimeError("MockPropelioClient: must call login() first")
        if "POISON_LEAD" in address:
            raise RuntimeError("MockPropelioClient: poisoned lead")
        if "POISON_AUTH" in address:
            raise RuntimeError("Mock 401 unauthorized response from Propelio")
        h = abs(hash(address)) % 10_000_000
        # Encode the address kind into the lead_id so add_cma/search_cma can detect it
        kind = (
            "cma" if "POISON_CMA" in address
            else "filter" if "POISON_FILTER" in address
            else "burst" if "POISON_BURST" in address
            else "ok"
        )
        return (f"lead_{kind}_{h:07d}", 7500.0, {"confirmation_key": f"ck_{kind}_{h:07d}"})

    def add_cma(
        self, lead_id: str, confirmation_key: str, months: int, range_mi: float
    ) -> dict[str, Any]:
        if not self._logged_in:
            raise RuntimeError("MockPropelioClient: must call login() first")
        if "_cma_" in lead_id:
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
        if "_filter_" in lead_id:
            raise RuntimeError("MockPropelioClient: poisoned filter")
        if "_burst_" in lead_id:
            # Use a per-lead_id counter to count zero-indexed search_cma calls
            n = self._burst_counter.get(lead_id, 0)
            self._burst_counter[lead_id] = n + 1
            if n in self._POISON_BURST_FAIL_INDICES:
                raise RuntimeError(f"MockPropelioClient: poisoned burst call #{n}")
            # Fall through to success path for indices 2, 5, 6, 7, ...
        base = int(20 * range_mi) + months
        comps = [
            {
                "address_full": f"{lead_id}_{i:03d}_comp_addr",
                "comp_address_key": f"{lead_id}_comp_{i:03d}",
                "months": months,
                "range_mi": range_mi,
            }
            for i in range(base)
        ]
        return {"data": {"sales": comps}}
```

Also remove the now-unused module-level constants:

```python
# Delete these lines from Task 5:
# MOCK_POISON_LEAD_ADDRESS = ...
# MOCK_POISON_CMA_LEAD_ID = ...
# MOCK_POISON_FILTER_LEAD_ID = ...
# MOCK_POISON_AUTH_ADDRESS = ...
```

- [ ] **Step 2: Write failing error-handling tests (all wrapped in `_zero_pacing()`)**

Append to `scripts/strip_runner_smoke.py`:

```python
from scripts.strip_runner import AuthBlockExit, _is_auth_class


@selftest("run_address: lead lookup failure → skipped (lead lookup failed)")
def _test_run_address_lead_fail():
    client = MockPropelioClient()
    client.login()
    with _zero_pacing():
        outcome = run_address(client=client, address="POISON_LEAD 0000, Dallas TX", idx=1, total=1, mock=True)
    _assert_eq(outcome.status, "skipped", "status")
    _assert_eq(outcome.skip_reason, "lead lookup failed", "skip reason")


@selftest("run_address: cma setup failure → skipped (cma setup failed)")
def _test_run_address_cma_fail():
    client = MockPropelioClient()
    client.login()
    with _zero_pacing():
        outcome = run_address(client=client, address="POISON_CMA 0000, Dallas TX", idx=1, total=1, mock=True)
    _assert_eq(outcome.status, "skipped", "status")
    _assert_eq(outcome.skip_reason, "cma setup failed", "skip reason")


@selftest("run_address: every search_cma fails → burst guard trips after 3 → skipped")
def _test_run_address_burst_guard():
    client = MockPropelioClient()
    client.login()
    with _zero_pacing():
        outcome = run_address(client=client, address="POISON_FILTER 0000, Dallas TX", idx=1, total=1, mock=True)
    _assert_eq(outcome.status, "skipped", "status")
    _assert_eq(outcome.skip_reason, "3 consecutive filter errors", "skip reason")
    _assert_eq(outcome.filters_ok, 0, "no filters succeeded")
    _assert_eq(outcome.filters_errored, 3, "exactly 3 filter errors before escalation")


@selftest("run_address: burst guard RESETS on success (fail-fail-succeed-fail-fail-then-succeed)")
def _test_run_address_burst_reset():
    # POISON_BURST mock raises on call indices 0, 1, 3, 4 (zero-indexed); succeeds elsewhere.
    # Sequence: pass1=fail(consec=1), pass2=fail(consec=2), pass3=success(consec=0 RESET),
    #           pass4=fail(consec=1), pass5=fail(consec=2), pass6+=success.
    # Guard threshold is 3; should NEVER trip. Address completes as "partial".
    client = MockPropelioClient()
    client.login()
    with _zero_pacing():
        outcome = run_address(client=client, address="POISON_BURST 0000, Dallas TX", idx=1, total=1, mock=True)
    _assert_eq(outcome.status, "partial", "status (errors but no skip)")
    _assert_eq(outcome.skip_reason, None, "skip_reason None — burst guard did NOT trip")
    _assert_eq(outcome.filters_errored, 4, "exactly 4 errors (passes 1, 2, 4, 5)")
    _assert_eq(outcome.filters_ok, 17, "17 successful pulls (21 - 4 errors)")


@selftest("run_address: auth-class error raises AuthBlockExit")
def _test_run_address_auth_block():
    client = MockPropelioClient()
    client.login()
    try:
        with _zero_pacing():
            run_address(client=client, address="POISON_AUTH 0000, Dallas TX", idx=1, total=1, mock=True)
    except AuthBlockExit:
        return
    raise AssertionError("expected AuthBlockExit for POISON_AUTH address")


@selftest("_is_auth_class returns True for 401/403/429 and throttle text, False otherwise")
def _test_is_auth_class_equivalence():
    # Local helper replaces deep_pull._classify_propelio_error per §12 divergence note.
    # Inputs cover: status_code-on-exc, response.status_code-on-exc, message-substring matches, benign.
    class _FakeExc(Exception):
        def __init__(self, msg: str = "", status_code: int | None = None) -> None:
            super().__init__(msg)
            if status_code is not None:
                self.status_code = status_code

    _assert_true(_is_auth_class(_FakeExc(status_code=401)), "401")
    _assert_true(_is_auth_class(_FakeExc(status_code=403)), "403")
    _assert_true(_is_auth_class(_FakeExc(status_code=429)), "429")
    _assert_true(_is_auth_class(_FakeExc("rate limit exceeded")), "rate limit text")
    _assert_true(_is_auth_class(_FakeExc("Too Many Requests")), "too many requests text")
    _assert_true(_is_auth_class(_FakeExc("403 forbidden")), "403 in text")
    _assert_true(_is_auth_class(_FakeExc("401 unauthor")), "401 unauthor in text")
    _assert_eq(_is_auth_class(_FakeExc(status_code=500)), False, "500 is not auth")
    _assert_eq(_is_auth_class(_FakeExc("generic timeout")), False, "generic message")
    _assert_eq(_is_auth_class(_FakeExc()), False, "no message no status")
```

- [ ] **Step 3: Run smoke, verify fails**

```bash
python scripts/strip_runner_smoke.py
```
Expected: ImportError on `AuthBlockExit`, then four FAILs on the new error tests.

- [ ] **Step 4: Replace `run_address` with the full error-handling version**

In `scripts/strip_runner.py`, replace the entire `run_address` function (and add `AuthBlockExit` exception class) with:

```python
class AuthBlockExit(SystemExit):
    """Raised when Propelio returns an auth/rate block. Propagates out of the run loop
    so the main() driver can exit with code 2 per spec §8."""

    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = message


def _is_auth_class(exc: Exception) -> bool:
    """Mirror api.propelio.deep_pull._classify_propelio_error semantics."""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status_code = getattr(response, "status_code", None)
    if status_code in (401, 403, 429):
        return True
    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "throttle" in msg or "too many" in msg:
        return True
    if "401" in msg or "403" in msg or "unauthor" in msg or "forbidden" in msg:
        return True
    return False


_BURST_GUARD_THRESHOLD = 3


def run_address(
    *,
    client: Any,
    address: str,
    idx: int,
    total: int,
    mock: bool = False,
) -> AddressOutcome:
    """Run the full 21-filter sweep against one address with full error handling per spec §8."""
    print(log_addr_header(datetime.now(), idx=idx, total=total, address=address))

    # --- Step 1: lead lookup ----------------------------------------------
    try:
        lead_id, _subject_sqft, parcel_bundle = client.find_lead_id(address)
    except Exception as exc:
        if _is_auth_class(exc):
            print(log_addr_skipped(datetime.now(), reason=f"auth block during lead lookup: {_short_err(exc)}"))
            raise AuthBlockExit(f"auth block during lead_lookup for {address}: {exc}") from exc
        print(log_addr_skipped(datetime.now(), reason=f"lead lookup failed: {_short_err(exc)}"))
        return AddressOutcome(
            status="skipped",
            filters_ok=0,
            filters_errored=0,
            addr_net_new=0,
            propelio_returned=0,
            cma_id=None,
            skip_reason="lead lookup failed",
        )

    confirmation_key = parcel_bundle.get("confirmation_key") if isinstance(parcel_bundle, dict) else None

    # --- Step 2: CMA setup ------------------------------------------------
    setup_start = time.monotonic()
    try:
        envelope = client.add_cma(lead_id, confirmation_key, months=FILTERS[0][0], range_mi=FILTERS[0][1])
        cma_id = _extract_cma_id_from_envelope(envelope)
    except Exception as exc:
        if _is_auth_class(exc):
            print(log_setup_fail(datetime.now(), error_summary=_short_err(exc)))
            raise AuthBlockExit(f"auth block during cma setup for {address}: {exc}") from exc
        print(log_setup_fail(datetime.now(), error_summary=_short_err(exc)))
        return AddressOutcome(
            status="skipped",
            filters_ok=0,
            filters_errored=0,
            addr_net_new=0,
            propelio_returned=0,
            cma_id=None,
            skip_reason="cma setup failed",
        )
    setup_elapsed = time.monotonic() - setup_start
    print(log_setup_ok(datetime.now(), cma_id=cma_id, elapsed_s=setup_elapsed))

    # Setup -> first-pull pause
    time.sleep(setup_to_first_pull_sleep_seconds())

    # --- Step 3: 21 filter pulls ------------------------------------------
    addr_seen_keys: set[str] = set()
    addr_total_new = 0
    propelio_returned_total = 0
    filters_ok = 0
    filters_errored = 0
    consecutive_errors = 0

    for pass_num, (months, range_mi) in enumerate(FILTERS, start=1):
        if pass_num > 1:
            time.sleep(inter_pull_sleep_seconds())

        try:
            envelope = client.search_cma(lead_id, cma_id, months=months, range_mi=range_mi)
        except Exception as exc:
            if _is_auth_class(exc):
                raise AuthBlockExit(f"auth block on pass {pass_num} for {address}: {exc}") from exc
            filters_errored += 1
            consecutive_errors += 1
            print(
                log_pass_error(
                    datetime.now(),
                    pass_num=pass_num,
                    pass_total=len(FILTERS),
                    months=months,
                    range_mi=range_mi,
                    error_summary=_short_err(exc),
                )
            )
            if consecutive_errors >= _BURST_GUARD_THRESHOLD:
                print(
                    log_addr_skipped(
                        datetime.now(),
                        reason=f"{consecutive_errors} consecutive filter errors — skipping remaining filters for this address",
                    )
                )
                return AddressOutcome(
                    status="skipped",
                    filters_ok=filters_ok,
                    filters_errored=filters_errored,
                    addr_net_new=addr_total_new,
                    propelio_returned=propelio_returned_total,
                    cma_id=cma_id,
                    skip_reason="3 consecutive filter errors",
                )
            continue

        # Success path
        consecutive_errors = 0
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
            filters_errored=filters_errored,
            addr_net_new=addr_total_new,
        )
    )
    return AddressOutcome(
        status="complete" if filters_errored == 0 else "partial",
        filters_ok=filters_ok,
        filters_errored=filters_errored,
        addr_net_new=addr_total_new,
        propelio_returned=propelio_returned_total,
        cma_id=cma_id,
    )


def _short_err(exc: Exception) -> str:
    """Compact one-line error summary for log lines: ExceptionClass message."""
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__} {msg}".strip()
```

- [ ] **Step 5: Run smoke, verify all 29 selftests pass**

```bash
python scripts/strip_runner_smoke.py
```
Expected: `running 29 selftest(s)`, all `[ok]`, exits 0. With `_zero_pacing()` wrapping every `run_address` call, the four poison-trigger selftests run in under a second total.

- [ ] **Step 6: Commit**

```bash
git add scripts/strip_runner.py scripts/strip_runner_smoke.py
git commit -m "feat(strip-runner): run_address error handling + burst-error guard per spec §8"
```

---

## Task 8: Main loop, summary printer, CLI wiring

**Files:**
- Modify: `scripts/strip_runner.py`
- Modify: `scripts/strip_runner_smoke.py`

The driver that iterates addresses, paces between them, accumulates totals, and prints the end-of-run summary from spec §10. Wires everything into `main()`.

- [ ] **Step 1: Write failing test**

Append to `scripts/strip_runner_smoke.py`:

```python
import io
import contextlib


@selftest("run_all: 3 addresses (2 happy + 1 lead-fail) produce expected summary")
def _test_run_all_mixed():
    from scripts.strip_runner import run_all
    client = MockPropelioClient()
    client.login()
    addresses = [
        "1234 Main St, Dallas TX 75201",
        "POISON_LEAD 0000, Dallas TX",
        "5678 Oak Ave, Dallas TX 75223",
    ]

    buf = io.StringIO()
    with _zero_pacing():
        with contextlib.redirect_stdout(buf):
            summary = run_all(client=client, addresses=addresses, mock=True)

    _assert_eq(summary.addresses_total, 3, "total")
    _assert_eq(summary.addresses_complete, 2, "complete")
    _assert_eq(summary.addresses_partial, 0, "partial")
    _assert_eq(summary.addresses_skipped, 1, "skipped")
    _assert_eq(summary.filter_pulls_total, 42, "21 filters × 2 happy addresses")
    _assert_true(summary.comps_net_new_total > 0, f"net-new > 0 (got {summary.comps_net_new_total})")
    _assert_true(summary.propelio_returned_sum > summary.comps_net_new_total, "returned > net-new")

    output = buf.getvalue()
    _assert_in("=== strip_runner summary ===", output, "summary header in output")
    _assert_in("POISON_LEAD 0000", output, "skipped address listed in output")


@selftest("run_all: auth-block exit prints summary BEFORE re-raising AuthBlockExit (spec §10)")
def _test_run_all_auth_block_summary():
    from scripts.strip_runner import run_all
    client = MockPropelioClient()
    client.login()
    addresses = [
        "1234 Main St, Dallas TX 75201",
        "POISON_AUTH 0000, Dallas TX",   # 2nd address triggers AuthBlockExit
        "9999 Never Reached, Dallas TX",
    ]

    buf = io.StringIO()
    raised = False
    try:
        with _zero_pacing():
            with contextlib.redirect_stdout(buf):
                run_all(client=client, addresses=addresses, mock=True)
    except AuthBlockExit:
        raised = True

    _assert_true(raised, "AuthBlockExit propagated out of run_all")
    output = buf.getvalue()
    # The summary must have been emitted (try/finally in run_all) BEFORE the exit propagated
    _assert_in("=== strip_runner summary ===", output, "summary header printed despite auth-block exit")
    _assert_in("addresses_complete:     1", output, "first address completed before block")
    _assert_in("1234 Main St", output, "completed address visible in output")
```

- [ ] **Step 2: Run smoke, verify fails**

```bash
python scripts/strip_runner_smoke.py
```
Expected: ImportError on `run_all`.

- [ ] **Step 3: Implement `RunSummary`, `run_all`, summary printer, and wire `main()`**

Append to `strip_runner.py`:

```python
@dataclass
class RunSummary:
    addresses_total: int = 0
    addresses_complete: int = 0
    addresses_partial: int = 0
    addresses_skipped: int = 0
    filter_pulls_total: int = 0
    propelio_returned_sum: int = 0
    comps_net_new_total: int = 0
    elapsed_min: float = 0.0
    partial_list: list[tuple[str, int]] = None  # type: ignore[assignment]  # filled in __post_init__
    skipped_list: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.partial_list is None:
            self.partial_list = []
        if self.skipped_list is None:
            self.skipped_list = []


def run_all(*, client: Any, addresses: list[str], mock: bool = False) -> RunSummary:
    """Drive the full address list through run_address, pacing between, accumulating totals.

    Per spec §10, the summary is printed in BOTH normal exit AND auth-block exit
    paths. We use try/finally so the summary always emits — including when an
    AuthBlockExit propagates from run_address mid-loop.
    """
    summary = RunSummary(addresses_total=len(addresses))
    start_monotonic = time.monotonic()

    try:
        for idx, address in enumerate(addresses, start=1):
            if idx > 1:
                time.sleep(inter_address_sleep_seconds())
                print()  # blank line between addresses per spec §9

            outcome = run_address(client=client, address=address, idx=idx, total=len(addresses), mock=mock)

            summary.filter_pulls_total += outcome.filters_ok + outcome.filters_errored
            summary.propelio_returned_sum += outcome.propelio_returned
            summary.comps_net_new_total += outcome.addr_net_new

            if outcome.status == "complete":
                summary.addresses_complete += 1
            elif outcome.status == "partial":
                summary.addresses_partial += 1
                summary.partial_list.append((address, outcome.filters_errored))
            else:  # "skipped"
                summary.addresses_skipped += 1
                summary.skipped_list.append((address, outcome.skip_reason or "unknown"))
    finally:
        summary.elapsed_min = (time.monotonic() - start_monotonic) / 60.0
        _print_summary(summary)

    return summary


def _print_summary(s: RunSummary) -> None:
    print()
    print("=== strip_runner summary ===")
    print(f"addresses_total:        {s.addresses_total:>4}")
    print(f"addresses_complete:     {s.addresses_complete:>4}  (all 21 filters fired)")
    print(f"addresses_partial:      {s.addresses_partial:>4}  (some filters errored)")
    print(f"addresses_skipped:      {s.addresses_skipped:>4}  (setup failed — lead lookup, cma setup, or burst-error guard)")
    print(f"filter_pulls_total:     {s.filter_pulls_total:>4}")
    print(f"propelio_returned_sum: {s.propelio_returned_sum:>5}  (raw comps returned across all pulls, duplicates included)")
    print(f"comps_net_new_total:   {s.comps_net_new_total:>5}  (rows inserted into propelio_comps cache)")
    print(f"elapsed_min:            {s.elapsed_min:>4.0f}")

    if s.partial_list:
        print()
        print("addresses_partial:")
        for addr, errs in s.partial_list:
            print(f"  - {addr}  ({errs} filter{'s' if errs != 1 else ''} errored)")
    if s.skipped_list:
        print()
        print("addresses_skipped:")
        for addr, reason in s.skipped_list:
            print(f"  - {addr}  ({reason})")
```

Then update the `main()` function:

```python
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

    try:
        addresses = load_addresses(args.addresses)
    except (FileNotFoundError, ValueError) as exc:
        print(f"address file error: {exc}", file=sys.stderr)
        return 1

    if args.mock:
        client = MockPropelioClient()
        client.login()
    else:
        from api.propelio.scraper import PropelioClient
        from api.propelio.config import PROPELIO_USERNAME, PROPELIO_PASSWORD
        client = PropelioClient(username=PROPELIO_USERNAME, password=PROPELIO_PASSWORD)
        client.login()

    try:
        run_all(client=client, addresses=addresses, mock=args.mock)
    except AuthBlockExit as exc:
        # run_all's try/finally already printed the summary before re-raising,
        # so we just need to surface the CRITICAL banner and exit with code 2.
        print(f"\nCRITICAL: {exc.message}", file=sys.stderr)
        return 2
    return 0
```

- [ ] **Step 4: Run smoke, verify all 31 selftests pass**

```bash
python scripts/strip_runner_smoke.py
```
Expected: `running 31 selftest(s)`, all `[ok]`, exits 0. The two run_all tests redirect stdout so their output doesn't pollute the test log (auth-block test captures the summary into a buffer and asserts on it).

- [ ] **Step 5: End-to-end CLI dry run with --mock and a tiny address file**

Create a temporary address list and exercise the full CLI:

```bash
cat > /tmp/strip_runner_test_addresses.txt <<'EOF'
# tiny test list
1234 Main St, Dallas TX 75201
5678 Oak Ave, Dallas TX 75223
POISON_CMA 0000, Dallas TX 75000
EOF
cd /home/kk/projects/clients/lot-ledger
python scripts/strip_runner.py --addresses /tmp/strip_runner_test_addresses.txt --mock
```

Expected: full per-pull log lines for the two happy addresses, a `setup: add_cma failed` line for the POISON_CMA address, and an end-of-run summary block showing `addresses_complete: 2`, `addresses_skipped: 1` with `cma setup failed` annotation.

- [ ] **Step 6: Commit**

```bash
git add scripts/strip_runner.py scripts/strip_runner_smoke.py
git commit -m "feat(strip-runner): main loop + run summary + CLI wiring"
```

---

## Task 9: Pilot real-address run (2-3 addresses, real Propelio)

**Files:** No code changes. This task validates the real Propelio path end-to-end with a deliberately tiny address file before the implementing engineer hands the work back to KK for the full 25-address strip.

**Why this task exists** (Copilot plan-review R1): the mock CLI rehearsal in Task 8 exercises code wiring but not the real Propelio session, real `find_lead_id` resolution, real `add_cma` cache behavior, real `search_cma` per-filter response shape, or real `merge_comps_into_global` persistence. A 2-3 address pilot catches integration mismatches in ~30 minutes instead of finding them 2 hours into a 25-address strip.

**Prerequisites:**

- `PROPELIO_USERNAME` and `PROPELIO_PASSWORD` env vars set (check with `env | grep PROPELIO`)
- DB connectivity working (the persistence path writes to `propelio_comps` via `merge_comps_into_global`)
- KK is around or pre-cleared the pilot, since this hits Propelio for real and creates a real CMA

- [ ] **Step 1: Pick 2-3 KK-confirmed test addresses**

Ask KK for 2-3 addresses he has already pulled manually (so we know they have valid leads in Propelio and won't fail on `find_lead_id`). Write them to a pilot file:

```bash
cat > scripts/strip_runner_addresses/pilot_2026_05_14.txt <<'EOF'
# Pilot run — confirm real Propelio path works end-to-end before the full strip.
# Addresses provided by KK.

<address 1>
<address 2>
<address 3>
EOF
```

Replace `<address 1>` etc. with the addresses KK provides. Commit this file — pilot files become part of the run history per spec §1 success criterion.

- [ ] **Step 2: Run the pilot**

```bash
cd /home/kk/projects/clients/lot-ledger
python scripts/strip_runner.py --addresses scripts/strip_runner_addresses/pilot_2026_05_14.txt
```

Expected runtime: ~25-35 minutes (3 addresses × ~10-12 min each, including the 4-hour-scaled inter-pull pacing).

- [ ] **Step 3: Verify the pilot output**

While the run is in progress:

- Per-pull log lines should appear with real `returned` counts (typically 10-300 depending on filter) and `new` counts (high on early passes, dropping toward 0 on tight final passes)
- `setup: add_cma ok cma_id=cma_XXXXXXX (Ns)` lines should show real CMA IDs and elapsed times (typically 2-5s)
- No Python tracebacks should hit stderr

After the run completes:

- End-of-run summary block should show `addresses_complete: 3` (or `2` + `1 partial` if one address had a filter error)
- `comps_net_new_total` should be > 0 (the real cache was warmed)
- Spot-check the global cache:

```sql
-- From psql against the dev DB
SELECT COUNT(*) FROM propelio_comps WHERE source = 'strip_runner';
SELECT source, MIN(created_at), MAX(created_at), COUNT(*)
FROM propelio_comps
WHERE source = 'strip_runner'
GROUP BY source;
```

Both queries should return non-zero counts dated within the run window.

- [ ] **Step 4: Commit the pilot address file**

```bash
git add scripts/strip_runner_addresses/pilot_2026_05_14.txt
git commit -m "chore(strip-runner): pilot 2026-05-14 address list — 3 real addresses"
```

If the pilot revealed any code defect, STOP here and fix it before proceeding. The pilot's whole point is to surface real-world breakage before the full strip.

---

## Task 10: Push and queue for Copilot code review

**Files:** No code changes.

- [ ] **Step 1: Verify the smoke harness is clean and the diff looks right**

```bash
cd /home/kk/projects/clients/lot-ledger
python scripts/strip_runner_smoke.py
git log --oneline feat/strip-runner ^develop
git diff develop..feat/strip-runner --stat
```

Expected:
- All 31 selftests pass
- 11-12 commits (3 spec commits + 1 plan commit + 1 plan-v1.1 commit + 7 implementation commits + 1 pilot file commit)
- Diff stat shows `scripts/strip_runner.py`, `scripts/strip_runner_smoke.py`, `scripts/strip_runner_addresses/.gitkeep`, `scripts/strip_runner_addresses/pilot_2026_05_14.txt`, `docs/propelio/STRIP_RUNNER_SPEC.md`, `docs/propelio/STRIP_RUNNER_PLAN.md`

- [ ] **Step 2: Push the branch**

```bash
git push
```

- [ ] **Step 3: Draft the Copilot code-review prompt for KK**

The prompt should ask Copilot to:
1. Verify the implementation matches spec v1.3 §1-§14 line by line
2. Confirm the `MockPropelioClient` accurately mimics the real `PropelioClient` surface for the four methods strip_runner uses (`login`, `find_lead_id`, `add_cma`, `search_cma`); spot-check signatures against `api/propelio/scraper.py`
3. Audit the error-handling paths against spec §8: auth-class detection (`_is_auth_class`), burst-guard reset-on-success (verified by `_test_run_address_burst_reset`), partial vs complete vs skipped status assignment, summary emission on auth-block (try/finally in `run_all`)
4. Confirm `_persist_pull_real` matches `deep_pull.py`'s comp-persistence path (parcel-match exception fallback in particular)
5. Surface any divergence from spec v1.3 — including the three intentional re-implementations of `deep_pull.py` helpers per Task 6's §12 note
6. Note the Task 9 pilot run results when reporting: if the pilot caught a real-world issue, that's important context for the code review
7. Flag any non-obvious risk before KK runs the full 25-address strip

Hand the prompt and the branch+commit ref to KK.

---

## Self-Review Notes (for the implementing engineer)

If you're implementing this plan and you hit any of the following, STOP and surface to KK:

1. **A selftest fails after step 4 of any task** — that means the test or the implementation diverges from spec. Don't paper over it; figure out which one is right.
2. **The pacing helpers' random output makes selftests flaky** — Task 4's ratio test allows 0.70-0.90 slack; if it fails repeatedly, increase the sample size before tightening or loosening the bounds.
3. **The mock client's hash-based triggers (Task 5) collide with a real address** in the smoke test — replace with the explicit-prefix triggers from Task 7 immediately; that's the whole reason Task 7 refactors them.
4. **`merge_comps_into_global` import path breaks** — the spec assumes `api.propelio.archive.merge_comps_into_global` exists. If it's moved/renamed, fix the import in `_persist_pull_real` (Task 6) and note it for the Copilot review.
5. **Real-run rehearsal (Task 8 step 5) hits Propelio for real instead of mock** — make sure `--mock` flag is set. If you accidentally hit real Propelio, expect a CMA to be created on KK's account; let him know immediately.
6. **Task 9 pilot reveals a defect** — STOP before pushing for code review. Fix the defect, re-run the pilot, only proceed to Task 10 when the pilot is clean.

---

## Copilot plan-review — outcomes (2026-05-14)

### Round 1 (v1 → v1.1)

One BLOCKER, two IMPORTANTs, two nice-to-haves. All addressed in v1.1:

| # | Task | Severity | Resolution |
|---|---|---|---|
| 1 | Task 6/7 | BLOCKER | Pacing helpers in `run_address` would make selftests take ~10 min each without monkeypatching. Added `_zero_pacing()` context manager in Task 6 and wrapped every `run_address` call in Tasks 6/7/8 selftests. |
| 2 | Task 8 | IMPORTANT | `run_all` returned on `AuthBlockExit` without printing summary, violating spec §10. Wrapped main loop in try/finally so summary always prints before re-raising. Added explicit `_test_run_all_auth_block_summary` selftest. |
| 3 | Task 7 | IMPORTANT | Burst-guard reset-on-success was implemented but untested. Redefined `POISON_BURST` mock trigger to fail on call indices {0,1,3,4} (instead of fail-first-3) so the reset path is actually exercised. Added `_test_run_address_burst_reset` selftest. |
| 4 | Task 6 §12 note | NICE-TO-HAVE | Note said `_classify_propelio_error` would be imported lazily in Task 7, but Task 7 re-implements it. Fixed the wording. Added `_test_is_auth_class_equivalence` selftest covering 401/403/429/throttle-text/benign inputs to catch drift. |
| 5 | Task 2 | NICE-TO-HAVE | BOM-prefixed first line would parse as malformed address. Switched `load_addresses` to `encoding="utf-8-sig"`. Added `_test_load_addresses_bom` selftest. |

Plus one direct-answer add: **Task 9 (new) — pilot real-address run** between mock CLI rehearsal (Task 8) and final push (now Task 10). Validates real Propelio path on 2-3 addresses before the full 25-address strip.

Copilot R1 verdict on v1: not implementation-ready (1 BLOCKER + 2 IMPORTANT). With v1.1 fixes folded in, the plan should be implementation-ready pending R2 verification.

---

## Spec Coverage Check (Claude self-review)

| Spec section | Implementing task(s) | Notes |
|---|---|---|
| §1 Purpose | Task 1, 6 | Architecture comment at top of strip_runner.py |
| §2 Non-goals | All tasks | No FSM, no breaker, no reconcile — verified by absence |
| §3 Architecture / invocation | Task 1, 8 | Repo-root requirement documented in module docstring |
| §4 Address list format | Task 2 | `load_addresses` parser with `utf-8-sig` for BOM safety (v1.1) |
| §5 Filter matrix | Task 2 | `FILTERS` constant with `assert len == 21` |
| §6 Per-address execution | Task 6, 7 | `run_address` function with Option A (add_cma setup → 21 search_cma); reset-on-success now explicitly tested (v1.1) |
| §7 Pacing | Task 4 | Three pacing helpers + run-1 floor at 15s + future-tuning principle in comments |
| §8 Error handling | Task 7 | AuthBlockExit + address-level skip + filter-level continue + burst guard with reset (now tested via `_test_run_address_burst_reset`, v1.1) |
| §9 Per-pull terminal logging | Task 3, 6, 7 | Seven log_* formatters + the setup line + pass line + footer |
| §10 End-of-run summary | Task 8 | `_print_summary` with three-skip-path annotation; emitted via `run_all` try/finally on BOTH normal exit AND auth-block exit (v1.1 fix) |
| §11 What this does NOT use | All tasks | No marathon imports anywhere |
| §12 Dependencies | Task 6 + Task 7 | Import `PropelioClient`, `merge_comps_into_global`, `match_comps_to_parcels`, `_parse_property`; local re-impl of `_extract_cma_id_from_envelope`, `_parse_cma_envelope_sales`, and `_is_auth_class` (avoids loading the scraper subsystem on smoke runs; equivalence test in Task 7 covers drift risk) |
| §13 Branch and commit | Task 10 | Final push step |
| §14 Deferred | N/A | Items intentionally not implemented |
| **Pilot validation** | Task 9 (new in v1.1) | Real-Propelio 2-3 address pilot before the full 25-address strip; catches integration mismatches the mock can't surface |

**Divergence from spec §12 — confirmed by Copilot plan-review R1:** locally re-implementing `_classify_propelio_error` → `_is_auth_class`, `_parse_cma_envelope_comps` → `_parse_cma_envelope_sales`, and `_extract_cma_id` → `_extract_cma_id_from_envelope`. Reason: importing from `api.propelio.deep_pull` triggers a module-load of the entire scraper subsystem (DB connections, etc.) which we want to avoid for smoke runs. The functions are 3-5 lines each, and Task 7's `_test_is_auth_class_equivalence` selftest covers the drift risk explicitly. Copilot R1 plan-review agreed with this approach.

---

## Execution

Plan complete and saved to `docs/propelio/STRIP_RUNNER_PLAN.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task with the spec + relevant slice of this plan. Two-stage review per task. Fast iteration.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`. Batch execution with checkpoints for review.

**Or:** send this plan to Copilot for plan review first (matching the spec → critique → adjust → code → verify loop), then execute. KK's normal pattern per `feedback_copilot_iteration_loop.md`.
