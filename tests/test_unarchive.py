"""Unarchive restores an archived agent, paused, on a new device.

Real keys and real files throughout; only the three network calls
(restore-device, old-device revoke, paused report) are stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from puffo_agent.crypto.certs import derive_public_key_id
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.keystore import KeyStore, StoredIdentity, decode_secret, encode_secret
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair
from puffo_agent.portal import unarchive
from puffo_agent.portal.state import agent_dir, archived_dir

pytestmark = pytest.mark.asyncio

AGENT = "helper-0001"
SLUG = "helper-0001"
SERVER = "https://puffo.test"


class Stub:
    def __init__(self) -> None:
        self.restores: list[dict] = []
        self.restore_error: unarchive.UnarchiveError | None = None
        self.revokes: list[str] = []
        self.revoke_error = ""
        self.reports: list[str] = []


@pytest.fixture
def stub(monkeypatch, tmp_path):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    s = Stub()

    async def post_restore(server_url, slug, staged):
        s.restores.append(json.loads(json.dumps(staged)))
        if s.restore_error is not None:
            raise s.restore_error
        return {"device_id": staged["device_cert"]["device_id"], "created": True}

    async def revoke(identity, staged, old_device_id):
        s.revokes.append(old_device_id)
        return s.revoke_error

    async def report(agent_id):
        s.reports.append(agent_id)
        return True

    monkeypatch.setattr(unarchive, "_post_restore", post_restore)
    monkeypatch.setattr(unarchive, "_revoke_old_device", revoke)
    monkeypatch.setattr(unarchive, "_report_paused", report)
    return s


def _identity() -> tuple[StoredIdentity, Ed25519KeyPair]:
    root = Ed25519KeyPair.generate()
    device = Ed25519KeyPair.generate()
    ident = StoredIdentity(
        slug=SLUG,
        device_id=derive_public_key_id("dev", device.public_key_bytes()),
        root_secret_key=encode_secret(root.secret_bytes()),
        device_signing_secret_key=encode_secret(device.secret_bytes()),
        kem_secret_key=encode_secret(KemKeyPair.generate().secret_bytes()),
        server_url=SERVER,
    )
    return ident, root


def make_archive(stamp: str = "20260901-120000", *, marker: dict | None = None) -> tuple[Path, StoredIdentity, Ed25519KeyPair]:
    archive = archived_dir() / f"{AGENT}-ws-{stamp}"
    (archive / "keys").mkdir(parents=True)
    (archive / ".puffo-agent").mkdir()
    ident, root = _identity()
    KeyStore(archive / "keys").save_identity(ident)
    (archive / "agent.yml").write_text(yaml.safe_dump({
        "id": AGENT,
        "state": "running",
        "puffo_core": {"server_url": SERVER, "slug": SLUG, "device_id": ident.device_id},
        "runtime": {"harness_command": ["/opt/lingtai", "acp", "--profile", "puffo-v1",
                                        "--runtime-id", "puffo-abc", "--registry", "/r.json"]},
    }))
    (archive / ".puffo-agent" / "archive.flag").touch()
    if marker is not None:
        (archive / ".puffo-agent" / "pending_revoke.json").write_text(json.dumps(marker))
    return archive, ident, root


async def test_restores_on_a_new_device_and_comes_back_paused(stub):
    archive, old, root = make_archive(marker={"kind": "archive_self_revoke",
                                              "lifecycle_status": "archived",
                                              "device_id": "dev_x", "slug": SLUG,
                                              "server_url": SERVER})
    result = await unarchive.unarchive_agent(AGENT)

    assert result["ok"] and result["mode"] == "restored" and result["state"] == "paused"
    new_id = result["device_id"]
    assert new_id != old.device_id
    assert not archive.exists()
    home = agent_dir(AGENT)
    dot = home / ".puffo-agent"
    # The flag that caused the archive must not travel back: it would
    # re-archive the agent (and revoke the new device) on the next tick.
    assert not (dot / "archive.flag").exists()
    # The archive-schema marker would crash revoke-pending, which reads the
    # same path with a different schema.
    assert not (dot / "pending_revoke.json").exists()
    assert not (dot / unarchive.STATE_FILE).exists()

    raw = yaml.safe_load((home / "agent.yml").read_text())
    assert raw["state"] == "paused"
    assert raw["puffo_core"]["device_id"] == new_id
    assert raw["runtime"]["harness_command"][5] == "puffo-abc", "LingTai binding kept"

    keys = KeyStore(home / "keys")
    now = keys.load_identity(SLUG)
    assert now.device_id == new_id and now.root_secret_key == old.root_secret_key
    retired = json.loads((home / "keys" / "retired" / f"{old.device_id}.json").read_text())
    assert retired["kem_secret_key"] == old.kem_secret_key, "old decryption key kept"
    assert keys.list_identities() == [SLUG], "retired identity must not look like a second one"
    session = json.loads((home / "keys" / f"{SLUG}.session.json").read_text())
    assert session["subkey_id"] == stub.restores[0]["subkey_cert"]["subkey_id"]

    cert = stub.restores[0]["device_cert"]
    assert cert["root_public_key"] == base64url_encode(root.public_key_bytes())
    signing_pk = Ed25519KeyPair.from_secret_bytes(
        decode_secret(stub.restores[0]["secrets"]["device_signing"])
    ).public_key_bytes()
    assert cert["device_id"] == derive_public_key_id("dev", signing_pk)
    assert stub.revokes == [old.device_id]
    assert stub.reports == [AGENT]


async def test_a_failed_restore_leaves_the_archive_untouched_and_retries_the_same_certs(stub):
    archive, old, _ = make_archive()
    before = sorted(p.relative_to(archive).as_posix() for p in archive.rglob("*"))
    stub.restore_error = unarchive.UnarchiveError("restore_pending", "server busy")

    result = await unarchive.unarchive_agent(AGENT)
    assert result == {"ok": False, "error_code": "restore_pending", "error": "server busy"}
    assert (archive / ".puffo-agent" / "archive.flag").exists()
    assert KeyStore(archive / "keys").load_identity(SLUG).device_id == old.device_id
    state = json.loads((archive / ".puffo-agent" / unarchive.STATE_FILE).read_text())
    assert state["last_error"]["code"] == "restore_pending"
    after = sorted(p.relative_to(archive).as_posix() for p in archive.rglob("*"))
    assert set(after) - set(before) == {".puffo-agent/" + unarchive.STATE_FILE}

    stub.restore_error = None
    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"]
    first, second = stub.restores
    assert first["device_cert"] == second["device_cert"], "retry must replay, not re-mint"
    assert first["subkey_cert"] == second["subkey_cert"]


async def test_a_refused_restore_is_final_and_keeps_the_archive(stub):
    archive, _, _ = make_archive()
    stub.restore_error = unarchive.UnarchiveError("restore_rejected", "server refused (HTTP 403)")
    result = await unarchive.unarchive_agent(AGENT)
    assert result["error_code"] == "restore_rejected"
    assert archive.is_dir() and not agent_dir(AGENT).exists()


async def test_an_unsettled_old_revoke_is_handed_to_revoke_pending(stub):
    _, old, _ = make_archive()
    stub.revoke_error = "network down"
    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and result["old_device_revoke_pending"]
    marker = json.loads((agent_dir(AGENT) / ".puffo-agent" / "pending_revoke.json").read_text())
    assert marker["old_device_id"] == old.device_id, "import schema, readable by revoke-pending"


async def test_an_archive_that_never_revoked_is_restored_in_place(stub):
    # The archived report failed, so the revoke was never sent: the server
    # still sees the agent live and its device is valid.
    archive, old, _ = make_archive()
    marker = {"kind": "archive_self_revoke", "lifecycle_status": "archived",
              "device_id": old.device_id, "slug": SLUG, "server_url": SERVER}
    (archive / ".puffo-agent" / "pending_revoke.json").write_text(json.dumps(marker))
    stub.restore_error = unarchive.UnarchiveError("not_archived_on_server", "not archived")

    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and result["device_id"] == old.device_id
    home = agent_dir(AGENT)
    assert KeyStore(home / "keys").load_identity(SLUG).device_id == old.device_id
    assert not (home / "keys" / "retired").exists()
    assert not (home / ".puffo-agent" / "archive.flag").exists()
    assert not (home / ".puffo-agent" / "pending_revoke.json").exists()
    assert stub.revokes == []
    assert yaml.safe_load((home / "agent.yml").read_text())["state"] == "paused"


async def test_not_archived_without_a_pending_archive_is_refused(stub):
    archive, _, _ = make_archive()
    stub.restore_error = unarchive.UnarchiveError("not_archived_on_server", "not archived")
    result = await unarchive.unarchive_agent(AGENT)
    assert result["error_code"] == "restore_rejected"
    assert archive.is_dir()


async def test_several_archives_need_a_choice(stub):
    make_archive("20260901-120000")
    make_archive("20260902-120000")
    result = await unarchive.unarchive_agent(AGENT)
    assert result["error_code"] == "archive_ambiguous"
    assert result["archive_ids"] == [f"{AGENT}-ws-20260901-120000", f"{AGENT}-ws-20260902-120000"]
    result = await unarchive.unarchive_agent(AGENT, f"{AGENT}-ws-20260902-120000")
    assert result["ok"] and result["restored_from"] == f"{AGENT}-ws-20260902-120000"


async def test_a_pending_deletion_cannot_be_restored(stub):
    (archived_dir() / f"{AGENT}-del-20260901-120000").mkdir(parents=True)
    result = await unarchive.unarchive_agent(AGENT)
    assert result["error_code"] == "archive_deleting"


async def test_nothing_to_restore(stub):
    assert (await unarchive.unarchive_agent(AGENT))["error_code"] == "archive_not_found"


async def test_a_live_agent_archived_only_on_the_server_is_just_paused(stub):
    home = agent_dir(AGENT)
    (home / "keys").mkdir(parents=True)
    ident, _ = _identity()
    (home / "agent.yml").write_text(yaml.safe_dump({
        "id": AGENT, "state": "running",
        "puffo_core": {"server_url": SERVER, "slug": SLUG, "device_id": ident.device_id},
    }))
    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and result["mode"] == "server_only"
    assert yaml.safe_load((home / "agent.yml").read_text())["state"] == "paused"
    assert stub.restores == [] and stub.reports == [AGENT]

    refused = await unarchive.unarchive_agent(AGENT, f"{AGENT}-ws-20260901-120000")
    assert refused["error_code"] == "agent_exists"


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".hidden", "", "x" * 65])
async def test_an_unsafe_agent_id_touches_nothing(stub, bad):
    result = await unarchive.unarchive_agent(bad)
    assert result["ok"] is False and result["error"] == "invalid agent id"


async def test_already_revoked_is_success_but_other_conflicts_are_not(monkeypatch):
    from puffo_agent.portal import import_agents

    ident, _ = _identity()
    staged = {
        "device_cert": {"device_id": "dev_new"},
        "subkey_cert": {"subkey_id": "sk_new"},
        "secrets": {
            "device_signing": encode_secret(Ed25519KeyPair.generate().secret_bytes()),
            "subkey": encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        },
    }

    async def raising(message):
        async def _revoke(**_):
            raise import_agents.ImportError(message)
        return _revoke

    monkeypatch.setattr(import_agents, "_revoke_old_device",
                        await raising('/devices/dev_old/revoke 409: {"code":"NOT_IMPROVING"}'))
    assert await unarchive._revoke_old_device(ident, staged, "dev_old") == ""
    monkeypatch.setattr(import_agents, "_revoke_old_device",
                        await raising("/devices/dev_old/revoke 409: nonce replay"))
    assert "nonce replay" in await unarchive._revoke_old_device(ident, staged, "dev_old")


async def test_the_control_op_reaches_unarchive(monkeypatch):
    from puffo_agent.portal.control import client

    seen = {}

    async def fake(agent_id, archive_id=None):
        seen["args"] = (agent_id, archive_id)
        return {"ok": True}

    monkeypatch.setattr(unarchive, "unarchive_agent", fake)
    result = await client.execute_command("unarchive", AGENT, {"archive_id": "a1"})
    assert result == {"ok": True} and seen["args"] == (AGENT, "a1")
    assert "unarchive" in client.BACKGROUND_OPS


class _Resp:
    def __init__(self, status: int, body: str) -> None:
        self.status, self._body = status, body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, resp: _Resp) -> None:
        self.resp, self.calls = resp, []

    def post(self, url, data=None, headers=None):
        self.calls.append((url, headers))
        return self.resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        # Bodies as puffo-server's V2ApiError serializes them.
        (409, '{"error":"CONFLICT","message":"agent is not archived"}', "not_archived_on_server"),
        (409, '{"error":"CONFLICT","message":"nonce replay"}', "restore_rejected"),
        (403, '{"error":"FORBIDDEN","message":"restore not permitted"}', "restore_rejected"),
        (400, '{"error":"BAD_REQUEST","message":"device is revoked"}', "restore_rejected"),
        (503, '{"error":"SERVICE_UNAVAILABLE","message":"authentication unavailable"}', "restore_pending"),
        (429, "", "restore_pending"),
    ],
)
async def test_server_answers_map_to_outcomes(monkeypatch, status, body, code):
    from puffo_agent.portal.control import machine_auth, store

    monkeypatch.setattr(store, "load_or_create_machine", lambda: object())
    monkeypatch.setattr(machine_auth, "signed_headers", lambda *a, **k: {"x-puffo-machine-id": "mac_1"})
    session = _Session(_Resp(status, body))
    monkeypatch.setattr(unarchive, "create_remote_http_session", lambda base: session)
    staged = {"device_cert": {"device_id": "dev_n"}, "subkey_cert": {"subkey_id": "sk_n"}}
    with pytest.raises(unarchive.UnarchiveError) as err:
        await unarchive._post_restore(SERVER, SLUG, staged)
    assert err.value.code == code
    assert session.calls[0][0] == f"{SERVER}/v2/machines/me/agents/{SLUG}/restore-device"


async def test_server_success_is_returned(monkeypatch):
    from puffo_agent.portal.control import machine_auth, store

    monkeypatch.setattr(store, "load_or_create_machine", lambda: object())
    monkeypatch.setattr(machine_auth, "signed_headers", lambda *a, **k: {})
    monkeypatch.setattr(unarchive, "create_remote_http_session",
                        lambda base: _Session(_Resp(201, '{"device_id":"dev_n","created":true}')))
    staged = {"device_cert": {"device_id": "dev_n"}, "subkey_cert": {"subkey_id": "sk_n"}}
    assert (await unarchive._post_restore(SERVER, SLUG, staged))["created"] is True
