"""Smoke test for ``machine link --code``, as close to e2e as a unit
test reaches: the real aiohttp client drives ``run_link`` end-to-end
over a real socket against a minimal fake puffo-server that mirrors the
mint/redeem/poll contract. Real machine registration + auth-header
signing run; only the operator's control-cert crypto (which needs the
operator root key) is stubbed, plus the post-link agent migration.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).parent))
from _bridge_support import isolated_home  # noqa: E402

from puffo_agent.portal.control import link as link_mod  # noqa: E402
from puffo_agent.portal.control.link import run_link  # noqa: E402

_REAL_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def _home():
    old = {k: os.environ.get(k) for k in ("PUFFO_AGENT_HOME", "PUFFO_HOME")}
    isolated_home()
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _make_app(state: dict) -> web.Application:
    """Minimal server: registers the machine, mints a code (mint path),
    records the claimed machine_id (redeem path), and flips the poll
    to 'approved' after one request so the loop exercises
    pending→approved."""

    async def register(request):
        body = await request.json()
        state["registered"] = body["machine_cert"]["machine_id"]
        return web.json_response({"machine_id": state["registered"]})

    async def mint(request):
        assert request.headers.get("x-puffo-machine-id")
        assert request.headers.get("x-puffo-signature")
        state["minted_by"] = request.headers["x-puffo-machine-id"]
        return web.json_response({"code": "SRVR1234"})

    async def redeem(request):
        assert request.headers.get("x-puffo-machine-id")
        assert request.headers.get("x-puffo-signature")
        state["claimed_by"] = request.headers["x-puffo-machine-id"]
        return web.json_response({"status": "claimed"})

    async def poll(request):
        state["polls"] = state.get("polls", 0) + 1
        if state["polls"] < 2:
            return web.json_response({"status": "claimed"})
        return web.json_response(
            {
                "status": "approved",
                "operator_slug": "op-0001",
                "operator_control_cert": {"kind": "control_cert"},
            }
        )

    app = web.Application()
    app.router.add_post("/v2/machines", register)
    app.router.add_post("/v2/machines/links", mint)
    app.router.add_post("/v2/machines/links/{code}/redeem", redeem)
    app.router.add_get("/v2/machines/links/{code}", poll)
    return app


@pytest.mark.asyncio
async def test_smoke_run_link_code_end_to_end(monkeypatch):
    state: dict = {}
    server = TestServer(_make_app(state))
    await server.start_server()
    try:
        base = str(server.make_url("")).rstrip("/")

        # Don't wait real seconds between polls; stub only the crypto that
        # needs the operator root key + the post-link agent migration.
        monkeypatch.setattr(link_mod.asyncio, "sleep", lambda _s: _REAL_SLEEP(0))
        monkeypatch.setattr(link_mod, "verify_control_cert", lambda *a: "op_root_pk")

        async def _noop_migrate(_root):
            return 0

        monkeypatch.setattr(link_mod, "migrate_owned_agents", _noop_migrate)

        rc = await run_link(base, "SmokeBox", open_browser=False, code="abcd-2345")
        assert rc == 0
    finally:
        await server.close()

    # The whole handshake actually happened over the wire.
    assert state["registered"].startswith("mac_")
    assert state["claimed_by"] == state["registered"]
    assert state["polls"] >= 2

    # The pairing landed on disk with the approved operator.
    from puffo_agent.portal.control.store import load_pairings

    pairings = load_pairings()
    assert "op-0001" in pairings


@pytest.mark.asyncio
async def test_smoke_run_link_mint_path_end_to_end(monkeypatch):
    """Symmetric smoke for the original mint path (no ``--code``): drives
    ``run_link`` end-to-end against a fake server that mints the code
    and then approves on the second poll. Guards the refactored mint
    branch inside ``if code: ... else: ...``."""
    state: dict = {}
    server = TestServer(_make_app(state))
    await server.start_server()
    try:
        base = str(server.make_url("")).rstrip("/")

        monkeypatch.setattr(link_mod.asyncio, "sleep", lambda _s: _REAL_SLEEP(0))
        monkeypatch.setattr(link_mod, "verify_control_cert", lambda *a: "op_root_pk")
        monkeypatch.setattr(link_mod.webbrowser, "open", lambda _url: None)

        async def _noop_migrate(_root):
            return 0

        monkeypatch.setattr(link_mod, "migrate_owned_agents", _noop_migrate)

        rc = await run_link(base, "SmokeBox", open_browser=False, code=None)
        assert rc == 0
    finally:
        await server.close()

    # Server actually minted the code for this machine, then approved on poll 2.
    assert state["registered"].startswith("mac_")
    assert state["minted_by"] == state["registered"]
    assert "claimed_by" not in state  # mint path never touches /redeem
    assert state["polls"] >= 2

    from puffo_agent.portal.control.store import load_pairings

    pairings = load_pairings()
    assert "op-0001" in pairings
