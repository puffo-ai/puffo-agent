from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Iterable

from .message_store import StoredMessage

MAX_MESSAGES = 50
MAX_ESTIMATED_TOKENS = 32_000
MAX_FORMATTED_BYTES = 96_000
COALESCE_SECONDS = 3.0

TargetProjection = tuple[str, ...]
Formatter = Callable[[StoredMessage], str]
Estimator = Callable[[str], int]


class NoticeDeliveryCapability(str, Enum):
    DIRECT = "direct"
    GATED = "gated"
    NEXT_TURN = "next_turn"


class InboxNoticeDelivery:
    """Inbox-owned busy-input gate; provider implementations stay outside it."""

    def __init__(
        self,
        capability: NoticeDeliveryCapability | str = NoticeDeliveryCapability.NEXT_TURN,
    ) -> None:
        self.capability = NoticeDeliveryCapability(capability)
        self._input_ready_turn = ""

    def note_input_ready(self, turn_id: str) -> None:
        if turn_id:
            self._input_ready_turn = turn_id

    async def offer(
        self,
        *,
        named_turn_id: str,
        active_turn_id: str,
        deliver: Callable[[], bool | Awaitable[bool]],
    ) -> bool:
        if not named_turn_id or named_turn_id != active_turn_id:
            return False
        if self.capability is NoticeDeliveryCapability.NEXT_TURN:
            return False
        if (
            self.capability is NoticeDeliveryCapability.GATED
            and self._input_ready_turn != named_turn_id
        ):
            return False
        result = deliver()
        if inspect.isawaitable(result):
            result = await result
        if self.capability is NoticeDeliveryCapability.GATED:
            self._input_ready_turn = ""
        return result is True


@dataclass(frozen=True)
class PlannedBatch:
    items: tuple[StoredMessage, ...]
    message_ids: tuple[str, ...]
    formatted_messages: tuple[str, ...]
    target_projections: tuple[TargetProjection, ...]
    pending_target_projections: tuple[TargetProjection, ...]
    estimated_tokens: int
    formatted_bytes: int
    more_available: bool
    unfit_head_id: str | None = None
    unfit_reason: str | None = None


