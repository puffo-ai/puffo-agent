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
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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


# Operator review #58 §8: malformed ``.claude.json`` must NOT silently
# reset (which would clobber userID / history / etc). Bail with a
# warning so the user-authored file is preserved.
def test_install_claude_mcp_malformed_existing_file_skips_without_reset(tmp_path, caplog):
    cj = tmp_path / ".claude.json"
    cj.write_text("{ this is not valid json", encoding="utf-8")
    spec = {"type": "stdio", "command": "B", "args": [], "env": {}}
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        result = install_claude_mcp(tmp_path, "fs", spec)
    assert result == "skipped"
    # File content untouched — the broken bytes are still there.
    assert cj.read_text(encoding="utf-8") == "{ this is not valid json"
    assert any("cannot read .claude.json" in r.getMessage() for r in caplog.records)


def test_install_claude_mcp_non_object_root_skips(tmp_path):
    """``.claude.json`` is a JSON array (somehow). We can't merge into
    it, so bail rather than reset to ``{}`` and clobber whatever the
    user actually wanted."""
    cj = tmp_path / ".claude.json"
    cj.write_text("[1, 2, 3]", encoding="utf-8")
    spec = {"type": "stdio", "command": "B", "args": [], "env": {}}
    result = install_claude_mcp(tmp_path, "fs", spec)
    assert result == "skipped"
    assert cj.read_text(encoding="utf-8") == "[1, 2, 3]"


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
        workspace_dir=tmp_path,
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
        http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
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
        http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
        harness_name="codex", desired_skills=[], desired_mcps=["fs"],
    ))
    assert extras == {"fs": {"command": "npx", "args": ["-y"], "env": {}}}
    # codex never touches .claude.json:
    assert not (tmp_path / ".claude.json").exists()


@pytest.mark.parametrize("harness_name", ["claude-code", "codex"])
def test_containerized_desired_mcp_skips_host_local_command(
    tmp_path, caplog, harness_name,
):
    http = FakeHttp({
        "/v2/mcp-templates/host-only": {
            "id": "host-only",
            "type": "stdio",
            "command": "/Users/operator/bin/mcp-server",
        },
    })
    with caplog.at_level(
        logging.WARNING,
        logger="puffo_agent.agent.adapters.desired_install",
    ):
        extras = _run(install_desired(
            http=http,
            agent_home=tmp_path,
            workspace_dir=tmp_path,
            agent_id="a1",
            harness_name=harness_name,
            desired_skills=[],
            desired_mcps=["host-only"],
            containerized=True,
        ))

    assert extras == {}
    assert not (tmp_path / ".claude.json").exists()
    assert any(
        "cannot resolve inside the container" in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("harness_name", ["claude-code", "codex"])
def test_containerized_desired_mcp_keeps_portable_command(tmp_path, harness_name):
    http = FakeHttp({
        "/v2/mcp-templates/portable": {
            "id": "portable",
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "portable-server"],
        },
    })
    extras = _run(install_desired(
        http=http,
        agent_home=tmp_path,
        workspace_dir=tmp_path,
        agent_id="a1",
        harness_name=harness_name,
        desired_skills=[],
        desired_mcps=["portable"],
        containerized=True,
    ))

    if harness_name == "codex":
        assert extras["portable"]["command"] == "npx"
    else:
        data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
        assert data["mcpServers"]["portable"]["command"] == "npx"


def test_install_desired_mcp_sse_claude_writes_url(tmp_path):
    http = FakeHttp({
        "/v2/mcp-templates/remote": {
            "id": "remote", "type": "sse", "url": "https://example.com/sse",
        },
    })
    _run(install_desired(
        http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
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
        http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
        harness_name="claude-code", desired_skills=[], desired_mcps=["github"],
    ))
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["github"]["type"] == "http"
    assert data["mcpServers"]["github"]["url"] == "https://api.github.com/mcp"


def test_install_desired_mcp_sse_codex_returns_extras(tmp_path):
    http = FakeHttp({
        "/v2/mcp-templates/remote": {
            "id": "remote", "type": "sse", "url": "https://example.com/sse",
        },
    })
    extras = _run(install_desired(
        http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
        harness_name="codex", desired_skills=[], desired_mcps=["remote"],
    ))
    assert extras == {"remote": {"url": "https://example.com/sse", "env": {}}}
    assert not (tmp_path / ".claude.json").exists()


def test_install_desired_mcp_http_codex_returns_extras(tmp_path):
    http = FakeHttp({
        "/v2/mcp-templates/github": {
            "id": "github", "type": "http", "url": "https://api.github.com/mcp",
        },
    })
    extras = _run(install_desired(
        http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
        harness_name="codex", desired_skills=[], desired_mcps=["github"],
    ))
    assert extras == {"github": {"url": "https://api.github.com/mcp", "env": {}}}
    assert not (tmp_path / ".claude.json").exists()


