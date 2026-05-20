"""Codex harness — OpenAI's `codex` CLI as a long-running agent.

Unlike Claude Code (stream-json subprocess) or hermes (one-shot exec),
codex's persistent integration surface is the **app-server**: a
JSON-RPC stdio process that exposes ``newConversation`` / ``sendUserTurn``
/ ``item/*`` event streams. The turn protocol lives in
``agent/adapters/codex_session.py``; this class is just metadata for
the runtime matrix and CLI registration.
"""

from __future__ import annotations

from .base import Harness


class CodexHarness(Harness):
    def name(self) -> str:
        return "codex"

    def supports_claude_specific_tools(self) -> bool:
        # codex has no ``.claude/skills/`` concept; ``install_skill`` /
        # ``refresh`` / etc. would have nowhere to write. Self-update
        # for codex agents in v1 is limited to ``reload_system_prompt``
        # (re-investigating AGENTS.md).
        return False

    def supported_providers(self) -> frozenset[str]:
        return frozenset({"openai"})
