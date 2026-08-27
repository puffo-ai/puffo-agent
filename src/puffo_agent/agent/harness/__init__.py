"""Protocol Drivers used by host-local and Docker Puffo runtimes."""

from dataclasses import dataclass
from typing import Any

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

@dataclass(frozen=True)
class UnsupportedDriver:
    harness: str
    diagnostic: str = "no local Driver implementation for this harness"


def build_driver(name: str, **kwargs: Any) -> Driver | UnsupportedDriver:
    """Construct only the two ratified Driver implementations.

    Process placement is supplied separately through ``process_factory``.
    """
    if name == "codex":
        return CodexAppServerDriver(**kwargs)
    if not name or name == "claude-code":
        return ClaudeCodeCliDriver(**kwargs)
    return UnsupportedDriver(name)


__all__ = [
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
