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


def test_auto_accept_dm_is_a_hidden_yaml_flag(tmp_path):
    """auto_accept_dm mirrors auto_accept_space_invitations: a yaml-only
    flag with no CLI subcommand, UI checkbox, or control op."""
    isolated_home()
    import yaml
    from puffo_agent.portal.state import AgentConfig, agent_dir
    from puffo_agent.portal import cli as cli_mod
    from puffo_agent.portal.control import client as control_mod
    import inspect

    adir = agent_dir("alpha")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "agent.yml").write_text(
        yaml.safe_dump({
            "id": "alpha",
            "state": "paused",
            "display_name": "Alpha",
            "puffo_core": {"slug": "alpha-bot", "auto_accept_dm": False},
            "runtime": {"kind": "chat-local"},
        }),
        encoding="utf-8",
    )
    # The yaml field still loads (and defaults True when absent).
    assert AgentConfig.load("alpha").puffo_core.auto_accept_dm is False

    # No mutation surfaces remain.
    assert not hasattr(cli_mod, "cmd_agent_dm_accept")
    assert "set_auto_accept_dm" not in inspect.getsource(control_mod.execute_command)


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


def test_pending_dm_approvals_non_dict_json_returns_empty(tmp_path):
    import json
    isolated_home()
    from puffo_agent.portal.state import agent_dir
    from puffo_agent.agent.dm_approvals import (
        load_pending_dm_approvals,
        pending_dm_approvals_path,
    )

    agent_dir("alpha").mkdir(parents=True, exist_ok=True)
    path = pending_dm_approvals_path("alpha")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
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
    client._last_dm_sender = ""
    client._log = logging.getLogger("auto-accept-dm-test")

    sent_dms: list[dict] = []

    async def _stub_send_dm(slug, text, root_id):  # mirror real _send_dm arity
        env_id = f"prompt_env_{len(sent_dms) + 1}"
        sent_dms.append({"to": slug, "text": text, "root_id": root_id, "env_id": env_id})
        return {"envelope_id": env_id}

    async def _stub_fetch_user_profile(slug, *, force_refresh=False):
        return (slug.title(), "")

    posts: list[tuple] = []
    deletes: list[tuple] = []
    gets: list[str] = []
    pending_rows: list[dict] = []  # tests set what /messages/pending returns

    spaces_rows: list[dict] = []  # what GET /spaces returns

    class _StubHttp:
        async def post(self, path, body):
            posts.append((path, body))
            return {}

        async def get(self, path):
            gets.append(path)
            if path == "/spaces":
                return {"spaces": list(spaces_rows)}
            return {"messages": list(pending_rows)}

        async def delete(self, path, body=None):
            deletes.append((path, body))
            return {}

    owner_of: dict[str, str] = {}

    async def _stub_fetch_owner_slug(slug):
        return owner_of.get(slug, "")

    client.http = _StubHttp()
    from puffo_agent.agent.contact_cache import ContactCache
    client._contacts = ContactCache(client.http, client._log)
    client._send_dm = _stub_send_dm  # type: ignore[assignment]
    client._fetch_user_profile = _stub_fetch_user_profile  # type: ignore[assignment]
    client._fetch_owner_slug = _stub_fetch_owner_slug  # type: ignore[assignment]
    client._owner_of = owner_of  # type: ignore[attr-defined]  # tests inject owners

    # Stub WS whose on_message the drain feeds re-fetched envelopes through.
    handled: list[dict] = []

    async def _stub_on_message(envelope):
        handled.append(envelope)
        return None  # not gated → ack

    class _StubWs:
        on_message = None

    ws = _StubWs()
    ws.on_message = _stub_on_message  # type: ignore[assignment]
    client._ws = ws  # type: ignore[assignment]

    class _StubNoticeStore:
        def __init__(self):
            self.notices: dict[str, int] = {}

        async def get_dm_notice(self, slug):
            return self.notices.get(slug)

        async def set_dm_notice(self, slug, ts):
            self.notices[slug] = ts

    client.store = _StubNoticeStore()  # type: ignore[assignment]

    async def _stub_fetch_display_name(slug):
        return slug.title()

    client._fetch_display_name = _stub_fetch_display_name  # type: ignore[assignment]

    space_members: dict[str, dict[str, str]] = {}

    async def _stub_get_space_members(space_id):
        return space_members.get(space_id, {})

    client._get_space_members = _stub_get_space_members  # type: ignore[assignment]
    client._space_members_stub = space_members  # type: ignore[attr-defined]
    client._spaces_rows = spaces_rows  # type: ignore[attr-defined]

    client._sent_dms = sent_dms  # type: ignore[attr-defined]
    client._posts = posts  # type: ignore[attr-defined]
    client._deletes = deletes  # type: ignore[attr-defined]
    client._gets = gets  # type: ignore[attr-defined]
    client._pending_rows = pending_rows  # type: ignore[attr-defined]
    client._handled = handled  # type: ignore[attr-defined]
    return client


