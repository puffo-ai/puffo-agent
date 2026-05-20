"""Codex daemon-owned credential refresh — file-mode auth.json with
JWT-exp-based expiry scheduling and ``codex exec`` one-shot refresh.

Mirrors test_credential_refresher.py's coverage shape for the claude
side (PUF-221), but against ``CodexFileBackend``."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

import pytest

from puffo_agent.portal import credential_refresh
from puffo_agent.portal.credential_refresh import (
    CodexFileBackend,
    CredentialRefresher,
    _jwt_exp_unix,
)


def _make_jwt(exp_unix: int) -> str:
    """Build a fake JWT whose ``exp`` claim is ``exp_unix``. Signature
    isn't validated by puffo-agent code; we put a placeholder there.
    """
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    ).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp_unix, "sub": "test-user"}).encode()
    ).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(b"fake-sig").decode().rstrip("=")
    return f"{header}.{payload}.{sig}"


def _write_codex_auth(
    host_home: Path,
    *,
    expires_in_seconds: int | None = 3600,
    access_token: str | None = None,
) -> Path:
    auth_path = host_home / ".codex" / "auth.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    if access_token is None and expires_in_seconds is not None:
        access_token = _make_jwt(int(time.time()) + expires_in_seconds)
    payload = {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": "fake-id",
            "access_token": access_token,
            "refresh_token": "fake-rt",
            "account_id": "acct-123",
        },
        "last_refresh": "2026-05-19T12:00:00.000Z",
    }
    auth_path.write_text(json.dumps(payload), encoding="utf-8")
    return auth_path


# ── _jwt_exp_unix helper ────────────────────────────────────────


def test_jwt_exp_unix_extracts_claim():
    exp = int(time.time()) + 1234
    tok = _make_jwt(exp)
    assert _jwt_exp_unix(tok) == exp


def test_jwt_exp_unix_handles_malformed_token():
    assert _jwt_exp_unix("not-a-jwt") is None
    assert _jwt_exp_unix("only.two") is None
    assert _jwt_exp_unix("a.b.c.d") is None


def test_jwt_exp_unix_handles_undecodable_payload():
    assert _jwt_exp_unix("header.!!not-base64!!.sig") is None


def test_jwt_exp_unix_handles_missing_exp_claim():
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "x"}).encode()
    ).decode().rstrip("=")
    assert _jwt_exp_unix(f"hdr.{payload}.sig") is None


def test_jwt_exp_unix_handles_non_numeric_exp():
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": "tomorrow"}).encode()
    ).decode().rstrip("=")
    assert _jwt_exp_unix(f"hdr.{payload}.sig") is None


# ── CodexFileBackend.expires_in_seconds ─────────────────────────


def test_expires_in_seconds_reads_jwt_exp(tmp_path):
    _write_codex_auth(tmp_path, expires_in_seconds=600)
    b = CodexFileBackend(host_home=tmp_path)
    ttl = b.expires_in_seconds()
    assert ttl is not None
    assert 595 <= ttl <= 605


def test_expires_in_seconds_handles_missing_auth_file(tmp_path):
    b = CodexFileBackend(host_home=tmp_path)
    assert b.expires_in_seconds() is None


def test_expires_in_seconds_handles_corrupt_json(tmp_path):
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{not json")
    b = CodexFileBackend(host_home=tmp_path)
    assert b.expires_in_seconds() is None


def test_expires_in_seconds_handles_missing_access_token(tmp_path):
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {}}))
    b = CodexFileBackend(host_home=tmp_path)
    assert b.expires_in_seconds() is None


def test_expires_in_seconds_handles_opaque_access_token(tmp_path):
    """A non-JWT access_token (operator on api_key mode, or codex
    switching token format upstream) → TTL is None, not a crash."""
    _write_codex_auth(tmp_path, access_token="not-a-jwt-token")
    b = CodexFileBackend(host_home=tmp_path)
    assert b.expires_in_seconds() is None


# ── CodexFileBackend.sync_to_agent ──────────────────────────────


def test_sync_to_agent_skips_non_codex_agents(tmp_path, monkeypatch):
    """Agent home without a ``.codex/`` subdir is a claude-only agent;
    we shouldn't clutter it with a stray auth.json symlink."""
    _write_codex_auth(tmp_path)
    b = CodexFileBackend(host_home=tmp_path)

    called = []
    monkeypatch.setattr(
        credential_refresh, "link_host_codex_auth",
        lambda host, agent_codex: called.append((host, agent_codex)) or "symlink",
    )
    claude_only_agent = tmp_path / "agent_claude"
    claude_only_agent.mkdir()
    b.sync_to_agent(claude_only_agent)
    assert called == []


