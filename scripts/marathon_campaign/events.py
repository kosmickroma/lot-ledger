# scripts/marathon_campaign/events.py
#
# Role: Structured JSON event emission for marathon runner observability.
#
# Connects to:
#   scripts/marathon_campaign/runner.py - emits run/seed lifecycle events
#   scripts/marathon_campaign/alerts.py - emits alert events

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


def _ts_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_campaign_fragment(campaign: object) -> str:
    raw = str(campaign or "unknown").strip() or "unknown"
    return re.sub(r"[^A-Za-z0-9_-]+", "_", raw)


def emit_event(event_type: str, **fields: Any) -> None:
    """Write a single JSON event line to stderr and optional campaign log file."""
    payload: dict[str, Any] = {
        "ts": _ts_utc(),
        "event": str(event_type or "unknown"),
        **fields,
    }
    line = json.dumps(payload, separators=(",", ":"), default=str)

    print(line, file=sys.stderr, flush=True)

    log_dir = str(os.getenv("MARATHON_LOG_DIR") or "").strip()
    campaign = fields.get("campaign")
    if not log_dir or campaign is None:
        return

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    campaign_frag = _safe_campaign_fragment(campaign)
    path = Path(log_dir) / f"marathon_{campaign_frag}_{day}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        # File tee is best-effort; stderr event is the source of truth.
        pass
