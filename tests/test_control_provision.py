from __future__ import annotations

import pytest

from puffo_agent.crypto.canonical import canonicalize_for_signing
from puffo_agent.crypto.encoding import base64url_decode, base64url_encode
from puffo_agent.crypto.primitives import Ed25519KeyPair
from puffo_agent.portal.control import certs, provision
from puffo_agent.portal.control.provision import (
    ProvisionError,
    provision_agent_from_bundle,
    verify_agent_bundle,
    write_agent_from_context,
)
from puffo_agent.portal.state import AgentConfig


def _signed(key: Ed25519KeyPair, value: dict, signature_field: str) -> dict:
    value[signature_field] = base64url_encode(key.sign(canonicalize_for_signing(value)))
    return value


def _payload():
    operator = Ed25519KeyPair.generate()
    agent = Ed25519KeyPair.generate()
    device = Ed25519KeyPair.generate()
    operator_public = base64url_encode(operator.public_key_bytes())
    agent_public = base64url_encode(agent.public_key_bytes())
    slug = "helper-1234"
    device_id = "dev_helper"
    identity_cert = _signed(
        agent,
        {
            "type": "identity_cert",
            "version": 1,
            "root_public_key": agent_public,
            "identity_type": "agent",
            "declared_operator_public_key": operator_public,
        },
        "self_signature",
    )
    device_cert = _signed(
        agent,
        {
            "type": "device_cert",
            "version": 1,
            "device_id": device_id,
            "root_public_key": agent_public,
            "keys": {
                "signing": {
                    "algorithm": "ed25519",
                    "public_key": base64url_encode(device.public_key_bytes()),
                },
                "encryption": {
                    "algorithm": "x25519",
                    "public_key": base64url_encode(b"k" * 32),
                },
            },
            "issued_at": 1,
            "expires_at": None,
        },
        "signature",
    )
    slug_binding = _signed(
        agent,
        {
            "type": "slug_binding",
            "version": 1,
            "slug": slug,
            "root_public_key": agent_public,
            "issued_at": 1,
        },
        "self_signature",
    )
    attestation = _signed(
        operator,
        {
            "type": "operator_attestation",
            "operator_root_public_key": operator_public,
            "agent_root_public_key": agent_public,
        },
        "signature",
    )
    payload = {
        "identity_bundle": {
            "identity_cert": identity_cert,
            "device_cert": device_cert,
            "slug_binding": slug_binding,
            "operator_attestation": attestation,
            "root_secret_key": base64url_encode(agent.secret_bytes()),
            "device_signing_secret_key": base64url_encode(device.secret_bytes()),
            "kem_secret_key": base64url_encode(b"s" * 32),
        },
        "puffo_core": {
            "server_url": "https://relay.example",
            "slug": slug,
            "device_id": device_id,
            "space_id": "space_1",
            "operator_slug": "owner-1",
        },
        "runtime": {"kind": "ws-local", "provider": "", "harness": ""},
        "display_name": "Helper",
        "role": "coder: writes code",
        "role_short": "stale",
        "profile": "# Helper\n\n# Soul\n\nUseful.\n",
        "desired_skills": ["skill-a"],
        "desired_mcps": ["mcp-a"],
    }
    return payload, operator_public


def test_verify_valid_bundle_derives_role_short(caplog):
    payload, operator_public = _payload()
    context = verify_agent_bundle(payload, operator_public)
    assert context["agent_id"] == "helper-1234"
    assert context["role_short"] == "coder"
    assert "ignoring deprecated role_short" in caplog.text


def test_verify_valid_bundle_without_role_short_override():
    payload, operator_public = _payload()
    del payload["role_short"]
    assert verify_agent_bundle(payload, operator_public)["role_short"] == "coder"


