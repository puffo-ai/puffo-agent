"""Read-only HTTP service exposing each agent's ``messages.db`` to
its MCP subprocess.

The daemon is the sole SQLite reader/writer; MCP goes through this
service so cli-docker doesn't open the WAL'd DB across a bind-mount
boundary (which fails with "disk I/O error"). Loopback-only, no auth
— same trust boundary as the keystore bind-mount.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from aiohttp import web

from ..agent.message_store import DataNotFound, MessageStore
from ._port import bind_tcp_with_fallback
from .state import agent_dir

logger = logging.getLogger(__name__)


@dataclass
class DataServiceConfig:
    enabled: bool = True
    bind_host: str = "127.0.0.1"
    port: int = 63386


# Set by the daemon at startup so the new POST profile-cache route
# can reach back into the running PuffoCoreMessageClient. Module-
# level (not stored on the App) because the build_app signature is
# locked by aiohttp's pluggable-config story and the alternative
# (custom AppKey) doesn't buy us anything beyond a wrapper.
_PROFILE_SETTER: Optional[Callable[[str, str, str, str], None]] = None


def set_profile_setter(
    fn: Optional[Callable[[str, str, str, str], None]],
) -> None:
    """Daemon-side hook. ``fn(agent_id, slug, display_name, avatar_url)``
    is called by the POST profile-cache route to inject MCP-tool-fresh
    values into the agent's in-memory cache. ``None`` clears the hook
    (used by tests + on shutdown)."""
    global _PROFILE_SETTER
    _PROFILE_SETTER = fn


# Resolves an agent's live message client for the on-miss re-warm.
_CLIENT_RESOLVER: Optional[Callable[[str], Any]] = None


def set_client_resolver(fn: Optional[Callable[[str], Any]]) -> None:
    """Daemon-side hook; ``None`` clears (tests + shutdown)."""
    global _CLIENT_RESOLVER
    _CLIENT_RESOLVER = fn


def _client_for(agent_id: str) -> Any:
    resolver = _CLIENT_RESOLVER
    if resolver is None:
        return None
    try:
        return resolver(agent_id)
    except Exception:  # noqa: BLE001
        logger.exception("data-service: client resolver raised")
        return None


@dataclass
class _AppState:
    # One MessageStore per agent_id, opened lazily and held for the
    # daemon's lifetime.
    stores: dict[str, MessageStore] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_STORES_KEY = web.AppKey("puffo_data_state", _AppState)


async def _store_for(app: web.Application, agent_id: str) -> MessageStore | None:
    """Return the MessageStore for ``agent_id``, or None when the db
    doesn't exist (handler returns 404)."""
    state: _AppState = app[_STORES_KEY]
    async with state.lock:
        store = state.stores.get(agent_id)
        if store is not None:
            return store
        db_path = agent_dir(agent_id) / "messages.db"
        if not db_path.exists():
            return None
        store = MessageStore(db_path)
        try:
            await store.open()
        except Exception as exc:
            logger.warning(
                "data-service: open(%s) failed: %s", db_path, exc,
            )
            return None
        state.stores[agent_id] = store
        return store


async def _close_all_stores(app: web.Application) -> None:
    """Flush WAL files deterministically on shutdown."""
    state: _AppState = app[_STORES_KEY]
    for store in state.stores.values():
        try:
            await store.close()
        except Exception:
            logger.exception("data-service: close failed")
    state.stores.clear()


async def lookup_channel_space(request: web.Request) -> web.Response:
    """GET channel→space mapping. 404 when the channel is unseen."""
    agent_id = request.match_info["agent_id"]
    channel_id = request.match_info["channel_id"]
    store = await _store_for(request.app, agent_id)
    if store is None:
        return web.json_response({"error": "agent db not found"}, status=404)
    try:
        space_id = await store.lookup_channel_space(channel_id)
        if not space_id and channel_id.startswith("ch_"):
            # Reconnect-dropped membership event → re-warm, re-check.
            client = _client_for(agent_id)
            if client is not None:
                await client.rewarm_channel_caches()
                space_id = await store.lookup_channel_space(channel_id)
    except Exception as exc:
        logger.exception(
            "data-service: lookup_channel_space failed (agent=%s ch=%s)",
            agent_id, channel_id,
        )
        return web.json_response(
            {"error": f"lookup failed: {exc}"}, status=500,
        )
    if not space_id:
        return web.json_response({"error": "channel unknown"}, status=404)
    return web.json_response({"space_id": space_id})


