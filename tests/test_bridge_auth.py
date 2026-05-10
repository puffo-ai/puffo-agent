"""Auth and pairing tests for the local bridge.

Covers: pair flow (cert verification, slug binding, human-only,
single-pairing rule), signed-request lifecycle (replay/stale/tamper
rejection), DNS-rebinding via Host header, and unpaired-daemon
rejection of protected endpoints.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _bridge_support import isolated_home, make_user, pair_request_body, signed_headers
from puffo_agent.portal.api.server import build_app
from puffo_agent.portal.state import DaemonConfig

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    isolated_home()
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


# Loopback requests must carry an allowlisted Host header. TestClient
# picks a random port so we override Host explicitly to the value
# BridgeConfig.port expects.
_HOST = {"Host": "127.0.0.1:63387"}


# ────────────────────────────────────────────────────────────────────
# pair flow
# ────────────────────────────────────────────────────────────────────


async def test_pair_succeeds_with_valid_certs(client):
    user = make_user()
    body = pair_request_body(user)
    headers = signed_headers(user, "POST", "/v1/pair", body)
    headers.update(_HOST)
    resp = await client.post("/v1/pair", data=body, headers=headers)
    assert resp.status == 200, await resp.text()
    j = await resp.json()
    assert j["paired_slug"] == user.slug
    assert j["paired_device_id"] == user.device_id


async def test_pair_idempotent_with_same_identity(client):
    user = make_user()
    body = pair_request_body(user)
    h1 = signed_headers(user, "POST", "/v1/pair", body); h1.update(_HOST)
    r1 = await client.post("/v1/pair", data=body, headers=h1)
    assert r1.status == 200
    h2 = signed_headers(user, "POST", "/v1/pair", body); h2.update(_HOST)
    r2 = await client.post("/v1/pair", data=body, headers=h2)
    assert r2.status == 200


async def test_pair_overwrites_existing_pairing(client):
    """Second ``POST /v1/pair`` from a different identity wins — same
    effect as ``pairing unpair`` + re-pair. Lets the web UI take over
    without forcing the operator to a terminal."""
    a = make_user(slug="alice-0001", device_id="dev_a")
    b = make_user(slug="bob-0002", device_id="dev_b")
    body_a = pair_request_body(a)
    h = signed_headers(a, "POST", "/v1/pair", body_a); h.update(_HOST)
    r = await client.post("/v1/pair", data=body_a, headers=h)
    assert r.status == 200

    body_b = pair_request_body(b)
    h2 = signed_headers(b, "POST", "/v1/pair", body_b); h2.update(_HOST)
    r2 = await client.post("/v1/pair", data=body_b, headers=h2)
    assert r2.status == 200, await r2.text()
    j = await r2.json()
    assert j["paired_slug"] == "bob-0002"
    assert j["paired_device_id"] == "dev_b"

    # /v1/info should now reflect the new pairing too.
    r3 = await client.get("/v1/info", headers=_HOST)
    assert r3.status == 200
    info = await r3.json()
    assert info["paired"] is True
    assert info["paired_slug"] == "bob-0002"
    assert info["paired_device_id"] == "dev_b"


async def test_pair_rejects_agent_identity_type(client):
    user = make_user()
    user.identity_cert["identity_type"] = "agent"
    # Re-sign after mutation so the cert is internally valid.
    from puffo_agent.crypto.canonical import canonicalize_for_signing
    from puffo_agent.crypto.encoding import base64url_encode
    user.identity_cert.pop("self_signature", None)
    canonical = canonicalize_for_signing(user.identity_cert)
    user.identity_cert["self_signature"] = base64url_encode(
        user.root_key.sign(canonical),
    )
    body = pair_request_body(user)
    h = signed_headers(user, "POST", "/v1/pair", body); h.update(_HOST)
    r = await client.post("/v1/pair", data=body, headers=h)
    assert r.status == 400
    assert "human" in (await r.json())["error"]


async def test_pair_rejects_slug_mismatch(client):
    user = make_user()
    body = pair_request_body(user)
    h = signed_headers(user, "POST", "/v1/pair", body)
    h["x-puffo-slug"] = "carol-9999"  # mismatch with cert
    h.update(_HOST)
    r = await client.post("/v1/pair", data=body, headers=h)
    assert r.status == 400


async def test_pair_rejects_tampered_identity_cert(client):
    user = make_user()
    user.identity_cert["username"] = "imposter"  # invalidates self_signature
    body = pair_request_body(user)
    h = signed_headers(user, "POST", "/v1/pair", body); h.update(_HOST)
    r = await client.post("/v1/pair", data=body, headers=h)
    assert r.status == 400


# ────────────────────────────────────────────────────────────────────
# signed request lifecycle
# ────────────────────────────────────────────────────────────────────


async def _pair(client, user):
    body = pair_request_body(user)
    h = signed_headers(user, "POST", "/v1/pair", body); h.update(_HOST)
    r = await client.post("/v1/pair", data=body, headers=h)
    assert r.status == 200, await r.text()


async def test_protected_endpoint_works_after_pairing(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "GET", "/v1/agents"); h.update(_HOST)
    r = await client.get("/v1/agents", headers=h)
    assert r.status == 200, await r.text()


async def test_protected_endpoint_rejected_before_pairing(client):
    user = make_user()
    h = signed_headers(user, "GET", "/v1/agents"); h.update(_HOST)
    r = await client.get("/v1/agents", headers=h)
    assert r.status == 401
    assert "not paired" in (await r.json())["error"]


async def test_replayed_nonce_rejected(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "GET", "/v1/agents"); h.update(_HOST)
    r1 = await client.get("/v1/agents", headers=h)
    assert r1.status == 200
    # Identical headers -> same nonce -> rejected.
    r2 = await client.get("/v1/agents", headers=h)
    assert r2.status == 401
    assert "nonce" in (await r2.json())["error"]


async def test_stale_timestamp_rejected(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "GET", "/v1/agents")
    # 10min in the past — outside the 5min skew window.
    import time as _t
    h["x-puffo-timestamp"] = str(int(_t.time() * 1000) - 10 * 60 * 1000)
    h.update(_HOST)
    r = await client.get("/v1/agents", headers=h)
    assert r.status == 401
    assert "timestamp" in (await r.json())["error"]


async def test_tampered_signature_rejected(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "GET", "/v1/agents")
    # Flip a middle char. The last char isn't safe — base64url's last
    # char encodes only a subset of bits and certain swaps decode to
    # the same bytes.
    sig = h["x-puffo-signature"]
    mid = len(sig) // 2
    h["x-puffo-signature"] = (
        sig[:mid] + ("A" if sig[mid] != "A" else "B") + sig[mid + 1:]
    )
    h.update(_HOST)
    r = await client.get("/v1/agents", headers=h)
    assert r.status == 401


async def test_wrong_slug_after_pairing_rejected(client):
    a = make_user(slug="alice-0001", device_id="dev_a")
    b = make_user(slug="bob-0002", device_id="dev_b")
    await _pair(client, a)
    h = signed_headers(b, "GET", "/v1/agents"); h.update(_HOST)
    r = await client.get("/v1/agents", headers=h)
    assert r.status == 401
    assert "paired identity" in (await r.json())["error"]


# ────────────────────────────────────────────────────────────────────
# DNS rebinding
# ────────────────────────────────────────────────────────────────────


async def test_rebinding_host_rejected(client):
    user = make_user()
    h = signed_headers(user, "GET", "/v1/info")
    h["Host"] = "evil.com"
    r = await client.get("/v1/info", headers=h)
    assert r.status == 403


async def test_loopback_host_accepted(client):
    h = {"Host": "localhost:63387"}
    r = await client.get("/v1/info", headers=h)
    assert r.status == 200
