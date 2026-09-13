"""tool_dispatch capture surface.

Pins that ``build_dispatch`` returns exactly the
``WS_LOCAL_ALLOWED_TOOLS`` subset against a real
``register_core_tools`` call, the closures bind to the supplied
``cfg``, and each handler is awaitable.
"""

from __future__ import annotations

import inspect
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
        "mark_covered",
        # read / navigation
        "read_inbox",
        "read_history",
        # durable Agent-local reminders
        "create_reminder",
        "list_reminders",
        "cancel_reminder",
        "replace_reminder",
        "get_user_info",
        "whoami",
        "get_post",
        "get_post_segment",
        "get_channel_notes",
        "get_thread_notes",
        "add_note",
        "list_channel_members",
        "list_spaces",
        "list_channels_in_space",
        "list_channels_in_all_spaces",
        "get_dm_allowlists",
        "get_dm_blocklists",
        # membership
        "leave_space",
        "leave_channel",
        # bridge-only sandbox lifecycle
        "schedule_wake",
        "cancel_wake",
        "get_scheduled_wake",
        "get_runtime_status",
        "keep_alive",
    })


def test_harness_and_host_tools_excluded():
    """Harness/host/identity ops must NOT be reachable over ws-local."""
    for t in (
        "refresh", "install_skill", "list_skills",
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


def test_ws_local_semantic_tool_signatures_expose_no_internal_controls():
    dispatch = build_dispatch(MagicMock())
    expected = {
        "read_inbox": {"target", "cursor", "limit"},
        "read_history": {
            "target", "cursor", "before_message_id", "after_message_id",
            "limit",
        },
        "create_reminder": {"content", "target", "intended_at", "covers"},
        "list_reminders": {"state", "limit"},
        "cancel_reminder": {"reminder_id"},
        "replace_reminder": {"reminder_id", "content", "target", "intended_at"},
        "send_message": {
            "channel", "text", "root_id", "visibility_level", "send_anyway",
            "covers",
        },
        "send_message_with_attachments": {
            "paths", "channel", "caption", "root_id",
            "visibility_level", "send_anyway", "covers",
        },
        "mark_covered": {"covers", "by_message_id", "note"},
    }
    forbidden = {
        "freshness", "freshness_mode", "mode", "context_baseline_seq",
        "seen_seq", "synchronized", "transport", "provider_session_id",
        "session_ref", "turn_id", "turn_ref", "sequence", "seq",
        "through_seq", "latest_seq", "latest_envelope_id", "held_pair",
        "client_ref", "admission_receipt", "correlation_receipt",
        "tool_name", "tool_arguments",
    }
    for name, property_set in expected.items():
        parameters = set(inspect.signature(dispatch[name]).parameters)
        assert parameters == property_set
        assert parameters.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_ws_local_read_inbox_uses_live_runtime():
    from puffo_agent.mcp.puffo_core_tools import PuffoCoreToolsConfig

    calls = []

    class Runtime:
        async def read_inbox(self, **kwargs):
            calls.append(kwargs)
            return {
                "messages": ["whole"],
                "next_cursor": "",
                "has_more": False,
                "remaining_count": 0,
                "snapshot_generation": 3,
            }

    cfg = PuffoCoreToolsConfig(
        slug="agent-1",
        device_id="dev-1",
        keystore=MagicMock(),
        http_client=MagicMock(),
        data_client=MagicMock(),
        inbox_runtime=Runtime(),
    )
    dispatch = build_dispatch(cfg)
    result = await dispatch["read_inbox"](
        target="channel:sp:ch",
        cursor="",
        limit=7,
    )
    assert '[window context_version=1 kind="inbox"' in result
    assert "[pending_messages context_version=1 message_count=1]" in result
    assert "whole" in result
    assert calls == [{
        "target": "channel:sp:ch",
        "cursor": "",
        "limit": 7,
        "tool_arguments": {"target": "channel:sp:ch", "limit": 7},
    }]


@pytest.mark.asyncio
async def test_ws_local_reminder_tools_use_the_live_runtime():
    from puffo_agent.mcp.puffo_core_tools import PuffoCoreToolsConfig

    calls = []

    class Runtime:
        async def create_reminder(self, **kwargs):
            calls.append(("create", kwargs))
            return {"reminder_id": "r", "occurrence_id": "o", "state": "scheduled"}

        async def list_reminders(self, **kwargs):
            calls.append(("list", kwargs))
            return {"reminders": []}

        async def cancel_reminder(self, **kwargs):
            calls.append(("cancel", kwargs))
            return {"reminder_id": "r", "occurrence_id": "o", "state": "cancelled"}

        async def replace_reminder(self, **kwargs):
            calls.append(("replace", kwargs))
            return {"cancelled": {"reminder_id": "r"}, "replacement": {"reminder_id": "r2"}}

    cfg = PuffoCoreToolsConfig(
        slug="agent-1", device_id="dev-1", keystore=MagicMock(),
        http_client=MagicMock(), data_client=MagicMock(), inbox_runtime=Runtime(),
    )
    dispatch = build_dispatch(cfg)
    assert await dispatch["create_reminder"](
        content="x", target="channel:sp:ch", intended_at="2026-08-02T12:00:00Z"
    ) == {"reminder_id": "r", "occurrence_id": "o", "state": "scheduled"}
    assert await dispatch["list_reminders"](state="scheduled", limit=7) == {"reminders": []}
    assert await dispatch["cancel_reminder"](reminder_id="r") == {
        "reminder_id": "r", "occurrence_id": "o", "state": "cancelled",
    }
    assert await dispatch["replace_reminder"](
        reminder_id="r", content="new",
    ) == {"cancelled": {"reminder_id": "r"}, "replacement": {"reminder_id": "r2"}}
    assert calls == [
        ("create", {
            "content": "x", "target": "channel:sp:ch",
            "intended_at": "2026-08-02T12:00:00Z", "covers": None,
        }),
        ("list", {"state": "scheduled", "limit": 7}),
        ("cancel", {"reminder_id": "r"}),
        ("replace", {
            "reminder_id": "r", "content": "new", "target": "", "intended_at": "",
        }),
    ]


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
