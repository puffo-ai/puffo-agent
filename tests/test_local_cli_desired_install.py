"""PUF-268 PR-B step 2: spawn-time install of desired_skills +
desired_mcps from puffo-server catalog templates.

Unit-level coverage of the ``desired_install`` module + the
LocalCLIAdapter wiring that drives it. HTTP fetches are mocked
via a fake PuffoCoreHttpClient — no real network.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters import desired_install
from puffo_agent.agent.adapters.desired_install import (
    DESIRED_INSTALLED_MARKER,
    install_claude_mcp,
    install_desired,
    normalize_mcp_spec,
    write_desired_skill,
)
from puffo_agent.crypto.http_client import HttpError


# ── fake http client ────────────────────────────────────────────────────────


class FakeHttp:
    """Stand-in for ``PuffoCoreHttpClient`` over only ``get`` + ``close``.

    ``responses`` maps ``"/v2/<kind>-templates/<id>"`` → either the
    response dict or an ``HttpError`` instance to raise.
    """
    def __init__(self, responses: dict[str, Any] | None = None):
        self.responses = responses or {}
        self.calls: list[str] = []
        self.closed = False

    async def get(self, path: str) -> Any:
        self.calls.append(path)
        if path not in self.responses:
            raise HttpError(404, "not found")
        v = self.responses[path]
        if isinstance(v, Exception):
            raise v
        return v

    async def close(self) -> None:
        self.closed = True


# ── normalize_mcp_spec ──────────────────────────────────────────────────────


def test_normalize_stdio_spec_keeps_command_args_env():
    spec = normalize_mcp_spec({
        "id": "fs",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        "env": {"ROOT": "/tmp"},
    })
    assert spec == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        "env": {"ROOT": "/tmp"},
    }


def test_normalize_sse_spec_has_url_no_command():
    spec = normalize_mcp_spec({
        "id": "fetch",
        "type": "sse",
        "url": "https://mcp.example.com/sse",
        "env": {"AUTH": "x"},
    })
    assert spec == {"type": "sse", "url": "https://mcp.example.com/sse", "env": {"AUTH": "x"}}


def test_normalize_http_spec_has_url():
    spec = normalize_mcp_spec({
        "id": "github",
        "type": "http",
        "url": "https://api.github.com/mcp",
    })
    assert spec == {"type": "http", "url": "https://api.github.com/mcp", "env": {}}


def test_normalize_stdio_missing_command_rejected():
    assert normalize_mcp_spec({"type": "stdio", "args": []}) is None


def test_normalize_sse_missing_url_rejected():
    assert normalize_mcp_spec({"type": "sse"}) is None


def test_normalize_unknown_transport_rejected():
    assert normalize_mcp_spec({"type": "ws", "url": "ws://x"}) is None


def test_normalize_defaults_args_and_env():
    spec = normalize_mcp_spec({"type": "stdio", "command": "x"})
    assert spec == {"type": "stdio", "command": "x", "args": [], "env": {}}


# ── write_desired_skill ─────────────────────────────────────────────────────


def test_write_desired_skill_writes_body_verbatim(tmp_path):
    body = "---\nname: Git PR flow\n---\n\n# body\n"
    result = write_desired_skill(tmp_path, "git-pr-flow", body)
    assert result == "installed"
    skill_md = tmp_path / ".claude" / "skills" / "git-pr-flow" / "SKILL.md"
    assert skill_md.read_text(encoding="utf-8") == body
    assert (skill_md.parent / DESIRED_INSTALLED_MARKER).exists()


def test_write_desired_skill_idempotent_when_already_present(tmp_path):
    dst = tmp_path / ".claude" / "skills" / "git-pr-flow"
    dst.mkdir(parents=True)
    (dst / "SKILL.md").write_text("OLD BODY", encoding="utf-8")
    result = write_desired_skill(tmp_path, "git-pr-flow", "NEW BODY")
    assert result == "already-present"
    assert (dst / "SKILL.md").read_text(encoding="utf-8") == "OLD BODY"


def test_write_desired_skill_rejects_invalid_id(tmp_path):
    assert write_desired_skill(tmp_path, "../etc/passwd", "x") == "invalid"
    assert write_desired_skill(tmp_path, "WithCaps", "x") == "invalid"
    assert write_desired_skill(tmp_path, "", "x") == "invalid"


def test_write_desired_skill_rejects_empty_body(tmp_path):
    assert write_desired_skill(tmp_path, "ok-id", "   ") == "invalid"


# ── install_claude_mcp ──────────────────────────────────────────────────────


def test_install_claude_mcp_writes_to_per_agent_claude_json(tmp_path):
    spec = {"type": "stdio", "command": "npx", "args": ["-y", "x"], "env": {}}
    result = install_claude_mcp(tmp_path, "fs", spec)
    assert result == "installed"
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["fs"] == spec


def test_install_claude_mcp_idempotent_leaves_existing_untouched(tmp_path):
    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"mcpServers": {"fs": {"command": "EXISTING"}}}), encoding="utf-8")
    spec = {"type": "stdio", "command": "DIFFERENT", "args": [], "env": {}}
    result = install_claude_mcp(tmp_path, "fs", spec)
    assert result == "already-present"
    data = json.loads(cj.read_text(encoding="utf-8"))
    assert data["mcpServers"]["fs"]["command"] == "EXISTING"


def test_install_claude_mcp_sse_writes_url_not_command(tmp_path):
    spec = {"type": "sse", "url": "https://x", "env": {}}
    install_claude_mcp(tmp_path, "remote", spec)
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["remote"] == spec
    assert "command" not in data["mcpServers"]["remote"]


def test_install_claude_mcp_preserves_existing_unrelated_keys(tmp_path):
    cj = tmp_path / ".claude.json"
    cj.write_text(
        json.dumps({"userID": "alice", "mcpServers": {"a": {"command": "A"}}}),
        encoding="utf-8",
    )
    install_claude_mcp(tmp_path, "b", {"type": "stdio", "command": "B", "args": [], "env": {}})
    data = json.loads(cj.read_text(encoding="utf-8"))
    assert data["userID"] == "alice"
    assert set(data["mcpServers"].keys()) == {"a", "b"}


def test_install_claude_mcp_rejects_invalid_id(tmp_path):
    assert install_claude_mcp(tmp_path, "../bad", {"type": "stdio", "command": "x"}) == "invalid"


# ── install_desired (orchestration) ─────────────────────────────────────────


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_install_desired_skill_happy_path(tmp_path):
    http = FakeHttp({
        "/v2/skill-templates/git-pr-flow": {
            "id": "git-pr-flow",
            "body": "---\nname: Git PR flow\n---\n\nhello\n",
        },
    })
    extras = _run(install_desired(
        http=http,
        agent_home=tmp_path,
        agent_id="a1",
        harness_name="claude-code",
        desired_skills=["git-pr-flow"],
        desired_mcps=[],
    ))
    assert extras == {}
    assert (tmp_path / ".claude" / "skills" / "git-pr-flow" / "SKILL.md").exists()


def test_install_desired_mcp_stdio_claude_path(tmp_path):
    http = FakeHttp({
        "/v2/mcp-templates/fs": {
            "id": "fs", "type": "stdio", "command": "npx",
            "args": ["-y", "@x/server-filesystem"], "env": {"R": "1"},
        },
    })
    _run(install_desired(
        http=http, agent_home=tmp_path, agent_id="a1",
        harness_name="claude-code", desired_skills=[], desired_mcps=["fs"],
    ))
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["fs"]["type"] == "stdio"
    assert data["mcpServers"]["fs"]["command"] == "npx"


def test_install_desired_mcp_stdio_codex_path_returns_extras_no_disk_write(tmp_path):
    http = FakeHttp({
        "/v2/mcp-templates/fs": {
            "id": "fs", "type": "stdio", "command": "npx", "args": ["-y"],
        },
    })
    extras = _run(install_desired(
        http=http, agent_home=tmp_path, agent_id="a1",
        harness_name="codex", desired_skills=[], desired_mcps=["fs"],
    ))
    assert extras == {"fs": {"command": "npx", "args": ["-y"], "env": {}}}
    # codex never touches .claude.json:
    assert not (tmp_path / ".claude.json").exists()


def test_install_desired_mcp_sse_claude_writes_url(tmp_path):
    http = FakeHttp({
        "/v2/mcp-templates/remote": {
            "id": "remote", "type": "sse", "url": "https://example.com/sse",
        },
    })
    _run(install_desired(
        http=http, agent_home=tmp_path, agent_id="a1",
        harness_name="claude-code", desired_skills=[], desired_mcps=["remote"],
    ))
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["remote"]["type"] == "sse"
    assert data["mcpServers"]["remote"]["url"] == "https://example.com/sse"


def test_install_desired_mcp_http_claude_writes_url(tmp_path):
    http = FakeHttp({
        "/v2/mcp-templates/github": {
            "id": "github", "type": "http", "url": "https://api.github.com/mcp",
        },
    })
    _run(install_desired(
        http=http, agent_home=tmp_path, agent_id="a1",
        harness_name="claude-code", desired_skills=[], desired_mcps=["github"],
    ))
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["github"]["type"] == "http"
    assert data["mcpServers"]["github"]["url"] == "https://api.github.com/mcp"


def test_install_desired_mcp_sse_codex_skipped_with_warning(tmp_path, caplog):
    http = FakeHttp({
        "/v2/mcp-templates/remote": {
            "id": "remote", "type": "sse", "url": "https://example.com/sse",
        },
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        extras = _run(install_desired(
            http=http, agent_home=tmp_path, agent_id="a1",
            harness_name="codex", desired_skills=[], desired_mcps=["remote"],
        ))
    assert extras == {}
    assert any("stdio-only" in r.message for r in caplog.records)


def test_install_desired_mcp_http_codex_skipped_with_warning(tmp_path, caplog):
    http = FakeHttp({
        "/v2/mcp-templates/github": {
            "id": "github", "type": "http", "url": "https://api.github.com/mcp",
        },
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        extras = _run(install_desired(
            http=http, agent_home=tmp_path, agent_id="a1",
            harness_name="codex", desired_skills=[], desired_mcps=["github"],
        ))
    assert extras == {}
    assert any("stdio-only" in r.message for r in caplog.records)


def test_install_desired_404_logs_warning_and_continues(tmp_path, caplog):
    http = FakeHttp({
        "/v2/mcp-templates/exists": {
            "id": "exists", "type": "stdio", "command": "npx",
        },
        # ``missing`` not registered → FakeHttp raises 404.
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        _run(install_desired(
            http=http, agent_home=tmp_path, agent_id="a1",
            harness_name="claude-code",
            desired_skills=[],
            desired_mcps=["missing", "exists"],
        ))
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert "exists" in data["mcpServers"]
    assert "missing" not in data["mcpServers"]
    assert any("missing" in r.message and "404" in r.message for r in caplog.records)


def test_install_desired_404_on_skill_logs_and_continues(tmp_path, caplog):
    http = FakeHttp({
        "/v2/skill-templates/ok": {"id": "ok", "body": "---\nname: ok\n---\n\nbody\n"},
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        _run(install_desired(
            http=http, agent_home=tmp_path, agent_id="a1",
            harness_name="claude-code",
            desired_skills=["missing", "ok"], desired_mcps=[],
        ))
    assert (tmp_path / ".claude" / "skills" / "ok" / "SKILL.md").exists()
    assert not (tmp_path / ".claude" / "skills" / "missing").exists()


def test_install_desired_codex_skills_skipped_with_one_warning(tmp_path, caplog):
    http = FakeHttp({
        # would resolve if asked, but for codex we never even fetch.
        "/v2/skill-templates/s1": {"id": "s1", "body": "body"},
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        extras = _run(install_desired(
            http=http, agent_home=tmp_path, agent_id="a1",
            harness_name="codex",
            desired_skills=["s1", "s2"], desired_mcps=[],
        ))
    assert extras == {}
    skips = [r for r in caplog.records if "no skills surface" in r.message]
    assert len(skips) == 1
    # Crucially, NO HTTP call was made for the skills (codex shortcuts):
    assert all("skill-templates" not in p for p in http.calls)


def test_install_desired_dedupes_existing_skill_dir(tmp_path):
    skill_dir = tmp_path / ".claude" / "skills" / "git-pr-flow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("PRE-EXISTING", encoding="utf-8")
    http = FakeHttp({
        "/v2/skill-templates/git-pr-flow": {
            "id": "git-pr-flow", "body": "FRESH FROM CATALOG",
        },
    })
    _run(install_desired(
        http=http, agent_home=tmp_path, agent_id="a1",
        harness_name="claude-code",
        desired_skills=["git-pr-flow"], desired_mcps=[],
    ))
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "PRE-EXISTING"


def test_install_desired_dedupes_existing_mcp_entry(tmp_path):
    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"mcpServers": {"fs": {"command": "HOST"}}}), encoding="utf-8")
    http = FakeHttp({
        "/v2/mcp-templates/fs": {
            "id": "fs", "type": "stdio", "command": "CATALOG",
        },
    })
    _run(install_desired(
        http=http, agent_home=tmp_path, agent_id="a1",
        harness_name="claude-code",
        desired_skills=[], desired_mcps=["fs"],
    ))
    data = json.loads(cj.read_text(encoding="utf-8"))
    assert data["mcpServers"]["fs"]["command"] == "HOST"


def test_install_desired_skill_with_invalid_body_skipped(tmp_path, caplog):
    http = FakeHttp({
        "/v2/skill-templates/bad-body": {"id": "bad-body"},  # no body field
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        _run(install_desired(
            http=http, agent_home=tmp_path, agent_id="a1",
            harness_name="claude-code",
            desired_skills=["bad-body"], desired_mcps=[],
        ))
    assert not (tmp_path / ".claude" / "skills" / "bad-body").exists()
    assert any("no body" in r.message for r in caplog.records)


def test_install_desired_unsupported_transport_skipped(tmp_path, caplog):
    http = FakeHttp({
        "/v2/mcp-templates/weird": {
            "id": "weird", "type": "websocket", "url": "ws://x",
        },
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        _run(install_desired(
            http=http, agent_home=tmp_path, agent_id="a1",
            harness_name="claude-code",
            desired_skills=[], desired_mcps=["weird"],
        ))
    assert not (tmp_path / ".claude.json").exists()
    assert any("unsupported transport" in r.message for r in caplog.records)


def test_install_desired_non_404_http_error_logs_and_continues(tmp_path, caplog):
    http = FakeHttp({
        "/v2/mcp-templates/broken": HttpError(500, "boom"),
        "/v2/mcp-templates/ok": {"id": "ok", "type": "stdio", "command": "x"},
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        _run(install_desired(
            http=http, agent_home=tmp_path, agent_id="a1",
            harness_name="claude-code",
            desired_skills=[], desired_mcps=["broken", "ok"],
        ))
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert "ok" in data["mcpServers"]
    assert "broken" not in data["mcpServers"]
    assert any("HTTP 500" in r.message for r in caplog.records)


# ── LocalCLIAdapter wiring ──────────────────────────────────────────────────


def _make_local_adapter(tmp_path, monkeypatch, *, harness_name="claude-code",
                       desired_skills=None, desired_mcps=None,
                       with_puffo_core=True):
    """Build a LocalCLIAdapter against tmp_path with claude binary
    mocked so _verify() succeeds. Returns (adapter, agent_home)."""
    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("USERPROFILE", str(host))
    from puffo_agent.agent.adapters import local_cli
    monkeypatch.setattr(local_cli.shutil, "which", lambda _: "/fake/claude")
    monkeypatch.setattr(
        local_cli, "resolve_claude_bin", lambda: "/fake/claude",
    )
    from puffo_agent.agent.harness import build_harness
    harness = build_harness(harness_name)
    agent_home = tmp_path / "agent_home"
    adapter = local_cli.LocalCLIAdapter(
        agent_id="t-agent",
        model="",
        workspace_dir=str(tmp_path / "ws"),
        claude_dir=str(tmp_path / "ws" / ".claude"),
        session_file=str(tmp_path / "sess.json"),
        mcp_config_file=str(tmp_path / "mcp.json"),
        agent_home_dir=str(agent_home),
        harness=harness,
        desired_skills=desired_skills or [],
        desired_mcps=desired_mcps or [],
        puffo_core_server_url="https://chat.test.invalid" if with_puffo_core else "",
        puffo_core_slug="alice-0001" if with_puffo_core else "",
        puffo_core_keys_dir=str(tmp_path / "keys") if with_puffo_core else "",
    )
    return adapter, agent_home


def _patch_http(monkeypatch, fake: FakeHttp):
    """Force ``PuffoCoreHttpClient(server_url, ks, slug)`` to return ``fake``."""
    from puffo_agent.agent.adapters import local_cli as lc

    class _Stub:
        def __init__(self, *a, **kw):
            self._fake = fake
        async def get(self, path):
            return await fake.get(path)
        async def close(self):
            await fake.close()

    import puffo_agent.crypto.http_client as hc
    monkeypatch.setattr(hc, "PuffoCoreHttpClient", _Stub)
    # Also stub KeyStore to a no-op so we don't need a real identity file.
    class _KS:
        def __init__(self, *a, **kw):
            pass
    monkeypatch.setattr(hc, "KeyStore", _KS, raising=False)
    import puffo_agent.crypto.keystore as ks_mod
    monkeypatch.setattr(ks_mod, "KeyStore", _KS)


def test_adapter_install_desired_runs_once(tmp_path, monkeypatch):
    fake = FakeHttp({
        "/v2/skill-templates/s1": {"id": "s1", "body": "BODY"},
    })
    _patch_http(monkeypatch, fake)
    adapter, agent_home = _make_local_adapter(
        tmp_path, monkeypatch, desired_skills=["s1"],
    )
    asyncio.new_event_loop().run_until_complete(adapter._install_desired())
    assert (agent_home / ".claude" / "skills" / "s1" / "SKILL.md").exists()
    # Second invocation is a no-op — verified by clearing responses
    # and confirming no error / no extra fetch.
    fake.calls.clear()
    asyncio.new_event_loop().run_until_complete(adapter._install_desired())
    assert fake.calls == []


def test_adapter_install_desired_no_puffo_core_skips_with_warning(tmp_path, monkeypatch, caplog):
    fake = FakeHttp({})
    _patch_http(monkeypatch, fake)
    adapter, _ = _make_local_adapter(
        tmp_path, monkeypatch, desired_skills=["s1"], with_puffo_core=False,
    )
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.local_cli"):
        asyncio.new_event_loop().run_until_complete(adapter._install_desired())
    assert fake.calls == []
    assert any("puffo_core wiring is incomplete" in r.message for r in caplog.records)


def test_adapter_install_desired_empty_lists_no_http(tmp_path, monkeypatch):
    fake = FakeHttp({})
    _patch_http(monkeypatch, fake)
    adapter, _ = _make_local_adapter(tmp_path, monkeypatch)
    asyncio.new_event_loop().run_until_complete(adapter._install_desired())
    assert fake.calls == []


def test_adapter_codex_extras_fold_into_config_toml(tmp_path, monkeypatch):
    fake = FakeHttp({
        "/v2/mcp-templates/fs": {
            "id": "fs", "type": "stdio", "command": "npx",
            "args": ["-y", "@x/fs"], "env": {"R": "/tmp"},
        },
    })
    _patch_http(monkeypatch, fake)
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))

    adapter, _ = _make_local_adapter(
        tmp_path, monkeypatch, harness_name="codex", desired_mcps=["fs"],
    )
    asyncio.new_event_loop().run_until_complete(adapter._install_desired())
    assert adapter._desired_codex_extras == {
        "fs": {"command": "npx", "args": ["-y", "@x/fs"], "env": {"R": "/tmp"}},
    }
    # Drive _ensure_codex_session to spawn-fail (no codex bin) but
    # verify config.toml is still written with the desired MCP.
    with pytest.raises(RuntimeError):
        adapter._ensure_codex_session()
    codex_home = Path(os.environ["PUFFO_AGENT_HOME"]) / "agents" / adapter.agent_id / ".codex"
    doc = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert "fs" in (doc.get("mcp_servers") or {})
    assert doc["mcp_servers"]["fs"]["command"] == "npx"
    assert doc["mcp_servers"]["fs"]["env"]["R"] == "/tmp"


def test_adapter_codex_host_mcp_shadows_desired_on_collision(tmp_path, monkeypatch):
    """Operator's local host config.toml beats catalog default.
    Mirrors claude's host-sync semantics where host wins."""
    fake = FakeHttp({
        "/v2/mcp-templates/fs": {
            "id": "fs", "type": "stdio", "command": "CATALOG",
        },
    })
    _patch_http(monkeypatch, fake)
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    # Seed host codex config with same name "fs".
    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    (host / ".codex").mkdir(parents=True, exist_ok=True)
    (host / ".codex" / "config.toml").write_text(
        '[mcp_servers.fs]\ncommand = "HOST-WINS"\nargs = []\n',
        encoding="utf-8",
    )

    adapter, _ = _make_local_adapter(
        tmp_path, monkeypatch, harness_name="codex", desired_mcps=["fs"],
    )
    asyncio.new_event_loop().run_until_complete(adapter._install_desired())
    with pytest.raises(RuntimeError):
        adapter._ensure_codex_session()
    codex_home = Path(os.environ["PUFFO_AGENT_HOME"]) / "agents" / adapter.agent_id / ".codex"
    doc = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert doc["mcp_servers"]["fs"]["command"] == "HOST-WINS"


def test_adapter_install_desired_crash_does_not_block(tmp_path, monkeypatch, caplog):
    class CrashingHttp:
        async def get(self, path):  # pragma: no cover - exercised below
            raise RuntimeError("network down")
        async def close(self):
            pass

    import puffo_agent.crypto.http_client as hc
    monkeypatch.setattr(hc, "PuffoCoreHttpClient", lambda *a, **kw: CrashingHttp())
    import puffo_agent.crypto.keystore as ks_mod
    monkeypatch.setattr(ks_mod, "KeyStore", lambda *a, **kw: object())

    adapter, _ = _make_local_adapter(
        tmp_path, monkeypatch, desired_skills=["s1"],
    )
    # Per-template catch-all logs warning + continues; the outer
    # try/except defends against any orchestrator-level crash too.
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        asyncio.new_event_loop().run_until_complete(adapter._install_desired())
    assert adapter._desired_installed is True
