# scripts/marathon_campaign/pass_configs.py
#
# Role: Pass configuration for marathon deep-pull jobs — single flat PASSES constant.
#       Density class no longer drives pass selection; all seeds use the same sweep.
#
# Connects to:
#   scripts/marathon_campaign/runner.py - imports passes_for_density_class (stub)

from __future__ import annotations

# 9 passes: 3 recent-tight (short lookback, tight radius) followed by
# 6 broad sweep (24-month, expanding radius). Order matters — runner
# executes passes in sequence and stops early when saturated.
PASSES: list[dict] = [
    # --- recent-tight (catch active market activity) ---
    {"months": 1,  "range_mi": 0.25, "label": "recent_1mo_tight"},
    {"months": 2,  "range_mi": 0.5,  "label": "recent_2mo_blocks"},
    {"months": 3,  "range_mi": 1.0,  "label": "recent_3mo_neighborhood"},
    # --- broad sweep (ambient saturation) ---
    {"months": 24, "range_mi": 0.25, "label": "tightest"},
    {"months": 24, "range_mi": 0.5,  "label": "blocks"},
    {"months": 24, "range_mi": 1.0,  "label": "neighborhood"},
    {"months": 24, "range_mi": 2.0,  "label": "broader"},
    {"months": 24, "range_mi": 5.0,  "label": "wider"},
    {"months": 24, "range_mi": 10.0, "label": "rural_fallback"},
]


def passes_for_density_class(_density_class: str) -> list[dict]:
    """Return the pass list for a seed.

    Density class no longer drives pass selection — all seeds use the same
    PASSES constant. The parameter is retained so caller sites in runner.py
    need no change.
    """
    return list(PASSES)
