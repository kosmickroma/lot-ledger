"""tests/test_sse_saved_parcel_broadcast.py
Role: Regression guard for the SSE broadcast Mike asked for 2026-06-06:
"stars don't show up when my assistants are adding areas with saved
parcels until I view the area with a saved parcel."

Before this fix, only filter-state PATCH, blob-explode PUT, and
stored-value PUT fired pg_notify. Saved-parcel adds/deletes were silent,
so user B never got an SSE event when user A bonded a parcel to a
shared area — the gold star only appeared after B manually opened the
area (which triggered a fresh /api/areas/{id} fetch).

Fix:
  - create_saved_parcel fires pg_notify with type=saved_parcel_change
    action=add when a bonded copy is created
  - delete_saved_parcel uses DELETE...RETURNING area_id to learn which
    bonded areas lost a copy, fires one pg_notify per distinct area
  - The SSE endpoint maps the new type to a named event so the
    frontend's addEventListener("saved_parcel_change", ...) fires
  - The frontend default-message handler ignores the new type (it has
    its own dedicated listener)
  - The frontend SSE listener calls _reloadSavedResources() so the
    saved-parcels list (and star rendering) refreshes immediately

Connects to: api/main.py create_saved_parcel, delete_saved_parcel,
stream_area_events; frontend/map.js _openSseStream, _handleSseFieldChange
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "api" / "main.py"
MAP_JS = ROOT / "frontend" / "map.js"


def _read(path: Path) -> str:
    return path.read_text()


# ---- Backend: create_saved_parcel fires the new event -------------------


def test_create_saved_parcel_fires_pg_notify_on_bonded_row() -> None:
    """When the bonded row is created (area_id supplied + user is area
    member), we must fire pg_notify so other connected users get the SSE."""
    src = _read(MAIN_PY)
    # The notify must sit inside the `if bonded_row` branch — fire ONLY
    # when an area-bonded copy actually got created. Standalone copies
    # are per-user and need no cross-user broadcast.
    pat = re.compile(
        r"bonded_row = cur\.fetchone\(\).*?"
        r'pg_notify\(.saved_area_filter_changes..*?'
        r'"type": "saved_parcel_change".*?'
        r'"action": "add"',
        re.DOTALL,
    )
    assert pat.search(src), (
        "create_saved_parcel must fire pg_notify with type=saved_parcel_change "
        "action=add inside the bonded_row branch."
    )


def test_create_saved_parcel_notify_payload_includes_account_county() -> None:
    """The frontend handler uses these to dedup / log. Future per-parcel
    rendering could also key off them, so include them now."""
    src = _read(MAIN_PY)
    # find the create_saved_parcel area
    idx = src.find("bonded_row = cur.fetchone()")
    assert idx > 0
    chunk = src[idx:idx + 2000]
    assert '"account_num": account_num' in chunk
    assert '"county": county' in chunk
    assert '"by_user_id"' in chunk
    assert '"by_session_id"' in chunk


# ---- Backend: delete_saved_parcel fires the new event -------------------


def test_delete_saved_parcel_uses_returning_to_collect_area_ids() -> None:
    """The DELETE doesn't filter by area_id (it removes all copies for
    this user/county/account). Use RETURNING area_id so the broadcast
    can fan out to every area that had a bonded copy."""
    src = _read(MAIN_PY)
    # Look for the DELETE with RETURNING area_id in delete_saved_parcel
    pat = re.compile(
        r"async def delete_saved_parcel\(.*?"
        r"DELETE FROM saved_parcels.*?"
        r"RETURNING area_id",
        re.DOTALL,
    )
    assert pat.search(src), (
        "delete_saved_parcel must use DELETE ... RETURNING area_id to "
        "collect every affected area before broadcasting."
    )


def test_delete_saved_parcel_fires_notify_per_affected_area() -> None:
    src = _read(MAIN_PY)
    pat = re.compile(
        r"async def delete_saved_parcel\(.*?"
        r"for affected_area_id in set\(deleted_area_ids\):.*?"
        r'pg_notify\(.saved_area_filter_changes..*?'
        r'"type": "saved_parcel_change".*?'
        r'"action": "delete"',
        re.DOTALL,
    )
    assert pat.search(src), (
        "delete_saved_parcel must loop over the distinct area_ids it just "
        "removed bonded copies from and fire one notify per area."
    )


def test_delete_saved_parcel_skips_notify_when_only_standalone() -> None:
    """The `if deleted_area_ids` guard means a delete that only nuked
    standalone (non-bonded) rows fires no broadcast. Standalone parcels
    are per-user — nothing to sync."""
    src = _read(MAIN_PY)
    pat = re.compile(
        r"async def delete_saved_parcel\(.*?"
        r"if deleted_area_ids:",
        re.DOTALL,
    )
    assert pat.search(src)


# ---- Backend: SSE stream maps the new type to a named event ------------


def test_sse_stream_recognizes_saved_parcel_change_type() -> None:
    """stream_area_events must emit `event: saved_parcel_change` (named)
    so the frontend's addEventListener picks it up. Without this it would
    fall through to the default `message` event and get filtered out."""
    src = _read(MAIN_PY)
    pat = re.compile(
        r'msg\.get\("type"\) in \(\s*'
        r'"resync", "blob_explode", "stored_value", "saved_parcel_change",?\s*\)',
        re.DOTALL,
    )
    assert pat.search(src), (
        "stream_area_events must include 'saved_parcel_change' in its "
        "named-event whitelist so EventSource.addEventListener fires."
    )


# ---- Frontend: SSE listener wired + handler calls _reloadSavedResources -


def test_frontend_listens_for_saved_parcel_change() -> None:
    src = _read(MAP_JS)
    assert 'es.addEventListener("saved_parcel_change"' in src, (
        "Frontend must register an addEventListener for saved_parcel_change."
    )
    pat = re.compile(
        r'es\.addEventListener\("saved_parcel_change", \(\) => \{.*?'
        r"_reloadSavedResources\(\)",
        re.DOTALL,
    )
    assert pat.search(src), (
        "saved_parcel_change handler must call _reloadSavedResources() "
        "to refresh the saved-parcels cache + re-render stars."
    )


def test_frontend_default_message_handler_skips_saved_parcel_change() -> None:
    """_handleSseFieldChange must early-return on saved_parcel_change
    type — that event has its own dedicated listener and the field-change
    handler would otherwise try to apply it as a filter delta and warn."""
    src = _read(MAP_JS)
    pat = re.compile(
        r'msg\.type === "resync" \|\| msg\.type === "blob_explode" \|\| '
        r'msg\.type === "saved_parcel_change"',
    )
    assert pat.search(src), (
        "_handleSseFieldChange must include saved_parcel_change in its "
        "early-return list alongside the other type-tagged events."
    )
