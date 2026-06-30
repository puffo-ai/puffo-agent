"""Unit tests for portal.import_agents — 3-phase flow against a
mocked puffo-server, including happy path, revoke failure, and the
revoke_pending retry."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from _bridge_support import isolated_home

from puffo_agent.crypto.canonical import canonicalize_for_signing
from puffo_agent.crypto.certs import derive_public_key_id
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.keystore import StoredIdentity, encode_secret
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair

pytestmark = pytest.mark.asyncio


# ────────────────────────────────────────────────────────────────────
# Mock puffo-server. Records every request so tests can assert what
# the import flow sent. Each route can be flipped to fail via
# ``state["fail"]``.
# ────────────────────────────────────────────────────────────────────


def _make_mock_server_state():
    return {
        "calls": [],
        "fail": set(),  # path strings or "revoke" sentinel
    }


def _make_mock_app(state):
    app = web.Application()

    async def record(request):
        path = request.path
        state["calls"].append((request.method, path))
        if path in state["fail"] or any(p in path for p in state["fail"] if isinstance(p, str)):
            return web.json_response({"error": "induced failure"}, status=500)
        return web.json_response({"ok": True})

    app.router.add_post("/devices/subkeys", record)
    app.router.add_post("/devices/enroll/init", record)
    app.router.add_post("/devices/enroll/{nonce}/complete", record)
    app.router.add_post("/devices/{device_id}/revoke", record)
    return app


@pytest_asyncio.fixture
async def mock_server():
    state = _make_mock_server_state()
    server = TestServer(_make_mock_app(state))
    await server.start_server()
    try:
        yield server, state
    finally:
        await server.close()


@pytest.fixture(autouse=True)
def fresh_home():
    isolated_home()
    yield


# ────────────────────────────────────────────────────────────────────
# Builders: a realistic .puffoagent bundle pointing at the mock URL.
# ────────────────────────────────────────────────────────────────────


def _build_signed_identity_cert(root: Ed25519KeyPair) -> dict:
    cert = {
        "type": "identity_cert",
        "version": 1,
        "root_public_key": base64url_encode(root.public_key_bytes()),
        "identity_type": "human",
        "declared_operator_public_key": None,
    }
    cert["self_signature"] = base64url_encode(
        root.sign(canonicalize_for_signing(cert))
    )
    return cert


def _build_signed_slug_binding(root: Ed25519KeyPair, slug: str) -> dict:
    sb = {
        "type": "slug_binding",
        "version": 1,
        "root_public_key": base64url_encode(root.public_key_bytes()),
        "slug": slug,
        "issued_at": int(time.time() * 1000),
    }
    sb["self_signature"] = base64url_encode(
        root.sign(canonicalize_for_signing(sb))
    )
    return sb


def _seed_source_agent(home: str, agent_id: str, slug: str, server_url: str) -> dict:
    """Materialise an agent dir as it would exist on the *source*
    machine — with full keys, agent.yml pointing at the mock server.
    Returns dict with the generated key material for assertions."""
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
        "id": agent_id,
        "state": "running",
        "display_name": agent_id,
        "puffo_core": {
            "server_url": server_url,
            "slug": slug,
            "device_id": old_device_id,
            "space_id": "sp_test",
        },
        "runtime": {
            "kind": "chat-local",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_key": "sk-test",
            "harness": "claude-code",
            "permission_mode": "bypassPermissions",
        },
        "profile": "profile.md",
        "memory_dir": "memory",
        "workspace_dir": "workspace",
        "triggers": {"on_mention": True, "on_dm": True},
    }
    (adir / "agent.yml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    identity = StoredIdentity(
        slug=slug,
        device_id=old_device_id,
        root_secret_key=encode_secret(root.secret_bytes()),
        device_signing_secret_key=encode_secret(device_signing.secret_bytes()),
        kem_secret_key=encode_secret(kem.secret_bytes()),
        server_url=server_url,
        slug_binding_json=json.dumps(_build_signed_slug_binding(root, slug)),
        identity_cert_json=json.dumps(_build_signed_identity_cert(root)),
    )
    (adir / "keys" / f"{slug}.json").write_text(
        json.dumps(identity.to_dict(), indent=2), encoding="utf-8"
    )
    return {
        "root": root,
        "device_signing": device_signing,
        "kem": kem,
        "old_device_id": old_device_id,
        "slug": slug,
        "agent_id": agent_id,
    }


def _build_bundle(server_url: str, password: str = "hunter2") -> tuple[bytes, dict]:
    from puffo_agent.portal import export as exp

    info = _seed_source_agent(os.environ["PUFFO_AGENT_HOME"], "alpha", "alpha-bot", server_url)
    blob = exp.pack(["alpha"], password=password, exported_by_slug="op-source")
    # Drop the source agent dir so import doesn't hit the skip-existing branch.
    import shutil
    shutil.rmtree(Path(os.environ["PUFFO_AGENT_HOME"]) / "agents" / "alpha")
    return blob, info


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────


async def test_remote_http_session_trusts_proxy_env():
    from puffo_agent.portal import import_agents as imp

    async with imp._remote_http_session("https://api.puffo.ai") as session:
        assert getattr(session, "_trust_env", None) is True


async def test_import_happy_path(mock_server):
    from puffo_agent.portal import import_agents as imp
    from puffo_agent.portal.state import AgentConfig

    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    blob, info = _build_bundle(url)

    report = await imp.import_bundle(blob, password="hunter2")
    assert report.failed == 0
    assert report.imported == 1
    result = report.results[0]
    assert result.status == "imported"
    assert result.new_device_id != info["old_device_id"]
    assert result.old_device_id == info["old_device_id"]

    cfg = AgentConfig.load("alpha")
    assert cfg.puffo_core.device_id == result.new_device_id
    assert cfg.puffo_core.slug == "alpha-bot"
    assert cfg.state == "running"

    # 2 subkey POSTs (old for enroll, new persisted as session); revoke reuses the new one.
    paths = [p for _, p in state["calls"]]
    assert paths.count("/devices/subkeys") == 2
    assert "/devices/enroll/init" in paths
    assert any(p.startswith("/devices/enroll/") and p.endswith("/complete") for p in paths)
    assert any(p.endswith("/revoke") for p in paths)

    session_path = Path(os.environ["PUFFO_AGENT_HOME"]) / "agents" / "alpha" / "keys" / "alpha-bot.session.json"
    assert session_path.exists()
    sess = json.loads(session_path.read_text(encoding="utf-8"))
    assert sess["slug"] == "alpha-bot"
    assert sess["subkey_id"] and sess["subkey_secret_key"]
    assert sess["expires_at"] > int(time.time() * 1000)


async def test_import_skips_existing(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    blob, _info = _build_bundle(url)

    first = await imp.import_bundle(blob, password="hunter2")
    assert first.imported == 1

    pre = len(state["calls"])
    second = await imp.import_bundle(blob, password="hunter2")
    assert second.imported == 0
    assert second.skipped == 1
    # No additional server calls on skip.
    assert len(state["calls"]) == pre


async def test_import_enrollment_failure_cleans_staging(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    state["fail"] = {"/devices/enroll/init"}
    url = str(server.make_url("/")).rstrip("/")
    blob, _ = _build_bundle(url)

    report = await imp.import_bundle(blob, password="hunter2")
    assert report.failed == 1
    assert report.imported == 0
    # Staging dir should be gone, no live agent dir created.
    assert not imp.staging_dir("alpha").exists()
    assert not Path(os.environ["PUFFO_AGENT_HOME"], "agents", "alpha").exists()


async def test_import_revoke_failure_leaves_pending(mock_server):
    from puffo_agent.portal import import_agents as imp
    from puffo_agent.portal.state import AgentConfig

    server, state = mock_server
    state["fail"] = {"revoke"}  # matches by substring in path
    url = str(server.make_url("/")).rstrip("/")
    blob, info = _build_bundle(url)

    report = await imp.import_bundle(blob, password="hunter2")
    assert report.failed == 0
    assert report.pending_revokes == 1
    r = report.results[0]
    assert r.status == "imported_pending_revoke"
    pending = imp.pending_revoke_path("alpha")
    assert pending.exists()
    payload = json.loads(pending.read_text(encoding="utf-8"))
    assert payload["old_device_id"] == info["old_device_id"]
    # Revoke is best-effort — new device works, state still flips to running.
    assert AgentConfig.load("alpha").state == "running"


async def test_import_new_subkey_failure_is_soft(mock_server, monkeypatch):
    # Subkey reg may 401 (chain validation lag after enrol) — must still land + flip to running.
    from puffo_agent.portal import import_agents as imp
    from puffo_agent.portal.state import AgentConfig

    server, _state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    blob, _info = _build_bundle(url)

    async def _boom(**_kwargs):
        raise RuntimeError("/devices/subkeys 401: chain validation failed")

    monkeypatch.setattr(imp, "_register_new_device_subkey", _boom)

    report = await imp.import_bundle(blob, password="hunter2")
    assert report.failed == 0
    assert report.imported == 1
    cfg = AgentConfig.load("alpha")
    assert cfg.state == "running"
    session_path = Path(os.environ["PUFFO_AGENT_HOME"]) / "agents" / "alpha" / "keys" / "alpha-bot.session.json"
    assert not session_path.exists()


async def test_revoke_pending_succeeds_on_retry(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    state["fail"] = {"revoke"}
    url = str(server.make_url("/")).rstrip("/")
    blob, info = _build_bundle(url)
    await imp.import_bundle(blob, password="hunter2")

    # Server is healthy now; retry should succeed and clear the marker.
    state["fail"] = set()
    result = await imp.revoke_pending("alpha")
    assert result.status == "imported"
    assert not imp.pending_revoke_path("alpha").exists()


async def test_revoke_pending_no_marker():
    from puffo_agent.portal import import_agents as imp
    from _bridge_support import write_test_agent

    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "alpha")
    result = await imp.revoke_pending("alpha")
    assert result.status == "skipped"


async def test_revoke_pending_agent_not_found():
    from puffo_agent.portal import import_agents as imp

    result = await imp.revoke_pending("ghost")
    assert result.status == "failed"


async def test_list_pending_revokes_and_cleanup_staging():
    from puffo_agent.portal import import_agents as imp
    from puffo_agent.portal.state import agents_dir

    # Drop a fake pending marker on disk.
    a = agents_dir() / "alpha"
    (a / ".puffo-agent").mkdir(parents=True, exist_ok=True)
    (a / "agent.yml").write_text("id: alpha\n", encoding="utf-8")
    (a / ".puffo-agent" / "pending_revoke.json").write_text(
        json.dumps({"old_device_id": "dev_old", "last_error": "boom",
                    "attempted_at": 0}),
        encoding="utf-8",
    )
    found = imp.list_pending_revokes()
    assert ("alpha", "dev_old") in found

    # Staging cleanup is a no-op when nothing's there, and removes
    # an existing .import-staging tree otherwise.
    imp.cleanup_staging_dir()
    staging = agents_dir() / ".import-staging" / "x"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "f").write_text("x")
    assert staging.exists()
    imp.cleanup_staging_dir()
    assert not (agents_dir() / ".import-staging").exists()


# ────────────────────────────────────────────────────────────────────
# Self-revoke for archive / delete: ensure POST /devices/<id>/revoke
# fires, and that pending markers in the archived dir get swept.
# ────────────────────────────────────────────────────────────────────


async def test_revoke_agent_device_posts_to_revoke_endpoint(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_source_agent(
        os.environ["PUFFO_AGENT_HOME"], "alpha", "alpha-bot", url,
    )

    await imp.revoke_agent_device("alpha")

    revoke_calls = [
        (m, p) for (m, p) in state["calls"]
        if f"/devices/{info['old_device_id']}/revoke" in p
    ]
    assert revoke_calls == [("POST", f"/devices/{info['old_device_id']}/revoke")]
    subkey_calls = [
        (m, p) for (m, p) in state["calls"] if p == "/devices/subkeys"
    ]
    assert len(subkey_calls) == 1


async def test_revoke_agent_device_propagates_server_failure(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    state["fail"] = {"revoke"}
    url = str(server.make_url("/")).rstrip("/")
    _seed_source_agent(
        os.environ["PUFFO_AGENT_HOME"], "alpha", "alpha-bot", url,
    )
    with pytest.raises(Exception):
        await imp.revoke_agent_device("alpha")


async def test_revoke_agent_device_reuses_fresh_session_subkey(mock_server):
    """A fresh ``<slug>.session.json`` already on disk (typically left by
    the lifecycle-heartbeat path running first) should be reused for
    the revoke POST — exactly one ``/devices/subkeys`` registration per
    archive instead of two."""
    from puffo_agent.portal import import_agents as imp
    from puffo_agent.crypto.keystore import KeyStore, Session, encode_secret
    from puffo_agent.crypto.primitives import Ed25519KeyPair

    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_source_agent(
        os.environ["PUFFO_AGENT_HOME"], "alpha", "alpha-bot", url,
    )
    fresh_subkey = Ed25519KeyPair.generate()
    KeyStore.for_agent("alpha").save_session(Session(
        slug=info["slug"],
        subkey_id="sk_preregistered",
        subkey_secret_key=encode_secret(fresh_subkey.secret_bytes()),
        expires_at=int(time.time() * 1000) + 24 * 3600 * 1000,
    ))

    await imp.revoke_agent_device("alpha")

    subkey_posts = [
        (m, p) for (m, p) in state["calls"] if p == "/devices/subkeys"
    ]
    revoke_posts = [
        (m, p) for (m, p) in state["calls"]
        if f"/devices/{info['old_device_id']}/revoke" in p
    ]
    assert subkey_posts == []
    assert len(revoke_posts) == 1


async def test_revoke_agent_device_registers_fresh_when_session_expired(
    mock_server,
):
    from puffo_agent.portal import import_agents as imp
    from puffo_agent.crypto.keystore import KeyStore, Session, encode_secret
    from puffo_agent.crypto.primitives import Ed25519KeyPair

    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_source_agent(
        os.environ["PUFFO_AGENT_HOME"], "alpha", "alpha-bot", url,
    )
    stale_subkey = Ed25519KeyPair.generate()
    KeyStore.for_agent("alpha").save_session(Session(
        slug=info["slug"],
        subkey_id="sk_stale",
        subkey_secret_key=encode_secret(stale_subkey.secret_bytes()),
        expires_at=1,
    ))

    await imp.revoke_agent_device("alpha")

    subkey_posts = [
        (m, p) for (m, p) in state["calls"] if p == "/devices/subkeys"
    ]
    assert len(subkey_posts) == 1


async def test_revoke_agent_device_noop_when_puffo_core_unconfigured(tmp_path):
    """Chat-local / standalone agents with no ``puffo_core`` block
    can't be revoked (no server). Helper returns cleanly, no error."""
    from puffo_agent.portal import import_agents as imp
    from puffo_agent.portal.state import agent_dir
    import yaml

    adir = agent_dir("standalone")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "agent.yml").write_text(
        yaml.safe_dump({
            "id": "standalone",
            "state": "paused",
            "display_name": "standalone",
            "runtime": {"kind": "chat-local"},
        }),
        encoding="utf-8",
    )
    await imp.revoke_agent_device("standalone")


