"""Docker Driver runtime boundary.

One compact test pins the Dockerfile's Codex/Claude installs and the
container argv, mounts, config.toml, exec transport, secret forwarding,
bounded stop ordering, and the Worker composition wiring that injects
the selected Driver + container stop into the Runtime Manager.
Mocked at the docker-command boundary — no daemon or credential required.

Regression guard: if the pinned codex install, agent-scoped Codex home
mount, ``/workspace`` thread cwd, container-local MCP paths, named-only
bearer forwarding, stop-before-recreate, or the Driver wiring drift, this
fails before a real container smoke would surface it.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from puffo_agent.agent.harness.drivers.codex import CodexAppServerDriver
from puffo_agent.agent.harness.drivers.claude_code import ClaudeCodeCliDriver
from puffo_agent.agent.harness.runtime import docker_support
from puffo_agent.agent.harness.runtime.docker_runtime import DockerRuntimePreparer
from puffo_agent.agent.harness.runtime.runtime_manager import RuntimeManagerAdapter
from puffo_agent.portal.state import (
    AgentConfig,
    DaemonConfig,
    PuffoCoreConfig,
    RuntimeConfig,
)
from puffo_agent.portal.worker_run import StandardWorkerRun, WorkerRunPaths
from puffo_agent.portal.workspace_layout import ensure_workspace_shared_link


def _preparer(
    tmp_path,
    monkeypatch,
    *,
    gateway=False,
    custom_memory=False,
) -> DockerRuntimePreparer:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "host"))
    config = AgentConfig(
        id="docker-codex",
        memory_dir=(str(tmp_path / "external-memory") if custom_memory else "memory"),
        runtime=RuntimeConfig(
            kind="cli-docker",
            provider="openai",
            harness="codex",
            api_key="gateway-key" if gateway else "",
            llm_base_url="https://gateway.example/v1" if gateway else "",
        ),
        puffo_core=PuffoCoreConfig(
            server_url="http://localhost:3000",
            slug="bot-0001",
            device_id="dev_1",
            space_id="sp_1",
        ),
    )
    return DockerRuntimePreparer(DaemonConfig(), config)


def _capture_run():
    captured: list[list[str]] = []

    async def fake_run(cmd, check=True, **_kwargs):
        captured.append(list(cmd))
        return 0, b"", b""

    return captured, fake_run


def _docker_patches(fake_run):
    return (
        patch(
            "puffo_agent.agent.harness.runtime.docker_runtime.resolve_docker_bin",
            lambda: "/fake/docker",
        ),
        patch("puffo_agent.agent.harness.runtime.docker_runtime.run_cmd", new=fake_run),
        patch(
            "puffo_agent.agent.harness.runtime.docker_runtime.container_state",
            new=AsyncMock(return_value="running"),
        ),
        patch(
            "puffo_agent.agent.harness.runtime.docker_runtime.ensure_docker_image",
            new=AsyncMock(),
        ),
    )


def _wire_driver_runtime(tmp_path, preparer):
    worker = SimpleNamespace(_adapter=None, agent_cfg=preparer.agent_cfg)
    run = StandardWorkerRun(worker)  # type: ignore[arg-type]
    paths = WorkerRunPaths(
        agent_id=preparer.agent_id,
        effective_harness=preparer.harness_name,
        profile_path=str(tmp_path / "profile"),
        memory_path=str(tmp_path / "memory"),
        workspace_path=str(preparer.workspace_dir),
        claude_path=str(preparer.claude_dir),
        shared_path=tmp_path / "shared",
        workspace_shared_status="mounted",
        system_prompt="prompt",
    )
    outbox_ref: list = [None]
    outbox, _session_ref, _prepared = asyncio.run(
        run._prepare_driver_runtime(paths, outbox_ref, preparer)
    )
    return outbox, worker._adapter


def _seed_local_to_docker_layout(preparer):
    preparer.agent_home.mkdir(parents=True)
    (preparer.agent_home / ".docker-layout").write_text("22\n", encoding="utf-8")
    assert (
        ensure_workspace_shared_link(preparer.workspace_dir, preparer.shared_fs_dir)
        == "created"
    )


def _assert_codex_mounts(preparer, run_cmd):
    assert f"{preparer.codex_home}:/home/agent/.codex" in run_cmd
    assert f"{preparer.agent_home}:/home/agent/.puffo-agent-state" in run_cmd
    memory_mount = f"{preparer.memory_dir}:/home/agent/.puffo-agent-state/memory"
    assert memory_mount in run_cmd
    assert any(":/workspace" in part for part in run_cmd)
    assert f"{preparer.shared_fs_dir}:/workspace/shared" in run_cmd
    assert not (preparer.workspace_dir / "shared").is_symlink()
    assert any(":/workspace/.shared" in part for part in run_cmd)
    assert any(str(part).endswith(":/opt/puffoagent-pkg:ro") for part in run_cmd)
    assert not any(str(Path.home() / ".codex") in str(part) for part in run_cmd)
    assert [part for part in run_cmd if str(part).endswith(":/home/agent/.codex")] == [
        f"{preparer.codex_home}:/home/agent/.codex"
    ]


def test_docker_codex_runtime_boundary(tmp_path, monkeypatch):
    preparer = _preparer(tmp_path, monkeypatch, gateway=True, custom_memory=True)
    (Path.home() / ".codex").mkdir(parents=True, exist_ok=True)
    (Path.home() / ".codex" / "config.toml").write_text(
        '[mcp_servers.host_only]\ncommand = "/Users/operator/bin/server"\n',
        encoding="utf-8",
    )
    _seed_local_to_docker_layout(preparer)

    assert docker_support.DEFAULT_IMAGE == "puffo/agent-runtime:v20"
    assert "mcp>=1.0,<2" in docker_support.DOCKERFILE
    assert (
        f"@openai/codex@{docker_support.CODEX_NPM_VERSION}" in docker_support.DOCKERFILE
    )
    assert (
        f"@anthropic-ai/claude-code@{docker_support.CLAUDE_CODE_NPM_VERSION}"
        in docker_support.DOCKERFILE
    )

    spec = asyncio.run(preparer.refresh_spec("prompt"))
    config = (preparer.codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'CODEX_HOME = "/home/agent/.codex"' in config
    assert 'PUFFO_WORKSPACE = "/workspace"' in config
    assert 'PUFFO_SHARED_WORKSPACE = "/workspace/shared"' in config
    assert 'PUFFO_RUNTIME_KIND = "cli-docker"' in config
    assert 'PUFFO_HARNESS = "codex"' in config
    assert "host.docker.internal" in config
    assert 'command = "python3"' in config
    assert "host_only" not in config
    assert "/Users/operator" not in config
    assert str(preparer.codex_home).replace("\\", "\\\\") not in config
    assert spec.workspace_dir == "/workspace"
    assert spec.sandbox == "danger-full-access"

    captured, fake_run = _capture_run()
    exec_calls: list[dict] = []

    class _FakeProc:
        returncode = None

    async def fake_create(*args, **kwargs):
        exec_calls.append({"args": list(args), "kwargs": kwargs})
        return _FakeProc()

    with ExitStack() as stack:
        for dependency_patch in _docker_patches(fake_run):
            stack.enter_context(dependency_patch)
        # A live prior container on a stale layout must be stopped through
        # the owned bounded lifecycle before removal — never ``rm -f`` a
        # running container.
        asyncio.run(preparer.ensure_container())
        assert captured[0] == [
            "/fake/docker",
            "stop",
            "-t",
            "5",
            "puffo-docker-codex",
        ]
        assert captured[1] == ["/fake/docker", "rm", "puffo-docker-codex"]
        run_cmd = captured[2]
        assert run_cmd[:2] == ["/fake/docker", "run"]
        _assert_codex_mounts(preparer, run_cmd)

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            asyncio.run(preparer._exec_codex_process(spec))
        command = exec_calls[0]["args"]
        assert command[-3:] == ["puffo-docker-codex", "codex", "app-server"]
        assert "gateway-key" not in command
        index = command.index("OPENAI_API_KEY")
        assert command[index - 1] == "-e"
        assert command[index + 1] != "gateway-key"
        assert "CODEX_HOME=/home/agent/.codex" in command
        assert "ANTHROPIC_API_KEY" not in command
        assert exec_calls[0]["kwargs"]["env"]["OPENAI_API_KEY"] == "gateway-key"

        outbox, adapter = _wire_driver_runtime(tmp_path, preparer)
        assert isinstance(adapter, RuntimeManagerAdapter)
        assert isinstance(adapter.manager.driver, CodexAppServerDriver)
        assert adapter.manager.driver.process_factory.__self__ is preparer
        assert adapter.manager.driver.process_factory.__func__ is (
            DockerRuntimePreparer._exec_codex_process
        )
        assert adapter.post_close.__self__ is preparer

        captured.clear()
        asyncio.run(adapter.aclose())
        assert captured == [["/fake/docker", "stop", "-t", "5", "puffo-docker-codex"]]
    outbox.close()


def test_docker_claude_uses_the_shared_driver_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    host = tmp_path / "host"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: host))
    plugins = host / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    config = AgentConfig(
        id="docker-claude",
        runtime=RuntimeConfig(
            kind="cli-docker",
            provider="anthropic",
            harness="claude-code",
            api_key="claude-secret",
            llm_base_url="https://gateway.example",
        ),
        puffo_core=PuffoCoreConfig(
            server_url="http://localhost:3000",
            slug="bot-0002",
            device_id="dev_2",
            space_id="sp_1",
        ),
    )
    preparer = DockerRuntimePreparer(DaemonConfig(), config)
    preparer.agent_home.mkdir(parents=True)
    captured, fake_run = _capture_run()
    exec_calls: list[dict] = []

    async def fake_create(*args, **kwargs):
        exec_calls.append({"args": list(args), "kwargs": kwargs})
        return SimpleNamespace(returncode=None)

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(preparer, "_sync_claude_host_assets", new=AsyncMock())
        )
        stack.enter_context(patch.object(preparer, "ensure_container", new=AsyncMock()))
        stack.enter_context(
            patch(
                "puffo_agent.agent.harness.runtime.docker_runtime.run_cmd",
                new=fake_run,
            )
        )
        spec = asyncio.run(preparer.refresh_spec("prompt"))
        preparer._docker_bin = "/fake/docker"
        asyncio.run(preparer._start_container())
        run_cmd = captured[0]
        assert f"{preparer.agent_home / '.claude'}:/home/agent/.claude" in run_cmd
        assert f"{plugins}:/home/agent/.claude/plugins:ro" in run_cmd
        assert f"{preparer.agent_home}:/home/agent/.puffo-agent-state" in run_cmd

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            asyncio.run(preparer._exec_claude_process(["claude", "-p"], spec))
        command = exec_calls[0]["args"]
        assert command[-3:] == ["puffo-docker-claude", "claude", "-p"]
        assert "claude-secret" not in command
        assert "ANTHROPIC_API_KEY" in command
        assert exec_calls[0]["kwargs"]["env"]["ANTHROPIC_API_KEY"] == ("claude-secret")

        outbox, adapter = _wire_driver_runtime(tmp_path, preparer)
        assert isinstance(adapter, RuntimeManagerAdapter)
        assert isinstance(adapter.manager.driver, ClaudeCodeCliDriver)
        assert adapter.manager.driver.process_factory.__func__ is (
            DockerRuntimePreparer._exec_claude_process
        )
    outbox.close()
