"""CORS / Private Network Access preflight tests for the bridge.

Browsers gate HTTPS-to-loopback calls on (a) the daemon answering the
PNA preflight with ``Access-Control-Allow-Private-Network: true`` and
(b) a matching ``Access-Control-Allow-Origin``. These tests pin both.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _bridge_support import isolated_home
from puffo_agent.portal.api.server import build_app
from puffo_agent.portal.state import DaemonConfig

pytestmark = pytest.mark.asyncio

_HOST = {"Host": "127.0.0.1:63387"}


@pytest_asyncio.fixture
async def client():
    isolated_home()
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


async def test_preflight_with_allowed_origin_returns_pna_header(client):
    h = {
        "Origin": "https://chat.puffo.ai",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-puffo-signature, content-type",
        **_HOST,
    }
    r = await client.options("/v1/pair", headers=h)
    assert r.status == 204
    assert r.headers["Access-Control-Allow-Origin"] == "https://chat.puffo.ai"
    assert r.headers["Access-Control-Allow-Private-Network"] == "true"
    assert "POST" in r.headers["Access-Control-Allow-Methods"]
    assert "x-puffo-signature" in r.headers["Access-Control-Allow-Headers"].lower()


async def test_preflight_with_disallowed_origin_omits_allow_origin(client):
    h = {
        "Origin": "https://evil.com",
        "Access-Control-Request-Method": "GET",
        **_HOST,
    }
    r = await client.options("/v1/info", headers=h)
    assert r.status == 204
    # No Allow-Origin -> browser drops the actual request.
    assert "Access-Control-Allow-Origin" not in r.headers
    assert "Access-Control-Allow-Private-Network" not in r.headers


async def test_actual_response_carries_allow_origin_for_allowed_origin(client):
    h = {"Origin": "https://chat.puffo.ai", **_HOST}
    r = await client.get("/v1/info", headers=h)
    assert r.status == 200
    assert r.headers["Access-Control-Allow-Origin"] == "https://chat.puffo.ai"
    assert "Origin" in r.headers["Vary"]


async def test_actual_response_omits_allow_origin_for_disallowed(client):
    h = {"Origin": "https://evil.com", **_HOST}
    r = await client.get("/v1/info", headers=h)
    assert r.status == 200
    assert "Access-Control-Allow-Origin" not in r.headers


async def test_localhost_dev_origin_allowed(client):
    h = {"Origin": "http://localhost:5173", **_HOST}
    r = await client.get("/v1/info", headers=h)
    assert r.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