async def test_sweep_archived_pending_revokes_retries_and_clears(mock_server):
    """Drop a freshly-archived agent dir + pending_revoke marker into
    ``archived/``; the sweep should retry the revoke against the
    healthy mock server and unlink the marker."""
    from puffo_agent.portal import import_agents as imp
    from puffo_agent.portal.state import agent_dir, archived_dir

    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_source_agent(
        os.environ["PUFFO_AGENT_HOME"], "alpha", "alpha-bot", url,
    )

    # Simulate a completed archive (move to archived dir + drop marker).
    archived_dir().mkdir(parents=True, exist_ok=True)
    dest = archived_dir() / "alpha-20260629-120000"
    Path(agent_dir("alpha")).rename(dest)
    imp.write_archived_pending_revoke(
        dest,
        server_url=url,
        slug=info["slug"],
        device_id=info["old_device_id"],
        last_error="transient: 503",
    )
    assert imp.archived_pending_revoke_path(dest).exists()

    n = await imp.sweep_archived_pending_revokes()
    assert n == 1
    assert not imp.archived_pending_revoke_path(dest).exists()
    revoke_calls = [
        (m, p) for (m, p) in state["calls"]
        if f"/devices/{info['old_device_id']}/revoke" in p
    ]
    assert revoke_calls == [("POST", f"/devices/{info['old_device_id']}/revoke")]