def test_verify_rejects_invalid_agent_id(monkeypatch):
    payload, operator_public = _payload()
    monkeypatch.setattr(provision, "is_valid_agent_id", lambda _slug: False)
    with pytest.raises(ProvisionError, match="not a valid agent id"):
        verify_agent_bundle(payload, operator_public)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda value: value.update({"runtime": {"kind": "bogus"}}), "runtime"),
        (
            lambda value: value["runtime"].update({"inference_level": "turbo"}),
            "inference_level",
        ),
        (lambda value: value.update({"desired_skills": [""]}), "desired_skills"),
        (lambda value: value["puffo_core"].update({"device_id": "wrong"}), "device_id"),
        (lambda value: value.update({"role": "", "role_short": "coder"}), "role_short"),
    ],
)
def test_verify_rejects_invalid_bundle(mutation, error):
    payload, operator_public = _payload()
    mutation(payload)
    with pytest.raises(ProvisionError, match=error):
        verify_agent_bundle(payload, operator_public)


def test_verify_rejects_wrong_operator():
    payload, _ = _payload()
    other_operator = base64url_encode(Ed25519KeyPair.generate().public_key_bytes())
    with pytest.raises(ProvisionError, match="paired operator"):
        verify_agent_bundle(payload, other_operator)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda payload: payload.update({"identity_bundle": None}), "identity_bundle"),
        (
            lambda payload: payload["identity_bundle"].update({"device_cert": None}),
            "identity_bundle missing",
        ),
        (lambda payload: payload["puffo_core"].update({"space_id": ""}), "must include"),
        (
            lambda payload: payload["puffo_core"].update({"slug": "other-1234"}),
            "slug_binding",
        ),
        (lambda payload: payload["puffo_core"].update({"slug": "Bad Slug"}), "slug"),
        (lambda payload: payload.update({"role_short": 3}), "role_short must"),
        (lambda payload: payload.update({"role": "x" * 141}), "role must"),
        (lambda payload: payload.update({"role_short": "x" * 33}), "role_short must"),
        (lambda payload: payload.update({"profile": ""}), "profile"),
        (lambda payload: payload.update({"desired_mcps": [""]}), "desired_mcps"),
    ],
)
def test_verify_rejects_invalid_shapes(mutate, error):
    payload, operator_public = _payload()
    mutate(payload)
    with pytest.raises(ProvisionError, match=error):
        verify_agent_bundle(payload, operator_public)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"identity_type": "human"}, "identity_type"),
        ({"declared_operator_public_key": ""}, "declared_operator_public_key required"),
    ],
)
def test_verify_rejects_invalid_signed_identity_fields(monkeypatch, changes, error):
    payload, operator_public = _payload()
    identity = payload["identity_bundle"]["identity_cert"]
    identity.update(changes)
    monkeypatch.setattr(
        provision,
        "verify_identity_cert",
        lambda cert: base64url_decode(cert["root_public_key"]),
    )
    with pytest.raises(ProvisionError, match=error):
        verify_agent_bundle(payload, operator_public)


def test_verify_rejects_non_object_and_bad_operator_key():
    with pytest.raises(ProvisionError, match="body must"):
        verify_agent_bundle([], base64url_encode(b"o" * 32))
    with pytest.raises(ProvisionError, match="root pubkey decode"):
        verify_agent_bundle({}, "x")


def test_verify_wraps_bad_identity_and_attestation():
    payload, operator_public = _payload()
    payload["identity_bundle"]["identity_cert"]["self_signature"] = ""
    with pytest.raises(ProvisionError, match="identity_cert"):
        verify_agent_bundle(payload, operator_public)

    payload, operator_public = _payload()
    payload["identity_bundle"]["operator_attestation"]["signature"] = ""
    with pytest.raises(ProvisionError, match="signature"):
        verify_agent_bundle(payload, operator_public)


def test_attestation_validation_failures():
    payload, operator_public = _payload()
    bundle = payload["identity_bundle"]
    agent_key = base64url_decode(bundle["identity_cert"]["root_public_key"])
    operator_key = base64url_decode(operator_public)
    valid = bundle["operator_attestation"]
    cases = [
        (None, "must be an object"),
        ({**valid, "type": "wrong"}, "unexpected attestation"),
        ({**valid, "signature": "x"}, "field decode"),
        (
            {**valid, "operator_root_public_key": base64url_encode(b"x" * 32)},
            "paired user",
        ),
        (
            {**valid, "agent_root_public_key": base64url_encode(b"x" * 32)},
            "agent identity_cert",
        ),
        ({**valid, "signature": base64url_encode(b"x" * 64)}, "signature verification"),
    ]
    for value, error in cases:
        with pytest.raises(certs.CertError, match=error):
            provision._verify_attestation(value, agent_key, operator_key)


