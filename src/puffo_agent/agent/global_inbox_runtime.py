"""Serial orchestration for the durable global agent Inbox.

Storage, prefix selection, provider context control, and sending deliberately
remain in their leaf modules.  This module joins those contracts and owns only
the mutable state of the one active turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, Sequence

from .channel_audience import (
    ChannelAudienceLoader,
    load_channel_audiences,
    project_notice_targets,
)
from .context_controller import (
    AdmissionCandidate,
    ContextController,
    DecisionOutcome,
    ProviderAdmissionEvent,
    ToolResultAdmission,
)
from .inbox_scheduler import (
    COALESCE_SECONDS,
    MAX_ESTIMATED_TOKENS,
    MAX_FORMATTED_BYTES,
    InboxCoalescer,
    InboxNoticeDelivery,
    InboxPlanner,
    NoticeDeliveryCapability,
    PlannedBatch,
)
from .message_store import (
    PRIOR_CONTEXT_MAX_BYTES,
    PRIOR_CONTEXT_MAX_ITEMS,
    MessageStore,
    ProcessingState,
    StoredMessage,
)
from ._logging import log_runtime_event
from .message_projection import (
    CONTEXT_VERSION,
    format_inbox_notice,
    format_message_group,
)
from .reminder_scheduler import ReminderScheduler
from .shared_content import INBOX_TURN_CUE

logger = logging.getLogger(__name__)

# A degrade is a transient provider incident, never a durable verdict about
# pending Inbox work.  Recovery is a bounded backoff window the runtime re-arms
# itself, so requeued rows stay retryable without depending on unrelated ingress.
DEGRADED_RECOVERY_BASE_SECONDS = 5.0
DEGRADED_RECOVERY_MAX_SECONDS = 300.0

from .global_inbox_held import HeldRecoverySource
from .global_inbox_send import TrackingSendDelegate
from .global_inbox_admission import InboxAdmissionMixin
from .global_inbox_types import (
    ActiveBoundaryAdapter,
    ActiveExactUnion,
    BaselineAdapter,
    CURRENT_TURN_VERSION,
    HeldAdmissionEvidence,
    HeldStaging,
    MessageRoute,
    OUTPUT_TOOL_RESERVE_TOKENS,
    PlannedTurn,
    RuntimeHealth,
    SendAttemptState,
    await_listener_with_runtime,
    conservative_token_estimate,
    format_stored_message,
    route_for,
)

# Compatibility facade: callers have historically imported these protocol
# values from this module even though their ownership is now narrower.
__all__ = [
    "ActiveBoundaryAdapter",
    "ActiveExactUnion",
    "BaselineAdapter",
    "CURRENT_TURN_VERSION",
    "CONTEXT_VERSION",
    "GlobalInboxRuntime",
    "HeldAdmissionEvidence",
    "HeldRecoverySource",
    "HeldStaging",
    "MessageRoute",
    "OUTPUT_TOOL_RESERVE_TOKENS",
    "PRIOR_CONTEXT_MAX_BYTES",
    "PRIOR_CONTEXT_MAX_ITEMS",
    "PlannedTurn",
    "RuntimeHealth",
    "SendAttemptState",
    "TrackingSendDelegate",
    "ToolResultAdmission",
    "TurnStatusLifecycle",
    "await_listener_with_runtime",
    "conservative_token_estimate",
    "format_stored_message",
    "format_message_group",
    "route_for",
]

TurnRunner = Callable[[PlannedTurn], Awaitable[Any]]
UnfitPolicy = Callable[..., bool | Awaitable[bool]]


class TurnStatusLifecycle(Protocol):
    """Optional worker-owned mirror of the active Global Inbox turn.

    The runtime owns only the durable turn lifecycle; it notifies this
    callback with immutable notice and active-union snapshots so the worker
    can drive Server status without moving status policy in here.
    """

    async def on_notice_admitted(
        self, *, turn_id: str, message_ids: tuple[str, ...]
    ) -> None:
        """Report that a provider accepted an Inbox notice for this turn."""

    async def on_turn_active(
        self, *, turn_id: str, message_ids: tuple[str, ...]
    ) -> None:
        """Report that the active union is (or has grown to) ``message_ids``."""

    async def on_turn_terminal(
        self,
        *,
        turn_id: str,
        message_ids: tuple[str, ...],
        succeeded: bool,
        error_text: str | None,
    ) -> None:
        """Settle the exact active union in one terminal status batch."""


class GlobalInboxRuntime(InboxAdmissionMixin):
    """One serial provider boundary over the durable global Inbox."""

    def __init__(
        self,
        *,
        store: MessageStore,
        adapter: Any,
        run_turn: TurnRunner,
        workspace: str | Path,
        context_controller: ContextController | None = None,
        planner: InboxPlanner | None = None,
        coalescer: InboxCoalescer | None = None,
        formatter: Callable[[StoredMessage], str] = format_stored_message,
        estimator: Callable[[str], int] = conservative_token_estimate,
        unfit_policy: UnfitPolicy | None = None,
        coordinator: Any | None = None,
        max_context_decisions: int = 12,
        max_api_retries: int = 2,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        held_catchup: Callable[[str], Awaitable[bool]] | None = None,
        send_mode_keys: Sequence[str] = (),
        agent_id: str = "",
        notice_delivery: InboxNoticeDelivery | None = None,
        runtime_event_outbox: Any | None = None,
        reminder_scheduler: ReminderScheduler | None = None,
        status_lifecycle: TurnStatusLifecycle | None = None,
        channel_audience_loader: ChannelAudienceLoader | None = None,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.run_turn = run_turn
        self.workspace = Path(workspace)
        self.context_controller = context_controller or ContextController(adapter)
        self.planner = planner or InboxPlanner()
        self.coalescer = coalescer or InboxCoalescer()
        self._configured_formatter = formatter
        self.estimator = estimator
        self.unfit_policy = unfit_policy or (lambda *_args, **_kwargs: True)
        self.coordinator = coordinator
        self.active = ActiveExactUnion()
        self.attempts = SendAttemptState()
        self.health = RuntimeHealth()
        # Per ``(space_id, channel_id)``: sends serialize per target, so two
        # channels of one turn can be held and recovered concurrently and must
        # never project each other's synchronization status.
        self.held: dict[tuple[str, str], HeldStaging] = {}
        self._held_admission_evidence: dict[
            tuple[str, str, str, str, int, str], HeldAdmissionEvidence
        ] = {}
        self._boundary = asyncio.Lock()
        # Provider binding and direct Inbox admission can race on the first
        # turn of a newly opened harness session. Both mutate the same durable
        # turn owner and in-memory exact union, so they share one short lock.
        self._turn_state_lock = asyncio.Lock()
        self._stopping = False
        self._degraded = False
        self._degraded_until: float | None = None
        self._degraded_attempts = 0
        self._defer_requeued_recovery = False
        self.max_context_decisions = max_context_decisions
        self.max_api_retries = max_api_retries
        self.retry_sleep = retry_sleep
        self.send_mode_keys = tuple(dict.fromkeys(key for key in send_mode_keys if key))
        self.agent_id = agent_id
        # ``send_mode_keys`` is the existing runtime identity-alias set used
        # by the send-mode guard (normally the configured agent id and the
        # wire slug).  Inbox attribution is derived from the same identities;
        # no durable or provider state is introduced.
        self.formatter = self._format_for_provider
        capability = getattr(adapter, "inbox_notice_delivery_capability", None)
        self.notice_delivery = notice_delivery or InboxNoticeDelivery(
            capability if callable(capability) else NoticeDeliveryCapability.NEXT_TURN
        )
        self._busy_notice_task: asyncio.Task[None] | None = None
        self._busy_notice_dirty = False
        self._busy_notice_delay_seconds = COALESCE_SECONDS
        self.runtime_event_outbox = runtime_event_outbox
        self.reminder_scheduler = reminder_scheduler or ReminderScheduler(
            store=store,
            notify=self.notify,
        )
        self.status_lifecycle = status_lifecycle
        self.channel_audience_loader = channel_audience_loader
        self.send_delegate: TrackingSendDelegate | None = None
        self.held_recovery_source = HeldRecoverySource(
            self,
            catchup_pending=held_catchup,
        )

    def _current_agent_identity_aliases(self) -> tuple[str, ...]:
        """Return only runtime-owned identities usable for self attribution."""
        values: list[str] = [self.agent_id, *self.send_mode_keys]
        for owner in (self.adapter, self.coordinator):
            for name in ("slug", "agent_id", "agent_slug", "self_slug"):
                value = getattr(owner, name, "")
                if value:
                    values.append(str(value))
        return tuple(dict.fromkeys(value for value in values if value))

    def _format_for_provider(self, item: StoredMessage) -> str:
        """Apply current-Agent attribution only to the production formatter."""
        if self._configured_formatter is format_stored_message:
            return format_stored_message(
                item,
                current_agent_aliases=self._current_agent_identity_aliases(),
            )
        return self._configured_formatter(item)

    @property
    def current_turn_path(self) -> Path:
        return self.workspace / ".puffo-agent" / "current_turn.json"

    def notify(self) -> None:
        self._degraded = False
        self._degraded_until = None
        self._degraded_attempts = 0
        delay = self._busy_notice_delay_seconds if self.active.turn_id else 0.0
        self.coalescer.notify(delay_seconds=delay)
        self._schedule_busy_notice()

    async def create_reminder(
        self,
        *,
        content: str,
        target: str,
        intended_at: str,
    ) -> dict[str, object]:
        """Create local reminder intent without introducing provider policy."""
        return await self.reminder_scheduler.create_reminder(
            content=content,
            target=target,
            intended_at=intended_at,
        )

    async def list_reminders(
        self,
        *,
        state: str = "",
        limit: int = 50,
    ) -> dict[str, object]:
        return await self.reminder_scheduler.list_reminders(
            state=state,
            limit=limit,
        )

    async def cancel_reminder(self, *, reminder_id: str) -> dict[str, object]:
        return await self.reminder_scheduler.cancel_reminder(
            reminder_id=reminder_id,
        )

    async def replace_reminder(
        self,
        *,
        reminder_id: str,
        content: str = "",
        target: str = "",
        intended_at: str = "",
    ) -> dict[str, object]:
        return await self.reminder_scheduler.replace_reminder(
            reminder_id=reminder_id,
            content=content,
            target=target,
            intended_at=intended_at,
        )

    def notify_delivery(self) -> None:
        self.held_recovery_source.notify_delivery()

    @property
    def _admits_tool_results_on_return(self) -> bool:
        return (
            getattr(
                self.adapter,
                "tool_result_admission_boundary",
                "provider_completion",
            )
            == "tool_return"
        )

    async def _admit_returned_tool_result(
        self,
        callback: Callable[[ProviderAdmissionEvent], Awaitable[None]],
        *,
        planning_cycle_key: str,
        provider_session_id: str,
        provider_turn_id: str | None = None,
    ) -> None:
        await callback(
            ProviderAdmissionEvent(
                planning_cycle_key=planning_cycle_key,
                provider_session_id=provider_session_id,
                provider_turn_id=(
                    provider_turn_id
                    if provider_turn_id is not None
                    else self.active.provider_turn_id
                ),
                admitted_at=datetime.now(timezone.utc),
            )
        )

    async def run(self) -> None:
        reminder_task = asyncio.create_task(self.reminder_scheduler.run())
        try:
            await self.recover_current_turn()
            await self.recover_orphaned_turns()
            if self._defer_requeued_recovery:
                # Consume the recovery wake without immediately feeding the same
                # failed durable union through the initial-turn path.
                await self.coalescer.wait_for_burst()
            elif await self.store.get_pending(limit=1):
                if await self.store.get_notice_candidates(
                    self.adapter.get_provider_session_id()
                ):
                    self.notify()
            while not self._stopping:
                burst_task = asyncio.create_task(self.coalescer.wait_for_burst())
                done, _pending = await asyncio.wait(
                    {burst_task, reminder_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if reminder_task in done:
                    if not burst_task.done():
                        burst_task.cancel()
                        try:
                            await burst_task
                        except asyncio.CancelledError:
                            pass
                    # A scheduler error is an owning-runtime error, never a
                    # silently disabled timer loop.
                    await reminder_task
                    if self._stopping:
                        return
                    raise RuntimeError("reminder scheduler exited unexpectedly")
                await burst_task
                if self._stopping:
                    break
                await self.process_once()
        finally:
            self.reminder_scheduler.stop()
            busy_notice_task = self._busy_notice_task
            if busy_notice_task is not None and not busy_notice_task.done():
                busy_notice_task.cancel()
                try:
                    await busy_notice_task
                except asyncio.CancelledError:
                    pass
            self._busy_notice_task = None
            self._busy_notice_dirty = False
            if not reminder_task.done():
                reminder_task.cancel()
            try:
                await reminder_task
            except asyncio.CancelledError:
                pass

    async def recover_orphaned_turns(self) -> int:
        """Requeue active DB Turns left without a resumable crash join."""
        recovered = 0
        for run in await self.store.get_active_turn_runs():
            if run.message_ids:
                await self.store.requeue_messages(
                    run.message_ids,
                    turn_id=run.turn_id,
                )
            else:
                await self.store.finalize_empty_turn(
                    turn_id=run.turn_id,
                    state="requeued",
                )
            await self.store.release_notice_delivery(run.provider_session_id)
            log_runtime_event(
                logger,
                "turn.requeued",
                agent_id=self.agent_id,
                turn_id=run.turn_id,
                provider_session_id=run.provider_session_id,
                state="requeued",
                mode="startup_orphan_recovery",
                message_count=len(run.message_ids),
                outcome="requeued",
            )
            await self._settle_recovered_status(
                turn_id=run.turn_id,
                message_ids=tuple(run.message_ids),
                succeeded=False,
                error_text="orphaned durable turn requeued at startup",
            )
            recovered += 1
        return recovered

    def stop(self) -> None:
        self._stopping = True
        self.reminder_scheduler.stop()
        if self._busy_notice_task is not None:
            self._busy_notice_task.cancel()
        self.coalescer.notify()

    def _target_summary(
        self,
        targets: tuple[tuple[str, ...], ...],
        count: int,
        *,
        more_pending: bool,
        pending_targets: tuple[tuple[str, ...], ...],
    ) -> str:
        """Serialize the internal ws-local route summary, never model prose."""
        return json.dumps(
            {
                "version": 2,
                "message_count": count,
                "more_pending": more_pending,
                "pending_targets": pending_targets,
                "targets": targets,
            },
            separators=(",", ":"),
        )

    def _from_batch(
        self,
        batch: PlannedBatch,
        *,
        turn_id: str | None = None,
        planning_cycle_key: str | None = None,
    ) -> PlannedTurn | None:
        if not batch.items:
            return None
        routes = tuple(route_for(item) for item in batch.items)
        targets: list[tuple[str, ...]] = []
        for route in routes:
            if route.target not in targets:
                targets.append(route.target)
        target_tuple = tuple(targets)
        summary = self._target_summary(
            target_tuple,
            len(batch.items),
            more_pending=batch.more_available,
            pending_targets=batch.pending_target_projections,
        )
        prefix = f"<global_inbox_turn>\n{summary}\n"
        suffix = "\n</global_inbox_turn>"
        provider_input = prefix + "\n".join(batch.formatted_messages) + suffix
        wrapper_bytes = len((prefix + suffix).encode("utf-8"))
        wrapper_tokens = self.estimator(prefix + suffix)
        return PlannedTurn(
            turn_id=turn_id or f"turn_{uuid.uuid4().hex}",
            planning_cycle_key=planning_cycle_key or f"plan_{uuid.uuid4().hex}",
            message_ids=batch.message_ids,
            items=batch.items,
            routes=routes,
            targets=target_tuple,
            pending_targets=batch.pending_target_projections,
            target_summary=summary,
            formatted_blocks=batch.formatted_messages,
            provider_input=provider_input,
            formatted_tokens=batch.estimated_tokens,
            wrapper_overhead_tokens=wrapper_tokens,
            formatted_bytes=batch.formatted_bytes,
            wrapper_overhead_bytes=wrapper_bytes,
            more_available=batch.more_available,
            requires_encryption=any(item.is_encrypted for item in batch.items),
        )

    async def _plan_notice(
        self,
        notice_items: tuple[StoredMessage, ...],
        notice: Any,
        *,
        total_pending_count: int,
        turn_id: str | None,
        planning_cycle_key: str | None,
    ) -> PlannedTurn:
        routes = tuple(route_for(item) for item in notice_items)
        targets: list[tuple[str, ...]] = []
        normalized_counts: dict[str, int] = {}
        target_channels: dict[str, tuple[str, str]] = {}
        for route in routes:
            if route.target not in targets:
                targets.append(route.target)
        for item, route in zip(notice_items, routes, strict=True):
            projection = self.store.target_projection(item)
            normalized_counts[projection] = normalized_counts.get(projection, 0) + 1
            if route.kind in {"channel", "thread"}:
                target_channels.setdefault(
                    projection,
                    (route.space_id, route.channel_id),
                )
        audiences = await load_channel_audiences(
            self.channel_audience_loader,
            target_channels.values(),
            log=logger,
        )
        latest_seq = max(
            (
                item.server_seq
                for item in notice_items
                if item.server_seq is not None
            ),
            default=None,
        )
        notice_summary = format_inbox_notice(
            generation=notice.generation,
            changed_message_count=len(notice_items),
            total_pending_message_count=total_pending_count,
            targets=project_notice_targets(
                normalized_counts,
                target_channels,
                audiences,
            ),
            latest_seq=latest_seq,
            read_tool="read_inbox",
        )
        provider_input = (
            "<global_inbox_notice>\n"
            + notice_summary
            + "\n</global_inbox_notice>\n"
            + INBOX_TURN_CUE
        )
        log_runtime_event(
            logger,
            "notice.due",
            agent_id=self.agent_id,
            notice_generation=notice.generation,
            requires_encryption=any(item.is_encrypted for item in notice_items),
            changed_message_count=len(notice_items),
            total_pending_messages=total_pending_count,
            target_count=len(targets),
            latest_seq=latest_seq,
            outcome="planned",
        )
        return PlannedTurn(
            turn_id=turn_id or f"turn_{uuid.uuid4().hex}",
            planning_cycle_key=planning_cycle_key or f"notice_{uuid.uuid4().hex}",
            message_ids=(),
            items=(),
            routes=(),
            targets=tuple(targets),
            pending_targets=tuple(targets),
            target_summary=self._target_summary(
                tuple(targets),
                len(notice_items),
                more_pending=True,
                pending_targets=tuple(targets),
            ),
            formatted_blocks=(),
            provider_input=provider_input,
            formatted_tokens=0,
            wrapper_overhead_tokens=self.estimator(provider_input),
            formatted_bytes=0,
            wrapper_overhead_bytes=len(provider_input.encode("utf-8")),
            more_available=True,
            notice_generation=notice.generation,
            notice_message_ids=tuple(item.envelope_id for item in notice_items),
            requires_encryption=any(item.is_encrypted for item in notice_items),
        )

    def _log_planned_batch(self, planned: PlannedTurn) -> None:
        mode = (
            "continuation"
            if planned.planning_cycle_key.startswith("continuation_")
            else "recovery"
            if planned.planning_cycle_key.startswith("recovery_")
            else "initial"
        )
        log_runtime_event(
            logger,
            "batch.planned",
            level=logging.DEBUG,
            agent_id=self.agent_id,
            turn_id=planned.turn_id,
            batch_id=planned.planning_cycle_key,
            correlation_key=planned.planning_cycle_key,
            envelope_id=(
                planned.message_ids[0] if len(planned.message_ids) == 1 else None
            ),
            mode=mode,
            state="planned",
            message_count=len(planned.message_ids),
            target_count=len(planned.targets),
            envelope_count=len(planned.message_ids),
            envelope_ids=list(planned.message_ids[:16]),
            routes=self._batch_route_projection(planned)[:16],
            first_seq=next(
                (
                    item.server_seq
                    for item in planned.items
                    if item.server_seq is not None
                ),
                None,
            ),
            last_seq=next(
                (
                    item.server_seq
                    for item in reversed(planned.items)
                    if item.server_seq is not None
                ),
                None,
            ),
            formatted_bytes=(planned.formatted_bytes + planned.wrapper_overhead_bytes),
        )

    async def _plan_bounded_batch(
        self,
        pending_universe: tuple[StoredMessage, ...],
        *,
        max_items: int | None,
        turn_id: str | None,
        planning_cycle_key: str | None,
    ) -> PlannedTurn | None:
        pending = pending_universe
        if max_items is not None:
            pending = pending[:max_items]
        while pending:
            batch = self.planner.plan(
                pending, formatter=self.formatter, estimator=self.estimator
            )
            if batch.unfit_head_id:
                changed = await self.planner.resolve_unfit_head(
                    batch,
                    policy=self.unfit_policy,
                    quarantine=self.store.quarantine_pending,
                )
                if changed:
                    log_runtime_event(
                        logger,
                        "inbox.row_quarantined",
                        agent_id=self.agent_id,
                        message_id=batch.unfit_head_id,
                        error_category="context_oversize",
                        outcome="terminal",
                    )
                    pending_universe = await self.store.get_pending()
                    pending = pending_universe
                    if max_items is not None:
                        pending = pending[:max_items]
                    continue
                return None
            selected_ids = set(batch.message_ids)
            remaining_targets: list[tuple[str, ...]] = []
            seen_remaining_ids: set[str] = set()
            seen_remaining_targets: set[tuple[str, ...]] = set()
            for item in pending_universe:
                if (
                    item.envelope_id in selected_ids
                    or item.envelope_id in seen_remaining_ids
                ):
                    continue
                seen_remaining_ids.add(item.envelope_id)
                target = self.planner.target_projection(item)
                if target not in seen_remaining_targets:
                    seen_remaining_targets.add(target)
                    remaining_targets.append(target)
            batch = replace(
                batch,
                pending_target_projections=tuple(remaining_targets),
                more_available=bool(seen_remaining_ids),
            )
            planned = self._from_batch(
                batch,
                turn_id=turn_id,
                planning_cycle_key=planning_cycle_key,
            )
            if planned is None:
                return None
            if (
                planned.formatted_bytes + planned.wrapper_overhead_bytes
                <= MAX_FORMATTED_BYTES
                and planned.formatted_tokens + planned.wrapper_overhead_tokens
                <= MAX_ESTIMATED_TOKENS
            ):
                self._log_planned_batch(planned)
                return planned
            # Wrapper/container is charged: remove a real FIFO suffix and replan.
            pending = pending[: len(batch.items) - 1]
        return None

    async def plan_pending(
        self,
        *,
        items: tuple[StoredMessage, ...] | None = None,
        max_items: int | None = None,
        turn_id: str | None = None,
        planning_cycle_key: str | None = None,
        provider_session_id: str | None = None,
    ) -> PlannedTurn | None:
        if items is not None:
            return await self._plan_bounded_batch(
                items,
                max_items=max_items,
                turn_id=turn_id,
                planning_cycle_key=planning_cycle_key,
            )
        session = (
            provider_session_id
            if provider_session_id is not None
            else self.adapter.get_provider_session_id()
        )
        notice, notice_items = await self.store.get_notice_snapshot(session)
        if not notice_items:
            return None
        return await self._plan_notice(
            notice_items,
            notice,
            total_pending_count=notice.pending_count,
            turn_id=turn_id,
            planning_cycle_key=planning_cycle_key,
        )

    async def _replacement_candidate(
        self, previous: AdmissionCandidate
    ) -> AdmissionCandidate:
        old = previous.payload
        replacement = await self.plan_pending(
            turn_id=getattr(old, "turn_id", None),
            planning_cycle_key=previous.planning_cycle_key,
        )
        return replacement.candidate if replacement else previous

    async def _start_local_turn(self, planned: PlannedTurn) -> None:
        """Persist the daemon-owned turn before any provider input is written."""
        provider_session_id = self.adapter.get_provider_session_id()
        if planned.message_ids:
            run = await self.store.admit_messages(
                planned.message_ids,
                turn_id=planned.turn_id,
                provider_session_id=provider_session_id,
            )
        else:
            run = await self.store.start_turn(
                turn_id=planned.turn_id,
                provider_session_id=provider_session_id,
                notice_generation=planned.notice_generation,
                notice_message_ids=planned.notice_message_ids,
            )
        self.active.turn_id = run.turn_id
        self.active.message_ids[:] = list(run.message_ids)
        self.active.notice_message_ids[:] = list(planned.notice_message_ids)
        await self._add_visible_message_ids(list(run.message_ids))
        self.active.provider_session_id = provider_session_id
        self.active.provider_turn_id = None
        self.active.routes[:] = list(planned.routes)
        if self.coordinator is not None:
            self.coordinator.provider_session_id = provider_session_id
        self._write_current_turn(planned)
        await self._notify_status_active()
        log_runtime_event(
            logger,
            "turn.admitted",
            agent_id=self.agent_id,
            turn_id=run.turn_id,
            batch_id=planned.planning_cycle_key,
            provider_session_id=provider_session_id,
            notice_generation=planned.notice_generation,
            state=run.state,
            message_count=len(run.message_ids),
            outcome="local_active",
        )
        if planned.notice_generation:
            log_runtime_event(
                logger,
                "notice.admitted",
                agent_id=self.agent_id,
                turn_id=run.turn_id,
                provider_session_id=provider_session_id,
                notice_generation=planned.notice_generation,
                outcome="claimed",
            )

    async def _admit(self, planned: PlannedTurn, event: ProviderAdmissionEvent) -> None:
        """Bind provider identity to the already-active daemon turn."""
        if event.planning_cycle_key != planned.planning_cycle_key:
            raise RuntimeError("provider admission did not correlate to planned turn")
        async with self._turn_state_lock:
            if self.active.turn_id != planned.turn_id:
                raise RuntimeError("provider admission crossed the active turn")
            previous_session_id = self.active.provider_session_id
            provider_session_id = event.provider_session_id or previous_session_id
            if provider_session_id != previous_session_id:
                await self.store.transfer_turn_session(
                    planned.turn_id,
                    from_provider_session_id=previous_session_id,
                    to_provider_session_id=provider_session_id,
                )
            if planned.notice_message_ids and provider_session_id:
                await self.store.mark_notice_delivered(
                    planned.notice_generation,
                    provider_session_id,
                    planned.notice_message_ids,
                    turn_id=planned.turn_id,
                )
            self.active.provider_session_id = provider_session_id
            self.active.provider_turn_id = event.provider_turn_id
            if self.coordinator is not None:
                self.coordinator.provider_session_id = provider_session_id
            self._write_current_turn(planned)
        await self._notify_status_notice_admitted(
            tuple(planned.notice_message_ids)
        )
        log_runtime_event(
            logger,
            "turn.admitted",
            level=logging.DEBUG,
            agent_id=self.agent_id,
            turn_id=planned.turn_id,
            batch_id=event.planning_cycle_key,
            provider_session_id=provider_session_id,
            provider_turn_id=event.provider_turn_id,
            correlation_key=event.planning_cycle_key,
            envelope_id=(
                self.active.message_ids[0]
                if len(self.active.message_ids) == 1
                else None
            ),
            state=ProcessingState.IN_TURN.value,
            message_count=len(self.active.message_ids),
            outcome="bound",
        )

    async def _notify_status_notice_admitted(
        self, message_ids: tuple[str, ...]
    ) -> None:
        """Expose provider admission without claiming Inbox rows were read."""
        if (
            self.status_lifecycle is None
            or not self.active.turn_id
            or not message_ids
        ):
            return
        try:
            await self.status_lifecycle.on_notice_admitted(
                turn_id=self.active.turn_id,
                message_ids=message_ids,
            )
        except Exception:
            logger.warning(
                "agent %s: status lifecycle notice notify failed",
                self.agent_id,
                exc_info=True,
            )

    async def _notify_status_active(self) -> None:
        """Expose the exact active union to the worker's status lifecycle.

        Telemetry-only: a reporter failure must never break admission or
        processing-state decisions, so the callback is shielded here.
        """
        if (
            self.status_lifecycle is None
            or not self.active.turn_id
            or not self.active.message_ids
        ):
            return
        try:
            await self.status_lifecycle.on_turn_active(
                turn_id=self.active.turn_id,
                message_ids=tuple(self.active.message_ids),
            )
        except Exception:
            logger.warning(
                "agent %s: status lifecycle admission notify failed",
                self.agent_id,
                exc_info=True,
            )

    async def _notify_status_terminal(
        self,
        *,
        terminal: bool,
        succeeded: bool,
        error_text: str | None,
    ) -> None:
        """Settle the exact active union before ``_finalize_process`` clears it.

        Only a terminal turn settles status; the batch is built from the same
        immutable snapshot the durable requeue/process just used.
        """
        if self.status_lifecycle is None or not terminal or not self.active.turn_id:
            return
        try:
            await self.status_lifecycle.on_turn_terminal(
                turn_id=self.active.turn_id,
                message_ids=tuple(self.active.message_ids),
                succeeded=succeeded,
                error_text=error_text,
            )
        except Exception:
            logger.warning(
                "agent %s: status lifecycle terminal notify failed",
                self.agent_id,
                exc_info=True,
            )

    async def _settle_recovered_status(
        self,
        *,
        turn_id: str,
        message_ids: tuple[str, ...],
        succeeded: bool,
        error_text: str | None,
    ) -> None:
        """Reconstruct and settle a processing run after process restart."""
        if self.status_lifecycle is None or not turn_id or not message_ids:
            return
        try:
            await self.status_lifecycle.on_turn_active(
                turn_id=turn_id,
                message_ids=message_ids,
            )
        except Exception:
            logger.warning(
                "agent %s: recovered status lifecycle admission failed",
                self.agent_id,
                exc_info=True,
            )
        try:
            await self.status_lifecycle.on_turn_terminal(
                turn_id=turn_id,
                message_ids=message_ids,
                succeeded=succeeded,
                error_text=error_text,
            )
        except Exception:
            logger.warning(
                "agent %s: recovered status lifecycle terminal notify failed",
                self.agent_id,
                exc_info=True,
            )

    async def _admit_held_recovery(
        self,
        event: ProviderAdmissionEvent,
        *,
        fired: list[bool],
        correlation_key: str,
        active_turn_id: str,
        provider_session_id: str,
        evidence: Any,
        evidence_key: tuple[str, str, str, str, int, str],
        displayed_ids: tuple[str, ...],
        space_id: str,
        channel_id: str,
        latest_seq: int,
    ) -> None:
        """Admit a held-send recovery, then expose the grown active union."""
        await super()._admit_held_recovery(
            event,
            fired=fired,
            correlation_key=correlation_key,
            active_turn_id=active_turn_id,
            provider_session_id=provider_session_id,
            evidence=evidence,
            evidence_key=evidence_key,
            displayed_ids=displayed_ids,
            space_id=space_id,
            channel_id=channel_id,
            latest_seq=latest_seq,
        )
        await self._notify_status_active()

    def _write_current_turn(self, planned: PlannedTurn) -> None:
        path = self.current_turn_path
        path.parent.mkdir(parents=True, exist_ok=True)
        use_active = self.active.turn_id == planned.turn_id and (
            bool(self.active.message_ids) or not planned.message_ids
        )
        message_ids = (
            tuple(self.active.message_ids) if use_active else planned.message_ids
        )
        routes = tuple(self.active.routes) if use_active else planned.routes
        targets: list[tuple[str, ...]] = []
        for route in routes:
            if route.target not in targets:
                targets.append(route.target)
        if not targets:
            targets.extend(planned.targets)
        body: dict[str, Any] = {
            "version": CURRENT_TURN_VERSION,
            "turn_id": planned.turn_id,
            "message_ids": list(message_ids),
            "targets": [list(target) for target in targets],
            "routes": [asdict(route) for route in routes],
            "provider_session_id": self.active.provider_session_id,
            "provider_turn_id": self.active.provider_turn_id,
        }
        if self.runtime_event_outbox is not None:
            outbox_state = self.runtime_event_outbox.state()
            body.update(
                {
                    "logical_session_ref": outbox_state.get("session_ref", ""),
                    "logical_turn_ref": outbox_state.get("active_turn_ref", ""),
                    "native_session_id": outbox_state.get("native_session_id", ""),
                }
            )
        if len(targets) == 1 and routes and message_ids:
            route = routes[0]
            body["channel_id"] = route.channel_id
            body["root_id"] = route.thread_root_id
            body["triggering_post_id"] = message_ids[0]
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _degrade(self, diagnostic: str) -> None:
        self.health = RuntimeHealth("degraded", diagnostic)
        self._degraded = True
        self._degraded_attempts += 1
        backoff = min(
            DEGRADED_RECOVERY_BASE_SECONDS * 2 ** (self._degraded_attempts - 1),
            DEGRADED_RECOVERY_MAX_SECONDS,
        )
        self._degraded_until = time.monotonic() + backoff
        # Arm the autonomous recovery wake through the existing coalescer only:
        # no extra task, timer, or thread, so shutdown behaviour is unchanged.
        self.coalescer.notify(delay_seconds=backoff)

    def _try_degraded_recovery(self) -> bool:
        """Return whether a degraded runtime may retry its durable work now."""
        if not self._degraded:
            return True
        remaining = (
            0.0
            if self._degraded_until is None
            else self._degraded_until - time.monotonic()
        )
        if remaining > 0:
            # An earlier coalescer deadline may have fired ahead of the degrade
            # wake and consumed it; re-arm so the window still ends in a retry.
            self.coalescer.notify(delay_seconds=remaining)
            return False
        self._degraded = False
        self._degraded_until = None
        return True

    async def _resolve_context_plan(self, planned: PlannedTurn) -> PlannedTurn | None:
        rollover_seen = False
        for _ in range(self.max_context_decisions):
            decision = await self.context_controller.decide(
                planned.candidate, self._replacement_candidate
            )
            log_runtime_event(
                logger,
                "context.checked",
                agent_id=self.agent_id,
                turn_id=planned.turn_id,
                notice_generation=planned.notice_generation,
                projected_tokens=decision.projected_tokens,
                used_tokens_before=decision.snapshot.used_tokens,
                context_window=decision.snapshot.context_window,
                outcome=decision.outcome.value,
            )
            if decision.outcome is DecisionOutcome.REPLAN:
                log_runtime_event(
                    logger,
                    "context.compacted",
                    agent_id=self.agent_id,
                    turn_id=planned.turn_id,
                    notice_generation=planned.notice_generation,
                    outcome="completed",
                )
                replacement = decision.candidate.payload
                if not isinstance(replacement, PlannedTurn):
                    self._degrade("context replan did not return a planned turn")
                    return None
                planned = replacement
                continue
            if decision.outcome is DecisionOutcome.SHRINK:
                if len(planned.items) <= 1:
                    self._degrade(decision.diagnostic)
                    return None
                planned = await self.plan_pending(
                    items=planned.items[:-1],
                    turn_id=planned.turn_id,
                    planning_cycle_key=planned.planning_cycle_key,
                )
                if planned is None:
                    return None
                continue
            if decision.outcome is DecisionOutcome.ROLLOVER:
                log_runtime_event(
                    logger,
                    "context.rollover",
                    agent_id=self.agent_id,
                    turn_id=planned.turn_id,
                    notice_generation=planned.notice_generation,
                    outcome="completed",
                )
                if rollover_seen:
                    self._degrade("provider rollover re-evaluation was exhausted")
                    return None
                rollover_seen = True
                rolled_session = self.adapter.get_provider_session_id()
                if self.coordinator is not None:
                    self.coordinator.provider_session_id = rolled_session
                self.active.provider_session_id = rolled_session
                continue
            if decision.outcome is DecisionOutcome.DEGRADED:
                self._degrade(decision.diagnostic)
                return None
            return planned
        self._degrade("context decision budget exhausted")
        return None

    async def _notice_is_current(self, planned: PlannedTurn) -> bool:
        if not planned.notice_generation:
            return True
        candidates = await self.store.get_notice_candidates(
            self.adapter.get_provider_session_id()
        )
        candidate_ids = {item.envelope_id for item in candidates}
        return bool(candidate_ids.intersection(planned.notice_message_ids))

    async def _invoke_turn_with_retries(self, planned: PlannedTurn) -> None:
        retries = 0
        while True:
            try:
                if retries == 0:
                    await self.run_turn(planned)
                else:
                    await self._run_retry(await self._prepare_retry_attempt(planned))
                return
            except Exception as exc:
                from .core import AgentAPIError

                can_retry = (
                    isinstance(exc, AgentAPIError)
                    and not exc.is_auth
                    and self.active.turn_id == planned.turn_id
                    and retries < self.max_api_retries
                    and hasattr(self.run_turn, "handle_global_inbox_retry")
                )
                if not can_retry:
                    raise
                retries += 1
                await self.retry_sleep(min(2 ** (retries - 1), 4))
                self.active.provider_turn_id = None

    async def _prepare_retry_attempt(self, planned: PlannedTurn) -> PlannedTurn:
        """Build the retry payload from the turn's exact durable rows.

        A retry reaches either the *same* provider session — which still holds
        the original input in its transcript and only needs the cheap kick — or
        a *replacement* session, which receives ``provider_input`` as the
        adapter's fallback. A notice turn's original input carries no bodies at
        all, so a replacement session would otherwise be asked to finish rows it
        never saw. Rebuilding per attempt (the same reconstruction the crash
        file uses) keeps that fallback equal to what was durably admitted, and
        never touches the resumed transcript path.
        """
        attempt = planned
        if self.active.message_ids:
            rows = await self.store.get_in_turn_messages(
                planned.turn_id, self.active.provider_session_id
            )
            if tuple(row.envelope_id for row in rows) != tuple(
                self.active.message_ids
            ):
                # Not retryable: the durable union no longer matches the active
                # turn, so no faithful payload exists. Fail into the requeue.
                raise RuntimeError("durable turn membership changed before retry")
            attempt = self._reconstruct_exact_turn(
                turn_id=planned.turn_id, rows=rows
            )
        self.adapter.register_admission_callback(
            lambda event: self._admit_retry_attempt(attempt, event),
            attempt.planning_cycle_key,
        )
        return attempt

    async def _mark_active_processed(
        self,
        planned: PlannedTurn,
        process_started: float,
    ) -> None:
        # A processed turn ends the incident: consecutive-degrade backoff must
        # not carry across unrelated later incidents.
        self._degraded_attempts = 0
        if self.active.message_ids:
            await self.store.mark_processed(
                tuple(self.active.message_ids),
                turn_id=planned.turn_id,
                provider_session_id=self.active.provider_session_id,
            )
            await self.store.release_notice_delivery(
                self.active.provider_session_id,
                tuple(self.active.notice_message_ids),
            )
        else:
            # The provider received the notice but chose not to read Inbox.
            # That is a normal deferred outcome, not a transport failure and
            # therefore must not create another generation for the same set.
            await self.store.finalize_empty_turn(turn_id=planned.turn_id)
        for item_id in self.active.message_ids:
            row = await self.store.get_message_by_envelope(item_id)
            log_runtime_event(
                logger,
                "inbox.row_processed",
                agent_id=self.agent_id,
                turn_id=planned.turn_id,
                provider_session_id=self.active.provider_session_id,
                message_id=item_id,
                server_seq=row.server_seq if row is not None else None,
                outcome="processed",
            )
        log_runtime_event(
            logger,
            "turn.processed",
            agent_id=self.agent_id,
            turn_id=planned.turn_id,
            provider_session_id=self.active.provider_session_id,
            provider_turn_id=self.active.provider_turn_id,
            envelope_id=(
                self.active.message_ids[0]
                if len(self.active.message_ids) == 1
                else None
            ),
            state=ProcessingState.PROCESSED.value,
            message_count=len(self.active.message_ids),
            duration_ms=int((time.monotonic() - process_started) * 1000),
        )

    async def _requeue_active_turn(
        self,
        planned: PlannedTurn,
        process_started: float,
        error_category: str,
    ) -> bool:
        if self.active.turn_id != planned.turn_id:
            return False
        if self.active.message_ids:
            await self.store.requeue_messages(
                tuple(self.active.message_ids), turn_id=planned.turn_id
            )
        else:
            await self.store.finalize_empty_turn(
                turn_id=planned.turn_id,
                state="requeued",
            )
        await self.store.release_notice_delivery(
            self.active.provider_session_id,
            tuple(self.active.notice_message_ids),
        )
        for item_id in self.active.message_ids:
            row = await self.store.get_message_by_envelope(item_id)
            log_runtime_event(
                logger,
                "inbox.row_requeued",
                agent_id=self.agent_id,
                turn_id=planned.turn_id,
                provider_session_id=self.active.provider_session_id,
                message_id=item_id,
                server_seq=row.server_seq if row is not None else None,
                outcome="requeued",
            )
        log_runtime_event(
            logger,
            "turn.requeued",
            agent_id=self.agent_id,
            turn_id=planned.turn_id,
            provider_session_id=self.active.provider_session_id,
            provider_turn_id=self.active.provider_turn_id,
            state="requeued",
            message_count=len(self.active.message_ids),
            duration_ms=int((time.monotonic() - process_started) * 1000),
            error_category=error_category,
        )
        return True

    def _finalize_process(self, planned: PlannedTurn, terminal: bool) -> None:
        from . import send_mode

        send_mode.clear_turn_bundle(list(self.send_mode_keys))
        self.adapter.register_admission_callback(None, "")
        was_active = self.active.turn_id == planned.turn_id
        if terminal:
            log_runtime_event(
                logger,
                "turn.finalized",
                agent_id=self.agent_id,
                turn_id=planned.turn_id,
                provider_session_id=self.active.provider_session_id,
                provider_turn_id=self.active.provider_turn_id,
                notice_generation=planned.notice_generation,
                message_count=len(self.active.message_ids),
                outcome=(
                    "processed" if self.health.state == "in_progress" else "requeued"
                ),
            )
        if terminal or not was_active:
            self._discard_held_admission_evidence(planned.turn_id)
            self.held.clear()
            self.active.clear()
        if self.coordinator is not None:
            self._discard_coordinator_held_evidence()
            self.coordinator.provider_session_id = None
        if terminal or not was_active:
            try:
                self.current_turn_path.unlink()
            except FileNotFoundError:
                pass
        if self.health.state == "in_progress":
            self.health = RuntimeHealth()

    async def _wake_remaining_pending(self) -> None:
        if self._degraded or not await self.store.get_pending(limit=1):
            return
        if await self.store.get_notice_candidates(
            self.adapter.get_provider_session_id()
        ):
            self.notify()

    async def process_once(self) -> bool:
        if not self._try_degraded_recovery():
            return False
        async with self._boundary:
            process_started = time.monotonic()
            planned = await self.plan_pending()
            if planned is None:
                self.health = RuntimeHealth()
                return False
            planned = await self._resolve_context_plan(planned)
            if planned is None:
                return False
            if not await self._notice_is_current(planned):
                self.health = RuntimeHealth()
                return False
            self.attempts.reset()
            async with self._turn_state_lock:
                await self._start_local_turn(planned)
            self.adapter.register_admission_callback(
                lambda event: self._admit(planned, event),
                planned.planning_cycle_key,
            )
            self.health = RuntimeHealth("in_progress", "")
            from . import send_mode

            send_mode.note_turn_bundle(
                list(self.send_mode_keys),
                planned.requires_encryption
                or any(item.is_encrypted for item in planned.items),
            )
            terminal = False
            terminal_succeeded = False
            terminal_error: str | None = None
            try:
                await self._invoke_turn_with_retries(planned)
                if self.active.turn_id == planned.turn_id:
                    async with self._turn_state_lock:
                        await self._mark_active_processed(planned, process_started)
                    terminal = True
                    terminal_succeeded = True
                else:
                    self._degrade("provider returned without correlated admission")
            except asyncio.CancelledError:
                async with self._turn_state_lock:
                    await self._requeue_active_turn(
                        planned, process_started, "cancelled"
                    )
                terminal = True
                terminal_error = "global inbox turn cancelled before completion"
                raise
            except Exception as exc:
                async with self._turn_state_lock:
                    terminal = await self._requeue_active_turn(
                        planned, process_started, "provider_error"
                    )
                terminal_error = f"{type(exc).__name__}: {exc}"
                diagnostic = (
                    "turn failed and was requeued"
                    if terminal
                    else "turn failed outside the active durable turn"
                )
                self._degrade(diagnostic)
                log_runtime_event(
                    logger,
                    "turn.failed",
                    level=logging.ERROR,
                    agent_id=self.agent_id,
                    turn_id=planned.turn_id,
                    provider_session_id=self.active.provider_session_id,
                    provider_turn_id=self.active.provider_turn_id,
                    notice_generation=planned.notice_generation,
                    target_count=len(planned.targets),
                    error_category="provider_error",
                    error_type=type(exc).__name__,
                    error_code=getattr(exc, "error_code", None),
                    outcome="requeued" if terminal else "degraded",
                )
            finally:
                await self._notify_status_terminal(
                    terminal=terminal,
                    succeeded=terminal_succeeded,
                    error_text=terminal_error,
                )
                self._finalize_process(planned, terminal)
            await self._wake_remaining_pending()
            return True

    async def _run_retry(self, planned: PlannedTurn) -> Any:
        retry = getattr(self.run_turn, "handle_global_inbox_retry", None)
        if retry is None:
            raise RuntimeError("global runtime retry is unavailable")
        return await retry(planned)

    async def _admit_retry_attempt(
        self, planned: PlannedTurn, event: ProviderAdmissionEvent
    ) -> None:
        """Bind a retry provider turn without re-admitting its durable union."""
        if (
            event.planning_cycle_key != planned.planning_cycle_key
            or self.active.turn_id != planned.turn_id
        ):
            raise RuntimeError("provider retry admission crossed the active turn")
        async with self._turn_state_lock:
            previous_session_id = self.active.provider_session_id
            provider_session_id = event.provider_session_id or previous_session_id
            transferred = provider_session_id != previous_session_id
            if transferred:
                # A replacement session received the rebuilt exact bodies as
                # its fallback payload; durable ownership follows atomically.
                await self.store.transfer_turn_session(
                    planned.turn_id,
                    from_provider_session_id=previous_session_id,
                    to_provider_session_id=provider_session_id,
                )
            if planned.notice_message_ids and provider_session_id:
                await self.store.mark_notice_delivered(
                    planned.notice_generation,
                    provider_session_id,
                    planned.notice_message_ids,
                    turn_id=planned.turn_id,
                )
            self.active.provider_session_id = provider_session_id
            self.active.provider_turn_id = event.provider_turn_id
            if self.coordinator is not None:
                self.coordinator.provider_session_id = provider_session_id
            self._write_current_turn(planned)

    def _clear_terminal_turn(self) -> None:
        self.adapter.register_admission_callback(None, "")
        self._discard_held_admission_evidence(self.active.turn_id)
        self.held.clear()
        self.active.clear()
        if self.coordinator is not None:
            self._discard_coordinator_held_evidence()
            self.coordinator.provider_session_id = None
        try:
            self.current_turn_path.unlink()
        except OSError:
            pass

    def _discard_coordinator_held_evidence(self) -> None:
        """Drop the finished turn's decrypted held context.

        ``_prune_stale_held_locked`` only evicts a record when the coordinator
        reports a *complete* other identity, and teardown clears the identity
        to ``("", "")`` — so without this call an abandoned turn's plaintext
        draft and recovered rows survive until the size cap evicts them.
        """
        discard = getattr(self.coordinator, "discard_held_evidence", None)
        if discard is not None:
            discard()

    def _discard_held_admission_evidence(self, turn_id: str) -> None:
        """Release transient held projections when their owning turn ends."""
        if not turn_id:
            return
        for key in tuple(self._held_admission_evidence):
            if key[0] == turn_id:
                self._held_admission_evidence.pop(key, None)

    def _reconstruct_exact_turn(
        self,
        *,
        turn_id: str,
        rows: tuple[StoredMessage, ...],
    ) -> PlannedTurn:
        blocks = tuple(self.formatter(row) for row in rows)
        routes = tuple(route_for(row) for row in rows)
        targets: list[tuple[str, ...]] = []
        for route in routes:
            if route.target not in targets:
                targets.append(route.target)
        target_tuple = tuple(targets)
        summary = self._target_summary(
            target_tuple,
            len(rows),
            more_pending=False,
            pending_targets=(),
        )
        prefix = f"<global_inbox_turn>\n{summary}\n"
        suffix = "\n</global_inbox_turn>"
        return PlannedTurn(
            turn_id=turn_id,
            planning_cycle_key=f"recovery_{turn_id}",
            message_ids=tuple(row.envelope_id for row in rows),
            items=rows,
            routes=routes,
            targets=target_tuple,
            pending_targets=(),
            target_summary=summary,
            formatted_blocks=blocks,
            provider_input=prefix + "\n".join(blocks) + suffix,
            formatted_tokens=sum(self.estimator(block) for block in blocks),
            wrapper_overhead_tokens=self.estimator(prefix + suffix),
            formatted_bytes=sum(len(block.encode("utf-8")) for block in blocks),
            wrapper_overhead_bytes=len((prefix + suffix).encode("utf-8")),
            # The triggering durable rows carry the same E2EE fact a normal
            # turn derives from its planned batch; a crash resume must not
            # silently downgrade to the plaintext default.
            requires_encryption=any(row.is_encrypted for row in rows),
        )

    async def _abandon_recovery_event(self, raw: Any, run: Any) -> None:
        if self.runtime_event_outbox is None:
            return
        from .runtime_events import RuntimeEvent

        outbox = self.runtime_event_outbox.state()
        turn_ref = outbox.get("active_turn_ref", "")
        session_ref = outbox.get("session_ref", "")
        native_session_id = outbox.get("native_session_id", "")
        join_matches = bool(
            turn_ref
            and session_ref
            and native_session_id == run.provider_session_id
            and (
                not isinstance(raw, dict)
                or (
                    raw.get("provider_session_id") in {None, run.provider_session_id}
                    and raw.get("native_session_id", native_session_id)
                    == native_session_id
                    and raw.get("logical_session_ref", session_ref) == session_ref
                    and raw.get("logical_turn_ref", turn_ref) == turn_ref
                )
            )
        )
        if not join_matches:
            return
        occurred_at = (
            datetime.fromtimestamp(run.started_at / 1000, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        await self.runtime_event_outbox.enqueue(
            RuntimeEvent(
                agent_id=self.agent_id,
                session_ref=session_ref,
                turn_ref=turn_ref,
                type="turn.finished",
                payload={"outcome": "abandoned"},
                event_id=(
                    f"evt_abandoned_{self.agent_id}_{session_ref}_{turn_ref}_{run.turn_id}"
                ),
                occurred_at=occurred_at,
            ),
            terminal=True,
        )
        self.runtime_event_outbox.set_active_turn(
            None,
            session_ref=session_ref,
            native_session_id=native_session_id,
        )

    async def _requeue_recovery_run(
        self,
        raw: Any,
        run: Any,
        durable_ids: tuple[str, ...],
        recovery_started: float,
        state: str,
        activated: bool = False,
    ) -> bool:
        if run is None or run.state != ProcessingState.IN_TURN.value:
            return False
        # ``durable_ids`` is the activation snapshot. Once the turn is running,
        # mid-turn admission grows the durable membership and republishes it on
        # ``active.message_ids``; requeueing the snapshot would abandon the rows
        # admitted since, leaving them IN_TURN under a finished run. Before
        # activation the snapshot is the truth and ``active`` may still hold an
        # unrelated turn's state, so only an activated run reads it.
        requeue_ids = (
            tuple(self.active.message_ids)
            if activated and self.active.turn_id == run.turn_id
            else durable_ids
        )
        if requeue_ids:
            await self._abandon_recovery_event(raw, run)
            await self.store.requeue_messages(requeue_ids, turn_id=run.turn_id)
        else:
            await self.store.finalize_empty_turn(
                turn_id=run.turn_id,
                state="requeued",
            )
        await self.store.release_notice_delivery(run.provider_session_id)
        log_runtime_event(
            logger,
            "turn.requeued",
            agent_id=self.agent_id,
            turn_id=run.turn_id,
            provider_session_id=run.provider_session_id,
            state="requeued",
            mode="recovery",
            message_count=len(requeue_ids),
            duration_ms=int((time.monotonic() - recovery_started) * 1000),
            error_category=state,
        )
        return True

    async def _unwind_recovery(
        self,
        *,
        raw: Any,
        run: Any,
        durable_ids: tuple[str, ...],
        recovery_started: float,
        diagnostic: str,
        state: str = "degraded",
        defer_requeued_recovery: bool = False,
        activated: bool = False,
    ) -> bool:
        async with self._turn_state_lock:
            requeued = await self._requeue_recovery_run(
                raw,
                run,
                durable_ids,
                recovery_started,
                state,
                activated=activated,
            )
        self.health = RuntimeHealth(state, diagnostic)
        self._defer_requeued_recovery = defer_requeued_recovery and requeued
        if activated:
            await self._notify_status_terminal(
                terminal=True,
                succeeded=False,
                error_text=diagnostic,
            )
        elif run is not None:
            succeeded = run.state == ProcessingState.PROCESSED.value
            await self._settle_recovered_status(
                turn_id=run.turn_id,
                message_ids=tuple(run.message_ids),
                succeeded=succeeded,
                error_text=None if succeeded else diagnostic,
            )
        self._clear_terminal_turn()
        if requeued:
            self.notify()
        return False

    def _recovery_outbox_diagnostic(self, raw: dict[str, Any], run: Any) -> str:
        if self.runtime_event_outbox is None:
            return ""
        outbox = self.runtime_event_outbox.state()
        if not outbox.get("active_turn_ref"):
            return ""
        mismatch = (
            outbox.get("native_session_id") != run.provider_session_id
            or raw.get("provider_session_id", run.provider_session_id)
            != run.provider_session_id
            or raw.get("native_session_id", outbox.get("native_session_id"))
            != outbox.get("native_session_id")
            or raw.get("logical_session_ref", outbox.get("session_ref"))
            != outbox.get("session_ref")
            or raw.get("logical_turn_ref", outbox.get("active_turn_ref"))
            != outbox.get("active_turn_ref")
        )
        return (
            "crash join and Runtime Event identity mismatch"
            if mismatch
            else "Driver does not support in-flight turn recovery"
        )

    async def _recovery_plan(
        self,
        raw: dict[str, Any],
        turn_id: str,
        run: Any,
        durable_ids: tuple[str, ...],
    ) -> PlannedTurn | None:
        session = run.provider_session_id
        rows = (
            await self.store.get_in_turn_messages(turn_id, session) if session else ()
        )
        planned = (
            self._reconstruct_exact_turn(turn_id=turn_id, rows=rows) if rows else None
        )
        if session is None or self.adapter.get_provider_session_id() != session:
            return None
        if planned is None or planned.message_ids != durable_ids:
            return None
        if not isinstance(raw.get("message_ids"), list):
            return None
        expected_routes = [asdict(route) for route in planned.routes]
        expected_targets = [list(target) for target in planned.targets]
        if (
            tuple(raw["message_ids"]) != durable_ids
            or raw.get("routes") != expected_routes
            or raw.get("targets") != expected_targets
        ):
            return None
        return planned

    def _activate_recovery(
        self,
        planned: PlannedTurn,
        durable_ids: tuple[str, ...],
        session: str,
    ) -> None:
        self.active.turn_id = planned.turn_id
        self.active.message_ids[:] = list(durable_ids)
        self.active.provider_session_id = session
        self.active.routes[:] = list(planned.routes)
        if self.coordinator is not None:
            self.coordinator.provider_session_id = session
        self.attempts.reset()
        self.health = RuntimeHealth("in_progress", "resuming durable crash join")

    async def _run_recovery_retries(
        self,
        planned: PlannedTurn,
    ) -> tuple[str, str] | None:
        retries = 0
        while True:
            self.active.provider_turn_id = None
            self.adapter.register_admission_callback(
                lambda event: self._admit_retry_attempt(planned, event),
                planned.planning_cycle_key,
            )
            try:
                await self._run_retry(planned)
                return None
            except Exception as exc:
                from .core import AgentAPIError

                if isinstance(exc, AgentAPIError) and exc.is_auth:
                    return "crash resume auth failure", "auth_failed"
                if not isinstance(exc, AgentAPIError):
                    return (
                        f"crash resume unsafe failure: {type(exc).__name__}",
                        "degraded",
                    )
                if retries >= self.max_api_retries:
                    return "crash resume retry budget exhausted", "api_error_abandoned"
                retries += 1
                await self.retry_sleep(min(2 ** (retries - 1), 4))

    async def _complete_recovery(
        self,
        turn_id: str,
        recovery_started: float,
    ) -> bool:
        await self.store.mark_processed(
            tuple(self.active.message_ids),
            turn_id=turn_id,
            provider_session_id=self.active.provider_session_id,
        )
        await self.store.release_notice_delivery(self.active.provider_session_id)
        log_runtime_event(
            logger,
            "turn.processed",
            agent_id=self.agent_id,
            turn_id=turn_id,
            provider_session_id=self.active.provider_session_id,
            provider_turn_id=self.active.provider_turn_id,
            state=ProcessingState.PROCESSED.value,
            mode="recovery",
            message_count=len(self.active.message_ids),
            duration_ms=int((time.monotonic() - recovery_started) * 1000),
        )
        self.health = RuntimeHealth()
        await self._notify_status_terminal(
            terminal=True,
            succeeded=True,
            error_text=None,
        )
        self._clear_terminal_turn()
        return True

    async def _cancel_recovery(
        self,
        turn_id: str,
        recovery_started: float,
    ) -> None:
        await self.store.requeue_messages(
            tuple(self.active.message_ids), turn_id=turn_id
        )
        await self.store.release_notice_delivery(self.active.provider_session_id)
        log_runtime_event(
            logger,
            "turn.requeued",
            agent_id=self.agent_id,
            turn_id=turn_id,
            provider_session_id=self.active.provider_session_id,
            provider_turn_id=self.active.provider_turn_id,
            state="requeued",
            mode="recovery",
            message_count=len(self.active.message_ids),
            duration_ms=int((time.monotonic() - recovery_started) * 1000),
            error_category="cancelled",
        )
        self.health = RuntimeHealth("degraded", "crash resume cancelled and requeued")
        await self._notify_status_terminal(
            terminal=True,
            succeeded=False,
            error_text="crash resume cancelled and requeued",
        )
        self._clear_terminal_turn()
        self.notify()

    async def recover_current_turn(self) -> bool:
        """Finish or unwind a durable crash join before normal planning."""
        recovery_started = time.monotonic()
        try:
            raw: Any = json.loads(self.current_turn_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            self.health = RuntimeHealth("degraded", "invalid crash join")
            self._clear_terminal_turn()
            return False
        turn_id = raw.get("turn_id") if isinstance(raw, dict) else None
        run = (
            await self.store.get_turn_run(turn_id)
            if isinstance(turn_id, str) and turn_id
            else None
        )
        durable_ids = (
            tuple(run.message_ids)
            if run is not None and run.state == ProcessingState.IN_TURN.value
            else ()
        )
        unwind = {
            "raw": raw,
            "run": run,
            "durable_ids": durable_ids,
            "recovery_started": recovery_started,
        }
        if (
            not isinstance(raw, dict)
            or raw.get("version") != CURRENT_TURN_VERSION
            or run is None
            or run.state != ProcessingState.IN_TURN.value
            or not durable_ids
        ):
            return await self._unwind_recovery(
                **unwind,
                diagnostic="invalid or stale crash join",
            )
        diagnostic = self._recovery_outbox_diagnostic(raw, run)
        if diagnostic:
            return await self._unwind_recovery(**unwind, diagnostic=diagnostic)
        planned = await self._recovery_plan(raw, turn_id, run, durable_ids)
        if planned is None:
            return await self._unwind_recovery(
                **unwind,
                diagnostic="crash join identity, route, or target mismatch",
            )
        self._activate_recovery(planned, durable_ids, run.provider_session_id)
        await self._notify_status_active()
        from . import send_mode

        # A resumed turn establishes the same send-mode facts ``process_once``
        # does; the module dict is process-local and therefore empty here.
        send_mode.note_turn_bundle(
            list(self.send_mode_keys),
            planned.requires_encryption
            or any(item.is_encrypted for item in planned.items),
        )
        try:
            return await self._resume_activated_turn(
                planned=planned,
                turn_id=turn_id,
                recovery_started=recovery_started,
                unwind=unwind,
            )
        finally:
            send_mode.clear_turn_bundle(list(self.send_mode_keys))

    async def _resume_activated_turn(
        self,
        *,
        planned: PlannedTurn,
        turn_id: str,
        recovery_started: float,
        unwind: dict[str, Any],
    ) -> bool:
        """Drive an activated crash-resumed turn to a terminal outcome."""
        if not hasattr(self.run_turn, "handle_global_inbox_retry"):
            return await self._unwind_recovery(
                **unwind,
                diagnostic="crash recovery retry unavailable",
                defer_requeued_recovery=True,
                activated=True,
            )
        try:
            failure = await self._run_recovery_retries(planned)
            if failure is not None:
                return await self._unwind_recovery(
                    **unwind,
                    diagnostic=failure[0],
                    state=failure[1],
                    defer_requeued_recovery=True,
                    activated=True,
                )
            async with self._turn_state_lock:
                return await self._complete_recovery(turn_id, recovery_started)
        except asyncio.CancelledError:
            async with self._turn_state_lock:
                await self._cancel_recovery(turn_id, recovery_started)
            raise
        except Exception as exc:
            return await self._unwind_recovery(
                **unwind,
                diagnostic=f"crash resume terminal failure: {type(exc).__name__}",
                defer_requeued_recovery=True,
                activated=True,
            )
