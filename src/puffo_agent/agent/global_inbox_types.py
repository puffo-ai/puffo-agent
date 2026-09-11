from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Sequence

from .context_controller import AdmissionCandidate
from .message_projection import canonical_target_parts, format_message_group
from .message_store import MessageStore, StoredMessage
from ..tasks import spawn

OUTPUT_TOOL_RESERVE_TOKENS = 4_096
CURRENT_TURN_VERSION = 2


def conservative_token_estimate(text: str) -> int:
    """Conservative leaf estimate used for Inbox prompt text."""
    return len(text.encode("utf-8"))


async def await_listener_with_runtime(
    listener: Awaitable[Any],
    runtime_task: asyncio.Task[Any],
    *,
    label: str,
) -> Any:
    """Stop a transport listener as soon as its Inbox consumer exits."""
    listener_task = spawn(listener, name="listener")
    try:
        done, _pending = await asyncio.wait(
            {listener_task, runtime_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if runtime_task in done:
            listener_failure: BaseException | None = None
            if listener_task in done:
                try:
                    await listener_task
                except (asyncio.CancelledError, Exception) as exc:
                    listener_failure = exc
            listener_diagnostic = (
                f"; listener also failed: {listener_failure}"
                if listener_failure is not None
                else ""
            )
            if runtime_task.cancelled():
                raise RuntimeError(
                    f"{label} was cancelled unexpectedly{listener_diagnostic}"
                )
            failure = runtime_task.exception()
            if failure is not None:
                raise RuntimeError(
                    f"{label} crashed: {failure}{listener_diagnostic}"
                ) from failure
            raise RuntimeError(f"{label} exited unexpectedly{listener_diagnostic}")
        return await listener_task
    finally:
        if not listener_task.done():
            listener_task.cancel()
            try:
                await listener_task
            except (asyncio.CancelledError, Exception):
                pass


@dataclass(frozen=True)
class MessageRoute:
    envelope_id: str
    kind: str
    space_id: str = ""
    channel_id: str = ""
    thread_root_id: str = ""
    dm_peer: str = ""

    @property
    def target(self) -> tuple[str, ...]:
        if self.kind == "dm":
            return ("dm", self.dm_peer)
        if self.kind == "thread":
            return ("thread", self.space_id, self.channel_id, self.thread_root_id)
        return ("channel", self.space_id, self.channel_id)


@dataclass(frozen=True)
class PlannedTurn:
    turn_id: str
    planning_cycle_key: str
    message_ids: tuple[str, ...]
    items: tuple[StoredMessage, ...]
    routes: tuple[MessageRoute, ...]
    targets: tuple[tuple[str, ...], ...]
    pending_targets: tuple[tuple[str, ...], ...]
    target_summary: str
    formatted_blocks: tuple[str, ...]
    provider_input: str
    formatted_tokens: int
    wrapper_overhead_tokens: int
    formatted_bytes: int
    wrapper_overhead_bytes: int
    more_available: bool = False
    notice_generation: int = 0
    notice_message_ids: tuple[str, ...] = ()
    requires_encryption: bool = False

    @property
    def candidate(self) -> AdmissionCandidate:
        first = (
            conservative_token_estimate(self.formatted_blocks[0])
            if self.formatted_blocks
            else 0
        )
        return AdmissionCandidate(
            planning_cycle_key=self.planning_cycle_key,
            formatted_batch_tokens=self.formatted_tokens,
            wrapper_overhead_tokens=self.wrapper_overhead_tokens,
            output_tool_reserve_tokens=OUTPUT_TOOL_RESERVE_TOKENS,
            payload=self,
            minimum_formatted_batch_tokens=first,
        )


@dataclass
class ActiveExactUnion:
    turn_id: str = ""
    message_ids: list[str] = field(default_factory=list)
    notice_message_ids: list[str] = field(default_factory=list)
    # In-memory only: rows whose plaintext has crossed a provider boundary.
    visible_message_ids: list[str] = field(default_factory=list)
    provider_session_id: str | None = None
    provider_turn_id: str | None = None
    routes: list[MessageRoute] = field(default_factory=list)
    through_by_channel: dict[tuple[str, str], int] = field(default_factory=dict)

    def clear(self) -> None:
        self.turn_id = ""
        self.message_ids.clear()
        self.notice_message_ids.clear()
        self.visible_message_ids.clear()
        self.provider_session_id = None
        self.provider_turn_id = None
        self.routes.clear()
        self.through_by_channel.clear()


@dataclass(frozen=True)
class RuntimeHealth:
    state: str = "idle"
    diagnostic: str = ""


ProcessOutcomeCallback = Callable[[str, str | None], None]


class TurnStatusLifecycle(Protocol):
    """Worker-owned mirror of one durable Global Inbox turn."""

    async def on_notice_admitted(
        self, *, turn_id: str, message_ids: tuple[str, ...]
    ) -> None: ...

    async def on_turn_active(
        self, *, turn_id: str, message_ids: tuple[str, ...]
    ) -> None: ...

    async def on_turn_terminal(
        self,
        *,
        turn_id: str,
        message_ids: tuple[str, ...],
        succeeded: bool,
        error_text: str | None,
    ) -> None: ...


@dataclass
class HeldStaging:
    # Compatibility/status snapshot only. Admission never reads this mutable
    # field; each callback uses HeldAdmissionEvidence instead.
    message_ids: tuple[str, ...] = ()
    latest_seq: int | None = None
    latest_envelope_id: str | None = None
    recovered_through_seq: int | None = None
    more_pending: bool = False
    synchronized: bool = False
    diagnostic: str = ""
    correlation_key: str = ""


@dataclass(frozen=True)
class HeldAdmissionEvidence:
    """Local-only recovery evidence frozen for one held send result."""

    active_turn_id: str
    provider_session_id: str
    provider_turn_id: str
    space_id: str
    channel_id: str
    latest_seq: int
    latest_envelope_id: str
    displayed_ids: tuple[str, ...]


@dataclass
class SendAttemptState:
    attempts: int = 0
    destinations: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)

    def record(self, destination: str, state: str) -> None:
        self.attempts += 1
        self.destinations.append(destination)
        self.states.append(state)

    def reset(self) -> None:
        self.attempts = 0
        self.destinations.clear()
        self.states.clear()


def route_for(item: StoredMessage) -> MessageRoute:
    target = canonical_target_parts(item)
    if target[0] == "dm":
        return MessageRoute(item.envelope_id, "dm", dm_peer=target[1])
    if target[0] == "thread":
        return MessageRoute(
            item.envelope_id,
            "thread",
            target[1],
            target[2],
            target[3],
        )
    return MessageRoute(
        item.envelope_id,
        "channel",
        target[1],
        target[2],
    )


def format_stored_message(
    item: StoredMessage,
    *,
    current_agent_aliases: Sequence[str] = (),
) -> str:
    """Readable per-message model view (legacy entry point for callers)."""
    return format_message_group((item,), current_agent_aliases=current_agent_aliases)


class BaselineAdapter:
    def __init__(self, store: MessageStore):
        self.store = store

    async def get_context_baseline_seq(
        self, space_id: str, channel_id: str
    ) -> int | None:
        return await self.store.get_context_baseline(space_id, channel_id)

    async def set_context_baseline_seq(
        self, space_id: str, channel_id: str, context_baseline_seq: int
    ) -> None:
        await self.store.set_context_baseline(space_id, channel_id, context_baseline_seq)


class ActiveBoundaryAdapter:
    """Inject the active turn id into the authoritative store query."""

    def __init__(self, store: MessageStore, active: ActiveExactUnion):
        self.store = store
        self.active = active

    async def get_active_turn_through_seq(
        self, space_id: str, channel_id: str
    ) -> int | None:
        if not self.active.turn_id:
            return None
        persisted = await self.store.get_model_visible_through_seq(
            self.active.turn_id, space_id, channel_id
        )
        # ``through_by_channel`` contains only Store-proven values written by
        # ``advance`` below (including a history-tool candidate), never raw
        # selected rows.
        advanced = self.active.through_by_channel.get((space_id, channel_id))
        values = [value for value in (persisted, advanced) if value is not None]
        return max(values) if values else None

    async def advance_active_turn_through_seq(
        self, space_id: str, channel_id: str, seq: int
    ) -> None:
        if not self.active.turn_id:
            return
        key = (space_id, channel_id)
        proven = await self.store.get_model_visible_through_seq(
            self.active.turn_id,
            space_id,
            channel_id,
            candidate_seq=seq,
        )
        # With no locally-known rows there is no local blocker to prove; this
        # preserves the stateless boundary adapter contract used before the
        # receipt path has populated the Store.
        if proven is not None:
            self.active.through_by_channel[key] = proven
        elif not await self.store.has_known_channel_rows_above_baseline(
            space_id,
            channel_id,
        ):
            self.active.through_by_channel[key] = seq


def opt_str(value: object) -> str | None:
    """Coerce to str and fold empty to None (NULL in the covers table)."""
    text = str(value or "")
    return text or None
