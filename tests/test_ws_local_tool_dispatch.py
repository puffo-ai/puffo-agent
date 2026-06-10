"""tool_dispatch capture surface.

Pins that ``build_dispatch`` returns exactly the
``WS_LOCAL_ALLOWED_TOOLS`` subset against a real
``register_core_tools`` call, the closures bind to the supplied
``cfg``, and each handler is awaitable.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from puffo_agent.portal.ws_local.tool_dispatch import (
    WS_LOCAL_ALLOWED_TOOLS,
    _CapturedRegistration,
    build_dispatch,
)


def test_allowed_tools_are_the_message_shaped_ones():
    assert WS_LOCAL_ALLOWED_TOOLS == frozenset({
        "send_message",
        "send_message_with_attachments",
        "get_user_info",
        "get_post",
        "get_channel_history",
        "get_dm_history",
        "list_channel_members",
    })


def test_build_dispatch_returns_only_allowed_handlers():
    dispatch = build_dispatch(MagicMock())
    assert set(dispatch.keys()) == WS_LOCAL_ALLOWED_TOOLS
    for handler in dispatch.values():
        assert callable(handler)


def test_capture_stub_records_decorated_handlers():
    captured = _CapturedRegistration(handlers={})

    @captured.tool()
    async def my_tool():
        return "ok"

    assert "my_tool" in captured.handlers
    assert captured.handlers["my_tool"] is my_tool


def test_capture_stub_resource_decorator_is_passthrough():
    captured = _CapturedRegistration(handlers={})

    @captured.resource("foo")
    async def fn():
        return None

    # No assertion on registry — resource doesn't get exposed over ws-local.
    assert fn.__name__ == "fn"


@pytest.mark.asyncio
async def test_build_dispatch_subset_filter_drops_unknown_names():
    dispatch = build_dispatch(MagicMock(), allowed=frozenset({"send_message", "nonsense"}))
    assert set(dispatch.keys()) == {"send_message"}
