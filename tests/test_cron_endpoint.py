"""PUF-239: operator-side cron endpoints — list + disable + auth +
404 paths."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _bridge_support import (
    isolated_home, make_user, pair_request_body, signed_headers,
    write_test_agent,
)
from puffo_agent.portal.api.server import build_app
from puffo_agent.portal.cron_state import (
    CronSchedule, upsert_cron,
)
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


async def _pair(client, user):
    body = pair_request_body(user)
    h = signed_headers(user, "POST", "/v1/pair", body); h.update(_HOST)
    r = await client.post("/v1/pair", data=body, headers=h)
    assert r.status == 200, await r.text()


def _seed_cron(agent_id: str, cron_id: str, **overrides):
    base = dict(
        id=cron_id,
        schedule="0 9 * * *",
        prompt="report status",
        enabled=True,
        created_at=1716372000000,
        last_fire=None,
        fire_count=0,
    )
    base.update(overrides)
    upsert_cron(agent_id, CronSchedule(**base))


# ────────────────────────────────────────────────────────────────────
# GET /v1/agents/{id}/crons
# ────────────────────────────────────────────────────────────────────


async def test_list_crons_empty(client):
    user = make_user()
    await _pair(client, user)
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-empty")

    h = signed_headers(user, "GET", "/v1/agents/agt-empty/crons"); h.update(_HOST)
    r = await client.get("/v1/agents/agt-empty/crons", headers=h)
    assert r.status == 200
    j = await r.json()
    assert j["agent_id"] == "agt-empty"
    assert j["crons"] == []


async def test_list_crons_returns_registered_rows(client):
    user = make_user()
    await _pair(client, user)
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-list")
    _seed_cron("agt-list", "cron_one", schedule="0 9 * * *", prompt="hi")
    _seed_cron("agt-list", "cron_two", schedule="*/5 * * * *", prompt="ping")

    h = signed_headers(user, "GET", "/v1/agents/agt-list/crons"); h.update(_HOST)
    r = await client.get("/v1/agents/agt-list/crons", headers=h)
    j = await r.json()
    ids = {c["id"] for c in j["crons"]}
    assert ids == {"cron_one", "cron_two"}


async def test_list_crons_unknown_agent_returns_404(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "GET", "/v1/agents/agt-nope/crons"); h.update(_HOST)
    r = await client.get("/v1/agents/agt-nope/crons", headers=h)
    assert r.status == 404


async def test_list_crons_unpaired_returns_401(client):
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-noauth")
    r = await client.get(
        "/v1/agents/agt-noauth/crons", headers={**_HOST},
    )
    assert r.status == 401


# ────────────────────────────────────────────────────────────────────
# DELETE /v1/agents/{id}/crons/{cron_id}
# ────────────────────────────────────────────────────────────────────


async def test_disable_cron_flips_enabled_false_and_returns_row(client):
    user = make_user()
    await _pair(client, user)
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-disable")
    _seed_cron("agt-disable", "cron_active", enabled=True)

    h = signed_headers(
        user, "DELETE", "/v1/agents/agt-disable/crons/cron_active",
    ); h.update(_HOST)
    r = await client.delete(
        "/v1/agents/agt-disable/crons/cron_active", headers=h,
    )
    assert r.status == 200
    j = await r.json()
    assert j["cron"]["id"] == "cron_active"
    assert j["cron"]["enabled"] is False


async def test_disable_cron_unknown_id_returns_404(client):
    user = make_user()
    await _pair(client, user)
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-404")

    h = signed_headers(
        user, "DELETE", "/v1/agents/agt-404/crons/cron_missing",
    ); h.update(_HOST)
    r = await client.delete(
        "/v1/agents/agt-404/crons/cron_missing", headers=h,
    )
    assert r.status == 404


async def test_disable_cron_re_delete_still_returns_404_after_first(client):
    # Idempotency: a second DELETE on the same id surfaces 404 so
    # the operator sees the audit signal rather than silently
    # "succeeding" against a now-missing row.
    user = make_user()
    await _pair(client, user)
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-redelete")
    _seed_cron("agt-redelete", "cron_x", enabled=True)

    # First DELETE — succeeds + flips enabled flag. Use a fresh
    # signed header per request (nonces are single-use).
    h1 = signed_headers(
        user, "DELETE", "/v1/agents/agt-redelete/crons/cron_x",
    ); h1.update(_HOST)
    r1 = await client.delete(
        "/v1/agents/agt-redelete/crons/cron_x", headers=h1,
    )
    assert r1.status == 200

    # Second DELETE on the SAME id — the row still exists (we don't
    # remove rows on disable). ``disable_cron`` is idempotent at the
    # state level; the endpoint returns 200 again (NOT 404). Re-
    # disable on an already-disabled row is a no-op flip, not a
    # missing row.
    h2 = signed_headers(
        user, "DELETE", "/v1/agents/agt-redelete/crons/cron_x",
    ); h2.update(_HOST)
    r2 = await client.delete(
        "/v1/agents/agt-redelete/crons/cron_x", headers=h2,
    )
    assert r2.status == 200


async def test_disable_cron_unpaired_returns_401(client):
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-noauth-del")
    _seed_cron("agt-noauth-del", "cron_x")
    r = await client.delete(
        "/v1/agents/agt-noauth-del/crons/cron_x", headers={**_HOST},
    )
    assert r.status == 401
