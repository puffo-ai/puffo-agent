"""Provider-neutral durable timer service for one-shot local reminders."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Awaitable, Callable

from .message_store import (
    MAX_REMINDER_LIST_LIMIT,
    REMINDER_STATES,
    MessageStore,
    ReminderOccurrence,
    ReminderSyncRecord,
    reminder_time_to_rfc3339,
)


_RFC3339_OFFSET = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class ReminderDeliveryAuthorization:
    """Provider-neutral permission result for due reminder delivery."""

    occurrence_ids: tuple[str, ...] = ()
    retry_after_ms: int | None = None


ReminderDeliveryAuthorizer = Callable[
    [tuple[ReminderSyncRecord, ...]], Awaitable[ReminderDeliveryAuthorization]
]


def normalize_reminder_timestamp(value: str) -> tuple[int, str]:
    """Parse an explicit-offset RFC3339 value and return canonical UTC data."""
    if not isinstance(value, str) or not _RFC3339_OFFSET.fullmatch(value):
        raise ValueError("intended_at must be a non-empty RFC3339 timestamp")
    source = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(source)
    except ValueError as exc:
        raise ValueError("intended_at must be RFC3339 with an explicit UTC offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("intended_at must include an explicit UTC offset")
    utc = parsed.astimezone(timezone.utc)
    milliseconds = int(utc.timestamp() * 1000)
    return milliseconds, reminder_time_to_rfc3339(milliseconds)


class ReminderScheduler:
    """Wake only around the next durable local reminder deadline.

    The scheduler has no model/provider policy.  It commits a structured
    local Inbox event through ``MessageStore`` and then uses the same runtime
    ``notify`` seam as an incoming message.
    """

    def __init__(
        self,
        *,
        store: MessageStore,
        notify: Callable[[], None],
        now_ms: Callable[[], int] = _now_ms,
        wait_for_change: Callable[[asyncio.Event, float | None], Awaitable[None]] | None = None,
        delivery_authorizer: ReminderDeliveryAuthorizer | None = None,
        on_lifecycle_committed: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self._notify = notify
        self._now_ms = now_ms
        self._wait_for_change = wait_for_change
        self._delivery_authorizer = delivery_authorizer
        self._on_lifecycle_committed = on_lifecycle_committed
        self._authorization_retry_after_ms: int | None = None
        self._wakeup = asyncio.Event()
        self._stopping = False

    def signal(self) -> None:
        """Recompute the durable deadline after a create or cancellation."""
        self._wakeup.set()

    def stop(self) -> None:
        """Idempotently end a pending wait without owning a worker task."""
        self._stopping = True
        self._wakeup.set()

    def set_delivery_authorizer(
        self, authorizer: ReminderDeliveryAuthorizer | None,
    ) -> None:
        """Install remote delivery custody before this scheduler worker starts."""
        self._delivery_authorizer = authorizer
        self._authorization_retry_after_ms = None
        self.signal()

    def set_lifecycle_committed_callback(
        self, callback: Callable[[], None] | None,
    ) -> None:
        """Wake a provider-neutral outbox after any local lifecycle commit."""
        self._on_lifecycle_committed = callback

    async def create_reminder(
        self, *, content: str, target: str, intended_at: str,
    ) -> dict[str, object]:
        intended_at_ms, _canonical = normalize_reminder_timestamp(intended_at)
        reminder = await self.store.create_reminder(
            content=content,
            target=target,
            intended_at_ms=intended_at_ms,
        )
        self.signal()
        if self._on_lifecycle_committed is not None:
            self._on_lifecycle_committed()
        return reminder.as_dict()

    async def list_reminders(
        self, *, state: str = "", limit: int = 50,
    ) -> dict[str, object]:
        if not isinstance(state, str) or (state and state not in REMINDER_STATES):
            raise ValueError("state must be empty or one of scheduled, claimed, cancelled, delivered")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_REMINDER_LIST_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {MAX_REMINDER_LIST_LIMIT}"
            )
        reminders = await self.store.list_reminders(state=state, limit=limit)
        return {"reminders": [item.as_dict() for item in reminders]}

    async def cancel_reminder(self, *, reminder_id: str) -> dict[str, object]:
        reminder = await self.store.cancel_reminder(reminder_id)
        self.signal()
        if self._on_lifecycle_committed is not None:
            self._on_lifecycle_committed()
        return reminder.as_dict()

    async def process_due_once(self) -> tuple[ReminderOccurrence, ...]:
        """Deliver due or restart-recovered claims and wake after each commit set."""
        now = int(self._now_ms())
        if self._delivery_authorizer is None:
            delivered = await self.store.deliver_due_reminders(now_ms=now)
        else:
            candidates = await self.store.due_reminder_delivery_candidates(now_ms=now)
            authorization = await self._delivery_authorizer(candidates)
            self._authorization_retry_after_ms = authorization.retry_after_ms
            if authorization.occurrence_ids:
                await self.store.claim_authorized_reminders(
                    authorization.occurrence_ids, now_ms=now,
                )
            delivered = await self.store.deliver_authorized_reminders(
                authorization.occurrence_ids, now_ms=now,
            )
        for _occurrence in delivered:
            self._notify()
        if delivered and self._on_lifecycle_committed is not None:
            self._on_lifecycle_committed()
        return delivered

    async def _wait_until_changed_or_due(self, deadline_ms: int | None) -> None:
        if self._wakeup.is_set():
            self._wakeup.clear()
            return
        timeout: float | None = None
        if deadline_ms is not None:
            timeout = max(0.0, (deadline_ms - int(self._now_ms())) / 1000)
        if self._wait_for_change is not None:
            await self._wait_for_change(self._wakeup, timeout)
            self._wakeup.clear()
            return
        try:
            if timeout is None:
                await self._wakeup.wait()
            else:
                await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wakeup.clear()

    async def run(self) -> None:
        """Recover claims first, then wait only for a durable deadline/change."""
        while not self._stopping:
            await self.process_due_once()
            if self._stopping:
                break
            now = int(self._now_ms())
            local_only = self._delivery_authorizer is None
            deadline = await self.store.next_reminder_deadline(
                now_ms=now, local_only=local_only,
            )
            retry = self._authorization_retry_after_ms
            if retry is not None:
                if deadline is None or deadline <= now:
                    # The earliest durable deadline belongs to due work whose
                    # claim was blocked; waiting on it would hot-spin, and the
                    # retry is that work's earliest useful wake. Independent
                    # work scheduled before the retry must still fire on time.
                    deadline = await self.store.next_reminder_deadline(
                        now_ms=now, local_only=local_only, after_ms=now,
                    )
                deadline = retry if deadline is None else min(deadline, retry)
            await self._wait_until_changed_or_due(deadline)
