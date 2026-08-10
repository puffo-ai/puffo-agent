from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence, TYPE_CHECKING

from .global_inbox_types import HeldAdmissionEvidence, HeldStaging
from .message_store import ProcessingState

if TYPE_CHECKING:
    from .global_inbox_runtime import GlobalInboxRuntime


class HeldRecoverySource:
    """Live durable held catch-up for the one active exact turn.

    This deliberately does not invent a remote catch-up API.  It waits for
    receipt commits, then proves the exact terminal watermark exists as a
    pending durable row for the active provider session. The proof is
    synchronization metadata only; it is independent of content pagination
    and never claims that the provider saw the recovered content.
    """

    def __init__(
        self,
        runtime: GlobalInboxRuntime,
        *,
        wait_timeout_s: float = 2.0,
        catchup_pending: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self.runtime = runtime
        self.wait_timeout_s = wait_timeout_s
        self.catchup_pending = catchup_pending
        self._changed = asyncio.Event()

    def notify_delivery(self) -> None:
        self._changed.set()

    async def wait_for_held_delivery(
        self,
        space_id: str,
        channel_id: str,
        latest_seq: int,
        latest_envelope_id: str,
    ) -> bool:
        async def proven() -> bool:
            row = await self.runtime.store.get_message_by_envelope(latest_envelope_id)
            return bool(
                row is not None
                and row.server_seq == latest_seq
                and row.space_id == space_id
                and row.channel_id == channel_id
            )

        deadline = time.monotonic() + self.wait_timeout_s
        while not await proven():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._changed.clear()
            # Re-check after clearing so a commit between the first query and
            # clear cannot be lost.
            if await proven():
                return True
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
        if await proven():
            return True
        if self.catchup_pending is not None:
            try:
                await self.catchup_pending(latest_envelope_id)
            except Exception:
                pass
            if await proven():
                return True
        self.runtime.held[(space_id, channel_id)] = HeldStaging(
            latest_seq=latest_seq,
            latest_envelope_id=latest_envelope_id,
            diagnostic=(
                "exact held watermark unavailable after WebSocket wait "
                "and signed pending catch-up"
            ),
        )
        return False

    async def query_held_messages(
        self,
        space_id: str,
        channel_id: str,
        latest_seq: int,
        latest_envelope_id: str,
        provider_session_id: str | None,
    ) -> Sequence[Mapping[str, Any]]:
        active = self.runtime.active
        if (
            not active.turn_id
            or not provider_session_id
            or active.provider_session_id != provider_session_id
        ):
            self.runtime.held[(space_id, channel_id)] = HeldStaging(
                latest_seq=latest_seq,
                latest_envelope_id=latest_envelope_id,
                diagnostic="stateful active provider session unavailable",
            )
            return ()
        watermark = await self.runtime.store.get_message_by_envelope(latest_envelope_id)
        if (
            watermark is None
            or watermark.server_seq != latest_seq
            or watermark.space_id != space_id
            or watermark.channel_id != channel_id
            or watermark.processing_state is not ProcessingState.PENDING
        ):
            self.runtime.held[(space_id, channel_id)] = HeldStaging(
                latest_seq=latest_seq,
                latest_envelope_id=latest_envelope_id,
                diagnostic="exact held watermark mismatch",
            )
            return ()
        staging = self.runtime.held[(space_id, channel_id)] = HeldStaging(
            latest_seq=latest_seq,
            latest_envelope_id=latest_envelope_id,
            recovered_through_seq=latest_seq,
            synchronized=True,
        )
        through = await self.runtime.store.get_model_visible_through_seq(
            active.turn_id,
            space_id,
            channel_id,
        )
        rows = await self.runtime.store.get_held_reconsideration_rows(
            space_id=space_id,
            channel_id=channel_id,
            after_seq=through if through is not None else -1,
            through_seq=latest_seq,
            limit=51,
        )
        if len(rows) > 50:
            self.runtime.held[(space_id, channel_id)] = HeldStaging(
                latest_seq=latest_seq,
                latest_envelope_id=latest_envelope_id,
                diagnostic="held context exceeds the bounded recovery limit",
            )
            return ()
        projected: list[Mapping[str, Any]] = []
        for row in rows:
            projected.append(
                {
                    "space_id": row.space_id,
                    "channel_id": row.channel_id,
                    "thread_root_id": row.thread_root_id or "",
                    "envelope_id": row.envelope_id,
                    "server_seq": row.server_seq,
                    "latest_seq": latest_seq,
                    "latest_envelope_id": latest_envelope_id,
                    "provider_session_id": provider_session_id,
                    "sender_slug": row.sender_slug,
                    "envelope_kind": row.envelope_kind,
                    "sent_at": row.sent_at,
                    "is_encrypted": row.is_encrypted,
                    "content": row.content,
                }
            )
        evidence = HeldAdmissionEvidence(
            active_turn_id=active.turn_id,
            provider_session_id=provider_session_id,
            provider_turn_id=active.provider_turn_id or "",
            space_id=space_id,
            channel_id=channel_id,
            latest_seq=latest_seq,
            latest_envelope_id=latest_envelope_id,
            displayed_ids=tuple(row.envelope_id for row in rows),
        )
        self.runtime._held_admission_evidence[
            (
                active.turn_id,
                provider_session_id,
                space_id,
                channel_id,
                latest_seq,
                latest_envelope_id,
            )
        ] = evidence
        staging.message_ids = evidence.displayed_ids
        return tuple(projected)
