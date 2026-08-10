"""``update_dm_blocklist`` speaks the real ``/blocklists`` contract.

Unblock is the half that had no working path: the tool called
``delete(..., body=...)`` against a ``delete()`` that took no body, so
every unblock raised ``TypeError`` before reaching the server. These
cases pin the wire shape the server actually implements —
``DELETE /blocklists`` with ``{"id": ...}``, 204 removed / 404 already
gone — and the ordering rule that local contact state only moves once
the server has agreed, so a failed call can never leave the agent
believing it unblocked someone it did not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from puffo_agent.crypto.http_client import HttpError
from puffo_agent.mcp.core_dm_preference_tools import register_dm_preference_tools

TARGET = "mallory-0009"


class _FakeHttp:
    def __init__(self, *, delete_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.keyless = False
        self._delete_error = delete_error

    async def get(self, path, *a, **k):
        self.calls.append(("GET", path, None))
        return {"entries": [], "blocks": []}

    async def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        return {"ok": True}

    async def delete(self, path, body=None):
        self.calls.append(("DELETE", path, body))
        if self._delete_error is not None:
            raise self._delete_error
        return None


class _FakeContacts:
    def __init__(self) -> None:
        self.blocked: set[str] = {TARGET}

    def note_allowed(self, slug):  # pragma: no cover - unused here
        pass

    def note_blocked(self, slug, blocked):
        if blocked:
            self.blocked.add(slug)
        else:
            self.blocked.discard(slug)


def _tools(http):
    contacts = _FakeContacts()
    cfg = SimpleNamespace(
        http_client=http,
        keyless=http.keyless,
        message_client=SimpleNamespace(_contacts=contacts),
    )
    mcp = FastMCP("test")
    register_dm_preference_tools(mcp, cfg)
    return mcp, contacts


async def _call(mcp, args):
    result = await mcp.call_tool("update_dm_blocklist", args)
    if isinstance(result, tuple):
        result = result[0]
    return "".join(getattr(item, "text", str(item)) for item in result)


@pytest.mark.asyncio
async def test_unblock_deletes_by_body_id_and_clears_local_state():
    http = _FakeHttp()
    mcp, contacts = _tools(http)

    text = await _call(mcp, {"slug": TARGET, "on": False})

    assert http.calls == [("DELETE", "/blocklists", {"id": TARGET})]
    assert TARGET not in contacts.blocked
    assert "unblocked" in text


@pytest.mark.asyncio
async def test_unblock_treats_404_as_already_unblocked():
    http = _FakeHttp(delete_error=HttpError(404, '{"error":"not found"}'))
    mcp, contacts = _tools(http)

    text = await _call(mcp, {"slug": TARGET, "on": False})

    # The desired end state holds, so local state is accurate to settle.
    assert TARGET not in contacts.blocked
    assert "was not blocked" in text


@pytest.mark.asyncio
async def test_unblock_failure_leaves_local_block_intact():
    http = _FakeHttp(delete_error=HttpError(500, "boom"))
    mcp, contacts = _tools(http)

    with pytest.raises(Exception):
        await _call(mcp, {"slug": TARGET, "on": False})

    # Still blocked everywhere — the agent must not act unblocked while
    # the server still drops that sender's DMs.
    assert TARGET in contacts.blocked


@pytest.mark.asyncio
async def test_keyless_agent_gets_an_explicit_unsupported_error():
    http = _FakeHttp()
    http.keyless = True
    mcp, contacts = _tools(http)

    with pytest.raises(Exception, match="keyless"):
        await _call(mcp, {"slug": TARGET, "on": True})

    assert http.calls == []
    assert TARGET in contacts.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, args",
    [
        ("get_dm_allowlists", {}),
        ("get_dm_blocklists", {}),
        ("add_dm_allowlist", {"slug": TARGET}),
        ("update_dm_blocklist", {"slug": TARGET, "on": True}),
    ],
)
async def test_every_dm_preference_tool_names_the_keyless_gap(tool, args):
    """All four tools fail the same explicit way, not just the one.

    ``/allowlists`` and ``/blocklists`` are subkey-signed server-side with
    no keyless counterpart, so none of these can work on a keyless agent.
    Only ``update_dm_blocklist`` said so; the other three sent the request
    anyway and surfaced whatever opaque auth error came back — the same
    dead end, described as a transport failure the operator could plausibly
    read as transient.
    """
    http = _FakeHttp()
    http.keyless = True
    mcp, _ = _tools(http)

    with pytest.raises(Exception, match="keyless"):
        await mcp.call_tool(tool, args)

    assert http.calls == []
