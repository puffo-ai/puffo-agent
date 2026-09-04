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
from puffo_agent.agent.harness.runtime.local_runtime import (
    LocalRuntimePreparer,
    select_native_session,
)
from puffo_agent.agent.harness.runtime.runtime_manager import (
    RuntimeManager,
    RuntimeManagerAdapter,
)
from puffo_agent.portal.state import (
    AgentConfig,
    DaemonConfig,
    PuffoCoreConfig,
    RuntimeConfig,
    agent_codex_user_dir,
    agent_dir,
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


@pytest.mark.parametrize("harness_name", ["pi", "opencode", "acp"])
def test_generic_harness_ignores_legacy_claude_session(
    harness_name: str,
):
    native_session_id, migration_source = select_native_session(
        harness_name=harness_name,
        persisted_native_session_id="tagged-claude-session",
        persisted_native_session_harness="claude-code",
        legacy_native_session_id="legacy-claude-session",
    )

    assert native_session_id == ""
    assert migration_source == "harness_changed"


@pytest.mark.asyncio
async def test_legacy_claude_file_cannot_reach_switched_opencode(
    puffo_home, monkeypatch,
):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime,
        "resolve_opencode_bin",
        lambda: "/opt/bin/opencode",
    )
    agent_id = "switched-agent"
    config = AgentConfig(
        id=agent_id,
        runtime=RuntimeConfig(
            kind="cli-local",
            harness="opencode",
            model="deepseek/deepseek-chat",
        ),
    )
    agent_dir(agent_id).mkdir(parents=True, exist_ok=True)
    # Old Claude files can predate the model field, so model comparison
    # cannot establish ownership of this session.
    cli_session_json_path(agent_id).write_text(
        json.dumps({"session_id": "claude-era-session"}),
        encoding="utf-8",
    )

    prepared = await LocalRuntimePreparer(
        DaemonConfig(), config
    ).prepare(
        system_prompt="managed prompt",
        persisted_native_session_id="claude-era-session",
        persisted_native_session_harness="claude-code",
    )

    assert prepared.native_session_id == ""
    assert prepared.migration_source == "harness_changed"


@pytest.mark.asyncio
async def test_old_claude_agent_reuses_paths_and_imports_session(
    puffo_home, monkeypatch,
):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

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
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

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
async def test_durable_session_resumes_across_prompt_refresh(
    puffo_home, monkeypatch,
):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime, "resolve_claude_bin", lambda: "/opt/bin/claude"
    )
    monkeypatch.setattr(
        local_runtime,
        "sync_host_claude_code_auth_view",
        lambda *_args: "view",
    )
    config = AgentConfig(
        id="durable-claude",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="anthropic",
            harness="claude-code",
        ),
    )
    preparer = LocalRuntimePreparer(DaemonConfig(), config)
    resumed = await preparer.prepare(
        system_prompt="stable prompt",
        persisted_native_session_id="native-old",
        persisted_native_session_harness="claude-code",
    )
    refreshed = await preparer.prepare(
        system_prompt="changed identity prompt",
        persisted_native_session_id="native-old",
        persisted_native_session_harness="claude-code",
    )

    assert resumed.native_session_id == "native-old"
    assert refreshed.native_session_id == "native-old"


@pytest.mark.asyncio
async def test_durable_session_is_not_reused_after_harness_change(
    puffo_home, monkeypatch,
):
    """A Claude session ID must never be passed to OpenCode on restart."""
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime, "resolve_opencode_bin", lambda: "/opt/bin/opencode"
    )
    config = AgentConfig(
        id="swapped-to-opencode",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="openai",
            harness="opencode",
        ),
    )

    prepared = await LocalRuntimePreparer(
        DaemonConfig(), config
    ).prepare(
        system_prompt="managed prompt",
        persisted_native_session_id="claude-session-old",
        persisted_native_session_harness="claude-code",
    )

    assert prepared.native_session_id == ""
    assert prepared.migration_source == "harness_changed"


@pytest.mark.asyncio
async def test_generic_acp_command_projects_to_runtime_spec(puffo_home):
    config = AgentConfig(
        id="generic-acp",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="google",
            harness="acp",
            harness_command=["/opt/bin/gemini", "--experimental-acp"],
        ),
    )

    prepared = await LocalRuntimePreparer(
        DaemonConfig(), config
    ).prepare(system_prompt="managed prompt")

    assert prepared.harness_name == "acp"
    assert prepared.spec.executable == "/opt/bin/gemini"
    assert prepared.spec.launch_args == ("--experimental-acp",)


