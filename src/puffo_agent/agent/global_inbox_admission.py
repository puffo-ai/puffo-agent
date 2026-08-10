"""Provider-visible Inbox tool admission for the active durable turn."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ._logging import log_runtime_event
from .context_controller import (
    DecisionOutcome,
    ProviderAdmissionEvent,
    ToolResultAdmission,
)
from .global_inbox_types import (
    ActiveBoundaryAdapter,
    MessageRoute,
    PlannedTurn,
    route_for,
)
from .inbox_scheduler import MAX_FORMATTED_BYTES
from .message_projection import CONTEXT_VERSION
from .message_store import (
    PRIOR_CONTEXT_MAX_BYTES,
    PRIOR_CONTEXT_MAX_ITEMS,
    ProcessingState,
    ReceiptDisposition,
    StoredMessage,
)

# Existing operators and tests subscribe to the runtime logger, not this
# implementation module.
logger = logging.getLogger("puffo_agent.agent.global_inbox_runtime")


@dataclass(frozen=True)
class VisibleReadContext:
    active_turn_id: str
    provider_session_id: str
    space_id: str
    channel_id: str
    through_seq: int
    through_envelope_id: str
    visible_ids: tuple[str, ...]
    correlation_key: str
    correlation_receipt: str


@dataclass(frozen=True)
class InboxPageProjection:
    blocks: tuple[str, ...]
    selected: tuple[StoredMessage, ...]
    next_cursor: str | None
    has_more: bool
    remaining_count: int


@dataclass(frozen=True)
class PriorContextProjection:
    blocks: tuple[str, ...]
    message_ids: tuple[str, ...]
    has_more: bool


@dataclass(frozen=True)
class InboxReadContext:
    active_turn_id: str
    provider_session_id: str
    selected: tuple[StoredMessage, ...]
    routes: tuple[MessageRoute, ...]
    prior_context_ids: tuple[str, ...]
    correlation_key: str
    correlation_receipt: str
    snapshot_generation: int
    remaining_count: int


class InboxAdmissionMixin:
    """Implementation trait for ``GlobalInboxRuntime`` admission handling.

    The runtime remains the only lifecycle and mutable-state owner. This trait
    exists solely to keep the compatibility facade readable and bounded.
    """

    def _raise_send_mode_for(self, rows: Sequence[Any]) -> None:
        """Raise the turn's E2EE obligation for a mid-turn admission.

        The bundle flag was established from the planned batch. Admitting an
        encrypted row afterwards extends that obligation to the rest of the
        turn; a plaintext admission must never lower it.
        """
        if not any(getattr(row, "is_encrypted", False) for row in rows if row):
            return
        from . import send_mode

        send_mode.raise_turn_bundle(list(self.send_mode_keys))

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
        if fired[0] or event.planning_cycle_key != correlation_key:
            return
        if (
            self.active.turn_id != active_turn_id
            or self.active.provider_session_id != provider_session_id
            or self.active.provider_turn_id != evidence.provider_turn_id
            or event.provider_session_id != provider_session_id
            or event.provider_turn_id != evidence.provider_turn_id
        ):
            raise RuntimeError("held admission crossed the active provider turn")
        rows = [
            await self.store.get_message_by_envelope(item) for item in displayed_ids
        ]
        if any(
            row is None
            or row.space_id != space_id
            or row.channel_id != channel_id
            or row.server_seq is None
            or row.server_seq > latest_seq
            for row in rows
        ):
            raise RuntimeError("held admission evidence no longer matches local rows")
        if any(
            row is not None
            and row.processing_state is not ProcessingState.PENDING
            and not (
                row.processing_state is ProcessingState.IN_TURN
                and row.processing_turn_id == active_turn_id
            )
            and row.receipt_disposition is not ReceiptDisposition.TERMINAL
            for row in rows
        ):
            raise RuntimeError("held admission rows crossed another turn boundary")
        pending_ids = [
            row.envelope_id
            for row in rows
            if row is not None and row.processing_state is ProcessingState.PENDING
        ]
        fired[0] = True
        self._raise_send_mode_for(rows)
        try:
            if pending_ids:
                await self.store.admit_messages(
                    pending_ids,
                    turn_id=active_turn_id,
                    provider_session_id=provider_session_id,
                )
            await self._add_visible_message_ids(
                displayed_ids,
                space_id=space_id,
                channel_id=channel_id,
                through_seq=latest_seq,
            )
            # The durable turn-membership table is authoritative.  In
            # particular, do not construct this union from the previous
            # in-memory snapshot: another same-turn admission may already
            # have changed it, and callers retain this list object.
            durable_rows = await self.store.get_in_turn_messages(
                active_turn_id,
                provider_session_id,
            )
            durable_ids = [row.envelope_id for row in durable_rows]
            self.active.message_ids[:] = durable_ids
            durable_routes = [route_for(row) for row in durable_rows]
            self.active.routes[:] = list(dict.fromkeys(durable_routes))
            self._write_active_current_turn()
            await ActiveBoundaryAdapter(
                self.store, self.active
            ).advance_active_turn_through_seq(
                space_id,
                channel_id,
                latest_seq,
            )
        except Exception:
            self._log_held_admission_failure(
                event=event,
                active_turn_id=active_turn_id,
                provider_session_id=provider_session_id,
                evidence=evidence,
            )
            raise
        self._held_admission_evidence.pop(evidence_key, None)

    def _log_held_admission_failure(
        self,
        *,
        event: ProviderAdmissionEvent,
        active_turn_id: str,
        provider_session_id: str,
        evidence: Any,
    ) -> None:
        log_runtime_event(
            logger,
            "held.admission_failed",
            level=logging.WARNING,
            agent_id=self.agent_id,
            turn_id=active_turn_id,
            provider_session_id=provider_session_id,
            provider_turn_id=event.provider_turn_id,
            tool_call_id=event.tool_call_id,
            envelope_id=evidence.latest_envelope_id,
            space_id=evidence.space_id,
            channel_id=evidence.channel_id,
            latest_seq=evidence.latest_seq,
            state="admission_failed",
        )

    async def stage_held_send_result(
        self,
        result: dict[str, Any],
        *,
        tool_name: str,
        tool_arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Stage exactly the semantic held rows at the original tool boundary."""
        payload = result.get("reconsideration")
        if not isinstance(payload, Mapping) or not payload.get("context_ready"):
            return result
        active_turn_id = self.active.turn_id
        provider_session_id = self.active.provider_session_id
        if not active_turn_id or not provider_session_id:
            payload = dict(payload)
            payload["context_ready"] = False
            payload["diagnostic"] = "active held admission identity unavailable"
            result["reconsideration"] = payload
            return result
        target = (
            payload.get("target") if isinstance(payload.get("target"), Mapping) else {}
        )
        space_id = str(target.get("space_id") or "")
        channel_id = str(target.get("channel_id") or "")
        latest_seq = payload.get("latest_seq")
        latest_envelope_id = payload.get("latest_envelope_id")
        if (
            not space_id
            or not channel_id
            or not isinstance(latest_seq, int)
            or not isinstance(latest_envelope_id, str)
        ):
            return result
        evidence_key = (
            active_turn_id,
            provider_session_id,
            space_id,
            channel_id,
            latest_seq,
            latest_envelope_id,
        )
        evidence = self._held_admission_evidence.get(evidence_key)
        if evidence is None or not evidence.displayed_ids:
            payload = dict(payload)
            payload["context_ready"] = False
            payload["diagnostic"] = "exact held recovery evidence unavailable"
            result["reconsideration"] = payload
            return result
        displayed_ids = evidence.displayed_ids
        correlation_key = f"held_send_{uuid.uuid4().hex}"
        receipt = uuid.uuid4().hex
        marker = ToolResultAdmission.build(
            lambda _event: None,
            correlation_key,
            evidence.provider_turn_id,
            correlation_receipt=receipt,
        ).receipt_marker
        fired = [False]

        async def admit_held(event: ProviderAdmissionEvent) -> None:
            await self._admit_held_recovery(
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

        if self._admits_tool_results_on_return:
            await self._admit_returned_tool_result(
                admit_held,
                planning_cycle_key=correlation_key,
                provider_session_id=provider_session_id,
                provider_turn_id=evidence.provider_turn_id,
            )
        else:
            register = getattr(self.adapter, "register_continuation_callback", None)
            if not callable(register):
                payload = dict(payload)
                payload["context_ready"] = False
                payload["diagnostic"] = "provider cannot correlate held tool results"
                result["reconsideration"] = payload
                return result
            register(
                admit_held,
                correlation_key,
                tool_names=(tool_name,),
                tool_arguments=dict(tool_arguments),
                correlation_receipt=receipt,
            )
        result["tool_result_admission"] = marker
        if (staging := self.held.get((space_id, channel_id))) is not None:
            staging.correlation_key = correlation_key
        return result

    def _write_active_current_turn(self) -> None:
        """Atomically persist the exact active union after held admission."""
        targets = tuple(dict.fromkeys(route.target for route in self.active.routes))
        self._write_current_turn(
            PlannedTurn(
                turn_id=self.active.turn_id,
                planning_cycle_key="held_admission",
                message_ids=tuple(self.active.message_ids),
                items=(),
                routes=tuple(self.active.routes),
                targets=targets,
                pending_targets=(),
                target_summary="",
                formatted_blocks=(),
                provider_input="",
                formatted_tokens=0,
                wrapper_overhead_tokens=0,
                formatted_bytes=0,
                wrapper_overhead_bytes=0,
            )
        )

    def note_input_ready(self, turn_id: str) -> None:
        self.notice_delivery.note_input_ready(turn_id)

    async def offer_busy_notice(self, *, turn_id: str) -> bool:
        """Offer metadata-only work to the named active Turn when safe."""
        if turn_id != self.active.turn_id:
            return False
        planned = await self.plan_pending(
            provider_session_id=self.active.provider_session_id,
        )
        if planned is None:
            return False
        decision = await self.context_controller.decide(
            planned.candidate, self._replacement_candidate
        )
        if decision.outcome is not DecisionOutcome.ADMIT:
            return False
        planned = decision.candidate.payload
        if not isinstance(planned, PlannedTurn):
            return False
        offer = getattr(self.adapter, "offer_inbox_notice", None)
        if not callable(offer):
            return False

        async def deliver() -> bool:
            return bool(await offer(turn_id, planned.provider_input))

        accepted = await self.notice_delivery.offer(
            named_turn_id=turn_id,
            active_turn_id=self.active.turn_id,
            deliver=deliver,
        )
        if not accepted:
            return False
        return await self.store.mark_notice_delivered(
            planned.notice_generation,
            self.active.provider_session_id,
        )

    def resolve_active_send_route(
        self,
        destination: str,
        request: Any,
        kwargs: Mapping[str, Any],
    ) -> MessageRoute | None:
        """Return only a route already resolved for the active durable turn."""
        root_id = (
            request.get("root_id", "")
            if isinstance(request, Mapping)
            else getattr(request, "root_id", "")
            if request is not None
            else kwargs.get("root_id", "")
        )
        dm_peer = destination[1:] if destination.startswith("@") else ""
        for route in self.active.routes:
            if route.kind == "dm":
                if dm_peer and route.dm_peer == dm_peer:
                    return route
                continue
            if route.channel_id != destination:
                continue
            if root_id:
                if route.thread_root_id == str(root_id):
                    return route
                continue
            if route.kind == "channel":
                return route
        return None

    def resolve_plain_fallback_route(self) -> MessageRoute | None:
        """Return an implicit route only when admitted context is unambiguous."""
        unique: dict[tuple[str, ...], MessageRoute] = {}
        for route in self.active.routes:
            unique.setdefault(route.target, route)
        if len(unique) != 1:
            return None
        return next(iter(unique.values()))

    @staticmethod
    def _batch_route_projection(planned: PlannedTurn) -> list[dict[str, Any]]:
        projected: dict[tuple[str, ...], dict[str, Any]] = {}
        for item, route in zip(planned.items, planned.routes):
            entry = projected.setdefault(
                route.target,
                {
                    "space_id": route.space_id,
                    "channel_id": route.channel_id,
                    "thread_root_id": route.thread_root_id,
                    "dm_peer": route.dm_peer,
                    "count": 0,
                    "min_seq": None,
                    "max_seq": None,
                },
            )
            entry["count"] += 1
            if item.server_seq is not None:
                current_min = entry["min_seq"]
                current_max = entry["max_seq"]
                entry["min_seq"] = (
                    item.server_seq
                    if current_min is None
                    else min(current_min, item.server_seq)
                )
                entry["max_seq"] = (
                    item.server_seq
                    if current_max is None
                    else max(current_max, item.server_seq)
                )
        return [
            {key: value for key, value in route.items() if value not in (None, "")}
            for route in projected.values()
        ]

    async def _prepare_visible_read(
        self,
        *,
        space_id: str,
        channel_id: str,
        through_seq: int,
        through_envelope_id: str,
        visible_message_ids: list[str] | None,
    ) -> VisibleReadContext:
        if through_seq < 0:
            raise RuntimeError("model-visible read sequence must be non-negative")
        row = await self.store.get_message_by_envelope(through_envelope_id)
        if (
            row is None
            or row.server_seq != through_seq
            or row.space_id != space_id
            or row.channel_id != channel_id
            or row.envelope_kind == "dm"
        ):
            log_runtime_event(
                logger,
                "history.read_staged",
                level=logging.DEBUG,
                agent_id=self.agent_id,
                turn_id=self.active.turn_id,
                provider_session_id=self.active.provider_session_id,
                envelope_id=through_envelope_id,
                space_id=space_id,
                channel_id=channel_id,
                latest_seq=through_seq,
                state=(
                    "dm_unsupported"
                    if row is not None and row.envelope_kind == "dm"
                    else "invalid_watermark"
                ),
            )
            raise RuntimeError(
                "model-visible read watermark does not match local storage"
            )
        active_turn_id = self.active.turn_id
        provider_session_id = self.active.provider_session_id
        if not active_turn_id or not provider_session_id:
            log_runtime_event(
                logger,
                "history.read_staged",
                level=logging.DEBUG,
                agent_id=self.agent_id,
                envelope_id=through_envelope_id,
                space_id=space_id,
                channel_id=channel_id,
                latest_seq=through_seq,
                state="no_active_turn",
            )
            raise RuntimeError("no active provider turn for model-visible read")
        visible_ids = list(visible_message_ids or ())
        await self._add_visible_message_ids(
            visible_ids,
            space_id=space_id,
            channel_id=channel_id,
            through_seq=through_seq,
            mutate=False,
        )
        return VisibleReadContext(
            active_turn_id=active_turn_id,
            provider_session_id=provider_session_id,
            space_id=space_id,
            channel_id=channel_id,
            through_seq=through_seq,
            through_envelope_id=through_envelope_id,
            visible_ids=tuple(visible_ids),
            correlation_key=f"visible_read_{uuid.uuid4().hex}",
            correlation_receipt=uuid.uuid4().hex,
        )

    def _log_visible_read(
        self,
        context: VisibleReadContext,
        state: str,
        *,
        event: ProviderAdmissionEvent | None = None,
        name: str = "history.read_staged",
    ) -> None:
        log_runtime_event(
            logger,
            name,
            level=logging.DEBUG,
            agent_id=self.agent_id,
            turn_id=context.active_turn_id,
            provider_session_id=context.provider_session_id,
            provider_turn_id=event.provider_turn_id if event else None,
            tool_call_id=event.tool_call_id if event else None,
            correlation_key=context.correlation_key,
            envelope_id=context.through_envelope_id,
            space_id=context.space_id,
            channel_id=context.channel_id,
            latest_seq=context.through_seq,
            state=state,
        )

    async def _admit_visible_read(
        self,
        context: VisibleReadContext,
        event: ProviderAdmissionEvent,
        fired: list[bool],
    ) -> None:
        if fired[0] or event.planning_cycle_key != context.correlation_key:
            return
        if (
            self.active.turn_id != context.active_turn_id
            or self.active.provider_session_id != context.provider_session_id
            or event.provider_session_id != context.provider_session_id
        ):
            raise RuntimeError(
                "model-visible read admission crossed the active provider turn"
            )
        fired[0] = True
        try:
            await self._add_visible_message_ids(
                context.visible_ids,
                space_id=context.space_id,
                channel_id=context.channel_id,
                through_seq=context.through_seq,
            )
            await ActiveBoundaryAdapter(
                self.store, self.active
            ).advance_active_turn_through_seq(
                context.space_id,
                context.channel_id,
                context.through_seq,
            )
        except Exception:
            self._log_visible_read(context, "admission_failed", event=event)
            raise
        self._log_visible_read(
            context,
            "admitted",
            event=event,
            name="history.read_admitted",
        )

    async def _register_visible_read(
        self,
        context: VisibleReadContext,
        callback: Callable[[ProviderAdmissionEvent], Awaitable[None]],
        *,
        tool_name: str,
        tool_arguments: Mapping[str, Any],
    ) -> str:
        if self._admits_tool_results_on_return:
            self._log_visible_read(context, "tool_return")
            await self._admit_returned_tool_result(
                callback,
                planning_cycle_key=context.correlation_key,
                provider_session_id=context.provider_session_id,
            )
            return "admitted"
        register = getattr(self.adapter, "register_continuation_callback", None)
        if not callable(register):
            self._log_visible_read(context, "unsupported_adapter")
            raise RuntimeError(
                "provider cannot correlate model-visible history results"
            )
        try:
            register(
                callback,
                context.correlation_key,
                tool_names=(tool_name,),
                tool_arguments=dict(tool_arguments),
                correlation_receipt=context.correlation_receipt,
            )
        except Exception:
            self._log_visible_read(context, "registration_failed")
            raise
        self._log_visible_read(context, "staged")
        return "staged"

    async def stage_model_visible_read(
        self,
        *,
        space_id: str,
        channel_id: str,
        through_seq: int,
        through_envelope_id: str,
        tool_name: str,
        tool_arguments: Mapping[str, Any],
        visible_message_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Advance freshness at the adapter's proven model-visible boundary."""
        context = await self._prepare_visible_read(
            space_id=space_id,
            channel_id=channel_id,
            through_seq=through_seq,
            through_envelope_id=through_envelope_id,
            visible_message_ids=visible_message_ids,
        )
        fired = [False]

        async def admit_read(event: ProviderAdmissionEvent) -> None:
            await self._admit_visible_read(context, event, fired)

        state = await self._register_visible_read(
            context,
            admit_read,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
        )
        return {
            "state": state,
            "correlation_key": context.correlation_key,
            "correlation_receipt": context.correlation_receipt,
            "space_id": space_id,
            "channel_id": channel_id,
            "through_seq": through_seq,
            "through_envelope_id": through_envelope_id,
        }

    async def _add_visible_message_ids(
        self,
        envelope_ids: list[str] | tuple[str, ...],
        *,
        space_id: str | None = None,
        channel_id: str | None = None,
        through_seq: int | None = None,
        mutate: bool = True,
    ) -> None:
        """Validate local rows first, then append first-seen IDs in order."""
        ids = list(envelope_ids)
        if any(not isinstance(item, str) or not item for item in ids):
            raise RuntimeError("visible message IDs must be non-empty strings")
        rows = [await self.store.get_message_by_envelope(item) for item in ids]
        for row in rows:
            if row is None:
                raise RuntimeError("visible message ID does not resolve locally")
            if space_id is not None and (
                row.envelope_kind == "dm"
                or row.space_id != space_id
                or row.channel_id != channel_id
                or not isinstance(row.server_seq, int)
                or isinstance(row.server_seq, bool)
                or through_seq is None
                or row.server_seq > through_seq
            ):
                raise RuntimeError("visible message ID is incompatible with watermark")
        if mutate:
            seen = set(self.active.visible_message_ids)
            self.active.visible_message_ids.extend(
                item for item in ids if not (item in seen or seen.add(item))
            )

    def _project_inbox_page(self, page: Any) -> InboxPageProjection:
        blocks: list[str] = []
        selected: list[StoredMessage] = []
        byte_count = 0
        for item in page.items:
            block = self.formatter(item)
            block_bytes = len(block.encode("utf-8"))
            if byte_count + block_bytes > MAX_FORMATTED_BYTES:
                break
            blocks.append(block)
            selected.append(item)
            byte_count += block_bytes
        truncated = len(selected) < len(page.items)
        if truncated:
            # Re-page from the last actually returned item while retaining the
            # store-pinned ceiling/generation.
            if not selected:
                raise RuntimeError("oldest Inbox message exceeds the page byte guard")
            decoded = self.store._decode_inbox_cursor(
                page.next_cursor
                or self.store._encode_inbox_cursor(
                    {
                        "v": 1,
                        "target": page.target,
                        "generation": page.snapshot_generation,
                        "ceiling": list(self.store._inbox_order(page.items[-1])),
                        "last": [-1, -1, -1, ""],
                    }
                )
            )
            decoded["last"] = list(self.store._inbox_order(selected[-1]))
            next_cursor = self.store._encode_inbox_cursor(decoded)
            has_more = True
            remaining_count = page.remaining_count + len(page.items) - len(selected)
        else:
            next_cursor = page.next_cursor
            has_more = page.has_more
            remaining_count = page.remaining_count
        return InboxPageProjection(
            blocks=tuple(blocks),
            selected=tuple(selected),
            next_cursor=next_cursor,
            has_more=has_more,
            remaining_count=remaining_count,
        )

    async def _project_prior_context(
        self,
        selected: tuple[StoredMessage, ...],
    ) -> PriorContextProjection:
        prior_context: list[str] = []
        prior_context_ids: list[str] = []
        prior_context_has_more = False
        if selected:
            anchors: dict[tuple[str, ...], StoredMessage] = {}
            for item in selected:
                anchors.setdefault(route_for(item).target, item)
            prior_rows: dict[str, StoredMessage] = {}
            for anchor in anchors.values():
                prior_page = await self.store.get_prior_context_page(
                    anchor,
                    limit=PRIOR_CONTEXT_MAX_ITEMS,
                    max_bytes=PRIOR_CONTEXT_MAX_BYTES,
                )
                prior_context_has_more = prior_context_has_more or prior_page.has_more
                for item in prior_page.items:
                    prior_rows.setdefault(item.envelope_id, item)
            prior_byte_count = 0
            for item in sorted(prior_rows.values(), key=self.store._inbox_order):
                block = self.formatter(item)
                block_bytes = len(block.encode("utf-8"))
                if (
                    len(prior_context) >= PRIOR_CONTEXT_MAX_ITEMS
                    or prior_byte_count + block_bytes > PRIOR_CONTEXT_MAX_BYTES
                ):
                    prior_context_has_more = True
                    continue
                prior_context.append(block)
                prior_context_ids.append(item.envelope_id)
                prior_byte_count += block_bytes
        return PriorContextProjection(
            blocks=tuple(prior_context),
            message_ids=tuple(prior_context_ids),
            has_more=prior_context_has_more,
        )

    async def _admit_inbox_read(
        self,
        context: InboxReadContext,
        event: ProviderAdmissionEvent,
        fired: list[bool],
    ) -> None:
        if fired[0] or event.planning_cycle_key != context.correlation_key:
            return
        if (
            self.active.turn_id != context.active_turn_id
            or self.active.provider_session_id != context.provider_session_id
            or event.provider_session_id != context.provider_session_id
        ):
            raise RuntimeError("Inbox read admission crossed the active Turn")
        ids = tuple(item.envelope_id for item in context.selected)
        run = await self.store.admit_messages(
            ids,
            turn_id=context.active_turn_id,
            provider_session_id=context.provider_session_id,
        )
        fired[0] = True
        self._raise_send_mode_for(context.selected)
        self.active.message_ids[:] = list(run.message_ids)
        await self._add_visible_message_ids(list(ids) + list(context.prior_context_ids))
        self.active.routes.extend(context.routes)
        turn_rows = await self.store.get_in_turn_messages(
            context.active_turn_id,
            context.provider_session_id,
        )
        if turn_rows:
            self._write_current_turn(
                self._reconstruct_exact_turn(
                    turn_id=context.active_turn_id,
                    rows=turn_rows,
                )
            )
        for item, route in zip(context.selected, context.routes):
            if item.server_seq is not None and route.kind != "dm":
                key = (route.space_id, route.channel_id)
                proven = await self.store.get_model_visible_through_seq(
                    context.active_turn_id,
                    route.space_id,
                    route.channel_id,
                )
                if proven is not None:
                    self.active.through_by_channel[key] = proven
            log_runtime_event(
                logger,
                "inbox.row_in_turn",
                agent_id=self.agent_id,
                turn_id=context.active_turn_id,
                provider_session_id=context.provider_session_id,
                provider_turn_id=event.provider_turn_id,
                tool_call_id=event.tool_call_id,
                correlation_key=context.correlation_key,
                message_id=item.envelope_id,
                server_seq=item.server_seq,
                target=self.store.target_projection(item),
                notice_generation=context.snapshot_generation,
                outcome="in_turn",
            )
        log_runtime_event(
            logger,
            "inbox.read_admitted",
            agent_id=self.agent_id,
            turn_id=context.active_turn_id,
            provider_session_id=context.provider_session_id,
            provider_turn_id=event.provider_turn_id,
            correlation_key=context.correlation_key,
            notice_generation=context.snapshot_generation,
            message_count=len(ids),
            outcome="admitted",
        )

    async def _register_inbox_read(
        self,
        context: InboxReadContext,
        callback: Callable[[ProviderAdmissionEvent], Awaitable[None]],
        arguments: Mapping[str, Any],
    ) -> None:
        if self._admits_tool_results_on_return:
            self._log_inbox_read_staged(context, "tool_return")
            await self._admit_returned_tool_result(
                callback,
                planning_cycle_key=context.correlation_key,
                provider_session_id=context.provider_session_id,
            )
            return
        register = getattr(self.adapter, "register_continuation_callback", None)
        if not callable(register):
            raise RuntimeError("provider cannot correlate Inbox tool results")
        register(
            callback,
            context.correlation_key,
            tool_names=("read_inbox",),
            tool_arguments=dict(arguments),
            correlation_receipt=context.correlation_receipt,
        )
        self._log_inbox_read_staged(context, "staged")

    def _log_inbox_read_staged(
        self,
        context: InboxReadContext,
        outcome: str,
    ) -> None:
        log_runtime_event(
            logger,
            "inbox.read_staged",
            agent_id=self.agent_id,
            turn_id=context.active_turn_id,
            provider_session_id=context.provider_session_id,
            correlation_key=context.correlation_key,
            notice_generation=context.snapshot_generation,
            message_count=len(context.selected),
            remaining_count=context.remaining_count,
            outcome=outcome,
        )

    async def read_inbox(
        self,
        *,
        target: str = "",
        cursor: str = "",
        limit: int = 50,
        tool_arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return one content page and admit it at the adapter's read boundary."""
        active_turn_id = self.active.turn_id
        provider_session_id = self.active.provider_session_id
        if not active_turn_id or not provider_session_id:
            raise RuntimeError("no active provider turn for Inbox read")
        page = await self.store.read_inbox_page(
            target=target, cursor=cursor, limit=limit
        )
        projection = self._project_inbox_page(page)
        prior = await self._project_prior_context(projection.selected)
        correlation_receipt = ""
        if projection.selected:
            context = InboxReadContext(
                active_turn_id=active_turn_id,
                provider_session_id=provider_session_id,
                selected=projection.selected,
                routes=tuple(route_for(item) for item in projection.selected),
                prior_context_ids=prior.message_ids,
                correlation_key=f"inbox_read_{uuid.uuid4().hex}",
                correlation_receipt=uuid.uuid4().hex,
                snapshot_generation=page.snapshot_generation,
                remaining_count=projection.remaining_count,
            )
            fired = [False]

            async def admit_read(event: ProviderAdmissionEvent) -> None:
                await self._admit_inbox_read(context, event, fired)

            arguments = (
                tool_arguments
                if tool_arguments is not None
                else {"target": target, "cursor": cursor, "limit": limit}
            )
            await self._register_inbox_read(context, admit_read, arguments)
            correlation_receipt = context.correlation_receipt
        return {
            "context_version": CONTEXT_VERSION,
            "messages": list(projection.blocks),
            "prior_context": list(prior.blocks),
            "prior_context_has_more": prior.has_more,
            "next_cursor": projection.next_cursor,
            "has_more": projection.has_more,
            "remaining_count": projection.remaining_count,
            "snapshot_generation": page.snapshot_generation,
            "correlation_receipt": correlation_receipt,
        }