def test_sync_to_agent_links_when_codex_dir_exists(tmp_path, monkeypatch):
    _write_codex_auth(tmp_path)
    b = CodexFileBackend(host_home=tmp_path)

    called: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        credential_refresh, "link_host_codex_auth",
        lambda host, agent_codex: called.append((host, agent_codex)) or "symlink",
    )
    codex_agent = tmp_path / "agent_codex"
    (codex_agent / ".codex").mkdir(parents=True)
    b.sync_to_agent(codex_agent)
    assert called == [(tmp_path, codex_agent / ".codex")]


# ── CodexFileBackend.bootstrap ──────────────────────────────────


def test_bootstrap_reports_missing_host_auth(tmp_path):
    b = CodexFileBackend(host_home=tmp_path)
    ok, reason = asyncio.run(b.bootstrap())
    assert ok is False
    assert "no-host" in (reason or "")


def test_bootstrap_ok_when_host_auth_present(tmp_path):
    _write_codex_auth(tmp_path)
    b = CodexFileBackend(host_home=tmp_path)
    ok, reason = asyncio.run(b.bootstrap())
    assert ok is True
    assert reason and "authoritative" in reason


# ── CodexFileBackend.refresh ────────────────────────────────────


def test_refresh_spawns_codex_exec_oneshot(tmp_path, monkeypatch):
    _write_codex_auth(tmp_path, expires_in_seconds=60)
    b = CodexFileBackend(host_home=tmp_path)

    spawned: list = []

    async def fake_exec(*argv, **kwargs):
        spawned.append({"argv": argv, "env_home": kwargs.get("env", {}).get("HOME")})
        # Simulate codex rotating the token — advance JWT exp.
        _write_codex_auth(tmp_path, expires_in_seconds=3600)
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        credential_refresh, "_resolve_codex_bin", lambda: "codex",
    )

    outcome = asyncio.run(b.refresh())
    assert outcome == credential_refresh.RefreshOutcome.REFRESHED
    assert len(spawned) == 1
    argv = spawned[0]["argv"]
    assert argv[0] == "codex"
    assert "exec" in argv
    assert "--ephemeral" in argv
    assert "--skip-git-repo-check" in argv
    # HOME env points at host so codex reads ~/.codex/auth.json.
    assert spawned[0]["env_home"] == str(tmp_path)


def test_refresh_returns_failed_when_codex_binary_missing(tmp_path, monkeypatch):
    _write_codex_auth(tmp_path, expires_in_seconds=60)
    b = CodexFileBackend(host_home=tmp_path)
    monkeypatch.setattr(
        credential_refresh, "_resolve_codex_bin", lambda: None,
    )

    outcome = asyncio.run(b.refresh())
    assert outcome == credential_refresh.RefreshOutcome.FAILED


def test_refresh_returns_unchanged_when_exp_did_not_advance(tmp_path, monkeypatch):
    """codex exec exit=0 but JWT exp unchanged → operator likely
    runs cli_auth_credentials_store=keyring; auth.json is stale.
    Surface this as UNCHANGED so the next tick can retry rather
    than burn a refresh slot."""
    _write_codex_auth(tmp_path, expires_in_seconds=60)
    b = CodexFileBackend(host_home=tmp_path)

    async def fake_exec(*argv, **kwargs):
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        credential_refresh, "_resolve_codex_bin", lambda: "codex",
    )

    outcome = asyncio.run(b.refresh())
    assert outcome == credential_refresh.RefreshOutcome.UNCHANGED


