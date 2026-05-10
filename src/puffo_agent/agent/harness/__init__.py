"""Harness abstraction — which agent engine runs inside a runtime.

Runtime answers WHERE the agent executes; harness answers WHAT.
Only meaningful for the CLI runtimes; ``chat-local`` / ``sdk-local``
ignore the field. Three harnesses ship: ``claude-code`` (Anthropic),
``hermes`` (Anthropic + OpenAI), ``gemini-cli`` (Google). Each
declares ``supported_providers`` so the runtime matrix can reject
mismatched triples at load time.
"""

from .base import Harness, HarnessTurn
from .claude_code import ClaudeCodeHarness
from .gemini_cli import GeminiCLIHarness
from .hermes import HermesHarness


def build_harness(name: str) -> Harness:
    """Resolve a harness name from agent.yml. Default Claude Code so
    agents without the field keep existing behaviour.
    """
    if not name or name == "claude-code":
        return ClaudeCodeHarness()
    if name == "hermes":
        return HermesHarness()
    if name == "gemini-cli":
        return GeminiCLIHarness()
    raise ValueError(
        f"unknown harness {name!r}: expected one of "
        "'claude-code', 'hermes', 'gemini-cli'"
    )


__all__ = [
    "Harness",
    "HarnessTurn",
    "ClaudeCodeHarness",
    "GeminiCLIHarness",
    "HermesHarness",
    "build_harness",
]
