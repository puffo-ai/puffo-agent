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
        self.device = {"known": True, "revoked": True}
        self.status_queries: list[str] = []


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

    async def device_status(server_url, slug, device_id):
        s.status_queries.append(device_id)
        return dict(s.device, device_id=device_id)

    monkeypatch.setattr(unarchive, "_device_status", device_status)
    monkeypatch.setattr(unarchive, "_worker_stopper", None)
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
    stub.device = {"known": True, "revoked": False}

    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and result["device_id"] == old.device_id
    home = agent_dir(AGENT)
    assert KeyStore(home / "keys").load_identity(SLUG).device_id == old.device_id
    assert not (home / "keys" / "retired").exists()
    assert not (home / ".puffo-agent" / "archive.flag").exists()
    assert not (home / ".puffo-agent" / "pending_revoke.json").exists()
    assert stub.revokes == []
    assert yaml.safe_load((home / "agent.yml").read_text())["state"] == "paused"


@pytest.mark.parametrize("device", [
    {"known": True, "revoked": True}, {"known": False, "revoked": False},
])
async def test_not_archived_with_a_revoked_device_is_refused(stub, device):
    # The archive's revoke landed but its response was lost, so the local
    # marker still says "archived, revoke pending" — exactly what a revoke
    # never sent leaves. Only the server's record decides.
    archive, old, _ = make_archive(marker={"kind": "archive_self_revoke",
                                           "lifecycle_status": "archived",
                                           "device_id": "", "slug": SLUG,
                                           "server_url": SERVER})
    marker = json.loads((archive / ".puffo-agent" / "pending_revoke.json").read_text())
    marker["device_id"] = old.device_id
    (archive / ".puffo-agent" / "pending_revoke.json").write_text(json.dumps(marker))
    before = sorted(p.relative_to(archive).as_posix() for p in archive.rglob("*"))
    stub.restore_error = unarchive.UnarchiveError("not_archived_on_server", "not archived")
    stub.device = device
    result = await unarchive.unarchive_agent(AGENT)
    assert result["error_code"] == "restore_rejected"
    assert stub.status_queries == [old.device_id]
    assert not agent_dir(AGENT).exists()
    after = sorted(p.relative_to(archive).as_posix() for p in archive.rglob("*"))
    assert set(after) - set(before) <= {".puffo-agent/" + unarchive.STATE_FILE}
    assert (archive / ".puffo-agent" / "pending_revoke.json").exists()


async def test_several_archives_need_a_choice(stub):
    make_archive("20260901-120000")
    make_archive("20260902-120000")
    result = await unarchive.unarchive_agent(AGENT)
    assert result["error_code"] == "archive_ambiguous"
    # The portal lists these for the user to pick from: ids and times only,
    # never a local path.
    archives = result["archives"]
    assert [a["archive_id"] for a in archives] == [
        f"{AGENT}-ws-20260901-120000", f"{AGENT}-ws-20260902-120000",
    ]
    assert all(set(a) == {"archive_id", "archived_at"} for a in archives)
    assert all(a["archived_at"].endswith("Z") and "/" not in a["archived_at"] for a in archives)
    assert archives[0]["archived_at"] < archives[1]["archived_at"]
    result = await unarchive.unarchive_agent(AGENT, f"{AGENT}-ws-20260902-120000")
    assert result["ok"] and result["restored_from"] == f"{AGENT}-ws-20260902-120000"


async def test_a_pending_deletion_cannot_be_restored(stub):
    (archived_dir() / f"{AGENT}-del-20260901-120000").mkdir(parents=True)
    result = await unarchive.unarchive_agent(AGENT)
    assert result["error_code"] == "archive_deleting"


async def test_nothing_to_restore(stub):
    assert (await unarchive.unarchive_agent(AGENT))["error_code"] == "archive_not_found"


