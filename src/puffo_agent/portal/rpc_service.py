"""Loopback HTTP RPC service for puffo-core MCP subprocesses.

The MCP submits a JSON request, this service resolves the worker's
``HostMcpContext`` via the resolver the daemon registers at startup,
and dispatches to ``portal/host_mcp_handler.py``. cli-docker reaches
it via ``host.docker.internal`` → host's 127.0.0.1."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from aiohttp import web

from . import host_mcp_handler
from ._port import bind_tcp_with_fallback
from .host_mcp_handler import HostMcpContext
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


def build_app(cfg: RpcServiceConfig) -> web.Application:
    app = web.Application()
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
