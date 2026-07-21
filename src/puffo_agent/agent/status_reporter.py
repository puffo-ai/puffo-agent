"""Reports agent status (idle / busy / error) and per-message
processing runs to the server. Server is the source of truth;
this module only emits events, best-effort.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from ..crypto.http_client import HttpError, PuffoCoreHttpClient

logger = logging.getLogger(__name__)

# Server's offline cutoff is 120s and rate-limits to one beat
# per 10s. 60s gives one missed beat of grace.
DEFAULT_HEARTBEAT_INTERVAL_S = 60.0


# Synthetic local envelopes have no server row; /messages/<id>/processing/*
# would 404, so skip the HTTP round-trip (the worker still tracks the run).
_LOCAL_ONLY_ENVELOPE_PREFIXES = ("intro-prompt-",)


def _is_local_only_envelope(message_id: str) -> bool:
    return message_id.startswith(_LOCAL_ONLY_ENVELOPE_PREFIXES)


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
        runtime_health_provider: Optional[Callable[[], str]] = None,
        runtime_provider: Optional[Callable[[], dict[str, Any]]] = None,
        status_sender: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> None:
        self._http = http
        # Keyless (bridge) agents can't sign the HTTP status routes, so the
        # worker hands us this async sender to report status over the bridge WS
        # instead. The server folds it into the same ``agent_status`` row + WS
        # broadcast, so the operator's status dot + Log are identical. None on
        # native agents → the signed HTTP path below is used unchanged.
        self._status_sender = status_sender
        # Keyless (T23 bridge) transport: the agent authenticates with an
        # egress-injected ``x-sandbox-token`` and holds NO local signing
        # identity. Every status wire call here goes through the *signed*
        # ``PuffoCoreHttpClient.post`` path (``_ensure_subkey`` →
        # ``load_session`` → ``_rotate_subkey`` → ``load_identity``), which
        # raises "identity not found: <slug>" for a keyless agent — on every
        # heartbeat, ``begin_turn`` and ``end_turn``. Reading the flag off the
        # http client (the SAME signal the tools + worker use;
        # ``getattr(..., False)`` keeps test fakes and native agents at False)
        # lets us route those status updates over the bridge WS
        # (``status_sender``) instead, so no ``load_identity`` is ever attempted.
        self._keyless = bool(getattr(http, "keyless", False))
        self._interval = max(10.0, heartbeat_interval_s)
        self._current_status: str = "idle"
        self._current_message_id: str | None = None
        # None keeps the legacy single-stream wire shape.
        self._runtime_health_provider = runtime_health_provider
        # Agent Portal: reports kind/provider/harness/model so the operator's
        # portal can show + pre-select the live model.
        self._runtime_provider = runtime_provider
        self._stop = asyncio.Event()

    async def run_heartbeat_loop(self) -> None:
        # Native agents POST /agents/me/heartbeat; keyless bridge agents emit a
        # status frame over the bridge (``_send_heartbeat`` routes by transport).
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

    async def _emit_keyless(
        self,
        status: str,
        current_message_id: str | None,
        error_text: str | None,
    ) -> None:
        """Report status over the bridge (keyless agents). Best-effort — a
        failed send must never block a turn (mirrors the HTTP path's
        exception-swallowing)."""
        if self._status_sender is None:
            return
        try:
            await self._status_sender(
                status,
                current_message_id=current_message_id,
                error_text=error_text,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("keyless status emit (%s) failed (%s)", status, exc)

    async def begin_turn(self, message_id: str) -> str:
        """Returns a ``run_id`` to pass back to ``end_turn``."""
        run_id = f"run_{uuid.uuid4().hex}"
        if self._keyless:
            # No signed /processing/* call for bridge agents (see __init__);
            # report "busy" over the bridge so the operator's Log gets the
            # Working row + the yellow dot.
            self._current_status = "busy"
            self._current_message_id = message_id
            await self._emit_keyless("busy", message_id, None)
            return run_id
        if _is_local_only_envelope(message_id):
            # Daemon-minted synthetic envelope (e.g. intro-prompt): no
            # server row, so skip /processing/start — but push an
            # immediate busy heartbeat so the agent shows in-progress
            # while it composes (e.g. its intro). No current_message_id:
            # the message doesn't exist server-side.
            self._current_status = "busy"
            self._current_message_id = None
            await self._send_heartbeat()
            return run_id
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
        if self._keyless:
            # Bridge agents: resolve local status + report it over the bridge
            # (the idle status stamps the Working row's duration + a Ready row).
            self._current_status = "idle" if succeeded else "error"
            self._current_message_id = None
            await self._emit_keyless(
                self._current_status, None, None if succeeded else error_text,
            )
            return
        if _is_local_only_envelope(message_id):
            # Symmetric to ``begin_turn`` — no server-side row, but push
            # the idle/error status now instead of waiting for the next
            # scheduled beat, so the in-progress flag clears promptly.
            self._current_status = "idle" if succeeded else "error"
            self._current_message_id = None
            await self._send_heartbeat()
            return
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

    async def end_turn_batch(self, runs: list[dict]) -> None:
        """Mark every message in a thread batch as processed in one
        round trip.

        ``runs`` is the list of ``{run_id, message_id, succeeded,
        error_text?}`` dicts the server's ``/messages/processing/
        end:batch`` endpoint accepts. The first message in the batch
        has a ``run_id`` from a prior ``begin_turn`` call (so it gets
        a yellow dot); the rest are UPSERTed by the server with
        ``started_at = ended_at = now`` and skip straight to green.
        """
        if not runs:
            return
        if self._keyless:
            # Bridge agents: mirror the batch outcome into local status +
            # report it over the bridge (see __init__).
            any_failed = any(not r["succeeded"] for r in runs)
            self._current_status = "error" if any_failed else "idle"
            self._current_message_id = None
            await self._emit_keyless(self._current_status, None, None)
            return
        payload_runs: list[dict[str, Any]] = []
        for r in runs:
            mid = r.get("message_id", "")
            if _is_local_only_envelope(mid):
                # Drop local-only synthetic envelopes — see
                # ``begin_turn`` rationale. Their run lifecycle is
                # in-memory only.
                continue
            entry: dict[str, Any] = {
                "run_id": r["run_id"],
                "message_id": mid,
                "succeeded": bool(r["succeeded"]),
            }
            err = r.get("error_text")
            if err is not None:
                entry["error_text"] = err[:1024]
            payload_runs.append(entry)
        # If every run was local-only, there's nothing to POST — but
        # still push the resolved status now (see ``begin_turn``).
        if not payload_runs:
            any_failed = any(not r["succeeded"] for r in runs)
            self._current_status = "error" if any_failed else "idle"
            self._current_message_id = None
            await self._send_heartbeat()
            return
        try:
            await self._http.post(
                "/messages/processing/end:batch", {"runs": payload_runs},
            )
            # Server flips agent_status atomically — mirror locally.
            any_failed = any(not r["succeeded"] for r in runs)
            self._current_status = "error" if any_failed else "idle"
            self._current_message_id = None
        except HttpError as exc:
            logger.warning(
                "end_turn_batch (%d runs) failed (%s)", len(runs), exc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "end_turn_batch (%d runs) errored (%s)", len(runs), exc,
            )

    async def report_error(self, error_text: str) -> None:
        """Catch-all for unrecoverable failures; cleared by the
        next successful heartbeat.
        """
        if self._keyless:
            # Bridge agents: record the error locally + report it over the
            # bridge (see __init__).
            self._current_status = "error"
            self._current_message_id = None
            await self._emit_keyless("error", None, error_text[:1024])
            return
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
        if self._keyless:
            # Keyless bridge agents have no signing identity for the HTTP
            # /agents/me/heartbeat route — report the current status over the
            # bridge instead (keeps the operator's status dot fresh + emits a
            # settled Ready row between turns).
            await self._emit_keyless(
                self._current_status,
                self._current_message_id if self._current_status == "busy" else None,
                None,
            )
            return
        body: dict[str, Any] = {"status": self._current_status}
        if self._current_status == "busy" and self._current_message_id is not None:
            body["current_message_id"] = self._current_message_id
        # Agent Portal: tag which machine this agent runs on (None if unlinked).
        from ..portal.control.store import current_machine_id

        machine_id = current_machine_id()
        if machine_id:
            body["machine_id"] = machine_id
            # Runtime only matters in the portal context (a linked machine).
            if self._runtime_provider is not None:
                try:
                    body["runtime"] = self._runtime_provider()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("runtime_provider raised (%s)", exc)
        if self._runtime_health_provider is not None:
            try:
                body["health"] = self._runtime_health_provider()
            except Exception as exc:  # noqa: BLE001
                logger.debug("runtime_health_provider raised (%s)", exc)
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
