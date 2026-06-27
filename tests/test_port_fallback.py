"""Auto-port-fallback on bind-failure for the daemon's loopback HTTP services.

Real socket binds on 127.0.0.1 (no mocking) so the actual OSError
shape the helper relies on is exercised end-to-end.
"""

from __future__ import annotations

import logging
import os
import socket
import sys

import pytest
from aiohttp import web

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal._port import bind_tcp_with_fallback
from puffo_agent.portal import data_service as ds
from puffo_agent.portal import rpc_service as rs
from puffo_agent.portal.state import RpcServiceConfig


def _free_port() -> int:
    """Pick a free port; the caller may re-bind the same number."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _occupy(port: int) -> socket.socket:
    """Hold ``127.0.0.1:port`` so the next bind hits EADDRINUSE."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # No SO_REUSEADDR — we want the next bind to actually conflict.
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


async def _runner_for_empty_app() -> web.AppRunner:
    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    return runner


# ─── helper: baseline + fallback contract ────────────────────────


@pytest.mark.asyncio
async def test_helper_binds_requested_port_when_free():
    requested = _free_port()
    runner = await _runner_for_empty_app()
    try:
        site, bound = await bind_tcp_with_fallback(
            runner, host="127.0.0.1", port=requested,
        )
        assert bound == requested
        assert site is not None
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_helper_falls_back_one_port_when_requested_taken():
    requested = _free_port()
    blocker = _occupy(requested)
    runner = await _runner_for_empty_app()
    try:
        _, bound = await bind_tcp_with_fallback(
            runner, host="127.0.0.1", port=requested,
        )
        assert bound == requested + 1
    finally:
        await runner.cleanup()
        blocker.close()


@pytest.mark.asyncio
async def test_helper_scans_across_multiple_busy_ports():
    """Block requested + requested+1; helper should land on +2."""
    requested = _free_port()
    blockers = [_occupy(requested), _occupy(requested + 1)]
    runner = await _runner_for_empty_app()
    try:
        _, bound = await bind_tcp_with_fallback(
            runner, host="127.0.0.1", port=requested,
        )
        assert bound == requested + 2
    finally:
        await runner.cleanup()
        for b in blockers:
            b.close()


@pytest.mark.asyncio
async def test_helper_raises_oserror_when_window_exhausted():
    """Exhausted window → re-raise so the caller's bind-failure path fires."""
    requested = _free_port()
    blockers = [_occupy(requested + i) for i in range(3)]
    runner = await _runner_for_empty_app()
    try:
        with pytest.raises(OSError):
            await bind_tcp_with_fallback(
                runner,
                host="127.0.0.1",
                port=requested,
                max_attempts=3,
            )
    finally:
        await runner.cleanup()
        for b in blockers:
            b.close()


# ─── data_service: cfg mutation + log surface ────────────────────


@pytest.mark.asyncio
async def test_data_service_mutates_cfg_port_on_fallback(caplog):
    """Fallback must mutate cfg.port — otherwise the MCP env-vars
    tell subprocesses to talk to the wrong port."""
    requested = _free_port()
    blocker = _occupy(requested)
    cfg = ds.DataServiceConfig(
        enabled=True, bind_host="127.0.0.1", port=requested,
    )
    runner = None
    try:
        with caplog.at_level(logging.INFO, logger="puffo_agent.portal.data_service"):
            runner = await ds.start_data_service(cfg)
        assert runner is not None
        assert cfg.port == requested + 1
        assert any(
            "fell back to" in rec.message
            for rec in caplog.records
        )
    finally:
        if runner is not None:
            await ds.stop_data_service(runner)
        blocker.close()


@pytest.mark.asyncio
async def test_data_service_leaves_cfg_port_alone_when_default_works():
    requested = _free_port()
    cfg = ds.DataServiceConfig(
        enabled=True, bind_host="127.0.0.1", port=requested,
    )
    runner = None
    try:
        runner = await ds.start_data_service(cfg)
        assert runner is not None
        assert cfg.port == requested
    finally:
        if runner is not None:
            await ds.stop_data_service(runner)


# ─── rpc_service: cfg mutation symmetric to data_service ─────────


@pytest.mark.asyncio
async def test_rpc_service_mutates_cfg_port_on_fallback(caplog):
    requested = _free_port()
    blocker = _occupy(requested)
    cfg = RpcServiceConfig(
        enabled=True, bind_host="127.0.0.1", port=requested,
    )
    runner = None
    try:
        with caplog.at_level(logging.INFO, logger="puffo_agent.portal.rpc_service"):
            runner = await rs.start_rpc_service(cfg)
        assert runner is not None
        assert cfg.port == requested + 1
        assert any(
            "fell back to" in rec.message
            for rec in caplog.records
        )
    finally:
        if runner is not None:
            await rs.stop_rpc_service(runner)
        blocker.close()


@pytest.mark.asyncio
async def test_rpc_service_leaves_cfg_port_alone_when_default_works():
    requested = _free_port()
    cfg = RpcServiceConfig(
        enabled=True, bind_host="127.0.0.1", port=requested,
    )
    runner = None
    try:
        runner = await rs.start_rpc_service(cfg)
        assert runner is not None
        assert cfg.port == requested
    finally:
        if runner is not None:
            await rs.stop_rpc_service(runner)
