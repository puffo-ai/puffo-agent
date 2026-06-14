"""Bridge handler tests: info no-auth, agents list/detail redaction,
file endpoint path safety + caps.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _bridge_support import (
    isolated_home, make_user, pair_request_body, signed_headers,
    write_test_agent,
)
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.portal.api.server import build_app
from puffo_agent.portal.state import DaemonConfig

pytestmark = pytest.mark.asyncio

_HOST = {"Host": "127.0.0.1:63387"}


@pytest_asyncio.fixture
async def client():
    isolated_home()
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


async def _pair(client, user):
    body = pair_request_body(user)
    h = signed_headers(user, "POST", "/v1/pair", body); h.update(_HOST)
    r = await client.post("/v1/pair", data=body, headers=h)
    assert r.status == 200, await r.text()


# ────────────────────────────────────────────────────────────────────
# /v1/info — no auth
# ────────────────────────────────────────────────────────────────────


async def test_info_no_auth(client):
    r = await client.get("/v1/info", headers=_HOST)
    assert r.status == 200
    j = await r.json()
    assert j["service"] == "puffo-agent-bridge"
    assert j["runtime"] == "puffo-agent"
    assert isinstance(j["daemon_version"], str) and j["daemon_version"]
    assert j["paired"] is False


async def test_providers_no_auth(client, monkeypatch):
    from puffo_agent.agent import model_catalog as mc

    fake = {
        "claude-code": [
            mc.ModelOption("", "(daemon default)"),
            mc.ModelOption("opus", "opus", is_alias=True),
            mc.ModelOption("claude-fable-5", "Claude Fable 5"),
        ],
        "codex": [mc.ModelOption("", "(daemon default)"), mc.ModelOption("gpt-5.5", "GPT-5.5")],
    }
    monkeypatch.setattr(
        mc, "provider_models",
        lambda h, *, fetch=False: fake.get(h, [mc.ModelOption("", "(daemon default)")]),
    )
    r = await client.get("/v1/providers", headers=_HOST)
    assert r.status == 200
    by = {p["provider"]: p["models"] for p in (await r.json())["providers"]}
    assert set(by) == set(mc.KNOWN_HARNESSES)  # every known harness reported
    # daemon-default sentinel dropped; alias flag + label carried through
    assert by["claude-code"] == [
        {"id": "opus", "label": "opus", "alias": True},
        {"id": "claude-fable-5", "label": "Claude Fable 5", "alias": False},
    ]
    assert by["codex"] == [{"id": "gpt-5.5", "label": "GPT-5.5", "alias": False}]


async def test_info_carries_cli_tools_status(client, monkeypatch):
    from puffo_agent.portal.api import handlers
    monkeypatch.setattr(
        "puffo_agent.agent.cli_bin.resolve_claude_bin", lambda: "/bin/claude",
    )
    monkeypatch.setattr(
        "puffo_agent.agent.cli_bin.claude_has_credentials", lambda: True,
    )
    monkeypatch.setattr(
        "puffo_agent.agent.cli_bin.resolve_codex_bin", lambda: None,
    )
    monkeypatch.setattr(
        "puffo_agent.agent.cli_bin.codex_has_credentials", lambda: False,
    )
    _ = handlers  # silence unused-import warning for the patch scope
    r = await client.get("/v1/info", headers=_HOST)
    j = await r.json()
    assert j["cli_tools"] == {
        "claude-code": "ready",
        "codex": "not_installed",
    }


async def test_info_reflects_pairing_state(client):
    user = make_user()
    await _pair(client, user)
    r = await client.get("/v1/info", headers=_HOST)
    j = await r.json()
    assert j["paired"] is True
    assert j["paired_slug"] == user.slug


# ────────────────────────────────────────────────────────────────────
# /v1/agents
# ────────────────────────────────────────────────────────────────────


async def test_list_agents_empty(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "GET", "/v1/agents"); h.update(_HOST)
    r = await client.get("/v1/agents", headers=h)
    j = await r.json()
    assert j["agents"] == []


async def test_list_marks_owned_correctly(client):
    user = make_user()
    home = isolated_home()  # fresh home so we control which agents exist
    user_root_pk = base64url_encode(user.root_key.public_key_bytes())
    other_root_pk = base64url_encode(b"\x99" * 32)
    write_test_agent(home, "owned-bot", owner_root_pubkey=user_root_pk)
    write_test_agent(home, "stranger-bot", owner_root_pubkey=other_root_pk)
    write_test_agent(home, "orphan-bot", owner_root_pubkey=None)

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents"); h.update(_HOST)
        r = await c.get("/v1/agents", headers=h)
        j = await r.json()
    by_id = {a["id"]: a for a in j["agents"]}
    assert by_id["owned-bot"]["owned"] is True
    assert by_id["stranger-bot"]["owned"] is False
    assert by_id["orphan-bot"]["owned"] is False


async def test_get_agent_redacts_secrets_for_non_owner():
    user = make_user()
    home = isolated_home()
    other_root_pk = base64url_encode(b"\x99" * 32)
    write_test_agent(home, "stranger-bot", owner_root_pubkey=other_root_pk)
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/stranger-bot"); h.update(_HOST)
        r = await c.get("/v1/agents/stranger-bot", headers=h)
        j = await r.json()
    assert j["owned"] is False
    assert j["runtime"]["api_key"] is None
    # Boolean stays exposed so UI can still render "(set)".
    assert j["runtime"]["api_key_set"] is True


async def test_get_agent_exposes_secrets_for_owner():
    user = make_user()
    home = isolated_home()
    user_root_pk = base64url_encode(user.root_key.public_key_bytes())
    write_test_agent(home, "owned-bot", owner_root_pubkey=user_root_pk)
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/owned-bot"); h.update(_HOST)
        r = await c.get("/v1/agents/owned-bot", headers=h)
        j = await r.json()
    assert j["owned"] is True
    assert j["runtime"]["api_key"] == "sk-ant-test-secret"


async def test_get_agent_404(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "GET", "/v1/agents/nope"); h.update(_HOST)
    r = await client.get("/v1/agents/nope", headers=h)
    assert r.status == 404


# ────────────────────────────────────────────────────────────────────
# /v1/agents/{id}/files
# ────────────────────────────────────────────────────────────────────


async def test_list_files_returns_workspace_root():
    user = make_user()
    home = isolated_home()
    workspace = write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
        workspace_files={"README.md": "hi", "src/main.py": "print('x')\n"},
    )
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files", headers=h)
        j = await r.json()
    names = {e["name"]: e for e in j["entries"]}
    assert names["README.md"]["kind"] == "file"
    assert names["README.md"]["size"] == 2
    assert names["src"]["kind"] == "dir"


async def test_list_files_subdir():
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
        workspace_files={"src/main.py": "x"},
    )
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files?path=src"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files?path=src", headers=h)
        j = await r.json()
    assert [e["name"] for e in j["entries"]] == ["main.py"]


async def test_list_files_rejects_traversal():
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files?path=../../etc"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files?path=../../etc", headers=h)
        assert r.status == 400


async def test_list_files_rejects_absolute_path():
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files?path=/etc"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files?path=/etc", headers=h)
        assert r.status == 400


async def test_read_file_returns_text():
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
        workspace_files={"hello.txt": "hello world\n"},
    )
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files/raw?path=hello.txt"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files/raw?path=hello.txt", headers=h)
        assert r.status == 200
        assert (await r.text()) == "hello world\n"


async def test_read_file_rejects_binary():
    user = make_user()
    home = isolated_home()
    workspace = write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    (Path(workspace) / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files/raw?path=blob.bin"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files/raw?path=blob.bin", headers=h)
        assert r.status == 415


async def test_read_file_caps_size():
    user = make_user()
    home = isolated_home()
    workspace = write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    # 2 MiB > 1 MiB cap
    (Path(workspace) / "big.txt").write_bytes(b"x" * (2 * 1024 * 1024))
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files/raw?path=big.txt"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files/raw?path=big.txt", headers=h)
        assert r.status == 413


# ────────────────────────────────────────────────────────────────────
# PUF-208 v2: profile_summary byte-cap on PATCH /v1/agents/{id}/profile
# ────────────────────────────────────────────────────────────────────


async def test_update_profile_accepts_summary_at_cap():
    # Exactly MAX_PROFILE_SUMMARY_BYTES (10000) UTF-8 bytes must pass.
    # The cap is byte-counted so the on-disk profile.md size matches
    # what the server enforced.
    from puffo_agent.portal.api.handlers import MAX_PROFILE_SUMMARY_BYTES

    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "soul-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    summary = "x" * MAX_PROFILE_SUMMARY_BYTES
    assert len(summary.encode("utf-8")) == MAX_PROFILE_SUMMARY_BYTES

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        body = json.dumps({"profile_summary": summary}).encode("utf-8")
        h = signed_headers(user, "PATCH", "/v1/agents/soul-bot/profile", body)
        h.update(_HOST)
        h["content-type"] = "application/json"
        r = await c.patch("/v1/agents/soul-bot/profile", data=body, headers=h)
        assert r.status == 200, await r.text()


async def test_update_profile_rejects_summary_over_cap():
    # MAX + 1 byte must reject with 400 + a body that fingers the
    # offending byte count + the configured cap, so the UI can map
    # the message without re-parsing.
    from puffo_agent.portal.api.handlers import MAX_PROFILE_SUMMARY_BYTES

    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "soul-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    summary = "x" * (MAX_PROFILE_SUMMARY_BYTES + 1)

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        body = json.dumps({"profile_summary": summary}).encode("utf-8")
        h = signed_headers(user, "PATCH", "/v1/agents/soul-bot/profile", body)
        h.update(_HOST)
        h["content-type"] = "application/json"
        r = await c.patch("/v1/agents/soul-bot/profile", data=body, headers=h)
        assert r.status == 400, await r.text()
        body_json = await r.json()
        assert str(MAX_PROFILE_SUMMARY_BYTES) in body_json["error"]
        assert str(MAX_PROFILE_SUMMARY_BYTES + 1) in body_json["error"]


async def test_update_profile_caps_on_utf8_bytes_not_codepoints():
    # CJK characters take 3 UTF-8 bytes each. 3334 CJK characters =
    # 10002 bytes → rejected. (Even though 3334 codepoints is well
    # under any plausible codepoint cap.)
    from puffo_agent.portal.api.handlers import MAX_PROFILE_SUMMARY_BYTES

    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "soul-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    # 3334 × 3 bytes = 10002 (just over the 10000 cap).
    summary = "字" * 3334
    assert len(summary.encode("utf-8")) > MAX_PROFILE_SUMMARY_BYTES

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        body = json.dumps({"profile_summary": summary}).encode("utf-8")
        h = signed_headers(user, "PATCH", "/v1/agents/soul-bot/profile", body)
        h.update(_HOST)
        h["content-type"] = "application/json"
        r = await c.patch("/v1/agents/soul-bot/profile", data=body, headers=h)
        assert r.status == 400, await r.text()


async def test_update_profile_caps_post_strip():
    # PR #51 review item 2: the cap must check the same payload that
    # storage writes. A user sending MAX + 20 bytes of content where
    # the leading/trailing whitespace strips to exactly MAX bytes
    # must be accepted — pre-fix, this was 400'd because the cap ran
    # on the RAW payload while storage ran on the STRIPPED payload.
    from puffo_agent.portal.api.handlers import MAX_PROFILE_SUMMARY_BYTES

    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "soul-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    # Pad with whitespace so strip() lands exactly at the cap.
    summary = " " * 10 + ("x" * MAX_PROFILE_SUMMARY_BYTES) + " " * 10
    assert len(summary.encode("utf-8")) == MAX_PROFILE_SUMMARY_BYTES + 20

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        body = json.dumps({"profile_summary": summary}).encode("utf-8")
        h = signed_headers(user, "PATCH", "/v1/agents/soul-bot/profile", body)
        h.update(_HOST)
        h["content-type"] = "application/json"
        r = await c.patch("/v1/agents/soul-bot/profile", data=body, headers=h)
        assert r.status == 200, await r.text()


# ────────────────────────────────────────────────────────────────────
# PUF-294 (FB-294): rename folds into PATCH /v1/agents/{id}/profile —
# profile.md is rewritten with the new display_name and a reload.flag
# is dropped so the worker re-assembles CLAUDE.md / AGENTS.md /
# GEMINI.md on the next message (no operator-DM workaround).
# ────────────────────────────────────────────────────────────────────


async def _rename_agent(c, user, agent_id: str, new_name: str):
    body = json.dumps({"display_name": new_name}).encode("utf-8")
    path = f"/v1/agents/{agent_id}/profile"
    h = signed_headers(user, "PATCH", path, body); h.update(_HOST)
    h["content-type"] = "application/json"
    return await c.patch(path, data=body, headers=h)


async def test_puf294_rename_rewrites_profile_md_and_drops_reload_flag():
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "rename-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    agent_dir = Path(home) / "agents" / "rename-bot"
    # The default display_name from write_test_agent is the agent id;
    # seed profile.md with references that look operator-written.
    (agent_dir / "profile.md").write_text(
        "# Your role\n\n"
        "You are rename-bot, our helpful assistant. rename-bot writes "
        "docs and pings rename-bot's teammates when stuck.\n",
        encoding="utf-8",
    )
    flag_path = agent_dir / "workspace" / ".puffo-agent" / "reload.flag"
    assert not flag_path.exists()

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        r = await _rename_agent(c, user, "rename-bot", "Helper Bot")
        assert r.status == 200, await r.text()

    body = (agent_dir / "profile.md").read_text(encoding="utf-8")
    assert "rename-bot" not in body
    assert body.count("Helper Bot") == 3
    assert flag_path.exists()
    flag_body = json.loads(flag_path.read_text(encoding="utf-8"))
    assert flag_body.get("version") == 1
    assert "requested_at" in flag_body
    assert flag_body.get("reason") == "agent rename"


async def test_puf294_unchanged_display_name_is_a_noop():
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "stay-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    agent_dir = Path(home) / "agents" / "stay-bot"
    (agent_dir / "profile.md").write_text(
        "# Your role\n\nYou are stay-bot.\n", encoding="utf-8",
    )
    profile_mtime_before = (agent_dir / "profile.md").stat().st_mtime_ns
    flag_path = agent_dir / "workspace" / ".puffo-agent" / "reload.flag"

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        # PATCH with the SAME display_name → no rename → no rewrite +
        # no reload.flag.
        r = await _rename_agent(c, user, "stay-bot", "stay-bot")
        assert r.status == 200, await r.text()

    assert (agent_dir / "profile.md").stat().st_mtime_ns == profile_mtime_before
    assert not flag_path.exists()


async def test_puf294_rename_drops_reload_flag_even_when_profile_md_has_no_old_name():
    # The agent's profile.md doesn't reference the old name at all —
    # the rewrite is a no-op (0 replacements) but the reload.flag still
    # fires because agent.yml's display_name changed and we want the
    # next reload to pick up the new state.
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "nameless-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    agent_dir = Path(home) / "agents" / "nameless-bot"
    (agent_dir / "profile.md").write_text(
        "# Your role\n\nGeneric helpful agent. No name embedded.\n",
        encoding="utf-8",
    )
    profile_before = (agent_dir / "profile.md").read_text(encoding="utf-8")
    flag_path = agent_dir / "workspace" / ".puffo-agent" / "reload.flag"

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        r = await _rename_agent(c, user, "nameless-bot", "Renamed Bot")
        assert r.status == 200, await r.text()

    # profile.md content unchanged (the old name wasn't there to find)
    assert (agent_dir / "profile.md").read_text(encoding="utf-8") == profile_before
    # reload.flag still dropped so the next reload picks up agent.yml's
    # new display_name in case other surfaces (banner, agent list, etc)
    # read it.
    assert flag_path.exists()


async def test_puf294_rename_handles_cjk_display_names():
    # Family-ops fleet uses CJK names; rewrite must work without \b
    # word boundaries (which don't fire between CJK characters).
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "cjk-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    agent_dir = Path(home) / "agents" / "cjk-bot"
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        # Initial rename so the bot is set up with the CJK old name.
        r = await _rename_agent(c, user, "cjk-bot", "田中")
        assert r.status == 200
        (agent_dir / "profile.md").write_text(
            "# 角色\n\n你是田中，家庭群组里的助手。田中负责整理大家的安排。\n",
            encoding="utf-8",
        )
        # Now rename to the new CJK name.
        r = await _rename_agent(c, user, "cjk-bot", "山田")
        assert r.status == 200, await r.text()

    body = (agent_dir / "profile.md").read_text(encoding="utf-8")
    assert "田中" not in body
    assert body.count("山田") == 2


async def test_puf294_rename_doesnt_overreach_other_fields():
    # Renaming via PATCH should NOT clobber operator-edited
    # profile.md sections that don't reference the old name. Regression
    # guard: the rewrite is a literal substring replace, not a profile
    # regeneration.
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "preserve-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    agent_dir = Path(home) / "agents" / "preserve-bot"
    operator_prose = (
        "# Your role\n\n"
        "You are preserve-bot — a senior backend engineer who loves "
        "rust, prefers small PRs, and never force-pushes to main.\n\n"
        "## Style notes\n\nReply in clipped sentences. Avoid emoji.\n"
    )
    (agent_dir / "profile.md").write_text(operator_prose, encoding="utf-8")

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        r = await _rename_agent(c, user, "preserve-bot", "Preserve Bot")
        assert r.status == 200, await r.text()

    body = (agent_dir / "profile.md").read_text(encoding="utf-8")
    # Name flipped; everything else verbatim.
    assert "preserve-bot" not in body
    assert "Preserve Bot — a senior backend engineer" in body
    assert "## Style notes" in body
    assert "force-pushes to main" in body


async def test_puf294_rename_succeeds_when_reload_flag_write_fails(monkeypatch):
    # PR #82 polish: the rename PATCH must still return 200 even when
    # the reload.flag write hits a transient OSError (read-only
    # workspace / disk-full / cross-uid ``.puffo-agent/``). The agent
    # still picks up the new name on the worker's next restart;
    # ``logger.warning`` carries the diagnostic. Lock the contract so a
    # future regression that bubbles the OSError fails here loudly.
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "ro-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    agent_dir = Path(home) / "agents" / "ro-bot"
    (agent_dir / "profile.md").write_text(
        "You are ro-bot.\n", encoding="utf-8",
    )

    # Patch ``Path.write_text`` so the reload.flag write blows up, but
    # the profile.md rewrite (which uses the same method) still
    # succeeds. We branch by the path's basename so only the flag is
    # affected.
    real_write_text = Path.write_text
    flag_calls = {"n": 0}

    def fake_write_text(self, *args, **kwargs):
        if self.name == "reload.flag":
            flag_calls["n"] += 1
            raise PermissionError("simulated readonly workspace")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        r = await _rename_agent(c, user, "ro-bot", "Resilient Bot")
        # Rename still succeeds even though the flag write failed.
        assert r.status == 200, await r.text()
    assert flag_calls["n"] == 1, "expected one flag-write attempt"
    # profile.md still landed (proof the OSError was scoped to the flag).
    assert "Resilient Bot" in (agent_dir / "profile.md").read_text(encoding="utf-8")


async def test_puf294_substring_replace_is_intentional_design():
    # PR #82 polish: pin the substring-replace contract — the known
    # "Bob → Robert inside Bobcat rewrites to Robertcat" limit is
    # *intentional* (so CJK names work without word boundaries). A
    # future "fix" that adds ``\b`` guards would break the CJK cohort;
    # this test fails loudly if someone tries.
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home,
        "footgun-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    agent_dir = Path(home) / "agents" / "footgun-bot"
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        # Set initial name to "Bob".
        r = await _rename_agent(c, user, "footgun-bot", "Bob")
        assert r.status == 200
        (agent_dir / "profile.md").write_text(
            "You are Bob, who watches Bobcats and writes about Bob's cabin.\n",
            encoding="utf-8",
        )
        r = await _rename_agent(c, user, "footgun-bot", "Robert")
        assert r.status == 200, await r.text()

    body = (agent_dir / "profile.md").read_text(encoding="utf-8")
    # All Bob occurrences flipped — including the Bobcat one. The
    # operator can clean this up by editing profile.md, which is the
    # documented tradeoff for the CJK cohort working at all.
    assert body == (
        "You are Robert, who watches Robertcats and writes about Robert's cabin.\n"
    )


# Unit-level coverage on the ``rewrite_profile_name`` helper lives in
# ``tests/test_rewrite_profile_name.py`` — sync tests don't compose
# with this file's ``pytestmark = pytest.mark.asyncio``.
