"""Declarative engine metadata used only by the Docker runtime.

A harness exposes:
  - ``name()`` — identifier for status output and MCP tool gating.
  - ``supports_claude_specific_tools()`` — gates ``install_skill`` /
    ``refresh`` / etc., which assume Claude Code's skills layout.
  - ``supported_providers()`` — for runtime-matrix validation.

Interactive host-local engines implement :class:`driver.Driver` instead.
The Docker adapter owns process/session behavior; these objects only declare
the selected engine's name, provider compatibility, and tool-layout traits.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class DockerHarness(ABC):
    """Metadata for an engine launched by :class:`DockerCLIAdapter`."""

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
