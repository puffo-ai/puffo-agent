"""Tests for ``portal.rpc_service`` — the dispatch layer between
the MCP-side ``PuffoRpcClient`` and the daemon-side
``host_mcp_handler`` functions."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from aiohttp.test_utils import TestClient as AiohttpTestClient, TestServer

from puffo_agent.agent.message_store_models import LifecycleConflict
from puffo_agent.mcp._host_mcp import PuffoRpcClient
from puffo_agent.portal import rpc_service
from puffo_agent.portal.host_mcp_handler import HostMcpContext
from puffo_agent.portal.local_service_auth import (
    issue_local_service_token,
    local_service_headers,
)


class TestClient(AiohttpTestClient):
    """Authenticate test requests for the Agent id selected by the path."""

    async def _request(self, method, path, **kwargs):
        parts = urlsplit(str(path)).path.split("/")
        agent_id = unquote(parts[3])
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault(
            "Authorization",
            local_service_headers(issue_local_service_token(agent_id))[
                "Authorization"
            ],
        )
        return await super()._request(method, path, headers=headers, **kwargs)


def _stub_ctx(agent_id: str = "agent_test") -> HostMcpContext:
    return HostMcpContext(
        agent_id=agent_id,
        slug="bot-test",
        operator_slug="op-test",
        host_home=Any,   # handlers are stubbed in these tests
        agent_home=Any,
        harness="claude-code",
        keystore=None,
        http_client=None,
    )


@pytest.fixture
def app_client_factory():
    """Async fixtures cause pytest_asyncio warnings here; hand the
    test a builder that returns an entered async client. Each test
    manages teardown via the cleanup function the factory returns."""
    created: list = []
    async def _make():
        cfg = rpc_service.RpcServiceConfig(enabled=True, port=0)
        app = rpc_service.build_app(cfg)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        created.append(client)
        return client
    yield _make
    # Sync teardown — clients close themselves when their event loop
    # tears down, but clearing the global resolver is non-async.
    rpc_service.set_rpc_resolver(None)


@pytest.mark.asyncio
async def test_rpc_service_rejects_request_without_agent_token():
    """An arbitrary local process cannot invoke one Agent's host RPC."""
    resolver_calls: list[str] = []
    rpc_service.set_rpc_resolver(lambda agent_id: resolver_calls.append(agent_id))
    app = rpc_service.build_app(rpc_service.RpcServiceConfig(enabled=True, port=0))
    async with AiohttpTestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/rpc/agent_a/send-message",
            json={"channel": "ch_a", "text": "x"},
        )
    assert response.status == 401
    assert resolver_calls == []


@pytest.mark.asyncio
async def test_install_503_when_resolver_unset(app_client_factory):
    rpc_service.set_rpc_resolver(None)
    app_client = await app_client_factory()
    resp = await app_client.post(
        "/v1/rpc/agent_test/install-mcp",
        json={"name": "x", "spec": {"type": "stdio", "command": "node"}},
    )
    assert resp.status == 503
    body = await resp.json()
    assert "rpc resolver not wired" in body["error"]


@pytest.mark.asyncio
async def test_install_404_when_resolver_returns_none(app_client_factory):
    rpc_service.set_rpc_resolver(lambda _aid: None)
    app_client = await app_client_factory()
    resp = await app_client.post(
        "/v1/rpc/missing/install-mcp",
        json={"name": "x", "spec": {"type": "stdio", "command": "node"}},
    )
    assert resp.status == 404
    body = await resp.json()
    assert "missing" in body["error"]


@pytest.mark.asyncio
async def test_install_dispatches_to_handler(app_client_factory, monkeypatch):
    app_client = await app_client_factory()
    """Happy path: resolver returns a ctx, handler returns a
    message, route packages it as {"message": ...}."""
    captured: dict[str, Any] = {}
    async def _stub_install(ctx, *, name, template_id, spec):
        captured.update(
            ctx=ctx, name=name, template_id=template_id, spec=spec,
        )
        return "installed!"
    monkeypatch.setattr(
        rpc_service.host_mcp_handler, "install", _stub_install,
    )
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))

    resp = await app_client.post(
        "/v1/rpc/agent_a/install-mcp",
        json={"name": "x", "template_id": "gmail-read"},
    )

    assert resp.status == 200
    body = await resp.json()
    assert body == {"message": "installed!"}
    assert captured["name"] == "x"
    assert captured["template_id"] == "gmail-read"
    # template_id was provided but spec was omitted in the body — the
    # route normalises missing fields to None for spec.
    assert captured["spec"] is None
    assert captured["ctx"].agent_id == "agent_a"


