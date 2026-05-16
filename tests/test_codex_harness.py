"""Phase 1 tests — codex harness metadata + runtime_matrix triple
validation. Pure data tests; no subprocess, no I/O.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.harness import build_harness, CodexHarness
from puffo_agent.portal.runtime_matrix import (
    HARNESS_CODEX,
    HARNESS_PROVIDERS,
    PROVIDER_OPENAI,
    RUNTIME_CLI_LOCAL,
    RUNTIME_CLI_DOCKER,
    DEFAULT_HARNESS_FOR_PROVIDER,
    VALID_HARNESSES,
    validate_triple,
)


def test_build_harness_returns_codex_instance():
    h = build_harness("codex")
    assert isinstance(h, CodexHarness)
    assert h.name() == "codex"


def test_codex_harness_metadata():
    h = CodexHarness()
    assert h.name() == "codex"
    # No skills concept — install_skill / refresh shouldn't be offered.
    assert h.supports_claude_specific_tools() is False
    # OpenAI only.
    assert h.supported_providers() == frozenset({"openai"})


def test_codex_in_valid_harnesses():
    assert HARNESS_CODEX in VALID_HARNESSES


def test_codex_provider_constraint():
    assert HARNESS_PROVIDERS[HARNESS_CODEX] == frozenset({PROVIDER_OPENAI})


def test_codex_is_not_the_default_for_openai():
    # Plan §0/§1 — codex is opt-in for openai; default stays hermes
    # so existing openai agents don't get a runtime change.
    assert DEFAULT_HARNESS_FOR_PROVIDER[PROVIDER_OPENAI] != HARNESS_CODEX


# ─────────────────────────────────────────────────────────────────────────────
# validate_triple
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_codex_with_openai_on_cli_local():
    r = validate_triple(RUNTIME_CLI_LOCAL, PROVIDER_OPENAI, HARNESS_CODEX)
    assert r.ok, r.error


def test_validate_codex_with_openai_on_cli_docker():
    # No technical reason cli-docker shouldn't pass triple validation
    # — even though the docker adapter doesn't dispatch codex yet, the
    # matrix is "is this combo conceptually valid"; the adapter
    # rejects gracefully if it can't actually run.
    r = validate_triple(RUNTIME_CLI_DOCKER, PROVIDER_OPENAI, HARNESS_CODEX)
    assert r.ok, r.error


def test_validate_codex_rejects_anthropic_provider():
    r = validate_triple(RUNTIME_CLI_LOCAL, "anthropic", HARNESS_CODEX)
    assert not r.ok
    assert "codex" in r.error and "anthropic" in r.error


def test_validate_codex_rejects_google_provider():
    r = validate_triple(RUNTIME_CLI_LOCAL, "google", HARNESS_CODEX)
    assert not r.ok
    assert "codex" in r.error


def test_validate_codex_with_empty_provider_accepted():
    # Empty provider means "use default" — accepted by the matrix;
    # adapter resolves the actual provider separately.
    r = validate_triple(RUNTIME_CLI_LOCAL, "", HARNESS_CODEX)
    assert r.ok, r.error


def test_unknown_harness_still_rejected():
    # Sanity — adding codex didn't accidentally open the gate.
    r = validate_triple(RUNTIME_CLI_LOCAL, PROVIDER_OPENAI, "bogus")
    assert not r.ok
    assert "bogus" in r.error
