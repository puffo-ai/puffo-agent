"""Pre-delivery token gate.

When the Worker is about to dispatch a batch to its adapter and the
daemon-owned credential isn't fresh, ``ensure_fresh_token`` drives a
refresh through the daemon's mutex. Failure flips ``auth_failed``,
DMs the operator ONCE per expiration episode (dedup via
``_auth_failed_notification_sent``), and raises ``AgentAPIError`` so
the consumer re-enqueues the batch."""

from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.runtime_matrix import (
    RUNTIME_CLI_LOCAL,
    RUNTIME_WS_LOCAL,
)
from puffo_agent.portal.state import AgentConfig, DaemonConfig, RuntimeState
from puffo_agent.portal.worker import Worker


def _isolated_home() -> str:
    home = tempfile.mkdtemp(prefix="puffo-pre-deliver-")
    os.environ["PUFFO_AGENT_HOME"] = home
    return home


def _make_worker(runtime_kind: str, *, ensure_fresh=None) -> Worker:
    cfg = MagicMock(spec=AgentConfig)
    cfg.id = "test-agent"
    cfg.runtime = MagicMock()
    cfg.runtime.kind = runtime_kind
    cfg.runtime.harness = "claude-code"
    cfg.display_name = "Test"

    daemon_cfg = DaemonConfig()
    w = Worker(
        daemon_cfg,
        cfg,
        notify_refresh_needed=lambda: None,
        ensure_fresh_token=ensure_fresh,
    )
    w.runtime = RuntimeState(status="running")
    return w


@pytest.mark.asyncio
async def test_ensure_fresh_callback_skipped_for_non_claude_runtime():
    # ws-local (and api-puffo) don't use claude OAuth — the gate must
    # short-circuit on runtime.kind so we don't spuriously refresh.
    ensure = AsyncMock(return_value=False)
    w = _make_worker(RUNTIME_WS_LOCAL, ensure_fresh=ensure)
    should_check = (
        w._ensure_fresh_token is not None
        and w.agent_cfg.runtime.kind in (
            RUNTIME_CLI_LOCAL, "cli-docker",
        )
    )
    assert should_check is False
    assert not ensure.called


@pytest.mark.asyncio
async def test_ensure_fresh_callback_fires_for_cli_local_runtime():
    # The gate is platform-agnostic: cli-local (and cli-docker) always
    # runs it. The rotating single-use RT race exists on Linux/Windows
    # too, so gating on OS was overly restrictive.
    ensure = AsyncMock(return_value=True)
    w = _make_worker(RUNTIME_CLI_LOCAL, ensure_fresh=ensure)
    should_check = (
        w._ensure_fresh_token is not None
        and w.agent_cfg.runtime.kind in (
            RUNTIME_CLI_LOCAL, "cli-docker",
        )
    )
    assert should_check is True


@pytest.mark.asyncio
async def test_dm_dedup_one_per_expiration_episode():
    # After ``_enter_auth_failed`` fires, ``_on_auth_failed_enter`` is
    # gated by ``_auth_failed_notification_sent`` so re-entries within
    # the SAME episode don't re-DM. The flag re-arms only after the
    # daemon's on_refresh_success.
    w = _make_worker(RUNTIME_CLI_LOCAL)
    w._notify_operator_of_auth_failed_oauth = AsyncMock()

    # First trigger: schedules the DM.
    assert w._auth_failed_notification_sent is False
    w._on_auth_failed_enter()
    assert w._auth_failed_notification_sent is True

    # Second trigger in the same episode: dedup gate fires, no
    # re-schedule.
    w._on_auth_failed_enter()
    assert w._auth_failed_notification_sent is True

    # Simulate daemon's on_refresh_success re-arming the flag.
    w._auth_failed_notification_sent = False
    w._on_auth_failed_enter()
    assert w._auth_failed_notification_sent is True


def test_worker_constructor_accepts_ensure_fresh_token():
    # Belt-and-suspenders: the new kwarg is wired all the way through
    # without exploding default-None behaviour for callers that don't
    # pass it (non-claude runtimes: ws-local, api-puffo, hermes).
    w_with = _make_worker(RUNTIME_CLI_LOCAL, ensure_fresh=AsyncMock())
    assert w_with._ensure_fresh_token is not None
    w_without = _make_worker(RUNTIME_CLI_LOCAL)
    assert w_without._ensure_fresh_token is None
