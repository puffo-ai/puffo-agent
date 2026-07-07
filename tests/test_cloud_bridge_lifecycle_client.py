"""``CloudBridgeClient`` keyless lifecycle REST methods vs. a loopback
aiohttp mock app (mirrors ``test_cloud_bridge_client.py``). Exercises the
``x-sandbox-token`` HTTP surface — POST/GET/DELETE on
``/v2/cloud-agents/{schedule-wake,scheduled-wake,runtime-status,keepalive}``
— asserting the client sends the right route/body/header and maps the
response. Loopback only, no real network / E2B / server.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent.agent.bridge_client import BridgeError, CloudBridgeClient


class _MockLifecycleApp:
    """Records requests to the lifecycle routes and returns canned
    bodies/status the test configures."""

    def __init__(self) -> None:
        self.token_seen: str | None = None
        self.requests: list[dict] = []
        # Per-route canned responses: (status, json_body). Defaults
        # exercise the happy path; tests override to drive edge cases.
        self.schedule_wake_response: tuple[int, dict] = (
            200, {"wake_at": "2026-07-07T12:00:00Z", "reason": "ok"},
        )
        self.scheduled_wake_response: tuple[int, dict | None] = (
            200, {"wake_at": None},
        )
        self.cancel_wake_status = 204
        self.runtime_status_response: tuple[int, dict] = (
            200, {
                "state": "running",
                "timeout_at": "2026-07-07T13:00:00Z",
                "seconds_until_sleep": 900,
                "sandbox_id": "sbx_1",
            },
        )
        self.keepalive_response: tuple[int, dict] = (
            200, {
                "timeout_at": "2026-07-07T14:00:00Z",
                "seconds_until_sleep": 3600,
            },
        )

    async def _record(self, request: web.Request) -> None:
        self.token_seen = request.headers.get("x-sandbox-token")
        body = None
        if request.can_read_body:
            try:
                body = await request.json()
            except Exception:
                body = None
        self.requests.append({
            "method": request.method,
            "path": request.path,
            "token": request.headers.get("x-sandbox-token"),
            "body": body,
        })

    async def schedule_wake(self, request: web.Request) -> web.Response:
        await self._record(request)
        status, body = self.schedule_wake_response
        return web.json_response(body, status=status)

    async def scheduled_wake_get(self, request: web.Request) -> web.Response:
        await self._record(request)
        status, body = self.scheduled_wake_response
        return web.json_response(body, status=status)

    async def scheduled_wake_delete(self, request: web.Request) -> web.Response:
        await self._record(request)
        if self.cancel_wake_status == 204:
            return web.Response(status=204)
        return web.json_response({"cancelled": True}, status=self.cancel_wake_status)

    async def runtime_status(self, request: web.Request) -> web.Response:
        await self._record(request)
        status, body = self.runtime_status_response
        return web.json_response(body, status=status)

    async def keepalive(self, request: web.Request) -> web.Response:
        await self._record(request)
        status, body = self.keepalive_response
        return web.json_response(body, status=status)


def _build_app(app: _MockLifecycleApp) -> web.Application:
    a = web.Application()
    a.router.add_post("/v2/cloud-agents/schedule-wake", app.schedule_wake)
    a.router.add_get("/v2/cloud-agents/scheduled-wake", app.scheduled_wake_get)
    a.router.add_delete(
        "/v2/cloud-agents/scheduled-wake", app.scheduled_wake_delete,
    )
    a.router.add_get("/v2/cloud-agents/runtime-status", app.runtime_status)
    a.router.add_post("/v2/cloud-agents/keepalive", app.keepalive)
    return a


@pytest.mark.asyncio
async def test_schedule_wake_after_seconds_posts_body_and_token():
    mock = _MockLifecycleApp()
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        result = await c.schedule_wake(after_seconds=1800, reason="long task")
    assert result == {"wake_at": "2026-07-07T12:00:00Z", "reason": "ok"}
    req = mock.requests[-1]
    assert req["method"] == "POST"
    assert req["path"] == "/v2/cloud-agents/schedule-wake"
    assert req["token"] == "sbx_tok"
    assert req["body"] == {"after_seconds": 1800, "reason": "long task"}


@pytest.mark.asyncio
async def test_schedule_wake_wake_at_posts_body():
    mock = _MockLifecycleApp()
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        await c.schedule_wake(wake_at="2026-07-07T15:30:00Z")
    req = mock.requests[-1]
    assert req["body"] == {"wake_at": "2026-07-07T15:30:00Z"}
    assert "after_seconds" not in req["body"]


@pytest.mark.asyncio
async def test_schedule_wake_non_2xx_raises_bridge_error():
    mock = _MockLifecycleApp()
    mock.schedule_wake_response = (409, {"error": "already scheduled"})
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        with pytest.raises(BridgeError) as excinfo:
            await c.schedule_wake(after_seconds=60)
    assert excinfo.value.code == "SCHEDULE_WAKE"


@pytest.mark.asyncio
async def test_get_scheduled_wake_null_when_none():
    mock = _MockLifecycleApp()
    mock.scheduled_wake_response = (200, {"wake_at": None})
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        result = await c.get_scheduled_wake()
    assert result == {"wake_at": None}
    req = mock.requests[-1]
    assert req["method"] == "GET"
    assert req["path"] == "/v2/cloud-agents/scheduled-wake"
    assert req["token"] == "sbx_tok"


@pytest.mark.asyncio
async def test_get_scheduled_wake_returns_fields_when_set():
    mock = _MockLifecycleApp()
    mock.scheduled_wake_response = (
        200, {"wake_at": "2026-07-07T12:00:00Z", "reason": "batch"},
    )
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        result = await c.get_scheduled_wake()
    assert result["wake_at"] == "2026-07-07T12:00:00Z"
    assert result["reason"] == "batch"


@pytest.mark.asyncio
async def test_cancel_wake_issues_delete():
    mock = _MockLifecycleApp()
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        result = await c.cancel_wake()
    # 204 empty body → {}
    assert result == {}
    req = mock.requests[-1]
    assert req["method"] == "DELETE"
    assert req["path"] == "/v2/cloud-agents/scheduled-wake"
    assert req["token"] == "sbx_tok"


@pytest.mark.asyncio
async def test_runtime_status_returns_fields_incl_null_seconds():
    mock = _MockLifecycleApp()
    mock.runtime_status_response = (
        200, {
            "state": "running",
            "timeout_at": "2026-07-07T13:00:00Z",
            "seconds_until_sleep": None,
            "sandbox_id": "sbx_9",
        },
    )
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        result = await c.runtime_status()
    assert result["state"] == "running"
    assert result["sandbox_id"] == "sbx_9"
    # null seconds_until_sleep is preserved verbatim (not fabricated).
    assert result["seconds_until_sleep"] is None
    req = mock.requests[-1]
    assert req["method"] == "GET"
    assert req["path"] == "/v2/cloud-agents/runtime-status"
    assert req["token"] == "sbx_tok"


@pytest.mark.asyncio
async def test_keepalive_available_returns_deadline():
    mock = _MockLifecycleApp()
    mock.keepalive_response = (
        200, {"timeout_at": "2026-07-07T14:00:00Z", "seconds_until_sleep": 3600},
    )
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        result = await c.keepalive(600)
    assert result["available"] is True
    assert result["timeout_at"] == "2026-07-07T14:00:00Z"
    assert result["seconds_until_sleep"] == 3600
    req = mock.requests[-1]
    assert req["method"] == "POST"
    assert req["path"] == "/v2/cloud-agents/keepalive"
    assert req["body"] == {"seconds": 600}
    assert req["token"] == "sbx_tok"


@pytest.mark.asyncio
async def test_keepalive_unavailable_flags_available_false():
    mock = _MockLifecycleApp()
    # Server signals the AIM deadline-refresh isn't landed yet.
    mock.keepalive_response = (501, {"error": "keepalive not implemented yet"})
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        result = await c.keepalive(600)
    assert result["available"] is False
    assert "keepalive not implemented yet" in result["detail"]


@pytest.mark.asyncio
async def test_keepalive_unavailable_via_200_available_false():
    mock = _MockLifecycleApp()
    # Alternate contract: 200 with an explicit available:false body.
    mock.keepalive_response = (200, {"available": False, "detail": "not ready"})
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        result = await c.keepalive(600)
    assert result["available"] is False
    assert "not ready" in result["detail"]


@pytest.mark.asyncio
async def test_keepalive_other_non_2xx_raises_bridge_error():
    mock = _MockLifecycleApp()
    mock.keepalive_response = (500, {"error": "boom"})
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_tok", "slug")
        with pytest.raises(BridgeError) as excinfo:
            await c.keepalive(600)
    assert excinfo.value.code == "KEEPALIVE"
