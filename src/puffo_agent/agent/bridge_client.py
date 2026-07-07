"""Keyless cloud-bridge WS client (T23 phase 1, experimental).

Ported from the thin runtime's
``packages/puffo-agent-cloud/src/puffo_agent_cloud/cloud_client.py``
(proven in E2B, PR #157) with frame semantics unchanged. The server
holds all crypto — frames are plaintext JSON, authenticated by the
``x-sandbox-token`` header. Wire spec:
``puffo-server/roadmap/cloud-agent/BRIDGE-WIRE-PROTOCOL.md``.

Selected per agent via ``puffo_core.transport: "bridge"`` in agent.yml;
the default ``"native"`` transport keeps today's signed-crypto path and
never imports this module. Deliberately does NOT import anything from
``crypto/`` (slated for deletion once the bridge is the only transport).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any, AsyncIterator, Optional

import aiohttp

logger = logging.getLogger(__name__)


# Module constant (not class attribute) so tests can monkeypatch a
# short interval. Server recv-timeout is 90s.
_HEARTBEAT_INTERVAL_SECONDS = 30.0


class BridgeError(Exception):
    """Server-emitted ``error`` frame (code + message). Categories:
    ``NO_SUBKEY``, ``NOT_AUTHORIZED``, ``DECRYPT_FAILED``,
    ``BAD_FRAME``, ``INTERNAL``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BridgeClosed(Exception):
    """Raised when ``send_*`` is called after the WS closed and
    before reconnect."""