@pytest.mark.asyncio
async def test_is_foreign_dm_sender_excludes_operator_self_and_co_owned_agents():
    client = _make_client(auto_accept_dm=False)
    client._owner_of["sibling-agt-9"] = "op-1"          # my operator's agent
    client._owner_of["other-agt-5"] = "someone-else-2222"  # a different operator's
    assert await client._is_foreign_dm_sender("op-1") is False
    assert await client._is_foreign_dm_sender("agent-1") is False
    assert await client._is_foreign_dm_sender("sibling-agt-9") is False
    assert await client._is_foreign_dm_sender("other-agt-5") is True
    assert await client._is_foreign_dm_sender("alice-1234") is True  # human, no owner
    assert await client._is_foreign_dm_sender("") is False


@pytest.mark.asyncio
async def test_gate_buffers_foreign_dm_and_prompts_operator(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)

    handled = await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234",
        text="hello agent",
    )
    assert handled is True
    # Operator prompt + a one-time ack back to the sender.
    assert len(client._sent_dms) == 2
    sent = client._sent_dms[0]
    assert sent["to"] == "op-1"
    # `/permission` prefix → Yes/No buttons in the operator's client.
    assert sent["text"].startswith("/permission ")
    assert "alice-1234" in sent["text"]
    assert "hello agent" in sent["text"]
    prompt_env = sent["env_id"]
    assert prompt_env in client._pending_dm_approvals
    assert client._pending_dm_approvals[prompt_env]["sender_slug"] == "alice-1234"
    # Sender is told the DM landed and is awaiting approval.
    ack = client._sent_dms[1]
    assert ack["to"] == "alice-1234"
    assert not ack["text"].startswith("/permission")


@pytest.mark.asyncio
async def test_gate_drops_duplicate_sender_while_pending(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)

    await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="hello",
    )
    handled = await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="bothering you again",
    )
    assert handled is True
    # Duplicate re-gated but NOT re-prompted: one operator prompt + one
    # sender ack from the first gate, nothing from the second.
    op_prompts = [d for d in client._sent_dms if d["to"] == "op-1"]
    assert len(op_prompts) == 1
    assert len(client._sent_dms) == 2


@pytest.mark.asyncio
async def test_approval_y_allowlists_and_drains_pending_dms(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)
    await client._maybe_gate_foreign_dm(sender_slug="alice-1234", text="hello")
    prompt_env_id = client._sent_dms[0]["env_id"]
    # Two DMs from alice held un-acked in /messages/pending.
    client._pending_rows.extend([
        {"envelope": {"envelope_id": "env_a", "sender_slug": "alice-1234"}},
        {"envelope": {"envelope_id": "env_b", "sender_slug": "alice-1234"}},
    ])

    handled = await client._maybe_handle_dm_approval_reply(
        thread_root_id=prompt_env_id, text="y",
    )
    assert handled is True
    assert ("/allowlists", {"slugs": ["alice-1234"]}) in client._posts
    assert "alice-1234" in client._contacts._allow
    assert client._pending_dm_approvals == {}
    # Drain scoped-fetched alice's pending DMs + fed them through on_message.
    assert any("kind=dm" in g and "sender=alice-1234" in g for g in client._gets)
    assert [e.get("envelope_id") for e in client._handled] == ["env_a", "env_b"]
    # Then acked exactly those.
    assert ("/messages/ack", {"envelope_ids": ["env_a", "env_b"]}) in client._posts


