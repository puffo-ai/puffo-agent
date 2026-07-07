"""Bridge-only lifecycle MCP tools: mapping / fallback / gating logic.

Handlers are obtained through
``portal.ws_local.tool_dispatch.build_dispatch`` — that both proves the
allowlist wiring and exercises the ``register_core_tools`` gate on
``cfg.bridge_client``. Most cases use a fake ``bridge_client`` for the
mapping/fallback/gating logic; the two ``*_posts_right_body_*`` /
``*_hit_right_routes`` cases drive a **real** ``CloudBridgeClient``
against a loopback mock so we pin the actual ``x-sandbox-token`` HTTP.
LLM-free, E2B-free, server-free.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent.agent.bridge_client import BridgeError, CloudBridgeClient
from puffo_agent.mcp.puffo_core_tools import PuffoCoreToolsConfig
from puffo_agent.portal.ws_local.tool_dispatch import build_dispatch


LIFECYCLE_TOOLS = frozenset({
    "schedule_wake",
    "cancel_wake",
    "get_scheduled_wake",
    "get_runtime_status",
    "keep_alive",
})


def _cfg(bridge) -> PuffoCoreToolsConfig:
    """Minimal tools config; only ``bridge_client`` matters here — the
    lifecycle tools never touch keystore/http/data."""
    return PuffoCoreToolsConfig(
        slug="agent-1",
        device_id="dev-1",
        keystore=MagicMock(),
        http_client=MagicMock(),
        data_client=MagicMock(),
        bridge_client=bridge,
    )


class _FakeBridge:
    """Records lifecycle-method calls and returns canned results.
    Set ``raise_on`` to a method name to make it raise ``BridgeError``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.raise_on: str | None = None
        self.schedule_wake_result: dict = {"wake_at": "2026-07-07T12:00:00Z"}
        self.get_scheduled_wake_result: dict = {"wake_at": None}
        self.cancel_wake_result: dict = {}
        self.runtime_status_result: dict = {
            "state": "running",
            "timeout_at": "2026-07-07T13:00:00Z",
            "seconds_until_sleep": 900,
            "sandbox_id": "sbx_1",
        }
        self.keepalive_result: dict = {
            "available": True,
            "timeout_at": "2026-07-07T14:00:00Z",
            "seconds_until_sleep": 3600,
        }

    def _maybe_raise(self, name: str) -> None:
        if self.raise_on == name:
            raise BridgeError("KEEPALIVE", "simulated transport failure")

    async def schedule_wake(self, *, after_seconds=None, wake_at=None, reason=""):
        self.calls.append((
            "schedule_wake",
            {"after_seconds": after_seconds, "wake_at": wake_at, "reason": reason},
        ))
        self._maybe_raise("schedule_wake")
        return self.schedule_wake_result

    async def get_scheduled_wake(self):
        self.calls.append(("get_scheduled_wake", {}))
        self._maybe_raise("get_scheduled_wake")
        return self.get_scheduled_wake_result

    async def cancel_wake(self):
        self.calls.append(("cancel_wake", {}))
        self._maybe_raise("cancel_wake")
        return self.cancel_wake_result

    async def runtime_status(self):
        self.calls.append(("runtime_status", {}))
        self._maybe_raise("runtime_status")
        return self.runtime_status_result

    async def keepalive(self, seconds):
        self.calls.append(("keepalive", {"seconds": seconds}))
        self._maybe_raise("keepalive")
        return self.keepalive_result


# --------------------------------------------------------------------------
# Loopback mock app for the real-CloudBridgeClient end-to-end cases
# --------------------------------------------------------------------------