def make_live(state: str = "running") -> tuple[Path, StoredIdentity]:
    home = agent_dir(AGENT)
    (home / "keys").mkdir(parents=True)
    (home / ".puffo-agent").mkdir()
    ident, _ = _identity()
    KeyStore(home / "keys").save_identity(ident)
    (home / "agent.yml").write_text(yaml.safe_dump({
        "id": AGENT, "state": state,
        "puffo_core": {"server_url": SERVER, "slug": SLUG, "device_id": ident.device_id},
    }))
    return home, ident


async def test_a_live_agent_archived_only_on_the_server_still_gets_a_new_device(stub):
    # Its directory never moved, but its device may have been revoked
    # meanwhile: it is replaced, never trusted.
    home, old = make_live()
    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and result["mode"] == "in_place" and result["state"] == "paused"
    new_id = result["device_id"]
    assert new_id != old.device_id and len(stub.restores) == 1
    assert KeyStore(home / "keys").load_identity(SLUG).device_id == new_id
    assert (home / "keys" / "retired" / f"{old.device_id}.json").exists()
    raw = yaml.safe_load((home / "agent.yml").read_text())
    assert raw["state"] == "paused" and raw["puffo_core"]["device_id"] == new_id
    assert stub.revokes == [old.device_id] and stub.reports == [AGENT]
    assert not (home / ".puffo-agent" / unarchive.STATE_FILE).exists()

    refused = await unarchive.unarchive_agent(AGENT, f"{AGENT}-ws-20260901-120000")
    assert refused["error_code"] == "agent_exists"


async def test_a_live_agent_the_server_does_not_consider_archived_is_left_alone(stub):
    home, old = make_live()
    stub.restore_error = unarchive.UnarchiveError("not_archived_on_server", "not archived")
    result = await unarchive.unarchive_agent(AGENT)
    assert result["error_code"] == "not_archived_on_server"
    raw = yaml.safe_load((home / "agent.yml").read_text())
    assert raw["state"] == "running" and raw["puffo_core"]["device_id"] == old.device_id
    assert KeyStore(home / "keys").load_identity(SLUG).device_id == old.device_id
    assert stub.revokes == [] and stub.reports == []


async def test_a_retry_after_a_lost_ack_gets_the_same_answer(stub):
    archive, _, _ = make_archive()
    first = await unarchive.unarchive_agent(AGENT, archive.name)
    assert first["ok"]
    for archive_id in (archive.name, None):
        again = await unarchive.unarchive_agent(AGENT, archive_id)
        assert again["ok"] and again["mode"] == "already_restored"
        assert again["state"] == "paused" and again["device_id"] == first["device_id"]
        assert again["restored_from"] == archive.name
    assert len(stub.restores) == 1, "a retry must not mint or install another device"


async def test_a_stale_retry_does_not_pause_a_resumed_agent(stub):
    archive, _, _ = make_archive()
    assert (await unarchive.unarchive_agent(AGENT))["ok"]
    yml = agent_dir(AGENT) / "agent.yml"
    raw = yaml.safe_load(yml.read_text())
    raw["state"] = "running"  # the user resumed it
    yml.write_text(yaml.safe_dump(raw))
    late = await unarchive.unarchive_agent(AGENT, archive.name)
    assert late["error_code"] == "agent_exists"
    assert yaml.safe_load(yml.read_text())["state"] == "running"


