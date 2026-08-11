from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from puffo_agent.agent.adapters import docker_cli
from puffo_agent.agent.adapters.docker_cli import DockerCLIAdapter


def _adapter(tmp_path) -> DockerCLIAdapter:
    return DockerCLIAdapter(
        agent_id="safe",
        model="",
        image="puffo/agent-runtime:test",
        workspace_dir=str(tmp_path / "workspace"),
        claude_dir=str(tmp_path / "workspace" / ".claude"),
        session_file=str(tmp_path / "session.json"),
        agent_home_dir=str(tmp_path / "agent-home"),
        shared_fs_dir=str(tmp_path / "shared"),
    )


def test_probe_result_distinguishes_stale_from_infrastructure_failure():
    assert docker_cli._probe_result(0) is True
    assert docker_cli._probe_result(docker_cli._PROBE_FALSE_EXIT) is False
    assert docker_cli._probe_result(1) is None
    assert docker_cli._probe_result(125) is None


def test_container_state_distinguishes_absent_from_docker_failure(tmp_path):
    adapter = _adapter(tmp_path)

    async def absent(*_args, **_kwargs):
        return 0, b"", b""

    async def unavailable(*_args, **_kwargs):
        return 1, b"", b"daemon unavailable"

    with patch.object(docker_cli, "_run_cmd", new=absent):
        assert asyncio.run(adapter._container_state()) == ""
    with patch.object(docker_cli, "_run_cmd", new=unavailable):
        assert asyncio.run(adapter._container_state()) is None


def _patch_startup_dependencies(adapter, host_home, *, package_probe):
    return (
        patch.object(docker_cli.Path, "home", staticmethod(lambda: host_home)),
        patch.object(docker_cli, "resolve_docker_bin", lambda: "/fake/docker"),
        patch.object(docker_cli, "seed_claude_home", lambda *_args: False),
        patch.object(
            docker_cli, "sync_host_claude_code_auth_view", lambda *_args: "none",
        ),
        patch.object(docker_cli, "sync_host_skills", lambda *_args: 0),
        patch.object(
            docker_cli,
            "sync_host_mcp_servers",
            lambda *_args, **_kwargs: (0, []),
        ),
        patch.object(docker_cli, "sync_host_enabled_plugins", lambda *_args: 0),
        patch.object(adapter, "_install_desired", new=AsyncMock()),
        patch.object(adapter, "_container_state", new=AsyncMock(return_value="running")),
        patch.object(
            adapter,
            "_puffo_pkg_mount_is_current",
            new=AsyncMock(return_value=package_probe),
        ),
        patch.object(
            adapter,
            "_container_harness_is_current",
            new=AsyncMock(return_value=True),
        ),
        patch.object(adapter, "_ensure_image", new=AsyncMock()),
        patch.object(adapter, "_start_container", new=AsyncMock()),
    )


def test_stale_rebuild_preserves_host_workspace_and_claude_state(tmp_path):
    adapter = _adapter(tmp_path)
    workspace_sentinel = tmp_path / "workspace" / "keep.txt"
    claude_sentinel = adapter.claude_home_src / "session.json"
    workspace_sentinel.parent.mkdir(parents=True)
    claude_sentinel.parent.mkdir(parents=True)
    workspace_sentinel.write_text("workspace", encoding="utf-8")
    claude_sentinel.write_text("session", encoding="utf-8")
    (adapter.agent_home_dir / ".docker-layout").write_text(
        docker_cli.CONTAINER_LAYOUT_VERSION, encoding="utf-8",
    )
    captured: list[list[str]] = []

    async def capture(cmd, check=True, **_kwargs):
        captured.append(list(cmd))
        return 0, b"", b""

    patches = _patch_startup_dependencies(
        adapter, tmp_path / "host", package_probe=False,
    )
    with ExitStack() as stack:
        for dependency_patch in patches:
            stack.enter_context(dependency_patch)
        stack.enter_context(patch.object(docker_cli, "_run_cmd", new=capture))
        asyncio.run(adapter._ensure_started())

    assert ["/fake/docker", "rm", "-f", adapter.container_name] in captured
    assert workspace_sentinel.read_text(encoding="utf-8") == "workspace"
    assert claude_sentinel.read_text(encoding="utf-8") == "session"


def test_unknown_staleness_probe_never_removes_container(tmp_path):
    adapter = _adapter(tmp_path)
    captured: list[list[str]] = []

    async def capture(cmd, check=True, **_kwargs):
        captured.append(list(cmd))
        return 0, b"", b""

    patches = _patch_startup_dependencies(
        adapter, tmp_path / "host", package_probe=None,
    )
    with ExitStack() as stack:
        for dependency_patch in patches:
            stack.enter_context(dependency_patch)
        stack.enter_context(patch.object(docker_cli, "_run_cmd", new=capture))
        with pytest.raises(RuntimeError, match="refusing to remove"):
            asyncio.run(adapter._ensure_started())

    assert not any(cmd[1:4] == ["rm", "-f", adapter.container_name] for cmd in captured)


class _HangingProcess:
    def __init__(self):
        self.returncode = None
        self.killed = False
        self.reaped = False
        self._done = asyncio.Event()

    async def communicate(self, _input=None):
        await self._done.wait()
        self.reaped = True
        return b"", b""

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


def test_docker_timeout_kills_and_reaps_child():
    async def scenario():
        proc = _HangingProcess()
        with pytest.raises(RuntimeError, match="timed out"):
            await docker_cli._communicate_with_timeout(
                proc,
                timeout_seconds=0.01,
                operation="docker inspect",
            )
        assert proc.killed is True
        assert proc.reaped is True

    asyncio.run(scenario())


def test_docker_cancellation_kills_and_reaps_child():
    async def scenario():
        proc = _HangingProcess()
        task = asyncio.create_task(docker_cli._communicate_with_timeout(
            proc,
            timeout_seconds=60,
            operation="docker inspect",
        ))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.killed is True
        assert proc.reaped is True

    asyncio.run(scenario())
