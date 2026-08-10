"""Execution engines used by Puffo runtimes.

Runtime answers WHERE the agent executes; harness answers WHAT.
Docker retains three declarative ``DockerHarness`` metadata types.
The host-local runtime uses the long-lived Driver implementations for
``claude-code`` and ``codex`` only.
"""

from .base import DockerHarness
from .claude_code import ClaudeCodeHarness
from .gemini_cli import GeminiCLIHarness
from .hermes import HermesHarness
from .driver import (
    Driver,
    RuntimeRef,
    SessionRef,
    TurnRef,
    PermissionRef,
    UnsupportedCapability,
)
from .codex_driver import CodexAppServerDriver, CodexDriver
from .claude_code_driver import ClaudeCodeCliDriver, ClaudeDriver
from dataclasses import dataclass
from typing import Any


def build_docker_harness(name: str) -> DockerHarness:
    """Resolve Docker engine metadata from ``agent.yml``.

    Claude Code is the compatibility default for configs without a harness.
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


@dataclass(frozen=True)
class UnsupportedDriver:
    harness: str
    diagnostic: str = "no local Driver implementation for this harness"


def build_driver(name: str, **kwargs: Any) -> Driver | UnsupportedDriver:
    """Construct only the two ratified Driver implementations.

    This factory is deliberately separate from :func:`build_docker_harness`,
    which is the declarative registry used by the Docker runtime.
    """
    if name == "codex":
        return CodexAppServerDriver(**kwargs)
    if not name or name == "claude-code":
        return ClaudeCodeCliDriver(**kwargs)
    return UnsupportedDriver(name)


__all__ = [
    "DockerHarness",
    "ClaudeCodeHarness",
    "GeminiCLIHarness",
    "HermesHarness",
    "build_docker_harness",
    "Driver",
    "RuntimeRef", "SessionRef", "TurnRef", "PermissionRef",
    "UnsupportedCapability", "UnsupportedDriver",
    "CodexAppServerDriver", "CodexDriver",
    "ClaudeCodeCliDriver", "ClaudeDriver",
    "build_driver",
]