@pytest.mark.asyncio
async def test_approval_n_blocklists_and_drops_pending_dms(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)
    await client._maybe_gate_foreign_dm(sender_slug="bob-7777", text="spam spam")
    prompt_env_id = client._sent_dms[0]["env_id"]
    client._pending_rows.append(
        {"envelope": {"envelope_id": "env_x", "sender_slug": "bob-7777"}}
    )

    handled = await client._maybe_handle_dm_approval_reply(
        thread_root_id=prompt_env_id, text="n",
    )
    assert handled is True
    assert ("/blocklists", {"target": "user", "id": "bob-7777"}) in client._posts
    assert client._pending_dm_approvals == {}
    # Dropped: acked without handing anything to the agent.
    assert client._handled == []
    assert ("/messages/ack", {"envelope_ids": ["env_x"]}) in client._posts


@pytest.mark.asyncio
async def test_approval_keeps_pending_and_confirms_error_when_post_raises(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)

    async def _raising_post(path, body):
        raise RuntimeError("server unreachable")
    client.http.post = _raising_post  # type: ignore[assignment]

    await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="hello",
    )
    prompt_env_id = client._sent_dms[0]["env_id"]
    n_before = len(client._sent_dms)
    handled = await client._maybe_handle_dm_approval_reply(
        thread_root_id=prompt_env_id, text="y",
    )
    assert handled is True
    # Pending entry KEPT so a retry works once the server is reachable.
    assert prompt_env_id in client._pending_dm_approvals
    # Error-confirm DM sent in the prompt thread.
    assert len(client._sent_dms) == n_before + 1
    err = client._sent_dms[-1]
    assert err["root_id"] == prompt_env_id
    assert "failed" in err["text"].lower()


@pytest.mark.asyncio
async def test_approval_reply_ignores_non_yn_text(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)
    await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="hello",
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
        sender_slug="alice-1234", text="hello",
    )
    # Falls through (returns False) so the DM still reaches the agent.
    assert handled is False
    assert client._sent_dms == []


@pytest.mark.asyncio
async def test_gate_delivers_ungated_when_prompt_send_fails(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)

    async def _raising_send_dm(slug, text, root_id=""):
        raise RuntimeError("send failed")
    client._send_dm = _raising_send_dm  # type: ignore[assignment]

    handled = await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="hi",
    )
    # Prompt couldn't be sent → deliver ungated rather than swallow the DM.
    assert handled is False
    assert client._pending_dm_approvals == {}


@pytest.mark.asyncio
async def test_gate_delivers_ungated_when_prompt_has_no_envelope_id(tmp_path):
    isolated_home()
    from puffo_agent.portal.state import agent_dir

    agent_dir("agent-1").mkdir(parents=True, exist_ok=True)
    client = _make_client(auto_accept_dm=False)

    async def _no_env_send_dm(slug, text, root_id=""):
        return {}
    client._send_dm = _no_env_send_dm  # type: ignore[assignment]

    handled = await client._maybe_gate_foreign_dm(
        sender_slug="alice-1234", text="hi",
    )
    assert handled is False
    assert client._pending_dm_approvals == {}


# ─────────────────────────────────────────────────────────────────────
# MCP tools: add_dm_allowlist + update_dm_blocklist call the right paths
# ─────────────────────────────────────────────────────────────────────


