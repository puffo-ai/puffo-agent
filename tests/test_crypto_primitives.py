import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.crypto.encoding import base64url_decode, base64url_encode, generate_nonce
from puffo_agent.crypto.primitives import (
    Ed25519KeyPair,
    KemKeyPair,
    aead_decrypt,
    aead_encrypt,
    ed25519_verify,
    generate_aead_nonce,
    generate_content_key,
    hpke_open,
    hpke_seal,
    MESSAGE_HPKE_INFO,
)

VECTORS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "puffo_agent", "crypto", "test_vectors.json"
)
RUST_VECTORS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "puffo_agent", "crypto", "test_vectors_from_rust.json"
)


def _load_vectors():
    with open(VECTORS_PATH) as f:
        return json.load(f)


# ---- Ed25519 ----


class TestEd25519:
    def test_sign_verify_roundtrip(self):
        kp = Ed25519KeyPair.generate()
        msg = b"hello world"
        sig = kp.sign(msg)
        assert ed25519_verify(kp.public_key_bytes(), msg, sig)

    def test_wrong_message_fails(self):
        kp = Ed25519KeyPair.generate()
        sig = kp.sign(b"correct")
        assert not ed25519_verify(kp.public_key_bytes(), b"wrong", sig)

    def test_deterministic_signature(self):
        kp = Ed25519KeyPair.generate()
        msg = b"deterministic"
        assert kp.sign(msg) == kp.sign(msg)

    def test_from_secret_bytes_roundtrip(self):
        kp = Ed25519KeyPair.generate()
        restored = Ed25519KeyPair.from_secret_bytes(kp.secret_bytes())
        assert restored.public_key_bytes() == kp.public_key_bytes()
        msg = b"roundtrip"
        assert restored.sign(msg) == kp.sign(msg)

    def test_vectors_deterministic(self):
        vectors = _load_vectors()["ed25519"]
        secret = base64url_decode(vectors["secret_key"])
        expected_pk = base64url_decode(vectors["public_key"])

        kp = Ed25519KeyPair.from_secret_bytes(secret)
        assert kp.public_key_bytes() == expected_pk

        for sv in vectors["signatures"]:
            message = base64url_decode(sv["message"])
            expected_sig = base64url_decode(sv["signature"])
            actual_sig = kp.sign(message)
            assert actual_sig == expected_sig, (
                f"Signature mismatch for: {sv.get('message_utf8', '?')}"
            )
            assert ed25519_verify(expected_pk, message, expected_sig)


# ---- ChaCha20Poly1305 ----


class TestChaCha20Poly1305:
    def test_encrypt_decrypt_roundtrip(self):
        key = generate_content_key()
        nonce = generate_aead_nonce()
        aad = b"alice\nch_1\nchannel"
        pt = b"hello world"
        ct = aead_encrypt(key, nonce, pt, aad)
        assert aead_decrypt(key, nonce, ct, aad) == pt

    def test_aad_mismatch_fails(self):
        key = generate_content_key()
        nonce = generate_aead_nonce()
        ct = aead_encrypt(key, nonce, b"data", b"correct-aad")
        try:
            aead_decrypt(key, nonce, ct, b"wrong-aad")
            assert False, "should fail"
        except Exception:
            pass

    def test_wrong_key_fails(self):
        key1 = generate_content_key()
        key2 = generate_content_key()
        nonce = generate_aead_nonce()
        ct = aead_encrypt(key1, nonce, b"data", b"aad")
        try:
            aead_decrypt(key2, nonce, ct, b"aad")
            assert False, "should fail"
        except Exception:
            pass

    def test_vectors_deterministic(self):
        vectors = _load_vectors()["chacha20poly1305"]
        key = base64url_decode(vectors["key"])
        nonce = base64url_decode(vectors["nonce"])
        aad = base64url_decode(vectors["aad"])
        pt = base64url_decode(vectors["plaintext"])
        expected_ct = base64url_decode(vectors["ciphertext"])

        actual_ct = aead_encrypt(key, nonce, pt, aad)
        assert actual_ct == expected_ct, "ChaCha20Poly1305 ciphertext mismatch"

        decrypted = aead_decrypt(key, nonce, expected_ct, aad)
        assert decrypted == pt


# ---- HPKE ----