def test_install_desired_404_logs_warning_and_continues(tmp_path, caplog):
    http = FakeHttp({
        "/v2/mcp-templates/exists": {
            "id": "exists", "type": "stdio", "command": "npx",
        },
        # ``missing`` not registered → FakeHttp raises 404.
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        _run(install_desired(
            http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
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
            http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
            harness_name="claude-code",
            desired_skills=["missing", "ok"], desired_mcps=[],
        ))
    assert (tmp_path / ".claude" / "skills" / "ok" / "SKILL.md").exists()
    assert not (tmp_path / ".claude" / "skills" / "missing").exists()


def test_install_desired_codex_skills_install_to_agents_dir(tmp_path):
    http = FakeHttp({
        "/v2/skill-templates/s1": {
            "id": "s1",
            "body": "---\nname: S1\n---\n\ncall `mcp__puffo__send_message`\n",
        },
    })
    extras = _run(install_desired(
        http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
        harness_name="codex",
        desired_skills=["s1"], desired_mcps=[],
    ))
    assert extras == {}
    skill_md = tmp_path / ".agents" / "skills" / "s1" / "SKILL.md"
    assert skill_md.exists()
    # codex's tool router uses bare names — prefix stripped on disk.
    assert "mcp__puffo__" not in skill_md.read_text(encoding="utf-8")
    assert "send_message" in skill_md.read_text(encoding="utf-8")
    # claude-code path NOT written.
    assert not (tmp_path / ".claude" / "skills" / "s1").exists()


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
        http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
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
        http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
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
            http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
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
            http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
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
            http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
            harness_name="claude-code",
            desired_skills=[], desired_mcps=["broken", "ok"],
        ))
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert "ok" in data["mcpServers"]
    assert "broken" not in data["mcpServers"]
    assert any("HTTP 500" in r.message for r in caplog.records)


# Operator review #58 §8: pin the three-transport happy path so a
# future ``normalize_mcp_spec`` refactor can't accidentally lose
# ordering or per-transport handling.
def test_install_desired_mixed_stdio_sse_http_all_install_under_claude(tmp_path):
    http = FakeHttp({
        "/v2/mcp-templates/fs": {
            "id": "fs", "type": "stdio", "command": "fs-mcp",
            "args": [], "env": {},
        },
        "/v2/mcp-templates/remote-sse": {
            "id": "remote-sse", "type": "sse",
            "url": "https://relay.example/sse",
        },
        "/v2/mcp-templates/remote-http": {
            "id": "remote-http", "type": "http",
            "url": "https://relay.example/http",
        },
    })
    _run(install_desired(
        http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
        harness_name="claude-code",
        desired_skills=[],
        desired_mcps=["fs", "remote-sse", "remote-http"],
    ))
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    servers = data["mcpServers"]
    assert set(servers.keys()) == {"fs", "remote-sse", "remote-http"}
    assert servers["fs"]["type"] == "stdio" and servers["fs"]["command"] == "fs-mcp"
    assert servers["remote-sse"]["type"] == "sse" and servers["remote-sse"]["url"] == "https://relay.example/sse"
    assert servers["remote-http"]["type"] == "http" and servers["remote-http"]["url"] == "https://relay.example/http"
    assert "command" not in servers["remote-sse"]
    assert "command" not in servers["remote-http"]


# Operator review #58 §8: skill + MCP fetch failures coexist and
# DON'T tangle (independent logging + independent continuation).
def test_install_desired_skill_404_and_mcp_500_both_log_and_keep_going(tmp_path, caplog):
    http = FakeHttp({
        "/v2/skill-templates/ghost": HttpError(404, "nope"),
        "/v2/skill-templates/git-pr-flow": {
            "id": "git-pr-flow",
            "body": "---\nname: Git PR\n---\n\nbody\n",
        },
        "/v2/mcp-templates/broken": HttpError(500, "boom"),
        "/v2/mcp-templates/fs": {
            "id": "fs", "type": "stdio", "command": "fs-mcp",
        },
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.desired_install"):
        _run(install_desired(
            http=http, agent_home=tmp_path, workspace_dir=tmp_path, agent_id="a1",
            harness_name="claude-code",
            desired_skills=["ghost", "git-pr-flow"],
            desired_mcps=["broken", "fs"],
        ))
    # Healthy artifacts landed.
    assert (tmp_path / ".claude" / "skills" / "git-pr-flow" / "SKILL.md").exists()
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert "fs" in data["mcpServers"]
    # Failed artifacts didn't.
    assert not (tmp_path / ".claude" / "skills" / "ghost").exists()
    assert "broken" not in data["mcpServers"]
    # Both failures logged independently — the skill 404 mentions
    # "skill" and the mcp 500 mentions "mcp" / "HTTP 500".
    messages = [r.getMessage() for r in caplog.records]
    assert any("skill 'ghost'" in m and "404" in m for m in messages)
    assert any("mcp 'broken'" in m and "HTTP 500" in m for m in messages)


# ── Driver runtime wiring ───────────────────────────────────────────────────


def test_local_runtime_installs_once_and_retains_codex_extras(
    tmp_path, monkeypatch,
):
    import puffo_agent.agent.harness.local_runtime as local_runtime
    from puffo_agent.agent.harness.local_runtime import LocalRuntimePreparer
    from puffo_agent.portal.state import AgentConfig, DaemonConfig, RuntimeConfig

    calls: list[dict[str, Any]] = []

    async def _fake_install(**kwargs):
        calls.append(kwargs)
        return {"fs": {"command": "npx", "args": [], "env": {}}}

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    monkeypatch.setattr(local_runtime, "run_spawn_install", _fake_install)
    cfg = AgentConfig(
        id="codex-assets",
        runtime=RuntimeConfig(
            kind="cli-local", provider="openai", harness="codex",
        ),
        desired_skills=["s1"],
        desired_mcps=["fs"],
    )
    preparer = LocalRuntimePreparer(DaemonConfig(), cfg)

    _run(preparer._install_desired_once())
    _run(preparer._install_desired_once())

    assert len(calls) == 1
    assert calls[0]["desired_skills"] == ["s1"]
    assert calls[0]["desired_mcps"] == ["fs"]
    assert preparer._desired_codex_extras == {
        "fs": {"command": "npx", "args": [], "env": {}},
    }
