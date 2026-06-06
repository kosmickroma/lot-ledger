"""tests/test_sse_area_meta_broadcast.py
Role: Regression guard for KK debug 2026-06-06: "originator change on
shared area never propagated to user B until eventually."

Before this fix, the PUT /api/areas/{area_id} endpoint only fired
pg_notify for the blob_explode case (filter_state in body). Name +
originator changes were silent. User B only saw them when their tab
regained visibility (via the visibilitychange handler in map.js:198)
or coincidentally when another SSE event arrived.

Fix:
  - PUT /api/areas/{area_id} fires pg_notify with type=area_meta_change
    when name OR originator_parcel_* is in the update body
  - stream_area_events maps area_meta_change to a named SSE event
  - Frontend addEventListener("area_meta_change", _reloadSavedResources)
  - _handleSseFieldChange ignores area_meta_change (dedicated listener
    handles it)

Connects to: api/main.py update_saved_area, stream_area_events;
frontend/map.js _openSseStream, _handleSseFieldChange
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "api" / "main.py"
MAP_JS = ROOT / "frontend" / "map.js"


def _read(path: Path) -> str:
    return path.read_text()


# ---- Backend ------------------------------------------------------------


def test_put_area_fires_pg_notify_on_name_or_originator_change() -> None:
    src = _read(MAIN_PY)
    pat = re.compile(
        r"name_or_originator_changed = \(\s*"
        r"request\.name is not None\s*"
        r"or request\.originator_parcel_county is not None\s*"
        r"or request\.originator_parcel_account_num is not None\s*"
        r"\).*?"
        r"if name_or_originator_changed:.*?"
        r'pg_notify\(.saved_area_filter_changes..*?'
        r'"type": "area_meta_change"',
        re.DOTALL,
    )
    assert pat.search(src), (
        "update_saved_area must fire pg_notify with type=area_meta_change "
        "when name OR originator_parcel_* is in the update body."
    )


def test_put_area_notify_payload_distinguishes_name_vs_originator() -> None:
    """The payload should expose name_changed + originator_changed
    booleans so the frontend (or future analytics) can tell which path
    triggered the broadcast."""
    src = _read(MAIN_PY)
    pat = re.compile(
        r'"type": "area_meta_change".*?'
        r'"name_changed": request\.name is not None.*?'
        r'"originator_changed": \(\s*'
        r"request\.originator_parcel_county is not None\s*"
        r"or request\.originator_parcel_account_num is not None\s*"
        r"\)",
        re.DOTALL,
    )
    assert pat.search(src)


def test_sse_stream_recognizes_area_meta_change_type() -> None:
    src = _read(MAIN_PY)
    pat = re.compile(
        r'msg\.get\("type"\) in \(\s*'
        r'"resync", "blob_explode", "stored_value",\s*'
        r'"saved_parcel_change", "area_meta_change",?\s*\)',
        re.DOTALL,
    )
    assert pat.search(src), (
        "stream_area_events must include 'area_meta_change' in the "
        "named-event whitelist."
    )


# ---- Frontend handler --------------------------------------------------


def test_frontend_listens_for_area_meta_change() -> None:
    src = _read(MAP_JS)
    assert 'es.addEventListener("area_meta_change"' in src
    pat = re.compile(
        r'es\.addEventListener\("area_meta_change", \(\) => \{.*?'
        r"_reloadSavedResources\(\)",
        re.DOTALL,
    )
    assert pat.search(src), (
        "area_meta_change handler must call _reloadSavedResources() so "
        "the area card name + map subject-property refresh live."
    )


def test_area_meta_change_handler_skips_self_echo_when_local_save_in_flight() -> None:
    """KK regression 2026-06-06: when the user clicks Save Parcel, the
    local code calls _commitOriginatorToArea and then _reloadSavedResources
    itself. If the SSE self-echo from area_meta_change ALSO triggers a
    reload, the subject-property outline gets cleared and re-fetched
    out from under the local render — visible flicker / blank state.

    Gate: skip the SSE-triggered reload while
    _pendingSubjectSaves + _pendingFilterSaves > 0. The local code's
    finally block will reload after the save commits."""
    src = _read(MAP_JS)
    pat = re.compile(
        r'es\.addEventListener\("area_meta_change", \(\) => \{.*?'
        r"if \(_pendingSubjectSaves \+ _pendingFilterSaves > 0\) return;",
        re.DOTALL,
    )
    assert pat.search(src), (
        "area_meta_change handler must skip the reload when a local save "
        "is in flight to avoid the self-echo race."
    )


def test_saved_parcel_change_handler_skips_self_echo_when_local_save_in_flight() -> None:
    """Same gate on saved_parcel_change for the same reason — local Save
    Parcel triggers both create_saved_parcel (saved_parcel_change pg_notify)
    and _commitOriginatorToArea (area_meta_change pg_notify). Both come
    back as self-echoes and both need to skip."""
    src = _read(MAP_JS)
    pat = re.compile(
        r'es\.addEventListener\("saved_parcel_change", \(\) => \{.*?'
        r"if \(_pendingSubjectSaves \+ _pendingFilterSaves > 0\) return;",
        re.DOTALL,
    )
    assert pat.search(src), (
        "saved_parcel_change handler must skip the reload when a local "
        "save is in flight to avoid the self-echo race."
    )


def test_frontend_default_message_handler_skips_area_meta_change() -> None:
    src = _read(MAP_JS)
    assert "msg.type === \"area_meta_change\"" in src, (
        "_handleSseFieldChange must include area_meta_change in its "
        "early-return list so the dedicated listener handles it cleanly."
    )


# ---- Watchdog -----------------------------------------------------------


def test_sse_last_message_at_state_declared() -> None:
    """The watchdog needs a module-level _sseLastMessageAt timestamp."""
    src = _read(MAP_JS)
    assert "let _sseLastMessageAt = 0;" in src
    assert "let _sseWatchdogInterval = null;" in src


def test_every_sse_handler_bumps_last_message_at() -> None:
    """All SSE event handlers must update _sseLastMessageAt so the
    watchdog only fires when the stream really has been silent — not
    because we forgot to bump on some event."""
    src = _read(MAP_JS)
    # The five named handlers + the synthetic 'connected' init
    handlers = [
        'addEventListener\\("connected"',
        'addEventListener\\("message"',
        'addEventListener\\("resync"',
        'addEventListener\\("blob_explode"',
        'addEventListener\\("stored_value"',
        'addEventListener\\("saved_parcel_change"',
        'addEventListener\\("area_meta_change"',
    ]
    for handler_marker in handlers:
        # In each handler's body, _sseLastMessageAt = Date.now() should appear
        pat = re.compile(
            handler_marker + r".*?_sseLastMessageAt = Date\.now\(\)",
            re.DOTALL,
        )
        assert pat.search(src), (
            f"Handler matching /{handler_marker}/ must bump _sseLastMessageAt"
            " so the watchdog has accurate liveness data."
        )


def test_watchdog_function_exists_and_force_reconnects_on_silence() -> None:
    src = _read(MAP_JS)
    pat = re.compile(
        r"function _sseWatchdogTick\(\) \{.*?"
        r"if \(!_sseEventSource\) return;.*?"
        r'if \(document\.visibilityState !== "visible"\) return;.*?'
        r"const silenceMs = Date\.now\(\) - _sseLastMessageAt;.*?"
        r"if \(silenceMs <= 90_000\) return;.*?"
        r"_closeSseStream\(\);.*?"
        r"_openSseStream\(areaId\)",
        re.DOTALL,
    )
    assert pat.search(src), (
        "_sseWatchdogTick must (a) self-gate on having a stream + area + "
        "visible tab, (b) check the 90s silence threshold, (c) force a "
        "close + reopen on threshold breach."
    )


def test_watchdog_interval_armed_module_load() -> None:
    """The watchdog needs to actually run. setInterval(_sseWatchdogTick,
    30_000) must be wired at module load (guarded so it doesn't double-
    arm in test contexts)."""
    src = _read(MAP_JS)
    pat = re.compile(
        r"if \(typeof window !== \"undefined\" && !_sseWatchdogInterval\) \{\s*"
        r"_sseWatchdogInterval = setInterval\(_sseWatchdogTick, 30_000\);",
        re.DOTALL,
    )
    assert pat.search(src), (
        "Watchdog interval must be armed at module load with a 30s tick."
    )


def test_watchdog_ignores_hidden_tabs() -> None:
    """Don't fight the browser when it has legitimately frozen our timer
    + EventSource together — tab focus will trigger _reloadSavedResources
    via the visibilitychange handler anyway."""
    src = _read(MAP_JS)
    pat = re.compile(
        r"function _sseWatchdogTick\(\) \{.*?"
        r'if \(document\.visibilityState !== "visible"\) return',
        re.DOTALL,
    )
    assert pat.search(src)
