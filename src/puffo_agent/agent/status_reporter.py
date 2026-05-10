"""Reports agent status (idle / busy / error) and per-message
processing runs to the server. Server is the source of truth;
this module only emits events, best-effort.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from ..crypto.http_client import HttpError, PuffoCoreHttpClient

logger = logging.getLogger(__name__)

# Server's offline cutoff is 120s and rate-limits to one beat
# per 10s. 60s gives one missed beat of grace.
DEFAULT_HEARTBEAT_INTERVAL_S = 60.0


class StatusReporter:
    """Owns the heartbeat loop and turn lifecycle hooks.

    Spawn ``run_heartbeat_loop`` once on startup, then call
    ``begin_turn`` / ``end_turn`` from the message handler.
    """

    def __init__(
        self,
        http: PuffoCoreHttpClient,
        *,
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._http = http
        self._interval = max(10.0, heartbeat_interval_s)
        self._current_status: str = "idle"
        self._current_message_id: str | None = None
        self._stop = asyncio.Event()

    async def run_heartbeat_loop(self) -> None:
        # Send one immediately so a fresh agent doesn't sit
        # offline waiting for the first scheduled tick.
        await self._send_heartbeat()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            await self._send_heartbeat()

    def stop(self) -> None:
        self._stop.set()

    async def begin_turn(self, message_id: str) -> str:
        """Returns a ``run_id`` to pass back to ``end_turn``."""
        run_id = f"run_{uuid.uuid4().hex}"
        try:
            await self._http.post(
                f"/messages/{message_id}/processing/start",
                {"run_id": run_id},
            )
            self._current_status = "busy"
            self._current_message_id = message_id
        except HttpError as exc:
            logger.warning("begin_turn message=%s failed (%s)", message_id, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("begin_turn message=%s errored (%s)", message_id, exc)
        return run_id

    async def end_turn(
        self,
        message_id: str,
        run_id: str,
        *,
        succeeded: bool,
        error_text: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"run_id": run_id, "succeeded": succeeded}
        if error_text is not None:
            body["error_text"] = error_text[:1024]  # server MAX_TEXT_LEN
        try:
            await self._http.post(
                f"/messages/{message_id}/processing/end", body
            )
            self._current_status = "idle" if succeeded else "error"
            self._current_message_id = None
        except HttpError as exc:
            logger.warning("end_turn message=%s failed (%s)", message_id, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("end_turn message=%s errored (%s)", message_id, exc)

    async def report_error(self, error_text: str) -> None:
        """Catch-all for unrecoverable failures; cleared by the
        next successful heartbeat.
        """
        try:
            await self._http.post(
                "/agents/me/heartbeat",
                {"status": "error", "error_text": error_text[:1024]},
            )
            self._current_status = "error"
            self._current_message_id = None
        except HttpError as exc:
            logger.warning("report_error failed (%s)", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("report_error errored (%s)", exc)

    async def _send_heartbeat(self) -> None:
        body: dict[str, Any] = {"status": self._current_status}
        if self._current_status == "busy" and self._current_message_id is not None:
            body["current_message_id"] = self._current_message_id
        try:
            await self._http.post("/agents/me/heartbeat", body)
        except HttpError as exc:
            # 429 is expected when a /processing/* call wrote the
            # row inside the 10s rate-limit window.
            level = logging.DEBUG if exc.status == 429 else logging.WARNING
            logger.log(level, "heartbeat failed (%s)", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("heartbeat errored (%s)", exc)


def _ts_ms() -> int:
    return int(time.time() * 1000)
