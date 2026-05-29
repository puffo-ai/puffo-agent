"""End-to-end test of the bridge ``/v1/agents/export``,
``/v1/agents/import``, and ``/v1/agents/{id}/revoke-pending``
endpoints. Drives the full flow through the same HTTP signing path
the web client uses."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _bridge_support import (
    isolated_home, make_user, pair_request_body, signed_headers,
)

from puffo_agent.crypto.canonical import canonicalize_for_signing
from puffo_agent.crypto.certs import derive_public_key_id
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.keystore import StoredIdentity, encode_secret
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair
from puffo_agent.portal.api.server import build_app
from puffo_agent.portal.state import DaemonConfig

pytestmark = pytest.mark.asyncio

_HOST = {"Host": "127.0.0.1:63387"}


@pytest_asyncio.fixture
async def mock_puffo_server():
    """Stub of puffo-server that always 200s the enrollment + revoke
    endpoints the import flow calls."""
    state = {"calls": []}
    app = web.Application()

    async def record(request):
        state["calls"].append(request.path)
        return web.json_response({"ok": True})

    app.router.add_post("/devices/subkeys", record)
    app.router.add_post("/devices/enroll/init", record)
    app.router.add_post("/devices/enroll/{nonce}/complete", record)
    app.router.add_post("/devices/{device_id}/revoke", record)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server, state
    finally:
        await server.close()


@pytest_asyncio.fixture
async def bridge_client():
    isolated_home()
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


async def _pair(client, user):
    body = pair_request_body(user)
    h = signed_headers(user, "POST", "/v1/pair", body)
    h.update(_HOST)
    r = await client.post("/v1/pair", data=body, headers=h)
    assert r.status == 200, await r.text()


def _seed_agent(
    home: str,
    agent_id: str,
    slug: str,
    server_url: str,
    *,
    state: str = "paused",
) -> str:
    # PUF-263 made paused the canonical export state — default the
    # seed accordingly so the export tests don't all need to flip
    # state by hand. Pass ``state="running"`` to test the 409 path.
    import yaml

    root = Ed25519KeyPair.generate()
    device_signing = Ed25519KeyPair.generate()
    kem = KemKeyPair.generate()
    old_device_id = derive_public_key_id("dev", device_signing.public_key_bytes())

    adir = Path(home) / "agents" / agent_id
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "memory").mkdir(exist_ok=True)
    (adir / "keys").mkdir(exist_ok=True)
    (adir / "profile.md").write_text("# profile\n", encoding="utf-8")
    cfg = {
        "id": agent_id, "state": state, "display_name": agent_id,
        "puffo_core": {
            "server_url": server_url, "slug": slug,
            "device_id": old_device_id, "space_id": "sp_test",
        },
        "runtime": {
            "kind": "chat-local", "provider": "anthropic",
            "model": "claude-sonnet-4-6", "api_key": "sk-test",
            "harness": "claude-code", "permission_mode": "bypassPermissions",
        },
        "profile": "profile.md", "memory_dir": "memory",
        "workspace_dir": "workspace",
        "triggers": {"on_mention": True, "on_dm": True},
    }
    (adir / "agent.yml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    identity_cert = {
        "type": "identity_cert", "version": 1,
        "root_public_key": base64url_encode(root.public_key_bytes()),
        "identity_type": "human", "declared_operator_public_key": None,
    }
    identity_cert["self_signature"] = base64url_encode(
        root.sign(canonicalize_for_signing(identity_cert))
    )
    slug_binding = {
        "type": "slug_binding", "version": 1,
        "root_public_key": base64url_encode(root.public_key_bytes()),
        "slug": slug, "issued_at": int(time.time() * 1000),
    }
    slug_binding["self_signature"] = base64url_encode(
        root.sign(canonicalize_for_signing(slug_binding))
    )
    identity = StoredIdentity(
        slug=slug, device_id=old_device_id,
        root_secret_key=encode_secret(root.secret_bytes()),
        device_signing_secret_key=encode_secret(device_signing.secret_bytes()),
        kem_secret_key=encode_secret(kem.secret_bytes()),
        server_url=server_url,
        slug_binding_json=json.dumps(slug_binding),
        identity_cert_json=json.dumps(identity_cert),
    )
    (adir / "keys" / f"{slug}.json").write_text(
        json.dumps(identity.to_dict(), indent=2), encoding="utf-8"
    )
    return old_device_id


async def test_bridge_export_returns_binary_blob(bridge_client):
    user = make_user()
    await _pair(bridge_client, user)
    _seed_agent(os.environ["PUFFO_AGENT_HOME"], "alpha", "alpha-bot", "http://unused")

    body = json.dumps({"agent_ids": ["alpha"], "password": "hunter2"}).encode("utf-8")
    h = signed_headers(user, "POST", "/v1/agents/export", body)
    h.update(_HOST)
    h["content-type"] = "application/json"
    r = await bridge_client.post("/v1/agents/export", data=body, headers=h)
    assert r.status == 200, await r.text()
    assert r.headers["content-type"] == "application/octet-stream"
    blob = await r.read()
    from puffo_agent.portal import export as exp
    assert blob.startswith(exp.MAGIC)
    bundle = exp.unpack(blob, "hunter2")
    assert "alpha" in bundle.agents


async def test_bridge_export_rejects_invalid_input(bridge_client):
    user = make_user()
    await _pair(bridge_client, user)
    body = json.dumps({"agent_ids": [], "password": "x"}).encode("utf-8")
    h = signed_headers(user, "POST", "/v1/agents/export", body); h.update(_HOST)
    h["content-type"] = "application/json"
    r = await bridge_client.post("/v1/agents/export", data=body, headers=h)
    assert r.status == 400


# ── PUF-263: paused-only export ─────────────────────────────────────


async def test_bridge_export_rejects_running_agent(bridge_client):
    # Operator spec (msg_9d0aaa27 item 1a): only paused agents can be
    # exported. A running agent may be mid-write (memory updates, cli
    # session refresh) so the snapshot would be inconsistent. Return
    # 409 with a reason the UI can map to "Pause the agent first."
    user = make_user()
    await _pair(bridge_client, user)
    _seed_agent(
        os.environ["PUFFO_AGENT_HOME"],
        "alpha",
        "alpha-bot",
        "http://unused",
        state="running",
    )
    body = json.dumps({"agent_ids": ["alpha"], "password": "hunter2"}).encode("utf-8")
    h = signed_headers(user, "POST", "/v1/agents/export", body)
    h.update(_HOST)
    h["content-type"] = "application/json"
    r = await bridge_client.post("/v1/agents/export", data=body, headers=h)
    assert r.status == 409, await r.text()
    body_json = await r.json()
    assert "running" in body_json["error"]
    assert "alpha" in body_json["error"]


async def test_bridge_export_rejects_whole_batch_if_any_running(bridge_client):
    # Multi-agent migration use case: a single non-paused agent in the
    # batch fails the whole export. Preserves "either everything in
    # the bundle is a consistent snapshot, or nothing is."
    user = make_user()
    await _pair(bridge_client, user)
    home = os.environ["PUFFO_AGENT_HOME"]
    _seed_agent(home, "alpha", "alpha-bot", "http://unused")  # paused (default)
    _seed_agent(home, "beta", "beta-bot", "http://unused", state="running")
    body = json.dumps({"agent_ids": ["alpha", "beta"], "password": "hunter2"}).encode("utf-8")
    h = signed_headers(user, "POST", "/v1/agents/export", body)
    h.update(_HOST)
    h["content-type"] = "application/json"
    r = await bridge_client.post("/v1/agents/export", data=body, headers=h)
    assert r.status == 409, await r.text()
    # The 409 fingers the offending agent, not the paused one.
    body_json = await r.json()
    assert "beta" in body_json["error"]


async def test_bridge_import_roundtrip(bridge_client, mock_puffo_server):
    user = make_user()
    await _pair(bridge_client, user)
    puffo, calls_state = mock_puffo_server
    url = str(puffo.make_url("/")).rstrip("/")
    _seed_agent(os.environ["PUFFO_AGENT_HOME"], "alpha", "alpha-bot", url)

    # Export through the bridge.
    body = json.dumps({"agent_ids": ["alpha"], "password": "hunter2"}).encode("utf-8")
    h = signed_headers(user, "POST", "/v1/agents/export", body); h.update(_HOST)
    h["content-type"] = "application/json"
    r = await bridge_client.post("/v1/agents/export", data=body, headers=h)
    assert r.status == 200
    blob = await r.read()

    # Wipe local state to simulate the new machine, then re-pair so
    # we're authed against the fresh home.
    isolated_home()
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c2:
        await _pair(c2, user)
        import_body = json.dumps({
            "bundle_b64": base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii"),
            "password": "hunter2",
        }).encode("utf-8")
        h2 = signed_headers(user, "POST", "/v1/agents/import", import_body); h2.update(_HOST)
        h2["content-type"] = "application/json"
        r2 = await c2.post("/v1/agents/import", data=import_body, headers=h2)
        assert r2.status == 200, await r2.text()
        report = await r2.json()
        assert report["imported"] == 1
        assert report["results"][0]["agent_id"] == "alpha"
        # The mock server saw the enrollment + revoke calls.
        assert any(p == "/devices/enroll/init" for p in calls_state["calls"])
        assert any(p.endswith("/revoke") for p in calls_state["calls"])


async def test_bridge_import_wrong_password(bridge_client):
    user = make_user()
    await _pair(bridge_client, user)

    # Build any valid bundle (no agents exist yet, so seed one + pack).
    _seed_agent(os.environ["PUFFO_AGENT_HOME"], "alpha", "alpha-bot", "http://unused")
    from puffo_agent.portal import export as exp
    blob = exp.pack(["alpha"], password="hunter2")
    import shutil
    shutil.rmtree(Path(os.environ["PUFFO_AGENT_HOME"]) / "agents" / "alpha")

    body = json.dumps({
        "bundle_b64": base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii"),
        "password": "wrong",
    }).encode("utf-8")
    h = signed_headers(user, "POST", "/v1/agents/import", body); h.update(_HOST)
    h["content-type"] = "application/json"
    r = await bridge_client.post("/v1/agents/import", data=body, headers=h)
    assert r.status == 400


async def test_bridge_revoke_pending_no_marker(bridge_client):
    user = make_user()
    user_root_pk = base64url_encode(user.root_key.public_key_bytes())
    home = isolated_home()

    # Re-pair against the fresh home.
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        from _bridge_support import write_test_agent
        write_test_agent(home, "alpha", owner_root_pubkey=user_root_pk)
        h = signed_headers(user, "POST", "/v1/agents/alpha/revoke-pending", b"")
        h.update(_HOST)
        r = await c.post("/v1/agents/alpha/revoke-pending", data=b"", headers=h)
        assert r.status == 200, await r.text()
        body = await r.json()
        assert body["status"] == "skipped"


async def test_bridge_revoke_pending_requires_owner(bridge_client):
    user = make_user()
    other_root_pk = base64url_encode(b"\x99" * 32)
    home = isolated_home()
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        from _bridge_support import write_test_agent
        write_test_agent(home, "alpha", owner_root_pubkey=other_root_pk)
        h = signed_headers(user, "POST", "/v1/agents/alpha/revoke-pending", b"")
        h.update(_HOST)
        r = await c.post("/v1/agents/alpha/revoke-pending", data=b"", headers=h)
        assert r.status == 403
