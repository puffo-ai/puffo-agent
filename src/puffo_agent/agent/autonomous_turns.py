"""Autonomous provider-turn lifecycle for ``GlobalInboxRuntime``.

This private implementation trait keeps the runtime as the single owner of
the active Inbox union while separating one cohesive provider lifecycle from
the composition root.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from ._logging import log_runtime_event
from .global_inbox_types import PlannedTurn, RuntimeHealth

logger = logging.getLogger(__name__)


class AutonomousTurnLifecycleMixin:
    """Implementation trait for provider runs the daemon did not start."""

    def _initialize_autonomous_lifecycle(self) -> None:
        self._autonomous_turn_id = ""
        self._autonomous_planned: PlannedTurn | None = None
        self._autonomous_ready = False
        self._deferred_autonomous_start: Any | None = None
        self._autonomous_settle_pending: str | None = None
        self._autonomous_event_lock = asyncio.Lock()

    def register_autonomous_adoption(self) -> bool:
        """Register the required adapter callback for daemon-unstarted runs."""
        return bool(
            self.adapter.register_autonomous_callback(self._on_autonomous_event)
        )

    async def _enable_autonomous_adoption(self) -> None:
        self._autonomous_ready = True
        await self._replay_deferred_autonomous_start()

    async def _on_autonomous_event(self, event: Any) -> None:
        async with self._autonomous_event_lock:
            await self._handle_autonomous_event_locked(event)

    async def _handle_autonomous_event_locked(self, event: Any) -> None:
        event_type = getattr(event.type, "value", event.type)
        if event_type == "turn.autonomous_started":
            if not self._autonomous_ready or self.active.turn_id:
                self._defer_autonomous_start(
                    event,
                    reason=(
                        "startup_recovery"
                        if not self._autonomous_ready
                        else "active_daemon_turn"
                    ),
                )
                return
            if not await self._adopt_autonomous_event(event):
                self._deferred_autonomous_start = event
            return

        if self._deferred_autonomous_start is not None:
            # The provider run ended before the daemon released ownership. Its
            # held start must not be replayed into a turn with no future terminal.
            self._deferred_autonomous_start = None
            log_runtime_event(
                logger,
                "turn.autonomous_adoption",
                agent_id=self.agent_id,
                outcome="skipped",
                error_category="completed_before_adoption",
            )
            return
        await self.finish_autonomous_turn(
            outcome=str(event.data.get("outcome") or "succeeded")
        )

    def _defer_autonomous_start(self, event: Any, *, reason: str) -> None:
        self._deferred_autonomous_start = event
        log_runtime_event(
            logger,
            "turn.autonomous_adoption",
            agent_id=self.agent_id,
            outcome="deferred",
            error_category=reason,
        )

    async def _replay_deferred_autonomous_start(self) -> bool:
        async with self._autonomous_event_lock:
            event = self._deferred_autonomous_start
            if event is None or not self._autonomous_ready or self.active.turn_id:
                return False
            self._deferred_autonomous_start = None
            if await self._adopt_autonomous_event(event):
                return True
            # A daemon turn may have won the shared state lock, or persistence
            # may be temporarily unavailable. Preserve the provider's one-shot
            # announcement for the next durable wake.
            self._deferred_autonomous_start = event
            return False

    async def _adopt_autonomous_event(self, event: Any) -> bool:
        return await self.adopt_autonomous_turn(
            provider_session_id=(
                getattr(event, "native_session_id", "")
                or self.adapter.get_provider_session_id()
            ),
            provider_turn_id=getattr(event, "native_turn_id", "") or None,
        )

    async def adopt_autonomous_turn(
        self,
        *,
        provider_session_id: str | None,
        provider_turn_id: str | None = None,
    ) -> bool:
        """Bind a durable daemon turn to a provider-owned run."""
        async with self._turn_state_lock:
            if self.active.turn_id:
                return False
            turn_id = f"turn_{uuid.uuid4().hex}"
            try:
                await self.store.start_turn(
                    turn_id=turn_id,
                    provider_session_id=provider_session_id or None,
                )
            except Exception as exc:  # noqa: BLE001 - retried by runtime wake
                self._degrade(f"autonomous turn adoption failed: {exc}")
                log_runtime_event(
                    logger,
                    "turn.autonomous_adoption",
                    agent_id=self.agent_id,
                    turn_id=turn_id,
                    provider_session_id=provider_session_id,
                    outcome="rejected",
                    error_category=type(exc).__name__,
                )
                return False
            self._activate_autonomous_turn(
                turn_id=turn_id,
                provider_session_id=provider_session_id,
                provider_turn_id=provider_turn_id,
            )
            return True

    async def _start_notice_unless_autonomous(self, planned: PlannedTurn) -> bool:
        """Start a notice unless adoption won while its context was resolved.

        Context resolution stretches the race window past the top-of-loop
        guard. Queue behind the autonomous run instead of clobbering its
        active binding; the run's terminal ``notify()`` re-enters processing.
        """
        async with self._turn_state_lock:
            if self._autonomous_turn_id:
                return False
            await self._start_local_turn(planned)
            return True

    def _activate_autonomous_turn(
        self,
        *,
        turn_id: str,
        provider_session_id: str | None,
        provider_turn_id: str | None,
    ) -> None:
        self.active.clear()
        self.active.turn_id = turn_id
        self.active.provider_session_id = provider_session_id or None
        self.active.provider_turn_id = provider_turn_id
        self._autonomous_turn_id = turn_id
        self._autonomous_planned = self._empty_autonomous_plan(turn_id)
        if self.coordinator is not None:
            self.coordinator.provider_session_id = provider_session_id or None
        self.attempts.reset()
        self.health = RuntimeHealth("in_progress", "autonomous provider turn")
        log_runtime_event(
            logger,
            "turn.autonomous_adoption",
            agent_id=self.agent_id,
            turn_id=turn_id,
            provider_session_id=provider_session_id,
            provider_turn_id=provider_turn_id,
            state="in_turn",
            outcome="adopted",
        )

    @staticmethod
    def _empty_autonomous_plan(turn_id: str) -> PlannedTurn:
        return PlannedTurn(
            turn_id=turn_id,
            planning_cycle_key=f"autonomous_{turn_id}",
            message_ids=(),
            items=(),
            routes=(),
            targets=(),
            pending_targets=(),
            target_summary="",
            formatted_blocks=(),
            provider_input="",
            formatted_tokens=0,
            wrapper_overhead_tokens=0,
            formatted_bytes=0,
            wrapper_overhead_bytes=0,
        )

    async def _release_autonomous_turn(self, *, reason: str) -> bool:
        if not self._autonomous_turn_id:
            return False
        return await self.finish_autonomous_turn(outcome=reason)

    async def finish_autonomous_turn(self, *, outcome: str = "succeeded") -> bool:
        """Settle and release the turn opened for an autonomous provider run."""
        async with self._turn_state_lock:
            turn_id = self._autonomous_turn_id
            if not turn_id:
                return False
            if self.active.turn_id != turn_id:
                # The adopted turn lost its active binding to another writer.
                # Its durable row must still settle: a row left in_turn reads
                # as "agent busy" to the scheduler, so notices arm but never
                # come due, permanently.
                if not await self._settle_orphaned_autonomous_row(
                    turn_id, outcome=outcome
                ):
                    return False
                self.notify()
                return True
            self._autonomous_turn_id = ""
            self._autonomous_settle_pending = None
            planned = self._autonomous_planned or self._empty_autonomous_plan(turn_id)
            self._autonomous_planned = None
            succeeded = outcome in {"succeeded", ""}
            if not await self._settle_autonomous_turn(
                planned=planned,
                outcome=outcome,
                succeeded=succeeded,
            ):
                return False
            await self._complete_autonomous_turn(
                planned=planned,
                outcome=outcome,
                succeeded=succeeded,
            )
        self.notify()
        return True

    async def _settle_orphaned_autonomous_row(
        self, turn_id: str, *, outcome: str
    ) -> bool:
        """Settle the durable row of an adopted turn that lost its binding.

        Requeued semantics: once the binding is gone the run's outcome can no
        longer be attributed to this row, the same convention crash recovery
        uses for stale in_turn accounting. Rows admitted into the turn go back
        to pending, the provider session's notice-delivery evidence is
        released (a busy notice may have marked the rows delivered against
        this session, which would starve them from future candidates), and a
        mid-run status admission gets its terminal. Only the known idempotent
        terminal (``requeued``) counts as already-settled; anything else keeps
        the owner and retries via settle-pending -- never clear silently.
        """
        try:
            run = await self.store.get_turn_run(turn_id)
            if run is None:
                raise RuntimeError("orphaned autonomous turn disappeared")
            if run.state == "in_turn":
                if run.message_ids:
                    await self.store.requeue_messages(
                        run.message_ids, turn_id=turn_id
                    )
                else:
                    await self.store.finalize_empty_turn(
                        turn_id=turn_id, state="requeued"
                    )
                await self.store.release_notice_delivery(run.provider_session_id)
                # Durable ids, never the current ``active`` -- that state
                # belongs to whichever writer took the binding.
                await self._settle_recovered_status(
                    turn_id=turn_id,
                    message_ids=run.message_ids,
                    succeeded=False,
                    error_text="orphaned autonomous turn requeued",
                )
            elif run.state == "requeued":
                # A prior attempt (or crash recovery) committed the row
                # transition; the notice release may still be outstanding
                # from that partial attempt. Continue from the durable phase.
                await self.store.release_notice_delivery(run.provider_session_id)
            else:
                raise RuntimeError(
                    f"orphaned autonomous turn in unexpected state {run.state}"
                )
        except Exception as exc:  # noqa: BLE001 - retained for bounded retry
            self._autonomous_settle_pending = outcome
            self._degrade(f"orphaned autonomous turn settle failed: {exc}")
            log_runtime_event(
                logger,
                "turn.autonomous_finalized",
                level=logging.ERROR,
                agent_id=self.agent_id,
                turn_id=turn_id,
                outcome="failed",
                error_category=type(exc).__name__,
            )
            return False
        self._autonomous_turn_id = ""
        self._autonomous_planned = None
        self._autonomous_settle_pending = None
        log_runtime_event(
            logger,
            "turn.autonomous_finalized",
            agent_id=self.agent_id,
            turn_id=turn_id,
            state="requeued",
            outcome="orphaned",
        )
        return True

    async def _settle_autonomous_turn(
        self,
        *,
        planned: PlannedTurn,
        outcome: str,
        succeeded: bool,
    ) -> bool:
        turn_id = planned.turn_id
        try:
            run = await self.store.get_turn_run(turn_id)
            expected_state = "processed" if succeeded else "requeued"
            if run is None:
                raise RuntimeError("autonomous turn disappeared before settlement")
            if run.state == "in_turn":
                if succeeded:
                    await self._mark_active_processed(planned, time.monotonic())
                else:
                    await self._requeue_active_turn(
                        planned,
                        time.monotonic(),
                        error_category=outcome,
                    )
            elif run.state == expected_state:
                # The row transition may have committed before notice cleanup
                # failed. Continue from that durable phase on retry.
                await self.store.release_notice_delivery(
                    self.active.provider_session_id,
                    tuple(self.active.notice_message_ids),
                )
            else:
                raise RuntimeError(
                    f"autonomous turn settled as unexpected state {run.state}"
                )
        except Exception as exc:  # noqa: BLE001 - retained for bounded retry
            self._autonomous_turn_id = turn_id
            self._autonomous_planned = planned
            self._autonomous_settle_pending = outcome
            self._degrade(f"autonomous turn settle failed: {exc}")
            log_runtime_event(
                logger,
                "turn.autonomous_finalized",
                level=logging.ERROR,
                agent_id=self.agent_id,
                turn_id=turn_id,
                message_count=len(self.active.message_ids),
                outcome="failed",
                error_category=type(exc).__name__,
            )
            return False
        return True

    async def _complete_autonomous_turn(
        self,
        *,
        planned: PlannedTurn,
        outcome: str,
        succeeded: bool,
    ) -> None:
        log_runtime_event(
            logger,
            "turn.autonomous_finalized",
            agent_id=self.agent_id,
            turn_id=planned.turn_id,
            provider_session_id=self.active.provider_session_id,
            message_count=len(self.active.message_ids),
            state="processed" if succeeded else "requeued",
            outcome=outcome,
        )
        self.health = RuntimeHealth("in_progress" if succeeded else "degraded", "")
        await self._notify_status_terminal(
            terminal=True,
            succeeded=succeeded,
            error_text=None if succeeded else outcome,
        )
        self._finalize_process(planned, terminal=True)
        self.active.clear()
        self.attempts.reset()
        self.health = RuntimeHealth()
