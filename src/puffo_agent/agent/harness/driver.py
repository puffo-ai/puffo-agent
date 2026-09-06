"""Provider-neutral command/event contract for interactive harnesses.

The native diagnostic attached to :class:`HarnessEvent` is intentionally an
opaque, non-serializable object.  It is available to an in-process debugger,
but is never part of the public event or structured logging contracts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeRef:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SessionRef:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class TurnRef:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class PermissionRef:
    value: str

    def __str__(self) -> str:
        return self.value


class SteerCapability(str, Enum):
    NONE = "none"
    GATED = "gated"
    CURRENT_TURN = "current_turn"


class CancelCapability(str, Enum):
    NONE = "none"
    TYPED = "typed"


class ContextStatusCapability(str, Enum):
    NONE = "none"
    PUSH = "push"
    PULL = "pull"
    PUSH_AND_PULL = "push_and_pull"


class CompactCapability(str, Enum):
    NONE = "none"
    TYPED = "typed"
    SESSION_COMMAND = "session_command"


class RuntimeLifecycle(str, Enum):
    """How the harness process exists across turns.

    Read by the layers above the Driver to decide warm/reload/restart and
    whether a session can be held open between turns. The Driver still
    absorbs the mechanics: a ``PER_TURN_CHILD`` driver may accept ``open()``
    as a logical session and spawn only on ``start_turn()``.
    """

    PERSISTENT_CHILD = "persistent_child"
    PER_TURN_CHILD = "per_turn_child"
    IN_PROCESS = "in_process"


class BusyDelivery(str, Enum):
    """What the driver accepts while one of its turns is already running.

    Declared, not inferred: the caller must not probe by sending and seeing
    what happens. ``REJECT`` means the driver takes nothing mid-turn and the
    caller owns the pending input.
    """

    STEER = "steer"
    COALESCE = "coalesce"
    QUEUE = "queue"
    REJECT = "reject"


class PermissionDecision(str, Enum):
    APPROVE = "approved"
    DENY = "denied"


@dataclass(frozen=True, slots=True)
class DriverCapabilities:
    session_resume: bool
    inflight_turn_recovery: bool
    steer: SteerCapability | str
    cancel: CancelCapability | str
    context_status: ContextStatusCapability | str
    compact: CompactCapability | str
    permission_bridge: bool
    # Defaults describe the two drivers that shipped before this field existed;
    # a new driver must declare both explicitly (see
    # ``test_shipped_drivers_declare_lifecycle_and_busy_delivery``).
    lifecycle: RuntimeLifecycle | str = RuntimeLifecycle.PERSISTENT_CHILD
    busy_delivery: BusyDelivery | str = BusyDelivery.REJECT


@dataclass(frozen=True, slots=True)
class ProtocolDiagnostics:
    executable_version: str = ""
    schema_source: str = "pinned"
    native_capabilities: tuple[str, ...] = ()
    runtime_lifecycle: str = ""
    busy_delivery: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """Provider-neutral stdio MCP server passed at session creation."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    workspace_dir: str
    model: str = ""
    inference_level: str = ""
    system_prompt: str = ""
    executable: str = ""
    launch_args: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    mcp_servers: tuple[McpServerSpec, ...] = ()
    permission_mode: str = "bypassPermissions"
    sandbox: str = "danger-full-access"
    task_timeout_seconds: float = 1800.0
    auto_compact_threshold_pct: float | None = None
    auto_compact_threshold_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TurnInput:
    # Exactly one current Puffo semantic input. Native resumable sessions own
    # their prior conversation history; callers must not concatenate it here.
    content: str
    client_correlation_id: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompactRequest:
    instructions: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeOpened:
    runtime_ref: RuntimeRef
    session_ref: SessionRef
    native_session_id: str
    resumed: bool
    capabilities: DriverCapabilities
    diagnostics: ProtocolDiagnostics

    def __post_init__(self) -> None:
        """Project admission semantics into the diagnostic snapshot.

        Drivers declare these truths once in ``DriverCapabilities``. Keeping
        the copy here centralized prevents a diagnostic string from drifting
        away from the control-flow value the Runtime Manager actually reads.
        """
        if not isinstance(self.diagnostics, ProtocolDiagnostics):
            return
        lifecycle = getattr(
            self.capabilities.lifecycle,
            "value",
            self.capabilities.lifecycle,
        )
        busy_delivery = getattr(
            self.capabilities.busy_delivery,
            "value",
            self.capabilities.busy_delivery,
        )
        object.__setattr__(
            self,
            "diagnostics",
            replace(
                self.diagnostics,
                runtime_lifecycle=str(lifecycle),
                busy_delivery=str(busy_delivery),
            ),
        )


@dataclass(frozen=True, slots=True)
class TurnStarted:
    turn_ref: TurnRef
    native_turn_id: str = ""
    accepted: bool = True
    delivery: str = "accepted"
    replay_id: str = ""


@dataclass(frozen=True, slots=True)
class InputReceipt:
    accepted: bool
    turn_ref: TurnRef
    correlation_id: str = ""
    delivery: str = "accepted"
    session_reusable: bool = True


