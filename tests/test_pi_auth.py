"""Pi credentials: native readiness, provider selection, and private views."""

from __future__ import annotations

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


def test_pi_auth_view_copies_private_host_credentials(tmp_path):
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


def test_pi_auth_source_tracks_a_managed_view_back_to_host(tmp_path):
    host = tmp_path / "host"
    agent_pi = tmp_path / "agent" / ".pi" / "agent"
    host_auth = host / ".pi" / "agent" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"anthropic":{}}')

    assert sync_host_pi_auth_view(host, agent_pi) == "view"
    assert select_pi_auth_home(host, agent_pi) == host_auth.parent