class _StubHttp:
    def __init__(self):
        self.posts: list[tuple] = []
        self.deletes: list[tuple] = []
        self.allow_entries: list[dict] = []
        self.block_rows: list[dict] = []

    async def post(self, path, body):
        self.posts.append((path, body))
        return {}

    async def delete(self, path, body=None):
        self.deletes.append((path, body))
        return {}

    async def get(self, path):
        if path == "/allowlists":
            return {"entries": list(self.allow_entries)}
        if path == "/blocklists":
            return {"blocks": list(self.block_rows)}
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


# ─────────────────────────────────────────────────────────────────────
# Outbound-implies-allowlist (agent DMs a foreign peer first)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outbound_dm_allowlists_foreign_peer_and_notifies_operator():
    client = _make_client(auto_accept_dm=False)
    await client._maybe_allowlist_outbound_dm("charlie-9")
    assert ("/allowlists", {"slugs": ["charlie-9"]}) in client._posts
    assert "charlie-9" in client._contacts._allow
    op_dms = [d for d in client._sent_dms if d["to"] == "op-1"]
    assert len(op_dms) == 1
    assert "charlie-9" in op_dms[0]["text"]


@pytest.mark.asyncio
async def test_outbound_dm_to_operator_or_co_owned_is_noop():
    client = _make_client(auto_accept_dm=False)
    client._owner_of["sibling-agt"] = "op-1"
    await client._maybe_allowlist_outbound_dm("op-1")
    await client._maybe_allowlist_outbound_dm("sibling-agt")
    assert client._posts == []
    assert client._sent_dms == []


@pytest.mark.asyncio
async def test_outbound_dm_to_pending_sender_does_not_allowlist():
    client = _make_client(auto_accept_dm=False)
    # Sender we're currently gating; the ack DM to them echoes back here
    # and must NOT pre-empt the operator's pending y/n.
    client._pending_dm_approvals["prompt-1"] = {
        "sender_slug": "dave-7", "sender_display_name": "Dave",
    }
    await client._maybe_allowlist_outbound_dm("dave-7")
    assert client._posts == []
    assert "dave-7" not in client._contacts._allow


@pytest.mark.asyncio
async def test_outbound_dm_to_already_allowed_peer_is_noop():
    import time
    client = _make_client(auto_accept_dm=False)
    client._contacts.note_allowed("erin-3")
    client._contacts._fetched_at = time.monotonic()  # hydrated + fresh
    await client._maybe_allowlist_outbound_dm("erin-3")
    assert client._posts == []
    assert client._sent_dms == []


def test_blocked_channel_message_drop_is_wired_in_handle_envelope():
    """handle_envelope is a closure inside listen(); pin the blocked-
    sender channel-drop branch at source level so a revert can't slip
    past the unit tests (the full path is exercised by live smoke)."""
    import inspect
    from puffo_agent.agent import puffo_core_client as pcc

    src = inspect.getsource(pcc.PuffoCoreMessageClient.listen)
    assert 'payload.envelope_kind != "dm"' in src
    assert "_contacts.is_blocked(payload.sender_slug)" in src
    assert "_BLOCKED_MESSAGE_PLACEHOLDER" in src


def test_gate_consults_contact_cache_for_allowlist():
    """The DM gate must bypass allowlisted senders via the shared cache
    (not an ad-hoc set), so an operator/MCP allowlist takes effect."""
    import inspect
    from puffo_agent.agent import puffo_core_client as pcc

    src = inspect.getsource(pcc.PuffoCoreMessageClient.listen)
    assert "await self._contacts.is_allowed(payload.sender_slug)" in src


# ─────────────────────────────────────────────────────────────────────
# DM Gate ladder: trusted contacts, shared-space pass, 72h FYI
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trusted_contact_added_once():
    client = _make_client(auto_accept_dm=True)
    await client._ensure_trusted_contact("op-1")
    assert ("/allowlists", {"slugs": ["op-1"]}) in client._posts
    client._posts.clear()
    # Cached as allowed now — second DM doesn't re-POST.
    await client._ensure_trusted_contact("op-1")
    assert client._posts == []


