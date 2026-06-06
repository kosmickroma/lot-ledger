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


def test_area_meta_change_handler_has_no_self_echo_gate() -> None:
    """Copilot deep dive 2026-06-06: the self-echo gate I added at commit
    99be884 was over-engineered for a speculative race that doesn't
    actually exist. _renderSubjectPropertyOutlineLazy already has an
    _subjectPropertyGeometryInFlight early-return preventing the duplicate
    fetch. What the gate actually did was kill the FAST path for the
    writer's own tab — SSE self-echo arrives ~100ms after backend commit,
    well before the local awaited PUT chain returns, so suppressing it
    meant the writer waited the full HTTP round trip before the new gold
    star appeared. Pre-99be884 behavior was instant because the SSE
    self-echo was the fast path.

    Guard: assert the gate is GONE."""
    src = _read(MAP_JS)
    # Pull the area_meta_change handler body
    m = re.search(
        r'es\.addEventListener\("area_meta_change", \(\) => \{(.*?)\}\);',
        src,
        re.DOTALL,
    )
    assert m, "area_meta_change handler missing"
    body = m.group(1)
    assert "_pendingSubjectSaves" not in body, (
        "area_meta_change handler must NOT have a _pendingSubjectSaves gate. "
        "The gate is what regressed the writer-own-tab UX."
    )


def test_saved_parcel_change_handler_has_no_self_echo_gate() -> None:
    """Same reasoning as area_meta_change — gate must be absent."""
    src = _read(MAP_JS)
    m = re.search(
        r'es\.addEventListener\("saved_parcel_change", \(\) => \{(.*?)\}\);',
        src,
        re.DOTALL,
    )
    assert m, "saved_parcel_change handler missing"
    body = m.group(1)
    assert "_pendingSubjectSaves" not in body, (
        "saved_parcel_change handler must NOT have a _pendingSubjectSaves gate. "
        "Removing the gate restores the pre-99be884 fast-path behavior."
    )


def test_reload_saved_resources_updates_target_parcel_before_render() -> None:
    """KK regression 2026-06-06: on tab B, the outline migrated to the
    new subject but the star stayed pinned on the old subject. The
    _setCurrentTargetParcel block lived AFTER _renderSubjectProperties(),
    so the high-zoom "staged target star" branch read stale state at
    render time.

    Fix: the loaded-area-current-subject sync must happen BEFORE the
    _renderSubjectProperties() call so the star and the outline use the
    same current state."""
    src = _read(MAP_JS)
    # In _reloadSavedResources, the _setCurrentTargetParcel call must
    # appear BEFORE the _renderSubjectProperties() call.
    pat = re.compile(
        r"async function _reloadSavedResources\(\).*?"
        r"_setCurrentTargetParcel\(\{.*?county: c,.*?account: a.*?\}\);.*?"
        r"_renderSubjectProperties\(\);",
        re.DOTALL,
    )
    assert pat.search(src), (
        "_reloadSavedResources must sync _currentTargetParcel from the "
        "loaded area's persisted originator BEFORE calling "
        "_renderSubjectProperties so the high-zoom staged-star branch "
        "sees current state, not stale state."
    )


def test_reload_saved_resources_passes_subject_coords_to_target_parcel() -> None:
    """KK / Copilot debug 2026-06-06: tab B's reader saw the outline
    migrate but the STAR stayed on the old subject until a map wiggle.
    Root cause: _reloadSavedResources called _setCurrentTargetParcel
    with only { county, account } — no lat/lng. _normalizeTargetParcel
    then stored lat/lng as null. _ensureCurrentTargetParcelCoords
    started an async fetch. _renderSubjectProperties fired immediately
    after, with null staged coords. The high-zoom staged-star branch
    has a `Number.isFinite(staged.lat)` guard → skipped the render.
    Outline rendered fine because it reads from _subjectPropertiesByKey
    which has the backend-provided coords.

    Fix: pull lat/lng from _subjectPropertiesByKey (which was just
    populated right above this code from the same /api/areas response)
    and pass them through so the render-on-same-tick has finite coords."""
    src = _read(MAP_JS)
    pat = re.compile(
        r"const _subjectKey = _subjectPropertyKey\(c, a\);\s*"
        r"const _subjectEntry = _subjectPropertiesByKey\.get\(_subjectKey\);\s*"
        r"_setCurrentTargetParcel\(\{\s*"
        r"county: c,\s*"
        r"account: a,\s*"
        r"lat: _subjectEntry \? _subjectEntry\.lat : undefined,\s*"
        r"lng: _subjectEntry \? _subjectEntry\.lng : undefined,\s*"
        r"\}\);",
        re.DOTALL,
    )
    assert pat.search(src), (
        "_reloadSavedResources must pass lat/lng from _subjectPropertiesByKey "
        "into _setCurrentTargetParcel so the staged target has finite "
        "coords at first render — otherwise the star skips until moveend."
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
