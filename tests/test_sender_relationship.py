"""Sender relationship classification: precedence (owner block > agent
friend > owner friend > default), cache behavior, fetch-failure
tolerance — plus the list_friends MCP tool surface wiring."""

from __future__ import annotations

import asyncio
import logging
import time

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeHttp:
    def __init__(self, responses: dict, fail: bool = False):
        self.responses = responses
        self.fail = fail
        self.calls: list[str] = []

    async def get(self, path: str):
        self.calls.append(path)
        if self.fail:
            raise RuntimeError("server unreachable")
        return self.responses.get(path, {"relationships": []})


def _client(responses: dict, *, fail: bool = False) -> PuffoCoreMessageClient:
    c = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    c.slug = "kai-1234"
    c.operator_slug = "op-9999"
    c.http = _FakeHttp(responses, fail=fail)
    c._relationship_maps = None
    c._relationship_maps_at = 0.0
    c._log = logging.getLogger("test-rel")
    return c


def _responses(own: dict, owner: dict) -> dict:
    def payload(m: dict) -> dict:
        return {
            "relationships": [
                {"other_slug": k, "status": v} for k, v in m.items()
            ]
        }

    return {
        "/v2/identities/kai-1234/relationships": payload(own),
        "/v2/identities/op-9999/relationships": payload(owner),
    }


def test_owner_block_wins_over_everything():
    c = _client(_responses(
        own={"eve-1": "friend"},
        owner={"eve-1": "blocked"},
    ))
    assert _run(c._sender_relationship("eve-1")) == "blocked"


def test_agent_friend_beats_owner_friend():
    c = _client(_responses(
        own={"bob-1": "friend"},
        owner={"bob-1": "friend"},
    ))
    assert _run(c._sender_relationship("bob-1")) == "owner_and_my_friend"


def test_owner_friend_alone_tags_owner_friend():
    c = _client(_responses(own={}, owner={"bob-1": "friend"}))
    assert _run(c._sender_relationship("bob-1")) == "owner_friend"


def test_allowed_and_unknown_are_default():
    c = _client(_responses(own={"carol-1": "allowed"}, owner={}))
    assert _run(c._sender_relationship("carol-1")) == "default"
    assert _run(c._sender_relationship("stranger-1")) == "default"


def test_own_block_also_blocks():
    c = _client(_responses(own={"eve-2": "blocked"}, owner={}))
    assert _run(c._sender_relationship("eve-2")) == "blocked"


def test_operator_sender_is_always_default():
    c = _client(_responses(own={}, owner={}))
    assert _run(c._sender_relationship("op-9999")) == "default"
    assert c.http.calls == []  # short-circuits before any fetch


def test_fetch_failure_defaults_and_does_not_raise():
    c = _client({}, fail=True)
    assert _run(c._sender_relationship("anyone-1")) == "default"


def test_maps_are_cached_within_ttl():
    c = _client(_responses(own={}, owner={}))

    async def twice():
        await c._sender_relationship("x-1")
        await c._sender_relationship("y-2")

    _run(twice())
    assert len(c.http.calls) == 2  # one fetch pair, second lookup cached


def test_cache_expires_after_ttl(monkeypatch):
    c = _client(_responses(own={}, owner={}))

    async def flow():
        await c._sender_relationship("x-1")
        c._relationship_maps_at = time.monotonic() - 3600
        await c._sender_relationship("x-1")

    _run(flow())
    assert len(c.http.calls) == 4


def test_list_friends_wired_on_every_surface():
    from puffo_agent.mcp.config import PUFFO_CORE_TOOL_NAMES
    from puffo_agent.portal.ws_local import tool_dispatch

    assert "list_friends" in PUFFO_CORE_TOOL_NAMES
    assert "list_friends" in tool_dispatch.WS_LOCAL_ALLOWED_TOOLS
    from puffo_agent.agent.shared_content import DEFAULT_SHARED_CLAUDE_MD

    assert "list_friends" in DEFAULT_SHARED_CLAUDE_MD
    assert "sender_relationship" in DEFAULT_SHARED_CLAUDE_MD


class _Payload:
    def __init__(self, channel_id: str, sender_slug: str):
        self.channel_id = channel_id
        self.sender_slug = sender_slug
        self.envelope_id = "env_test"


def test_blocked_dm_is_dropped_before_store():
    c = _client(_responses(own={}, owner={"eve-1": "blocked"}))
    assert _run(c._drop_blocked_dm(_Payload("", "eve-1"))) is True


def test_blocked_group_message_is_not_dropped():
    c = _client(_responses(own={}, owner={"eve-1": "blocked"}))
    assert _run(c._drop_blocked_dm(_Payload("ch_123", "eve-1"))) is False


def test_unblocked_dm_is_not_dropped():
    c = _client(_responses(own={}, owner={}))
    assert _run(c._drop_blocked_dm(_Payload("", "friendly-1"))) is False


def test_drop_check_runs_before_persistence():
    import re
    from pathlib import Path

    src = Path("src/puffo_agent/agent/puffo_core_client.py").read_text(
        encoding="utf-8"
    )
    drop = src.index("await self._drop_blocked_dm(payload)")
    store = src.index("await self.store.store({")
    assert drop < store, "blocked-DM drop must precede messages.db persistence"