async def test_concurrent_restores_of_one_agent_install_one_device(stub):
    import asyncio

    make_archive()
    a, b = await asyncio.gather(unarchive.unarchive_agent(AGENT), unarchive.unarchive_agent(AGENT))
    assert a["ok"] and b["ok"] and a["device_id"] == b["device_id"]
    assert {a["mode"], b["mode"]} == {"restored", "already_restored"}
    assert len(stub.restores) == 1


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

    def get(self, url, headers=None):
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


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (200, '{"device_id":"dev_o","known":true,"revoked":true}', {"known": True, "revoked": True}),
        (200, '{"device_id":"dev_o","known":true,"revoked":false}', {"known": True, "revoked": False}),
        (200, '{"device_id":"dev_o"}', "restore_pending"),
        (503, "", "restore_pending"),
        (403, '{"error":"FORBIDDEN","message":"restore not permitted"}', "restore_rejected"),
    ],
)
async def test_device_status_answers_map_to_outcomes(monkeypatch, status, body, outcome):
    from puffo_agent.portal.control import machine_auth, store

    signed = []
    monkeypatch.setattr(store, "load_or_create_machine", lambda: object())
    monkeypatch.setattr(machine_auth, "signed_headers",
                        lambda m, method, path, body: signed.append((method, path, body)) or {})
    session = _Session(_Resp(status, body))
    monkeypatch.setattr(unarchive, "create_remote_http_session", lambda base: session)
    path = f"/v2/machines/me/agents/{SLUG}/devices/dev_o"
    if isinstance(outcome, dict):
        got = await unarchive._device_status(SERVER, SLUG, "dev_o")
        assert {k: got[k] for k in outcome} == outcome
    else:
        with pytest.raises(unarchive.UnarchiveError) as err:
            await unarchive._device_status(SERVER, SLUG, "dev_o")
        assert err.value.code == outcome
    assert signed == [("GET", path, b"")] and session.calls[0][0] == SERVER + path


async def test_a_valid_device_needs_no_local_marker_to_come_back_as_is(stub):
    archive, old, _ = make_archive()
    stub.restore_error = unarchive.UnarchiveError("not_archived_on_server", "not archived")
    stub.device = {"known": True, "revoked": False}
    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and result["device_id"] == old.device_id
    assert stub.revokes == [] and not archive.exists()


# ── the revoke obligation survives a crash at any point ──

class Crash(BaseException):
    """Stands for the process dying: nothing after it runs."""


def _marker(directory: Path) -> dict:
    return json.loads((directory / ".puffo-agent" / "pending_revoke.json").read_text())


async def test_a_crash_before_the_revoke_leaves_the_obligation_in_the_archive(stub, monkeypatch):
    from puffo_agent.portal import import_agents

    archive, old, _ = make_archive(marker={"kind": "archive_self_revoke",
                                           "lifecycle_status": "archived",
                                           "device_id": "dev_x", "slug": SLUG,
                                           "server_url": SERVER})

    async def crash(identity, staged, old_device_id):
        raise Crash()

    monkeypatch.setattr(unarchive, "_revoke_old_device", crash)
    with pytest.raises(Crash):
        await unarchive.unarchive_agent(AGENT)

    # The identity already changed; the record of what it owes did first.
    assert KeyStore(archive / "keys").load_identity(SLUG).device_id != old.device_id
    assert _marker(archive)["old_device_id"] == old.device_id
    # The archive sweep must neither misread nor discard it.
    outcome = await import_agents._retry_archived_pending_revoke(archive)
    assert outcome is import_agents._RetryOutcome.TRANSIENT
    assert _marker(archive)["old_device_id"] == old.device_id

    async def revoke(identity, staged, old_device_id):
        stub.revokes.append(old_device_id)
        return ""

    monkeypatch.setattr(unarchive, "_revoke_old_device", revoke)
    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and not result["old_device_revoke_pending"]
    first, second = stub.restores
    assert first["device_cert"] == second["device_cert"]
    assert stub.revokes == [old.device_id]
    assert not (agent_dir(AGENT) / ".puffo-agent" / "pending_revoke.json").exists()


async def test_a_crash_before_the_move_keeps_a_failed_revoke_owed(stub, monkeypatch):
    archive, old, _ = make_archive()
    stub.revoke_error = "network down"
    real_move = unarchive._move_back

    def crash(agent_id, directory):
        raise Crash()

    monkeypatch.setattr(unarchive, "_move_back", crash)
    with pytest.raises(Crash):
        await unarchive.unarchive_agent(AGENT)
    assert _marker(archive) == {**_marker(archive), "old_device_id": old.device_id,
                                "last_error": "network down"}

    monkeypatch.setattr(unarchive, "_move_back", real_move)
    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and result["old_device_revoke_pending"]
    home = agent_dir(AGENT)
    assert _marker(home)["old_device_id"] == old.device_id, "readable by revoke-pending"
    assert stub.revokes == [old.device_id, old.device_id]
    done = json.loads((home / ".puffo-agent" / unarchive.DONE_FILE).read_text())
    assert done["old_device_id"] == old.device_id and done["old_device_revoke_pending"]

    again = await unarchive.unarchive_agent(AGENT)
    assert again["mode"] == "already_restored" and again["old_device_revoke_pending"]


