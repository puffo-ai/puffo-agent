"""CloudMetadataClient — thin metadata HTTP client.

Spins an aiohttp TestServer exposing the 4 read-only routes and
asserts: each method GETs the right path + parses JSON, the
``x-sandbox-token`` header is sent ONLY when a token is configured
(explicit arg or ``PUFFO_SANDBOX_TOKEN``), and a >=400 surfaces as
CloudMetadataError. No signing path exists to test — that's the point.
"""

from __future__ import annotations

import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent_cloud.cloud_http import CloudMetadataClient, CloudMetadataError


class _MetaApp:
    def __init__(self) -> None:
        # path -> the x-sandbox-token header seen on that request
        self.tokens_seen: dict[str, str | None] = {}

    def _record(self, request: web.Request) -> None:
        self.tokens_seen[request.path] = request.headers.get("x-sandbox-token")

    async def spaces(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response([{"space_id": "sp_1", "name": "Eng"}])

    async def channels(self, request: web.Request) -> web.Response:
        self._record(request)
        sid = request.match_info["space_id"]
        return web.json_response([{"channel_id": "ch_1", "space_id": sid}])

    async def members(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response([{"slug": "alice"}, {"slug": "bob"}])

    async def profiles(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response({"profiles": [{"slug": "alice"}]})

    async def boom(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response({"error": "nope"}, status=403)


def _build_app(app_obj: _MetaApp) -> web.Application:
    app = web.Application()
    app.router.add_get("/spaces", app_obj.spaces)
    app.router.add_get("/spaces/{space_id}/channels", app_obj.channels)
    app.router.add_get("/spaces/{space_id}/members", app_obj.members)
    app.router.add_get("/identities/profiles", app_obj.profiles)
    app.router.add_get("/boom", app_obj.boom)
    return app


@pytest.mark.asyncio
async def test_all_four_routes_parse_json_and_send_token():
    meta = _MetaApp()
    async with TestClient(TestServer(_build_app(meta))) as client:
        base = str(client.make_url("")).rstrip("/")
        c = CloudMetadataClient(base, token="sbx_meta_123")
        try:
            spaces = await c.list_spaces()
            channels = await c.list_channels("sp_1")
            members = await c.list_members("sp_1")
            profiles = await c.list_profiles()
        finally:
            await c.close()

    assert spaces == [{"space_id": "sp_1", "name": "Eng"}]
    assert channels == [{"channel_id": "ch_1", "space_id": "sp_1"}]
    assert members == [{"slug": "alice"}, {"slug": "bob"}]
    assert profiles == {"profiles": [{"slug": "alice"}]}
    # Token sent on every route.
    assert set(meta.tokens_seen.values()) == {"sbx_meta_123"}
    assert "/spaces/sp_1/channels" in meta.tokens_seen
    assert "/spaces/sp_1/members" in meta.tokens_seen
    assert "/identities/profiles" in meta.tokens_seen


@pytest.mark.asyncio
async def test_no_token_means_no_header(monkeypatch):
    monkeypatch.delenv("PUFFO_SANDBOX_TOKEN", raising=False)
    meta = _MetaApp()
    async with TestClient(TestServer(_build_app(meta))) as client:
        base = str(client.make_url("")).rstrip("/")
        c = CloudMetadataClient(base)  # no token, no env
        try:
            await c.list_spaces()
        finally:
            await c.close()
    assert meta.tokens_seen["/spaces"] is None


@pytest.mark.asyncio
async def test_env_token_used_when_no_arg(monkeypatch):
    monkeypatch.setenv("PUFFO_SANDBOX_TOKEN", "sbx_from_env")
    meta = _MetaApp()
    async with TestClient(TestServer(_build_app(meta))) as client:
        base = str(client.make_url("")).rstrip("/")
        c = CloudMetadataClient(base)
        try:
            await c.list_spaces()
        finally:
            await c.close()
    assert meta.tokens_seen["/spaces"] == "sbx_from_env"


@pytest.mark.asyncio
async def test_explicit_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("PUFFO_SANDBOX_TOKEN", "sbx_from_env")
    meta = _MetaApp()
    async with TestClient(TestServer(_build_app(meta))) as client:
        base = str(client.make_url("")).rstrip("/")
        c = CloudMetadataClient(base, token="sbx_explicit")
        try:
            await c.list_spaces()
        finally:
            await c.close()
    assert meta.tokens_seen["/spaces"] == "sbx_explicit"


@pytest.mark.asyncio
async def test_http_error_raises_metadata_error():
    meta = _MetaApp()
    async with TestClient(TestServer(_build_app(meta))) as client:
        base = str(client.make_url("")).rstrip("/")
        c = CloudMetadataClient(base, token="t")
        try:
            with pytest.raises(CloudMetadataError) as exc:
                await c._get("/boom")
        finally:
            await c.close()
    assert exc.value.status == 403