@pytest.mark.asyncio
async def test_install_runtimerror_surfaces_as_400(app_client_factory, monkeypatch):
    app_client = await app_client_factory()
    async def _stub_install(ctx, *, name, template_id, spec):
        raise RuntimeError("install_host_mcp: name is required")
    monkeypatch.setattr(
        rpc_service.host_mcp_handler, "install", _stub_install,
    )
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))

    resp = await app_client.post(
        "/v1/rpc/agent_a/install-mcp",
        json={"name": ""},
    )

    assert resp.status == 400
    body = await resp.json()
    assert "name is required" in body["error"]


@pytest.mark.asyncio
async def test_install_unexpected_exception_500(app_client_factory, monkeypatch):
    app_client = await app_client_factory()
    async def _stub_install(ctx, *, name, template_id, spec):
        raise ValueError("boom")
    monkeypatch.setattr(
        rpc_service.host_mcp_handler, "install", _stub_install,
    )
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))

    resp = await app_client.post(
        "/v1/rpc/agent_a/install-mcp",
        json={"name": "x", "spec": {"type": "stdio", "command": "n"}},
    )

    assert resp.status == 500
    body = await resp.json()
    assert "boom" in body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "freshness", "freshness_mode", "mode", "context_baseline_seq",
    "seen_seq", "synchronized", "transport", "provider_session_id",
    "session_ref", "turn_id", "turn_ref", "sequence", "seq",
    "through_seq", "latest_seq", "latest_envelope_id", "held_pair",
    "client_ref", "admission_receipt", "correlation_receipt",
    "tool_name", "tool_arguments", "unexpected",
])
async def test_send_message_hidden_or_unknown_rejected_before_resolver(
    app_client_factory, field,
):
    resolver_calls = []
    rpc_service.set_rpc_resolver(lambda aid: resolver_calls.append(aid))
    client = await app_client_factory()
    response = await client.post(
        "/v1/rpc/agent_a/send-message",
        json={"channel": "ch_a", "text": "x", field: "forbidden"},
    )
    assert response.status == 400
    assert resolver_calls == []


@pytest.mark.asyncio
async def test_send_message_structured_round_trip(app_client_factory):
    class Coordinator:
        def __init__(self):
            self.calls = []

        async def send(self, request):
            self.calls.append(request)
            return {"state": "held", "attempted": True, "latest_seq": 9}

    coordinator = Coordinator()
    ctx = _stub_ctx()
    ctx.send_coordinator = coordinator
    rpc_service.set_rpc_resolver(lambda _aid: ctx)
    client = await app_client_factory()
    response = await client.post(
        "/v1/rpc/agent_a/send-message",
        json={"channel": "ch_a", "text": "x", "send_anyway": True},
    )
    assert response.status == 200
    assert await response.json() == {
        "state": "held", "attempted": True, "latest_seq": 9,
    }
    assert coordinator.calls[0].destination == "ch_a"
    assert coordinator.calls[0].send_anyway is True


@pytest.mark.asyncio
async def test_send_message_unavailable_is_structured(app_client_factory):
    rpc_service.set_rpc_resolver(lambda _aid: _stub_ctx())
    client = await app_client_factory()
    response = await client.post(
        "/v1/rpc/agent_a/send-message",
        json={"channel": "ch_a", "text": "x"},
    )
    assert response.status == 200
    body = await response.json()
    assert body["state"] == "failed"
    assert body["attempted"] is True