async def test_a_crash_after_the_move_answers_the_retry(stub, monkeypatch):
    archive, old, _ = make_archive()
    stub.revoke_error = "network down"
    real_discard = unarchive._discard_state

    def crash(directory):
        raise Crash()

    monkeypatch.setattr(unarchive, "_discard_state", crash)
    with pytest.raises(Crash):
        await unarchive.unarchive_agent(AGENT)
    home = agent_dir(AGENT)
    assert not archive.exists() and _marker(home)["old_device_id"] == old.device_id
    # The completion record landed before the staging was due to go.
    assert (home / ".puffo-agent" / unarchive.DONE_FILE).exists()

    monkeypatch.setattr(unarchive, "_discard_state", real_discard)
    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and result["mode"] == "already_restored"
    assert result["device_id"] == stub.restores[0]["device_cert"]["device_id"]
    assert len(stub.restores) == 1
    # Still owed, and now where revoke-pending reads it.
    assert result["old_device_revoke_pending"]
    assert _marker(home)["old_device_id"] == old.device_id


async def test_in_place_settles_an_older_unsettled_revoke_first(stub):
    home, old = make_live()
    (home / ".puffo-agent" / "pending_revoke.json").write_text(
        json.dumps({"old_device_id": "dev_older", "last_error": "x"}))
    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and stub.revokes == ["dev_older", old.device_id]
    assert not (home / ".puffo-agent" / "pending_revoke.json").exists()


# ── in place: the worker is gone before the keys change ──

async def test_in_place_stops_the_worker_before_the_keys_change(stub, monkeypatch):
    home, old = make_live()
    seen = {}

    async def stopper(agent_id):
        seen["held"] = unarchive.is_held(agent_id)
        seen["device"] = KeyStore(home / "keys").load_identity(SLUG).device_id
        seen["state"] = yaml.safe_load((home / "agent.yml").read_text())["state"]

    monkeypatch.setattr(unarchive, "_worker_stopper", stopper)
    result = await unarchive.unarchive_agent(AGENT)
    assert result["ok"] and result["mode"] == "in_place"
    assert seen == {"held": True, "device": old.device_id, "state": "paused"}
    assert not unarchive.is_held(AGENT)


async def test_a_worker_that_will_not_stop_leaves_the_keys_alone(stub, monkeypatch):
    import asyncio

    home, old = make_live()

    async def stuck(agent_id):
        await asyncio.sleep(10)

    monkeypatch.setattr(unarchive, "_worker_stopper", stuck)
    monkeypatch.setattr(unarchive, "STOP_TIMEOUT_S", 0.05)
    result = await unarchive.unarchive_agent(AGENT)
    assert result["error_code"] == "restore_pending"
    assert KeyStore(home / "keys").load_identity(SLUG).device_id == old.device_id
    assert stub.revokes == []


