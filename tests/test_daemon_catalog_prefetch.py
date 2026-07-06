"""``puffo-agent start`` (foreground) must warm the claude-code
model catalog so control-WS ``build_capabilities`` reports the live
list instead of the static fallback."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

from puffo_agent.portal import daemon as daemon_mod


def test_daemon_run_calls_model_catalog_prefetch_at_startup():
    """Source-level guard: text-match on ``Daemon.run`` so we don't
    have to mock the API server, RPC service, refresher, and control
    WS just to pin one call."""
    source = inspect.getsource(daemon_mod.Daemon.run)
    assert "model_catalog" in source and "prefetch" in source


def test_run_daemon_short_circuit_does_not_prefetch(monkeypatch):
    """The already-running short-circuit lives in ``run_daemon``, not
    ``Daemon.run`` — so a second daemon getting refused mustn't fire a
    stray /v1/models fetch."""
    called: list[int] = []
    monkeypatch.setattr(
        "puffo_agent.agent.model_catalog.prefetch",
        lambda: called.append(1),
    )
    with patch(
        "puffo_agent.portal.daemon.is_daemon_alive", return_value=True,
    ), patch(
        "puffo_agent.portal.daemon.read_daemon_pid", return_value=4242,
    ):
        rc = asyncio.run(daemon_mod.run_daemon())
    assert rc == 0
    assert called == []
