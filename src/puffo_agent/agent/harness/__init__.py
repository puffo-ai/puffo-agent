"""Protocol Drivers used by host-local and Docker Puffo runtimes."""

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from .driver import (
    Driver,
    RuntimeRef,
    SessionRef,
    TurnRef,
    PermissionRef,
    UnsupportedCapability,
)
from .drivers.codex import CodexAppServerDriver, CodexDriver
from .drivers.claude_code import ClaudeCodeCliDriver, ClaudeDriver
from .drivers.opencode import OpenCodeCliDriver, OpenCodeDriver
from .drivers.acp import AcpDriver, GenericAcpDriver
from .drivers.pi import (
    PI_CAPABILITIES,
    PiDriver,
    PiToolBridgeUnavailableError,
    verify_pi_tool_bridge,
)

_DRIVER_FACTORIES: dict[str, Callable[..., Driver]] = {
    "acp": AcpDriver,
    "claude-code": ClaudeCodeCliDriver,
    "codex": CodexAppServerDriver,
    "opencode": OpenCodeDriver,
    "pi": PiDriver,
}
SUPPORTED_LOCAL_DRIVERS = frozenset(_DRIVER_FACTORIES)


@dataclass(frozen=True)
class UnsupportedDriver:
    harness: str
    diagnostic: str = "no local Driver implementation for this harness"


def build_driver(name: str, **kwargs: Any) -> Driver | UnsupportedDriver:
    """Construct only Driver implementations admitted for production use.

    Process placement is supplied separately through ``process_factory``.
    """
    normalized_name = name or "claude-code"
    factory = _DRIVER_FACTORIES.get(normalized_name)
    if factory is not None:
        return factory(**kwargs)
    return UnsupportedDriver(name)


__all__ = [
    "Driver",
    "RuntimeRef",
    "SessionRef",
    "TurnRef",
    "PermissionRef",
    "UnsupportedCapability",
    "UnsupportedDriver",
    "SUPPORTED_LOCAL_DRIVERS",
    "CodexAppServerDriver",
    "CodexDriver",
    "ClaudeCodeCliDriver",
    "ClaudeDriver",
    "OpenCodeCliDriver",
    "OpenCodeDriver",
    "AcpDriver",
    "GenericAcpDriver",
    "PiDriver",
    "PI_CAPABILITIES",
    "PiToolBridgeUnavailableError",
    "verify_pi_tool_bridge",
    "build_driver",
]