async def test_the_daemon_waits_for_a_stop_already_under_way_and_starts_nothing_held(
    stub, monkeypatch,
):
    import asyncio

    import importlib

    from puffo_agent.portal.state import AgentConfig, DaemonConfig, RuntimeConfig

    # One module object for both the patch and the class: other tests
    # re-import the daemon module, which can leave the package attribute
    # pointing at a stale copy.
    daemon_module = importlib.import_module("puffo_agent.portal.daemon")
    Daemon = daemon_module.Daemon

    release = asyncio.Event()
    started: list[str] = []

    class SlowStopWorker:
        def __init__(self, _daemon_cfg, agent_cfg, **_kwargs):
            self.agent_cfg = agent_cfg

        def start(self):
            started.append(self.agent_cfg.id)

        async def wait_warm(self, *, timeout):
            return True

        async def stop(self):
            await release.wait()

    monkeypatch.setattr(daemon_module, "Worker", SlowStopWorker)
    daemon = Daemon(DaemonConfig())
    assert unarchive._worker_stopper == daemon._stop_worker_and_wait
    cfg = AgentConfig(id=AGENT, state="running", runtime=RuntimeConfig(harness="pi"))
    cfg.save()
    await daemon._start_and_observe_worker(cfg)
    assert started == [AGENT]

    # The reconciler began the stop; unarchive's wait must not return early.
    reconciler = asyncio.create_task(daemon._stop_worker(AGENT))
    await asyncio.sleep(0.01)
    waiter = asyncio.create_task(daemon._stop_worker_and_wait(AGENT))
    await asyncio.sleep(0.1)
    assert not waiter.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(reconciler, waiter), 2)

    unarchive._held.add(AGENT)
    try:
        await daemon._start_and_observe_worker(cfg)
    finally:
        unarchive._held.discard(AGENT)
    assert started == [AGENT] and AGENT not in daemon.workers


async def test_an_archive_owing_an_older_revoke_settles_it_before_coming_back(stub, monkeypatch):
    # Imported with its previous device still unrevoked, then archived: the
    # import marker travelled into the archive, where revoke-pending cannot
    # reach it. The restore settles it through the new device, first.
    from puffo_agent.portal import import_agents

    archive, old, _ = make_archive()
    (archive / ".puffo-agent" / "pending_revoke.json").write_text(
        json.dumps({"old_device_id": "dev_older", "last_error": "x"}))
    failing = {"dev_older"}

    async def revoke(identity, staged, device_id):
        stub.revokes.append(device_id)
        return "network down" if device_id in failing else ""

    monkeypatch.setattr(unarchive, "_revoke_old_device", revoke)
    result = await unarchive.unarchive_agent(AGENT, archive.name)
    assert result["error_code"] == "restore_pending" and "dev_older" in result["error"]
    # Not moved, the earlier obligation intact and not discarded by the sweep.
    assert archive.is_dir() and not agent_dir(AGENT).exists()
    assert _marker(archive)["old_device_id"] == "dev_older"
    assert await import_agents._retry_archived_pending_revoke(archive) is \
        import_agents._RetryOutcome.TRANSIENT
    assert _marker(archive)["old_device_id"] == "dev_older"

    failing.clear()
    result = await unarchive.unarchive_agent(AGENT, archive.name)
    assert result["ok"] and not result["old_device_revoke_pending"]
    assert stub.revokes == ["dev_older", "dev_older", old.device_id]
    assert len({json.dumps(r["device_cert"], sort_keys=True) for r in stub.restores}) == 1
    assert not (agent_dir(AGENT) / ".puffo-agent" / "pending_revoke.json").exists()


# ── review round 3: a selected archive interrupted after the move ──

@pytest.mark.parametrize("after_discard", [False, True])
async def test_a_selected_archive_interrupted_after_the_move_finishes_on_retry(
    stub, monkeypatch, after_discard,
):
    # The web always names the archive it restores. A crash after the move
    # (before or after the staging file goes) must not turn the retry into
    # agent_exists.
    archive, old, _ = make_archive()
    selected = archive.name
    real_discard = unarchive._discard_state

    def crash(directory):
        if after_discard:
            real_discard(directory)
        raise Crash()

    monkeypatch.setattr(unarchive, "_discard_state", crash)
    with pytest.raises(Crash):
        await unarchive.unarchive_agent(AGENT, selected)
    monkeypatch.setattr(unarchive, "_discard_state", real_discard)

    result = await unarchive.unarchive_agent(AGENT, selected)
    assert result["ok"] and result["state"] == "paused"
    assert result["restored_from"] == selected
    assert result["device_id"] == stub.restores[0]["device_cert"]["device_id"]
    assert all(r["device_cert"] == stub.restores[0]["device_cert"] for r in stub.restores)


