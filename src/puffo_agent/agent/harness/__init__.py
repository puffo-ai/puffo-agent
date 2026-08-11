"""Execution engines used by Puffo runtimes.

Docker executes Claude Code only. Host-local execution uses the long-lived
Claude Code and Codex Driver implementations.
"""

from dataclasses import dataclass
from typing import Any

from .base import DockerHarness
from .claude_code import ClaudeCodeHarness
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


def build_docker_harness(name: str) -> DockerHarness:
    """Resolve the sole executable Docker harness from ``agent.yml``.

    Claude Code is the compatibility default for configs without a harness.
    """
    if not name or name == "claude-code":
        return ClaudeCodeHarness()
    raise ValueError(
        f"Docker harness {name!r} is not executable; "
        "the supported Docker harness is 'claude-code'"
    )


@dataclass(frozen=True)
class UnsupportedDriver:
    harness: str
    diagnostic: str = "no local Driver implementation for this harness"


def build_driver(name: str, **kwargs: Any) -> Driver | UnsupportedDriver:
    """Construct only the two ratified Driver implementations.

    This factory is deliberately separate from :func:`build_docker_harness`.
    """
    if name == "codex":
        return CodexAppServerDriver(**kwargs)
    if not name or name == "claude-code":
        return ClaudeCodeCliDriver(**kwargs)
    return UnsupportedDriver(name)


__all__ = [
    "DockerHarness",
    "ClaudeCodeHarness",
    "build_docker_harness",
    "Driver",
    "RuntimeRef",
    "SessionRef",
    "TurnRef",
    "PermissionRef",
    "UnsupportedCapability",
    "UnsupportedDriver",
    "CodexAppServerDriver",
    "CodexDriver",
    "ClaudeCodeCliDriver",
    "ClaudeDriver",
    "build_driver",
]
