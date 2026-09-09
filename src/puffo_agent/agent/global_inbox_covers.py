"""Finalize-time cover reconciliation for the global Inbox runtime."""

from __future__ import annotations

import logging
import time
from typing import Any

from ._logging import log_runtime_event
from .global_inbox_types import PlannedTurn
from .message_projection import sender_type
from .message_store import ProcessingState

# Existing operators and tests subscribe to the runtime logger, not this
# implementation module.
logger = logging.getLogger("puffo_agent.agent.global_inbox_runtime")

MCP_SILENCE_WARNING_EVERY_TURNS = 3


class CoversReconciliationMixin:
    """Cover reconciliation + completion for ``GlobalInboxRuntime``.

    No independent lifecycle: the runtime owns ``store``, ``active``,
    ``covers_renotice_enabled`` and ``identity_aliases``; this trait only
    keeps the finalize-time reconciliation within the module size limit.
    """

    async def _reconcile_uncovered_messages(
        self, turn_id: str
    ) -> tuple[tuple[str, ...], dict[str, Any]]:
        """Return this turn's uncovered non-agent messages, plus its rows.

        The observation event fires unconditionally; the returned ids drive
        redelivery only when the renotice flag is on. Rows already
        redelivered once (``renotified``) are excluded, which is what bounds
        the cycle to a single extra presentation per message. Classification
        is by exclusion — only rows affirmatively identified as agent or
        system traffic are exempt, because an unidentifiable sender (e.g. a
        non-operator human DM that carries no roster facts) is far more
        likely a human than an agent.
        """
        rows = await self.store.get_in_turn_messages(
            turn_id, self.active.provider_session_id
        )
        rows_by_id = {row.envelope_id: row for row in rows}
        candidate_ids = [
            row.envelope_id
            for row in rows
            if not row.renotified
            and sender_type(
                row, current_agent_aliases=self.identity_aliases
            ) not in ("agent", "system")
        ]
        if not candidate_ids:
            return (), rows_by_id
        covered = await self.store.get_covered_ids(candidate_ids)
        uncovered = tuple(item for item in candidate_ids if item not in covered)
        if not uncovered:
            return (), rows_by_id
        log_runtime_event(
            logger,
            "turn.uncovered_messages",
            level=logging.WARNING,
            agent_id=self.agent_id,
            turn_id=turn_id,
            provider_session_id=self.active.provider_session_id,
            provider_turn_id=self.active.provider_turn_id,
            message_count=len(uncovered),
            envelope_ids=list(uncovered),
            outcome="renotice" if self.covers_renotice_enabled else "observed",
        )
        return (
            uncovered if self.covers_renotice_enabled else (),
            rows_by_id,
        )

    async def _finalize_active_messages(
        self, turn_id: str
    ) -> tuple[tuple[str, ...], dict[str, Any]]:
        """Reconcile covers and complete the active turn's message rows.

        Shared by the normal completion path and crash recovery, so a turn
        resumed after a daemon restart gets the same uncovered-message
        observation and redelivery as one that finished in place.
        """
        renotice_ids: tuple[str, ...] = ()
        rows_by_id: dict[str, Any] = {}
        try:
            renotice_ids, rows_by_id = (
                await self._reconcile_uncovered_messages(turn_id)
            )
        except Exception:
            # Reconciliation must never turn a completed turn into a
            # failure — but its own failure must not silently settle every
            # row either: that would make the safety net's breakage
            # reproduce exactly the loss it exists to stop. Surface the
            # failure and fall back to a durable store-side partition that
            # redelivers whatever still holds its one-shot bound.
            logger.exception("cover reconciliation failed for turn %s", turn_id)
            fallback = (
                "renotice_unrenotified"
                if self.covers_renotice_enabled
                else "observed"
            )
            log_runtime_event(
                logger,
                "turn.cover_reconciliation_failed",
                level=logging.ERROR,
                agent_id=self.agent_id,
                turn_id=turn_id,
                provider_session_id=self.active.provider_session_id,
                message_count=len(self.active.message_ids),
                outcome=fallback,
            )
            if self.covers_renotice_enabled:
                try:
                    redelivered = (
                        await self.store.complete_turn_renotice_unrenotified(
                            turn_id=turn_id,
                            provider_session_id=(
                                self.active.provider_session_id
                            ),
                        )
                    )
                    return redelivered, {}
                except Exception:
                    logger.exception(
                        "fallback renotice failed; completing turn %s plainly",
                        turn_id,
                    )
        if renotice_ids and set(self.active.message_ids) != set(rows_by_id):
            # The in-memory turn disagrees with the store's membership.
            # Renotice partitioning would paper over the drift, so fall
            # through to the strict path and let it raise.
            logger.error(
                "active turn membership drifted from the store; "
                "skipping renotice for turn %s", turn_id,
            )
            renotice_ids = ()
        if renotice_ids:
            renotice_set = set(renotice_ids)
            await self.store.complete_turn_with_renotice(
                tuple(
                    item
                    for item in self.active.message_ids
                    if item not in renotice_set
                ),
                renotice_ids,
                turn_id=turn_id,
                provider_session_id=self.active.provider_session_id,
            )
        else:
            await self.store.mark_processed(
                tuple(self.active.message_ids),
                turn_id=turn_id,
                provider_session_id=self.active.provider_session_id,
            )
        return renotice_ids, rows_by_id

    async def _mark_active_processed(
        self,
        planned: PlannedTurn,
        process_started: float,
    ) -> None:
        # A processed turn ends the incident: consecutive-degrade backoff must
        # not carry across unrelated later incidents.
        self._degraded_attempts = 0
        renotice_ids: tuple[str, ...] = ()
        rows_by_id: dict[str, Any] = {}
        if self.active.message_ids:
            renotice_ids, rows_by_id = await self._finalize_active_messages(
                planned.turn_id
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
        await self._observe_mcp_read_progress()
        renotice_set = set(renotice_ids)
        for item_id in self.active.message_ids:
            row = rows_by_id.get(item_id)
            if row is None:
                row = await self.store.get_message_by_envelope(item_id)
            log_runtime_event(
                logger,
                "inbox.row_processed",
                agent_id=self.agent_id,
                turn_id=planned.turn_id,
                provider_session_id=self.active.provider_session_id,
                message_id=item_id,
                server_seq=row.server_seq if row is not None else None,
                outcome="renoticed" if item_id in renotice_set else "processed",
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

    async def _observe_mcp_read_progress(self) -> None:
        """Warn when repeated notice turns never reach ``read_inbox``.

        One empty notice turn is a supported deferral. Repeated empty turns
        while messages remain pending are also the daemon-visible signature
        of a provider whose MCP client is wedged, so surface that ambiguity
        without claiming a transport root cause we cannot observe here.
        """
        if not self.active.notice_message_ids:
            return
        if self.active.message_ids or not await self.store.get_pending(limit=1):
            self._mcp_silence_streak = 0
            return
        self._mcp_silence_streak += 1
        if self._mcp_silence_streak % MCP_SILENCE_WARNING_EVERY_TURNS:
            return
        logger.warning(
            "agent %s: possible MCP control-plane failure — %d consecutive "
            "Inbox notice turns completed without read_inbox admission while "
            "messages remain pending",
            self.agent_id,
            self._mcp_silence_streak,
        )
