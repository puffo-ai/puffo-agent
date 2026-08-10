"""Loopback HTTP RPC service for puffo-core MCP subprocesses.

The MCP submits a JSON request, this service resolves the worker's
``HostMcpContext`` via the resolver the daemon registers at startup,
and dispatches to ``portal/host_mcp_handler.py``. cli-docker reaches
it via ``host.docker.internal`` → host's 127.0.0.1."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from aiohttp import web

from ..agent.message_store import MAX_REMINDER_LIST_LIMIT, REMINDER_STATES, parse_reminder_target
from ..agent.message_store_models import LifecycleConflict
from ..agent.reminder_scheduler import normalize_reminder_timestamp
from . import host_mcp_handler
from ._port import bind_tcp_with_fallback
from .host_mcp_handler import HostMcpContext
from .local_service_auth import require_local_service_auth
from .state import RpcServiceConfig

logger = logging.getLogger(__name__)


# Returns None when the worker isn't warm yet — handlers 404 and the
# MCP wrapper retries on the next agent turn.
RpcResolver = Callable[[str], Optional[HostMcpContext]]
_RPC_RESOLVER: Optional[RpcResolver] = None


def set_rpc_resolver(fn: Optional[RpcResolver]) -> None:
    """Daemon-side hook. ``None`` clears it; routes 503 while unset."""
    global _RPC_RESOLVER
    _RPC_RESOLVER = fn


async def _dispatch(
    request: web.Request,
    action: Callable[..., Awaitable[str]],
    body_keys: tuple[str, ...],
) -> web.Response:
    """Shared body-parse + resolver-lookup + handler dispatch."""
    agent_id = request.match_info["agent_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object"}, status=400,
        )
    resolver = _RPC_RESOLVER
    if resolver is None:
        return web.json_response(
            {"error": "rpc resolver not wired"}, status=503,
        )
    ctx = resolver(agent_id)
    if ctx is None:
        return web.json_response(
            {"error": f"no warm worker for agent_id {agent_id!r}"},
            status=404,
        )
    kwargs = {k: body.get(k) for k in body_keys}
    for k in ("name", "template_id"):
        if k in kwargs and kwargs[k] is None:
            kwargs[k] = ""
    try:
        message = await action(ctx, **kwargs)
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception(
            "rpc-service: %s raised for agent=%s: %s",
            action.__name__, agent_id, exc,
        )
        return web.json_response(
            {"error": f"handler raised: {exc}"}, status=500,
        )
    return web.json_response({"message": message})


async def install_host_mcp_route(request: web.Request) -> web.Response:
    """POST /v1/rpc/{agent_id}/install-mcp — ``{name, template_id?, spec?}``."""
    return await _dispatch(
        request, host_mcp_handler.install,
        body_keys=("name", "template_id", "spec"),
    )


async def sync_host_mcp_route(request: web.Request) -> web.Response:
    """POST /v1/rpc/{agent_id}/sync-mcp — ``{template_id}``."""
    return await _dispatch(
        request, host_mcp_handler.sync,
        body_keys=("template_id",),
    )


async def leave_request_route(request: web.Request) -> web.Response:
    """POST /v1/rpc/{agent_id}/leave-request —
    ``{kind, space_id, channel_id, reason}``."""
    return await _dispatch(
        request, host_mcp_handler.request_leave,
        body_keys=("kind", "space_id", "channel_id", "reason"),
    )


async def permission_request_route(request: web.Request) -> web.Response:
    """POST /v1/rpc/{agent_id}/permission-request —
    ``{tool_name, summary, timeout_s}``. Long-poll; ``message`` is
    ``allow`` / ``deny`` / ``timeout``."""
    return await _dispatch(
        request, host_mcp_handler.request_command_permission,
        body_keys=("tool_name", "summary", "timeout_s"),
    )


_SEND_BODY_KEYS = frozenset({
    "channel", "text", "paths", "caption", "root_id",
    "visibility_level", "send_anyway",
})

_MODEL_VISIBLE_READ_BODY_KEYS = frozenset({
    "space_id",
    "channel_id",
    "through_seq",
    "through_envelope_id",
    "tool_name",
    "tool_arguments",
    "visible_message_ids",
})
_MODEL_VISIBLE_READ_TOOLS = frozenset({
    "get_channel_history",
    "get_thread_history",
    "get_post",
    "get_post_segment",
})


async def send_message_route(request: web.Request) -> web.Response:
    """Strict structured semantic send RPC.

    Unknown fields are rejected before resolver lookup, which prevents hidden
    freshness inputs from reaching (or even invoking) the coordinator.
    """
    agent_id = request.match_info["agent_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object"}, status=400,
        )
    unknown = set(body) - _SEND_BODY_KEYS
    if unknown:
        return web.json_response(
            {"error": f"unknown send field(s): {', '.join(sorted(unknown))}"},
            status=400,
        )
    if not isinstance(body.get("channel"), str) or not body.get("channel", "").strip():
        return web.json_response({"error": "channel is required"}, status=400)
    for key in ("text", "caption", "root_id", "visibility_level"):
        if key in body and not isinstance(body[key], str):
            return web.json_response(
                {"error": f"{key} must be a string"}, status=400,
            )
    if "paths" in body and (
        not isinstance(body["paths"], list)
        or not all(isinstance(item, str) for item in body["paths"])
    ):
        return web.json_response(
            {"error": "paths must be a list of strings"}, status=400,
        )
    if "send_anyway" in body and not isinstance(body["send_anyway"], bool):
        return web.json_response(
            {"error": "send_anyway must be a boolean"}, status=400,
        )
    if "paths" in body and "text" in body:
        return web.json_response(
            {"error": "send body must contain text or paths, not both"}, status=400,
        )

    resolver = _RPC_RESOLVER
    if resolver is None:
        return web.json_response({"error": "rpc resolver not wired"}, status=503)
    ctx = resolver(agent_id)
    if ctx is None:
        return web.json_response(
            {"error": f"no warm worker for agent_id {agent_id!r}"}, status=404,
        )
    kwargs = {
        "channel": body["channel"],
        "text": body.get("text", ""),
        "paths": body.get("paths"),
        "caption": body.get("caption", ""),
        "root_id": body.get("root_id", ""),
        "visibility_level": body.get("visibility_level", "default"),
        "send_anyway": body.get("send_anyway", False),
    }
    try:
        result = await host_mcp_handler.send_message(ctx, **kwargs)
    except Exception as exc:
        logger.exception("rpc-service: structured send raised for agent=%s", agent_id)
        return web.json_response(
            {"error": f"handler raised: {exc}"}, status=500,
        )
    if not isinstance(result, dict):
        return web.json_response(
            {"error": "send handler returned a non-object result"}, status=500,
        )
    return web.json_response(result)


async def stage_model_visible_read_route(request: web.Request) -> web.Response:
    """Stage a trusted local-history watermark for provider admission."""
    agent_id = request.match_info["agent_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object"}, status=400,
        )
    unknown = set(body) - _MODEL_VISIBLE_READ_BODY_KEYS
    if unknown:
        return web.json_response(
            {
                "error": "unknown model-visible read field(s): "
                + ", ".join(sorted(unknown))
            },
            status=400,
        )
    for key in (
        "space_id",
        "channel_id",
        "through_envelope_id",
        "tool_name",
    ):
        if not isinstance(body.get(key), str) or not body[key].strip():
            return web.json_response(
                {"error": f"{key} is required"}, status=400,
            )
    if (
        not isinstance(body.get("through_seq"), int)
        or isinstance(body["through_seq"], bool)
        or body["through_seq"] < 0
    ):
        return web.json_response(
            {"error": "through_seq must be a non-negative integer"},
            status=400,
        )
    if body["tool_name"] not in _MODEL_VISIBLE_READ_TOOLS:
        return web.json_response(
            {"error": "tool_name is not a model-visible message read"},
            status=400,
        )
    tool_arguments = body.get("tool_arguments")
    if not isinstance(tool_arguments, dict) or not all(
        isinstance(key, str)
        and (
            value is None
            or isinstance(value, (str, int, float, bool))
        )
        for key, value in tool_arguments.items()
    ):
        return web.json_response(
            {"error": "tool_arguments must contain scalar JSON values"},
            status=400,
        )
    visible_message_ids = body.get("visible_message_ids")
    if visible_message_ids is not None and (
        not isinstance(visible_message_ids, list)
        or len(visible_message_ids) > 200
        or any(not isinstance(item, str) or not item for item in visible_message_ids)
        or len(set(visible_message_ids)) != len(visible_message_ids)
    ):
        return web.json_response(
            {"error": "visible_message_ids must be at most 200 unique non-empty strings"},
            status=400,
        )

    resolver = _RPC_RESOLVER
    if resolver is None:
        return web.json_response({"error": "rpc resolver not wired"}, status=503)
    ctx = resolver(agent_id)
    if ctx is None:
        return web.json_response(
            {"error": f"no warm worker for agent_id {agent_id!r}"}, status=404,
        )
    try:
        result = await host_mcp_handler.stage_model_visible_read(
            ctx,
            space_id=body["space_id"],
            channel_id=body["channel_id"],
            through_seq=body["through_seq"],
            through_envelope_id=body["through_envelope_id"],
            tool_name=body["tool_name"],
            tool_arguments=tool_arguments,
            visible_message_ids=visible_message_ids,
        )
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception(
            "rpc-service: model-visible read staging raised for agent=%s",
            agent_id,
        )
        return web.json_response(
            {"error": f"handler raised: {exc}"}, status=500,
        )
    return web.json_response(result)


async def read_inbox_route(request: web.Request) -> web.Response:
    """Strict loopback-only semantic Inbox read RPC."""
    agent_id = request.match_info["agent_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict) or set(body) - {"target", "cursor", "limit"}:
        return web.json_response(
            {"error": "read_inbox accepts only target, cursor, and limit"},
            status=400,
        )
    target = body.get("target", "")
    cursor = body.get("cursor", "")
    limit = body.get("limit", 50)
    if not isinstance(target, str) or not isinstance(cursor, str):
        return web.json_response(
            {"error": "target and cursor must be strings"}, status=400
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return web.json_response(
            {"error": "limit must be between 1 and 50"}, status=400
        )
    resolver = _RPC_RESOLVER
    ctx = resolver(agent_id) if resolver is not None else None
    if ctx is None:
        return web.json_response({"error": "no warm worker"}, status=503)
    try:
        return web.json_response(
            await host_mcp_handler.read_inbox(
                ctx, target=target, cursor=cursor, limit=limit
            )
        )
    except (RuntimeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


def _warm_context(agent_id: str) -> HostMcpContext | None:
    resolver = _RPC_RESOLVER
    return resolver(agent_id) if resolver is not None else None


async def create_reminder_route(request: web.Request) -> web.Response:
    """Strict loopback route for the semantic local reminder create tool."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict) or set(body) != {"content", "target", "intended_at"}:
        return web.json_response(
            {"error": "create_reminder accepts only content, target, and intended_at"},
            status=400,
        )
    content, target, intended_at = (
        body["content"], body["target"], body["intended_at"],
    )
    if not isinstance(content, str) or not content:
        return web.json_response(
            {"error": "content must be a non-empty string"}, status=400,
        )
    if not isinstance(target, str) or not isinstance(intended_at, str):
        return web.json_response(
            {"error": "target and intended_at must be strings"}, status=400,
        )
    try:
        parse_reminder_target(target)
        _ms, canonical_intended_at = normalize_reminder_timestamp(intended_at)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    ctx = _warm_context(request.match_info["agent_id"])
    if ctx is None:
        return web.json_response({"error": "no warm worker"}, status=503)
    try:
        result = await host_mcp_handler.create_reminder(
            ctx,
            content=content,
            target=target,
            intended_at=canonical_intended_at,
        )
    except (RuntimeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("rpc-service: create reminder failed")
        return web.json_response({"error": f"handler raised: {exc}"}, status=500)
    return web.json_response(result)


async def list_reminders_route(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict) or set(body) - {"state", "limit"}:
        return web.json_response(
            {"error": "list_reminders accepts only state and limit"}, status=400,
        )
    state = body.get("state", "")
    limit = body.get("limit", 50)
    if not isinstance(state, str) or (state and state not in REMINDER_STATES):
        return web.json_response(
            {"error": "state must be empty or a reminder state"}, status=400,
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_REMINDER_LIST_LIMIT:
        return web.json_response(
            {"error": f"limit must be between 1 and {MAX_REMINDER_LIST_LIMIT}"},
            status=400,
        )
    ctx = _warm_context(request.match_info["agent_id"])
    if ctx is None:
        return web.json_response({"error": "no warm worker"}, status=503)
    try:
        return web.json_response(
            await host_mcp_handler.list_reminders(ctx, state=state, limit=limit)
        )
    except (RuntimeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("rpc-service: list reminders failed")
        return web.json_response({"error": f"handler raised: {exc}"}, status=500)


async def cancel_reminder_route(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict) or set(body) != {"reminder_id"}:
        return web.json_response(
            {"error": "cancel_reminder accepts only reminder_id"}, status=400,
        )
    reminder_id = body["reminder_id"]
    if not isinstance(reminder_id, str) or not reminder_id:
        return web.json_response(
            {"error": "reminder_id must be a non-empty string"}, status=400,
        )
    ctx = _warm_context(request.match_info["agent_id"])
    if ctx is None:
        return web.json_response({"error": "no warm worker"}, status=503)
    try:
        return web.json_response(
            await host_mcp_handler.cancel_reminder(ctx, reminder_id=reminder_id)
        )
    except LifecycleConflict as exc:
        return web.json_response({"error": str(exc)}, status=409)
    except (RuntimeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("rpc-service: cancel reminder failed")
        return web.json_response({"error": f"handler raised: {exc}"}, status=500)


def build_app(cfg: RpcServiceConfig) -> web.Application:
    app = web.Application(middlewares=[require_local_service_auth])
    app.router.add_post(
        "/v1/rpc/{agent_id}/install-mcp",
        install_host_mcp_route,
    )
    app.router.add_post(
        "/v1/rpc/{agent_id}/sync-mcp",
        sync_host_mcp_route,
    )
    app.router.add_post(
        "/v1/rpc/{agent_id}/leave-request",
        leave_request_route,
    )
    app.router.add_post(
        "/v1/rpc/{agent_id}/permission-request",
        permission_request_route,
    )
    app.router.add_post(
        "/v1/rpc/{agent_id}/send-message",
        send_message_route,
    )
    app.router.add_post(
        "/v1/rpc/{agent_id}/model-visible-read",
        stage_model_visible_read_route,
    )
    app.router.add_post(
        "/v1/rpc/{agent_id}/read-inbox",
        read_inbox_route,
    )
    app.router.add_post(
        "/v1/rpc/{agent_id}/create-reminder",
        create_reminder_route,
    )
    app.router.add_post(
        "/v1/rpc/{agent_id}/list-reminders",
        list_reminders_route,
    )
    app.router.add_post(
        "/v1/rpc/{agent_id}/cancel-reminder",
        cancel_reminder_route,
    )
    return app


async def start_rpc_service(
    cfg: RpcServiceConfig,
    *,
    fallback_start: int | None = None,
) -> web.AppRunner | None:
    """``None`` on disabled / bind-window-exhausted. On fallback,
    mutates ``cfg.port`` so the MCP-subprocess env-var passthrough
    sees the resolved port."""
    if not cfg.enabled:
        logger.info("rpc-service: disabled in daemon.yml; not starting")
        return None
    app = build_app(cfg)
    access_logger = logging.getLogger("puffo_agent.portal.rpc_service.access")
    runner = web.AppRunner(app, access_log=access_logger)
    requested_port = cfg.port
    try:
        await runner.setup()
        _, bound_port = await bind_tcp_with_fallback(
            runner, host=cfg.bind_host, port=requested_port,
            fallback_start=fallback_start,
        )
    except OSError as exc:
        logger.warning(
            "rpc-service: bind %s:%d (+99 fallback) failed (%s); "
            "host-touching MCP ops will return errors to agents",
            cfg.bind_host, requested_port, exc,
        )
        try:
            await runner.cleanup()
        except Exception:
            pass
        return None
    if bound_port != requested_port:
        logger.info(
            "rpc-service: port %d in use; fell back to %d",
            requested_port, bound_port,
        )
        cfg.port = bound_port
    logger.info("rpc-service: listening on %s:%d", cfg.bind_host, cfg.port)
    return runner


async def stop_rpc_service(runner: web.AppRunner | None) -> None:
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception:
        logger.exception("rpc-service: cleanup failed")
