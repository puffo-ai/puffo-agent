"""Tests for the operator-initiated lifecycle endpoints on the
local bridge: pause / resume / archive. Mirrors the existing
delete-agent path (also covered upstream), so the tests follow the
same shape — paired user, ``write_test_agent`` for the fixture
agent, owner-check parity with the rest of the write endpoints.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from aiohttp.test_utils import TestClient, TestServer

from _bridge_support import (
    isolated_home, make_user, pair_request_body, signed_headers,
    write_test_agent,
)
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.portal.api.server import build_app
from puffo_agent.portal.state import (
    DaemonConfig, agent_yml_path, archive_flag_path,
)

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


def _read_state(agent_id: str) -> str:
    data = yaml.safe_load(agent_yml_path(agent_id).read_text(encoding="utf-8"))
    return data["state"]


async def _build_app_with_owned_agent(agent_id: str, user) -> tuple:
    """Materialise a fresh home + ``agent_id`` owned by ``user`` and
    return a (TestClient, ...) tuple ready for signed requests.

    Mirrors the per-test setup other handler tests do — each scenario
    needs its own home so cross-test state can't leak through the
    shared ``isolated_home()`` env vars."""
    home = isolated_home()
    user_root_pk = base64url_encode(user.root_key.public_key_bytes())
    write_test_agent(home, agent_id, owner_root_pubkey=user_root_pk)
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    return TestClient(server), home


async def _build_app_with_other_agent(agent_id: str) -> tuple:
    home = isolated_home()
    other_root_pk = base64url_encode(b"\x99" * 32)
    write_test_agent(home, agent_id, owner_root_pubkey=other_root_pk)
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    return TestClient(server), home


# ────────────────────────────────────────────────────────────────────
# pause
# ────────────────────────────────────────────────────────────────────


async def test_pause_flips_state_to_paused():
    user = make_user()
    c, _ = await _build_app_with_owned_agent("p-bot", user)
    async with c:
        await _pair(c, user)
        h = signed_headers(user, "POST", "/v1/agents/p-bot/pause", b""); h.update(_HOST)
        r = await c.post("/v1/agents/p-bot/pause", headers=h)
        j = await r.json()
    assert r.status == 200, j
    assert j == {"agent_id": "p-bot", "state": "paused", "ok": True,
                 "note": "daemon will apply the state change on the next "
                         "reconcile tick (~2s)"}
    assert _read_state("p-bot") == "paused"


async def test_pause_idempotent_when_already_paused():
    """Second pause returns 200 with ``note: already paused`` and
    doesn't rewrite agent.yml's mtime unnecessarily."""
    user = make_user()
    c, _ = await _build_app_with_owned_agent("p-bot", user)
    async with c:
        await _pair(c, user)
        h = signed_headers(user, "POST", "/v1/agents/p-bot/pause", b""); h.update(_HOST)
        await c.post("/v1/agents/p-bot/pause", headers=h)
        # Second call.
        h2 = signed_headers(user, "POST", "/v1/agents/p-bot/pause", b""); h2.update(_HOST)
        r = await c.post("/v1/agents/p-bot/pause", headers=h2)
        j = await r.json()
    assert r.status == 200
    assert j["state"] == "paused"
    assert j["note"] == "already paused"


async def test_pause_non_owner_403():
    user = make_user()
    c, _ = await _build_app_with_other_agent("p-bot")
    async with c:
        await _pair(c, user)
        h = signed_headers(user, "POST", "/v1/agents/p-bot/pause", b""); h.update(_HOST)
        r = await c.post("/v1/agents/p-bot/pause", headers=h)
        j = await r.json()
    assert r.status == 403
    assert "only the agent's operator" in j["error"]
    # State stays untouched.
    assert _read_state("p-bot") == "running"


async def test_pause_unknown_id_404(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "POST", "/v1/agents/nope/pause", b""); h.update(_HOST)
    r = await client.post("/v1/agents/nope/pause", headers=h)
    assert r.status == 404


# ────────────────────────────────────────────────────────────────────
# resume
# ────────────────────────────────────────────────────────────────────


async def test_resume_flips_state_back_to_running():
    user = make_user()
    c, _ = await _build_app_with_owned_agent("r-bot", user)
    async with c:
        await _pair(c, user)
        # Pause first so resume has work to do.
        h = signed_headers(user, "POST", "/v1/agents/r-bot/pause", b""); h.update(_HOST)
        await c.post("/v1/agents/r-bot/pause", headers=h)
        assert _read_state("r-bot") == "paused"
        # Resume.
        h2 = signed_headers(user, "POST", "/v1/agents/r-bot/resume", b""); h2.update(_HOST)
        r = await c.post("/v1/agents/r-bot/resume", headers=h2)
        j = await r.json()
    assert r.status == 200
    assert j["state"] == "running"
    assert _read_state("r-bot") == "running"


async def test_resume_idempotent_when_already_running():
    user = make_user()
    c, _ = await _build_app_with_owned_agent("r-bot", user)
    async with c:
        await _pair(c, user)
        h = signed_headers(user, "POST", "/v1/agents/r-bot/resume", b""); h.update(_HOST)
        r = await c.post("/v1/agents/r-bot/resume", headers=h)
        j = await r.json()
    assert r.status == 200
    assert j["state"] == "running"
    assert j["note"] == "already running"


async def test_resume_non_owner_403():
    user = make_user()
    c, _ = await _build_app_with_other_agent("r-bot")
    async with c:
        await _pair(c, user)
        h = signed_headers(user, "POST", "/v1/agents/r-bot/resume", b""); h.update(_HOST)
        r = await c.post("/v1/agents/r-bot/resume", headers=h)
    assert r.status == 403


# ────────────────────────────────────────────────────────────────────
# archive
# ────────────────────────────────────────────────────────────────────


async def test_archive_pauses_and_writes_flag():
    """Archive on a running agent flips state -> paused AND drops
    ``archive.flag`` for the reconciler to pick up on its next tick
    (move dir to ``~/.puffo-agent/archived/``)."""
    user = make_user()
    c, _ = await _build_app_with_owned_agent("a-bot", user)
    async with c:
        await _pair(c, user)
        h = signed_headers(user, "POST", "/v1/agents/a-bot/archive", b""); h.update(_HOST)
        r = await c.post("/v1/agents/a-bot/archive", headers=h)
        j = await r.json()
    assert r.status == 200, j
    assert j["ok"] is True
    # State should now read "paused" so the reconciler's worker-stop
    # branch (state=paused) runs before the archive-move branch.
    assert _read_state("a-bot") == "paused"
    # The flag's the actual hand-off to the daemon loop — verifying
    # its presence is the API-level guarantee the web client relies
    # on (refreshAgents will then see the dir disappear).
    assert archive_flag_path("a-bot").exists()


async def test_archive_writes_flag_even_when_already_paused():
    """If the operator paused the agent earlier through a separate
    call, archive still drops the flag so the reconciler moves the
    dir — the pause-first dance is just defensive, not a precondition."""
    user = make_user()
    c, _ = await _build_app_with_owned_agent("a-bot", user)
    async with c:
        await _pair(c, user)
        h = signed_headers(user, "POST", "/v1/agents/a-bot/pause", b""); h.update(_HOST)
        await c.post("/v1/agents/a-bot/pause", headers=h)
        h2 = signed_headers(user, "POST", "/v1/agents/a-bot/archive", b""); h2.update(_HOST)
        r = await c.post("/v1/agents/a-bot/archive", headers=h2)
    assert r.status == 200
    assert archive_flag_path("a-bot").exists()


async def test_archive_non_owner_403():
    user = make_user()
    c, _ = await _build_app_with_other_agent("a-bot")
    async with c:
        await _pair(c, user)
        h = signed_headers(user, "POST", "/v1/agents/a-bot/archive", b""); h.update(_HOST)
        r = await c.post("/v1/agents/a-bot/archive", headers=h)
        j = await r.json()
    assert r.status == 403
    assert "only the agent's operator" in j["error"]
    # No flag written.
    assert not archive_flag_path("a-bot").exists()
    # State untouched.
    assert _read_state("a-bot") == "running"


async def test_archive_unknown_id_404(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "POST", "/v1/agents/nope/archive", b""); h.update(_HOST)
    r = await client.post("/v1/agents/nope/archive", headers=h)
    assert r.status == 404
