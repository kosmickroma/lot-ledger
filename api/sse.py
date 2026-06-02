"""SSE push channel — subscriber map + asyncpg LISTEN supervised loop.

Per docs/MULTIUSER_COLLAB_SPRINT3_SPEC.md v1 §3.4.

The architecture is one supervised asyncpg LISTEN connection per
FastAPI process, listening on a single channel
`saved_area_filter_changes`. Notifications are routed to in-process
SSE subscribers via the `_subscribers` map keyed by area_id.

Agent C catch C-3: every (re)connect broadcasts a synthetic 'resync'
event to all subscribers so they refetch area state — covers the
window where listener was dead and missed events.

Agent A catch C-1: requires `--no-cpu-throttling` on Cloud Run, or
this background task halts between requests.
"""
import asyncio
import json
import logging
import socket as _socket_mod

import asyncpg


logger = logging.getLogger(__name__)

# Channel name for the global filter-change broadcast (must match
# `pg_notify` calls in api/main.py).
CHANNEL = "saved_area_filter_changes"

# In-process subscriber map.
# Key = area_id string, value = set of asyncio.Queue (one per SSE client).
_subscribers: dict[str, set[asyncio.Queue]] = {}
_subscribers_lock = asyncio.Lock()


async def register_subscriber(area_id: str, queue: asyncio.Queue) -> None:
    """Add a new SSE client's queue to the subscribers map."""
    async with _subscribers_lock:
        _subscribers.setdefault(area_id, set()).add(queue)


async def unregister_subscriber(area_id: str, queue: asyncio.Queue) -> None:
    """Remove a queue on SSE client disconnect. Drop the area key if empty."""
    async with _subscribers_lock:
        if area_id in _subscribers:
            _subscribers[area_id].discard(queue)
            if not _subscribers[area_id]:
                del _subscribers[area_id]


async def _on_notify(connection, pid: int, channel: str, payload: str) -> None:
    """Callback for every NOTIFY arrival on the LISTEN connection.

    Parses payload JSON, routes to in-process subscribers for the
    matching area_id. Drops events on queue overflow (slow consumer);
    frontend's refetch-on-reconnect makes this self-healing.
    """
    try:
        msg = json.loads(payload)
    except json.JSONDecodeError as err:
        logger.warning("[sse-listen] malformed payload: %s", err)
        return
    area_id = msg.get("area_id")
    if not area_id:
        return
    async with _subscribers_lock:
        queues = list(_subscribers.get(area_id, set()))
    for q in queues:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning("[sse-listen] queue full for area %s, dropping", area_id)


async def _broadcast_resync_to_all() -> None:
    """Push synthetic 'resync' event to every currently-connected
    subscriber across all areas. Called on every LISTEN connection
    (re)connect — covers missed events during disconnected window.

    Agent C catch C-3.
    """
    async with _subscribers_lock:
        all_queues = [(area_id, q) for area_id, qs in _subscribers.items() for q in qs]
    for area_id, q in all_queues:
        try:
            q.put_nowait({"type": "resync", "area_id": area_id, "reason": "listener_reconnect"})
        except asyncio.QueueFull:
            pass


async def _listen_forever(dsn: str) -> None:
    """Supervised reconnect loop. Runs as background task for the life
    of the FastAPI process.

    Survives: network blips, Postgres restarts, NAT idle-kill (TCP
    keepalive set explicitly), Cloud SQL maintenance. Every reconnect
    broadcasts a resync to all local subscribers so they refetch state.

    Per spec §3.4 + Agent A catches (TCP keepalive) + Agent C catches
    (supervised reconnect + resync broadcast).
    """
    backoff = 1.0
    while True:
        conn = None
        try:
            logger.info("[sse-listen] connecting to Postgres for LISTEN")
            conn = await asyncpg.connect(
                dsn=dsn,
                server_settings={"application_name": "lotledger-sse-listener"},
            )

            # Agent A catch: set TCP keepalive on the underlying socket.
            # asyncpg doesn't expose this directly; reach into the transport.
            try:
                sock = conn._transport.get_extra_info("socket")
                if sock is not None:
                    sock.setsockopt(_socket_mod.SOL_SOCKET, _socket_mod.SO_KEEPALIVE, 1)
                    if hasattr(_socket_mod, "TCP_KEEPIDLE"):
                        sock.setsockopt(_socket_mod.IPPROTO_TCP, _socket_mod.TCP_KEEPIDLE, 30)
                    if hasattr(_socket_mod, "TCP_KEEPINTVL"):
                        sock.setsockopt(_socket_mod.IPPROTO_TCP, _socket_mod.TCP_KEEPINTVL, 10)
                    if hasattr(_socket_mod, "TCP_KEEPCNT"):
                        sock.setsockopt(_socket_mod.IPPROTO_TCP, _socket_mod.TCP_KEEPCNT, 3)
            except Exception as ka_err:
                logger.warning("[sse-listen] could not set TCP keepalive: %s", ka_err)

            await conn.add_listener(CHANNEL, _on_notify)
            logger.info("[sse-listen] LISTEN started on %s; broadcasting resync", CHANNEL)
            backoff = 1.0

            # Resync to all currently-connected subscribers (recovers from
            # any events missed during the disconnected window).
            await _broadcast_resync_to_all()

            # Active probe loop. asyncpg won't notice a dead LISTEN socket
            # until next command — so issue SELECT 1 every 30s.
            while True:
                await asyncio.sleep(30)
                try:
                    await conn.fetchval("SELECT 1")
                except Exception as probe_err:
                    logger.warning("[sse-listen] probe failed, will reconnect: %s", probe_err)
                    raise

        except asyncio.CancelledError:
            logger.info("[sse-listen] cancelled, closing")
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            raise
        except Exception as err:
            logger.warning("[sse-listen] connection error: %s; backoff=%.1fs", err, backoff)
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            await asyncio.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)
            continue
