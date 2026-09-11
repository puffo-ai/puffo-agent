"""Plaintext envelope format (inbound compatibility) + the always-E2EE
outbound DM invariant that replaced the per-turn send-mode decision."""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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


# ── wire format (inbound plaintext envelopes are still readable) ─────


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


# ── outbound DMs always encrypt (no turn/lifecycle input) ────────────


def _make_client(device_slugs=None):
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient

    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client._log = logging.getLogger("plaintext-send-test")

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

        return [
            RecipientDevice(device_id=f"dev_{slug}", kem_public_key=_os.urandom(32))
            for slug in slugs
            if device_slugs is None or slug in device_slugs
        ]

    client._fetch_device_keys = _fetch
    return client, posts, fetched


@pytest.mark.asyncio
async def test_send_dm_always_encrypts():
    client, posts, fetched = _make_client()
    env = await client._send_dm("op-1", "hi", "")
    assert env["type"] == "message_envelope"
    assert posts[0][0] == "/messages"
    # The recipient's reachability is checked on its own devices first.
    assert fetched[0] == ["op-1"]


@pytest.mark.asyncio
async def test_send_dm_fails_when_recipient_has_no_devices():
    # The sender's own devices must not mask an unreachable recipient:
    # only agent-1 resolves devices here, so the send raises instead of
    # posting an envelope sealed to the sender alone.
    client, posts, fetched = _make_client(device_slugs={"agent-1"})
    with pytest.raises(RuntimeError, match="op-1 has no encryption devices"):
        await client._send_dm("op-1", "hi", "")
    assert posts == []  # nothing left the process
    assert fetched == [["op-1"]]  # failed before the sender fetch
