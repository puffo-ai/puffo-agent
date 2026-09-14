"""``runtime.auth_mode = subscription`` on the codex spec path.

Regression for staging 2026-09-14: the Hub stored a Codex plan credential,
AIM delivered it, and the worker died at startup with
``SubscriptionUnsupported`` because ``_prepare_codex_spec`` refused the mode
before touching the credential. What is pinned: the document lands as
``auth.json`` under the agent's ``CODEX_HOME`` at 0600, the child gets that
``CODEX_HOME`` and no gateway key, a configured gateway does not outrank the
plan (config.toml carries no provider block), the host auth view is never
consulted, and a missing document fails closed.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from puffo_agent.agent.harness.support.subscription_credentials import (
    AUTH_MODE_SUBSCRIPTION,
    CODEX_SUBSCRIPTION_ENV,
    SubscriptionCredentialMissing,
)

_RT = "puffo_agent.agent.harness.runtime.local_runtime"
_CREDS = "puffo_agent.agent.harness.support.subscription_credentials"
DOC = json.dumps({
    "auth_mode": "chatgpt",
    "tokens": {"access_token": "a.b.c", "refresh_token": "rt.1", "id_token": "i"},
    "last_refresh": "2026-09-14T22:26:05Z",
})


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


def _preparer(monkeypatch, *, gateway: bool):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime
    from puffo_agent.agent.harness.runtime.local_runtime import LocalRuntimePreparer
    from puffo_agent.portal.state import AgentConfig, DaemonConfig, RuntimeConfig

    monkeypatch.setattr(local_runtime, "resolve_codex_bin", lambda: "/bin/codex")
    monkeypatch.setattr(local_runtime, "is_macos", lambda: False)
    monkeypatch.setattr(f"{_CREDS}.is_macos", lambda: False)

    def _never(*_a, **_k):
        raise AssertionError("host auth view must not be consulted for a plan agent")

    monkeypatch.setattr(local_runtime, "sync_host_codex_auth_view", _never)
    cfg = AgentConfig(
        id="codex-plan",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="openai",
            harness="codex",
            auth_mode=AUTH_MODE_SUBSCRIPTION,
            llm_base_url="https://gateway.example/v1" if gateway else "",
            api_key="sk-virtual-key" if gateway else "",
        ),
    )
    return LocalRuntimePreparer(DaemonConfig(), cfg)


def test_plan_document_is_written_under_codex_home_and_no_key_reaches_the_child(
    home, monkeypatch,
):
    monkeypatch.setenv(CODEX_SUBSCRIPTION_ENV, DOC)
    spec = _preparer(monkeypatch, gateway=False)._prepare_codex_spec("prompt")

    codex_home = home / "agents" / "codex-plan" / ".codex"
    assert spec.environment["CODEX_HOME"] == str(codex_home)
    auth = codex_home / "auth.json"
    assert auth.read_text(encoding="utf-8") == DOC
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    assert "OPENAI_API_KEY" not in spec.environment
    assert CODEX_SUBSCRIPTION_ENV not in spec.environment  # document, not env
    toml = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'cli_auth_credentials_store = "file"' in toml


def test_plan_supersedes_a_configured_gateway(home, monkeypatch):
    """A leftover gateway URL + virtual key must not outrank the plan; codex
    would take the provider block and bill the metered account in silence."""
    monkeypatch.setenv(CODEX_SUBSCRIPTION_ENV, DOC)
    spec = _preparer(monkeypatch, gateway=True)._prepare_codex_spec("prompt")

    assert "OPENAI_API_KEY" not in spec.environment
    toml = (home / "agents" / "codex-plan" / ".codex" / "config.toml").read_text(
        encoding="utf-8"
    )
    assert "model_provider" not in toml and "litellm" not in toml


def test_missing_document_fails_closed(home, monkeypatch):
    monkeypatch.delenv(CODEX_SUBSCRIPTION_ENV, raising=False)
    with pytest.raises(SubscriptionCredentialMissing, match=CODEX_SUBSCRIPTION_ENV):
        _preparer(monkeypatch, gateway=False)._prepare_codex_spec("prompt")
    assert not (home / "agents" / "codex-plan" / ".codex" / "auth.json").exists()
