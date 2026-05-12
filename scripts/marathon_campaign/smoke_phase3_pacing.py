# scripts/marathon_campaign/smoke_phase3_pacing.py
#
# Role: Pure-CPU smoke test for Phase 3 pacing distributions.
#
# Connects to:
#   scripts/marathon_campaign/pacing.py - pacing helper functions under test

from __future__ import annotations

from pathlib import Path
import statistics
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.marathon_campaign.pacing import inter_seed_pause_seconds, maybe_take_break


def _check_inter_seed() -> tuple[bool, str]:
    samples = [inter_seed_pause_seconds() for _ in range(1000)]
    mn = min(samples)
    mx = max(samples)
    mean = statistics.mean(samples)

    ok = True
    reasons = []
    if mn < 30.0:
        ok = False
        reasons.append(f"min {mn:.2f} < 30")
    if mx > 180.0:
        ok = False
        reasons.append(f"max {mx:.2f} > 180")
    if not (70.0 <= mean <= 80.0):
        ok = False
        reasons.append(f"mean {mean:.2f} outside [70,80]")

    msg = f"inter_seed: mean={mean:.2f}s min={mn:.2f}s max={mx:.2f}s"
    if reasons:
        msg += " | " + "; ".join(reasons)
    return ok, msg


def _check_under_threshold() -> tuple[bool, str]:
    results = [maybe_take_break(seconds_since_last_break=90.0 * 60.0) for _ in range(10000)]
    breaks = sum(1 for item in results if item is not None)
    ok = breaks == 0
    msg = f"under_threshold: breaks={breaks}/10000"
    if not ok:
        msg += " | expected 0"
    return ok, msg


def _check_break_distribution() -> tuple[bool, str]:
    results = [maybe_take_break(seconds_since_last_break=120.0 * 60.0) for _ in range(10000)]
    breaks = [item for item in results if item is not None]
    break_count = len(breaks)

    if break_count == 0:
        return False, "break_distribution: no breaks returned at 120min"
    if break_count == 10000:
        return False, "break_distribution: all calls returned breaks at 120min"

    type_counts = {"short": 0, "medium": 0, "long": 0}
    for _, break_type in breaks:
        type_counts[break_type] += 1

    short_ratio = type_counts["short"] / break_count
    medium_ratio = type_counts["medium"] / break_count
    long_ratio = type_counts["long"] / break_count

    ok = True
    reasons = []
    if abs(short_ratio - 0.70) > 0.05:
        ok = False
        reasons.append(f"short ratio {short_ratio:.3f} not within 0.70±0.05")
    if abs(medium_ratio - 0.25) > 0.05:
        ok = False
        reasons.append(f"medium ratio {medium_ratio:.3f} not within 0.25±0.05")
    if abs(long_ratio - 0.05) > 0.05:
        ok = False
        reasons.append(f"long ratio {long_ratio:.3f} not within 0.05±0.05")

    msg = (
        "break_distribution: "
        f"breaks={break_count}/10000 "
        f"short={type_counts['short']}({short_ratio:.3f}) "
        f"medium={type_counts['medium']}({medium_ratio:.3f}) "
        f"long={type_counts['long']}({long_ratio:.3f})"
    )
    if reasons:
        msg += " | " + "; ".join(reasons)
    return ok, msg


def main() -> None:
    checks = [
        ("inter_seed", _check_inter_seed),
        ("under_threshold", _check_under_threshold),
        ("break_distribution", _check_break_distribution),
    ]

    failed = 0
    for name, fn in checks:
        ok, msg = fn()
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {msg}")
        if not ok:
            failed += 1

    if failed:
        print(f"pacing_smoke: FAIL ({failed} checks failed)")
        sys.exit(1)

    print("pacing_smoke: PASS")


if __name__ == "__main__":
    main()
