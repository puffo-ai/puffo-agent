"""Runtime migration and supported provider/harness combinations."""

from __future__ import annotations

import logging

import pytest

from puffo_agent.portal.runtime_matrix import (
    DEFAULT_HARNESS_FOR_PROVIDER,
    HARNESS_CLAUDE_CODE,
    HARNESS_CODEX,
    HARNESS_GEMINI_CLI,
    HARNESS_HERMES,
    HARNESS_PROVIDERS,
    PROVIDER_ANTHROPIC,
    PROVIDER_GOOGLE,
    PROVIDER_OPENAI,
    RESERVED_RUNTIMES,
    RUNTIME_CLI_DOCKER,
    RUNTIME_CLI_LOCAL,
    RUNTIME_CLI_SANDBOX,
    RUNTIME_WS_LOCAL,
    VALID_HARNESSES,
    VALID_PROVIDERS,
    VALID_RUNTIMES,
    harness_applies,
    migrate_legacy_kind,
    normalize_inference_level,
    resolve_effective_harness,
    resolve_effective_provider,
    validate_triple,
)


@pytest.mark.parametrize("legacy", [
    "chat-only",
    "chat-local",
    "sdk",
    "sdk-local",
])
def test_direct_provider_runtimes_migrate_to_cli_local(legacy, caplog):
    with caplog.at_level(
        logging.WARNING,
        logger="puffo_agent.portal.runtime_matrix",
    ):
        assert migrate_legacy_kind(legacy, agent_id="alice") == RUNTIME_CLI_LOCAL
    assert legacy in caplog.text
    assert "cli-local" in caplog.text
    assert "alice" in caplog.text


def test_current_and_unknown_runtime_names_are_not_rewritten(caplog):
    for kind in (RUNTIME_CLI_LOCAL, RUNTIME_CLI_DOCKER, RUNTIME_WS_LOCAL):
        assert migrate_legacy_kind(kind) == kind
    assert migrate_legacy_kind("unknown") == "unknown"
    assert not caplog.records


def test_supported_runtime_surface_is_explicit():
    assert VALID_RUNTIMES == {
        RUNTIME_CLI_LOCAL,
        RUNTIME_CLI_DOCKER,
        RUNTIME_WS_LOCAL,
    }
    assert RUNTIME_CLI_SANDBOX in RESERVED_RUNTIMES
    assert RUNTIME_CLI_SANDBOX not in VALID_RUNTIMES


@pytest.mark.parametrize("runtime,provider,harness", [
    (RUNTIME_CLI_LOCAL, PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE),
    (RUNTIME_CLI_LOCAL, PROVIDER_OPENAI, HARNESS_CODEX),
    (RUNTIME_CLI_LOCAL, PROVIDER_OPENAI, ""),
    (RUNTIME_CLI_DOCKER, PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE),
    (RUNTIME_CLI_DOCKER, "", ""),
    (RUNTIME_WS_LOCAL, "", "anything-external"),
])
def test_validate_triple_accepts_supported_combinations(
    runtime, provider, harness,
):
    assert validate_triple(runtime, provider, harness).ok


@pytest.mark.parametrize("runtime,provider,harness,error", [
    ("unknown", PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE, "unknown runtime"),
    (RUNTIME_CLI_LOCAL, "cohere", HARNESS_CLAUDE_CODE, "unknown provider"),
    (RUNTIME_CLI_LOCAL, PROVIDER_GOOGLE, HARNESS_GEMINI_CLI, "Driver runtime"),
    (RUNTIME_CLI_LOCAL, PROVIDER_ANTHROPIC, HARNESS_HERMES, "Driver runtime"),
    (RUNTIME_CLI_DOCKER, PROVIDER_OPENAI, HARNESS_CODEX, "Driver runtime"),
    (RUNTIME_CLI_DOCKER, PROVIDER_GOOGLE, HARNESS_CLAUDE_CODE, "does not support"),
    # Docker Hermes/Gemini cannot complete the metadata-notified Inbox
    # contract, so the matrix rejects them before any Worker is built —
    # explicitly, and via the provider-resolved default harness.
    (RUNTIME_CLI_DOCKER, PROVIDER_ANTHROPIC, HARNESS_HERMES, "design-only"),
    (RUNTIME_CLI_DOCKER, PROVIDER_GOOGLE, HARNESS_GEMINI_CLI, "design-only"),
    (RUNTIME_CLI_DOCKER, PROVIDER_OPENAI, "", "design-only"),
    (RUNTIME_CLI_SANDBOX, PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE, "reserved"),
])
def test_validate_triple_rejects_unsupported_combinations(
    runtime, provider, harness, error,
):
    result = validate_triple(runtime, provider, harness)
    assert not result.ok
    assert error in result.error


def test_runtime_defaults_select_the_ratified_local_drivers():
    assert resolve_effective_provider(RUNTIME_CLI_LOCAL, "") == PROVIDER_ANTHROPIC
    assert resolve_effective_harness(
        RUNTIME_CLI_LOCAL, PROVIDER_ANTHROPIC, "",
    ) == HARNESS_CLAUDE_CODE
    assert resolve_effective_harness(
        RUNTIME_CLI_LOCAL, PROVIDER_OPENAI, "",
    ) == HARNESS_CODEX
    assert harness_applies(RUNTIME_CLI_LOCAL)
    assert harness_applies(RUNTIME_CLI_DOCKER)
    assert not harness_applies(RUNTIME_WS_LOCAL)


@pytest.mark.parametrize(("provider", "harness", "level", "expected"), [
    # Supported by the named harness → kept.
    (PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE, "xhigh", "xhigh"),
    (PROVIDER_OPENAI, HARNESS_CODEX, "minimal", "minimal"),
    # Level belongs to the other harness → cleared.
    (PROVIDER_OPENAI, HARNESS_CODEX, "xhigh", ""),
    (PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE, "minimal", ""),
    # Empty harness resolves from the provider before the check.
    (PROVIDER_OPENAI, "", "minimal", "minimal"),
    (PROVIDER_ANTHROPIC, "", "xhigh", "xhigh"),
    # Empty stays empty; harnesses with no levels clear everything.
    (PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE, "", ""),
    (PROVIDER_OPENAI, HARNESS_HERMES, "high", ""),
])
def test_normalize_inference_level(provider, harness, level, expected):
    assert normalize_inference_level(
        RUNTIME_CLI_LOCAL, provider, harness, level,
    ) == expected


def test_harness_registry_defaults_are_internally_consistent():
    for harness in VALID_HARNESSES:
        assert HARNESS_PROVIDERS[harness]
        assert HARNESS_PROVIDERS[harness] <= VALID_PROVIDERS
    for provider, harness in DEFAULT_HARNESS_FOR_PROVIDER.items():
        assert provider in HARNESS_PROVIDERS[harness]
