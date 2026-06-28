"""api-puffo worker loop.

Replaces the regular ``PuffoCoreMessageClient`` + adapter flow for
cloud-hosted agents. Connects to puffo-cloud-server via WS (bearer
auth), receives encrypted envelopes inline with the sender's
signing pubkey, decrypts locally with the agent's KEM key, and
per-turn calls ``POST /v1/llm/complete`` with the assembled system
prompt + decrypted text. Tool calls returned by the LLM are
dispatched to the matching cloud endpoint via session-token RPC.

Per-envelope handling is single-turn (no thread queue, no
messages.db) until the wider Phase 5 work lands; this is enough
for end-to-end smoke against a mock cloud server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ...crypto.encoding import base64url_decode
from ...crypto.message import decrypt_message
from ...crypto.primitives import KemKeyPair
from ...portal.profile_sync import extract_soul_body
from ...portal.state import AgentConfig
from .cloud_client import CloudHttpClient, CloudWsClient, llm_complete
from .keystore import ApiPuffoKeystore
from .tools import TOOL_SCHEMAS, dispatch_tool

logger = logging.getLogger(__name__)


_MAX_TOOL_ROUNDS = 8


class ApiPuffoRunner:
    def __init__(self, agent_id: str, stop_event: asyncio.Event) -> None:
        self.agent_id = agent_id
        self._stop = stop_event
        self._keys: ApiPuffoKeystore | None = None
        self._kem_kp: KemKeyPair | None = None
        self._cfg: AgentConfig | None = None
        self._http: CloudHttpClient | None = None
        self._ws: CloudWsClient | None = None

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
                "api-puffo runner %s: init failed: %s",
                self.agent_id, exc,
            )
            return

        self._kem_kp = KemKeyPair.from_secret_bytes(
            base64url_decode(self._keys.kem_secret_key),
        )
        self._http = CloudHttpClient(
            self._keys.puffo_cloud_server_url, self._keys.session_token,
        )
        self._ws = CloudWsClient(
            self._keys.puffo_cloud_server_url,
            self._keys.session_token,
            self._keys.slug,
        )
        logger.info(
            "api-puffo runner %s: started (cloud=%s)",
            self.agent_id, self._keys.puffo_cloud_server_url,
        )

        try:
            await self._listen_loop()
        finally:
            await self._cleanup()

    async def _listen_loop(self) -> None:
        assert self._ws is not None
        listen_task = asyncio.create_task(self._consume_ws())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait(
                {listen_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self._ws.stop()
            for t in (listen_task, stop_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

    async def _consume_ws(self) -> None:
        assert self._ws is not None
        try:
            async for frame in self._ws.listen():
                try:
                    await self._handle_frame(frame)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "api-puffo runner %s: frame handler raised: %s",
                        self.agent_id, exc,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "api-puffo runner %s: WS loop exited: %s",
                self.agent_id, exc,
            )

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type", "")
        if kind == "envelope":
            await self._handle_envelope_frame(frame)
            return
        if kind == "ping":
            return
        logger.debug(
            "api-puffo runner %s: unhandled frame type %r", self.agent_id, kind,
        )

    async def _handle_envelope_frame(self, frame: dict[str, Any]) -> None:
        """Frame shape from cloud:
            {"type": "envelope",
             "envelope": <MessageEnvelope dict>,
             "sender_signing_public_key": "<base64url 32 bytes>"}

        Sender pubkey is inlined by the cloud (which already
        authenticated the sender) — saves the agent a /certs/sync
        round-trip per inbound message. Full cert-sync remains the
        long-term answer when certs rotate or trust needs to be
        re-anchored client-side.
        """
        assert self._keys is not None and self._kem_kp is not None and self._http is not None
        envelope = frame.get("envelope")
        sender_pk_b64 = frame.get("sender_signing_public_key", "")
        if not isinstance(envelope, dict) or not sender_pk_b64:
            logger.warning(
                "api-puffo runner %s: malformed envelope frame; skipping",
                self.agent_id,
            )
            return
        try:
            sender_pk = base64url_decode(sender_pk_b64)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "api-puffo runner %s: sender pubkey decode failed: %s",
                self.agent_id, exc,
            )
            return

        try:
            payload = decrypt_message(
                envelope, self._keys.device_id, self._kem_kp, sender_pk,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "api-puffo runner %s: decrypt failed for envelope_id=%s: %s",
                self.agent_id, envelope.get("envelope_id"), exc,
            )
            return

        if payload.content_type != "text/plain":
            logger.info(
                "api-puffo runner %s: skipping non-text content_type=%r",
                self.agent_id, payload.content_type,
            )
            return

        user_text = payload.content if isinstance(payload.content, str) else str(payload.content)
        logger.info(
            "api-puffo runner %s: dispatching turn (envelope_id=%s, sender=%s, %d chars)",
            self.agent_id, payload.envelope_id, payload.sender_slug, len(user_text),
        )
        await self._run_turn(user_text)

    async def _run_turn(self, user_text: str) -> None:
        """One LLM turn with tool calling. Loops up to
        ``_MAX_TOOL_ROUNDS`` until the model returns a final text
        reply. Errors are logged + the turn is dropped (no auto-
        post on failure — operator tails the audit log)."""
        assert self._http is not None and self._cfg is not None and self._keys is not None
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
                resp = await llm_complete(
                    self._http,
                    api_key=self._cfg.runtime.api_key,
                    provider=self._cfg.runtime.provider or "anthropic",
                    model=self._cfg.runtime.model,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "api-puffo runner %s: llm_complete failed (round %d): %s",
                    self.agent_id, round_idx, exc,
                )
                return

            content = resp.get("content") or []
            stop_reason = resp.get("stop_reason", "")
            tool_uses = [c for c in content if c.get("type") == "tool_use"]
            text_blocks = [c.get("text", "") for c in content if c.get("type") == "text"]

            messages.append({"role": "assistant", "content": content})

            if not tool_uses or stop_reason == "end_turn":
                reply = "".join(text_blocks).strip()
                if reply:
                    logger.info(
                        "api-puffo runner %s: turn complete (rounds=%d, %d chars)",
                        self.agent_id, round_idx + 1, len(reply),
                    )
                return

            tool_results: list[dict[str, Any]] = []
            for tu in tool_uses:
                name = tu.get("name", "")
                args = tu.get("input", {}) or {}
                tu_id = tu.get("id", "")
                result = await dispatch_tool(self._http, name, args)
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
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("api-puffo runner %s: stopped", self.agent_id)
