"""Claude Code harness — the default.

The turn protocol lives in ``agent/adapters/cli_session.py``; the
adapter constructs a ``ClaudeSession`` directly when
``harness.name() == "claude-code"``. Lift ``ClaudeSession`` behind
the harness boundary only when a second claude-like harness arrives.
"""

from __future__ import annotations

from .base import Harness


class ClaudeCodeHarness(Harness):
    def name(self) -> str:
        return "claude-code"

    def supports_claude_specific_tools(self) -> bool:
        return True

    def supported_providers(self) -> frozenset[str]:
        return frozenset({"anthropic"})