class CloudBridgeClient:
    """One-WS-per-agent plaintext bridge. ``send_*`` methods are
    request/response (correlated by ``client_ref`` or FIFO);
    ``frames()`` yields the inbound stream (``message`` /
    ``pending_delivered`` / uncorrelated ``error``). A background
    task pumps a heartbeat every 30s (server recv-timeout = 90s)."""

    def __init__(
        self, cloud_url: str, sandbox_token: str, agent_slug: str,
    ) -> None:
        ws_base = cloud_url.replace("http", "ws", 1)
        self._url = f"{ws_base.rstrip('/')}/v2/cloud-agents/subscribe"
        # Keep the original http(s):// base for the keyless blob REST
        # routes (``upload_blob`` / ``download_blob``) — same
        # ``x-sandbox-token`` auth as the WS, no signed-crypto seam.
        self._http_base = cloud_url.rstrip("/")
        self._token = sandbox_token
        self._slug = agent_slug
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._heartbeat_task: asyncio.Task | None = None
        # client_ref → Future for ack correlation (one ack per send).
        self._send_acks: dict[str, asyncio.Future] = {}
        # FIFO of futures awaiting ack_result / spaces (no client_ref
        # in the spec — only one in-flight at a time).
        self._ack_result_waiters: asyncio.Queue[asyncio.Future] = asyncio.Queue()
        self._spaces_waiters: asyncio.Queue[asyncio.Future] = asyncio.Queue()

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None),
        )
        headers = {"x-sandbox-token": self._token}
        try:
            self._ws = await self._session.ws_connect(
                self._url, headers=headers, heartbeat=None,
            )
        except aiohttp.WSServerHandshakeError as exc:
            await self._session.close()
            self._session = None
            raise BridgeError(
                "HANDSHAKE",
                f"WS upgrade failed (status={exc.status}): {exc.message}",
            ) from exc
        # Wait for the first frame — must be 'connected'.
        first = await self._ws.receive(timeout=10.0)
        if first.type != aiohttp.WSMsgType.TEXT:
            await self.close()
            raise BridgeError(
                "HANDSHAKE",
                f"expected text 'connected', got {first.type!r}",
            )
        try:
            frame = json.loads(first.data)
        except json.JSONDecodeError as exc:
            await self.close()
            raise BridgeError("HANDSHAKE", f"bad first frame: {exc}") from exc
        if frame.get("type") != "connected":
            await self.close()
            raise BridgeError(
                "HANDSHAKE",
                f"expected 'connected', got {frame.get('type')!r}",
            )
        logger.info("cloud bridge: WS connected (slug=%s)", self._slug)
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def frames(self) -> AsyncIterator[dict]:
        # Yields message / pending_delivered / uncorrelated error.
        # ping swallowed (no reply per spec §5.1); ack / ack_result /
        # spaces routed to send_*() futures.
        if self._ws is None:
            return
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    logger.info(
                        "cloud bridge: WS closing (%s)", msg.type,
                    )
                    break
                continue
            try:
                frame = json.loads(msg.data)
            except json.JSONDecodeError:
                logger.warning(
                    "cloud bridge: dropped non-JSON WS frame",
                )
                continue
            kind = frame.get("type", "")
            if kind == "ping":
                # Server keepalive — no reply per spec §5.1.
                continue
            if kind == "ack":
                client_ref = frame.get("client_ref")
                if client_ref and client_ref in self._send_acks:
                    fut = self._send_acks.pop(client_ref)
                    if not fut.done():
                        fut.set_result(frame)
                    continue
                # Unsolicited ack (e.g. server-side resend / lost
                # correlation) — surface it for diagnostics.
                logger.debug(
                    "cloud bridge: ack with unknown client_ref=%r",
                    client_ref,
                )
                continue
            if kind == "ack_result":
                if not self._ack_result_waiters.empty():
                    fut = self._ack_result_waiters.get_nowait()
                    if not fut.done():
                        fut.set_result(frame)
                    continue
                logger.debug("cloud bridge: ack_result with no waiter")
                continue
            if kind == "spaces":
                if not self._spaces_waiters.empty():
                    fut = self._spaces_waiters.get_nowait()
                    if not fut.done():
                        fut.set_result(frame)
                    continue
                logger.debug("cloud bridge: spaces with no waiter")
                continue
            if kind == "error" and frame.get("client_ref"):
                # An error correlated to a send — route to that future
                # as an exception.
                client_ref = frame["client_ref"]
                if client_ref in self._send_acks:
                    fut = self._send_acks.pop(client_ref)
                    if not fut.done():
                        fut.set_exception(BridgeError(
                            frame.get("code", "ERROR"),
                            frame.get("message", ""),
                        ))
                    continue
            yield frame

    async def _heartbeat_loop(self) -> None:
        while self._ws is not None and not self._ws.closed:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return
            if self._ws is None or self._ws.closed:
                return
            try:
                await self._ws.send_json({"type": "heartbeat"})
            except (aiohttp.ClientError, ConnectionError) as exc:
                logger.warning(
                    "cloud bridge: heartbeat send failed: %s", exc,
                )
                return

    async def _require_ws(self) -> aiohttp.ClientWebSocketResponse:
        if self._ws is None or self._ws.closed:
            raise BridgeClosed("WS is not connected")
        return self._ws

    async def upload_blob(self, data: bytes) -> dict:
        """Keyless blob upload: POST the raw plaintext bytes to the
        sandbox blob route, authenticated by ``x-sandbox-token`` (no
        signed crypto — the server holds the at-rest store). Returns the
        server ack JSON ``{ blob_id, size_bytes, uploaded_at }``.

        Raises ``BridgeError`` on any non-2xx / transport / bad-body
        failure so the caller (an outbound attachment send) surfaces a
        clear tool error rather than silently dropping the file. Opens a
        short-lived session per call so blob HTTP is independent of the
        WS lifecycle.
        """
        url = f"{self._http_base}/v2/cloud-agents/blobs/upload"
        headers = {
            "x-sandbox-token": self._token,
            "content-type": "application/octet-stream",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300),
            ) as session:
                async with session.post(
                    url, data=data, headers=headers,
                ) as resp:
                    body = await resp.read()
                    if resp.status // 100 != 2:
                        raise BridgeError(
                            "BLOB_UPLOAD",
                            f"upload failed (status={resp.status}): "
                            f"{body[:200]!r}",
                        )
                    try:
                        return json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise BridgeError(
                            "BLOB_UPLOAD",
                            f"upload returned non-JSON body: {exc}",
                        ) from exc
        except aiohttp.ClientError as exc:
            raise BridgeError(
                "BLOB_UPLOAD", f"upload transport error: {exc}",
            ) from exc

    async def download_blob(self, blob_id: str) -> bytes | None:
        """Keyless blob download by id: GET the raw bytes from the
        sandbox blob route, authenticated by ``x-sandbox-token`` (no
        decrypt). Fail-soft — returns ``None`` on any non-200 (404
        BLOB_NOT_FOUND, 413 FILE_TOO_LARGE, 401 UNAUTHORIZED) or
        transport error, logging at WARNING; a missing / oversized /
        unfetchable blob must never crash the inbound turn or the listen
        loop.
        """
        url = f"{self._http_base}/v2/cloud-agents/blobs/{blob_id}"
        headers = {"x-sandbox-token": self._token}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300),
            ) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "cloud bridge: blob download failed "
                            "(%s, status=%s)", blob_id, resp.status,
                        )
                        return None
                    return await resp.read()
        except Exception as exc:  # noqa: BLE001 — fail-soft download
            logger.warning(
                "cloud bridge: blob download error (%s): %s", blob_id, exc,
            )
            return None

    async def _token_request(
        self, method: str, path: str, *, json_body: Any = None,
    ) -> tuple[int, Any]:
        """Keyless REST call on the sandbox HTTP surface: send ``method``
        to ``{http_base}{path}`` authenticated by ``x-sandbox-token``
        (the same seam ``upload_blob`` / ``download_blob`` use — no
        signed crypto). ``json_body`` is sent as a JSON request body when
        given. Returns ``(status, parsed_json_or_None)``; an empty or
        non-JSON body (e.g. a 204) parses to ``None``. Wraps transport
        failures in ``BridgeError`` so lifecycle callers surface a clean
        error instead of a raw aiohttp exception.

        Opens a short-lived session per call so lifecycle HTTP stays
        independent of the WS lifecycle (mirrors the blob routes).
        """
        url = f"{self._http_base}{path}"
        headers = {"x-sandbox-token": self._token}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            ) as session:
                async with session.request(
                    method, url, headers=headers, json=json_body,
                ) as resp:
                    body = await resp.read()
                    if not body:
                        return resp.status, None
                    try:
                        return resp.status, json.loads(body)
                    except json.JSONDecodeError:
                        return resp.status, None
        except aiohttp.ClientError as exc:
            raise BridgeError(
                "LIFECYCLE", f"{method} {path} transport error: {exc}",
            ) from exc

    async def schedule_wake(
        self,
        *,
        after_seconds: int | None = None,
        wake_at: str | None = None,
        reason: str = "",
    ) -> dict:
        """Schedule a server-side wake for this sandbox: POST
        ``/v2/cloud-agents/schedule-wake`` keyless. Pass exactly one of
        ``after_seconds`` (relative) or ``wake_at`` (absolute ISO ts) —
        the caller enforces that; ``reason`` rides along when non-empty.
        Returns the server's confirmed ``{wake_at, reason}``. Non-2xx /
        transport → ``BridgeError``."""
        body: dict[str, Any] = {}
        if after_seconds is not None:
            body["after_seconds"] = after_seconds
        if wake_at is not None:
            body["wake_at"] = wake_at
        if reason:
            body["reason"] = reason
        status, parsed = await self._token_request(
            "POST", "/v2/cloud-agents/schedule-wake", json_body=body,
        )
        if status // 100 != 2:
            raise BridgeError(
                "SCHEDULE_WAKE",
                f"schedule-wake failed (status={status}): {parsed!r}",
            )
        return parsed if isinstance(parsed, dict) else {}

    async def get_scheduled_wake(self) -> dict:
        """Read the current scheduled wake: GET
        ``/v2/cloud-agents/scheduled-wake`` keyless. Returns the server
        body — ``{wake_at, reason}`` when one is set, or ``{wake_at:
        None}`` when none is scheduled. Non-2xx / transport →
        ``BridgeError``."""
        status, parsed = await self._token_request(
            "GET", "/v2/cloud-agents/scheduled-wake",
        )
        if status // 100 != 2:
            raise BridgeError(
                "SCHEDULED_WAKE",
                f"scheduled-wake read failed (status={status}): {parsed!r}",
            )
        return parsed if isinstance(parsed, dict) else {"wake_at": None}

    async def cancel_wake(self) -> dict:
        """Cancel the scheduled wake: DELETE
        ``/v2/cloud-agents/scheduled-wake`` keyless. Returns the parsed
        body, or ``{}`` on an empty 204. Non-2xx / transport →
        ``BridgeError``."""
        status, parsed = await self._token_request(
            "DELETE", "/v2/cloud-agents/scheduled-wake",
        )
        if status // 100 != 2:
            raise BridgeError(
                "CANCEL_WAKE",
                f"cancel-wake failed (status={status}): {parsed!r}",
            )
        return parsed if isinstance(parsed, dict) else {}

    async def runtime_status(self) -> dict:
        """Read this sandbox's runtime status: GET
        ``/v2/cloud-agents/runtime-status`` keyless. Returns the server
        body (``{state, timeout_at, seconds_until_sleep?, sandbox_id}``);
        ``seconds_until_sleep`` may be ``None`` when the server can't
        compute it — surfaced verbatim, never fabricated. Non-2xx /
        transport → ``BridgeError``."""
        status, parsed = await self._token_request(
            "GET", "/v2/cloud-agents/runtime-status",
        )
        if status // 100 != 2:
            raise BridgeError(
                "RUNTIME_STATUS",
                f"runtime-status read failed (status={status}): {parsed!r}",
            )
        return parsed if isinstance(parsed, dict) else {}

    async def keepalive(self, seconds: int) -> dict:
        """Push back this sandbox's auto-sleep deadline: POST
        ``/v2/cloud-agents/keepalive`` ``{seconds}`` keyless.

        Normalizes the "deadline-refresh not landed upstream" signal to
        a first-class result so the caller branches without catching:
          - 2xx → ``{"available": True, **body}`` (body carries
            ``timeout_at`` / ``seconds_until_sleep``).
          - 501 / 503, or a 2xx body with ``available`` false →
            ``{"available": False, "detail": <msg>}``.
        Any other non-2xx / transport failure → ``BridgeError``."""
        status, parsed = await self._token_request(
            "POST", "/v2/cloud-agents/keepalive",
            json_body={"seconds": seconds},
        )
        parsed = parsed if isinstance(parsed, dict) else {}
        if status in (501, 503):
            detail = (
                parsed.get("error")
                or parsed.get("detail")
                or f"keepalive unavailable (status={status})"
            )
            return {"available": False, "detail": detail}
        if status // 100 == 2:
            if parsed.get("available") is False:
                detail = (
                    parsed.get("error")
                    or parsed.get("detail")
                    or "keepalive reported unavailable"
                )
                return {"available": False, "detail": detail}
            return {"available": True, **parsed}
        raise BridgeError(
            "KEEPALIVE",
            f"keepalive failed (status={status}): {parsed!r}",
        )

    async def send_send(
        self,
        *,
        plaintext: str,
        recipient_slug: Optional[str] = None,
        space_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        reply_to_id: Optional[str] = None,
        thread_root_id: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
        timeout: float = 30.0,
    ) -> dict:
        # Pass EITHER recipient_slug (DM) OR space_id+channel_id
        # (channel); spec rejects mixed-shape frames as BAD_FRAME.
        # ``reply_to_id`` / ``thread_root_id`` are route-agnostic thread
        # linkage — the same snake_case field names a human/web message
        # carries; added only when truthy so a top-level post stays
        # shape-identical to the pre-threading frame.
        # ``attachments`` is the canonical top-level list of
        # ``AttachmentRef`` dicts ({ blob_id, filename?, mime_type?,
        # size_bytes? }); blobs were already uploaded keyless via
        # ``upload_blob``. Added only when non-empty so a plain send
        # stays byte-shape-identical to the pre-attachment frame.
        ws = await self._require_ws()
        client_ref = f"r_{uuid.uuid4().hex[:12]}"
        frame: dict[str, Any] = {
            "type": "send",
            "plaintext": plaintext,
            "client_ref": client_ref,
        }
        if recipient_slug:
            frame["recipient_slug"] = recipient_slug
        if space_id:
            frame["space_id"] = space_id
        if channel_id:
            frame["channel_id"] = channel_id
        if reply_to_id:
            frame["reply_to_id"] = reply_to_id
        if thread_root_id:
            frame["thread_root_id"] = thread_root_id
        if attachments:
            frame["attachments"] = attachments
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._send_acks[client_ref] = fut
        await ws.send_json(frame)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._send_acks.pop(client_ref, None)

    async def send_fetch_pending(self, *, limit: Optional[int] = None) -> None:
        # Resulting message + pending_delivered surface via frames().
        ws = await self._require_ws()
        frame: dict[str, Any] = {"type": "fetch_pending"}
        if limit is not None:
            frame["limit"] = limit
        await ws.send_json(frame)

    async def send_ack(
        self, envelope_ids: list[str], *, timeout: float = 30.0,
    ) -> dict:
        ws = await self._require_ws()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._ack_result_waiters.put(fut)
        await ws.send_json({"type": "ack", "envelope_ids": envelope_ids})
        return await asyncio.wait_for(fut, timeout=timeout)

    async def send_list_spaces(self, *, timeout: float = 30.0) -> dict:
        ws = await self._require_ws()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._spaces_waiters.put(fut)
        await ws.send_json({"type": "list_spaces"})
        return await asyncio.wait_for(fut, timeout=timeout)

    async def close(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
        if self._session is not None and not self._session.closed:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._session = None
        # Cancel pending waiters with a clean BridgeClosed.
        for fut in list(self._send_acks.values()):
            if not fut.done():
                fut.set_exception(BridgeClosed("WS closed"))
        self._send_acks.clear()
        while not self._ack_result_waiters.empty():
            fut = self._ack_result_waiters.get_nowait()
            if not fut.done():
                fut.set_exception(BridgeClosed("WS closed"))
        while not self._spaces_waiters.empty():
            fut = self._spaces_waiters.get_nowait()
            if not fut.done():
                fut.set_exception(BridgeClosed("WS closed"))
