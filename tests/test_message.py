import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.crypto.encoding import base64url_decode, base64url_encode
from puffo_agent.crypto.message import (
    EncryptInput,
    RecipientDevice,
    decrypt_message,
    encrypt_message,
)
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair


def _make_recipient() -> tuple[RecipientDevice, KemKeyPair]:
    kp = KemKeyPair.generate()
    return RecipientDevice(
        device_id=f"dev_{os.urandom(4).hex()}",
        kem_public_key=kp.public_key_bytes(),
    ), kp


def _channel_input(recipients: list[RecipientDevice]) -> EncryptInput:
    return EncryptInput(
        envelope_kind="channel",
        sender_slug="alice-0001",
        sender_subkey_id="sk_alice",
        space_id="sp_1",
        channel_id="ch_1",
        content_type="text/plain",
        content="Hello, world!",
        recipients=recipients,
    )


def _dm_input(recipients: list[RecipientDevice]) -> EncryptInput:
    return EncryptInput(
        envelope_kind="dm",
        sender_slug="alice-0001",
        sender_subkey_id="sk_alice",
        recipient_slug="bob-0001",
        content_type="text/plain",
        content="Secret message",
        recipients=recipients,
    )


class TestEncryptMessage:
    def test_envelope_structure(self):
        signing_key = Ed25519KeyPair.generate()
        dev, _ = _make_recipient()
        env = encrypt_message(_channel_input([dev]), signing_key)

        assert env["type"] == "message_envelope"
        assert env["version"] == 1
        assert env["envelope_id"].startswith("msg_")
        assert env["envelope_kind"] == "channel"
        assert env["sender_slug"] == "alice-0001"
        assert env["space_id"] == "sp_1"
        assert env["channel_id"] == "ch_1"
        assert len(env["recipients"]) == 1
        assert env["content_nonce"] != ""
        assert env["content_ciphertext"] != ""

    def test_dm_envelope(self):
        signing_key = Ed25519KeyPair.generate()
        dev, _ = _make_recipient()
        env = encrypt_message(_dm_input([dev]), signing_key)

        assert env["envelope_kind"] == "dm"
        assert env["recipient_slug"] == "bob-0001"
        # Route fields always present; null for the inactive route.
        assert env["channel_id"] is None
        assert env["space_id"] is None

    def test_no_recipients_raises(self):
        signing_key = Ed25519KeyPair.generate()
        try:
            encrypt_message(_channel_input([]), signing_key)
            assert False
        except ValueError as e:
            assert "no recipients" in str(e)

    def test_multiple_recipients_unique_hpke(self):
        signing_key = Ed25519KeyPair.generate()
        dev1, _ = _make_recipient()
        dev2, _ = _make_recipient()
        dev3, _ = _make_recipient()
        env = encrypt_message(_channel_input([dev1, dev2, dev3]), signing_key)

        assert len(env["recipients"]) == 3
        encs = [r["hpke_enc"] for r in env["recipients"]]
        assert len(set(encs)) == 3

    def test_ciphertext_is_not_plaintext(self):
        signing_key = Ed25519KeyPair.generate()
        dev, _ = _make_recipient()
        env = encrypt_message(_channel_input([dev]), signing_key)

        ct = base64url_decode(env["content_ciphertext"])
        assert b"Hello, world!" not in ct

    def test_two_encryptions_differ(self):
        signing_key = Ed25519KeyPair.generate()
        dev1, _ = _make_recipient()
        dev2, _ = _make_recipient()
        env1 = encrypt_message(_channel_input([dev1]), signing_key)
        env2 = encrypt_message(_channel_input([dev2]), signing_key)

        assert env1["content_ciphertext"] != env2["content_ciphertext"]
        assert env1["content_nonce"] != env2["content_nonce"]


