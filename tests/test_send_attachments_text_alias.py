"""``send_message_with_attachments``: ``text`` aliases ``caption``.

Pydantic drops unknown args, so a habitual ``text=`` used to vanish while
the send still reported ``sent``.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from puffo_agent.mcp import core_message_tools


def _build_mcp(monkeypatch, captured):
    from mcp.server.fastmcp import FastMCP

    async def _capture(cfg, request, tool_name="send_message"):
        captured.append(request)
        return {"state": "sent", "attempted": True, "seq": 1}

    monkeypatch.setattr(
        core_message_tools, "_dispatch_semantic_send", _capture
    )
    mcp = FastMCP("test")
    core_message_tools.register_message_tools(mcp, cfg=object())
    return mcp


async def _call(mcp, args):
    result = await mcp.call_tool("send_message_with_attachments", args)
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list):
        return "".join(getattr(item, "text", str(item)) for item in result)
    return str(result)


@pytest.mark.asyncio
async def test_text_reaches_the_caption(monkeypatch):
    """The production trap: ``text=`` reaches the caption."""
    captured = []
    mcp = _build_mcp(monkeypatch, captured)
    await _call(
        mcp,
        {"paths": ["a.md"], "channel": "ch_test", "text": "the lost body"},
    )
    assert len(captured) == 1
    assert captured[0].caption == "the lost body"


@pytest.mark.asyncio
async def test_caption_alone_is_unchanged(monkeypatch):
    captured = []
    mcp = _build_mcp(monkeypatch, captured)
    await _call(
        mcp,
        {"paths": ["a.md"], "channel": "ch_test", "caption": "as before"},
    )
    assert captured[0].caption == "as before"


@pytest.mark.asyncio
async def test_identical_caption_and_text_are_accepted(monkeypatch):
    captured = []
    mcp = _build_mcp(monkeypatch, captured)
    await _call(
        mcp,
        {
            "paths": ["a.md"],
            "channel": "ch_test",
            "caption": "same body",
            "text": "same body",
        },
    )
    assert captured[0].caption == "same body"


@pytest.mark.asyncio
async def test_conflicting_caption_and_text_error_loudly(monkeypatch):
    """Two different bodies never silently pick one."""
    captured = []
    mcp = _build_mcp(monkeypatch, captured)
    with pytest.raises(ToolError, match="alias"):
        await mcp.call_tool(
            "send_message_with_attachments",
            {
                "paths": ["a.md"],
                "channel": "ch_test",
                "caption": "one body",
                "text": "another body",
            },
        )
    assert captured == []


@pytest.mark.asyncio
async def test_empty_both_sends_empty_caption(monkeypatch):
    """Files-only sends (no body at all) keep working."""
    captured = []
    mcp = _build_mcp(monkeypatch, captured)
    await _call(mcp, {"paths": ["a.md"], "channel": "ch_test"})
    assert captured[0].caption == ""
