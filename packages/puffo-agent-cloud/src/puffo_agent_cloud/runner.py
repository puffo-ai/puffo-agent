"""api-puffo worker loop.

Single-turn per envelope (no thread queue / no local DB) —
conversation history is the live WS stream, ``fetch_pending`` is
the only backfill primitive. On-connect / dispatch / ack cadence
follows BRIDGE-WIRE-PROTOCOL.md §5."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from puffo_agent_core.profile import extract_soul_body
from .config import CloudAgentConfig
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
        self._cfg: CloudAgentConfig | None = None
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
            self._cfg = CloudAgentConfig.load(self.agent_id)
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

        # LLM plane: call the LiteLLM gateway directly with the per-agent
        # virtual key (bundle api_key). Distinct from the WS bridge, which uses
        # puffo_cloud_server_url + sandbox_token.
        self._llm = CloudLlmClient(
            self._cfg.runtime.litellm_gateway_url, self._cfg.runtime.api_key,
        )
        if not self._cfg.runtime.litellm_gateway_url:
            logger.warning(
                "api-puffo runner %s: runtime.litellm_gateway_url is unset — "
                "LLM calls will fail; the create bundle must set it",
                self.agent_id,
            )
        logger.info(
            "api-puffo runner %s: started (bridge=%s, llm_gateway=%s)",
            self.agent_id, self._keys.puffo_cloud_server_url,
            self._cfg.runtime.litellm_gateway_url or "(unset)",
        )

        try:
            await self._connect_and_serve_loop()
        finally:
            await self._cleanup()

    async def _connect_and_serve_loop(self) -> None:
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
        # Reader + worker split: send_send's ack arrives on the same
        # WS the reader is pumping, so the reader must never block on
        # LLM-turn work or dispatch_tool deadlocks.
        assert self._bridge is not None
        self._inbox_consumer = asyncio.create_task(self._inbox_loop())
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._bridge.send_fetch_pending()
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
        # Backfill envelopes batch into one ack on pending_delivered;
        # live messages ack one at a time after each turn.
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
        sender = frame.get("sender_slug", "")
        text = frame.get("plaintext", "")
        if not isinstance(text, str) or not text:
            logger.info(
                "api-puffo runner %s: skipping frame with empty plaintext",
                self.agent_id,
            )
            return
        logger.info(
            "api-puffo runner %s: turn (envelope_id=%s, sender=%s, %d chars)",
            self.agent_id, frame.get("envelope_id", "?"), sender or "?", len(text),
        )
        # MCP-only reply: the model must call send_message to answer. Thread the
        # reply target (DM sender, or the originating channel) into the turn so
        # the system prompt can tell the model exactly how to address it.
        await self._run_turn(
            text,
            sender=sender,
            space_id=frame.get("space_id"),
            channel_id=frame.get("channel_id"),
        )

    def _reply_instruction(
        self, sender: str, space_id: str | None, channel_id: str | None,
    ) -> str:
        """The routing directive appended to the system prompt so the model
        addresses ``send_message`` at whoever/whichever channel messaged it."""
        if space_id and channel_id:
            return (
                "\n\nYou received a message in a channel. To reply, you MUST call "
                f"the send_message tool with space_id={space_id!r} and "
                f"channel_id={channel_id!r}. Do not answer in plain text — a "
                "reply is only delivered when you call send_message."
            )
        if sender:
            return (
                "\n\nYou received a direct message. To reply, you MUST call the "
                f"send_message tool with recipient_slug={sender!r}. Do not answer "
                "in plain text — a reply is only delivered when you call "
                "send_message."
            )
        return ""

    async def _run_turn(
        self,
        user_text: str,
        *,
        sender: str = "",
        space_id: str | None = None,
        channel_id: str | None = None,
    ) -> None:
        """Run the agentic loop for one inbound message.

        Reply is **MCP-only**: the model answers by calling the ``send_message``
        tool. The system prompt (see ``_reply_instruction``) tells it exactly how
        to address the reply — a DM back to ``sender``, or the originating
        channel. A plain-text answer is NOT auto-delivered.
        """
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
        system_prompt += self._reply_instruction(sender, space_id, channel_id)

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_text},
        ]
        for round_idx in range(_MAX_TOOL_ROUNDS):
            try:
                resp = await self._llm.complete(
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
            messages.append({"role": "assistant", "content": content})

            if not tool_uses or stop_reason == "end_turn":
                # End of turn. With MCP-only reply the answer was already
                # delivered via send_message during a prior round; a bare
                # text end_turn means the model chose not to reply.
                logger.info(
                    "api-puffo runner %s: turn complete (rounds=%d)",
                    self.agent_id, round_idx + 1,
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
