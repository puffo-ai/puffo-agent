"""Migration contract from the pre-Driver local Puffo Agent runtime."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from puffo_agent.agent.harness.driver import (
    CancelReceipt,
    DriverCapabilities,
    Driver,
    ProtocolDiagnostics,
    RuntimeOpened,
    RuntimeRef,
    RuntimeSpec,
    SessionRef,
    TurnInput,
    TurnRef,
    UnsupportedCapability,
)
from puffo_agent.agent.harness.local_runtime import LocalRuntimePreparer
from puffo_agent.agent.harness.runtime_manager import (
    RuntimeManager,
    RuntimeManagerAdapter,
)
from puffo_agent.portal.state import (
    AgentConfig,
    DaemonConfig,
    RuntimeConfig,
    agent_codex_user_dir,
    agent_home_dir,
    cli_session_json_path,
)


@pytest.fixture
def puffo_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "puffo"
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: host))
    return home


@pytest.mark.asyncio
async def test_old_claude_agent_reuses_paths_and_imports_session(
    puffo_home, monkeypatch,
):
    import puffo_agent.agent.harness.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime, "resolve_claude_bin", lambda: "/opt/bin/claude"
    )
    monkeypatch.setattr(
        local_runtime,
        "sync_host_claude_code_auth_view",
        lambda *_args: "view",
    )
    agent_id = "old-claude"
    old_session = cli_session_json_path(agent_id)
    old_session.parent.mkdir(parents=True)
    old_session.write_text(
        json.dumps({
            "session_id": "claude-session-old",
            "model": "claude-sonnet-4-6",
        }),
        encoding="utf-8",
    )
    config = AgentConfig(
        id=agent_id,
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="anthropic",
            harness="claude-code",
            model="claude-sonnet-4-6",
        ),
    )

    prepared = await LocalRuntimePreparer(
        DaemonConfig(), config
    ).prepare(system_prompt="managed prompt")

    assert prepared.native_session_id == "claude-session-old"
    assert prepared.migration_source == "legacy_session_file"
    assert prepared.spec.workspace_dir == str(config.resolve_workspace_dir())
    assert prepared.spec.environment["HOME"] == str(agent_home_dir(agent_id))
    assert prepared.spec.executable == "/opt/bin/claude"
    assert old_session.exists(), "prepare must not delete state before open"
    prepared.finalize_legacy_session_migration()
    assert not old_session.exists()


@pytest.mark.asyncio
async def test_old_codex_agent_imports_session_with_matching_sandbox(
    puffo_home, monkeypatch,
):
    import puffo_agent.agent.harness.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime, "resolve_codex_bin", lambda: "/opt/bin/codex"
    )
    monkeypatch.setattr(local_runtime, "is_macos", lambda: False)
    monkeypatch.setattr(
        local_runtime, "sync_host_codex_auth_view", lambda *_args: "view"
    )
    agent_id = "old-codex"
    old_session = agent_codex_user_dir(agent_id) / "codex_session.json"
    old_session.parent.mkdir(parents=True)
    old_session.write_text(
        json.dumps({
            "conversation_id": "codex-thread-old",
            "sandbox": "workspace-write",
        }),
        encoding="utf-8",
    )
    config = AgentConfig(
        id=agent_id,
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="openai",
            harness="codex",
            sandbox="workspace-write",
        ),
    )

    prepared = await LocalRuntimePreparer(
        DaemonConfig(), config
    ).prepare(system_prompt="managed prompt")

    assert prepared.native_session_id == "codex-thread-old"
    assert prepared.spec.environment["CODEX_HOME"] == str(
        agent_codex_user_dir(agent_id)
    )
    assert prepared.spec.executable == "/opt/bin/codex"
    assert old_session.exists()
    prepared.finalize_legacy_session_migration()
    assert not old_session.exists()


@pytest.mark.asyncio
async def test_durable_session_resumes_only_with_matching_fingerprint(
    puffo_home, monkeypatch,
):
    import puffo_agent.agent.harness.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime, "resolve_claude_bin", lambda: "/opt/bin/claude"
    )
    monkeypatch.setattr(
        local_runtime,
        "sync_host_claude_code_auth_view",
        lambda *_args: "view",
    )
    config = AgentConfig(
        id="fingerprinted-claude",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="anthropic",
            harness="claude-code",
        ),
    )
    preparer = LocalRuntimePreparer(DaemonConfig(), config)
    baseline = await preparer.prepare(system_prompt="stable prompt")
    resumed = await preparer.prepare(
        system_prompt="stable prompt",
        persisted_native_session_id="native-old",
        persisted_session_fingerprint=baseline.session_fingerprint,
    )
    rotated = await preparer.prepare(
        system_prompt="changed identity prompt",
        persisted_native_session_id="native-old",
        persisted_session_fingerprint=baseline.session_fingerprint,
    )

    assert resumed.native_session_id == "native-old"
    assert not resumed.discarded_persisted_session
    assert rotated.native_session_id == ""
    assert rotated.discarded_persisted_session


class _ResumeFallbackDriver(Driver):
    def __init__(self):
        self.open_calls: list[str | None] = []
        self.close_calls = 0
        self.queue: asyncio.Queue = asyncio.Queue()

    async def open(self, spec, resume=None):
        self.open_calls.append(str(resume) if resume is not None else None)
        if resume is not None:
            raise RuntimeError("native transcript is gone")
        return RuntimeOpened(
            RuntimeRef("runtime-new"),
            SessionRef("native-new"),
            "native-new",
            False,
            DriverCapabilities(False, False, "none", "none", "none", "none", False),
            ProtocolDiagnostics(),
        )

    async def start_turn(self, input: TurnInput):
        return UnsupportedCapability("start")

    async def steer_turn(self, turn: TurnRef, input: TurnInput):
        return UnsupportedCapability("steer")

    async def cancel_turn(self, turn: TurnRef):
        return CancelReceipt(False, turn)

    async def context_status(self):
        return UnsupportedCapability("context")

    async def compact(self, request):
        return UnsupportedCapability("compact")

    async def resolve_permission(self, request, decision):
        return UnsupportedCapability("permission")

    def events(self):
        async def iterate():
            while True:
                event = await self.queue.get()
                if event is None:
                    return
                yield event
        return iterate()

    async def close(self):
        self.close_calls += 1


@pytest.mark.asyncio
async def test_missing_old_native_session_recovers_with_fresh_driver_session():
    driver = _ResumeFallbackDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec(workspace_dir="/tmp"),
        native_session_id="native-old",
        driver_name="test-driver",
    )
    adapter = RuntimeManagerAdapter(manager)

    assert adapter.get_provider_session_id() == "native-old"
    await adapter.warm("")

    assert driver.open_calls == ["native-old", None]
    assert adapter.get_provider_session_id() == "native-new"
    await adapter.aclose()
