# scripts/marathon_campaign/alerts.py
#
# Role: Minimal alert routing stub for marathon runner operational signals.
#
# Connects to:
#   scripts/marathon_campaign/runner.py - emits warning/critical alert calls
#   scripts/marathon_campaign/events.py - records alert events in JSON stream

from __future__ import annotations

import os
import sys
from typing import Any

from .events import emit_event


def alert(severity: str, message: str, **context: Any) -> None:
    """Route an alert by severity via stderr and structured events.

    INFO / WARNING / ERROR / CRITICAL
    """
    sev = str(severity or "INFO").strip().upper()
    context_str = " ".join(f"{k}={v}" for k, v in context.items())
    prefix = f"[ALERT severity={sev}] {message}"
    line = prefix if not context_str else f"{prefix} {context_str}"
    print(line, file=sys.stderr, flush=True)

    emit_event("alert", severity=sev, message=str(message), **context)

    email_enabled = str(os.getenv("MARATHON_ALERT_EMAIL_ENABLED") or "").strip() == "1"
    if email_enabled and sev in {"WARNING", "ERROR", "CRITICAL"}:
        subject = f"[Marathon {sev}] {message}"
        body = f"message={message} context={context}"
        print(f"would email: subject={subject} body={body}", file=sys.stderr, flush=True)