@pytest.mark.asyncio
async def test_trusted_contact_noop_for_self_and_empty():
    client = _make_client(auto_accept_dm=True)
    await client._ensure_trusted_contact("agent-1")
    await client._ensure_trusted_contact("")
    assert client._posts == []


@pytest.mark.asyncio
async def test_trusted_contact_post_failure_not_cached():
    client = _make_client(auto_accept_dm=True)

    async def _boom(path, body):
        raise RuntimeError("allowlists down")

    client.http.post = _boom  # type: ignore[assignment]
    await client._ensure_trusted_contact("op-1")
    assert await client._contacts.is_allowed("op-1") is False


@pytest.mark.asyncio
async def test_shares_space_with_member():
    client = _make_client(auto_accept_dm=False)
    client._spaces_rows.append({"space_id": "sp_1"})
    client._space_members_stub["sp_1"] = {"alice-1234": "human"}
    assert await client._shares_space_with("alice-1234") is True
    assert await client._shares_space_with("stranger-9") is False


@pytest.mark.asyncio
async def test_shares_space_fails_closed_on_fetch_error():
    client = _make_client(auto_accept_dm=False)

    async def _boom(path):
        raise RuntimeError("spaces down")

    client.http.get = _boom  # type: ignore[assignment]
    assert await client._shares_space_with("alice-1234") is False


@pytest.mark.asyncio
async def test_dm_notice_first_time_notifies_and_persists():
    client = _make_client(auto_accept_dm=True)
    await client._maybe_send_dm_notice("alice-1234")
    fyi = [d for d in client._sent_dms if "FYI" in d["text"]]
    assert len(fyi) == 1
    assert fyi[0]["to"] == "op-1"
    assert "Alice-1234" in fyi[0]["text"]
    assert "is sending direct messages to me" in fyi[0]["text"]
    assert client.store.notices["alice-1234"] > 0


@pytest.mark.asyncio
async def test_dm_notice_throttled_within_72h():
    client = _make_client(auto_accept_dm=True)
    await client._maybe_send_dm_notice("alice-1234")
    await client._maybe_send_dm_notice("alice-1234")
    fyi = [d for d in client._sent_dms if "FYI" in d["text"]]
    assert len(fyi) == 1


@pytest.mark.asyncio
async def test_dm_notice_fires_again_after_72h():
    import time as _time
    client = _make_client(auto_accept_dm=True)
    stale = int(_time.time() * 1000) - (72 * 3600 * 1000 + 60_000)
    client.store.notices["alice-1234"] = stale
    await client._maybe_send_dm_notice("alice-1234")
    fyi = [d for d in client._sent_dms if "FYI" in d["text"]]
    assert len(fyi) == 1
    assert client.store.notices["alice-1234"] > stale


@pytest.mark.asyncio
async def test_dm_notice_send_failure_does_not_persist():
    client = _make_client(auto_accept_dm=True)

    async def _boom(slug, text, root_id):
        raise RuntimeError("dm down")

    client._send_dm = _boom  # type: ignore[assignment]
    await client._maybe_send_dm_notice("alice-1234")
    # Not recorded → the next DM retries the notice.
    assert "alice-1234" not in client.store.notices


@pytest.mark.asyncio
async def test_dm_notice_noop_without_operator():
    client = _make_client(auto_accept_dm=True, operator_slug="")
    await client._maybe_send_dm_notice("alice-1234")
    assert client._sent_dms == []


def test_gate_ladder_wiring_order():
    """handle_envelope's ladder order is the contract: blocked-DM drop
    before persistence; FYI before the permission prompt; shared-space
    check gates the prompt."""
    import inspect
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
    src = inspect.getsource(PuffoCoreMessageClient)
    drop = src.index("dm_gate: dropped DM from blocked")
    store = src.index('"envelope_id": payload.envelope_id', drop)
    assert drop < store, "blocked-DM drop must precede persistence"
    fyi = src.index("_maybe_send_dm_notice(payload.sender_slug)")
    gate = src.index("_maybe_gate_foreign_dm(", fyi)
    assert fyi < gate, "FYI must precede the permission prompt"
    shared = src.index("_shares_space_with(payload.sender_slug)")
    assert fyi < shared < gate, "shared-space pass sits between FYI and gate"


