"""PATCH /v1/agents/{id}/runtime — field contract and writer normalization.

``allowed_tools`` / ``max_turns`` are part of the endpoint's contract; a
payload carrying them must apply and echo them rather than return 200 with
the fields silently dropped.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _bridge_support import (
    isolated_home, make_user, pair_request_body, signed_headers,
    write_test_agent,
)
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.portal.api.runtime_patch import runtime_response
from puffo_agent.portal.api.server import build_app
from puffo_agent.portal.state import AgentConfig, DaemonConfig

pytestmark = pytest.mark.asyncio

_HOST = {"Host": "127.0.0.1:63387"}


@pytest_asyncio.fixture
async def owner_client():
    """Paired client owning ``owned-bot``, so runtime PATCH is authorized."""
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home, "owned-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    app = build_app(DaemonConfig().bridge)
    async with TestClient(TestServer(app)) as c:
        body = pair_request_body(user)
        h = signed_headers(user, "POST", "/v1/pair", body); h.update(_HOST)
        assert (await c.post("/v1/pair", data=body, headers=h)).status == 200
        yield c, user


async def _patch(client, user, payload: dict):
    path = "/v1/agents/owned-bot/runtime"
    body = json.dumps(payload).encode("utf-8")
    h = signed_headers(user, "PATCH", path, body); h.update(_HOST)
    return await client.patch(path, data=body, headers=h)


@pytest.mark.parametrize(("payload", "status", "expected"), [
    # Supplied fields are applied, persisted, and echoed — never dropped at 200.
    ({"allowed_tools": ["Read"], "max_turns": 5}, 200, (["Read"], 5)),
    # Base contract coerces element types and integer-like max_turns.
    ({"allowed_tools": ["Bash(git status)", 7], "max_turns": "9"}, 200,
     (["Bash(git status)", "7"], 9)),
    # Wrong types take the error path, not a silent 200.
    ({"allowed_tools": "Read"}, 400, "allowed_tools must be a list of strings"),
    ({"max_turns": "x"}, 400, "max_turns must be an integer"),
])
async def test_patch_worker_limit_contract(owner_client, payload, status, expected):
    client, user = owner_client
    before = AgentConfig.load("owned-bot").runtime
    baseline = (list(before.allowed_tools), before.max_turns)

    r = await _patch(client, user, payload)
    assert r.status == status, await r.text()
    saved = AgentConfig.load("owned-bot").runtime

    if status == 400:
        assert expected in await r.text()
        assert (list(saved.allowed_tools), saved.max_turns) == baseline
        return

    tools, turns = expected
    runtime = (await r.json())["runtime"]
    assert (runtime["allowed_tools"], runtime["max_turns"]) == (tools, turns)
    assert (list(saved.allowed_tools), saved.max_turns) == (tools, turns)
    assert runtime_response(saved)["allowed_tools"] == tools