class TestHpke:
    def test_seal_open_roundtrip(self):
        kp = KemKeyPair.generate()
        info = MESSAGE_HPKE_INFO
        aad = b"env_test\ndev_test"
        pt = generate_content_key()
        output = hpke_seal(kp.public_key_bytes(), info, aad, pt)
        assert hpke_open(kp, output.enc, info, aad, output.ciphertext) == pt

    def test_wrong_key_fails(self):
        kp1 = KemKeyPair.generate()
        kp2 = KemKeyPair.generate()
        output = hpke_seal(kp1.public_key_bytes(), MESSAGE_HPKE_INFO, b"aad", b"secret")
        try:
            hpke_open(kp2, output.enc, MESSAGE_HPKE_INFO, b"aad", output.ciphertext)
            assert False, "should fail"
        except Exception:
            pass

    def test_aad_mismatch_fails(self):
        kp = KemKeyPair.generate()
        output = hpke_seal(kp.public_key_bytes(), MESSAGE_HPKE_INFO, b"aad1", b"secret")
        try:
            hpke_open(kp, output.enc, MESSAGE_HPKE_INFO, b"aad2", output.ciphertext)
            assert False, "should fail"
        except Exception:
            pass

    def test_info_mismatch_fails(self):
        kp = KemKeyPair.generate()
        output = hpke_seal(kp.public_key_bytes(), b"info-a", b"aad", b"secret")
        try:
            hpke_open(kp, output.enc, b"info-b", b"aad", output.ciphertext)
            assert False, "should fail"
        except Exception:
            pass

    def test_from_secret_bytes_roundtrip(self):
        kp = KemKeyPair.generate()
        restored = KemKeyPair.from_secret_bytes(kp.secret_bytes())
        assert restored.public_key_bytes() == kp.public_key_bytes()
        output = hpke_seal(kp.public_key_bytes(), MESSAGE_HPKE_INFO, b"aad", b"data")
        assert hpke_open(restored, output.enc, MESSAGE_HPKE_INFO, b"aad", output.ciphertext) == b"data"

    def test_vectors_python_generated(self):
        vectors = _load_vectors()["hpke"]
        sk = base64url_decode(vectors["recipient_secret_key"])
        expected_pk = base64url_decode(vectors["recipient_public_key"])

        kp = KemKeyPair.from_secret_bytes(sk)
        assert kp.public_key_bytes() == expected_pk

        enc = base64url_decode(vectors["enc"])
        ct = base64url_decode(vectors["ciphertext"])
        info = base64url_decode(vectors["info"])
        aad = base64url_decode(vectors["aad"])
        expected_pt = base64url_decode(vectors["plaintext"])

        decrypted = hpke_open(kp, enc, info, aad, ct)
        assert decrypted == expected_pt

    def test_vectors_from_rust(self):
        if not os.path.exists(RUST_VECTORS_PATH):
            return  # skip if Rust hasn't generated vectors yet.

        with open(RUST_VECTORS_PATH) as f:
            vectors = json.load(f)

        sk = base64url_decode(vectors["recipient_secret_key"])
        kp = KemKeyPair.from_secret_bytes(sk)

        expected_pk = base64url_decode(vectors["recipient_public_key"])
        assert kp.public_key_bytes() == expected_pk

        enc = base64url_decode(vectors["enc"])
        ct = base64url_decode(vectors["ciphertext"])
        info = base64url_decode(vectors["info"])
        aad = base64url_decode(vectors["aad"])
        expected_pt = base64url_decode(vectors["plaintext"])

        decrypted = hpke_open(kp, enc, info, aad, ct)
        assert decrypted == expected_pt, "Failed to decrypt Rust-generated HPKE ciphertext"


# ---- base64url ----


class TestBase64url:
    def test_vectors(self):
        vectors = _load_vectors()["base64url"]
        for case in vectors["cases"]:
            encoded = case["encoded"]
            if "raw" in case:
                raw = case["raw"].encode()
                assert base64url_encode(raw) == encoded
                assert base64url_decode(encoded) == raw
            elif "raw_hex" in case:
                raw = bytes.fromhex(case["raw_hex"])
                assert base64url_encode(raw) == encoded

    def test_no_padding(self):
        for length in range(20):
            data = os.urandom(length)
            encoded = base64url_encode(data)
            assert "=" not in encoded
            assert base64url_decode(encoded) == data

    def test_generate_nonce_length(self):
        n = generate_nonce()
        assert len(n) == 22
        decoded = base64url_decode(n)
        assert len(decoded) == 16