def test_gate_ladder_fyi_covers_contacts_too():
    """FYI exempts only the operator and co-owned agents — an allowlisted
    contact's DM still notifies, so the is_allowed check must sit AFTER
    the notice call in the ladder."""
    import inspect
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
    src = inspect.getsource(PuffoCoreMessageClient)
    fyi = src.index("_maybe_send_dm_notice(payload.sender_slug)")
    allowed = src.index("_contacts.is_allowed(payload.sender_slug)", fyi)
    assert fyi < allowed, "FYI must fire before the contact-pass check"


@pytest.mark.asyncio
async def test_shares_space_skips_malformed_space_rows():
    client = _make_client(auto_accept_dm=False)
    client._spaces_rows.append({"name": "no id here"})
    client._spaces_rows.append({"space_id": "sp_2"})
    client._space_members_stub["sp_2"] = {"alice-1234": "human"}
    assert await client._shares_space_with("alice-1234") is True


@pytest.mark.asyncio
async def test_dm_notice_store_read_failure_still_notifies():
    client = _make_client(auto_accept_dm=True)

    async def _read_boom(slug):
        raise RuntimeError("db locked")

    client.store.get_dm_notice = _read_boom  # type: ignore[assignment]
    await client._maybe_send_dm_notice("alice-1234")
    assert any("FYI" in d["text"] for d in client._sent_dms)


@pytest.mark.asyncio
async def test_dm_notice_store_write_failure_does_not_crash():
    client = _make_client(auto_accept_dm=True)

    async def _write_boom(slug, ts):
        raise RuntimeError("db locked")

    client.store.set_dm_notice = _write_boom  # type: ignore[assignment]
    await client._maybe_send_dm_notice("alice-1234")
    assert any("FYI" in d["text"] for d in client._sent_dms)


@pytest.mark.asyncio
async def test_mcp_get_dm_allowlists_lists_sorted_slugs():
    mcp, http = _build_mcp_with_tools()
    http.allow_entries = [
        {"peer_slug": "zed-9", "added_at": 2},
        {"peer_slug": "alice-1234", "added_at": 1},
        {"added_at": 3},  # malformed row skipped
    ]
    tool = mcp._tool_manager._tools["get_dm_allowlists"]
    out = await tool.fn()
    assert out == "DM allowlist:\n- alice-1234\n- zed-9"


@pytest.mark.asyncio
async def test_mcp_get_dm_allowlists_empty():
    mcp, http = _build_mcp_with_tools()
    tool = mcp._tool_manager._tools["get_dm_allowlists"]
    assert await tool.fn() == "DM allowlist is empty."


@pytest.mark.asyncio
async def test_mcp_get_dm_blocklists_filters_user_targets():
    mcp, http = _build_mcp_with_tools()
    http.block_rows = [
        {"target": "user", "id": "spammer-1"},
        {"target": "channel", "id": "ch_x"},  # non-user rows excluded
        {"target": "user"},  # malformed row skipped
    ]
    tool = mcp._tool_manager._tools["get_dm_blocklists"]
    assert await tool.fn() == "DM blocklist:\n- spammer-1"


@pytest.mark.asyncio
async def test_mcp_get_dm_blocklists_empty():
    mcp, http = _build_mcp_with_tools()
    tool = mcp._tool_manager._tools["get_dm_blocklists"]
    assert await tool.fn() == "DM blocklist is empty."


def test_mcp_tool_names_include_read_tools():
    from puffo_agent.mcp.config import PUFFO_CORE_TOOL_NAMES
    assert "get_dm_allowlists" in PUFFO_CORE_TOOL_NAMES
    assert "get_dm_blocklists" in PUFFO_CORE_TOOL_NAMES
