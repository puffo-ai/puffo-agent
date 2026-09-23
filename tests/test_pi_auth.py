"""Pi credentials: native readiness, provider selection, and private views."""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

from puffo_agent.agent.pi_auth import (
    PiAuthProbeError,
    check_pi_auth,
    list_pi_models,
    pi_has_credentials,
)
from puffo_agent.portal.host_assets import sync_host_pi_auth_view
from puffo_agent.portal.host_assets import select_pi_auth_home
from puffo_agent.portal.host_assets import (
    derive_pi_codex_entry,
    pi_auth_expiry_ms,
    pi_auth_projection_state,
)


def _completed(*, code: int, payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout=json.dumps(payload), stderr=""
    )


def test_native_auth_probe_uses_explicit_config_and_scrubs_ambient_keys(
    tmp_path, monkeypatch,
):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return _completed(
            code=0,
            payload={"status": "ready", "provider": "anthropic"},
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-probe")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = check_pi_auth(
        "/opt/bin/pi",
        provider="anthropic",
        model="claude-opus-4-8",
        config_dir=tmp_path,
    )

    assert result.status == "ready"
    assert seen["command"] == [
        "/opt/bin/pi", "auth", "check", "--provider", "anthropic",
        "--model", "claude-opus-4-8", "--json", "--no-refresh",
    ]
    assert seen["kwargs"]["env"]["PI_CODING_AGENT_DIR"] == str(tmp_path)
    assert "ANTHROPIC_API_KEY" not in seen["kwargs"]["env"]
    assert seen["kwargs"]["timeout"] <= 5


@pytest.mark.parametrize(
    ("code", "payload"),
    [
        (0, {"status": "not_ready", "provider": "anthropic"}),
        (1, {"status": "ready", "provider": "anthropic"}),
        (2, {"status": "invalid", "provider": "anthropic"}),
        (0, {"status": "mystery", "provider": "anthropic"}),
    ],
)
def test_native_auth_probe_fails_closed_on_incoherent_results(
    tmp_path, monkeypatch, code, payload,
):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(code=code, payload=payload)
    )
    with pytest.raises(PiAuthProbeError):
        check_pi_auth("/opt/bin/pi", provider="anthropic", config_dir=tmp_path)


def test_host_readiness_checks_each_configured_provider_natively(
    tmp_path, monkeypatch,
):
    config_dir = tmp_path / ".pi" / "agent"
    config_dir.mkdir(parents=True)
    (config_dir / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "oauth"}, "openai": {"type": "api_key"}}),
        encoding="utf-8",
    )
    checked = []

    def fake_check(executable, *, provider, model="", config_dir):
        checked.append((executable, provider, config_dir))
        status = "ready" if provider == "openai" else "not_ready"
        return type("Result", (), {"status": status})()

    monkeypatch.setattr("puffo_agent.agent.pi_auth.check_pi_auth", fake_check)

    assert pi_has_credentials("/opt/bin/pi", home=tmp_path) is True
    assert checked == [
        ("/opt/bin/pi", "anthropic", config_dir),
        ("/opt/bin/pi", "openai", config_dir),
    ]


def test_empty_host_auth_is_logged_out_without_forking(tmp_path, monkeypatch):
    config_dir = tmp_path / ".pi" / "agent"
    config_dir.mkdir(parents=True)
    (config_dir / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("must not fork")
    )
    assert pi_has_credentials("/opt/bin/pi", home=tmp_path) is False


def test_native_model_list_uses_same_private_config_view(tmp_path, monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "provider model context input modalities thinking\n"
                "anthropic claude-sonnet-4-6 200k text yes\n"
                "openai gpt-5.5 400k text yes\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert list_pi_models(
        "/opt/bin/pi", config_dir=tmp_path,
    ) == (
        (
            "anthropic/claude-sonnet-4-6",
            "claude-sonnet-4-6 (anthropic)",
            True,
        ),
        ("openai/gpt-5.5", "gpt-5.5 (openai)", True),
    )
    assert seen["command"] == ["/opt/bin/pi", "--list-models"]
    assert seen["kwargs"]["env"]["PI_CODING_AGENT_DIR"] == str(tmp_path)


