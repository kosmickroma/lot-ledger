# scripts/marathon_campaign/cooldown.py
#
# Role: Cooldown waiting loop with operator-configurable max-total wait
#       guard for marathon campaign circuit breaker pauses.
#
# Connects to:
#   scripts/marathon_campaign/circuit_breaker.py - uses CircuitBreaker state
#

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
from typing import Awaitable, Callable

from .circuit_breaker import CircuitBreaker


logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def wait_for_cooldown_or_exit(
    *,
    run_id: int,
    circuit_breaker: CircuitBreaker,
    exit_emergency: Callable[[int, str], None] | None = None,
    send_alert_email: Callable[[str, str], None] | None = None,
) -> bool:
    """Wait for breaker cooldown to clear.

    Returns True when cooldown clears naturally.
    Returns False when max-total-wait guard triggers and exit callback is invoked.
    """
    max_wait_hours_raw = os.environ.get("MARATHON_MAX_COOLDOWN_WAIT_HOURS", "0").strip() or "0"
    try:
        max_wait_hours = float(max_wait_hours_raw)
    except ValueError:
        max_wait_hours = 0.0

    wait_started_at = _utcnow()
    logger.info("Circuit breaker open, entering cooldown wait")

    while circuit_breaker.is_open():
        if max_wait_hours > 0:
            waited_hours = (_utcnow() - wait_started_at).total_seconds() / 3600.0
            if waited_hours >= max_wait_hours:
                message = (
                    "Circuit breaker cooldown exceeded "
                    f"MARATHON_MAX_COOLDOWN_WAIT_HOURS={max_wait_hours}h. "
                    "Exiting session for operator review."
                )
                logger.critical(message)
                if send_alert_email is not None:
                    send_alert_email(
                        "Marathon: cooldown timeout exceeded",
                        f"Run {run_id} exited after {waited_hours:.1f}h in cooldown",
                    )
                if exit_emergency is not None:
                    exit_emergency(int(run_id), "cooldown_timeout")
                return False

        if circuit_breaker.cooldown_until is None:
            await asyncio.sleep(60)
            continue

        cooldown_left = (circuit_breaker.cooldown_until - _utcnow()).total_seconds()
        if cooldown_left <= 0:
            await asyncio.sleep(60)
        else:
            sleep_s = min(max(cooldown_left + 30, 0), 600)
            logger.info("Cooldown %.0fs remaining", sleep_s)
            await asyncio.sleep(sleep_s)

    logger.info("Circuit breaker reset, resuming campaign")
    return True
