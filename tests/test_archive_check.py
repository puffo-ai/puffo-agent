"""Unit + e2e tests for portal.import_agents.check_archived_device.

Covers the five outcomes (CONSISTENT / RECONCILED / DEVICE_NOT_FOUND /
NO_KEYS / UNREACHABLE), the pending_revoke marker cleanup, the sweep
runner, and race safety when two concurrent probes hit the same dir.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
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
# Mock puffo-server with configurable behaviour per device_id.
# ────────────────────────────────────────────────────────────────────


def _make_state():
    return {
        "calls": [],
        "revoked_devices": set(),       # device_ids the server already has revoked
        "missing_devices": set(),       # 400 DEVICE_NOT_FOUND
        "subkey_fail_status": None,     # override probe status entirely
        "revoke_fail_status": None,     # induce failure on the second POST
    }


def _make_app(state):
    app = web.Application()

    async def post_subkey(request):
        body = await request.json()
        cert = body.get("subkey_cert") or {}
        device_id = cert.get("device_id", "")
        state["calls"].append(("POST", "/devices/subkeys", device_id))
        if state["subkey_fail_status"] is not None:
            return web.json_response(
                {"error": "INDUCED", "message": "test"},
                status=state["subkey_fail_status"],
            )
        if device_id in state["missing_devices"]:
            return web.json_response(
                {"error": "DEVICE_NOT_FOUND", "message": "not in cert registry"},
                status=400,
            )
        if device_id in state["revoked_devices"]:
            return web.json_response(
                {"error": "DEVICE_REVOKED", "message": "device has been revoked"},
                status=403,
            )
        return web.json_response({"ok": True})

    async def post_revoke(request):
        device_id = request.match_info["device_id"]
        state["calls"].append(("POST", f"/devices/{device_id}/revoke", device_id))
        if state["revoke_fail_status"] is not None:
            return web.json_response({"error": "INDUCED"}, status=state["revoke_fail_status"])
        state["revoked_devices"].add(device_id)
        return web.json_response({"ok": True}, status=201)

    app.router.add_post("/devices/subkeys", post_subkey)
    app.router.add_post("/devices/{device_id}/revoke", post_revoke)
    return app


@pytest_asyncio.fixture
async def mock_server():
    state = _make_state()
    server = TestServer(_make_app(state))
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
# Seed helpers: materialise an archived agent as it would exist on
# disk after cmd_agent_archive moved its directory.
# ────────────────────────────────────────────────────────────────────


def _signed_identity_cert(root: Ed25519KeyPair, operator_pk: str = "") -> dict:
    cert = {
        "type": "identity_cert",
        "version": 1,
        "root_public_key": base64url_encode(root.public_key_bytes()),
        "identity_type": "human",
        "declared_operator_public_key": operator_pk or None,
    }
    cert["self_signature"] = base64url_encode(
        root.sign(canonicalize_for_signing(cert))
    )
    return cert


def _signed_slug_binding(root: Ed25519KeyPair, slug: str) -> dict:
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


def _seed_archived(
    dir_name: str,
    *,
    slug: str,
    server_url: str,
    operator_pk: str = "",
) -> dict:
    home = Path(os.environ["PUFFO_AGENT_HOME"])
    adir = home / "archived" / dir_name
    (adir / "keys").mkdir(parents=True, exist_ok=True)

    root = Ed25519KeyPair.generate()
    device_signing = Ed25519KeyPair.generate()
    kem = KemKeyPair.generate()
    device_id = derive_public_key_id("dev", device_signing.public_key_bytes())

    cfg = {
        "id": dir_name.split("-2026")[0],
        "state": "paused",
        "display_name": slug,
        "puffo_core": {
            "server_url": server_url,
            "slug": slug,
            "device_id": device_id,
            "space_id": "sp_test",
        },
        "runtime": {
            "kind": "chat-local",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_key": "",
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
        device_id=device_id,
        root_secret_key=encode_secret(root.secret_bytes()),
        device_signing_secret_key=encode_secret(device_signing.secret_bytes()),
        kem_secret_key=encode_secret(kem.secret_bytes()),
        server_url=server_url,
        slug_binding_json=json.dumps(_signed_slug_binding(root, slug)),
        identity_cert_json=json.dumps(_signed_identity_cert(root, operator_pk)),
    )
    (adir / "keys" / f"{slug}.json").write_text(
        json.dumps(identity.to_dict(), indent=2), encoding="utf-8"
    )
    return {
        "dir": adir,
        "slug": slug,
        "device_id": device_id,
        "root": root,
        "operator_pk": operator_pk,
    }


# ────────────────────────────────────────────────────────────────────
# Outcome matrix
# ────────────────────────────────────────────────────────────────────


async def test_reconciled_when_server_still_active(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("alpha-20260707-000000", slug="alpha", server_url=url)

    from puffo_agent.portal import import_agents as imp
    result = await imp.check_archived_device(info["dir"])
    assert result.outcome is imp.ArchiveCheckOutcome.RECONCILED
    assert result.slug == "alpha"
    assert result.device_id == info["device_id"]
    # Server got both calls: probe accepted, revoke posted.
    paths = [c[1] for c in state["calls"]]
    assert "/devices/subkeys" in paths
    assert f"/devices/{info['device_id']}/revoke" in paths
    assert info["device_id"] in state["revoked_devices"]


async def test_consistent_when_server_already_revoked(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("beta-20260707-000000", slug="beta", server_url=url)
    state["revoked_devices"].add(info["device_id"])  # server already knows

    from puffo_agent.portal import import_agents as imp
    result = await imp.check_archived_device(info["dir"])
    assert result.outcome is imp.ArchiveCheckOutcome.CONSISTENT
    # No revoke POST — probe alone told us we're consistent.
    revoke_calls = [c for c in state["calls"] if c[1].endswith("/revoke")]
    assert revoke_calls == []


async def test_device_not_found(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("gamma-20260707-000000", slug="gamma", server_url=url)
    state["missing_devices"].add(info["device_id"])

    from puffo_agent.portal import import_agents as imp
    result = await imp.check_archived_device(info["dir"])
    assert result.outcome is imp.ArchiveCheckOutcome.DEVICE_NOT_FOUND


async def test_unreachable_when_probe_5xx(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("delta-20260707-000000", slug="delta", server_url=url)
    state["subkey_fail_status"] = 500

    from puffo_agent.portal import import_agents as imp
    result = await imp.check_archived_device(info["dir"])
    assert result.outcome is imp.ArchiveCheckOutcome.UNREACHABLE
    assert "500" in result.detail


async def test_unreachable_when_revoke_5xx(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("eps-20260707-000000", slug="eps", server_url=url)
    state["revoke_fail_status"] = 500

    from puffo_agent.portal import import_agents as imp
    result = await imp.check_archived_device(info["dir"])
    assert result.outcome is imp.ArchiveCheckOutcome.UNREACHABLE
    # Probe succeeded, revoke didn't — server had NOT actually revoked.
    assert info["device_id"] not in state["revoked_devices"]


async def test_no_keys_when_agent_yml_missing(mock_server):
    server, _state = mock_server
    home = Path(os.environ["PUFFO_AGENT_HOME"])
    adir = home / "archived" / "zeta-20260707-000000"
    adir.mkdir(parents=True)
    # No agent.yml, no keys/.

    from puffo_agent.portal import import_agents as imp
    result = await imp.check_archived_device(adir)
    assert result.outcome is imp.ArchiveCheckOutcome.NO_KEYS
    assert result.slug == ""


async def test_no_keys_when_agent_yml_lacks_puffo_core():
    home = Path(os.environ["PUFFO_AGENT_HOME"])
    adir = home / "archived" / "empty-20260707-000000"
    adir.mkdir(parents=True)
    (adir / "agent.yml").write_text("id: foo\nstate: paused\n", encoding="utf-8")

    from puffo_agent.portal import import_agents as imp
    r = await imp.check_archived_device(adir)
    assert r.outcome is imp.ArchiveCheckOutcome.NO_KEYS


async def test_no_keys_when_agent_yml_unparseable():
    home = Path(os.environ["PUFFO_AGENT_HOME"])
    adir = home / "archived" / "junk-20260707-000000"
    adir.mkdir(parents=True)
    (adir / "agent.yml").write_text("::: not: valid: yaml\n:\n", encoding="utf-8")

    from puffo_agent.portal import import_agents as imp
    r = await imp.check_archived_device(adir)
    assert r.outcome is imp.ArchiveCheckOutcome.NO_KEYS


async def test_corrupt_identity_cert_json_leaves_owner_blank(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("crptcert-20260707-000000", slug="crptcert", server_url=url)
    state["revoked_devices"].add(info["device_id"])
    # Overwrite the keystore JSON so identity_cert_json is a
    # non-JSON string — code path 833-834.
    keys_file = info["dir"] / "keys" / "crptcert.json"
    stored = json.loads(keys_file.read_text(encoding="utf-8"))
    stored["identity_cert_json"] = "not-valid-json {"
    keys_file.write_text(json.dumps(stored), encoding="utf-8")

    from puffo_agent.portal import import_agents as imp
    r = await imp.check_archived_device(info["dir"])
    assert r.outcome is imp.ArchiveCheckOutcome.CONSISTENT
    assert r.owner_root_pubkey == ""


async def test_no_keys_when_root_secret_key_corrupt(mock_server):
    server, _state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("brokenkey-20260707-000000", slug="brokenkey", server_url=url)
    keys_file = info["dir"] / "keys" / "brokenkey.json"
    stored = json.loads(keys_file.read_text(encoding="utf-8"))
    # Well-formed base64 but wrong length → Ed25519 constructor rejects.
    stored["root_secret_key"] = "AAAA"
    keys_file.write_text(json.dumps(stored), encoding="utf-8")

    from puffo_agent.portal import import_agents as imp
    r = await imp.check_archived_device(info["dir"])
    assert r.outcome is imp.ArchiveCheckOutcome.NO_KEYS
    assert "key decode failed" in r.detail


async def test_unreachable_when_server_closed():
    # Spin up a TestServer, capture its URL, then close it so the
    # first probe hits the aiohttp.ClientError branch.
    server = TestServer(_make_app(_make_state()))
    await server.start_server()
    url = str(server.make_url("/")).rstrip("/")
    await server.close()

    info = _seed_archived("dead-20260707-000000", slug="dead", server_url=url)
    from puffo_agent.portal import import_agents as imp
    r = await imp.check_archived_device(info["dir"])
    assert r.outcome is imp.ArchiveCheckOutcome.UNREACHABLE


async def test_no_keys_when_keystore_dir_missing(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("eta-20260707-000000", slug="eta", server_url=url)
    # Nuke the keystore so load_identity errors.
    import shutil
    shutil.rmtree(info["dir"] / "keys")

    from puffo_agent.portal import import_agents as imp
    result = await imp.check_archived_device(info["dir"])
    assert result.outcome is imp.ArchiveCheckOutcome.NO_KEYS


async def test_reconcile_clears_pending_revoke_marker(mock_server):
    server, _state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("theta-20260707-000000", slug="theta", server_url=url)

    from puffo_agent.portal import import_agents as imp
    imp.write_archived_pending_revoke(
        info["dir"],
        server_url=url,
        slug="theta",
        device_id=info["device_id"],
        last_error="prior fail",
    )
    marker = imp.archived_pending_revoke_path(info["dir"])
    assert marker.exists()

    result = await imp.check_archived_device(info["dir"])
    assert result.outcome is imp.ArchiveCheckOutcome.RECONCILED
    assert not marker.exists()  # cleaned up on successful revoke


async def test_consistent_does_not_touch_marker(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("iota-20260707-000000", slug="iota", server_url=url)
    state["revoked_devices"].add(info["device_id"])

    from puffo_agent.portal import import_agents as imp
    imp.write_archived_pending_revoke(
        info["dir"], server_url=url, slug="iota",
        device_id=info["device_id"], last_error="prior",
    )
    marker = imp.archived_pending_revoke_path(info["dir"])
    assert marker.exists()

    result = await imp.check_archived_device(info["dir"])
    assert result.outcome is imp.ArchiveCheckOutcome.CONSISTENT
    # Marker left intact — we didn't post a revoke, so nothing to
    # unmark. The daemon's startup sweep will handle it.
    assert marker.exists()


# ────────────────────────────────────────────────────────────────────
# sweep_archive_check + result surfacing
# ────────────────────────────────────────────────────────────────────


async def test_sweep_walks_all_archived_dirs(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info_a = _seed_archived("aaa-20260707-000000", slug="aaa", server_url=url)
    info_b = _seed_archived("bbb-20260707-000000", slug="bbb", server_url=url)
    info_c = _seed_archived("ccc-20260707-000000", slug="ccc", server_url=url)
    state["revoked_devices"].add(info_b["device_id"])  # already revoked
    state["missing_devices"].add(info_c["device_id"])

    from puffo_agent.portal import import_agents as imp
    results = await imp.sweep_archive_check()

    by_slug = {r.slug: r for r in results}
    assert by_slug["aaa"].outcome is imp.ArchiveCheckOutcome.RECONCILED
    assert by_slug["bbb"].outcome is imp.ArchiveCheckOutcome.CONSISTENT
    assert by_slug["ccc"].outcome is imp.ArchiveCheckOutcome.DEVICE_NOT_FOUND


async def test_sweep_returns_empty_when_no_archived_dir():
    from puffo_agent.portal import import_agents as imp
    results = await imp.sweep_archive_check()
    assert results == []


async def test_sweep_ignores_files_at_top_level(mock_server):
    server, _state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("solo-20260707-000000", slug="solo", server_url=url)
    # Drop a stray file that must not confuse the walker.
    (info["dir"].parent / ".DS_Store").write_bytes(b"junk")

    from puffo_agent.portal import import_agents as imp
    results = await imp.sweep_archive_check()
    assert len(results) == 1
    assert results[0].slug == "solo"


async def test_owner_pk_propagates_from_identity_cert(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    op_pk = "op-root-pubkey-test"
    info = _seed_archived(
        "owned-20260707-000000",
        slug="owned",
        server_url=url,
        operator_pk=op_pk,
    )
    state["revoked_devices"].add(info["device_id"])

    from puffo_agent.portal import import_agents as imp
    r = await imp.check_archived_device(info["dir"])
    assert r.owner_root_pubkey == op_pk


# ────────────────────────────────────────────────────────────────────
# Race: two probes on the same dir in parallel must not corrupt state.
# ────────────────────────────────────────────────────────────────────


async def test_parallel_probes_are_safe(mock_server):
    server, state = mock_server
    url = str(server.make_url("/")).rstrip("/")
    info = _seed_archived("race-20260707-000000", slug="race", server_url=url)

    from puffo_agent.portal import import_agents as imp
    r1, r2 = await asyncio.gather(
        imp.check_archived_device(info["dir"]),
        imp.check_archived_device(info["dir"]),
    )
    # Both should succeed logically: one reconciles, the other sees
    # the freshly-revoked device and reports CONSISTENT (server is
    # stateful across both requests, so second probe hits the
    # already-revoked branch).
    outcomes = {r1.outcome, r2.outcome}
    assert imp.ArchiveCheckOutcome.RECONCILED in outcomes
    assert outcomes.issubset({
        imp.ArchiveCheckOutcome.RECONCILED,
        imp.ArchiveCheckOutcome.CONSISTENT,
    })
    assert info["device_id"] in state["revoked_devices"]


# ────────────────────────────────────────────────────────────────────
# CLI wrapper — rc=0 when everything clean, rc=1 when problems.
# ────────────────────────────────────────────────────────────────────


async def test_cli_format_all_consistent():
    from puffo_agent.portal import cli, import_agents as imp
    results = [
        imp.ArchiveCheckResult(
            dir_name="alpha-20260707-000000", slug="alpha",
            device_id="dev_x", outcome=imp.ArchiveCheckOutcome.CONSISTENT,
        ),
        imp.ArchiveCheckResult(
            dir_name="beta-20260707-000000", slug="beta",
            device_id="dev_y", outcome=imp.ArchiveCheckOutcome.RECONCILED,
        ),
    ]
    text, rc = cli.format_archive_check_results(results)
    assert rc == 0
    assert "[ok] alpha-20260707-000000" in text
    assert "[revoked] beta-20260707-000000" in text
    assert "summary: 2 checked, 1 revoked, 0 needing attention" in text


async def test_cli_format_flags_problems():
    from puffo_agent.portal import cli, import_agents as imp
    results = [
        imp.ArchiveCheckResult(
            dir_name="alpha-20260707-000000", slug="alpha", device_id="dev_x",
            outcome=imp.ArchiveCheckOutcome.UNREACHABLE, detail="TimeoutError",
        ),
        imp.ArchiveCheckResult(
            dir_name="beta-20260707-000000", slug="",
            device_id="", outcome=imp.ArchiveCheckOutcome.NO_KEYS,
            detail="agent.yml missing",
        ),
        imp.ArchiveCheckResult(
            dir_name="gamma-20260707-000000", slug="gamma", device_id="dev_z",
            outcome=imp.ArchiveCheckOutcome.DEVICE_NOT_FOUND,
        ),
    ]
    text, rc = cli.format_archive_check_results(results)
    assert rc == 1
    assert "[unreachable]" in text
    assert "[no-keys]" in text
    assert "[device-not-found]" in text
    assert "summary: 3 checked, 0 revoked, 3 needing attention" in text


async def test_cli_format_empty():
    from puffo_agent.portal import cli
    text, rc = cli.format_archive_check_results([])
    assert rc == 0
    assert text == "no archived agents to check"
