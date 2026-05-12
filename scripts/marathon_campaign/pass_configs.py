# scripts/marathon_campaign/pass_configs.py
#
# Role: Pass configuration presets by seed density class for marathon deep-pull jobs.
#
# Connects to:
#   scripts/marathon_campaign/runner.py - chooses pass set per seed and passes to deep pull

from __future__ import annotations

PASSES_URBAN_SUBURBAN = [
    {"months": 24, "range_mi": 0.25, "label": "tightest"},
    {"months": 24, "range_mi": 0.5, "label": "blocks"},
    {"months": 24, "range_mi": 1.0, "label": "neighborhood"},
    {"months": 24, "range_mi": 2.0, "label": "broader"},
    {"months": 24, "range_mi": 5.0, "label": "wider"},
    {"months": 24, "range_mi": 10.0, "label": "rural_fallback"},
]

PASSES_RURAL = [
    # NO 0.25 pass - too tight for rural, near-zero returns.
    {"months": 24, "range_mi": 0.5, "label": "blocks"},
    {"months": 24, "range_mi": 1.0, "label": "neighborhood"},
    {"months": 24, "range_mi": 2.0, "label": "broader"},
    {"months": 24, "range_mi": 5.0, "label": "wider"},
    {"months": 24, "range_mi": 10.0, "label": "rural_fallback"},
]


def passes_for_density_class(density_class: str) -> list[dict]:
    """Return pass config for a seed's density class.

    Urban + suburban: 6-pass full sweep.
    Rural: 5-pass (skip 0.25mi tightest).
    Unknown class: default to urban/suburban (fail-safe, more aggressive).
    """
    cls = str(density_class or "").strip().lower()
    if cls == "rural":
        return list(PASSES_RURAL)
    return list(PASSES_URBAN_SUBURBAN)