@pytest.mark.asyncio
async def test_read_inbox_strict_three_field_round_trip(
    app_client_factory, monkeypatch,
):
    captured: dict[str, Any] = {}

    async def _stub_read(ctx, **kwargs):
        captured.update(ctx=ctx, **kwargs)
        return {
            "messages": ["whole message"],
            "next_cursor": "opaque",
            "has_more": True,
            "remaining_count": 61,
            "snapshot_generation": 4,
            "correlation_receipt": "receipt",
        }

    monkeypatch.setattr(rpc_service.host_mcp_handler, "read_inbox", _stub_read)
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))
    client = await app_client_factory()
    response = await client.post(
        "/v1/rpc/agent_a/read-inbox",
        json={
            "target": "channel:sp_1:ch_1",
            "cursor": "cursor",
            "limit": 17,
        },
    )
    assert response.status == 200
    assert await response.json() == {
        "messages": ["whole message"],
        "next_cursor": "opaque",
        "has_more": True,
        "remaining_count": 61,
        "snapshot_generation": 4,
        "correlation_receipt": "receipt",
    }
    assert captured == {
        "ctx": captured["ctx"],
        "target": "channel:sp_1:ch_1",
        "cursor": "cursor",
        "limit": 17,
    }
    assert captured["ctx"].agent_id == "agent_a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        *[
            {field: "forbidden"}
            for field in (
                "freshness", "freshness_mode", "mode",
                "context_baseline_seq", "seen_seq", "synchronized",
                "transport", "provider_session_id", "session_ref",
                "turn_id", "turn_ref", "sequence", "seq", "through_seq",
                "latest_seq", "latest_envelope_id", "held_pair",
                "client_ref", "admission_receipt", "correlation_receipt",
                "tool_name", "tool_arguments",
            )
        ],
        {"target": "", "cursor": "", "limit": 51},
        {"target": [], "cursor": "", "limit": 1},
    ],
)
async def test_read_inbox_rejects_hidden_unknown_or_invalid_fields_before_resolver(
    app_client_factory, body,
):
    resolver_calls = []
    rpc_service.set_rpc_resolver(lambda aid: resolver_calls.append(aid))
    client = await app_client_factory()
    response = await client.post(
        "/v1/rpc/agent_a/read-inbox",
        json=body,
    )
    assert response.status == 400
    assert resolver_calls == []


@pytest.mark.asyncio
async def test_reminder_routes_are_strict_and_return_structured_objects(
    app_client_factory, monkeypatch,
):
    captured: list[tuple[str, dict[str, object]]] = []
    reminder = {
        "reminder_id": "reminder-1",
        "occurrence_id": "occurrence-1",
        "state": "scheduled",
        "target": "channel:sp:ch",
        "content": "exact content",
        "intended_at": "2026-08-02T12:00:00.000Z",
        "actual_fire_at": None,
        "created_at": "2026-08-02T11:00:00.000Z",
        "cancelled_at": None,
        "delivered_at": None,
    }

    async def create(_ctx, **kwargs):
        captured.append(("create", kwargs))
        return reminder

    async def list_(_ctx, **kwargs):
        captured.append(("list", kwargs))
        return {"reminders": [reminder]}

    async def cancel(_ctx, **kwargs):
        captured.append(("cancel", kwargs))
        return {**reminder, "state": "cancelled"}

    monkeypatch.setattr(rpc_service.host_mcp_handler, "create_reminder", create)
    monkeypatch.setattr(rpc_service.host_mcp_handler, "list_reminders", list_)
    monkeypatch.setattr(rpc_service.host_mcp_handler, "cancel_reminder", cancel)
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))
    client = await app_client_factory()

    response = await client.post(
        "/v1/rpc/agent_a/create-reminder",
        json={
            "content": "exact content", "target": "channel:sp:ch",
            "intended_at": "2026-08-02T14:00:00+02:00",
        },
    )
    assert response.status == 200 and await response.json() == reminder
    response = await client.post(
        "/v1/rpc/agent_a/list-reminders",
        json={"state": "scheduled", "limit": 3},
    )
    assert response.status == 200 and await response.json() == {"reminders": [reminder]}
    response = await client.post(
        "/v1/rpc/agent_a/cancel-reminder",
        json={"reminder_id": "reminder-1"},
    )
    assert response.status == 200 and (await response.json())["state"] == "cancelled"
    assert captured == [
        ("create", {
            "content": "exact content", "target": "channel:sp:ch",
            "intended_at": "2026-08-02T12:00:00.000Z",
        }),
        ("list", {"state": "scheduled", "limit": 3}),
        ("cancel", {"reminder_id": "reminder-1"}),
    ]


