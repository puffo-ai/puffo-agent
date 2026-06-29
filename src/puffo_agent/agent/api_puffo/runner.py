"""api-puffo worker loop.

Replaces the regular ``PuffoCoreMessageClient`` + adapter flow for
cloud-hosted agents. Per the bridge wire-protocol spec:

  1. Connect WS at ``<cloud>/v2/cloud-agents/subscribe`` with
     ``x-sandbox-token`` on the upgrade. Wait for ``connected``.
  2. ``fetch_pending`` → drain ``message`` + ``pending_delivered``
     frames → ack the batch. Loop on ``more = true``.
  3. Main loop: every inbound ``message`` frame fires one LLM turn;
     the LLM's tool calls dispatch to bridge frames (``send``,
     ``list_spaces``); ack the envelope after the turn returns
     (success OR failure — server already routes; ack just stops
     re-delivery). Heartbeat is handled by the bridge client
     background task.

Single-turn per envelope for now (no thread queue / no local DB).
Conversation history is the live WS stream; ``fetch_pending`` is
the only backfill primitive."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...portal.profile_sync import extract_soul_body
from ...portal.state import AgentConfig
from .cloud_client import (
    BridgeClosed,
    BridgeError,
    CloudBridgeClient,
    CloudHttpError,
    CloudLlmClient,
)
from .keystore import ApiPuffoKeystore
from .tools import TOOL_SCHEMAS, dispatch_tool

logger = logging.getLogger(__name__)


_MAX_TOOL_ROUNDS = 8
_RECONNECT_BACKOFF_MIN = 1.0
_RECONNECT_BACKOFF_MAX = 30.0


class ApiPuffoRunner:
    def __init__(self, agent_id: str, stop_event: asyncio.Event) -> None:
        self.agent_id = agent_id
        self._stop = stop_event
        self._keys: ApiPuffoKeystore | None = None
        self._cfg: AgentConfig | None = None
        self._llm: CloudLlmClient | None = None
        # Set during each connection epoch.
        self._bridge: CloudBridgeClient | None = None
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._inbox_consumer: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._backfill_done = asyncio.Event()

    async def run(self) -> None:
        try:
            self._keys = ApiPuffoKeystore.for_agent(self.agent_id)
            self._cfg = AgentConfig.load(self.agent_id)
        except FileNotFoundError:
            logger.error(
                "api-puffo runner %s: keystore missing; bundle ingestion failed?",
                self.agent_id,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "api-puffo runner %s: init failed: %s", self.agent_id, exc,
            )
            return

        self._llm = CloudLlmClient(
            self._keys.puffo_cloud_server_url, self._keys.sandbox_token,
        )
        logger.info(
            "api-puffo runner %s: started (cloud=%s)",
            self.agent_id, self._keys.puffo_cloud_server_url,
        )

        try:
            await self._connect_and_serve_loop()
        finally:
            await self._cleanup()

    async def _connect_and_serve_loop(self) -> None:
        """Reconnect with exponential backoff on transport errors.
        Authentication failures (BridgeError code=HANDSHAKE on 401)
        log loud + back off — operator action required."""
        backoff = _RECONNECT_BACKOFF_MIN
        while not self._stop.is_set():
            try:
                self._bridge = CloudBridgeClient(
                    self._keys.puffo_cloud_server_url,
                    self._keys.sandbox_token,
                    self._keys.slug,
                )
                await self._bridge.connect()
                backoff = _RECONNECT_BACKOFF_MIN
                await self._on_connect_flow()
                await self._consume_frames()
            except BridgeError as exc:
                logger.warning(
                    "api-puffo runner %s: bridge error (%s); reconnect in %.1fs",
                    self.agent_id, exc, backoff,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "api-puffo runner %s: bridge loop crashed (%s: %s); "
                    "reconnect in %.1fs",
                    self.agent_id, type(exc).__name__, exc, backoff,
                )
            finally:
                for t_name in ("_reader_task", "_inbox_consumer"):
                    t = getattr(self, t_name)
                    if t is not None:
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                        setattr(self, t_name, None)
                while not self._inbox.empty():
                    try:
                        self._inbox.get_nowait()
                        self._inbox.task_done()
                    except (asyncio.QueueEmpty, ValueError):
                        break
                self._backfill_done = asyncio.Event()
                if self._bridge is not None:
                    try:
                        await self._bridge.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._bridge = None
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)

    async def _on_connect_flow(self) -> None:
        """Spec §5.4: backfill via ``fetch_pending`` → process →
        ack. Reader/worker split so frame consumption never blocks
        on the LLM turn (``send_send``'s ack needs the iterator
        free). Both reader + worker run as concurrent tasks; the
        reader puts ALL relevant frames on the inbox and the worker
        is the single state owner (backfill batch accumulator + ack
        cadence)."""
        assert self._bridge is not None
        self._inbox_consumer = asyncio.create_task(self._inbox_loop())
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._bridge.send_fetch_pending()
        # Wait until the worker reports "backfill drained" or stop fires.
        try:
            await asyncio.wait_for(
                self._backfill_done.wait(), timeout=300.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "api-puffo runner %s: backfill drain timeout; proceeding",
                self.agent_id,
            )

    async def _reader_loop(self) -> None:
        """Pulls every relevant frame off the bridge into the inbox.
        Errors are logged inline (they don't tear down the WS)."""
        assert self._bridge is not None
        try:
            async for frame in self._bridge.frames():
                if self._stop.is_set():
                    return
                kind = frame.get("type", "")
                if kind == "message":
                    await self._inbox.put(("message", frame))
                elif kind == "pending_delivered":
                    await self._inbox.put(("pending_delivered", frame))
                elif kind == "error":
                    logger.warning(
                        "api-puffo runner %s: bridge error frame: %s",
                        self.agent_id, frame,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "api-puffo runner %s: reader loop exited: %s",
                self.agent_id, exc,
            )
        finally:
            # Sentinel to wake the consumer for clean shutdown.
            try:
                self._inbox.put_nowait(("close", {}))
            except asyncio.QueueFull:
                pass

    async def _inbox_loop(self) -> None:
        """Serial worker: process frames in arrival order. Accumulates
        backfill envelope_ids and acks them in one shot when
        ``pending_delivered`` lands; live messages are acked one at a
        time after each turn."""
        assert self._bridge is not None
        in_backfill = True
        backfill_ids: list[str] = []
        while not self._stop.is_set():
            kind, frame = await self._inbox.get()
            try:
                if kind == "close":
                    return
                if kind == "message":
                    envelope_id = frame.get("envelope_id", "")
                    try:
                        await self._run_turn_for_frame(frame)
                    finally:
                        if envelope_id:
                            if in_backfill:
                                backfill_ids.append(envelope_id)
                            else:
                                try:
                                    await self._bridge.send_ack([envelope_id])
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning(
                                        "api-puffo runner %s: live ack "
                                        "failed for %s: %s",
                                        self.agent_id, envelope_id, exc,
                                    )
                elif kind == "pending_delivered":
                    if backfill_ids:
                        try:
                            await self._bridge.send_ack(backfill_ids)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "api-puffo runner %s: backfill ack "
                                "failed: %s",
                                self.agent_id, exc,
                            )
                        backfill_ids = []
                    if frame.get("more"):
                        await self._bridge.send_fetch_pending()
                    else:
                        in_backfill = False
                        logger.info(
                            "api-puffo runner %s: backfill drained, "
                            "entering main loop",
                            self.agent_id,
                        )
                        self._backfill_done.set()
            finally:
                self._inbox.task_done()

    async def _consume_frames(self) -> None:
        """Main loop wait — the reader + inbox tasks do the work.
        Block until stop or reader exits."""
        if self._reader_task is None:
            return
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait(
                {self._reader_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not stop_task.done():
                stop_task.cancel()

    async def _run_turn_for_frame(self, frame: dict[str, Any]) -> None:
        sender = frame.get("sender_slug", "?")
        text = frame.get("plaintext", "")
        if not isinstance(text, str) or not text:
            logger.info(
                "api-puffo runner %s: skipping frame with empty plaintext",
                self.agent_id,
            )
            return
        logger.info(
            "api-puffo runner %s: turn (envelope_id=%s, sender=%s, %d chars)",
            self.agent_id, frame.get("envelope_id", "?"), sender, len(text),
        )
        await self._run_turn(text)

    async def _run_turn(self, user_text: str) -> None:
        """One LLM turn with tool-loop. Caps at ``_MAX_TOOL_ROUNDS``
        rounds before logging a hard cap warning + returning."""
        assert (
            self._llm is not None and self._cfg is not None
            and self._keys is not None and self._bridge is not None
        )
        try:
            soul = extract_soul_body(
                self._cfg.resolve_profile_path().read_text(encoding="utf-8"),
            )
        except Exception:
            soul = ""
        system_prompt = soul or f"You are {self._cfg.display_name}."

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_text},
        ]
        for round_idx in range(_MAX_TOOL_ROUNDS):
            try:
                resp = await self._llm.complete(
                    api_key=self._cfg.runtime.api_key,
                    provider=self._cfg.runtime.provider or "anthropic",
                    model=self._cfg.runtime.model,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                )
            except CloudHttpError as exc:
                logger.warning(
                    "api-puffo runner %s: LLM call failed (round %d): %s",
                    self.agent_id, round_idx, exc,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "api-puffo runner %s: LLM transport (round %d): %s",
                    self.agent_id, round_idx, exc,
                )
                return

            content = resp.get("content") or []
            stop_reason = resp.get("stop_reason", "")
            tool_uses = [c for c in content if c.get("type") == "tool_use"]
            text_blocks = [
                c.get("text", "") for c in content if c.get("type") == "text"
            ]
            messages.append({"role": "assistant", "content": content})

            if not tool_uses or stop_reason == "end_turn":
                reply = "".join(text_blocks).strip()
                if reply:
                    logger.info(
                        "api-puffo runner %s: turn complete "
                        "(rounds=%d, %d chars)",
                        self.agent_id, round_idx + 1, len(reply),
                    )
                return

            tool_results: list[dict[str, Any]] = []
            for tu in tool_uses:
                name = tu.get("name", "")
                args = tu.get("input", {}) or {}
                tu_id = tu.get("id", "")
                try:
                    result = await dispatch_tool(self._bridge, name, args)
                except BridgeClosed:
                    result = "error: bridge closed mid-turn"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})

        logger.warning(
            "api-puffo runner %s: turn hit %d-round cap without end_turn",
            self.agent_id, _MAX_TOOL_ROUNDS,
        )

    async def _cleanup(self) -> None:
        if self._bridge is not None:
            try:
                await self._bridge.close()
            except Exception:  # noqa: BLE001
                pass
            self._bridge = None
        if self._llm is not None:
            try:
                await self._llm.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("api-puffo runner %s: stopped", self.agent_id)