def test_refresh_returns_failed_on_subprocess_timeout(tmp_path, monkeypatch):
    _write_codex_auth(tmp_path, expires_in_seconds=60)
    b = CodexFileBackend(host_home=tmp_path)

    async def fake_exec(*argv, **kwargs):
        class _Proc:
            returncode = None
            async def communicate(self):
                # Sleep longer than the wait_for budget so wait_for
                # raises TimeoutError on the caller side.
                await asyncio.sleep(10)
                return b"", b""
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        credential_refresh, "_resolve_codex_bin", lambda: "codex",
    )
    monkeypatch.setattr(
        credential_refresh, "REFRESH_ONESHOT_TIMEOUT_SECONDS", 0.01,
    )

    outcome = asyncio.run(b.refresh())
    assert outcome == credential_refresh.RefreshOutcome.FAILED


def test_refresh_returns_failed_when_spawn_raises_file_not_found(
    tmp_path, monkeypatch,
):
    """``_resolve_codex_bin`` returned a path, but the binary
    disappeared between resolution and spawn — race with the
    operator uninstalling codex mid-refresh."""
    _write_codex_auth(tmp_path, expires_in_seconds=60)
    b = CodexFileBackend(host_home=tmp_path)

    async def fake_exec(*argv, **kwargs):
        raise FileNotFoundError(2, "no such file", argv[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        credential_refresh, "_resolve_codex_bin", lambda: "codex",
    )

    outcome = asyncio.run(b.refresh())
    assert outcome == credential_refresh.RefreshOutcome.FAILED


def test_refresh_returns_failed_on_nonzero_exit(tmp_path, monkeypatch):
    _write_codex_auth(tmp_path, expires_in_seconds=60)
    b = CodexFileBackend(host_home=tmp_path)

    async def fake_exec(*argv, **kwargs):
        class _Proc:
            returncode = 1
            async def communicate(self):
                return b"", b"codex error: refresh_token revoked"
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        credential_refresh, "_resolve_codex_bin", lambda: "codex",
    )

    outcome = asyncio.run(b.refresh())
    assert outcome == credential_refresh.RefreshOutcome.FAILED


# ── Refresher wiring against CodexFileBackend ────────────────────


def test_refresher_with_codex_backend_ticks_through(tmp_path, monkeypatch):
    """End-to-end: a CredentialRefresher with CodexFileBackend ticks
    once, sees fresh token, runs sync_to_agent for registered codex
    agents (only)."""
    _write_codex_auth(tmp_path, expires_in_seconds=3600)
    r = CredentialRefresher(backend=CodexFileBackend(host_home=tmp_path))

    # One codex agent, one claude-only agent.
    codex_agent = tmp_path / "agent_codex"
    (codex_agent / ".codex").mkdir(parents=True)
    claude_agent = tmp_path / "agent_claude"
    claude_agent.mkdir()
    r.register_agent(codex_agent)
    r.register_agent(claude_agent)

    synced: list[Path] = []
    monkeypatch.setattr(
        credential_refresh, "link_host_codex_auth",
        lambda host, agent_codex: synced.append(Path(agent_codex)) or "symlink",
    )
    # Make sure NO refresh subprocess fires when fresh.
    async def fake_exec(*a, **k):
        raise AssertionError("should not spawn when fresh")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(r._tick())
    # Only the codex agent's .codex/ got synced; claude-only skipped.
    assert synced == [codex_agent / ".codex"]


def test_refresher_with_codex_backend_refreshes_when_close_to_expiry(
    tmp_path, monkeypatch,
):
    _write_codex_auth(tmp_path, expires_in_seconds=5 * 60)  # < 10min margin
    r = CredentialRefresher(backend=CodexFileBackend(host_home=tmp_path))

    spawned: list = []
    async def fake_exec(*argv, **kwargs):
        spawned.append(argv)
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        credential_refresh, "_resolve_codex_bin", lambda: "codex",
    )
    monkeypatch.setattr(
        credential_refresh, "link_host_codex_auth",
        lambda *a, **k: "symlink",
    )

    asyncio.run(r._tick())
    assert len(spawned) == 1
    assert spawned[0][0] == "codex"
    assert "exec" in spawned[0]


