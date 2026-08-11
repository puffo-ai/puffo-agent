"""Bounded, restart-safe SQLite outbox for Runtime Events v1."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, TypeVar

from ._logging import log_runtime_event
from .harness.driver import HarnessEvent, HarnessEventType
from .runtime_events import (
    TURN_OUTCOMES,
    LifecycleValidator,
    RuntimeEvent,
    RuntimeEventProjector,
    safe_error,
)

APPEND_PATH = "/v2/agent-runtime/events:append"
# These responses describe the upload channel, not a specific event row.  In
# particular, a rolling Server deployment can briefly expose an authenticated
# Agent to a Server without the append endpoint.  Retain FIFO evidence until
# the operator repairs that boundary rather than treating the head as bad.
_DEGRADED_CHANNEL_HTTP_STATUSES = frozenset({401, 403, 404, 405})

_Result = TypeVar("_Result")


def _metadata_only_event(value: Any) -> RuntimeEvent | None:
    """Sanitize one pre-1.3 outbox row without retaining content fields."""
    if not isinstance(value, dict):
        return None
    if value.get("version") != 1 or value.get("scope") != {"kind": "operator"}:
        return None
    for field in ("event_id", "agent_id", "session_ref", "turn_ref", "occurred_at"):
        if not isinstance(value.get(field), str) or not value[field]:
            return None
    event_type = value.get("type")
    payload = value.get("payload")
    if not isinstance(payload, dict) or event_type == "output.updated":
        return None
    if event_type == "turn.started":
        payload = {}
    elif event_type == "activity.updated":
        payload = {"text": None if payload.get("text") is None else "Working"}
    elif event_type == "tool.updated":
        if not isinstance(payload.get("tool_call_ref"), str):
            return None
        payload = {
            "tool_call_ref": payload.get("tool_call_ref"),
            "label": "Tool",
            "state": payload.get("state"),
        }
    elif event_type == "permission.updated":
        if not isinstance(payload.get("permission_ref"), str):
            return None
        legacy_title = payload.get("title")
        payload = {
            "permission_ref": payload.get("permission_ref"),
            "state": payload.get("state"),
        }
        if legacy_title is not None:
            payload["title"] = "Permission required"
    elif event_type == "turn.finished":
        outcome = payload.get("outcome")
        if not isinstance(outcome, str) or outcome not in TURN_OUTCOMES:
            return None
        legacy_error = payload.get("error")
        payload = {"outcome": outcome}
        if outcome == "failed" and isinstance(legacy_error, dict):
            payload["error"] = safe_error(
                str(legacy_error.get("code") or "unknown"),
                retryable=legacy_error.get("retryable") is True,
            )
    else:
        return None
    try:
        return RuntimeEvent(
            version=value.get("version"),
            event_id=value.get("event_id"),
            agent_id=value.get("agent_id"),
            session_ref=value.get("session_ref"),
            turn_ref=value.get("turn_ref"),
            type=event_type,
            occurred_at=value.get("occurred_at"),
            payload=payload,
        )
    except (TypeError, ValueError):
        return None


def runtime_event_outbox_path(agent_state_dir: str | Path) -> Path:
    """Return the daemon-owned event DB beside the Agent message DB."""
    return Path(agent_state_dir) / "runtime_events.db"


class OutboxCapacityError(RuntimeError):
    pass


class _MalformedAppendResponse(Exception):
    """An append response whose body is not a decodable acknowledgement.

    ``status`` is carried whenever the HTTP framing was readable, so a 2xx with
    an empty or undecodable body is still judged by the acknowledgement
    boundary instead of being mistaken for a transport fault.
    """

    def __init__(self, status: int | None = None):
        super().__init__("append response body must be an object")
        self.status = status


@dataclass(frozen=True, slots=True)
class OutboxRow:
    sequence: int
    event_id: str
    event_type: str
    event_json: bytes
    retry_count: int

    @property
    def event(self) -> dict[str, Any]:
        return json.loads(self.event_json)


@dataclass(frozen=True, slots=True)
class UploadResult:
    state: str
    count: int = 0
    error_code: str = ""


class RuntimeEventOutbox:
    """One per Agent. All writes use immediate SQLite transactions.

    ``synchronous=FULL`` makes every commit wait on an fsync, so the DB work
    is owned by one dedicated thread rather than the caller's thread.  Async
    callers await that thread, which keeps the daemon's shared event loop
    free while the durability guarantee is unchanged.  A single owning thread
    also means the connection is never used concurrently, so SQLite
    serialization is not being relied on.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_rows: int = 10_000,
        max_bytes: int = 16 * 1024 * 1024,
        terminal_reserve_rows: int = 1,
        terminal_reserve_bytes: int = 2048,
        logger=None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.terminal_reserve_rows = terminal_reserve_rows
        self.terminal_reserve_bytes = terminal_reserve_bytes
        self.logger = logger
        self._worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="runtime-event-outbox"
        )
        self._worker_ident = 0
        self._closed = False
        try:
            self._db = self._worker.submit(self._connect).result()
        except BaseException:
            self._worker.shutdown(wait=False)
            raise
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        """Open the durable connection on the thread that will own it."""
        self._worker_ident = threading.get_ident()
        db = sqlite3.connect(self.path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE,
              event_type TEXT NOT NULL,
              event_json BLOB NOT NULL,
              byte_count INTEGER NOT NULL,
              retry_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )
        self._migrate_metadata_only_rows(db)
        db.commit()
        return db

    def _migrate_metadata_only_rows(self, db: sqlite3.Connection) -> None:
        """Prevent queued pre-1.3 content from crossing the upload boundary."""
        rows = db.execute(
            "SELECT sequence, event_type, event_json FROM events ORDER BY sequence"
        ).fetchall()
        for row in rows:
            try:
                raw = json.loads(row["event_json"])
            except (TypeError, ValueError):
                raw = None
            event = _metadata_only_event(raw)
            if event is None or event.type != row["event_type"]:
                db.execute("DELETE FROM events WHERE sequence = ?", (row["sequence"],))
                continue
            encoded = self.canonical_bytes(event)
            db.execute(
                "UPDATE events SET event_json = ?, byte_count = ? WHERE sequence = ?",
                (encoded, len(encoded), row["sequence"]),
            )

    def _call(self, work: Callable[[], _Result]) -> _Result:
        """Run DB work on the owning thread and block for its result."""
        if threading.get_ident() == self._worker_ident:
            return work()
        return self._worker.submit(work).result()

    async def _acall(self, work: Callable[[], _Result]) -> _Result:
        """Await DB work on the owning thread without blocking the loop."""
        if threading.get_ident() == self._worker_ident:
            return work()
        return await asyncio.wrap_future(self._worker.submit(work))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            # Drain queued work before closing so no job races the close.
            self._worker.submit(self._db.close).result()
        finally:
            self._worker.shutdown(wait=True)

    async def aclose(self) -> None:
        self.close()

    @staticmethod
    def canonical_bytes(event: RuntimeEvent | dict[str, Any]) -> bytes:
        value = event.as_dict() if isinstance(event, RuntimeEvent) else event
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")

    def usage(self) -> tuple[int, int]:
        return self._call(self._usage)

    def _usage(self) -> tuple[int, int]:
        row = self._db.execute(
            "SELECT COUNT(*) AS rows, COALESCE(SUM(byte_count), 0) AS bytes "
            "FROM events"
        ).fetchone()
        return int(row["rows"]), int(row["bytes"])

    def can_start_turn(self, *, estimated_start_bytes: int = 0) -> bool:
        rows, bytes_ = self.usage()
        return self._fits_turn(rows, bytes_, estimated_start_bytes)

    def _fits_turn(
        self, rows: int, bytes_: int, estimated_start_bytes: int
    ) -> bool:
        return (
            rows + 2 + self.terminal_reserve_rows <= self.max_rows
            and bytes_ + estimated_start_bytes
            + self.terminal_reserve_bytes <= self.max_bytes
        )

    def require_turn_capacity(self, *, estimated_start_bytes: int = 0) -> None:
        if not self.can_start_turn(estimated_start_bytes=estimated_start_bytes):
            self._refuse_turn()

    async def arequire_turn_capacity(
        self, *, estimated_start_bytes: int = 0
    ) -> None:
        """Enforce turn capacity without blocking the caller's event loop."""
        rows, bytes_ = await self._acall(self._usage)
        if not self._fits_turn(rows, bytes_, estimated_start_bytes):
            self._refuse_turn()

    def _refuse_turn(self) -> None:
        self._log("runtime.capacity", state="blocked", error_code="capacity")
        raise OutboxCapacityError("runtime event outbox is at capacity")

    async def enqueue(
        self, event: RuntimeEvent, *, terminal: bool | None = None
    ) -> int:
        encoded = self.canonical_bytes(event)
        is_terminal = (
            event.type == "turn.finished" if terminal is None else terminal
        )
        async with self._lock:
            # The lock is acquired in call order, so submission order decides
            # sequence order; capacity check, INSERT and commit are one job so
            # no other writer can slip between them.
            return await self._acall(
                lambda: self._insert(event, encoded, is_terminal)
            )

    def _insert(
        self, event: RuntimeEvent, encoded: bytes, is_terminal: bool
    ) -> int:
        rows, bytes_ = self._usage()
        row_limit = self.max_rows
        byte_limit = self.max_bytes
        if not is_terminal:
            row_limit -= self.terminal_reserve_rows
            byte_limit -= self.terminal_reserve_bytes
        if rows + 1 > row_limit or bytes_ + len(encoded) > byte_limit:
            self._log(
                "runtime.capacity", state="blocked",
                event_id=event.event_id, event_type=event.type,
                error_code="capacity",
            )
            raise OutboxCapacityError(
                "enqueue would exceed runtime event outbox capacity"
            )
        try:
            cursor = self._db.execute(
                "INSERT INTO events"
                "(event_id,event_type,event_json,byte_count) VALUES(?,?,?,?)",
                (event.event_id, event.type, encoded, len(encoded)),
            )
            self._db.commit()
        except sqlite3.IntegrityError:
            row = self._db.execute(
                "SELECT sequence,event_json FROM events WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            if row is None or bytes(row["event_json"]) != encoded:
                raise ValueError("event_id reused with different content")
            return int(row["sequence"])
        sequence = int(cursor.lastrowid)
        self._log(
            "runtime.enqueued", event_id=event.event_id,
            event_type=event.type, outbox_sequence=sequence,
            agent_id=event.agent_id, session_ref=event.session_ref,
            turn_ref=event.turn_ref,
        )
        return sequence

    def prefix(
        self, *, max_rows: int = 50, max_bytes: int = 256 * 1024
    ) -> list[OutboxRow]:
        return self._call(lambda: self._prefix(max_rows, max_bytes))

    def _prefix(self, max_rows: int, max_bytes: int) -> list[OutboxRow]:
        result: list[OutboxRow] = []
        # Account for the complete JSON envelope, including wrapper and commas.
        total = len(b'{"events":[]}')
        for row in self._db.execute(
            "SELECT sequence,event_id,event_type,event_json,retry_count "
            "FROM events ORDER BY sequence LIMIT ?", (max_rows,)
        ):
            encoded = bytes(row["event_json"])
            added = len(encoded) + (1 if result else 0)
            if result and total + added > max_bytes:
                break
            if not result and total + added > max_bytes:
                # Return the oversized head alone so the uploader can
                # deterministically quarantine it and unblock later rows.
                result.append(OutboxRow(int(row["sequence"]), str(row["event_id"]), str(row["event_type"]), encoded, int(row["retry_count"])))
                break
            result.append(OutboxRow(
                int(row["sequence"]), str(row["event_id"]),
                str(row["event_type"]), encoded, int(row["retry_count"]),
            ))
            total += added
        return result

    def increment_retries(self, sequences: Iterable[int]) -> None:
        values = tuple(int(value) for value in sequences)
        self._call(lambda: self._increment_retries(values))

    def _increment_retries(self, values: tuple[int, ...]) -> None:
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        self._db.execute(
            f"UPDATE events SET retry_count=retry_count+1 "
            f"WHERE sequence IN ({placeholders})", values,
        )
        self._db.commit()

    def acknowledge(self, rows: list[OutboxRow], accepted_ids: list[str]) -> None:
        if accepted_ids != [row.event_id for row in rows]:
            raise ValueError("append response does not exactly match batch")
        if not rows:
            return
        # Verify and delete in one job so the batch cannot change in between.
        self._call(lambda: self._acknowledge(rows, accepted_ids))

    def _acknowledge(
        self, rows: list[OutboxRow], accepted_ids: list[str]
    ) -> None:
        first, last = rows[0].sequence, rows[-1].sequence
        actual = [
            str(row["event_id"]) for row in self._db.execute(
                "SELECT event_id FROM events WHERE sequence BETWEEN ? AND ? "
                "ORDER BY sequence", (first, last)
            )
        ]
        if actual != accepted_ids:
            raise ValueError("outbox changed while acknowledging batch")
        self._db.execute(
            "DELETE FROM events WHERE sequence BETWEEN ? AND ?", (first, last)
        )
        self._db.commit()

    def discard(self, row: OutboxRow, *, error_code: str) -> None:
        """Quarantine a permanently rejected head by removing it from FIFO."""
        self._call(lambda: self._discard(row.sequence))
        self._log("runtime.discarded", outbox_sequence=row.sequence,
                  event_id=row.event_id, error_code=error_code)

    def _discard(self, sequence: int) -> None:
        self._db.execute("DELETE FROM events WHERE sequence = ?", (sequence,))
        self._db.commit()

    def set_active_turn(
        self, turn_ref: str | None, *, session_ref: str = "",
        native_session_id: str = "",
        session_fingerprint: str | None = None,
    ) -> None:
        self._call(
            lambda: self._set_active_turn(
                turn_ref, session_ref, native_session_id,
                session_fingerprint,
            )
        )

    async def aset_active_turn(
        self, turn_ref: str | None, *, session_ref: str = "",
        native_session_id: str = "",
        session_fingerprint: str | None = None,
    ) -> None:
        """Commit the active turn without blocking the caller's event loop."""
        await self._acall(
            lambda: self._set_active_turn(
                turn_ref, session_ref, native_session_id,
                session_fingerprint,
            )
        )

    def _set_active_turn(
        self,
        turn_ref: str | None,
        session_ref: str,
        native_session_id: str,
        session_fingerprint: str | None,
    ) -> None:
        values = {
            "active_turn_ref": turn_ref or "",
            "session_ref": session_ref,
            "native_session_id": native_session_id,
        }
        if session_fingerprint is not None:
            values["session_fingerprint"] = session_fingerprint
        with self._db:
            for key, value in values.items():
                self._db.execute(
                    "INSERT INTO state(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    def state(self) -> dict[str, str]:
        return self._call(self._state)

    async def astate(self) -> dict[str, str]:
        """Read persisted state without blocking the caller's event loop."""
        return await self._acall(self._state)

    def _state(self) -> dict[str, str]:
        return {
            str(row["key"]): str(row["value"])
            for row in self._db.execute("SELECT key,value FROM state")
        }

    def _log(self, event: str, **fields: Any) -> None:
        if self.logger is not None:
            log_runtime_event(self.logger, event, **fields)


class RuntimeEventUploader:
    """Serial uploader which lets a permanent bad head unblock later rows."""

    def __init__(
        self,
        outbox: RuntimeEventOutbox,
        transport: Callable[..., Awaitable[Any]],
        *,
        batch_rows: int = 50,
        batch_bytes: int = 256 * 1024,
    ):
        self.outbox = outbox
        self.transport = transport
        self.batch_rows = batch_rows
        self.batch_bytes = batch_bytes
        self.degraded_error = ""
        self._lock = asyncio.Lock()
        self._isolate_head = False

    async def _increment_retries(self, rows: list[OutboxRow]) -> None:
        sequences = tuple(row.sequence for row in rows)
        await self.outbox._acall(
            lambda: self.outbox.increment_retries(sequences)
        )

    async def _discard_head(self, row: OutboxRow) -> None:
        error_code = self.degraded_error
        await self.outbox._acall(
            lambda: self.outbox.discard(row, error_code=error_code)
        )

    async def _reject_acknowledgement(
        self, rows: list[OutboxRow], status: int | None
    ) -> UploadResult:
        """Refuse to read an unvalidated response as an acknowledgement.

        HTTP status alone never authorises deleting durable evidence: a 2xx
        whose body does not acknowledge this exact batch degrades the channel
        with every row retained, and a body that cannot even be attributed to
        a status is retried.
        """
        if status is not None and 200 <= status < 300:
            self.degraded_error = "malformed_response"
            self.outbox._log(
                "runtime.retry", outbox_sequence=rows[0].sequence,
                retry_count=rows[0].retry_count,
                state="degraded", error_code=self.degraded_error,
            )
            return UploadResult("degraded", error_code=self.degraded_error)
        await self._increment_retries(rows)
        self.outbox._log(
            "runtime.retry", outbox_sequence=rows[0].sequence,
            retry_count=rows[0].retry_count + 1,
            error_code="malformed_response",
        )
        return UploadResult("retry", error_code="malformed_response")

    async def _isolate_or_discard(
        self, rows: list[OutboxRow], error_code: str
    ) -> UploadResult:
        """Reject a batch by retrying its head alone before discarding it.

        A batch rejection does not identify its bad member, so FIFO evidence
        is retained until a singleton attempt names the head.
        """
        self.degraded_error = error_code
        if len(rows) > 1:
            self._isolate_head = True
            return UploadResult("retry", error_code=error_code)
        self.outbox._log(
            "runtime.retry", outbox_sequence=rows[0].sequence,
            retry_count=rows[0].retry_count,
            state="degraded", error_code=error_code,
        )
        await self._discard_head(rows[0])
        self._isolate_head = False
        return UploadResult("discarded", error_code=error_code)

    async def upload_once(self) -> UploadResult:
        async with self._lock:
            batch_rows = 1 if self._isolate_head else self.batch_rows
            rows = await self.outbox._acall(
                lambda: self.outbox.prefix(
                    max_rows=batch_rows, max_bytes=self.batch_bytes
                )
            )
            if not rows:
                return UploadResult("idle")
            body = b'{"events":[' + b",".join(
                row.event_json for row in rows
            ) + b"]}"
            # An oversized singleton is quarantined locally; it must never be
            # sent as an over-limit HTTP request.
            if len(body) > self.batch_bytes:
                self.degraded_error = "body_too_large"
                await self._discard_head(rows[0])
                return UploadResult("discarded", error_code=self.degraded_error)
            self.outbox._log(
                "runtime.batch_attempt",
                first_sequence=rows[0].sequence,
                last_sequence=rows[-1].sequence,
                retry_count=max(row.retry_count for row in rows),
                event_count=len(rows),
            )
            try:
                response = self.transport(APPEND_PATH, body)
                if inspect.isawaitable(response):
                    response = await response
                status, payload = await _decode_response(response)
            except _MalformedAppendResponse as exc:
                return await self._reject_acknowledgement(rows, exc.status)
            except Exception:
                await self._increment_retries(rows)
                self.outbox._log(
                    "runtime.retry", outbox_sequence=rows[0].sequence,
                    retry_count=rows[0].retry_count + 1,
                    error_code="transport",
                )
                return UploadResult("retry", error_code="transport")
            if status == 429 or status >= 500:
                await self._increment_retries(rows)
                self.outbox._log(
                    "runtime.retry", outbox_sequence=rows[0].sequence,
                    retry_count=rows[0].retry_count + 1,
                    error_code=f"http_{status}",
                )
                return UploadResult("retry", error_code=f"http_{status}")
            if status in _DEGRADED_CHANNEL_HTTP_STATUSES:
                self.degraded_error = f"http_{status}"
                self.outbox._log(
                    "runtime.retry", outbox_sequence=rows[0].sequence,
                    retry_count=rows[0].retry_count,
                    state="degraded", error_code=self.degraded_error,
                )
                return UploadResult("degraded", error_code=self.degraded_error)
            if status < 200 or status >= 300:
                return await self._isolate_or_discard(rows, f"http_{status}")
            try:
                accepted = payload["accepted"]
                accepted_ids = [str(item["event_id"]) for item in accepted]
                if len(accepted_ids) != len(rows):
                    raise ValueError("partial acknowledgement")
                await self.outbox._acall(
                    lambda: self.outbox.acknowledge(rows, accepted_ids)
                )
            except (KeyError, TypeError, ValueError):
                return await self._reject_acknowledgement(rows, status)
            self.outbox._log(
                "runtime.acknowledged", first_sequence=rows[0].sequence,
                last_sequence=rows[-1].sequence, event_count=len(rows),
            )
            self.degraded_error = ""
            self._isolate_head = False
            return UploadResult("uploaded", len(rows))


class RuntimeEventProjectingSink:
    """Durably project metadata from the Runtime Manager's event stream.

    Assistant output, reasoning, tool content, and provider-native payloads are
    deliberately not projected into this remotely uploaded outbox.
    """

    def __init__(
        self,
        outbox: RuntimeEventOutbox,
        projector: RuntimeEventProjector,
    ):
        self.outbox = outbox
        self.projector = projector
        self.validator = LifecycleValidator()

    async def __call__(self, event: HarnessEvent) -> None:
        kind = (
            event.type.value
            if isinstance(event.type, HarnessEventType)
            else str(event.type)
        )
        self.outbox._log(
            "runtime.normalized_event",
            agent_id=self.projector.agent_id,
            session_ref=self.projector.session_ref,
            turn_ref=str(event.turn_ref) if event.turn_ref is not None else "",
            event_type=kind,
        )
        await self._project(event)

    async def _project(self, event: HarnessEvent) -> None:
        for projected in self.projector.project_all(event):
            self.outbox._log(
                "runtime.projected",
                agent_id=projected.agent_id,
                session_ref=projected.session_ref,
                turn_ref=projected.turn_ref,
                event_id=projected.event_id,
                event_type=projected.type,
            )
            self.validator.accept(projected)
            await self.outbox.enqueue(
                projected, terminal=projected.type == "turn.finished"
            )


def _readable_status(status: Any) -> int | None:
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


async def _decode_response(response: Any) -> tuple[int, dict[str, Any]]:
    if isinstance(response, tuple) and len(response) == 2:
        status, payload = response
    elif isinstance(response, dict) and "status" in response:
        status, payload = response["status"], response.get("body", {})
    else:
        status = response.status
        payload = response.json()
        if inspect.isawaitable(payload):
            payload = await payload
    code = _readable_status(status)
    if isinstance(payload, (bytes, bytearray, str)):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise _MalformedAppendResponse(code) from exc
    if not isinstance(payload, dict):
        raise _MalformedAppendResponse(code)
    return int(status), payload