class _MockApp:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def _record(self, request: web.Request) -> None:
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
        return web.json_response(
            {"wake_at": "2026-07-07T12:00:00Z", "reason": "batch job"},
        )

    async def scheduled_wake_get(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"wake_at": None})

    async def scheduled_wake_delete(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.Response(status=204)


def _build_app(mock: _MockApp) -> web.Application:
    a = web.Application()
    a.router.add_post("/v2/cloud-agents/schedule-wake", mock.schedule_wake)
    a.router.add_get("/v2/cloud-agents/scheduled-wake", mock.scheduled_wake_get)
    a.router.add_delete(
        "/v2/cloud-agents/scheduled-wake", mock.scheduled_wake_delete,
    )
    return a


# --------------------------------------------------------------------------
# (verify a) real-client end-to-end: right route/body/token
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_wake_tool_posts_right_body_with_token():
    mock = _MockApp()
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        bridge = CloudBridgeClient(url, "sbx_e2e", "slug")
        dispatch = build_dispatch(_cfg(bridge))
        assert "schedule_wake" in dispatch
        result = await dispatch["schedule_wake"](
            after_seconds=1800, reason="batch job",
        )
    req = mock.requests[-1]
    assert req["method"] == "POST"
    assert req["path"] == "/v2/cloud-agents/schedule-wake"
    assert req["token"] == "sbx_e2e"
    assert req["body"] == {"after_seconds": 1800, "reason": "batch job"}
    # Tool surfaces the confirmed wake_at from the server.
    assert "2026-07-07T12:00:00Z" in result


@pytest.mark.asyncio
async def test_cancel_and_get_scheduled_wake_hit_right_routes():
    mock = _MockApp()
    async with TestClient(TestServer(_build_app(mock))) as client:
        url = str(client.make_url("")).rstrip("/")
        bridge = CloudBridgeClient(url, "sbx_e2e", "slug")
        dispatch = build_dispatch(_cfg(bridge))
        await dispatch["cancel_wake"]()
        delete_req = mock.requests[-1]
        result = await dispatch["get_scheduled_wake"]()
        get_req = mock.requests[-1]
    assert delete_req["method"] == "DELETE"
    assert delete_req["path"] == "/v2/cloud-agents/scheduled-wake"
    assert get_req["method"] == "GET"
    assert get_req["path"] == "/v2/cloud-agents/scheduled-wake"
    assert get_req["token"] == "sbx_e2e"
    # No wake scheduled → readable "none" summary.
    assert "no wake" in result.lower()


# --------------------------------------------------------------------------
# (verify b) runtime-status null → "unknown"
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_runtime_status_maps_null_seconds_to_unknown():
    bridge = _FakeBridge()
    bridge.runtime_status_result = {
        "state": "running",
        "timeout_at": "2026-07-07T13:00:00Z",
        "seconds_until_sleep": None,
        "sandbox_id": "sbx_1",
    }
    dispatch = build_dispatch(_cfg(bridge))
    result = await dispatch["get_runtime_status"]()
    assert "unknown" in result
    # Never fabricate a number for an unknown deadline.
    assert "seconds_until_sleep=unknown" in result


@pytest.mark.asyncio
async def test_get_runtime_status_renders_real_seconds():
    bridge = _FakeBridge()  # default seconds_until_sleep=900
    dispatch = build_dispatch(_cfg(bridge))
    result = await dispatch["get_runtime_status"]()
    assert "900" in result
    assert "unknown" not in result


# --------------------------------------------------------------------------
# (verify c) keep_alive available vs. fallback
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_alive_returns_refreshed_deadline():
    bridge = _FakeBridge()
    bridge.keepalive_result = {
        "available": True,
        "timeout_at": "2026-07-07T14:00:00Z",
        "seconds_until_sleep": 3600,
    }
    dispatch = build_dispatch(_cfg(bridge))
    result = await dispatch["keep_alive"](600)
    assert "2026-07-07T14:00:00Z" in result
    assert ("keepalive", {"seconds": 600}) in bridge.calls
    # No fallback schedule_wake when keepalive succeeded.
    assert all(name != "schedule_wake" for name, _ in bridge.calls)


@pytest.mark.asyncio
async def test_keep_alive_falls_back_to_schedule_wake_when_unavailable():
    bridge = _FakeBridge()
    bridge.keepalive_result = {"available": False, "detail": "not landed yet"}
    bridge.schedule_wake_result = {"wake_at": "2026-07-07T12:10:00Z"}
    dispatch = build_dispatch(_cfg(bridge))
    result = await dispatch["keep_alive"](600)
    # keepalive was tried, then schedule_wake fallback fired with ~600s.
    sched = [args for name, args in bridge.calls if name == "schedule_wake"]
    assert len(sched) == 1
    assert sched[0]["after_seconds"] == 600
    # Message explains the fallback and self-resume.
    lowered = result.lower()
    assert "wake" in lowered
    assert "fallback" in lowered or "not available" in lowered
    assert "2026-07-07T12:10:00Z" in result


# --------------------------------------------------------------------------
# (verify d) registration gating on bridge transport
# --------------------------------------------------------------------------


def test_lifecycle_tools_not_registered_under_native_transport():
    dispatch = build_dispatch(_cfg(None))  # native: bridge_client is None
    for name in LIFECYCLE_TOOLS:
        assert name not in dispatch


def test_lifecycle_tools_registered_under_bridge_transport():
    dispatch = build_dispatch(_cfg(_FakeBridge()))
    for name in LIFECYCLE_TOOLS:
        assert name in dispatch


# --------------------------------------------------------------------------
# (verify e) transport error is a fail-soft string, not a crash
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_error_returned_as_tool_error_not_crash():
    bridge = _FakeBridge()
    bridge.raise_on = "schedule_wake"
    dispatch = build_dispatch(_cfg(bridge))
    # Must NOT propagate — the tool returns a string the model can read.
    result = await dispatch["schedule_wake"](after_seconds=60)
    assert isinstance(result, str)
    assert "schedule_wake failed" in result
    assert "simulated transport failure" in result


@pytest.mark.asyncio
async def test_keep_alive_transport_error_is_fail_soft():
    bridge = _FakeBridge()
    bridge.raise_on = "keepalive"
    dispatch = build_dispatch(_cfg(bridge))
    result = await dispatch["keep_alive"](600)
    assert isinstance(result, str)
    assert "keep_alive failed" in result


# --------------------------------------------------------------------------
# input validation (fail-soft strings, no exceptions)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_wake_rejects_both_after_and_wake_at():
    bridge = _FakeBridge()
    dispatch = build_dispatch(_cfg(bridge))
    result = await dispatch["schedule_wake"](
        after_seconds=60, wake_at="2026-07-07T12:00:00Z",
    )
    assert "exactly one" in result
    # Nothing was sent to the bridge.
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_schedule_wake_rejects_neither():
    bridge = _FakeBridge()
    dispatch = build_dispatch(_cfg(bridge))
    result = await dispatch["schedule_wake"]()
    assert "one of after_seconds" in result
    assert bridge.calls == []
