"""tests/test_sse_connect_resync.py
Role: Regression guard for KK's "watchdog reconnect doesn't recover
missed events" investigation 2026-06-06.

PostgreSQL LISTEN/NOTIFY does not buffer events for disconnected
subscribers. When a client EventSource goes zombie (Edge tab throttling,
network blip, watchdog force-close), every pg_notify fired during the
disconnected window is lost. The Sprint 3 spec covered the case where
the SERVER-side LISTEN connection reconnects (broadcasts resync to all
subscribers via _broadcast_resync_to_all in api/sse.py:76). It did NOT
cover the much more common case of CLIENT-side EventSource reconnect.

Symptom: tab B was watching for an area_meta_change to update its gold
star. KK's tab was hidden long enough for the SSE to die silently. The
watchdog force-reconnected after detecting 212s of silence (KK report
2026-06-06), but the area_meta_change event from the writer's session
had been fired during the dead window and was permanently lost. Tab B's
star stayed on the old subject.

Fix: stream_area_events emits a synthetic 'resync' event immediately
after 'connected' so the client refetches state on every (re)connect.
Frontend resync handler already calls _sseRefetchArea -> _reloadSavedResources.

Connects to: api/main.py stream_area_events event_generator
"""
from __future__ import annotations

import re
from pathlib import Path


MAIN_PY = Path(__file__).resolve().parent.parent / "api" / "main.py"


def _read() -> str:
    return MAIN_PY.read_text()


def test_event_generator_emits_resync_immediately_after_connected() -> None:
    """The synthetic resync must follow the connected event WITHIN
    event_generator so every (re)connect triggers a state refetch."""
    src = _read()
    pat = re.compile(
        r'yield \{\s*"event": "connected",.*?\}.*?'
        r'yield \{\s*"event": "resync",\s*"data": _json\.dumps\(\{\s*'
        r'"area_id": area_id,\s*"reason": "client_connect"',
        re.DOTALL,
    )
    assert pat.search(src), (
        "event_generator must yield a synthetic 'resync' event right "
        "after 'connected' so any EventSource (re)connect causes the "
        "client to refetch state. Without this, events fired during a "
        "zombie/disconnected window are permanently lost."
    )


def test_resync_payload_carries_client_connect_reason() -> None:
    """Distinguish the synthetic on-connect resync from the
    _broadcast_resync_to_all 'listener_reconnect' resync (api/sse.py:87)
    so the frontend / future analytics can tell them apart."""
    src = _read()
    assert '"reason": "client_connect"' in src, (
        "Connect-time resync must use reason=client_connect to distinguish "
        "it from the server-side listener_reconnect resync."
    )


def test_resync_must_NOT_be_inside_the_while_true_loop() -> None:
    """The resync emit is one-shot: it fires once per stream open. If it
    accidentally lived inside the while True dispatch loop, every event
    would trigger a refetch — a runaway client refresh."""
    src = _read()
    # Find the event_generator function body
    idx = src.find("async def event_generator():")
    assert idx > 0
    # Find the while True: marker after that
    while_idx = src.find("while True:", idx)
    assert while_idx > idx
    # Find the resync yield
    resync_idx = src.find('"event": "resync"', idx)
    assert resync_idx > idx, "resync yield missing from event_generator"
    assert resync_idx < while_idx, (
        "resync yield must be BEFORE the while True loop. If it landed "
        "inside the loop, every relayed event would re-trigger a client "
        "refetch (death spiral)."
    )