class TestDecryptMessage:
    def test_roundtrip_channel(self):
        signing_key = Ed25519KeyPair.generate()
        dev, kp = _make_recipient()
        env = encrypt_message(_channel_input([dev]), signing_key)

        payload = decrypt_message(env, dev.device_id, kp, signing_key.public_key_bytes())

        assert payload.content == "Hello, world!"
        assert payload.sender_slug == "alice-0001"
        assert payload.sender_subkey_id == "sk_alice"
        assert payload.content_type == "text/plain"
        assert payload.envelope_id == env["envelope_id"]
        assert payload.envelope_kind == "channel"
        assert payload.space_id == "sp_1"
        assert payload.channel_id == "ch_1"

    def test_roundtrip_dm(self):
        signing_key = Ed25519KeyPair.generate()
        dev, kp = _make_recipient()
        env = encrypt_message(_dm_input([dev]), signing_key)

        payload = decrypt_message(env, dev.device_id, kp, signing_key.public_key_bytes())

        assert payload.content == "Secret message"
        assert payload.envelope_kind == "dm"
        assert payload.recipient_slug == "bob-0001"

    def test_wrong_device_id(self):
        signing_key = Ed25519KeyPair.generate()
        dev, kp = _make_recipient()
        env = encrypt_message(_channel_input([dev]), signing_key)

        try:
            decrypt_message(env, "dev_wrong", kp, signing_key.public_key_bytes())
            assert False
        except ValueError as e:
            assert "no recipient entry" in str(e)

    def test_wrong_kem_key(self):
        signing_key = Ed25519KeyPair.generate()
        dev, _ = _make_recipient()
        wrong_kp = KemKeyPair.generate()
        env = encrypt_message(_channel_input([dev]), signing_key)

        try:
            decrypt_message(env, dev.device_id, wrong_kp, signing_key.public_key_bytes())
            assert False
        except Exception:
            pass

    def test_wrong_verifying_key(self):
        signing_key = Ed25519KeyPair.generate()
        wrong_key = Ed25519KeyPair.generate()
        dev, kp = _make_recipient()
        env = encrypt_message(_channel_input([dev]), signing_key)

        try:
            decrypt_message(env, dev.device_id, kp, wrong_key.public_key_bytes())
            assert False
        except ValueError as e:
            assert "signature verification failed" in str(e)

    def test_tampered_ciphertext(self):
        signing_key = Ed25519KeyPair.generate()
        dev, kp = _make_recipient()
        env = encrypt_message(_channel_input([dev]), signing_key)

        ct = bytearray(base64url_decode(env["content_ciphertext"]))
        ct[0] ^= 0xFF
        env["content_ciphertext"] = base64url_encode(bytes(ct))

        try:
            decrypt_message(env, dev.device_id, kp, signing_key.public_key_bytes())
            assert False
        except Exception:
            pass

    def test_tampered_envelope_id_fails_hpke(self):
        signing_key = Ed25519KeyPair.generate()
        dev, kp = _make_recipient()
        env = encrypt_message(_channel_input([dev]), signing_key)

        env["envelope_id"] = "msg_tampered"

        try:
            decrypt_message(env, dev.device_id, kp, signing_key.public_key_bytes())
            assert False
        except Exception:
            pass

    def test_tampered_sender_slug_fails_aead(self):
        signing_key = Ed25519KeyPair.generate()
        dev, kp = _make_recipient()
        env = encrypt_message(_channel_input([dev]), signing_key)

        env["sender_slug"] = "eve-0001"

        try:
            decrypt_message(env, dev.device_id, kp, signing_key.public_key_bytes())
            assert False
        except Exception:
            pass

    def test_tampered_channel_id_fails_aead(self):
        signing_key = Ed25519KeyPair.generate()
        dev, kp = _make_recipient()
        env = encrypt_message(_channel_input([dev]), signing_key)

        env["channel_id"] = "ch_evil"

        try:
            decrypt_message(env, dev.device_id, kp, signing_key.public_key_bytes())
            assert False
        except Exception:
            pass

    def test_multi_recipient_each_decrypts(self):
        signing_key = Ed25519KeyPair.generate()
        dev1, kp1 = _make_recipient()
        dev2, kp2 = _make_recipient()
        dev3, kp3 = _make_recipient()

        inp = _dm_input([dev1, dev2, dev3])
        env = encrypt_message(inp, signing_key)

        for dev, kp in [(dev1, kp1), (dev2, kp2), (dev3, kp3)]:
            payload = decrypt_message(env, dev.device_id, kp, signing_key.public_key_bytes())
            assert payload.content == "Secret message"
            assert payload.envelope_kind == "dm"

    def test_cross_device_key_fails(self):
        signing_key = Ed25519KeyPair.generate()
        dev1, kp1 = _make_recipient()
        dev2, kp2 = _make_recipient()

        env = encrypt_message(_channel_input([dev1, dev2]), signing_key)

        try:
            decrypt_message(env, dev1.device_id, kp2, signing_key.public_key_bytes())
            assert False
        except Exception:
            pass

    def test_threading_fields_roundtrip(self):
        signing_key = Ed25519KeyPair.generate()
        dev, kp = _make_recipient()
        inp = _channel_input([dev])
        inp.thread_root_id = "env_root_abc"
        inp.reply_to_id = "env_parent_def"

        env = encrypt_message(inp, signing_key)
        payload = decrypt_message(env, dev.device_id, kp, signing_key.public_key_bytes())

        assert payload.thread_root_id == "env_root_abc"
        assert payload.reply_to_id == "env_parent_def"

    def test_threading_fields_none_by_default(self):
        signing_key = Ed25519KeyPair.generate()
        dev, kp = _make_recipient()
        env = encrypt_message(_channel_input([dev]), signing_key)

        payload = decrypt_message(env, dev.device_id, kp, signing_key.public_key_bytes())

        assert payload.thread_root_id is None
        assert payload.reply_to_id is None
