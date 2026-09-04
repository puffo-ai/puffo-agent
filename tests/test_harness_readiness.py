"""Three-state harness readiness (ready / degraded / unavailable).

The legacy ``cli_tools`` strings could not say "startable but unverified":
harnesses with no checkable credential store were reported ``ready`` via a
constant-True check, and the ``acp`` entry was keyed to the *opencode*
binary although the ACP driver targets whatever executable the agent
config names. The three-state model says what is actually known; the
legacy wire strings stay byte-stable for old portal consumers and are
derived from the same computation, never maintained in parallel.
"""

import pytest

from puffo_agent.agent import cli_bin, model_catalog
from puffo_agent.agent.cli_bin import harness_readiness
from puffo_agent.portal.control.client import build_capabilities


def _raise():
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    ("resolver", "check", "state", "reason", "legacy"),
    [
        (lambda: None, lambda: True, "unavailable", "not_installed", "not_installed"),
        (_raise, lambda: True, "unavailable", "not_installed", "not_installed"),
        (lambda: None, None, "unavailable", "not_installed", "not_installed"),
        (lambda: "/bin/tool", None, "degraded", "credentials_unknown", "ready"),
        (lambda: "/bin/tool", lambda: True, "ready", "", "ready"),
        (lambda: "/bin/tool", lambda: False, "unavailable", "need_login", "need_login"),
        (lambda: "/bin/tool", _raise, "degraded", "credential_check_error", "need_login"),
    ],
)
def test_three_state_mapping(resolver, check, state, reason, legacy):
    r = harness_readiness(resolver, check)
    assert (r.state, r.reason, r.legacy) == (state, reason, legacy)


@pytest.mark.parametrize(
    ("resolver", "check"),
    [
        (lambda: None, lambda: True),
        (lambda: "/bin/tool", lambda: True),
        (lambda: "/bin/tool", lambda: False),
        (lambda: "/bin/tool", _raise),
        (_raise, lambda: True),
    ],
)
def test_legacy_string_is_derived_not_parallel(resolver, check):
    assert cli_bin.cli_tool_status(resolver, check) == harness_readiness(
        resolver, check
    ).legacy


def _patch_hosts(monkeypatch, *, opencode_path):
    monkeypatch.setattr(cli_bin, "resolve_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(cli_bin, "claude_has_credentials", lambda: True)
    monkeypatch.setattr(cli_bin, "resolve_codex_bin", lambda: "/bin/codex")
    monkeypatch.setattr(cli_bin, "codex_has_credentials", lambda: False)
    monkeypatch.setattr(cli_bin, "resolve_pi_bin", lambda: "/bin/pi")
    monkeypatch.setattr(cli_bin, "pi_has_credentials", lambda *a, **k: False)
    monkeypatch.setattr(cli_bin, "resolve_opencode_bin", lambda: opencode_path)
    monkeypatch.setattr(cli_bin, "opencode_has_accessible_models", lambda: True)
    monkeypatch.setattr(model_catalog, "KNOWN_HARNESSES", ())
    monkeypatch.setattr(
        model_catalog, "provider_models", lambda harness, *, fetch=False: []
    )


def test_capabilities_report_truth_beside_frozen_legacy(monkeypatch):
    _patch_hosts(monkeypatch, opencode_path="/bin/opencode")
    caps = build_capabilities()

    # Frozen legacy vocabulary, byte-stable — including the historical
    # quirk that "acp" mirrors the opencode binary's status.
    assert caps["cli_tools"] == {
        "claude-code": "ready",
        "codex": "need_login",
        "pi": "need_login",
        "opencode": "ready",
        "acp": "ready",
    }
    assert caps["harness_readiness"] == {
        "claude-code": {"state": "ready", "reason": ""},
        "codex": {"state": "unavailable", "reason": "need_login"},
        "pi": {"state": "unavailable", "reason": "need_login"},
        "opencode": {"state": "ready", "reason": ""},
        "acp": {"state": "degraded", "reason": "target_probe_required"},
    }


def test_acp_readiness_is_not_keyed_to_the_opencode_binary(monkeypatch):
    _patch_hosts(monkeypatch, opencode_path=None)
    caps = build_capabilities()

    # Legacy keeps the quirk (acp follows opencode → not_installed) …
    assert caps["cli_tools"]["opencode"] == "not_installed"
    assert caps["cli_tools"]["acp"] == "not_installed"
    # … the truth channel does not: the ACP driver's target is named per
    # agent config and probed at creation time.
    assert caps["harness_readiness"]["opencode"] == {
        "state": "unavailable",
        "reason": "not_installed",
    }
    assert caps["harness_readiness"]["acp"] == {
        "state": "degraded",
        "reason": "target_probe_required",
    }


def test_capabilities_publish_model_specific_pi_thinking_levels(monkeypatch):
    """The web must receive only thinking levels vouched for by Pi's catalog."""
    _patch_hosts(monkeypatch, opencode_path=None)
    monkeypatch.setattr(model_catalog, "KNOWN_HARNESSES", ("pi",))
    monkeypatch.setattr(
        model_catalog,
        "provider_models",
        lambda harness, *, fetch=False: [
            model_catalog.ModelOption(
                "openai-codex/gpt-5.4-mini",
                "gpt-5.4-mini (openai-codex)",
                supported_inference_levels=("off", "low", "high"),
            )
        ],
    )

    caps = build_capabilities()

    assert caps["providers"] == [{
        "provider": "pi",
        "models": [{
            "id": "openai-codex/gpt-5.4-mini",
            "label": "gpt-5.4-mini (openai-codex)",
            "alias": False,
            "supported_inference_levels": ["off", "low", "high"],
        }],
    }]
