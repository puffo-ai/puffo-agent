from __future__ import annotations

import asyncio
import os
import sys

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


def test_verify_accepts_codex_minimal_inference_level():
    payload, operator_public = _payload()
    payload["runtime"] = {
        "kind": "cli-local",
        "provider": "openai",
        "harness": "codex",
        "inference_level": "minimal",
    }
    context = verify_agent_bundle(payload, operator_public)
    assert context["runtime"].inference_level == "minimal"


def test_verify_migrates_legacy_runtime_before_validation():
    payload, operator_public = _payload()
    payload["runtime"] = {
        "kind": "chat-local",
        "provider": "openai",
        "harness": "claude-code",
    }
    runtime = verify_agent_bundle(payload, operator_public)["runtime"]
    assert (runtime.kind, runtime.harness) == ("cli-local", "codex")


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
            lambda value: value.update({
                "runtime": {
                    "kind": "cli-local",
                    "provider": "openai",
                    "harness": "codex",
                    "inference_level": "xhigh",
                }
            }),
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
    agent_root = tmp_path / "agents/helper-1234"
    key_path = agent_root / "keys/helper-1234.json"
    assert key_path.is_file()
    if os.name != "nt":
        assert agent_root.stat().st_mode & 0o777 == 0o700
        assert key_path.parent.stat().st_mode & 0o777 == 0o700
    assert key_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_provision_preflight_rejects_before_materialization_or_write(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    payload, operator_public = _payload()
    events = []

    async def preflight(context):
        events.append(("preflight", context["agent_id"]))
        raise ProvisionError(
            "Pi sign-in required",
            error_code="harness_not_ready",
            harness="pi",
            reason="need_login",
        )

    async def materialize(context):
        events.append(("materialize", context["agent_id"]))

    with pytest.raises(ProvisionError, match="Pi sign-in required"):
        await provision_agent_from_bundle(
            payload,
            operator_public,
            preflight=preflight,
            materialize=materialize,
        )

    assert events == [("preflight", "helper-1234")]
    assert not (tmp_path / "agents" / "helper-1234").exists()


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


@pytest.mark.asyncio
async def test_lingtai_browser_create_preserves_workspace_and_registry(tmp_path, monkeypatch):
    """Browser-selected folders must survive provision into the driver argv/cwd."""
    import json
    import sys

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "daemon"))
    source = tmp_path / "existing-lingtai"
    workspace = tmp_path / "existing-workspace"
    source.mkdir()
    workspace.mkdir()
    (source / "init.json").write_text('{"manifest":{"agent_name":"Helper"}}')
    marker = tmp_path / "cli-args.json"
    executable = tmp_path / "lingtai-agent"
    executable.write_text(
        f"#!{sys.executable}\nimport json,sys\n"
        f"open({str(marker)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o700)
    payload, operator = _payload()
    payload.update(role="", role_short="", profile="# Helper\n")
    payload["runtime"] = {
        "kind": "cli-local", "harness": "acp", "provider": "openai",
        "lingtai": {"executable": str(executable), "agent_dir": str(source), "workspace": str(workspace), "agent_name": "Helper"},
    }

    async def materialize(context):
        assert marker.exists(), "runtime must be provisioned before remote identity materializes"

    await provision_agent_from_bundle(payload, operator, materialize=materialize)
    cfg = AgentConfig.load("helper-1234")
    argv = cfg.runtime.harness_command
    provision_args = json.loads(marker.read_text())
    assert cfg.resolve_workspace_dir() == workspace
    assert argv[:4] == [str(executable), "acp", "--profile", "puffo-v1"]
    for field in ["--runtime-id", "--registry"]:
        assert argv[argv.index(field) + 1] == provision_args[provision_args.index(field) + 1]
    assert provision_args[provision_args.index("--agent-dir") + 1] == str(source)
    assert (source / "init.json").read_text() == '{"manifest":{"agent_name":"Helper"}}'


@pytest.mark.asyncio
async def test_lingtai_provision_failure_leaves_identity_unmaterialized(tmp_path, monkeypatch):
    """An unusable LingTai runtime must not leave a running Puffo agent behind."""
    import sys

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "daemon"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "init.json").write_text('{"manifest":{"agent_name":"Helper"}}')
    executable = tmp_path / "lingtai-agent"
    executable.write_text(f"#!{sys.executable}\nimport sys\nprint('error: puffo-v0 runtime registry parent directory is owned by another user', file=sys.stderr)\nraise SystemExit(1)\n")
    executable.chmod(0o700)
    payload, operator = _payload()
    payload.update(role="", role_short="", profile="# Helper\n")
    payload["runtime"] = {
        "kind": "cli-local", "harness": "acp", "provider": "openai",
        "lingtai": {"executable": str(executable), "agent_dir": str(source), "workspace": str(source), "agent_name": "Helper"},
    }
    materialized = []

    async def materialize(context):
        materialized.append(context)

    with pytest.raises(ProvisionError, match="registry parent directory is owned by another user"):
        await provision_agent_from_bundle(payload, operator, materialize=materialize)
    assert materialized == []
    assert not (tmp_path / "daemon/agents/helper-1234/agent.yml").exists()


