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


def test_allowed_tools_are_the_send_read_and_membership_tools():
    assert WS_LOCAL_ALLOWED_TOOLS == frozenset({
        # send
        "send_message",
        "send_message_with_attachments",
        # read / navigation
        "get_user_info",
        "whoami",
        "get_post",
        "get_post_segment",
        "get_channel_history",
        "get_dm_history",
        "get_thread_history",
        "list_channel_members",
        "list_spaces",
        "list_channels_in_space",
        "list_channels_in_all_spaces",
        # membership
        "leave_space",
        "leave_channel",
    })


def test_harness_and_host_tools_excluded():
    """Harness/host/identity ops must NOT be reachable over ws-local."""
    for t in (
        "refresh", "reload_system_prompt", "install_skill", "list_skills",
        "install_mcp_server", "list_mcp_servers", "install_host_mcp",
        "sync_host_mcp",
    ):
        assert t not in WS_LOCAL_ALLOWED_TOOLS


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


@pytest.mark.asyncio
async def test_ws_local_leave_space_drives_client_in_process():
    """ws-local has no rpc_client; leave_space must call the message
    client's request_leave_approval directly."""
    from puffo_agent.mcp.puffo_core_tools import PuffoCoreToolsConfig

    calls: list[tuple] = []

    class _Client:
        async def request_leave_approval(self, *, kind, space_id, channel_id, reason):
            calls.append((kind, space_id, channel_id, reason))
            return "asked your operator"

    cfg = PuffoCoreToolsConfig(
        slug="agent-1", device_id="dev-1", keystore=MagicMock(),
        http_client=MagicMock(), data_client=MagicMock(),
        message_client=_Client(),
    )
    dispatch = build_dispatch(cfg)
    assert "leave_space" in dispatch
    result = await dispatch["leave_space"]("sp_1", "too noisy")
    assert result == "asked your operator"
    assert calls == [("leave_space", "sp_1", "", "too noisy")]


@pytest.mark.asyncio
async def test_ws_local_leave_channel_resolves_space_and_drives_client():
    from puffo_agent.mcp.puffo_core_tools import PuffoCoreToolsConfig

    calls: list[tuple] = []

    class _Client:
        async def request_leave_approval(self, *, kind, space_id, channel_id, reason):
            calls.append((kind, space_id, channel_id, reason))
            return "asked your operator"

    class _Data:
        async def lookup_channel_space(self, channel_id):
            return "sp_1"

    cfg = PuffoCoreToolsConfig(
        slug="agent-1", device_id="dev-1", keystore=MagicMock(),
        http_client=MagicMock(), data_client=_Data(),
        message_client=_Client(),
    )
    dispatch = build_dispatch(cfg)
    result = await dispatch["leave_channel"]("ch_1", "leaving")
    assert result == "asked your operator"
    assert calls == [("leave_channel", "sp_1", "ch_1", "leaving")]