def _codex_access_token(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{claims}.signature"


def _write_codex(host, *, tokens) -> None:
    codex = host / ".codex" / "auth.json"
    codex.parent.mkdir(parents=True, exist_ok=True)
    codex.write_text(json.dumps({"tokens": tokens}), encoding="utf-8")


def test_pi_auth_view_passes_host_bytes_through_without_codex_credentials(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    host_auth = host / ".pi" / "agent" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"anthropic":{"type":"oauth","refresh":"secret"}}')

    assert sync_host_pi_auth_view(host, agent_pi) == "view"
    assert (agent_pi / "auth.json").read_text() == host_auth.read_text()
    if __import__("os").name != "nt":
        assert (agent_pi / "auth.json").stat().st_mode & 0o777 == 0o600


def test_pi_auth_view_does_not_overwrite_operator_owned_target(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    (host / ".pi" / "agent").mkdir(parents=True)
    (host / ".pi" / "agent" / "auth.json").write_text('{"host":{}}')
    agent_pi.mkdir(parents=True)
    target = agent_pi / "auth.json"
    target.write_text('{"operator":{}}')

    assert sync_host_pi_auth_view(host, agent_pi) == "operator-owned"
    assert target.read_text() == '{"operator":{}}'
    assert select_pi_auth_home(host, agent_pi) == agent_pi


def test_pi_view_leaves_a_target_rewritten_after_projection_alone(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    (host / ".pi" / "agent").mkdir(parents=True)
    (host / ".pi" / "agent" / "auth.json").write_text('{"host":{}}')

    assert sync_host_pi_auth_view(host, agent_pi) == "view"
    assert pi_auth_projection_state(agent_pi) == "view"
    marker = (agent_pi / ".puffo-host-auth.sha256").read_text()

    (agent_pi / "auth.json").write_text('{"pi-rewrote-this":{}}')
    assert pi_auth_projection_state(agent_pi) == "operator-owned"
    assert sync_host_pi_auth_view(host, agent_pi) == "operator-owned"
    assert (agent_pi / "auth.json").read_text() == '{"pi-rewrote-this":{}}'
    assert (agent_pi / ".puffo-host-auth.sha256").read_text() == marker


def test_pi_auth_source_prefers_a_materialised_view_over_the_host(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    host_auth = host / ".pi" / "agent" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"anthropic":{}}')

    assert select_pi_auth_home(host, agent_pi) == host_auth.parent
    assert sync_host_pi_auth_view(host, agent_pi) == "view"
    assert select_pi_auth_home(host, agent_pi) == agent_pi


def test_pi_view_derives_codex_entry_and_keeps_other_providers(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    host_auth = host / ".pi" / "agent" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text(
        json.dumps(
            {
                "anthropic": {"type": "oauth", "refresh": "anthropic-rt"},
                "openai-codex": {
                    "type": "oauth",
                    "access": "stale-access",
                    "refresh": "consumed-rt",
                },
            }
        )
    )
    _write_codex(
        host,
        tokens={
            "access_token": _codex_access_token(1_700_000_000),
            "refresh_token": "live-rotating-rt",
            "account_id": "acct-77",
        },
    )

    assert sync_host_pi_auth_view(host, agent_pi) == "view"

    view = json.loads((agent_pi / "auth.json").read_text())
    assert view["anthropic"] == {"type": "oauth", "refresh": "anthropic-rt"}
    assert view["openai-codex"] == {
        "type": "oauth",
        "access": _codex_access_token(1_700_000_000),
        "refresh": "",
        "accountId": "acct-77",
        "expires": 1_700_000_000_000,
    }
    assert "live-rotating-rt" not in (agent_pi / "auth.json").read_text()


def test_pi_view_reprojects_after_the_codex_token_rotates(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    (host / ".pi" / "agent").mkdir(parents=True)
    (host / ".pi" / "agent" / "auth.json").write_text("{}")
    _write_codex(host, tokens={"access_token": _codex_access_token(1_700_000_000)})
    assert sync_host_pi_auth_view(host, agent_pi) == "view"

    rotated = _codex_access_token(1_800_000_000)
    _write_codex(host, tokens={"access_token": rotated})

    assert sync_host_pi_auth_view(host, agent_pi) == "view"
    view = json.loads((agent_pi / "auth.json").read_text())
    assert view["openai-codex"]["access"] == rotated
    assert view["openai-codex"]["expires"] == 1_800_000_000_000
    assert pi_auth_projection_state(agent_pi) == "view"


def test_pi_view_is_fresh_when_the_derived_bytes_are_unchanged(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    (host / ".pi" / "agent").mkdir(parents=True)
    (host / ".pi" / "agent" / "auth.json").write_text('{"anthropic":{}}')
    _write_codex(host, tokens={"access_token": _codex_access_token(1_700_000_000)})

    assert sync_host_pi_auth_view(host, agent_pi) == "view"
    assert sync_host_pi_auth_view(host, agent_pi) == "view (fresh)"


def test_pi_view_derives_codex_entry_without_a_host_pi_file(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    _write_codex(host, tokens={"access_token": _codex_access_token(1_700_000_000)})

    assert sync_host_pi_auth_view(host, agent_pi) == "view"
    assert json.loads((agent_pi / "auth.json").read_text()) == {
        "openai-codex": {
            "type": "oauth",
            "access": _codex_access_token(1_700_000_000),
            "refresh": "",
            "expires": 1_700_000_000_000,
        }
    }


def test_pi_view_without_any_host_credential_reports_no_host_file(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"

    assert sync_host_pi_auth_view(host, agent_pi) == "no-host-file"
    assert not (agent_pi / "auth.json").exists()


def test_pi_view_rejects_an_unparseable_host_pi_file(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    (host / ".pi" / "agent").mkdir(parents=True)
    (host / ".pi" / "agent" / "auth.json").write_text("not json")
    _write_codex(host, tokens={"access_token": _codex_access_token(1_700_000_000)})

    assert sync_host_pi_auth_view(host, agent_pi) == "unparseable-host-file"
    assert not (agent_pi / "auth.json").exists()


def test_pi_view_falls_back_to_host_bytes_on_an_unusable_codex_file(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    host_auth = host / ".pi" / "agent" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"openai-codex":{"type":"oauth","access":"host"}}')
    (host / ".codex").mkdir(parents=True)
    (host / ".codex" / "auth.json").write_text("not json")

    assert sync_host_pi_auth_view(host, agent_pi) == "view"
    assert (agent_pi / "auth.json").read_text() == host_auth.read_text()


@pytest.mark.parametrize(
    "blob",
    [
        "not json",
        "[]",
        "{}",
        '{"tokens": "nope"}',
        '{"tokens": {}}',
        '{"tokens": {"access_token": ""}}',
        '{"tokens": {"access_token": 7}}',
    ],
)
def test_codex_entry_derivation_rejects_unusable_blobs(blob):
    assert derive_pi_codex_entry(blob) is None


@pytest.mark.parametrize(
    "access",
    [
        "opaque-token",
        "a.b",
        "a.!!!.c",
        "a.W10=.c",
        f"a.{'e30='}.c",
        "a.eyJleHAiOiAxfQ.c.d",
    ],
)
def test_codex_entry_omits_expiry_when_the_access_token_has_no_exp(access):
    entry = derive_pi_codex_entry(json.dumps({"tokens": {"access_token": access}}))
    assert entry == {"type": "oauth", "access": access, "refresh": ""}


def test_codex_entry_omits_a_non_integer_expiry():
    access = (
        base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(b'{"exp": true}').rstrip(b"=").decode()
        + ".sig"
    )
    assert "expires" not in derive_pi_codex_entry(
        json.dumps({"tokens": {"access_token": access}})
    )


def test_projection_state_reports_an_unprojected_and_an_operator_target(tmp_path):
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    assert pi_auth_projection_state(agent_pi) == "not-projected"

    agent_pi.mkdir(parents=True)
    (agent_pi / "auth.json").write_text('{"operator":{}}')
    assert pi_auth_projection_state(agent_pi) == "operator-owned"


def test_projected_expiry_is_read_without_exposing_the_token(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    assert pi_auth_expiry_ms(agent_pi) is None

    _write_codex(host, tokens={"access_token": _codex_access_token(1_700_000_000)})
    assert sync_host_pi_auth_view(host, agent_pi) == "view"
    assert pi_auth_expiry_ms(agent_pi) == 1_700_000_000_000


@pytest.mark.parametrize(
    "payload",
    ["[]", '{"openai-codex": "nope"}', '{"openai-codex": {}}',
     '{"openai-codex": {"expires": "soon"}}',
     '{"openai-codex": {"expires": true}}'],
)
def test_projected_expiry_is_unknown_without_a_usable_entry(tmp_path, payload):
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    agent_pi.mkdir(parents=True)
    (agent_pi / "auth.json").write_text(payload)
    assert pi_auth_expiry_ms(agent_pi) is None
