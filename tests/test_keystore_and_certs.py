import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.crypto.canonical import canonicalize, canonicalize_for_signing
from puffo_agent.crypto.certs import (
    SUBKEY_TTL_HOURS,
    create_subkey_cert,
    is_subkey_expired,
    needs_rotation,
)
from puffo_agent.crypto.encoding import base64url_decode, base64url_encode
from puffo_agent.crypto.http_auth import sign_request
from puffo_agent.crypto.keystore import (
    KeyStore,
    Session,
    StoredIdentity,
    decode_secret,
    encode_secret,
)
from puffo_agent.crypto.primitives import Ed25519KeyPair, ed25519_verify


# ---- Canonical JSON ----


class TestCanonical:
    def test_sort_keys(self):
        result = canonicalize({"b": 2, "a": 1}).decode()
        assert result == '{"a":1,"b":2}'

    def test_nested_sorted(self):
        result = canonicalize({"z": {"b": 2, "a": 1}, "a": 0}).decode()
        assert result == '{"a":0,"z":{"a":1,"b":2}}'

    def test_array_preserved(self):
        assert canonicalize([3, 1, 2]).decode() == "[3,1,2]"

    def test_string_escaping(self):
        result = canonicalize({"msg": "line1\nline2\ttab"}).decode()
        assert result == '{"msg":"line1\\nline2\\ttab"}'

    def test_strip_signature(self):
        result = canonicalize_for_signing({
            "type": "cert",
            "signature": "sig",
            "self_signature": "selfsig",
            "data": "keep",
        }).decode()
        assert "signature" not in result
        assert "self_signature" not in result
        assert "data" in result

    def test_cross_language_vector(self):
        value = {
            "type": "identity_cert",
            "version": 1,
            "root_public_key": "dGVzdA",
            "identity_type": "human",
            "username": "alice",
            "created_at": 1700000000000,
        }
        result = canonicalize(value).decode()
        expected = '{"created_at":1700000000000,"identity_type":"human","root_public_key":"dGVzdA","type":"identity_cert","username":"alice","version":1}'
        assert result == expected


# ---- KeyStore ----


class TestKeyStore:
    def _temp_store(self):
        d = tempfile.mkdtemp()
        return KeyStore(os.path.join(d, "keys")), d

    def _sample_identity(self, slug="alice-0001"):
        return StoredIdentity(
            slug=slug,
            device_id="dev_test",
            root_secret_key=encode_secret(bytes(32)),
            device_signing_secret_key=encode_secret(bytes(range(32))),
            kem_secret_key=encode_secret(bytes([3] * 32)),
            server_url="http://localhost:3000",
        )

    def test_save_load_roundtrip(self):
        store, _ = self._temp_store()
        identity = self._sample_identity()
        store.save_identity(identity)
        loaded = store.load_identity("alice-0001")
        assert loaded.slug == "alice-0001"
        assert loaded.device_id == "dev_test"
        assert loaded.root_secret_key == identity.root_secret_key

    def test_load_nonexistent(self):
        store, _ = self._temp_store()
        try:
            store.load_identity("ghost")
            assert False
        except FileNotFoundError:
            pass

    def test_list_identities(self):
        store, _ = self._temp_store()
        store.save_identity(self._sample_identity("bob-0002"))
        store.save_identity(self._sample_identity("alice-0001"))
        assert store.list_identities() == ["alice-0001", "bob-0002"]

    def test_delete_identity(self):
        store, _ = self._temp_store()
        store.save_identity(self._sample_identity())
        store.delete_identity("alice-0001")
        try:
            store.load_identity("alice-0001")
            assert False
        except FileNotFoundError:
            pass

    def test_session_save_load(self):
        store, _ = self._temp_store()
        session = Session(
            slug="alice-0001",
            subkey_id="sk_test",
            subkey_secret_key=encode_secret(bytes([5] * 32)),
            expires_at=int(time.time() * 1000) + 3_600_000,
        )
        store.save_session(session)
        loaded = store.load_session("alice-0001")
        assert loaded.subkey_id == "sk_test"

    def test_expired_session_deleted(self):
        store, _ = self._temp_store()
        session = Session(
            slug="alice-0001",
            subkey_id="sk_old",
            subkey_secret_key=encode_secret(bytes([5] * 32)),
            expires_at=1000,  # long expired
        )
        store.save_session(session)
        try:
            store.load_session("alice-0001")
            assert False
        except FileNotFoundError:
            pass

    def test_default_identity(self):
        store, _ = self._temp_store()
        assert store.default_identity() is None
        store.save_identity(self._sample_identity())
        assert store.default_identity() == "alice-0001"

    def test_encode_decode_secret(self):
        original = bytes(range(32))
        encoded = encode_secret(original)
        decoded = decode_secret(encoded)
        assert decoded == original


# ---- SubkeyCert ----


