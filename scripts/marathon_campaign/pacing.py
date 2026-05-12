# scripts/marathon_campaign/pacing.py
#
# Role: Session pacing helpers for inter-seed pauses and natural breaks.
#
# Connects to:
#   scripts/marathon_campaign runner logic - calls pause/break helpers between seeds

from __future__ import annotations

import random

# Inter-seed pacing distribution.
INTER_SEED_FOCUSED_PROB = 0.80
INTER_SEED_FOCUSED_MIN_S = 30.0
INTER_SEED_FOCUSED_MAX_S = 90.0
INTER_SEED_DISTRACTED_MIN_S = 90.0
INTER_SEED_DISTRACTED_MAX_S = 180.0

# Break eligibility and trigger settings.
BREAK_ELIGIBILITY_MIN_S = 100.0 * 60.0
BREAK_ELIGIBILITY_MAX_S = 130.0 * 60.0
BREAK_TRIGGER_PROB = 0.50

# Break type distribution.
BREAK_SHORT_PROB = 0.70
BREAK_MEDIUM_PROB = 0.25
BREAK_LONG_PROB = 0.05

BREAK_SHORT_MIN_S = 8.0 * 60.0
BREAK_SHORT_MAX_S = 15.0 * 60.0
BREAK_MEDIUM_MIN_S = 25.0 * 60.0
BREAK_MEDIUM_MAX_S = 40.0 * 60.0
BREAK_LONG_MIN_S = 50.0 * 60.0
BREAK_LONG_MAX_S = 75.0 * 60.0


def inter_seed_pause_seconds() -> float:
    """Return inter-seed pause duration using the 80/20 two-band distribution."""
    if random.random() < INTER_SEED_FOCUSED_PROB:
        return random.uniform(INTER_SEED_FOCUSED_MIN_S, INTER_SEED_FOCUSED_MAX_S)
    return random.uniform(INTER_SEED_DISTRACTED_MIN_S, INTER_SEED_DISTRACTED_MAX_S)


def maybe_take_break(seconds_since_last_break: float) -> tuple[float, str] | None:
    """Return (duration_seconds, break_type) or None when no break should occur.

    break_type is one of: short, medium, long.
    """
    elapsed_s = float(seconds_since_last_break)
    if elapsed_s < BREAK_ELIGIBILITY_MIN_S:
        return None

    eligibility_threshold_s = random.uniform(BREAK_ELIGIBILITY_MIN_S, BREAK_ELIGIBILITY_MAX_S)
    if elapsed_s < eligibility_threshold_s:
        return None

    if random.random() >= BREAK_TRIGGER_PROB:
        return None

    roll = random.random()
    if roll < BREAK_SHORT_PROB:
        return random.uniform(BREAK_SHORT_MIN_S, BREAK_SHORT_MAX_S), "short"
    if roll < BREAK_SHORT_PROB + BREAK_MEDIUM_PROB:
        return random.uniform(BREAK_MEDIUM_MIN_S, BREAK_MEDIUM_MAX_S), "medium"
    return random.uniform(BREAK_LONG_MIN_S, BREAK_LONG_MAX_S), "long"
