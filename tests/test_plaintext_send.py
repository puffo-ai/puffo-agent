"""Plaintext (non-E2EE) send: producer round-trip + the daemon-level
encryption decision + the client DM send branch."""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent import send_mode
from puffo_agent.crypto.message import (
    EncryptInput,
    build_plaintext_message,
    read_plaintext_message,
)
from puffo_agent.crypto.primitives import Ed25519KeyPair
from puffo_agent.crypto.keystore import encode_secret


def _inp(**over):
    base = dict(
        envelope_kind="dm",
        sender_slug="agent-1",
        sender_subkey_id="sk_1",
        is_visible_to_human=True,
        recipient_slug="op-1",
        content_type="text/plain",
        content="hello",
    )
    base.update(over)
    return EncryptInput(**base)


@pytest.fixture(autouse=True)
def _clean_registry():
    send_mode._turn_bundle_encrypted.clear()
    yield
    send_mode._turn_bundle_encrypted.clear()


# ── producer ─────────────────────────────────────────────────────────


def test_plaintext_round_trips_through_reader():
    kp = Ed25519KeyPair.generate()
    env = build_plaintext_message(_inp(), kp)
    assert env["type"] == "plaintext_message_envelope"
    assert env["version"] == 1
    assert env["envelope_id"].startswith("msg_")
    payload = read_plaintext_message(env, kp.public_key_bytes())
    assert payload.content == "hello"
    assert payload.envelope_kind == "dm"
    assert payload.recipient_slug == "op-1"
    # Inactive route fields serialize as explicit null.
    assert env["signed_payload"]["payload"]["space_id"] is None


def test_tampered_plaintext_fails_verification():
    kp = Ed25519KeyPair.generate()
    env = build_plaintext_message(_inp(), kp)
    env["signed_payload"]["payload"]["content"] = "evil"
    with pytest.raises(ValueError):
        read_plaintext_message(env, kp.public_key_bytes())


def test_wrong_key_fails_verification():
    kp = Ed25519KeyPair.generate()
    env = build_plaintext_message(_inp(), kp)
    with pytest.raises(ValueError):
        read_plaintext_message(env, Ed25519KeyPair.generate().public_key_bytes())


# ── decision ─────────────────────────────────────────────────────────


class _Row:
    def __init__(self, is_encrypted):
        self.is_encrypted = is_encrypted


class _Store:
    def __init__(self, row=None, raise_=False):
        self.row = row
        self.raise_ = raise_

    async def get_message_by_envelope(self, _eid):
        if self.raise_:
            raise RuntimeError("db down")
        return self.row


@pytest.mark.asyncio
async def test_default_is_plaintext():
    assert await send_mode.encryption_required("a-1", _Store(), None) is False


@pytest.mark.asyncio
async def test_encrypted_bundle_forces_encryption():
    send_mode.note_turn_bundle(["a-1"], True)
    assert await send_mode.encryption_required("a-1", _Store(), None) is True
    # A later all-plaintext bundle releases it.
    send_mode.note_turn_bundle(["a-1"], False)
    assert await send_mode.encryption_required("a-1", _Store(), None) is False


@pytest.mark.asyncio
async def test_encrypted_thread_root_forces_encryption():
    assert (
        await send_mode.encryption_required("a-1", _Store(_Row(True)), "msg_r")
        is True
    )
    assert (
        await send_mode.encryption_required("a-1", _Store(_Row(False)), "msg_r")
        is False
    )


@pytest.mark.asyncio
async def test_unknown_root_and_store_failure_fail_safe_to_encrypted():
    assert await send_mode.encryption_required("a-1", _Store(None), "msg_r") is True
    assert (
        await send_mode.encryption_required("a-1", _Store(raise_=True), "msg_r")
        is True
    )


# ── client DM send branch ────────────────────────────────────────────


def _make_client(flag: bool):
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient

    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client._log = logging.getLogger("plaintext-send-test")
    client.store = _Store(row=None)

    kp = Ed25519KeyPair.generate()

    class _Sess:
        subkey_id = "sk_1"
        subkey_secret_key = encode_secret(kp.secret_bytes())

    class _Keystore:
        def load_session(self, _slug):
            return _Sess()

    client.keystore = _Keystore()

    posts = []

    class _Http:
        async def post(self, path, body):
            posts.append((path, body))
            return {"devices_queued": 1}

    client.http = _Http()

    fetched = []

    async def _fetch(slugs):
        fetched.append(slugs)
        from puffo_agent.crypto.message import RecipientDevice
        import os as _os

        return [RecipientDevice(device_id="dev_1", kem_public_key=_os.urandom(32))]

    client._fetch_device_keys = _fetch
    send_mode.note_turn_bundle(["agent-1"], flag)
    return client, posts, fetched


@pytest.mark.asyncio
async def test_send_dm_goes_plaintext_by_default():
    client, posts, fetched = _make_client(flag=False)
    env = await client._send_dm("op-1", "hi", "")
    assert env["type"] == "plaintext_message_envelope"
    assert posts[0][0] == "/v2/messages/plaintext"
    assert fetched == []  # no device resolution on the plaintext path


@pytest.mark.asyncio
async def test_send_dm_encrypts_when_turn_bundle_was_encrypted():
    client, posts, fetched = _make_client(flag=True)
    env = await client._send_dm("op-1", "hi", "")
    assert env["type"] == "message_envelope"
    assert posts[0][0] == "/messages"
    assert fetched  # devices resolved for sealing


@pytest.mark.asyncio
async def test_in_process_data_client_delegates_to_send_mode():
    from puffo_agent.portal.ws_local.in_process_data_client import (
        InProcessDataClient,
    )

    c = InProcessDataClient.__new__(InProcessDataClient)
    c._store = _Store(_Row(False))
    assert await c.get_send_encryption("a-1", "msg_r") is False
    send_mode.note_turn_bundle(["a-1"], True)
    assert await c.get_send_encryption("a-1", None) is True


@pytest.mark.asyncio
async def test_clear_turn_bundle_restores_the_plaintext_default():
    send_mode.note_turn_bundle(["a-1"], True)
    assert await send_mode.encryption_required("a-1", _Store(), None) is True
    send_mode.clear_turn_bundle(["a-1"])
    assert await send_mode.encryption_required("a-1", _Store(), None) is False
    # Clearing an unset key is a no-op.
    send_mode.clear_turn_bundle(["never-set"])