async def test_sweep_archived_pending_revokes_leaves_marker_on_transient_failure(
    mock_server,
):
    from puffo_agent.portal import import_agents as imp
    from puffo_agent.portal.state import agent_dir, archived_dir

    server, state = mock_server
    state["fail"] = {"revoke"}
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_source_agent(
        os.environ["PUFFO_AGENT_HOME"], "alpha", "alpha-bot", url,
    )
    archived_dir().mkdir(parents=True, exist_ok=True)
    dest = archived_dir() / "alpha-20260629-120000"
    Path(agent_dir("alpha")).rename(dest)
    imp.write_archived_pending_revoke(
        dest,
        server_url=url,
        slug=info["slug"],
        device_id=info["old_device_id"],
        last_error="initial: 500",
    )

    n = await imp.sweep_archived_pending_revokes()
    assert n == 0
    assert imp.archived_pending_revoke_path(dest).exists()


async def test_sweep_archived_pending_revokes_handles_empty_archived_dir():
    from puffo_agent.portal import import_agents as imp

    # ``archived/`` may not exist yet on a fresh install.
    n = await imp.sweep_archived_pending_revokes()
    assert n == 0


async def test_write_archived_pending_revoke_schema(tmp_path):
    from puffo_agent.portal import import_agents as imp

    dest = tmp_path / "alpha-20260629-120000"
    dest.mkdir()
    imp.write_archived_pending_revoke(
        dest,
        server_url="http://example",
        slug="alpha-bot",
        device_id="dev_xyz",
        last_error="boom",
    )
    payload = json.loads(
        imp.archived_pending_revoke_path(dest).read_text(encoding="utf-8"),
    )
    assert payload["kind"] == "archive_self_revoke"
    assert payload["server_url"] == "http://example"
    assert payload["slug"] == "alpha-bot"
    assert payload["device_id"] == "dev_xyz"
    assert payload["last_error"] == "boom"
    assert isinstance(payload["attempted_at"], int)
