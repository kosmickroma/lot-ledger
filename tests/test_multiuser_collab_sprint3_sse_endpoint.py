"""Inspection-based assertions for the SSE endpoint
GET /api/areas/{area_id}/events.

Per docs/MULTIUSER_COLLAB_SPRINT3_SPEC.md v1 §3.5.
"""
import inspect
from api import main as api_main


def _extract_fn_source(module_src: str, fn_name: str) -> str:
    start_marker = f"def {fn_name}"
    if start_marker not in module_src:
        return ""
    start = module_src.index(start_marker)
    rest = module_src[start + len(start_marker):]
    next_def = rest.find("\ndef ")
    if next_def == -1:
        return module_src[start:]
    return module_src[start:start + len(start_marker) + next_def]


def test_sse_endpoint_decorator_exists():
    src = inspect.getsource(api_main)
    assert '@app.get("/api/areas/{area_id}/events")' in src, (
        "SSE endpoint not registered (spec §3.5)"
    )


def test_sse_endpoint_uses_membership_check():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "stream_area_events")
    assert "_assert_user_is_area_member" in fn_src


def test_sse_endpoint_uses_saved_area_exists():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "stream_area_events")
    assert "_saved_area_exists" in fn_src


def test_sse_endpoint_returns_event_source_response():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "stream_area_events")
    assert "EventSourceResponse" in fn_src


def test_sse_endpoint_registers_and_unregisters_subscriber():
    """Spec §3.5: must register on connect, unregister on disconnect
    (via finally block to guarantee cleanup)."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "stream_area_events")
    assert "register_subscriber" in fn_src
    assert "unregister_subscriber" in fn_src
    assert "finally:" in fn_src


def test_sse_endpoint_uses_timeout_pattern():
    """Agent C catch: must use asyncio.wait_for(queue.get(), timeout=...)
    to avoid parking forever on dead clients."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "stream_area_events")
    assert "asyncio.wait_for" in fn_src or "_asyncio.wait_for" in fn_src
    assert "is_disconnected" in fn_src


def test_sse_endpoint_has_ping_heartbeat():
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "stream_area_events")
    assert "ping=" in fn_src or "ping =" in fn_src


def test_sse_endpoint_sets_no_buffer_headers():
    """Agent A catch: tell intermediaries not to buffer."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "stream_area_events")
    assert "X-Accel-Buffering" in fn_src


def test_sse_endpoint_emits_resync_event_type():
    """Spec §3.5: when msg.type == 'resync', the SSE event name must be 'resync'."""
    src = inspect.getsource(api_main)
    fn_src = _extract_fn_source(src, "stream_area_events")
    assert '"resync"' in fn_src or "'resync'" in fn_src
    assert '"blob_explode"' in fn_src or "'blob_explode'" in fn_src