@pytest.fixture
def lingtai_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "daemon"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "init.json").write_text('{"manifest":{"agent_name":"Helper"}}')
    payload, operator = _payload()
    payload.update(role="", role_short="", profile="# Helper\n")
    payload["runtime"] = {
        "kind": "cli-local", "harness": "acp", "provider": "openai",
        "lingtai": {"executable": sys.executable, "agent_dir": str(source), "workspace": str(source), "agent_name": "Helper"},
    }
    associations = set()

    async def register(launch):
        associations.add((launch.runtime_id, launch.registry))

    async def revoke(launch):
        associations.remove((launch.runtime_id, launch.registry))

    monkeypatch.setattr(provision, "provision_lingtai", register)
    monkeypatch.setattr(provision, "revoke_lingtai", revoke)
    return payload, operator, associations


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["materialize", "write"])
async def test_lingtai_creation_failure_revokes_association(lingtai_creation, monkeypatch, stage):
    """Either post-registration failure must revoke the same registry entry."""
    payload, operator, associations = lingtai_creation
    original = RuntimeError("creation failed")

    async def materialize(context):
        assert associations
        if stage == "materialize":
            raise original

    def write(context):
        raise original

    monkeypatch.setattr(provision, "write_agent_from_context", write)
    with pytest.raises(RuntimeError) as caught:
        await provision_agent_from_bundle(payload, operator, materialize=materialize)
    assert caught.value is original
    assert not associations


@pytest.mark.asyncio
async def test_lingtai_rollback_failure_preserves_original_error(lingtai_creation, monkeypatch, caplog):
    """Cleanup failure must be logged without replacing the creation error."""
    payload, operator, _ = lingtai_creation
    original = RuntimeError("materialize failed")

    async def materialize(context):
        raise original

    async def revoke(launch):
        raise OSError("cleanup failed")

    monkeypatch.setattr(provision, "revoke_lingtai", revoke)
    with pytest.raises(RuntimeError) as caught:
        await provision_agent_from_bundle(payload, operator, materialize=materialize)
    assert caught.value is original
    assert "LingTai rollback failed" in caplog.text


@pytest.mark.asyncio
async def test_lingtai_repeated_cancellation_finishes_rollback(lingtai_creation, monkeypatch):
    """A second shutdown cancellation must not interrupt revoke or replace the first."""
    payload, operator, associations = lingtai_creation
    materializing = asyncio.Event()
    revoking = asyncio.Event()
    release = asyncio.Event()

    async def materialize(context):
        materializing.set()
        await asyncio.Event().wait()

    async def revoke(launch):
        revoking.set()
        await release.wait()
        associations.remove((launch.runtime_id, launch.registry))

    monkeypatch.setattr(provision, "revoke_lingtai", revoke)
    task = asyncio.create_task(provision_agent_from_bundle(payload, operator, materialize=materialize))
    await asyncio.wait_for(materializing.wait(), 2)
    task.cancel("first cancellation")
    await asyncio.wait_for(revoking.wait(), 2)
    task.cancel("second cancellation")
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert not associations
    assert caught.value.args == ("first cancellation",)


@pytest.mark.parametrize("change", ["display_name", "role", "soul", "profile", "source", "missing_source"])
def test_lingtai_import_rejects_source_profile_drift_before_creation(lingtai_creation, change):
    """A stale browser choice or persona payload must never materialize identity."""
    from pathlib import Path

    payload, operator, associations = lingtai_creation
    if change in ("source", "missing_source"):
        source = Path(payload["runtime"]["lingtai"]["agent_dir"])
        (source / "init.json").write_text('{"manifest":{"agent_name":"Changed"}}' if change == "source" else "{}")
    else:
        payload[change] = "Override"
    with pytest.raises(ProvisionError, match="LingTai"):
        verify_agent_bundle(payload, operator)
    assert not associations


@pytest.mark.asyncio
async def test_lingtai_source_drift_during_preflight_cannot_register(lingtai_creation):
    """An async preflight must not turn source validation into a stale snapshot."""
    from pathlib import Path

    payload, operator, associations = lingtai_creation
    async def preflight(context):
        source = Path(payload["runtime"]["lingtai"]["agent_dir"])
        (source / "init.json").write_text('{"manifest":{"agent_name":"Changed"}}')
    with pytest.raises(ProvisionError, match="source name changed"):
        await provision_agent_from_bundle(payload, operator, preflight=preflight)
    assert not associations


@pytest.mark.parametrize("name", [None, ""])
def test_unnamed_lingtai_import_uses_fixed_placeholder_not_init_name(lingtai_creation, name):
    """Readable unnamed sources can import without exposing an operator rename bypass."""
    import json
    from pathlib import Path

    payload, operator, _ = lingtai_creation
    source = Path(payload["runtime"]["lingtai"]["agent_dir"])
    (source / ".agent.json").write_text(json.dumps({"agent_name": name}))
    payload["runtime"]["lingtai"]["agent_name"] = None
    payload.update(display_name="Unnamed Agent", profile="# Unnamed Agent\n")
    assert verify_agent_bundle(payload, operator)["display_name"] == "Unnamed Agent"
    payload.update(display_name="Helper", profile="# Helper\n")
    with pytest.raises(ProvisionError, match="source name changed"):
        verify_agent_bundle(payload, operator)


@pytest.mark.parametrize("selection", ["missing", "named-placeholder", "unnamed-to-named"])
def test_lingtai_import_requires_exact_nullable_name_snapshot(lingtai_creation, selection):
    """Placeholder display equality must not mask named/unnamed source drift."""
    from pathlib import Path

    payload, operator, _ = lingtai_creation
    source = Path(payload["runtime"]["lingtai"]["agent_dir"])
    if selection == "missing":
        del payload["runtime"]["lingtai"]["agent_name"]
    else:
        payload.update(display_name="Unnamed Agent", profile="# Unnamed Agent\n")
        selected_name = "Unnamed Agent" if selection == "named-placeholder" else None
        payload["runtime"]["lingtai"]["agent_name"] = selected_name
        (source / ".agent.json").write_text(
            '{"agent_name":null}' if selected_name else '{"agent_name":"Unnamed Agent"}'
        )
    with pytest.raises(ProvisionError, match="source name changed"):
        verify_agent_bundle(payload, operator)