async def test_a_selected_archive_interrupted_before_the_completion_record_resumes(
    stub, monkeypatch,
):
    archive, old, _ = make_archive()
    selected = archive.name
    real_done = unarchive._write_done

    def crash(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(unarchive, "_write_done", crash)
    with pytest.raises(Crash):
        await unarchive.unarchive_agent(AGENT, selected)
    home = agent_dir(AGENT)
    state = json.loads((home / ".puffo-agent" / unarchive.STATE_FILE).read_text())
    assert state["restored_from"] == selected, "persisted before the move"
    monkeypatch.setattr(unarchive, "_write_done", real_done)

    result = await unarchive.unarchive_agent(AGENT, selected)
    assert result["ok"] and result["mode"] == "restored"
    assert result["restored_from"] == selected
    assert len({json.dumps(r["device_cert"], sort_keys=True) for r in stub.restores}) == 1
    assert stub.revokes == [old.device_id, old.device_id]
    assert not (home / ".puffo-agent" / unarchive.STATE_FILE).exists()
    # A different archive id is still not this operation.
    other = await unarchive.unarchive_agent(AGENT, f"{AGENT}-ws-20990101-000000")
    assert other["error_code"] == "agent_exists"


# ── review round 3: a stop that gave up is not an exit ──

def _daemon_module():
    import importlib

    return importlib.import_module("puffo_agent.portal.daemon")


async def test_a_timed_out_stop_is_still_awaited_on_retry(stub, monkeypatch):
    import asyncio

    from puffo_agent.portal.state import DaemonConfig

    daemon = _daemon_module().Daemon(DaemonConfig())
    release = asyncio.Event()

    class StillRunning:
        async def stop(self):
            await release.wait()
            self.stop_confirmed = True

    daemon.workers[AGENT] = StillRunning()
    monkeypatch.setattr(unarchive, "_worker_stopper", daemon._stop_worker_and_wait)
    monkeypatch.setattr(unarchive, "STOP_TIMEOUT_S", 0.01)
    for _ in range(2):
        with pytest.raises(unarchive.UnarchiveError, match="did not stop"):
            await unarchive._stop_worker(AGENT)
    release.set()
    monkeypatch.setattr(unarchive, "STOP_TIMEOUT_S", 2)
    await unarchive._stop_worker(AGENT)
    assert AGENT not in daemon._stop_tasks and AGENT not in daemon._unconfirmed_stops


async def test_an_unconfirmed_exit_is_stopped_again_before_it_counts(stub, monkeypatch):
    from puffo_agent.portal.state import DaemonConfig

    daemon = _daemon_module().Daemon(DaemonConfig())
    outcomes = [False, False, True]
    calls = []

    class Wedged:
        stop_confirmed = False

        async def stop(self):
            calls.append(1)
            self.stop_confirmed = outcomes.pop(0)

    daemon.workers[AGENT] = Wedged()
    monkeypatch.setattr(unarchive, "_worker_stopper", daemon._stop_worker_and_wait)
    with pytest.raises(unarchive.UnarchiveError, match="exit not confirmed"):
        await unarchive._stop_worker(AGENT)
    assert len(calls) == 2
    await unarchive._stop_worker(AGENT)
    assert len(calls) == 3 and AGENT not in daemon._unconfirmed_stops


async def test_worker_stop_reports_a_wedged_adapter_as_unconfirmed(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from puffo_agent.portal.worker import Worker

    def bare(adapter):
        w = Worker.__new__(Worker)
        w._stop = asyncio.Event()
        w._task = None
        w._client = None
        w._adapter = adapter
        w.agent_cfg = SimpleNamespace(id=AGENT)
        w.runtime = SimpleNamespace(status="running", save=lambda _id: None)
        return w

    class Failing:
        async def aclose(self):
            raise OSError("docker stop failed")

    class Clean:
        async def aclose(self):
            return None

    failing = bare(Failing())
    await failing.stop()
    assert failing.stop_confirmed is False and failing.runtime.status == "stopped"
    clean = bare(Clean())
    await clean.stop()
    assert clean.stop_confirmed is True
