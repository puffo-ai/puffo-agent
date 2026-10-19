"""Auto-port-fallback for the daemon's loopback HTTP services.
Real socket binds (no mocking) — exercises the real OSError shape."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import time

import pytest
from aiohttp import web

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal import _port
from puffo_agent.portal._port import bind_tcp_with_fallback
from puffo_agent.portal import data_service as ds
from puffo_agent.portal import rpc_service as rs
from puffo_agent.portal.state import RpcServiceConfig


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _occupy(port: int) -> socket.socket:
    # No SO_REUSEADDR — we want the next bind to actually conflict.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def _occupy_contiguous(n: int) -> tuple[int, list[socket.socket]]:
    """Hold n contiguous ports starting at some free base. Retries
    because `_free_port()` only guarantees the base is free —
    adjacent ports may be in use under CI ephemeral-port contention.
    Skips the test on persistent contention rather than failing."""
    for _ in range(30):
        base = _free_port()
        socks: list[socket.socket] = []
        try:
            for i in range(n):
                socks.append(_occupy(base + i))
        except OSError:
            for s in socks:
                s.close()
            continue
        return base, socks
    pytest.skip(f"could not find {n} contiguous free ports under load")


async def _runner_for_empty_app() -> web.AppRunner:
    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    return runner


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
async def test_helper_finds_next_available_port_when_requested_taken():
    requested = _free_port()
    blocker = _occupy(requested)
    runner = await _runner_for_empty_app()
    try:
        _, bound = await bind_tcp_with_fallback(
            runner, host="127.0.0.1", port=requested,
        )
        assert bound > requested
    finally:
        await runner.cleanup()
        blocker.close()


@pytest.mark.asyncio
async def test_helper_scans_across_multiple_busy_ports():
    requested, blockers = _occupy_contiguous(2)
    runner = await _runner_for_empty_app()
    try:
        _, bound = await bind_tcp_with_fallback(
            runner, host="127.0.0.1", port=requested,
        )
        assert bound > requested + 1
    finally:
        await runner.cleanup()
        for b in blockers:
            b.close()


@pytest.mark.asyncio
async def test_helper_jumps_to_fallback_start_on_conflict():
    primary = _free_port()
    blocker = _occupy(primary)
    fallback = _free_port()
    while fallback == primary:
        fallback = _free_port()
    runner = await _runner_for_empty_app()
    try:
        _, bound = await bind_tcp_with_fallback(
            runner, host="127.0.0.1", port=primary,
            fallback_start=fallback,
        )
        assert bound >= fallback
    finally:
        await runner.cleanup()
        blocker.close()


@pytest.mark.asyncio
async def test_helper_fallback_start_scans_forward_too():
    # If fallback_start is also taken, scan from there — never fall
    # back to primary+1 (the load-bearing claim).
    primary = _free_port()
    fallback = _free_port()
    while fallback in (primary, primary + 1):
        fallback = _free_port()
    blockers = [_occupy(primary), _occupy(fallback)]
    runner = await _runner_for_empty_app()
    try:
        _, bound = await bind_tcp_with_fallback(
            runner, host="127.0.0.1", port=primary,
            fallback_start=fallback,
        )
        assert bound > fallback
        assert bound != primary + 1
    finally:
        await runner.cleanup()
        for b in blockers:
            b.close()


@pytest.mark.asyncio
async def test_helper_raises_oserror_when_window_exhausted():
    requested, blockers = _occupy_contiguous(3)
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


@pytest.mark.asyncio
async def test_data_service_mutates_cfg_port_on_fallback(caplog):
    # Fallback must mutate cfg.port — else the MCP env-vars point
    # subprocesses at the wrong port.
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
        assert cfg.port > requested
        assert any(
            f"fell back to {cfg.port}" in rec.message
            for rec in caplog.records
        )
    finally:
        if runner is not None:
            await ds.stop_data_service(runner)
        blocker.close()


@pytest.mark.asyncio
async def test_data_service_honors_fallback_start():
    requested = _free_port()
    blocker = _occupy(requested)
    fallback = _free_port()
    while fallback == requested:
        fallback = _free_port()
    cfg = ds.DataServiceConfig(
        enabled=True, bind_host="127.0.0.1", port=requested,
    )
    runner = None
    try:
        runner = await ds.start_data_service(cfg, fallback_start=fallback)
        assert runner is not None
        assert cfg.port >= fallback
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
        assert cfg.port > requested
        assert any(
            f"fell back to {cfg.port}" in rec.message
            for rec in caplog.records
        )
    finally:
        if runner is not None:
            await rs.stop_rpc_service(runner)
        blocker.close()


@pytest.mark.asyncio
async def test_rpc_service_honors_fallback_start():
    requested = _free_port()
    blocker = _occupy(requested)
    fallback = _free_port()
    while fallback == requested:
        fallback = _free_port()
    cfg = RpcServiceConfig(
        enabled=True, bind_host="127.0.0.1", port=requested,
    )
    runner = None
    try:
        runner = await rs.start_rpc_service(cfg, fallback_start=fallback)
        assert runner is not None
        assert cfg.port >= fallback
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


def _accepts(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
    except OSError:
        return False
    return True


async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_rpc_service_rebinds_its_exact_port_after_listener_death(
    monkeypatch, caplog,
):
    """Windows' proactor closes the listen socket after one failed
    AcceptEx; agents pin the port, so the same port must come back."""
    monkeypatch.setattr(_port, "_LISTENER_PROBE_INTERVAL_SECONDS", 0.01)
    port = _free_port()
    cfg = RpcServiceConfig(enabled=True, bind_host="127.0.0.1", port=port)
    runner = await rs.start_rpc_service(cfg)
    assert runner is not None
    try:
        # What asyncio's accept loop does after the failed AcceptEx.
        (site,) = runner.sites
        site._server.close()
        died_at = time.monotonic()
        # A bound, non-listening socket refuses connects and blocks the
        # rebind, so the first rebind attempt must fail and be retried.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", port))
        # Read live: the MCP probe must not trust the guard's last tick.
        assert await rs.listener_accepting_since() is None
        with caplog.at_level(logging.WARNING, logger="puffo_agent.portal._port"):
            await _until(lambda: any(
                "rebind" in rec.message and "failed" in rec.message
                for rec in caplog.records
            ))
            blocker.close()
            await _until(lambda: _accepts(port))
        assert cfg.port == port
        assert await rs.listener_accepting_since() > died_at
        # More guard ticks on the healthy listener must not rebind it.
        records = len(caplog.records)
        await asyncio.sleep(0.1)
        assert len(caplog.records) == records
        assert any("listener rebound" in rec.message for rec in caplog.records)
    finally:
        await rs.stop_rpc_service(runner)
    await asyncio.sleep(0.05)
    assert not _accepts(port)


@pytest.mark.asyncio
async def test_a_runner_that_never_listened_still_cleans_up():
    """start_*_service cleans up the runner when every bind fails,
    before any guard exists; that must not raise."""
    runner = _port.ListenerRunner(web.Application())
    await runner.setup()
    await runner.cleanup()
