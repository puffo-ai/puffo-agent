"""Drives one connected tool: pump bundles, gate on ack, relay replies,
and judge the connection dead when it goes quiet.

All collaborators are injected so the whole orchestration is testable
with a fake transport and a fake clock:

  transport     .send(str) / .recv()->str|None / .close()
  reporter      .begin_turn(mid)->run_id / .end_turn_batch(runs)
  tool_dispatch {name: handler}  # WS_LOCAL_ALLOWED_TOOLS subset
  on_acked      (Bundle) -> awaitable   # advance server cursor

Liveness is connection-level (point 2): if no inbound frame arrives
within ``ack_timeout_s``, the tool is presumed dead — the in-flight
bundle rolls back + merges (``BundleQueue``) and the transport closes,
freeing the identity for a fresh handshake. An *alive* tool may take
as long as it likes to ack (point 4b); pings keep it counted alive.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional, Protocol

from .bundles import Bundle, BundleQueue
from .protocol import (
    Ack,
    End,
    Ping,
    Pong,
    ProtocolError,
    SendBundle,
    ToolCall,
    ToolResult,
    decode_inbound,
    encode,
)

logger = logging.getLogger(__name__)


class Transport(Protocol):
    async def send(self, raw: str) -> None: ...
    async def recv(self) -> Optional[str]: ...
    async def close(self) -> None: ...


class Reporter(Protocol):
    async def begin_turn(self, message_id: str) -> str: ...
    async def end_turn_batch(self, runs: list[dict]) -> None: ...


class WsLocalSession:
    def __init__(
        self,
        *,
        slug: str,
        session_id: str,
        transport: Transport,
        queue: BundleQueue,
        reporter: Reporter,
        tool_dispatch: dict[str, Callable[..., Awaitable[Any]]],
        on_acked: Callable[[Bundle], Awaitable[None]],
        now: Callable[[], float],
        ack_timeout_s: float,
        ping_interval_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        make_run_id: Callable[[], str] = lambda: f"run_{uuid.uuid4().hex}",
        on_dead: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.slug = slug
        self.session_id = session_id
        self._transport = transport
        self._queue = queue
        self._reporter = reporter
        self._tool_dispatch = tool_dispatch
        self._on_acked = on_acked
        self._on_dead = on_dead
        self._now = now
        self._ack_timeout_s = ack_timeout_s
        self._ping_interval_s = ping_interval_s
        self._sleep = sleep
        self._make_run_id = make_run_id

        self._alive = True
        self._last_rx = 0.0
        self._death_reason: str | None = None
        # run_id minted at begin_turn for the in-flight bundle's first
        # message; the rest of the batch gets fresh ids at ack time.
        self._inflight_run_id: str | None = None

    @property
    def alive(self) -> bool:
        return self._alive

    # ── public entry points ──────────────────────────────────────────────────

    async def deliver(
        self, root_id: str, message: dict[str, Any], channel_meta: dict[str, Any]
    ) -> None:
        """Hand one freshly decrypted server message to the queue and try
        to send."""
        self._queue.enqueue(root_id, message, channel_meta)
        await self._pump()

    async def deliver_batch(
        self, root_id: str, messages: list[dict[str, Any]], channel_meta: dict[str, Any]
    ) -> None:
        """Enqueue a whole root batch (the shape the server hands the
        daemon) then pump once, so co-arriving messages share a bundle."""
        for message in messages:
            self._queue.enqueue(root_id, message, channel_meta)
        await self._pump()

    async def run(self) -> str:
        """Serve until the connection closes or is judged dead. Returns
        the death reason. The handshake (connect/connected) is done by
        the caller before this; ``run`` owns the steady state. Always
        rolls back any in-flight bundle on the way out."""
        self._last_rx = self._now()
        await self._pump()
        watchdog = asyncio.ensure_future(self._watchdog())
        try:
            while self._alive:
                raw = await self._transport.recv()
                if raw is None:
                    break
                self._last_rx = self._now()
                try:
                    frame = decode_inbound(raw)
                except ProtocolError as exc:
                    logger.warning("ws-local %s: bad frame: %s", self.slug, exc)
                    continue
                await self._dispatch(frame)
        finally:
            watchdog.cancel()
            await self._die("connection closed")
        return self._death_reason or "connection closed"

    # ── frame handling ───────────────────────────────────────────────────────

    async def _dispatch(self, frame: Any) -> None:
        if isinstance(frame, Ack):
            await self._on_ack(frame.bundle_id)
        elif isinstance(frame, End):
            await self._on_end(frame.bundle_id)
        elif isinstance(frame, ToolCall):
            await self._run_tool_call(frame)
        elif isinstance(frame, Ping):
            await self._transport.send(encode(Pong()))
        elif isinstance(frame, Pong):
            pass

    def _report_status(self, event: str, payload: dict) -> None:
        """Best-effort agent.status to the operator — never break the loop."""
        try:
            from ..control.reporter import get_reporter

            asyncio.ensure_future(get_reporter().emit(self.slug, event, payload))
        except Exception:  # noqa: BLE001
            pass

    async def _run_tool_call(self, call: ToolCall) -> None:
        handler = self._tool_dispatch.get(call.tool)
        if handler is None:
            await self._transport.send(encode(ToolResult(
                command_id=call.command_id, ok=False,
                error=f"unknown tool: {call.tool!r}",
            )))
            return
        tool_event = {"tool": call.tool}
        if "send_message" in call.tool and isinstance(call.params.get("text"), str):
            tool_event["content"] = call.params["text"][:200]
        self._report_status("tool_use", tool_event)
        try:
            result = await handler(**call.params)
        except Exception as exc:
            logger.warning(
                "ws-local %s: tool %s raised: %s",
                self.slug, call.tool, exc,
            )
            await self._transport.send(encode(ToolResult(
                command_id=call.command_id, ok=False, error=str(exc),
            )))
            return
        await self._transport.send(encode(ToolResult(
            command_id=call.command_id, ok=True, result=result,
        )))

    async def _pump(self) -> None:
        if not self._alive or self._queue.has_inflight:
            return
        bundle = self._queue.next_to_send()
        if bundle is None:
            return
        await self._transport.send(encode(SendBundle(
            bundle_id=bundle.bundle_id,
            root_id=bundle.root_id,
            channel_meta=bundle.channel_meta,
            messages=bundle.messages,
        )))

    async def _on_ack(self, bundle_id: str) -> None:
        """Flip status to working_on. Idempotent — duplicate, post-end,
        or unknown bundle_id are all no-ops."""
        inflight = self._queue.inflight
        if inflight is None or inflight.bundle_id != bundle_id:
            return
        if self._inflight_run_id is not None:
            return
        first = inflight.envelope_ids()[0] if inflight.messages else ""
        if first:
            self._inflight_run_id = await self._reporter.begin_turn(first)
            text = inflight.messages[0].get("text") if inflight.messages else ""
            self._report_status(
                "turn_start", {"message": text[:200] if isinstance(text, str) else ""}
            )

    async def _on_end(self, bundle_id: str) -> None:
        """Close the turn, advance the cursor, pump next. Mints
        begin_turn inline when ack was skipped so the turn record is
        complete either way."""
        bundle = self._queue.ack(bundle_id)
        if bundle is None:
            return
        if self._inflight_run_id is None:
            first = bundle.envelope_ids()[0] if bundle.messages else ""
            if first:
                self._inflight_run_id = await self._reporter.begin_turn(first)
        await self._reporter.end_turn_batch(self._runs_for(bundle))
        self._report_status("turn_complete", {})  # ws-local has no token usage
        await self._on_acked(bundle)
        self._inflight_run_id = None
        await self._pump()

    def _runs_for(self, bundle: Bundle) -> list[dict]:
        runs: list[dict] = []
        for i, eid in enumerate(bundle.envelope_ids()):
            if not eid:
                continue
            run_id = (
                self._inflight_run_id
                if i == 0 and self._inflight_run_id
                else self._make_run_id()
            )
            runs.append({"run_id": run_id, "message_id": eid, "succeeded": True})
        return runs

    # ── liveness ─────────────────────────────────────────────────────────────

    async def _watchdog(self) -> None:
        try:
            while self._alive:
                await self._sleep(self._ping_interval_s)
                if not await self._watchdog_tick():
                    return
        except asyncio.CancelledError:
            pass

    async def _watchdog_tick(self) -> bool:
        """One liveness check + keepalive ping. False = stop the loop."""
        if not self._alive:
            return False
        if self._now() - self._last_rx > self._ack_timeout_s:
            await self._die("liveness timeout")
            return False
        try:
            await self._transport.send(encode(Ping()))
        except Exception:
            await self._die("send failed")
            return False
        return True

    async def _die(self, reason: str) -> str:
        if not self._alive:
            return self._death_reason or reason
        self._alive = False
        self._death_reason = reason
        self._queue.rollback_inflight()
        self._inflight_run_id = None
        if self._on_dead is not None:
            try:
                await self._on_dead(reason)
            except Exception as exc:  # noqa: BLE001
                logger.debug("ws-local %s: on_dead raised: %s", self.slug, exc)
        try:
            await self._transport.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ws-local %s: close raised: %s", self.slug, exc)
        return reason
