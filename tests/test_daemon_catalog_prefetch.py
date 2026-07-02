"""Regression pin: ``puffo-agent start`` (foreground) must warm the
claude-code model catalog on startup so control-WS
``build_capabilities`` reports the live model list instead of the
static fallback.

Before this fix, the ``fetch=True`` path was only reachable through
``AgentDetail.__init__.prefetch()`` — which only runs when an
operator opens the desktop UI. Foreground ``start`` (no ``--ui`` /
``--background``) never touched the UI code, so the on-disk cache
stayed empty and the machine reported the hardcoded fallback list to
puffo-server. ``start --background`` accidentally worked because the
operator would open the tray UI at some point, filling the shared
in-process cache the daemon thread reads."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

from puffo_agent.portal import daemon as daemon_mod


def test_daemon_run_calls_model_catalog_prefetch_at_startup():
    """Source-level guard: ``Daemon.run`` must invoke
    ``model_catalog.prefetch`` (or import it under an aliased name)
    before entering the main reconcile loop.

    Text-match test because the alternative — running ``Daemon.run``
    for real — pulls in the API server, RPC service, credential
    refresher, and control WS. All we care about here is that the
    call site exists; the rest is covered by other suites."""
    source = inspect.getsource(daemon_mod.Daemon.run)
    assert (
        "model_catalog" in source and "prefetch" in source
    ), (
        "Daemon.run must call model_catalog.prefetch on startup so the "
        "control-WS capability report includes the live model list. "
        "Foreground ``puffo-agent start`` has no other trigger for the "
        "fetch=True path."
    )


def test_run_daemon_calls_prefetch_before_capability_report(monkeypatch):
    """Functional guard: when ``run_daemon`` short-circuits because a
    daemon is already alive, ``prefetch`` should NOT have fired (early
    return happens before ``Daemon.run``). Symmetrically, when it
    doesn't short-circuit, ``prefetch`` must fire from ``Daemon.run``.

    We only assert the short-circuit half here (the positive path is
    covered by the source guard above and by the observable behavior
    downstream — capability reports carrying live model ids)."""
    called: list[int] = []

    def _spy_prefetch():
        called.append(1)

    monkeypatch.setattr(
        "puffo_agent.agent.model_catalog.prefetch", _spy_prefetch,
    )
    # Short-circuit early: another daemon is "alive" (mock).
    with patch(
        "puffo_agent.portal.daemon.is_daemon_alive", return_value=True,
    ), patch(
        "puffo_agent.portal.daemon.read_daemon_pid", return_value=4242,
    ):
        rc = asyncio.run(daemon_mod.run_daemon())
    assert rc == 0
    assert called == [], (
        "prefetch fired before the already-running short-circuit — "
        "the fix should be inside ``Daemon.run``, not in ``run_daemon``, "
        "so it doesn't spawn a stray HTTP fetch when a second daemon is "
        "refused."
    )
