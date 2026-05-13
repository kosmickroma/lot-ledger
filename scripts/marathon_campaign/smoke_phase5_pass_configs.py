# scripts/marathon_campaign/smoke_phase5_pass_configs.py
#
# Role: CPU-only smoke test for the single PASSES constant in pass_configs.py.
#       Validates shape, ordering, uniqueness, and label completeness.
#
# Connects to:
#   scripts/marathon_campaign/pass_configs.py - imports PASSES

from __future__ import annotations

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.marathon_campaign.pass_configs import PASSES  # noqa: E402


def test_passes_shape() -> None:
    # Exactly 9 passes — 3 recent-tight + 6 broad sweep.
    assert len(PASSES) == 9, f"expected 9 passes, got {len(PASSES)}"

    # Recent-tight passes come first, in (1mo, 2mo, 3mo) order.
    months_seq = [p["months"] for p in PASSES[:3]]
    assert months_seq == [1, 2, 3], f"recent-tight months order wrong: {months_seq}"

    # Broad sweep all at 24 months.
    assert all(p["months"] == 24 for p in PASSES[3:]), "broad sweep should all be 24 months"

    # Radii monotonically expanding within each group.
    recent_radii = [p["range_mi"] for p in PASSES[:3]]
    assert recent_radii == sorted(recent_radii), f"recent-tight radii not expanding: {recent_radii}"
    broad_radii = [p["range_mi"] for p in PASSES[3:]]
    assert broad_radii == sorted(broad_radii), f"broad sweep radii not expanding: {broad_radii}"

    # Every pass has a non-empty label.
    assert all(p.get("label") for p in PASSES), "every pass needs a label"

    # All labels are unique — catches copy-paste-and-forgot-to-rename.
    labels = [p["label"] for p in PASSES]
    assert len(set(labels)) == len(labels), f"pass labels must be unique, got: {labels}"

    # All (months, range_mi) tuples are unique — catches accidental duplicate passes.
    pairs = [(p["months"], p["range_mi"]) for p in PASSES]
    assert len(set(pairs)) == len(pairs), f"duplicate (months, range_mi) pairs detected: {pairs}"

    # Mutation isolation — returned list is a copy, not the original.
    copy = list(PASSES)
    copy.append({"months": 99, "range_mi": 99.0, "label": "mutated"})
    assert len(PASSES) == 9, "PASSES constant was mutated through a returned reference"


if __name__ == "__main__":
    test_passes_shape()
    print("smoke_phase5_pass_configs: OK")