async def list_recent_messages(request: web.Request) -> web.Response:
    """Recent messages for a channel, oldest first. ``channel`` may
    be ``__all__`` to fetch across every channel."""
    agent_id = request.match_info["agent_id"]
    channel_id = request.query.get("channel", "")
    if not channel_id:
        return web.json_response(
            {"error": "channel query param required"}, status=400,
        )
    try:
        limit = int(request.query.get("limit", "20"))
    except ValueError:
        return web.json_response(
            {"error": "limit must be an integer"}, status=400,
        )
    limit = max(1, min(limit, 200))
    store = await _store_for(request.app, agent_id)
    if store is None:
        return web.json_response({"error": "agent db not found"}, status=404)
    try:
        msgs = await store.get_channel_history(channel_id, limit)
    except Exception as exc:
        logger.exception(
            "data-service: get_channel_history failed (agent=%s ch=%s)",
            agent_id, channel_id,
        )
        return web.json_response(
            {"error": f"history fetch failed: {exc}"}, status=500,
        )
    return web.json_response({
        "messages": [_msg_to_dict(m) for m in msgs],
    })


async def list_dm_history(request: web.Request) -> web.Response:
    """Recent DM messages with a peer (by slug), oldest first."""
    agent_id = request.match_info["agent_id"]
    peer = request.query.get("peer", "")
    if not peer:
        return web.json_response(
            {"error": "peer query param required"}, status=400,
        )
    try:
        limit = max(1, min(int(request.query.get("limit", "20")), 200))
        before_raw = request.query.get("before")
        before = int(before_raw) if before_raw else None
    except ValueError:
        return web.json_response(
            {"error": "limit/before must be integers"}, status=400,
        )
    store = await _store_for(request.app, agent_id)
    if store is None:
        return web.json_response({"error": "agent db not found"}, status=404)
    try:
        msgs = await store.get_dm_history(peer, limit, before)
    except Exception as exc:
        logger.exception(
            "data-service: get_dm_history failed (agent=%s peer=%s)",
            agent_id, peer,
        )
        return web.json_response(
            {"error": f"dm history fetch failed: {exc}"}, status=500,
        )
    return web.json_response({
        "messages": [_msg_to_dict(m) for m in msgs],
    })


def _parse_int_param(value: str | None, name: str) -> tuple[int | None, web.Response | None]:
    """Return (parsed, error_response). ``None, None`` if missing."""
    if value is None or value == "":
        return None, None
    try:
        return int(value), None
    except ValueError:
        return None, web.json_response(
            {"error": f"{name} must be an integer"}, status=400,
        )


async def list_channel_roots(request: web.Request) -> web.Response:
    """Root posts in a channel with reply counts. Replaces the
    flat ``messages/recent`` view for agents that want to see what
    conversations exist without dragging every reply into context.
    Query params: ``channel`` (required), ``limit``, ``since`` (an
    envelope_id), ``before`` (ms-epoch), ``after`` (ms-epoch)."""
    agent_id = request.match_info["agent_id"]
    channel_id = request.query.get("channel", "")
    if not channel_id:
        return web.json_response(
            {"error": "channel query param required"}, status=400,
        )
    limit, err = _parse_int_param(request.query.get("limit", "20"), "limit")
    if err is not None:
        return err
    before_ts, err = _parse_int_param(request.query.get("before"), "before")
    if err is not None:
        return err
    after_ts, err = _parse_int_param(request.query.get("after"), "after")
    if err is not None:
        return err
    since = request.query.get("since") or None
    limit = max(1, min(limit or 20, 200))
    store = await _store_for(request.app, agent_id)
    if store is None:
        return web.json_response({"error": "agent db not found"}, status=404)
    try:
        roots = await store.get_channel_roots(
            channel_id,
            limit=limit,
            since_envelope_id=since,
            before_ts=before_ts,
            after_ts=after_ts,
        )
    except DataNotFound:
        return web.json_response(
            {"error": "channel not found"}, status=404,
        )
    except Exception as exc:
        logger.exception(
            "data-service: get_channel_roots failed (agent=%s ch=%s)",
            agent_id, channel_id,
        )
        return web.json_response(
            {"error": f"roots fetch failed: {exc}"}, status=500,
        )
    return web.json_response({
        "roots": [
            {"message": _msg_to_dict(r.message), "reply_count": r.reply_count}
            for r in roots
        ],
    })