class TestSubkeyCert:
    def test_create_cert(self):
        device_key = Ed25519KeyPair.generate()
        subkey = Ed25519KeyPair.generate()
        cert = create_subkey_cert(device_key, "dev_test", subkey.public_key_bytes())

        assert cert["type"] == "subkey_cert"
        assert cert["version"] == 1
        assert cert["subkey_id"].startswith("sk_")
        assert cert["device_id"] == "dev_test"
        assert cert["signature"] != ""

    def test_cert_signature_verifiable(self):
        device_key = Ed25519KeyPair.generate()
        subkey = Ed25519KeyPair.generate()
        cert = create_subkey_cert(device_key, "dev_test", subkey.public_key_bytes())

        sig = base64url_decode(cert["signature"])
        canonical = canonicalize_for_signing(cert)
        assert ed25519_verify(device_key.public_key_bytes(), canonical, sig)

    def test_tampered_cert_fails_verification(self):
        device_key = Ed25519KeyPair.generate()
        subkey = Ed25519KeyPair.generate()
        cert = create_subkey_cert(device_key, "dev_test", subkey.public_key_bytes())

        other_key = Ed25519KeyPair.generate()
        cert["subkey_public_key"] = base64url_encode(other_key.public_key_bytes())

        sig = base64url_decode(cert["signature"])
        canonical = canonicalize_for_signing(cert)
        assert not ed25519_verify(device_key.public_key_bytes(), canonical, sig)

    def test_expiry(self):
        device_key = Ed25519KeyPair.generate()
        subkey = Ed25519KeyPair.generate()
        issued = 1_000_000_000_000
        cert = create_subkey_cert(
            device_key, "dev_test", subkey.public_key_bytes(),
            ttl_hours=24, issued_at=issued,
        )

        assert cert["issued_at"] == issued
        assert cert["expires_at"] == issued + 24 * 3_600_000
        assert not is_subkey_expired(cert, issued)
        assert is_subkey_expired(cert, cert["expires_at"])

    def test_needs_rotation(self):
        future = int(time.time() * 1000) + 3_600_000
        assert not needs_rotation(future)

        near = int(time.time() * 1000) + 60_000  # 1 minute from now
        assert needs_rotation(near)

        past = int(time.time() * 1000) - 1000
        assert needs_rotation(past)


# ---- HTTP Auth ----


class TestHttpAuth:
    def test_sign_request_format(self):
        key = Ed25519KeyPair.generate()
        auth = sign_request(key, "alice", "sk_test", "POST", "/messages", b"hello")

        assert auth.version == "v1"
        assert auth.slug == "alice"
        assert auth.signer_id == "sk_test"
        assert auth.timestamp.isdigit()
        assert len(auth.nonce) == 22  # 16 bytes -> 22 base64url chars
        assert len(auth.signature) > 0

    def test_sign_request_deterministic_with_fixed_params(self):
        key = Ed25519KeyPair.generate()
        nonce = "AAAAAAAAAAAAAAAAAAAAAA"
        ts = 1700000000000

        auth1 = sign_request(key, "alice", "sk_test", "POST", "/x", b"body", timestamp_ms=ts, nonce=nonce)
        auth2 = sign_request(key, "alice", "sk_test", "POST", "/x", b"body", timestamp_ms=ts, nonce=nonce)

        assert auth1.signature == auth2.signature

    def test_sign_request_verifiable(self):
        key = Ed25519KeyPair.generate()
        nonce = base64url_encode(bytes(16))
        ts = 1700000000000

        auth = sign_request(key, "alice", "sk_test", "POST", "/messages", b"hello", timestamp_ms=ts, nonce=nonce)

        expected_msg = f"POST\n/messages\n{ts}\n{nonce}\n".encode() + b"hello"
        sig = base64url_decode(auth.signature)
        assert ed25519_verify(key.public_key_bytes(), expected_msg, sig)

    def test_different_body_different_signature(self):
        key = Ed25519KeyPair.generate()
        nonce = base64url_encode(bytes(16))
        ts = 1700000000000

        auth1 = sign_request(key, "alice", "sk_test", "POST", "/x", b"body1", timestamp_ms=ts, nonce=nonce)
        auth2 = sign_request(key, "alice", "sk_test", "POST", "/x", b"body2", timestamp_ms=ts, nonce=nonce)

        assert auth1.signature != auth2.signature

    def test_to_dict(self):
        key = Ed25519KeyPair.generate()
        auth = sign_request(key, "alice", "sk_test", "GET", "/health")
        d = auth.to_dict()
        assert d["x-puffo-version"] == "v1"
        assert d["x-puffo-slug"] == "alice"
        assert d["x-puffo-signer-id"] == "sk_test"
        assert "x-puffo-timestamp" in d
        assert "x-puffo-nonce" in d
        assert "x-puffo-signature" in d
