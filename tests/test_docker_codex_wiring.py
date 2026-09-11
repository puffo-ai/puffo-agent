"""``DockerCLIAdapter`` codex-harness wiring.

The codex harness on cli-docker reuses the same long-lived
``CodexSession`` JSON-RPC pump the cli-local adapter uses — the only
difference is the argv, which is a ``docker exec -i`` into the agent's
container running ``codex app-server``. These tests pin the seams that
make that work without a live container or codex binary:

  * the exec argv (``-w /workspace``, ``-e CODEX_HOME=<container>``,
    ``codex app-server``);
  * the thread cwd is the container path, while the *spawn* cwd is left
    to the host (so ``docker exec`` doesn't chdir into a path that only
    exists inside the container);
  * config.toml registers the puffo MCP server with CONTAINER-local
    paths (the subprocess runs inside the container);
  * auth.json is a real-file copy, never a host symlink (a symlink
    would dangle across the bind mount).

``_ensure_codex_session`` only writes host-side files + builds the
session object, so it runs without Docker.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters import docker_cli
from puffo_agent.agent.adapters.docker_cli import (
    DockerCLIAdapter,
    _CONTAINER_CODEX_HOME,
    _CONTAINER_KEYSTORE_DIR,
)
from puffo_agent.agent.harness import CodexHarness


def _make_codex_adapter(tmp_path, *, with_mcp=True):
    adapter = DockerCLIAdapter(
        agent_id="puffpuff",
        model="gpt-5.5-codex",
        image="puffo/agent-runtime:test",
        workspace_dir=str(tmp_path / "agent" / "workspace"),
        claude_dir=str(tmp_path / "agent" / "workspace" / ".claude"),
        session_file=str(tmp_path / "agent" / "cli_session.json"),
        # agent_home_dir IS the bind-mount source for $CODEX_HOME.
        agent_home_dir=str(tmp_path / "agent"),
        shared_fs_dir=str(tmp_path / "shared"),
        harness=CodexHarness(),
    )
    if with_mcp:
        adapter.puffo_core_mcp_env = {
            "PUFFO_CORE_SLUG": "puffpuff",
            "PUFFO_CORE_DEVICE_ID": "dev_x",
            "PUFFO_CORE_SERVER_URL": "https://chat.puffo.ai/relay",
            # Host paths the worker would have set — the adapter must
            # rewrite these to container paths.
            "PUFFO_CORE_KEYSTORE_DIR": str(tmp_path / "agent" / "keys"),
            "PUFFO_WORKSPACE": str(tmp_path / "agent" / "workspace"),
            "PUFFO_DATA_SERVICE_URL": "http://host.docker.internal:63386",
            "PUFFO_RPC_URL": "http://host.docker.internal:63385",
        }
    return adapter


def _seed_host_codex_auth(host_home: Path) -> Path:
    p = host_home / ".codex" / "auth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"token": "v1"}', encoding="utf-8")
    return p


def test_codex_exec_argv_targets_container(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_cli.Path, "home", staticmethod(lambda: tmp_path / "host"))
    _seed_host_codex_auth(tmp_path / "host")
    adapter = _make_codex_adapter(tmp_path)

    session = adapter._ensure_codex_session()

    assert session.argv[:3] == ["docker", "exec", "-i"]
    assert session.argv[-2:] == ["codex", "app-server"]
    # cwd flag points at the container workspace.
    assert "-w" in session.argv
    assert session.argv[session.argv.index("-w") + 1] == "/workspace"
    # CODEX_HOME forwarded to the in-container codex process.
    assert f"CODEX_HOME={_CONTAINER_CODEX_HOME}" in session.argv
    # The container name is the exec target.
    assert adapter.container_name in session.argv


def test_codex_thread_cwd_is_container_not_host(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_cli.Path, "home", staticmethod(lambda: tmp_path / "host"))
    _seed_host_codex_auth(tmp_path / "host")
    adapter = _make_codex_adapter(tmp_path)

    session = adapter._ensure_codex_session()

    # Thread runs at the container path; the host ``docker exec`` spawn
    # must NOT chdir into /workspace (it doesn't exist on the host).
    assert session.conversation_cwd == "/workspace"
    assert session.cwd is None
    assert session.model == "gpt-5.5-codex"


def test_codex_session_is_memoised(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_cli.Path, "home", staticmethod(lambda: tmp_path / "host"))
    _seed_host_codex_auth(tmp_path / "host")
    adapter = _make_codex_adapter(tmp_path)
    assert adapter._ensure_codex_session() is adapter._ensure_codex_session()


def test_codex_config_uses_container_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_cli.Path, "home", staticmethod(lambda: tmp_path / "host"))
    _seed_host_codex_auth(tmp_path / "host")
    adapter = _make_codex_adapter(tmp_path)

    adapter._ensure_codex_session()

    config_path = tmp_path / "agent" / ".codex" / "config.toml"
    assert config_path.exists()
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["cli_auth_credentials_store"] == "file"
    puffo = data["mcp_servers"]["puffo"]
    assert puffo["command"] == "python3"
    assert puffo["args"] == ["-m", "puffo_agent.mcp.puffo_core_server"]
    env = puffo["env"]
    # Host paths rewritten to container paths.
    assert env["PUFFO_CORE_KEYSTORE_DIR"] == _CONTAINER_KEYSTORE_DIR
    assert env["PUFFO_WORKSPACE"] == "/workspace"
    assert env["PYTHONPATH"] == "/opt/puffoagent-pkg"
    assert env["PUFFO_RUNTIME_KIND"] == "cli-docker"
    assert env["PUFFO_HARNESS"] == "codex"
    assert env["CODEX_HOME"] == _CONTAINER_CODEX_HOME


def test_codex_auth_copied_not_symlinked(tmp_path, monkeypatch):
    # A host symlink would dangle inside the container; the adapter must
    # force a real-file copy into the bind-mounted $CODEX_HOME.
    monkeypatch.setattr(docker_cli.Path, "home", staticmethod(lambda: tmp_path / "host"))
    _seed_host_codex_auth(tmp_path / "host")
    adapter = _make_codex_adapter(tmp_path)

    adapter._ensure_codex_session()

    agent_auth = tmp_path / "agent" / ".codex" / "auth.json"
    assert agent_auth.exists()
    assert not agent_auth.is_symlink()
    assert agent_auth.read_text() == '{"token": "v1"}'


def test_codex_missing_auth_raises(tmp_path, monkeypatch):
    # No ~/.codex/auth.json → clear, actionable error (codex needs OAuth).
    monkeypatch.setattr(docker_cli.Path, "home", staticmethod(lambda: tmp_path / "host"))
    adapter = _make_codex_adapter(tmp_path)
    try:
        adapter._ensure_codex_session()
    except RuntimeError as exc:
        assert "codex login" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when host auth is missing")


def test_codex_config_without_mcp_still_writes_auth_block(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_cli.Path, "home", staticmethod(lambda: tmp_path / "host"))
    _seed_host_codex_auth(tmp_path / "host")
    adapter = _make_codex_adapter(tmp_path, with_mcp=False)

    adapter._ensure_codex_session()

    data = tomllib.loads(
        (tmp_path / "agent" / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    assert data["cli_auth_credentials_store"] == "file"
    # No puffo_core configured → no MCP servers registered.
    assert "puffo" not in data.get("mcp_servers", {})