async def list_thread_messages(request: web.Request) -> web.Response:
    """Messages in a thread (the root + every reply), filtered by
    optional ``since`` (envelope_id), ``before`` / ``after`` (ms-
    epoch). Returned oldest-first up to ``limit``."""
    agent_id = request.match_info["agent_id"]
    root_id = request.match_info["root_id"]
    limit, err = _parse_int_param(request.query.get("limit", "50"), "limit")
    if err is not None:
        return err
    before_ts, err = _parse_int_param(request.query.get("before"), "before")
    if err is not None:
        return err
    after_ts, err = _parse_int_param(request.query.get("after"), "after")
    if err is not None:
        return err
    since = request.query.get("since") or None
    limit = max(1, min(limit or 50, 200))
    store = await _store_for(request.app, agent_id)
    if store is None:
        return web.json_response({"error": "agent db not found"}, status=404)
    try:
        msgs = await store.get_thread_messages(
            root_id,
            limit=limit,
            since_envelope_id=since,
            before_ts=before_ts,
            after_ts=after_ts,
        )
    except DataNotFound:
        return web.json_response(
            {"error": "thread root not found"}, status=404,
        )
    except Exception as exc:
        logger.exception(
            "data-service: get_thread_messages failed (agent=%s root=%s)",
            agent_id, root_id,
        )
        return web.json_response(
            {"error": f"thread fetch failed: {exc}"}, status=500,
        )
    return web.json_response({
        "messages": [_msg_to_dict(m) for m in msgs],
    })


async def get_message_by_envelope(request: web.Request) -> web.Response:
    """GET a single message by envelope_id. 404 if not stored."""
    agent_id = request.match_info["agent_id"]
    envelope_id = request.match_info["envelope_id"]
    store = await _store_for(request.app, agent_id)
    if store is None:
        return web.json_response({"error": "agent db not found"}, status=404)
    try:
        msg = await store.get_message_by_envelope(envelope_id)
    except Exception as exc:
        logger.exception(
            "data-service: lookup by envelope_id failed (agent=%s env=%s)",
            agent_id, envelope_id,
        )
        return web.json_response(
            {"error": f"lookup failed: {exc}"}, status=500,
        )
    if msg is None:
        return web.json_response({"error": "message not found"}, status=404)
    return web.json_response({"message": _msg_to_dict(msg)})


def _msg_to_dict(m: Any) -> dict[str, Any]:
    return {
        "envelope_id": m.envelope_id,
        "envelope_kind": m.envelope_kind,
        "sender_slug": m.sender_slug,
        "channel_id": m.channel_id,
        "space_id": m.space_id,
        "recipient_slug": m.recipient_slug,
        "content_type": m.content_type,
        "content": m.content,
        "sent_at": m.sent_at,
        "received_at": m.received_at,
        "thread_root_id": m.thread_root_id,
        "reply_to_id": m.reply_to_id,
        "is_encrypted": m.is_encrypted,
    }


