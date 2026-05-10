"""Harness interface.

A harness exposes:
  - ``name()`` — identifier for status output and MCP tool gating.
  - ``supports_claude_specific_tools()`` — gates ``install_skill`` /
    ``refresh`` / etc., which assume Claude Code's skills layout.
  - ``supported_providers()`` — for runtime-matrix validation.

The turn protocol lives on the adapter side; the harness owns its
own session model (claude-code = persistent stream-json subprocess;
hermes = one-shot per turn) but both look the same to the adapter.
Runtime adapters still own credential linking, HOME overrides, and
docker-exec vs host subprocess — the harness is agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class HarnessTurn:
    """Per-turn input to a harness. Decoupled from ``TurnContext`` so
    harnesses don't depend on adapter internals.
    """
    user_message: str
    system_prompt: str
    # Absolute workspace path; cwd + project-level .claude/ root for
    # claude-code, cwd for ``hermes chat -q``.
    workspace_dir: str
    # Model id (empty = harness default). Forwarded via ``--model``.
    model: str


class Harness(ABC):
    """Agent engine abstraction. See module docstring."""

    @abstractmethod
    def name(self) -> str:
        """Stable identifier — ``"claude-code"`` / ``"hermes"`` / etc."""

    def supports_claude_specific_tools(self) -> bool:
        """True when this harness uses Claude Code's skills-dir
        format + ``--resume`` session protocol. Default False so new
        harnesses opt in deliberately."""
        return False

    def supported_providers(self) -> frozenset[str]:
        """Model providers this harness can drive. Empty = "not
        declared"; concrete harnesses should override.
        """
        return frozenset()
