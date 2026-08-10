"""Declarative Claude Code harness used by the Docker runtime.

Host-local Claude Code execution uses :class:`ClaudeCodeCliDriver` instead;
the Docker adapter retains this lightweight metadata object and its
``ClaudeSession`` transport.
"""

from __future__ import annotations

from .base import DockerHarness


class ClaudeCodeHarness(DockerHarness):
    def name(self) -> str:
        return "claude-code"

    def supports_claude_specific_tools(self) -> bool:
        return True

    def supported_providers(self) -> frozenset[str]:
        return frozenset({"anthropic"})
