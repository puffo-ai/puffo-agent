"""Hermes harness — Nous Research's agent engine.

Hermes has no stream-json protocol; the supported programmatic path
is one-shot ``hermes chat -q <message>`` per turn (interactive mode
requires a TTY). Multi-turn continuity is hermes-managed in
``~/.hermes/state.db`` and resumed via ``--continue``. Cold start
per turn is ~3-7s.

Auth: hermes auto-discovers Claude Code's credential file at
``$HOME/.claude/.credentials.json``. Note: when used via OAuth
tokens, Anthropic routes usage to its ``extra_usage`` billing pool
— not the Claude subscription.

Runtime support: cli-docker only; cli-local rejects this harness at
construction. Claude-Code-specific MCP tools (install_skill,
refresh, etc.) are disabled since hermes uses its own skill /
session systems.
"""

from __future__ import annotations

from .base import Harness


class HermesHarness(Harness):
    def name(self) -> str:
        return "hermes"

    def supported_providers(self) -> frozenset[str]:
        # Anthropic via Claude Code credentials; OpenAI via
        # OPENAI_API_KEY. Google isn't supported by upstream hermes.
        return frozenset({"anthropic", "openai"})