async def update_profile_cache(request: web.Request) -> web.Response:
    """POST {slug, display_name, avatar_url} — inject fresh values
    into the agent's in-memory ``_profile_cache``. Body shape::

        {"slug": "alice-0001", "display_name": "Alice", "avatar_url": "..."}

    Called by the MCP ``get_user_info`` tool right after its
    ``/identities/profiles`` fetch so the daemon's render path picks
    up the new values immediately instead of waiting for the TTL.
    No-op (200) when the daemon hasn't wired the setter — keeps the
    tool resilient against partial-startup states. 400 on malformed
    body."""
    agent_id = request.match_info["agent_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    slug = str(body.get("slug") or "")
    if not slug:
        return web.json_response({"error": "slug is required"}, status=400)
    display_name = str(body.get("display_name") or "")
    avatar_url = str(body.get("avatar_url") or "")
    setter = _PROFILE_SETTER
    if setter is None:
        logger.debug(
            "data-service: profile-cache write for agent=%s slug=%s "
            "skipped — setter not wired (daemon partial startup?)",
            agent_id, slug,
        )
        return web.json_response({"ok": True, "note": "setter not wired"})
    try:
        setter(agent_id, slug, display_name, avatar_url)
    except Exception as exc:
        logger.exception(
            "data-service: profile-cache setter raised for agent=%s slug=%s: %s",
            agent_id, slug, exc,
        )
        return web.json_response(
            {"error": f"setter raised: {exc}"}, status=500,
        )
    return web.json_response({"ok": True})


# ── Lifecycle ────────────────────────────────────────────────────


def build_app(cfg: DataServiceConfig) -> web.Application:
    app = web.Application()
    app[_STORES_KEY] = _AppState()
    app.router.add_get(
        "/v1/data/{agent_id}/channels/{channel_id}/space",
        lookup_channel_space,
    )
    app.router.add_get(
        "/v1/data/{agent_id}/messages/recent",
        list_recent_messages,
    )
    app.router.add_get(
        "/v1/data/{agent_id}/dms/recent",
        list_dm_history,
    )
    app.router.add_get(
        "/v1/data/{agent_id}/channels/roots",
        list_channel_roots,
    )
    app.router.add_get(
        "/v1/data/{agent_id}/threads/{root_id}",
        list_thread_messages,
    )
    app.router.add_get(
        "/v1/data/{agent_id}/messages/{envelope_id}",
        get_message_by_envelope,
    )
    app.router.add_post(
        "/v1/data/{agent_id}/profile-cache",
        update_profile_cache,
    )
    app.on_shutdown.append(_close_all_stores)
    return app


async def start_data_service(
    cfg: DataServiceConfig,
    *,
    fallback_start: int | None = None,
) -> web.AppRunner | None:
    """``None`` on disabled / bind-window-exhausted. On fallback,
    mutates ``cfg.port`` so the MCP-subprocess env-var passthrough
    sees the resolved port."""
    if not cfg.enabled:
        logger.info("data-service: disabled in daemon.yml; not starting")
        return None
    app = build_app(cfg)
    access_logger = logging.getLogger("puffo_agent.portal.data_service.access")
    runner = web.AppRunner(
        app,
        access_log=access_logger,
        access_log_format='%r -> %s (%Tf s)',
    )
    await runner.setup()
    requested_port = cfg.port
    try:
        _, bound_port = await bind_tcp_with_fallback(
            runner, host=cfg.bind_host, port=requested_port,
            fallback_start=fallback_start,
        )
    except OSError as exc:
        logger.warning(
            "data-service: bind %s:%d (+99 fallback) failed (%s); "
            "cli-docker MCP tools will see disk I/O errors",
            cfg.bind_host, requested_port, exc,
        )
        await runner.cleanup()
        return None
    if bound_port != requested_port:
        logger.info(
            "data-service: port %d in use; fell back to %d",
            requested_port, bound_port,
        )
        cfg.port = bound_port
    logger.info("data-service: listening on http://%s:%d", cfg.bind_host, cfg.port)
    return runner


async def stop_data_service(runner: web.AppRunner | None) -> None:
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception as exc:
        logger.warning("data-service: cleanup failed: %s", exc)
