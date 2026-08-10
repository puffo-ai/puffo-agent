from __future__ import annotations

from types import SimpleNamespace

import pytest

from puffo_agent.portal.state import (
    AgentConfig,
    DaemonConfig,
    ProviderConfig,
    RuntimeConfig,
)
from puffo_agent.portal.worker import Worker


def test_runtime_info_reports_effective_codex_defaults():
    daemon = DaemonConfig(openai=ProviderConfig(model="gpt-5.4"))
    agent = AgentConfig(
        id="cloud-agent",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="",
            harness="codex",
            model="",
        ),
    )

    worker = Worker(daemon, agent)

    assert worker._runtime_info() == {
        "kind": "cli-local",
        "provider": "openai",
        "harness": "codex",
        "model": "gpt-5.4",
    }


def test_runtime_info_reports_explicit_codex_driver_and_model():
    daemon = DaemonConfig(
        default_provider="openai",
        openai=ProviderConfig(model="gpt-4.1"),
    )
    agent = AgentConfig(
        id="cloud-agent",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="openai",
            harness="codex",
        ),
    )

    worker = Worker(daemon, agent)

    assert worker._runtime_info() == {
        "kind": "cli-local",
        "provider": "openai",
        "harness": "codex",
        "model": "gpt-4.1",
    }


def test_runtime_info_does_not_invent_ws_local_provider():
    worker = Worker(
        DaemonConfig(),
        AgentConfig(
            id="wallet-agent",
            runtime=RuntimeConfig(kind="ws-local"),
        ),
    )

    assert worker._runtime_info() == {
        "kind": "ws-local",
        "provider": "",
        "harness": "",
        "model": "",
    }


@pytest.mark.asyncio
async def test_worker_registers_reconnect_status_callback():
    callbacks = []
    sent = []

    class Bridge:
        async def send_status(self, status, **kwargs):
            sent.append((status, kwargs))

        def add_connected_callback(self, callback):
            callbacks.append(callback)

    worker = Worker(
        DaemonConfig(openai=ProviderConfig(model="gpt-5.4")),
        AgentConfig(
            id="cloud-agent",
            runtime=RuntimeConfig(
                kind="cli-local",
                harness="codex",
            ),
        ),
    )
    bridge = Bridge()
    client = SimpleNamespace(
        http=SimpleNamespace(keyless=True),
        _bridge=bridge,
    )

    reporter = worker._build_status_reporter(client)

    assert callbacks == [reporter.report_current_status]
    await callbacks[0]()
    assert sent == [(
        "idle",
        {
            "current_message_id": None,
            "error_text": None,
            "runtime": {
                "kind": "cli-local",
                "provider": "openai",
                "harness": "codex",
                "model": "gpt-5.4",
            },
        },
    )]
