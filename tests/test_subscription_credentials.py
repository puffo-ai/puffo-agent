"""``runtime.auth_mode`` and the credentials it selects.

The matrix is small and worth pinning exactly: two auth modes by two
harnesses, plus the negatives that keep a plan token from leaking in through
a channel that is not ``controlled``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from puffo_agent.agent.harness.support.child_env import (
    PROVIDER_CREDENTIAL_ENV_NAMES,
    build_child_environment,
)
from puffo_agent.agent.harness.support.subscription_credentials import (
    AUTH_MODE_API_GATEWAY,
    AUTH_MODE_SUBSCRIPTION,
    AUTH_MODES,
    CLAUDE_SUBSCRIPTION_ENV,
    CODEX_SUBSCRIPTION_ENV,
    SubscriptionCredentialMissing,
    SubscriptionUnsupported,
    resolve_subscription_credentials,
)
from puffo_agent.portal.state import RuntimeConfig

_CREDS = "puffo_agent.agent.harness.support.subscription_credentials"


@pytest.fixture
def on_linux(monkeypatch):
    """Resolution is refused on macOS (Keychain deletion, #37512), so the
    credential tests pin the Linux/sandbox behaviour explicitly rather than
    depending on whoever's laptop runs them."""
    monkeypatch.setattr(f"{_CREDS}.is_macos", lambda: False)


def test_auth_mode_defaults_to_api_gateway():
    """The metered path is the default: this package is vendored into images
    that may be promoted to production, where agents must stay on the gateway."""
    assert RuntimeConfig().auth_mode == AUTH_MODE_API_GATEWAY
    assert AUTH_MODES == {AUTH_MODE_API_GATEWAY, AUTH_MODE_SUBSCRIPTION}


def test_claude_code_subscription_supplies_the_oauth_token(on_linux, tmp_path: Path):
    creds = resolve_subscription_credentials(
        "claude-code",
        agent_home=tmp_path,
        token="sk-ant-oat01-example",
    )
    assert dict(creds.env) == {CLAUDE_SUBSCRIPTION_ENV: "sk-ant-oat01-example"}
    # No ANTHROPIC_BASE_URL: it outranks the plan token in the CLI's own
    # precedence, so leaving one set would silently bill the gateway.
    assert "ANTHROPIC_BASE_URL" not in creds.env
    assert not creds.files


def test_codex_subscription_is_file_based(on_linux, tmp_path: Path):
    """Codex has no token variable; its plan auth is an auth.json under
    CODEX_HOME, so the credential is written rather than exported."""
    blob = json.dumps({"tokens": {"access_token": "x"}})
    creds = resolve_subscription_credentials(
        "codex", agent_home=tmp_path, token=blob
    )
    target = tmp_path / ".codex" / "auth.json"
    assert dict(creds.files) == {target: blob}
    assert creds.env == {"CODEX_HOME": str(tmp_path / ".codex")}
    # CODEX_HOME is set by us, so it travels in env -> controlled, which is what
    # guarantees the child sees our value. extra_allowed is for inheriting an
    # ambient variable, which this is not.
    assert creds.extra_allowed == ()


@pytest.mark.parametrize(
    "harness, env_name",
    [("claude-code", CLAUDE_SUBSCRIPTION_ENV), ("codex", CODEX_SUBSCRIPTION_ENV)],
)
def test_missing_credential_fails_closed(
    on_linux, harness: str, env_name: str, tmp_path: Path
):
    """Never fall back to the gateway. A fallback keeps working, bills the
    metered account, and looks like success."""
    with pytest.raises(SubscriptionCredentialMissing) as excinfo:
        resolve_subscription_credentials(harness, agent_home=tmp_path, token="")
    assert env_name in str(excinfo.value)


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_blank_credential_is_treated_as_missing(on_linux, harness: str, tmp_path: Path):
    with pytest.raises(SubscriptionCredentialMissing):
        resolve_subscription_credentials(harness, agent_home=tmp_path, token="   ")


def test_subscription_tokens_cannot_be_inherited_from_ambient_env():
    """Only ``controlled`` may set a plan credential."""
    env = build_child_environment(
        source={"PATH": "/usr/bin", CLAUDE_SUBSCRIPTION_ENV: "leaked"}
    )
    assert CLAUDE_SUBSCRIPTION_ENV not in env


def test_subscription_tokens_cannot_be_injected_via_env_overrides():
    env = build_child_environment(
        overrides={CLAUDE_SUBSCRIPTION_ENV: "smuggled"},
        source={"PATH": "/usr/bin"},
    )
    assert CLAUDE_SUBSCRIPTION_ENV not in env


def test_controlled_is_the_one_channel_that_works():
    env = build_child_environment(
        controlled={CLAUDE_SUBSCRIPTION_ENV: "legitimate"},
        source={"PATH": "/usr/bin"},
    )
    assert env[CLAUDE_SUBSCRIPTION_ENV] == "legitimate"


def test_both_subscription_names_are_in_the_never_inherit_set():
    assert {CLAUDE_SUBSCRIPTION_ENV, CODEX_SUBSCRIPTION_ENV} <= (
        PROVIDER_CREDENTIAL_ENV_NAMES
    )


def test_subscription_token_reads_the_daemon_process_environment(monkeypatch):
    """A cloud sandbox receives the token at creation; the daemon resolves it
    once at startup so the harness boundary never touches os.environ."""
    from puffo_agent.portal.state import subscription_token

    monkeypatch.setenv(CLAUDE_SUBSCRIPTION_ENV, "sk-ant-oat01-from-env")
    monkeypatch.setenv(CODEX_SUBSCRIPTION_ENV, '{"tokens":{}}')
    assert subscription_token(None, "claude-code") == "sk-ant-oat01-from-env"
    assert subscription_token(None, "codex") == '{"tokens":{}}'


def test_subscription_token_is_empty_when_nothing_supplies_it(monkeypatch):
    from puffo_agent.portal.state import subscription_token

    monkeypatch.delenv(CLAUDE_SUBSCRIPTION_ENV, raising=False)
    assert subscription_token(None, "claude-code") == ""


def test_invalid_auth_mode_is_rejected():
    from puffo_agent.portal.state import _validate_auth_mode

    assert _validate_auth_mode("a", None) == AUTH_MODE_API_GATEWAY
    assert _validate_auth_mode("a", "") == AUTH_MODE_API_GATEWAY
    assert _validate_auth_mode("a", AUTH_MODE_SUBSCRIPTION) == AUTH_MODE_SUBSCRIPTION
    with pytest.raises(RuntimeError, match="auth_mode must be one of"):
        _validate_auth_mode("a", "gateway")


def test_macos_refuses_subscription_outright(monkeypatch, tmp_path: Path):
    """anthropics/claude-code#37512: with CLAUDE_CODE_OAUTH_TOKEN set, the CLI
    deletes the operator's own Keychain login on exit. HOME/CLAUDE_CONFIG_DIR
    redirection does not help -- the item is user-wide -- so the only safe
    answer on macOS is to refuse. Cloud agents are Linux and unaffected."""
    monkeypatch.setattr(f"{_CREDS}.is_macos", lambda: True)
    with pytest.raises(SubscriptionUnsupported, match="macOS"):
        resolve_subscription_credentials(
            "claude-code", agent_home=tmp_path, token="sk-ant-oat01-real"
        )


def test_macos_refusal_beats_a_missing_credential(monkeypatch, tmp_path: Path):
    """Platform is checked first: on macOS the answer is 'never', not 'set the
    variable and retry'."""
    monkeypatch.setattr(f"{_CREDS}.is_macos", lambda: True)
    with pytest.raises(SubscriptionUnsupported):
        resolve_subscription_credentials("claude-code", agent_home=tmp_path, token="")


def test_subscription_agents_are_exempt_from_credential_refresh():
    """A subscription agent has nothing to refresh: its credential is a
    self-contained, year-long plan token supplied by the provisioner.

    Regression for a real staging failure. `llm_base_url` is deliberately empty
    for these agents, and the daemon's refresh gate keyed off *that* -- so the
    agent fell through to the OAuth path, hunted for an operator credential view
    that does not exist inside a sandbox, logged `credential view-sync
    incomplete`, declared auth-failed and blocked the turn. The mode must decide,
    not a side effect of which fields happen to be set."""
    from types import SimpleNamespace

    from puffo_agent.portal.daemon import _is_subscription

    sub = SimpleNamespace(runtime=SimpleNamespace(auth_mode=AUTH_MODE_SUBSCRIPTION))
    gw = SimpleNamespace(runtime=SimpleNamespace(auth_mode=AUTH_MODE_API_GATEWAY))
    assert _is_subscription(sub) is True
    assert _is_subscription(gw) is False
    # a runtime predating the field is not in subscription mode
    assert _is_subscription(SimpleNamespace(runtime=SimpleNamespace())) is False
    assert _is_subscription(SimpleNamespace()) is False