@dataclass(frozen=True, slots=True)
class CancelReceipt:
    accepted: bool
    turn_ref: TurnRef


@dataclass(frozen=True, slots=True)
class ContextStatus:
    used_tokens: int | None = None
    context_window: int | None = None
    stale: bool = False
    measured_at: str = ""
    auto_compact_threshold_tokens: int | None = None
    auto_compact_enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class CompactReceipt:
    accepted: bool
    operation_ref: str = ""


@dataclass(frozen=True, slots=True)
class PermissionReceipt:
    accepted: bool
    permission_ref: PermissionRef


@dataclass(frozen=True, slots=True)
class UnsupportedCapability:
    capability: str
    diagnostic: str = "capability is not supported by this driver"

    @property
    def accepted(self) -> bool:
        return False


class HarnessEventType(str, Enum):
    RUNTIME_READY = "runtime.ready"
    RUNTIME_WARNING = "runtime.warning"
    RUNTIME_FAILED = "runtime.failed"
    RUNTIME_EXITED = "runtime.exited"
    SESSION_OPENED = "session.opened"
    SESSION_RESUMED = "session.resumed"
    SESSION_UPDATED = "session.updated"
    TURN_STARTED = "turn.started"
    ASSISTANT_DELTA = "turn.assistant_delta"
    ASSISTANT_COMPLETED = "turn.assistant_completed"
    TOOL_STARTED = "turn.tool_started"
    TOOL_UPDATED = "turn.tool_updated"
    TOOL_COMPLETED = "turn.tool_completed"
    PERMISSION_REQUESTED = "turn.permission_requested"
    PERMISSION_UPDATED = "turn.permission_updated"
    CONTEXT_UPDATED = "turn.context_updated"
    TURN_COMPLETED = "turn.completed"
    TURN_ABANDONED = "turn.abandoned"
    # A provider turn the daemon did not start: a harness background task
    # woke the model after the daemon's turn was finalized. Reported so the
    # daemon can bind a turn to it instead of leaving the model unbound.
    AUTONOMOUS_STARTED = "turn.autonomous_started"
    AUTONOMOUS_COMPLETED = "turn.autonomous_completed"
    COMPACTION_STARTED = "compaction.started"
    COMPACTION_COMPLETED = "compaction.completed"
    # Provider compaction failed; the session itself stays usable. The
    # runtime manager fails the outstanding compaction future on this
    # instead of letting callers wait out their timeout — and it must
    # never be spelled as RUNTIME_EXITED, which retires the session.
    COMPACTION_FAILED = "compaction.failed"


class _NativeDiagnostic:
    """Opaque wrapper that deliberately rejects serialization."""

    __slots__ = ("payload",)

    def __init__(self, payload: Any):
        self.payload = payload

    def __reduce__(self):
        raise TypeError("native diagnostics are deliberately non-serializable")


@dataclass(frozen=True, slots=True)
class HarnessEvent:
    type: HarnessEventType | str
    driver: str
    session_ref: SessionRef
    turn_ref: TurnRef | None = None
    native_session_id: str = ""
    native_turn_id: str = ""
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    data: Mapping[str, Any] = field(default_factory=dict)
    _native: _NativeDiagnostic | None = field(
        default=None, repr=False, compare=False
    )

    @classmethod
    def normalized(cls, *, native_payload: Any = None, **kwargs: Any):
        return cls(
            **kwargs,
            _native=(
                _NativeDiagnostic(native_payload)
                if native_payload is not None
                else None
            ),
        )

    @property
    def native_diagnostic(self) -> Any:
        return self._native.payload if self._native is not None else None


class Driver(ABC):
    """The exact common command/event surface implemented by Drivers."""

    def current_capabilities(self) -> DriverCapabilities | None:
        """Return capabilities learned after ``open``, when available."""
        return None

    @abstractmethod
    async def open(
        self, spec: RuntimeSpec, resume: SessionRef | None = None
    ) -> RuntimeOpened: ...

    @abstractmethod
    async def start_turn(
        self, input: TurnInput
    ) -> TurnStarted | UnsupportedCapability: ...

    @abstractmethod
    async def steer_turn(
        self, turn: TurnRef, input: TurnInput
    ) -> InputReceipt | UnsupportedCapability: ...

    @abstractmethod
    async def cancel_turn(
        self, turn: TurnRef
    ) -> CancelReceipt | UnsupportedCapability: ...

    @abstractmethod
    async def context_status(
        self,
    ) -> ContextStatus | UnsupportedCapability: ...

    @abstractmethod
    async def compact(
        self, request: CompactRequest
    ) -> CompactReceipt | UnsupportedCapability: ...

    @abstractmethod
    async def resolve_permission(
        self, request: PermissionRef, decision: PermissionDecision
    ) -> PermissionReceipt | UnsupportedCapability: ...

    @abstractmethod
    def events(self) -> AsyncIterator[HarnessEvent]: ...

    @abstractmethod
    async def close(self) -> None: ...