class InboxPlanner:
    """Pure, policy-neutral leading-prefix planner."""

    @staticmethod
    def target_projection(item: StoredMessage) -> TargetProjection:
        if item.envelope_kind == "dm":
            return (
                "dm",
                item.sender_slug,
                item.recipient_slug or "",
            )
        if item.thread_root_id:
            return (
                "thread",
                item.space_id or "",
                item.channel_id or "",
                item.thread_root_id,
            )
        return ("channel", item.space_id or "", item.channel_id or "")

    def plan(
        self,
        items: Iterable[StoredMessage],
        *,
        formatter: Formatter,
        estimator: Estimator | None = None,
    ) -> PlannedBatch:
        unique: list[StoredMessage] = []
        seen_ids: set[str] = set()
        for item in items:
            if item.envelope_id in seen_ids:
                continue
            seen_ids.add(item.envelope_id)
            unique.append(item)

        selected: list[StoredMessage] = []
        formatted_selected: list[str] = []
        tokens = 0
        byte_count = 0
        unfit_head_id: str | None = None
        unfit_reason: str | None = None

        for item in unique:
            formatted = formatter(item)
            if not isinstance(formatted, str):
                raise TypeError("formatter must return str")
            formatted_bytes = len(formatted.encode("utf-8"))
            estimate = (
                estimator(formatted)
                if estimator is not None
                else max(1, formatted_bytes)
            )
            if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
                raise ValueError("estimator must return a non-negative integer")

            reasons: list[str] = []
            if len(selected) + 1 > MAX_MESSAGES:
                reasons.append("message count cap")
            if tokens + estimate > MAX_ESTIMATED_TOKENS:
                reasons.append("estimated token cap")
            if byte_count + formatted_bytes > MAX_FORMATTED_BYTES:
                reasons.append("formatted byte cap")
            if reasons:
                if not selected:
                    unfit_head_id = item.envelope_id
                    unfit_reason = ", ".join(reasons)
                break
            selected.append(item)
            formatted_selected.append(formatted)
            tokens += estimate
            byte_count += formatted_bytes

        projections: list[TargetProjection] = []
        seen_targets: set[TargetProjection] = set()
        for item in selected:
            target = self.target_projection(item)
            if target not in seen_targets:
                seen_targets.add(target)
                projections.append(target)

        consumed = len(selected)
        pending_projections: list[TargetProjection] = []
        pending_targets: set[TargetProjection] = set()
        for item in unique[consumed:]:
            target = self.target_projection(item)
            if target not in pending_targets:
                pending_targets.add(target)
                pending_projections.append(target)
        return PlannedBatch(
            items=tuple(selected),
            message_ids=tuple(item.envelope_id for item in selected),
            formatted_messages=tuple(formatted_selected),
            target_projections=tuple(projections),
            pending_target_projections=tuple(pending_projections),
            estimated_tokens=tokens,
            formatted_bytes=byte_count,
            more_available=consumed < len(unique),
            unfit_head_id=unfit_head_id,
            unfit_reason=unfit_reason,
        )

    async def resolve_unfit_head(
        self,
        batch: PlannedBatch,
        *,
        policy: Callable[..., bool | Awaitable[bool]],
        quarantine: Callable[..., bool | Awaitable[bool]],
    ) -> bool:
        """Run the injected oversize policy once, then guarded quarantine."""
        if batch.unfit_head_id is None:
            return False
        reason = batch.unfit_reason or "oldest item cannot fit"
        policy_signature = inspect.signature(policy)
        policy_parameters = policy_signature.parameters.values()
        policy_accepts_reason = any(
            parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
            for parameter in policy_parameters
        ) or len(policy_signature.parameters) >= 2
        still_unfit = (
            policy(batch.unfit_head_id, reason)
            if policy_accepts_reason
            else policy(batch.unfit_head_id)
        )
        if inspect.isawaitable(still_unfit):
            still_unfit = await still_unfit
        if not still_unfit:
            return False
        result = quarantine(batch.unfit_head_id, reason=reason)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)


class InboxCoalescer:
    """Metadata-free, non-resetting fixed-window wake coalescer."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        window_seconds: float = COALESCE_SECONDS,
    ):
        self._sleep = sleep
        self._monotonic = monotonic
        self._window_seconds = window_seconds
        self._wake = asyncio.Event()
        self._pulled = asyncio.Event()
        self._deadlines: deque[float] = deque()
        self._wait_lock = asyncio.Lock()

    def notify(self, *, delay_seconds: float | None = None) -> None:
        now = self._monotonic()
        delay = self._window_seconds if delay_seconds is None else max(
            0.0, delay_seconds
        )
        candidate = now + delay
        if not self._deadlines or now >= self._deadlines[-1]:
            self._deadlines.append(candidate)
        elif candidate < self._deadlines[0]:
            # A pending deadline may only move earlier, never later, so the
            # fixed non-resetting window still swallows a second notify inside
            # its own window.  Without this, a long degraded-backoff deadline
            # would hold newly arrived work for up to its whole backoff even
            # though the same notify already cleared the degraded state.
            self._deadlines[0] = candidate
            self._pulled.set()
        self._wake.set()

    async def _sleep_or_pull(self, remaining: float) -> bool:
        """Sleep ``remaining``; report whether a pulled deadline cut it short."""
        sleeper = asyncio.ensure_future(self._sleep(remaining))
        pull = asyncio.ensure_future(self._pulled.wait())
        try:
            done, _pending = await asyncio.wait(
                {sleeper, pull}, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (sleeper, pull):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        return sleeper not in done

    async def wait_for_burst(self) -> None:
        async with self._wait_lock:
            await self._wake.wait()
            while True:
                self._pulled.clear()
                # Re-read after clearing so a pull between the read and the
                # clear cannot be lost.
                deadline = self._deadlines[0]
                remaining = max(0.0, deadline - self._monotonic())
                if not await self._sleep_or_pull(remaining):
                    break
                if self._deadlines[0] >= deadline:
                    break
            self._deadlines.popleft()
            if self._deadlines:
                self._wake.set()
            else:
                self._wake.clear()