@pytest.mark.asyncio
async def test_opencode_uses_shared_binary_resolver(puffo_home, monkeypatch):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime, "resolve_opencode_bin", lambda: "/opt/bin/opencode"
    )
    config = AgentConfig(
        id="opencode-agent",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="anthropic",
            harness="opencode",
        ),
    )

    prepared = await LocalRuntimePreparer(
        DaemonConfig(), config
    ).prepare(system_prompt="managed prompt")

    assert prepared.harness_name == "opencode"
    assert prepared.spec.executable == "/opt/bin/opencode"
    inline = json.loads(prepared.spec.environment["OPENCODE_CONFIG_CONTENT"])
    instruction_path = Path(inline["instructions"][0])
    assert instruction_path.read_text(encoding="utf-8") == "managed prompt"


@pytest.mark.asyncio
async def test_pi_uses_shared_binary_resolver(puffo_home, monkeypatch):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime, "resolve_pi_bin", lambda: "/opt/bin/pi"
    )
    config = AgentConfig(
        id="pi-agent",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="openai",
            harness="pi",
        ),
    )

    prepared = await LocalRuntimePreparer(
        DaemonConfig(), config
    ).prepare(system_prompt="managed prompt")

    assert prepared.harness_name == "pi"
    assert prepared.spec.executable == "/opt/bin/pi"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "command"),
    [
        ("opencode", []),
        ("acp", ["/opt/bin/opencode", "acp"]),
    ],
)
async def test_generic_runtime_projects_puffo_tools(
    puffo_home, monkeypatch, harness, command,
):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime, "resolve_opencode_bin", lambda: "/opt/bin/opencode"
    )
    config = AgentConfig(
        id=f"{harness}-tools",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="anthropic",
            harness=harness,
            harness_command=command,
        ),
        puffo_core=PuffoCoreConfig(
            server_url="http://localhost:3000",
            slug="bot-0001",
            device_id="dev_1",
            space_id="sp_test",
        ),
    )

    prepared = await LocalRuntimePreparer(
        DaemonConfig(), config
    ).prepare(system_prompt="managed prompt")

    assert len(prepared.spec.mcp_servers) == 1
    server = prepared.spec.mcp_servers[0]
    assert server.name == "puffo"
    assert server.args == ("-m", "puffo_agent.mcp.puffo_core_server")
    assert server.environment["PUFFO_CORE_SLUG"] == "bot-0001"
    inline = json.loads(prepared.spec.environment["OPENCODE_CONFIG_CONTENT"])
    instruction_path = Path(inline["instructions"][0])
    assert instruction_path.read_text(encoding="utf-8") == "managed prompt"
    if harness == "opencode":
        assert inline["mcp"]["puffo"]["command"] == [
            server.command,
            *server.args,
        ]
    else:
        assert "mcp" not in inline


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [[], ["pi"]])
@pytest.mark.parametrize(
    ("model", "expected_args"),
    [("", ("--provider", "openai")), ("anthropic/claude-opus-4-8", ())],
)
async def test_pi_runtime_uses_daemon_binary_resolver_and_native_targeting(
    puffo_home, monkeypatch, command, model, expected_args,
):
    """Web defaults and legacy bare argv must survive a service's narrow PATH."""
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

    monkeypatch.setattr(local_runtime, "resolve_pi_bin", lambda: "/opt/bin/pi")
    config = AgentConfig(
        id="pi-resolver",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="openai",
            harness="pi",
            model=model,
            harness_command=command,
        ),
        puffo_core=PuffoCoreConfig(
            server_url="http://localhost:3000",
            slug="bot-0001",
            device_id="dev_1",
            space_id="sp_test",
        ),
    )

    prepared = await LocalRuntimePreparer(
        DaemonConfig(), config
    ).prepare(system_prompt="managed prompt")

    assert prepared.harness_name == "pi"
    assert prepared.spec.executable == "/opt/bin/pi"
    assert prepared.spec.launch_args == expected_args
    assert prepared.spec.mcp_servers == ()


@pytest.mark.asyncio
async def test_pi_runtime_forwards_selected_thinking_level(
    puffo_home, monkeypatch,
):
    """A level admitted by the Pi catalog must reach the Pi child process."""
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

    monkeypatch.setattr(local_runtime, "resolve_pi_bin", lambda: "/opt/bin/pi")
    config = AgentConfig(
        id="pi-thinking",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="openai-codex",
            harness="pi",
            model="openai-codex/gpt-5.4-mini",
            inference_level="high",
        ),
    )

    prepared = await LocalRuntimePreparer(
        DaemonConfig(), config
    ).prepare(system_prompt="managed prompt")

    assert prepared.spec.launch_args == ("--thinking", "high")


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
