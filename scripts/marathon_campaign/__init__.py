# scripts/marathon_campaign/__init__.py
#
# Role: Marathon campaign package exports for FSM and resilience helpers.
#
# Connects to:
#   scripts/marathon_campaign/state.py            - transition guard + FSM updates
#   scripts/marathon_campaign/circuit_breaker.py  - anti-bot breaker persistence
#   scripts/marathon_campaign/cooldown.py         - cooldown wait loop with timeout guard

from .state import ALLOWED_TRANSITIONS, IllegalStateTransition, transition
from .circuit_breaker import CircuitBreaker
from .cooldown import wait_for_cooldown_or_exit
from .pacing import inter_seed_pause_seconds, maybe_take_break

__all__ = [
    "ALLOWED_TRANSITIONS",
    "IllegalStateTransition",
    "transition",
    "CircuitBreaker",
    "wait_for_cooldown_or_exit",
    "inter_seed_pause_seconds",
    "maybe_take_break",
]
