"""``auto_accept_dm`` gate + ``add_dm_allowlist`` / ``update_dm_blocklist``
MCP tools. Covers config round-trip, disk persistence of pending
approvals, foreign-sender gating, operator y/n reply intercept, and
both mutation paths (CLI + linked-machine control op)."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from _bridge_support import isolated_home


# ─────────────────────────────────────────────────────────────────────
# Config round-trip + default
# ─────────────────────────────────────────────────────────────────────


def test_puffo_core_config_default_auto_accept_dm_is_true():
    from puffo_agent.portal.state import PuffoCoreConfig

    pc = PuffoCoreConfig()
    assert pc.auto_accept_dm is True


def test_agent_yml_round_trip_preserves_auto_accept_dm(tmp_path, monkeypatch):
    isolated_home()
    from puffo_agent.portal.state import (
        AgentConfig,
        PuffoCoreConfig,
        RuntimeConfig,
        TriggerRules,
        agent_dir,
    )

    cfg = AgentConfig(
        id="alpha",
        display_name="Alpha",
        puffo_core=PuffoCoreConfig(
            server_url="http://example",
            slug="alpha-bot",
            device_id="dev_x",
            space_id="sp_x",
            operator_slug="op-1",
            auto_accept_dm=False,
        ),
        runtime=RuntimeConfig(kind="chat-local"),
        triggers=TriggerRules(),
    )
    agent_dir("alpha").mkdir(parents=True, exist_ok=True)
    cfg.save()

    reloaded = AgentConfig.load("alpha")
    assert reloaded.puffo_core.auto_accept_dm is False


def test_agent_yml_missing_auto_accept_dm_defaults_to_true(tmp_path):
    isolated_home()
    import yaml
    from puffo_agent.portal.state import AgentConfig, agent_dir

    adir = agent_dir("alpha")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "agent.yml").write_text(
        yaml.safe_dump({
            "id": "alpha",
            "state": "running",
            "display_name": "Alpha",
            "puffo_core": {
                "server_url": "http://example",
                "slug": "alpha-bot",
                "device_id": "dev_x",
                "space_id": "sp_x",
                "operator_slug": "op-1",
                # NB: auto_accept_dm omitted on purpose.
            },
            "runtime": {"kind": "chat-local"},
        }),
        encoding="utf-8",
    )
    assert AgentConfig.load("alpha").puffo_core.auto_accept_dm is True


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def test_cli_dm_accept_flips_flag_on_disk(tmp_path, capsys):
    isolated_home()
    import yaml
    from puffo_agent.portal.state import AgentConfig, agent_dir
    from puffo_agent.portal.cli import cmd_agent_dm_accept

    adir = agent_dir("alpha")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "agent.yml").write_text(
        yaml.safe_dump({
            "id": "alpha",
            "state": "paused",
            "display_name": "Alpha",
            "puffo_core": {"slug": "alpha-bot"},
            "runtime": {"kind": "chat-local"},
        }),
        encoding="utf-8",
    )

    args = argparse.Namespace(id="alpha", mode="off")
    assert cmd_agent_dm_accept(args) == 0
    assert AgentConfig.load("alpha").puffo_core.auto_accept_dm is False

    args = argparse.Namespace(id="alpha", mode="on")
    assert cmd_agent_dm_accept(args) == 0
    assert AgentConfig.load("alpha").puffo_core.auto_accept_dm is True


def test_cli_dm_accept_unknown_agent_exits_nonzero(tmp_path, capsys):
    isolated_home()
    from puffo_agent.portal.cli import cmd_agent_dm_accept

    args = argparse.Namespace(id="ghost", mode="off")
    assert cmd_agent_dm_accept(args) == 2


# ─────────────────────────────────────────────────────────────────────
# Linked-machine control op
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_control_set_auto_accept_dm_flips_flag(tmp_path):
    isolated_home()
    import yaml
    from puffo_agent.portal.state import AgentConfig, agent_dir
    from puffo_agent.portal.control.client import execute_command

    adir = agent_dir("alpha")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "agent.yml").write_text(
        yaml.safe_dump({
            "id": "alpha",
            "state": "paused",
            "display_name": "Alpha",
            "puffo_core": {"slug": "alpha-bot"},
            "runtime": {"kind": "chat-local"},
        }),
        encoding="utf-8",
    )

    result = await execute_command(
        op="set_auto_accept_dm", agent_slug="alpha",
        params={"auto_accept_dm": False},
    )
    assert result == {"ok": True, "auto_accept_dm": False}
    assert AgentConfig.load("alpha").puffo_core.auto_accept_dm is False


@pytest.mark.asyncio
async def test_control_set_auto_accept_dm_rejects_non_bool(tmp_path):
    isolated_home()
    import yaml
    from puffo_agent.portal.state import agent_dir
    from puffo_agent.portal.control.client import execute_command

    adir = agent_dir("alpha")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "agent.yml").write_text(
        yaml.safe_dump({"id": "alpha", "display_name": "Alpha"}),
        encoding="utf-8",
    )

    result = await execute_command(
        op="set_auto_accept_dm", agent_slug="alpha",
        params={"auto_accept_dm": "yes"},
    )
    assert result["ok"] is False
    assert "bool" in result["error"]


# ─────────────────────────────────────────────────────────────────────
# Disk persistence
# ─────────────────────────────────────────────────────────────────────


def test_pending_dm_approvals_round_trip(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir
    from puffo_agent.agent.dm_approvals import (
        load_pending_dm_approvals,
        save_pending_dm_approvals,
    )

    agent_dir("alpha").mkdir(parents=True, exist_ok=True)
    assert load_pending_dm_approvals("alpha") == {}

    pending = {
        "prompt_env_id_1": {
            "sender_slug": "alice-1234",
            "sender_display_name": "Alice",
            "buffered_text": "hello",
            "buffered_envelope_id": "msg_abc",
            "buffered_sent_at": 1000,
            "buffered_root_id": "",
            "buffered_attachment_paths": [],
        }
    }
    save_pending_dm_approvals("alpha", pending)
    assert load_pending_dm_approvals("alpha") == pending


def test_pending_dm_approvals_malformed_file_returns_empty(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir
    from puffo_agent.agent.dm_approvals import (
        load_pending_dm_approvals,
        pending_dm_approvals_path,
    )

    agent_dir("alpha").mkdir(parents=True, exist_ok=True)
    path = pending_dm_approvals_path("alpha")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not JSON", encoding="utf-8")
    assert load_pending_dm_approvals("alpha") == {}


# ─────────────────────────────────────────────────────────────────────
# Gate logic — manual client surgery, no WS / HTTP
# ─────────────────────────────────────────────────────────────────────


def _make_client(*, auto_accept_dm: bool, operator_slug: str = "op-1"):
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient

    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.operator_slug = operator_slug
    client.auto_accept_dm = auto_accept_dm
    client._pending_dm_approvals = {}
    client._dm_allowlisted_senders = set()
    client._last_dm_sender = ""
    client._log = logging.getLogger("auto-accept-dm-test")

    sent_dms: list[dict] = []

    async def _stub_send_dm(slug, text, root_id=""):
        env_id = f"prompt_env_{len(sent_dms) + 1}"
        sent_dms.append({"to": slug, "text": text, "root_id": root_id, "env_id": env_id})
        return {"envelope_id": env_id}

    async def _stub_fetch_user_profile(slug, *, force_refresh=False):
        return (slug.title(), "")

    posts: list[tuple] = []
    deletes: list[tuple] = []

    class _StubHttp:
        async def post(self, path, body):
            posts.append((path, body))
            return {}
        async def delete(self, path, body=None):
            deletes.append((path, body))
            return {}

    client.http = _StubHttp()
    client._send_dm = _stub_send_dm  # type: ignore[assignment]
    client._fetch_user_profile = _stub_fetch_user_profile  # type: ignore[assignment]

    admitted: list[dict] = []

    async def _stub_admit_thread_message(*, root_id, priority, msg_dict, channel_meta):
        admitted.append({"root_id": root_id, "msg_dict": msg_dict, "channel_meta": channel_meta})

    client._admit_thread_message = _stub_admit_thread_message  # type: ignore[assignment]

    client._sent_dms = sent_dms  # type: ignore[attr-defined]
    client._posts = posts  # type: ignore[attr-defined]
    client._deletes = deletes  # type: ignore[attr-defined]
    client._admitted = admitted  # type: ignore[attr-defined]
    return client


def test_is_foreign_dm_sender_correctly_excludes_operator_and_self():
    client = _make_client(auto_accept_dm=False)
    assert client._is_foreign_dm_sender("op-1") is False
    assert client._is_foreign_dm_sender("agent-1") is False
    assert client._is_foreign_dm_sender("alice-1234") is True
    assert client._is_foreign_dm_sender("") is False


@pytest.mark.asyncio
async def test_gate_buffers_foreign_dm_and_prompts_operator(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)

    handled = await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234",
        text="hello agent",
        envelope_id="msg_inbound_1",
        sent_at=1000,
        thread_root_id="",
        attachment_paths=[],
    )
    assert handled is True
    assert len(client._sent_dms) == 1
    sent = client._sent_dms[0]
    assert sent["to"] == "op-1"
    # `/permission` prefix → Yes/No buttons in the operator's client.
    assert sent["text"].startswith("/permission ")
    assert "alice-1234" in sent["text"]
    assert "hello agent" in sent["text"]
    prompt_env = sent["env_id"]
    assert prompt_env in client._pending_dm_approvals
    assert client._pending_dm_approvals[prompt_env]["sender_slug"] == "alice-1234"


@pytest.mark.asyncio
async def test_gate_drops_duplicate_sender_while_pending(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)

    await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="hello", envelope_id="msg_1",
        sent_at=1000, thread_root_id="", attachment_paths=[],
    )
    handled = await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="bothering you again",
        envelope_id="msg_2", sent_at=2000, thread_root_id="",
        attachment_paths=[],
    )
    assert handled is True
    assert len(client._sent_dms) == 1


@pytest.mark.asyncio
async def test_approval_y_posts_allowlist_and_delivers_buffered_dm(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)
    await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="hello", envelope_id="msg_in_1",
        sent_at=1000, thread_root_id="", attachment_paths=[],
    )
    prompt_env_id = client._sent_dms[0]["env_id"]

    handled = await client._maybe_handle_dm_approval_reply(
        thread_root_id=prompt_env_id, text="y",
    )
    assert handled is True
    assert ("/allowlists", {"slugs": ["alice-1234"]}) in client._posts
    assert "alice-1234" in client._dm_allowlisted_senders
    assert client._pending_dm_approvals == {}
    assert len(client._admitted) == 1
    delivered = client._admitted[0]
    assert delivered["msg_dict"]["sender_slug"] == "alice-1234"
    assert delivered["msg_dict"]["text"] == "hello"
    assert delivered["msg_dict"]["is_dm"] is True


@pytest.mark.asyncio
async def test_approval_n_posts_blocklist_and_drops_buffered_dm(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)
    await client._maybe_gate_foreign_dm(
        sender_slug="bob-7777", text="spam spam", envelope_id="msg_in_2",
        sent_at=2000, thread_root_id="", attachment_paths=[],
    )
    prompt_env_id = client._sent_dms[0]["env_id"]

    handled = await client._maybe_handle_dm_approval_reply(
        thread_root_id=prompt_env_id, text="n",
    )
    assert handled is True
    assert ("/blocklists", {"target": "user", "id": "bob-7777"}) in client._posts
    assert client._pending_dm_approvals == {}
    # Blocked: the buffered message must NOT be delivered.
    assert client._admitted == []


@pytest.mark.asyncio
async def test_approval_reply_ignores_non_yn_text(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)
    await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="hello", envelope_id="msg_in_1",
        sent_at=1000, thread_root_id="", attachment_paths=[],
    )
    prompt_env_id = client._sent_dms[0]["env_id"]

    handled = await client._maybe_handle_dm_approval_reply(
        thread_root_id=prompt_env_id, text="maybe later",
    )
    assert handled is False
    # Pending entry survives so a later y/n can still resolve it.
    assert prompt_env_id in client._pending_dm_approvals


@pytest.mark.asyncio
async def test_gate_skips_when_operator_slug_missing(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False, operator_slug="")

    handled = await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="hello", envelope_id="msg_in_1",
        sent_at=1000, thread_root_id="", attachment_paths=[],
    )
    # Falls through (returns False) so the DM still reaches the agent.
    assert handled is False
    assert client._sent_dms == []


# ─────────────────────────────────────────────────────────────────────
# MCP tools: add_dm_allowlist + update_dm_blocklist call the right paths
# ─────────────────────────────────────────────────────────────────────


class _StubHttp:
    def __init__(self):
        self.posts: list[tuple] = []
        self.deletes: list[tuple] = []

    async def post(self, path, body):
        self.posts.append((path, body))
        return {}

    async def delete(self, path, body=None):
        self.deletes.append((path, body))
        return {}


def _build_mcp_with_tools():
    from mcp.server.fastmcp import FastMCP
    from puffo_agent.mcp.puffo_core_tools import (
        PuffoCoreToolsConfig, register_core_tools,
    )

    http = _StubHttp()
    cfg = PuffoCoreToolsConfig(
        slug="agent-1",
        device_id="dev_x",
        keystore=None,
        http_client=http,
        data_client=None,
        space_id="sp_x",
        workspace="/tmp",
    )
    mcp = FastMCP("puffo")
    register_core_tools(mcp, cfg)
    return mcp, http


@pytest.mark.asyncio
async def test_mcp_add_dm_allowlist_posts_to_allowlists():
    mcp, http = _build_mcp_with_tools()
    tool = mcp._tool_manager._tools["add_dm_allowlist"]
    result = await tool.fn(slug="alice-1234")
    assert http.posts == [("/allowlists", {"slugs": ["alice-1234"]})]
    assert "alice-1234" in result


@pytest.mark.asyncio
async def test_mcp_update_dm_blocklist_on_posts_to_blocklists():
    mcp, http = _build_mcp_with_tools()
    tool = mcp._tool_manager._tools["update_dm_blocklist"]
    await tool.fn(slug="bob-7777", on=True)
    assert http.posts == [("/blocklists", {"target": "user", "id": "bob-7777"})]


@pytest.mark.asyncio
async def test_mcp_update_dm_blocklist_off_deletes_blocklists():
    mcp, http = _build_mcp_with_tools()
    tool = mcp._tool_manager._tools["update_dm_blocklist"]
    await tool.fn(slug="bob-7777", on=False)
    assert http.deletes == [("/blocklists", {"id": "bob-7777"})]


def test_mcp_tool_names_list_has_new_tools():
    from puffo_agent.mcp.config import PUFFO_CORE_TOOL_NAMES

    assert "add_dm_allowlist" in PUFFO_CORE_TOOL_NAMES
    assert "update_dm_blocklist" in PUFFO_CORE_TOOL_NAMES
