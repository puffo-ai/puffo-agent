"""Post-send ``missing_devices`` supplementation."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.crypto.message import (
    EncryptInput,
    RecipientDevice,
    build_supplementation_envelope,
    decrypt_message,
    encrypt_message_with_content_key,
)
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair
from puffo_agent.mcp.puffo_core_tools import _supplement_missing_devices
from puffo_agent.agent.send_coordinator import CHANNEL_SEND_PATH
from .test_send_coordinator import coordinator_fixture


def _make_recipient() -> tuple[RecipientDevice, KemKeyPair]:
    kp = KemKeyPair.generate()
    return RecipientDevice(
        device_id=f"dev_{os.urandom(4).hex()}",
        kem_public_key=kp.public_key_bytes(),
    ), kp


def _channel_input(devices: list[RecipientDevice]) -> EncryptInput:
    return EncryptInput(
        envelope_kind="channel",
        sender_slug="alice-0001",
        sender_subkey_id="sk_alice",
        is_visible_to_human=True,
        space_id="sp_1",
        channel_id="ch_1",
        content_type="text/plain",
        content="Hello, world!",
        recipients=devices,
    )


class TestBuildSupplementationEnvelope:
    def test_preserves_envelope_identity(self):
        sk = Ed25519KeyPair.generate()
        dev0, _ = _make_recipient()
        env, ckey = encrypt_message_with_content_key(
            _channel_input([dev0]), sk,
        )
        dev_new, _ = _make_recipient()
        supp = build_supplementation_envelope(env, ckey, [dev_new])

        assert supp["envelope_id"] == env["envelope_id"]
        assert supp["content_nonce"] == env["content_nonce"]
        assert supp["content_ciphertext"] == env["content_ciphertext"]
        assert supp["envelope_kind"] == env["envelope_kind"]
        assert supp["space_id"] == env["space_id"]
        assert supp["channel_id"] == env["channel_id"]

    def test_recipients_carry_only_new_devices(self):
        sk = Ed25519KeyPair.generate()
        dev0, _ = _make_recipient()
        env, ckey = encrypt_message_with_content_key(
            _channel_input([dev0]), sk,
        )
        dev_a, _ = _make_recipient()
        dev_b, _ = _make_recipient()
        supp = build_supplementation_envelope(env, ckey, [dev_a, dev_b])

        ids = {r["device_id"] for r in supp["recipients"]}
        assert ids == {dev_a.device_id, dev_b.device_id}
        assert dev0.device_id not in ids

    def test_supplementation_recipient_decrypts_to_same_plaintext(self):
        sk = Ed25519KeyPair.generate()
        dev0, kp0 = _make_recipient()
        env, ckey = encrypt_message_with_content_key(
            _channel_input([dev0]), sk,
        )
        dev_new, kp_new = _make_recipient()
        supp = build_supplementation_envelope(env, ckey, [dev_new])

        orig_msg = decrypt_message(env, dev0.device_id, kp0, sk.public_key_bytes())
        supp_msg = decrypt_message(supp, dev_new.device_id, kp_new, sk.public_key_bytes())
        assert orig_msg.content == supp_msg.content
        assert orig_msg.envelope_id == supp_msg.envelope_id

    def test_empty_devices_rejected(self):
        sk = Ed25519KeyPair.generate()
        dev0, _ = _make_recipient()
        env, ckey = encrypt_message_with_content_key(
            _channel_input([dev0]), sk,
        )
        with pytest.raises(ValueError):
            build_supplementation_envelope(env, ckey, [])


class _FakeHttp:
    def __init__(self, fresh_devices: list[RecipientDevice]) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self._fresh = fresh_devices

    async def post(self, path: str, body: dict) -> dict:
        self.posts.append((path, body))
        return {}

    async def get(self, path: str) -> dict:
        self.gets.append(path)
        from puffo_agent.crypto.encoding import base64url_encode
        return {
            "entries": [
                {
                    "kind": "device_cert",
                    "seq": i + 1,
                    "cert": {
                        "device_id": d.device_id,
                        "keys": {
                            "encryption": {
                                "public_key": base64url_encode(d.kem_public_key),
                            },
                        },
                    },
                }
                for i, d in enumerate(self._fresh)
            ],
            "has_more": False,
        }


@pytest.mark.asyncio
async def test_supplementation_posts_only_missing_devices():
    sk = Ed25519KeyPair.generate()
    dev_known, _ = _make_recipient()
    env, ckey = encrypt_message_with_content_key(
        _channel_input([dev_known]), sk,
    )

    dev_added, _ = _make_recipient()
    dev_other_user, _ = _make_recipient()
    http = _FakeHttp(fresh_devices=[dev_known, dev_added, dev_other_user])

    await _supplement_missing_devices(
        http, env, ckey,
        recipient_slugs=["alice-0001", "bob-0001"],
        missing_device_ids=[dev_added.device_id],
    )

    assert len(http.posts) == 1
    path, supp = http.posts[0]
    assert path == "/messages"
    assert supp["envelope_id"] == env["envelope_id"]
    ids = {r["device_id"] for r in supp["recipients"]}
    assert ids == {dev_added.device_id}


@pytest.mark.asyncio
async def test_supplementation_silent_when_missing_id_not_in_fresh_certs():
    # Device rotated out between the server's missing_devices report
    # and our /certs/sync refetch — drop, don't POST garbage.
    sk = Ed25519KeyPair.generate()
    dev_known, _ = _make_recipient()
    env, ckey = encrypt_message_with_content_key(
        _channel_input([dev_known]), sk,
    )
    http = _FakeHttp(fresh_devices=[dev_known])

    await _supplement_missing_devices(
        http, env, ckey,
        recipient_slugs=["alice-0001"],
        missing_device_ids=["dev_vanished_xx"],
    )

    assert http.posts == []  # no /messages POST attempted


@pytest.mark.asyncio
async def test_supplementation_swallows_http_failure():
    sk = Ed25519KeyPair.generate()
    dev_known, _ = _make_recipient()
    env, ckey = encrypt_message_with_content_key(
        _channel_input([dev_known]), sk,
    )
    dev_added, _ = _make_recipient()

    class _BoomHttp(_FakeHttp):
        async def post(self, path: str, body: dict) -> dict:
            raise RuntimeError("server unavailable")

    http = _BoomHttp(fresh_devices=[dev_known, dev_added])
    await _supplement_missing_devices(
        http, env, ckey,
        recipient_slugs=["alice-0001"],
        missing_device_ids=[dev_added.device_id],
    )


@pytest.mark.asyncio
async def test_channel_supplementation_uses_v2_and_preserves_freshness():
    coordinator, _, http = await coordinator_fixture(baseline=3)
    original, original_key = _make_recipient()
    env, content_key = encrypt_message_with_content_key(
        _channel_input([original]), Ed25519KeyPair.generate(),
    )
    added, _ = _make_recipient()

    async def fetch(_path):
        from puffo_agent.crypto.encoding import base64url_encode
        return {
            "entries": [{
                "kind": "device_cert",
                "seq": 1,
                "cert": {
                    "device_id": added.device_id,
                    "kem_public_key": base64url_encode(added.kem_public_key),
                },
            }],
            "has_more": False,
        }

    posts = []
    http.get = fetch

    async def post(path, body):
        posts.append((path, body))
        return {
            "state": "sent",
            "envelope_id": env["envelope_id"],
            "seq": 8,
            "replay": True,
            "missing_devices": [],
            "freshness": {
                "mode": "require_current",
                "context_baseline_seq": 3,
                "seen_seq": 3,
                "latest_seq_before_send": 3,
            },
        }

    http.post = post
    freshness = {
        "context_baseline_seq": 3,
        "seen_seq": 3,
        "mode": "require_current",
    }
    await coordinator._supplement_channel(
        env, content_key, ["alice-1"], [added.device_id], freshness,
    )
    assert len(posts) == 1
    path, body = posts[0]
    assert path == CHANNEL_SEND_PATH
    assert body["freshness"] == freshness
    supplement = body["envelope"]
    assert supplement["envelope_id"] == env["envelope_id"]
    assert supplement["content_nonce"] == env["content_nonce"]
    assert supplement["content_ciphertext"] == env["content_ciphertext"]
    assert all(path != "/messages" for path, _ in posts)
