"""Docker harness construction contracts."""

from __future__ import annotations

import pytest

from puffo_agent.agent.harness import ClaudeCodeHarness, build_docker_harness


@pytest.mark.parametrize("name", ["", "claude-code"])
def test_build_docker_harness_constructs_claude_code(name: str) -> None:
    harness = build_docker_harness(name)

    assert isinstance(harness, ClaudeCodeHarness)
    assert harness.name() == "claude-code"


@pytest.mark.parametrize("name", ["hermes", "gemini-cli"])
def test_build_docker_harness_rejects_design_only_names(name: str) -> None:
    """A known config/migration name must never become executable."""
    with pytest.raises(ValueError, match="supported Docker harness is 'claude-code'"):
        build_docker_harness(name)