@pytest.mark.asyncio
async def test_cancel_reminder_maps_lifecycle_conflict_to_http_409(
    app_client_factory, monkeypatch,
):
    async def cancel(_ctx, **_kwargs):
        raise LifecycleConflict("reminder cancellation conflicts with delivery claim")

    monkeypatch.setattr(rpc_service.host_mcp_handler, "cancel_reminder", cancel)
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))
    client = await app_client_factory()

    response = await client.post(
        "/v1/rpc/agent_a/cancel-reminder",
        json={"reminder_id": "reminder-1"},
    )
    assert response.status == 409
    assert await response.json() == {
        "error": "reminder cancellation conflicts with delivery claim",
    }


@pytest.mark.asyncio
async def test_puffo_rpc_client_round_trips_all_reminder_objects(
    app_client_factory, monkeypatch,
):
    """Exercise the real loopback client used by a subprocess MCP server."""
    captured: list[tuple[str, dict[str, object]]] = []
    scheduled = {
        "reminder_id": "reminder-1",
        "occurrence_id": "occurrence-1",
        "state": "scheduled",
        "target": "channel:sp:ch",
        "content": "exact content",
        "intended_at": "2026-08-02T12:00:00.000Z",
        "actual_fire_at": None,
        "created_at": "2026-08-02T11:00:00.000Z",
        "cancelled_at": None,
        "delivered_at": None,
    }
    cancelled = {
        **scheduled,
        "state": "cancelled",
        "cancelled_at": "2026-08-02T11:01:00.000Z",
    }

    async def create(_ctx, **kwargs):
        captured.append(("create", kwargs))
        return scheduled

    async def list_(_ctx, **kwargs):
        captured.append(("list", kwargs))
        return {"reminders": [scheduled]}

    async def cancel(_ctx, **kwargs):
        captured.append(("cancel", kwargs))
        return cancelled

    monkeypatch.setattr(rpc_service.host_mcp_handler, "create_reminder", create)
    monkeypatch.setattr(rpc_service.host_mcp_handler, "list_reminders", list_)
    monkeypatch.setattr(rpc_service.host_mcp_handler, "cancel_reminder", cancel)
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))
    app_client = await app_client_factory()
    client = PuffoRpcClient(
        str(app_client.make_url("")).rstrip("/"),
        "agent_a",
        issue_local_service_token("agent_a"),
    )
    try:
        assert await client.create_reminder(
            content="exact content",
            target="channel:sp:ch",
            intended_at="2026-08-02T14:00:00+02:00",
        ) == scheduled
        assert await client.list_reminders(state="scheduled", limit=3) == {
            "reminders": [scheduled],
        }
        assert await client.cancel_reminder(reminder_id="reminder-1") == cancelled
    finally:
        await client.close()
    assert captured == [
        ("create", {
            "content": "exact content", "target": "channel:sp:ch",
            "intended_at": "2026-08-02T12:00:00.000Z",
        }),
        ("list", {"state": "scheduled", "limit": 3}),
        ("cancel", {"reminder_id": "reminder-1"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("create-reminder", {
            "content": "x", "target": "channel:sp:ch",
            "intended_at": "2026-08-02T12:00:00",  # no explicit offset
        }),
        ("create-reminder", {
            "content": "x", "target": "channel:sp:ch",
            "intended_at": "2026-08-02 12:00:00Z",  # not RFC3339
        }),
        ("create-reminder", {
            "content": "x", "target": "not-a-target",
            "intended_at": "2026-08-02T12:00:00Z",
        }),
        ("create-reminder", {
            "content": "x", "target": "channel:sp:ch",
            "intended_at": "2026-08-02T12:00:00Z", "provider": "no",
        }),
        ("list-reminders", {"state": "recurring"}),
        ("list-reminders", {"limit": 0}),
        ("list-reminders", {"state": "", "cloud": True}),
        ("cancel-reminder", {"reminder_id": ""}),
        ("cancel-reminder", {"reminder_id": "one", "occurrence_id": "two"}),
    ],
)
async def test_reminder_routes_reject_invalid_or_hidden_fields_before_resolver(
    app_client_factory, route, body,
):
    resolver_calls = []
    rpc_service.set_rpc_resolver(lambda aid: resolver_calls.append(aid))
    client = await app_client_factory()
    response = await client.post(f"/v1/rpc/agent_a/{route}", json=body)
    assert response.status == 400
    assert resolver_calls == []


@pytest.mark.asyncio
async def test_model_visible_read_structured_round_trip(
    app_client_factory, monkeypatch,
):
    captured: dict[str, Any] = {}

    async def _stub_stage(ctx, **kwargs):
        captured.update(ctx=ctx, **kwargs)
        return {
            "state": "staged",
            "correlation_key": "visible_read_1",
            "through_seq": kwargs["through_seq"],
        }

    monkeypatch.setattr(
        rpc_service.host_mcp_handler,
        "stage_model_visible_read",
        _stub_stage,
    )
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))
    client = await app_client_factory()
    body = {
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "through_seq": 9,
        "through_envelope_id": "msg_9",
        "tool_name": "get_channel_history",
        "tool_arguments": {"channel": "ch_1"},
        "visible_message_ids": ["msg_8", "msg_9"],
    }
    response = await client.post(
        "/v1/rpc/agent_a/model-visible-read",
        json=body,
    )

    assert response.status == 200
    assert await response.json() == {
        "state": "staged",
        "correlation_key": "visible_read_1",
        "through_seq": 9,
    }
    assert captured["ctx"].agent_id == "agent_a"
    assert captured["tool_arguments"] == {"channel": "ch_1"}
    assert captured["visible_message_ids"] == ["msg_8", "msg_9"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("through_seq", True),
        ("through_seq", -1),
        ("tool_name", "get_dm_history"),
        ("tool_arguments", {"channel": ["ch_1"]}),
        ("visible_message_ids", "msg_9"),
        ("visible_message_ids", ["msg_9", ""]),
        ("visible_message_ids", ["msg_9", "msg_9"]),
        ("visible_message_ids", [f"msg_{index}" for index in range(201)]),
        ("unexpected", "field"),
    ],
)
async def test_model_visible_read_rejects_invalid_body_before_resolver(
    app_client_factory, field, value,
):
    resolver_calls = []
    rpc_service.set_rpc_resolver(lambda aid: resolver_calls.append(aid))
    client = await app_client_factory()
    body = {
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "through_seq": 9,
        "through_envelope_id": "msg_9",
        "tool_name": "get_channel_history",
        "tool_arguments": {"channel": "ch_1"},
    }
    body[field] = value
    response = await client.post(
        "/v1/rpc/agent_a/model-visible-read",
        json=body,
    )
    assert response.status == 400
    assert resolver_calls == []


@pytest.mark.asyncio
async def test_install_rejects_non_json_body(app_client_factory):
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))
    app_client = await app_client_factory()
    resp = await app_client.post(
        "/v1/rpc/agent_a/install-mcp",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_sync_dispatches_to_handler(app_client_factory, monkeypatch):
    app_client = await app_client_factory()
    captured: dict[str, Any] = {}
    async def _stub_sync(ctx, *, template_id):
        captured.update(ctx=ctx, template_id=template_id)
        return "synced!"
    monkeypatch.setattr(
        rpc_service.host_mcp_handler, "sync", _stub_sync,
    )
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))

    resp = await app_client.post(
        "/v1/rpc/agent_a/sync-mcp",
        json={"template_id": "gmail-read"},
    )

    assert resp.status == 200
    body = await resp.json()
    assert body == {"message": "synced!"}
    assert captured["template_id"] == "gmail-read"