def test_refresher_with_codex_backend_refreshes_when_agent_reports_401(
    tmp_path, monkeypatch,
):
    _write_codex_auth(tmp_path, expires_in_seconds=3600)  # plenty of time
    r = CredentialRefresher(backend=CodexFileBackend(host_home=tmp_path))

    spawned: list = []
    async def fake_exec(*argv, **kwargs):
        spawned.append(argv)
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        credential_refresh, "_resolve_codex_bin", lambda: "codex",
    )

    asyncio.run(r._tick(triggered_by_agent=True))
    assert len(spawned) == 1


# ── write_codex_mcp_config pins auth store regardless of MCP env ──


def test_codex_config_pins_auth_store_to_file(tmp_path):
    """The CodexFileBackend symlink/refresh model only works if each
    per-agent codex respects file-mode auth — even when no MCP is
    configured."""
    from puffo_agent.mcp.config import write_codex_mcp_config

    dest = tmp_path / "config.toml"
    write_codex_mcp_config(dest)
    body = dest.read_text(encoding="utf-8")
    assert 'cli_auth_credentials_store = "file"' in body
    # No MCP section when nothing was provided.
    assert "[mcp_servers." not in body


def test_codex_config_pins_auth_store_with_mcp_env(tmp_path):
    from puffo_agent.mcp.config import write_codex_mcp_config

    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        command="/usr/bin/python3",
        args=["-m", "x"],
        env={"K": "V"},
    )
    body = dest.read_text(encoding="utf-8")
    assert 'cli_auth_credentials_store = "file"' in body
    assert "[mcp_servers.puffo]" in body


# ── Daemon refresher routing by harness ─────────────────────────


def test_daemon_routes_codex_agent_to_codex_refresher():
    """Codex-harness agents register with the codex refresher only;
    claude-code agents with the main refresher only. Verifies the
    daemon._refresher_for dispatch."""
    from puffo_agent.portal.daemon import Daemon
    from puffo_agent.portal.state import (
        AgentConfig, DaemonConfig, PuffoCoreConfig, RuntimeConfig,
    )

    daemon_cfg = DaemonConfig()
    d = Daemon(daemon_cfg)

    codex_agent = AgentConfig(
        id="agent-codex",
        runtime=RuntimeConfig(kind="cli-local", harness="codex"),
        puffo_core=PuffoCoreConfig(),
    )
    claude_agent = AgentConfig(
        id="agent-claude",
        runtime=RuntimeConfig(kind="cli-local", harness="claude-code"),
        puffo_core=PuffoCoreConfig(),
    )

    assert d._refresher_for(codex_agent) is d.codex_refresher
    assert d._refresher_for(claude_agent) is d.refresher
    # Default harness (empty / missing) falls back to claude.
    default_agent = AgentConfig(
        id="agent-default",
        runtime=RuntimeConfig(kind="cli-local", harness=""),
        puffo_core=PuffoCoreConfig(),
    )
    assert d._refresher_for(default_agent) is d.refresher


def test_daemon_stop_worker_unregisters_from_both_refreshers(tmp_path, monkeypatch):
    """A worker stopping should drop the agent from BOTH refresher
    sets — we don't track which refresher owns which agent past
    registration, so both unregisters must be idempotent and safe."""
    from puffo_agent.portal.daemon import Daemon
    from puffo_agent.portal.state import DaemonConfig

    daemon_cfg = DaemonConfig()
    d = Daemon(daemon_cfg)

    # Manually register in just the codex refresher.
    agent_home = tmp_path / "agent-x"
    d.codex_refresher.register_agent(agent_home)
    assert agent_home in d.codex_refresher._agent_homes

    # Simulate _stop_worker's unregister-both pattern.
    d.refresher.unregister_agent(agent_home)  # no-op (not registered)
    d.codex_refresher.unregister_agent(agent_home)  # actually removes
    assert agent_home not in d.codex_refresher._agent_homes
