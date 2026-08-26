"""Reports agent status (idle / busy / error) and per-message
processing runs to the server. Server is the source of truth;
terminal processing runs use a durable local replay queue.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

from ..crypto.http_client import HttpError, PuffoCoreHttpClient
from .processing_receipts import ProcessingReportDispatcher

logger = logging.getLogger(__name__)

# Server's offline cutoff is 120s and rate-limits to one beat
# per 10s. 60s gives one missed beat of grace.
DEFAULT_HEARTBEAT_INTERVAL_S = 60.0


# Synthetic local envelopes have no server row; /messages/<id>/processing/*
# would 404, so skip the HTTP round-trip (the worker still tracks the run).
_LOCAL_ONLY_ENVELOPE_PREFIXES = (
    "intro-prompt-",
    "membership-",
    "reminder-occurrence:",
)


def _is_local_only_envelope(message_id: str) -> bool:
    return message_id.startswith(_LOCAL_ONLY_ENVELOPE_PREFIXES)


class StatusReporter:
    """Owns the heartbeat loop and turn lifecycle hooks.

    Spawn ``run_heartbeat_loop`` once on startup. A provider admission calls
    ``begin_notice_turn``; concrete Inbox admission then calls ``begin_turn``
    and the matching terminal method.
    """

    def __init__(
        self,
        http: PuffoCoreHttpClient,
        *,
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        runtime_health_provider: Optional[Callable[[], str]] = None,
        runtime_provider: Optional[Callable[[], dict[str, Any]]] = None,
        status_sender: Optional[Callable[..., Awaitable[None]]] = None,
        processing_reports: ProcessingReportDispatcher | None = None,
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
        self._processing_reports = processing_reports
        if processing_reports is not None:
            processing_reports.set_status_refresh(self.report_current_status)
        self._run_stop: asyncio.Event | None = None

    async def run_heartbeat_loop(self) -> None:
        # Native agents POST /agents/me/heartbeat; keyless bridge agents emit a
        # status frame over the bridge (``_send_heartbeat`` routes by transport).
        # Send one immediately so a fresh agent doesn't sit
        # offline waiting for the first scheduled tick.
        if self._run_stop is not None:
            raise RuntimeError("status reporter is already running")
        stop = asyncio.Event()
        self._run_stop = stop
        replay_task = None
        if self._processing_reports is not None:
            replay_task = asyncio.create_task(self._processing_reports.run())
        try:
            await self._send_heartbeat()
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
                if stop.is_set():
                    break
                await self._send_heartbeat()
        finally:
            if self._processing_reports is not None:
                self._processing_reports.stop()
            if replay_task is not None:
                replay_task.cancel()
                try:
                    await replay_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - cleanup must release the latch
                    logger.warning("processing report replay stopped", exc_info=True)
            if self._run_stop is stop:
                self._run_stop = None

    def stop(self) -> None:
        if self._run_stop is not None:
            self._run_stop.set()
        if self._processing_reports is not None:
            self._processing_reports.stop()

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
        health: str | None = None
        if self._runtime_health_provider is not None:
            try:
                health = self._runtime_health_provider()
            except Exception as exc:  # noqa: BLE001
                logger.debug("runtime_health_provider raised (%s)", exc)
        try:
            await self._status_sender(
                status,
                current_message_id=current_message_id,
                error_text=error_text,
                runtime=self._runtime_payload(),
                health=health,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("keyless status emit (%s) failed (%s)", status, exc)

    def _runtime_payload(self) -> dict[str, Any] | None:
        if self._runtime_provider is None:
            return None
        try:
            runtime = self._runtime_provider()
        except Exception as exc:  # noqa: BLE001
            logger.debug("runtime_provider raised (%s)", exc)
            return None
        text_fields = (
            "kind", "provider", "harness", "model", "inference_level",
        )
        payload: dict[str, Any] = {
            key: str(runtime.get(key, ""))[:256]
            for key in text_fields
            if key in runtime
        }
        for key in ("max_context", "auto_compact_threshold_pct"):
            value = runtime.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                payload[key] = value
        return payload

    async def report_current_status(self) -> None:
        """Best-effort status refresh used after bridge reconnects."""
        await self._send_heartbeat()

    async def begin_notice_turn(self, message_id: str) -> None:
        """Report a provider turn before the model reads its Inbox.

        ``message_id`` is only a UI routing anchor. This method deliberately
        does not create a processing run: the message remains pending until
        ``read_inbox`` exposes it to the model and ``begin_turn`` is called.
        """
        self._current_status = "busy"
        self._current_message_id = (
            None if _is_local_only_envelope(message_id) else message_id
        )
        await self._send_heartbeat()

    async def end_notice_turn(
        self,
        *,
        succeeded: bool,
        error_text: str | None = None,
    ) -> None:
        """Settle a notice turn that never created a processing run."""
        if not succeeded:
            await self.report_error(error_text or "agent turn failed")
            return
        self._current_status = "idle"
        self._current_message_id = None
        await self._send_heartbeat()

    async def begin_turn(self, message_id: str, *, run_id: str | None = None) -> str:
        """Returns a ``run_id`` to pass back to ``end_turn``."""
        run_id = run_id or f"run_{uuid.uuid4().hex}"
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
            should_emit = (
                self._current_status != "busy"
                or self._current_message_id is not None
            )
            self._current_status = "busy"
            self._current_message_id = None
            if should_emit:
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
        if self._processing_reports is not None:
            await self._finish_processing_runs(
                ({"message_id": message_id, **body},),
                any_failed=not succeeded,
            )
            return
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
            failed = next((r for r in runs if not r["succeeded"]), None)
            self._current_status = "error" if failed is not None else "idle"
            self._current_message_id = None
            error_text = None
            if failed is not None and failed.get("error_text") is not None:
                error_text = str(failed["error_text"])[:1024]
            await self._emit_keyless(self._current_status, None, error_text)
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
        any_failed = any(not r["succeeded"] for r in runs)
        if self._processing_reports is not None:
            await self._finish_processing_runs(
                payload_runs,
                any_failed=any_failed,
            )
            return
        try:
            await self._http.post(
                "/messages/processing/end:batch", {"runs": payload_runs},
            )
            # Server flips agent_status atomically — mirror locally.
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

    async def _finish_processing_runs(
        self,
        runs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        any_failed: bool,
    ) -> None:
        self._current_status = "error" if any_failed else "idle"
        self._current_message_id = None
        try:
            result = await self._processing_reports.enqueue(runs, immediate=True)
        except Exception:  # noqa: BLE001 - status must not break the turn
            logger.warning("failed to persist processing reports", exc_info=True)
            await self._send_heartbeat()
            return
        if (
            result.state != "uploaded"
            or result.requested_pending
            or result.count != len(runs)
        ):
            # The terminal batch will eventually settle Server status. Until
            # then, or after a mixed old/current batch, re-assert the live
            # snapshot. Server rate-limits duplicate beats but accepts visible
            # status/message transitions immediately.
            await self._send_heartbeat()

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
