"""Inspection-based assertions for api/sse.py.

Per docs/MULTIUSER_COLLAB_SPRINT3_SPEC.md v1 §3.4 + §3.6.
"""
import inspect


def _src():
    from api import sse
    return inspect.getsource(sse)


def test_subscribers_map_defined():
    from api import sse
    assert hasattr(sse, "_subscribers"), "_subscribers map missing"


def test_listen_forever_function_exists():
    from api import sse
    assert hasattr(sse, "_listen_forever"), "_listen_forever required by spec §3.4"


def test_listen_forever_is_supervised_loop():
    """Critical: must wrap LISTEN in a while True: try/except backoff loop.
    Without supervision, a single network blip silently strands subscribers.
    Per Agent C catch C-3."""
    src = _src()
    fn_idx = src.index("def _listen_forever")
    block = src[fn_idx:fn_idx + 5000]
    assert "while True:" in block, "_listen_forever must be a supervised while-loop"
    assert "except" in block, "_listen_forever must catch exceptions for reconnect"


def test_listen_forever_sets_tcp_keepalive():
    """Agent A catch: NAT idle-kill at ~10min without TCP keepalive.
    asyncpg won't detect dead LISTEN socket without OS-level keepalive."""
    src = _src()
    assert "SO_KEEPALIVE" in src, "Must set SO_KEEPALIVE on the LISTEN socket"
    assert "TCP_KEEPIDLE" in src, "Must set TCP_KEEPIDLE to detect idle drops"


def test_listen_forever_has_active_probe():
    """Even with TCP keepalive, an active SELECT 1 probe surfaces
    half-open connections faster (Agent A catch)."""
    src = _src()
    fn_idx = src.index("def _listen_forever")
    block = src[fn_idx:fn_idx + 5000]
    assert "SELECT 1" in block, "Must run periodic SELECT 1 probe"


def test_listen_forever_resyncs_on_reconnect():
    """Agent C catch C-3: on every reconnect, broadcast a synthetic
    'resync' event to all local subscribers so they refetch."""
    src = _src()
    assert "_broadcast_resync_to_all" in src, (
        "Must broadcast resync to subscribers on every (re)connect"
    )


def test_broadcast_resync_iterates_all_subscribers():
    src = _src()
    fn_idx = src.index("def _broadcast_resync_to_all")
    block = src[fn_idx:fn_idx + 1500]
    assert '"resync"' in block or "'resync'" in block


def test_on_notify_routes_by_area_id():
    src = _src()
    assert "_on_notify" in src
    fn_idx = src.index("def _on_notify")
    block = src[fn_idx:fn_idx + 1500]
    assert "area_id" in block
    assert "_subscribers" in block


def test_on_notify_handles_malformed_payload():
    """Should not crash on bad JSON."""
    src = _src()
    fn_idx = src.index("def _on_notify")
    block = src[fn_idx:fn_idx + 1500]
    assert "json.JSONDecodeError" in block or "try:" in block


def test_sse_listener_startup_hook_registered():
    import inspect
    from api import main as api_main
    src = inspect.getsource(api_main)
    assert "_startup_sse_listener" in src, (
        "Startup hook for SSE listener must exist (spec §3.6)"
    )
    assert "_listen_forever" in src
    assert "asyncio.create_task" in src


def test_sse_listener_shutdown_hook_registered():
    import inspect
    from api import main as api_main
    src = inspect.getsource(api_main)
    assert "_shutdown_sse_listener" in src, (
        "Shutdown hook for SSE listener must exist (spec §3.6)"
    )
    assert "sse_listen_task" in src
    assert ".cancel()" in src


def test_session_dsn_helper_exists():
    """SSE listener needs a DSN to connect to lotledger_sessions DB."""
    import inspect
    from api import main as api_main
    src = inspect.getsource(api_main)
    assert "postgresql://" in src or "_build_session_dsn" in src or "SESSION_DATABASE_URL" in src
