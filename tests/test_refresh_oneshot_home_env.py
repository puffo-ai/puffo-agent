"""PUF-217: verify _run_refresh_oneshot inherits the operator's HOME
so claude writes the refreshed credentials to the canonical host file
directly, instead of via the per-agent symlink (where atomic
tmp+rename clobbers the symlink and puffo-agent's own re-sync then
copies stale host content back over the fresh token)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


def _make_adapter(
    agent_home: Path, workspace: Path, monkeypatch,
) -> LocalCLIAdapter:
    agent_home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    adapter = LocalCLIAdapter(
        agent_id="agent-puf217",
        model="claude-sonnet-4-6",
        workspace_dir=str(workspace),
        claude_dir=str(claude_dir),
        session_file=str(workspace / "session.json"),
        mcp_config_file=str(workspace / "mcp.json"),
        agent_home_dir=str(agent_home),
    )
    # _verify would shell out to ``shutil.which("claude")`` and seed
    # the agent's virtual HOME from the operator's. No-op it here so
    # the test rides on the public method rather than coupling to the
    # private ``_verified`` cache flag.
    monkeypatch.setattr(adapter, "_verify", lambda: None)
    return adapter


def test_refresh_oneshot_inherits_operator_home(tmp_path, monkeypatch):
    """The fix: env passed to claude --print must NOT override HOME
    or USERPROFILE. Operator's HOME flows through unchanged so claude
    resolves ~/.claude/.credentials.json to the host file directly."""
    monkeypatch.setenv("HOME", "/home/test-operator")
    monkeypatch.setenv("USERPROFILE", "/home/test-operator")
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = _make_adapter(tmp_path / "agent_home", tmp_path / "workspace", monkeypatch)
    asyncio.run(adapter._run_refresh_oneshot())

    env = captured["env"]
    assert env is not None
    # The contract: env's HOME / USERPROFILE pass through unchanged.
    # Pre-fix, these were overridden to the agent_home_dir.
    assert env.get("HOME") == "/home/test-operator"
    assert env.get("USERPROFILE") == "/home/test-operator"
    # Defensive: NOT pointing at the agent_home (the pre-fix value).
    assert env.get("HOME") != str(adapter.agent_home_dir)


def test_refresh_oneshot_write_lands_at_host_path_visible_via_agent_symlink(
    tmp_path, monkeypatch,
):
    """End-to-end-ish: simulate claude writing a fresh token at the
    host path (the env's HOME), then assert the agent's symlinked
    .credentials.json surfaces the new value to puffo-agent's read
    path. Locks in the symlink-distribution contract this fix
    depends on."""
    host_home = tmp_path / "operator_home"
    agent_home = tmp_path / "agent_home"
    (host_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("USERPROFILE", str(host_home))

    # Seed a stale credentials file at the host path.
    host_creds = host_home / ".claude" / ".credentials.json"
    stale_expires_ms = int((time.time() - 60) * 1000)
    host_creds.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "stale", "expiresAt": stale_expires_ms},
    }))

    fresh_expires_ms = int((time.time() + 3600) * 1000)

    async def fake_exec(*argv, **kwargs):
        target = Path(kwargs["env"]["HOME"]) / ".claude" / ".credentials.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Mimic atomic tmp+rename. Under the fix, this rename happens
        # at the host path, not at the agent symlink — so the agent
        # symlink stays intact.
        tmp_target = target.with_suffix(".tmp")
        tmp_target.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "fresh", "expiresAt": fresh_expires_ms},
        }))
        os.rename(tmp_target, target)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda *a, **k: "/usr/local/bin/claude")

    adapter = _make_adapter(agent_home, tmp_path / "workspace", monkeypatch)

    # First touch: link_host_credentials creates the symlink so the
    # agent has a view onto the host file.
    initial_ttl = adapter._credentials_expires_in_seconds()
    assert initial_ttl is not None
    assert initial_ttl < 0  # stale

    asyncio.run(adapter._run_refresh_oneshot())

    # The agent's symlink must still resolve to a file with the new
    # token visible. (If the symlink had been clobbered by a write
    # via the agent path, this assertion would fail.)
    agent_creds = agent_home / ".claude" / ".credentials.json"
    assert agent_creds.is_symlink(), "agent .credentials.json must remain a symlink"
    assert json.loads(agent_creds.read_text())["claudeAiOauth"]["accessToken"] == "fresh"

    # And puffo-agent's read path picks up the fresh expiry.
    refreshed_ttl = adapter._credentials_expires_in_seconds()
    assert refreshed_ttl is not None
    assert refreshed_ttl > 0


def test_refresh_oneshot_does_not_create_regular_file_at_agent_path(
    tmp_path, monkeypatch,
):
    """Anti-regression for the amplification path: even if a future
    refactor reintroduces a HOME override, the agent path must never
    end up with a regular file (which would let
    link_host_credentials's copy-mode clobber the fresh token with
    the stale host content)."""
    host_home = tmp_path / "operator_home"
    agent_home = tmp_path / "agent_home"
    (host_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("USERPROFILE", str(host_home))

    fresh_expires_ms = int((time.time() + 3600) * 1000)
    host_home_creds = host_home / ".claude" / ".credentials.json"
    host_home_creds.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "seed", "expiresAt": fresh_expires_ms},
    }))

    async def fake_exec(*argv, **kwargs):
        target = Path(kwargs["env"]["HOME"]) / ".claude" / ".credentials.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target.with_suffix(".tmp")
        tmp_target.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "post-refresh", "expiresAt": fresh_expires_ms},
        }))
        os.rename(tmp_target, target)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda *a, **k: "/usr/local/bin/claude")

    adapter = _make_adapter(agent_home, tmp_path / "workspace", monkeypatch)
    # Establish the symlink first.
    adapter._credentials_expires_in_seconds()
    asyncio.run(adapter._run_refresh_oneshot())

    agent_creds = agent_home / ".claude" / ".credentials.json"
    # The load-bearing assertion: the agent path is STILL a symlink
    # after the refresh, never a regular file.
    assert agent_creds.is_symlink()
    # And no rogue tmp file was left at the agent path.
    assert not (agent_home / ".claude" / ".credentials.tmp").exists()