def test_certificate_shape_and_signature_failures():
    payload, _ = _payload()
    bundle = payload["identity_bundle"]
    identity = bundle["identity_cert"]
    device = bundle["device_cert"]
    binding = bundle["slug_binding"]
    root_key = base64url_decode(identity["root_public_key"])

    decode_cases = [
        (None, "missing"),
        ("x", "decode"),
        (base64url_encode(b"short"), "must be 32 bytes"),
    ]
    for value, error in decode_cases:
        with pytest.raises(certs.CertError, match=error):
            certs._decode(value, "field", 32)

    identity_cases = [
        (None, "must be an object"),
        ({**identity, "type": "wrong"}, "unexpected cert_type"),
        ({**identity, "self_signature": base64url_encode(b"x" * 64)}, "verification"),
    ]
    for value, error in identity_cases:
        with pytest.raises(certs.CertError, match=error):
            certs.verify_identity_cert(value)

    binding_cases = [
        (None, "must be an object"),
        ({**binding, "type": "wrong"}, "unexpected slug_binding"),
        ({**binding, "slug": ""}, "missing slug"),
        ({**binding, "root_public_key": base64url_encode(b"x" * 32)}, "does not match"),
        ({**binding, "self_signature": base64url_encode(b"x" * 64)}, "verification"),
    ]
    for value, error in binding_cases:
        with pytest.raises(certs.CertError, match=error):
            certs.verify_slug_binding(value, root_key)

    device_cases = [
        (None, "must be an object"),
        ({**device, "type": "wrong"}, "unexpected cert_type"),
        ({**device, "root_public_key": base64url_encode(b"x" * 32)}, "does not match"),
        ({**device, "keys": None}, "signing missing"),
        (
            {**device, "keys": {"signing": {"algorithm": "rsa"}}},
            "algorithm must",
        ),
        ({**device, "device_id": ""}, "missing device_id"),
        ({**device, "signature": base64url_encode(b"x" * 64)}, "verification"),
    ]
    for value, error in device_cases:
        with pytest.raises(certs.CertError, match=error):
            certs.verify_device_cert(value, root_key)


@pytest.mark.asyncio
async def test_provision_materializes_then_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    payload, operator_public = _payload()
    materialized = []

    async def materialize(context):
        assert not (tmp_path / "agents" / context["agent_id"]).exists()
        materialized.append(context["agent_id"])

    result = await provision_agent_from_bundle(
        payload, operator_public, materialize=materialize,
    )
    assert materialized == ["helper-1234"]
    assert result["agent_id"] == "helper-1234"
    config = AgentConfig.load("helper-1234")
    assert config.runtime.kind == "ws-local"
    assert config.desired_skills == ["skill-a"]
    assert (tmp_path / "agents/helper-1234/keys/helper-1234.json").is_file()


@pytest.mark.asyncio
async def test_provision_without_materialize(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    payload, operator_public = _payload()
    result = await provision_agent_from_bundle(payload, operator_public)
    assert result["agent_id"] == "helper-1234"


def test_existing_directory_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    payload, operator_public = _payload()
    context = verify_agent_bundle(payload, operator_public)
    existing = tmp_path / "agents/helper-1234"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("user data", encoding="utf-8")
    with pytest.raises(ProvisionError, match="already exists"):
        write_agent_from_context(context)
    assert marker.read_text(encoding="utf-8") == "user data"


def test_existing_agent_config_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    payload, operator_public = _payload()
    context = verify_agent_bundle(payload, operator_public)
    config_path = tmp_path / "agents/helper-1234/agent.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("id: helper-1234\n", encoding="utf-8")
    with pytest.raises(ProvisionError, match="already exists"):
        write_agent_from_context(context)


def test_partial_write_is_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    payload, operator_public = _payload()
    context = verify_agent_bundle(payload, operator_public)
    del context["bundle"]["kem_secret_key"]
    with pytest.raises(KeyError):
        write_agent_from_context(context)
    assert not (tmp_path / "agents/helper-1234").exists()
