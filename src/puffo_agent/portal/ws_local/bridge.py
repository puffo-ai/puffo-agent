"""Bridge the durable global inbox runtime to a connected local tool.

``GlobalInboxRuntime`` plans and admits one turn at a time.
``WsLocalBridge.dispatch_planned`` sends that frozen plan and blocks until
the tool acknowledges it; disconnects requeue the active message ids.

Daemon scheduling enters through ``dispatch_planned``, which picks the wire
shape from the peer's capabilities: a v2 peer gets the global bundle, a
capability-less v1 peer gets the frozen single-root batches SKILL.md still
guarantees. ``dispatch`` is retained as the direct v1 adapter for the legacy
consumer callback; removing v1 requires a separate protocol deprecation.

The bridge owns no transport or crypto — the ``WsLocalSession`` runs the
frame loop, relays replies, and judges liveness; the bridge only couples
"this batch" to "its ack".
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping

from .bundles import Bundle
from .session import V2_CAPABILITIES

# Both model-visible-read producers are legitimate and their key names are
# consumed as-is by the RPC/host-mcp path: ``stage_held_send_result`` returns
# ``tool_result_admission``; bounded history/post readers such as
# ``get_post_segment`` return ``admission_receipt``. The bridge accepts either
# private marker shape for continuations that still require provider proof.
_ADMISSION_MARKER_KEYS = ("tool_result_admission", "admission_receipt")


class BridgeClosed(Exception):
    """Raised from ``dispatch`` when the session died before the batch was
    acked — signals the consumer to preserve the cursor for redelivery."""


# ── frozen v1 projection ────────────────────────────────────────────────────
#
# The v1 wire shape is the ``(root_id, batch, channel_meta)`` triple the base
# consumer handed to ``on_message``. ``channel_name``/``space_name`` are
# display-only enrichment the base resolved from a per-session name cache and
# emitted as "" on a miss (DMs are labelled "Direct message"); the store rows a
# ``PlannedTurn`` carries do not hold them, so the miss value is used.


def _v1_field(item, route, name: str) -> str:
    value = getattr(route, name, "") if route is not None else ""
    return str(value or getattr(item, name, "") or "")


def _v1_is_dm(item, route) -> bool:
    kind = getattr(route, "kind", "") if route is not None else ""
    return (kind or getattr(item, "envelope_kind", "")) == "dm"


def _v1_content(item) -> Mapping[str, object]:
    content = getattr(item, "content", None)
    return content if isinstance(content, Mapping) else {}


def _v1_text(item) -> str:
    content = getattr(item, "content", "")
    if isinstance(content, Mapping):
        text = content.get("text")
        if isinstance(text, str) and text:
            return text
        caption = content.get("caption")
        return caption if isinstance(caption, str) else ""
    return content if isinstance(content, str) else ""


def _v1_list(content: Mapping[str, object], *names: str) -> list:
    for name in names:
        value = content.get(name)
        if isinstance(value, list):
            return list(value)
    return []


def _v1_channel_meta(item, route) -> dict[str, object]:
    is_dm = _v1_is_dm(item, route)
    return {
        "channel_id": _v1_field(item, route, "channel_id"),
        "channel_name": "Direct message" if is_dm else "",
        "space_id": _v1_field(item, route, "space_id"),
        "space_name": "",
        "is_dm": is_dm,
    }


def _v1_message(item, route) -> dict[str, object]:
    content = _v1_content(item)
    message = _v1_channel_meta(item, route)
    message.update({
        "sender_slug": str(getattr(item, "sender_slug", "") or ""),
        "sender_display_name": str(content.get("sender_display_name") or ""),
        "sender_email": "",
        "text": _v1_text(item),
        "root_id": str(getattr(item, "thread_root_id", "") or ""),
        "attachments": _v1_list(content, "attachment_paths", "attachments"),
        "sender_is_bot": bool(content.get("sender_is_bot")),
        "mentions": _v1_list(content, "mentions"),
        "envelope_id": str(getattr(item, "envelope_id", "") or ""),
        "sent_at": getattr(item, "sent_at", 0),
        "is_visible_to_human": bool(content.get("is_visible_to_human", True)),
    })
    return message


def _v1_notice_group(planned) -> tuple[str, dict[str, object], list[dict[str, object]]]:
    """The v1 rendering of a scheduled Inbox *notice*.

    A scheduled turn carries no items — it is a notice whose content the
    peer fetches through ``read_inbox``. The v2 bundle
    carries that in its
    ``notice`` field, which the v1 frame has no room for, so the identical
    ``provider_input`` rides the frozen message shape instead. The peer's
    model then reads the same instruction and calls the same tool.
    """
    meta = {
        "channel_id": "", "channel_name": "", "space_id": "",
        "space_name": "", "is_dm": False,
    }
    message = dict(meta)
    message.update({
        "sender_slug": "system",
        "sender_display_name": "",
        "sender_email": "",
        "text": planned.provider_input,
        "root_id": "",
        "attachments": [],
        "sender_is_bot": False,
        "mentions": [],
        "envelope_id": planned.turn_id,
        "sent_at": 0,
        "is_visible_to_human": False,
    })
    return planned.turn_id, meta, [message]


def _v1_groups(planned) -> list[tuple[str, dict[str, object], list[dict[str, object]]]]:
    """Group a planned turn into base-faithful single-root v1 batches.

    The base routed on the validated ``thread_root_id``, falling back to the
    envelope's own id for a top-level post, so a top-level post is its own
    bundle. Insertion order preserves the plan's ordering.
    """
    routes = {route.envelope_id: route for route in planned.routes}
    groups: list[tuple[str, dict[str, object], list[dict[str, object]]]] = []
    index: dict[str, int] = {}
    for item in planned.items:
        route = routes.get(getattr(item, "envelope_id", ""))
        root_id = _v1_field(item, route, "thread_root_id") or str(
            getattr(item, "envelope_id", "") or ""
        )
        if root_id not in index:
            index[root_id] = len(groups)
            groups.append((root_id, _v1_channel_meta(item, route), []))
        groups[index[root_id]][2].append(_v1_message(item, route))
    return groups or [_v1_notice_group(planned)]


class WsLocalBridge:
    def __init__(self, runtime=None) -> None:
        self._waiter: asyncio.Future[None] | None = None
        self._dead_reason: str | None = None
        self._runtime = runtime
        self._admitted = False
        # v1 peers never send `admitted`, so the daemon synthesizes the
        # model-visible transitions for them. Set per dispatched turn.
        self._synthesize_admissions = False

    # ── session hooks (passed to WsLocalSession) ─────────────────────────────

    async def on_acked(self, bundle: Bundle) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    async def on_tool_result(
        self, tool_name: str, tool_arguments: Mapping[str, object], result: object,
    ) -> None:
        """Keep actual tool evidence private while linking it to its receipt."""
        if self._runtime is None or not isinstance(result, Mapping):
            return
        markers = [result.get(key) for key in _ADMISSION_MARKER_KEYS]
        marker = next((m for m in markers if isinstance(m, str)), "")
        if not marker:
            return
        match = re.search(r"\[puffo:model-visible-read:([^\]]+)\]", marker)
        observe = getattr(self._runtime.adapter, "observe_tool_result", None)
        if match is None or not callable(observe):
            return
        observe(match.group(1), tool_name, tool_arguments)
        planned = getattr(self._runtime, "_ws_local_planned", None)
        if self._synthesize_admissions and planned is not None:
            # Returning the result to a v1 peer *is* the model-visible
            # transition; without this the read would never be admitted and
            # the same notice would be re-presented every planning cycle.
            await self._runtime.adapter.emit_admission(
                turn_id=planned.turn_id,
                correlation_key=match.group(1),
                tool_name=tool_name,
                tool_arguments=tool_arguments,
            )

    async def on_admitted(self, bundle: Bundle, frame=None) -> None:
        """Apply the exact model-visible transition; ``ack`` never does."""
        if self._runtime is None or not bundle.turn_id:
            return
        planned = getattr(self._runtime, "_ws_local_planned", None)
        if planned is None or planned.turn_id != bundle.turn_id:
            raise BridgeClosed("ws-local admission correlation failed")
        correlation_key = getattr(frame, "correlation_key", "") or (
            planned.planning_cycle_key if not self._admitted else ""
        )
        emit = getattr(self._runtime.adapter, "emit_admission", None)
        if emit is None:
            raise BridgeClosed("ws-local runtime has no admission seam")
        await emit(
            turn_id=bundle.turn_id,
            correlation_key=correlation_key,
        )
        if correlation_key == planned.planning_cycle_key:
            self._admitted = True

    async def on_dead(self, reason: str) -> None:
        self._dead_reason = reason
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_exception(BridgeClosed(reason))
        if self._runtime is not None and self._runtime.active.turn_id:
            async with self._runtime._turn_state_lock:
                active = self._runtime.active
                if active.message_ids:
                    await self._runtime.store.requeue_messages(
                        tuple(active.message_ids),
                        turn_id=active.turn_id,
                    )
                else:
                    await self._runtime.store.finalize_empty_turn(
                        turn_id=active.turn_id,
                        state="requeued",
                    )
                await self._runtime.store.release_notice_delivery(
                    active.provider_session_id,
                    tuple(active.notice_message_ids),
                )
                self._runtime.active.clear()
            self._runtime.notify()

    async def dispatch_planned(self, session, planned) -> None:
        if self._dead_reason is not None:
            raise BridgeClosed(self._dead_reason)
        self._runtime._ws_local_planned = planned
        self._admitted = False
        self._synthesize_admissions = not V2_CAPABILITIES.issubset(
            getattr(session, "capabilities", frozenset())
        )
        if self._synthesize_admissions:
            await self._dispatch_planned_v1(session, planned)
            return
        loop = asyncio.get_event_loop()
        self._waiter = loop.create_future()
        await session.deliver_planned(planned)
        try:
            await self._waiter
        finally:
            self._waiter = None

    async def _dispatch_planned_v1(self, session, planned) -> None:
        """Deliver a planned turn over the frozen v1 single-root wire shape.

        A capability-less peer cannot receive the v2 global bundle, so the
        plan is mapped back onto ``deliver_batch`` groups — one in-flight
        bundle at a time, matching v1's serial contract. v1 has no
        ``admitted`` frame, so for it delivery *is* the model-visible
        transition: the initial admission is synthesized once, right after
        the first bundle reaches the peer, and the runtime's turn
        bookkeeping proceeds exactly as it does for a v2 peer.
        """
        admitted = False
        for root_id, channel_meta, batch in _v1_groups(planned):
            loop = asyncio.get_event_loop()
            # Set the waiter before delivering so an ack processed on the
            # session's frame loop can't race ahead of us.
            self._waiter = loop.create_future()
            await session.deliver_batch(root_id, batch, channel_meta)
            if not admitted:
                admitted = True
                await self._runtime.adapter.emit_admission(
                    turn_id=planned.turn_id,
                    correlation_key=planned.planning_cycle_key,
                )
            try:
                await self._waiter
            finally:
                self._waiter = None

    # ── consumer callback ────────────────────────────────────────────────────

    async def dispatch(self, session, root_id: str, batch: list[dict], channel_meta: dict) -> None:
        """Send ``batch`` to the tool and block until it acks. Raises
        ``BridgeClosed`` if the session is (or becomes) dead."""
        if self._dead_reason is not None:
            raise BridgeClosed(self._dead_reason)
        loop = asyncio.get_event_loop()
        self._waiter = loop.create_future()
        # Set the waiter before delivering so an ack processed on the
        # session's frame loop can't race ahead of us.
        await session.deliver_batch(root_id, batch, channel_meta)
        try:
            await self._waiter
        finally:
            self._waiter = None
