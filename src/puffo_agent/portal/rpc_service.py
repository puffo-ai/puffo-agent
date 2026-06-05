"""Loopback HTTP RPC service for puffo-core MCP subprocesses.

When the agent's puffo-core MCP needs the daemon to execute an op
on its behalf — today that's only ``install_host_mcp`` /
``sync_host_mcp``, both of which read+write operator-owned files
under ``~/`` — the MCP calls into this service.

Why a separate service from ``data_service``:

- ``data_service`` is the per-agent read-path for ``messages.db``
  (plus one small profile-cache POST). Adding host-touching writes
  there muddies the abstraction.
- This service runs daemon-side host ops. Single writer to
  operator's ``~/.claude.json`` regardless of how many agents
  request installs.
- cli-local and cli-docker share the same route — no per-runtime
  fork inside the tool body. Docker Desktop maps
  ``host.docker.internal:<port>`` back to the host's loopback, so a
  ``127.0.0.1`` bind covers both.

The MCP-side client (``mcp/_host_mcp.py:PuffoRpcClient``) and the
daemon-side handler (``portal/host_mcp_handler.py``) sandwich this
service: the MCP submits the request as JSON, the service looks up
the running worker's ``HostMcpContext`` via the resolver the daemon
registers at startup, and dispatches to the handler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from aiohttp import web

from . import host_mcp_handler
from .host_mcp_handler import HostMcpContext
from .state import RpcServiceConfig

logger = logging.getLogger(__name__)


# Daemon-side hook: ``agent_id -> HostMcpContext | None``. Returns
# None when the worker isn't warm yet (race between agent spawn and
# the first tool call) — handlers surface 404 to the MCP wrapper,
# which retries on the next agent turn.
RpcResolver = Callable[[str], Optional[HostMcpContext]]
_RPC_RESOLVER: Optional[RpcResolver] = None


def set_rpc_resolver(fn: Optional[RpcResolver]) -> None:
    """Daemon-side hook. ``None`` clears it (tests + shutdown).
    When unset, route handlers 503 — same shape data_service's
    profile-cache uses when its setter isn't wired."""
    global _RPC_RESOLVER
    _RPC_RESOLVER = fn


async def _dispatch(
    request: web.Request,
    action: Callable[..., Awaitable[str]],
    body_keys: tuple[str, ...],
) -> web.Response:
    """Shared body-parse + resolver-lookup + handler dispatch for
    every host-touching RPC route. ``action`` is one of
    ``host_mcp_handler.install`` / ``host_mcp_handler.sync``;
    ``body_keys`` lists the JSON keys forwarded as kwargs."""
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
    # Handler signatures want str (not None) for these — callers can
    # omit them and the handler treats empty == unset.
    for k in ("name", "template_id"):
        if k in kwargs and kwargs[k] is None:
            kwargs[k] = ""
    try:
        message = await action(ctx, **kwargs)
    except RuntimeError as exc:
        # Predictable validation/shape errors — 400 lets the MCP
        # wrapper bubble them to the agent as a tool error string.
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
    """POST /v1/rpc/{agent_id}/install-mcp body:
    ``{name, template_id?, spec?}``. Either ``template_id`` (catalog
    lookup) or ``spec`` (inline MCP config dict) — exclusive.
    Daemon writes the entry to operator's ``~/.claude.json`` and
    DMs the operator on success."""
    return await _dispatch(
        request, host_mcp_handler.install,
        body_keys=("name", "template_id", "spec"),
    )


async def sync_host_mcp_route(request: web.Request) -> web.Response:
    """POST /v1/rpc/{agent_id}/sync-mcp body: ``{template_id}``.
    Copies host's ``mcpServers[<template_id>]`` into the agent's
    ``<agent_home>/.claude.json``. For cli-docker the agent_home is
    bind-mounted into the container; the next ``refresh()`` picks
    it up."""
    return await _dispatch(
        request, host_mcp_handler.sync,
        body_keys=("template_id",),
    )


# ── Lifecycle ──────────────────────────────────────────────────────


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
    return app


async def start_rpc_service(
    cfg: RpcServiceConfig,
) -> web.AppRunner | None:
    """Start the RPC service. Returns ``None`` when disabled or the
    socket bind fails (non-fatal — install_host_mcp / sync_host_mcp
    are the only callers and they degrade to a clear tool error)."""
    if not cfg.enabled:
        logger.info("rpc-service: disabled in daemon.yml; not starting")
        return None
    app = build_app(cfg)
    access_logger = logging.getLogger("puffo_agent.portal.rpc_service.access")
    runner = web.AppRunner(app, access_log=access_logger)
    try:
        await runner.setup()
        site = web.TCPSite(runner, cfg.bind_host, cfg.port)
        await site.start()
    except OSError as exc:
        logger.warning(
            "rpc-service: bind %s:%d failed (%s); host-touching MCP "
            "ops will return errors to agents",
            cfg.bind_host, cfg.port, exc,
        )
        try:
            await runner.cleanup()
        except Exception:
            pass
        return None
    logger.info("rpc-service: listening on %s:%d", cfg.bind_host, cfg.port)
    return runner


async def stop_rpc_service(runner: web.AppRunner | None) -> None:
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception:
        logger.exception("rpc-service: cleanup failed")
