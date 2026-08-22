"""Durable delivery of per-message processing results to Puffo Server."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Sequence

from ..crypto.http_client import HttpError
from ._logging import log_runtime_event

logger = logging.getLogger(__name__)

PROCESSING_REPORT_PATH = "/messages/processing/end:batch"
PROCESSING_REPORT_BATCH_SIZE = 50
PROCESSING_REPORT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
PROCESSING_REPORT_RETRY_DELAYS_MS = (60_000, 300_000, 600_000)
PROCESSING_REPORT_PARKED_RETRY_MS = 60 * 60 * 1000
PROCESSING_REPORT_REQUEST_INTERVAL_S = 1.0
PROCESSING_REPORT_STARTUP_JITTER_S = 30.0

PROCESSING_REPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS processing_report_outbox (
    run_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    succeeded INTEGER NOT NULL CHECK (succeeded IN (0, 1)),
    error_text TEXT,
    created_at_ms INTEGER NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_attempt_at_ms INTEGER NOT NULL,
    isolated INTEGER NOT NULL DEFAULT 0 CHECK (isolated IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_processing_report_due
    ON processing_report_outbox (
        isolated, next_attempt_at_ms, created_at_ms, run_id
    );
"""


def processing_run_id(turn_id: str, message_id: str) -> str:
    identity = f"{turn_id}\0{message_id}".encode("utf-8")
    return f"run_{hashlib.sha256(identity).hexdigest()[:32]}"


@dataclass(frozen=True)
class ProcessingReport:
    run_id: str
    message_id: str
    succeeded: bool
    error_text: str | None
    created_at_ms: int
    retry_count: int
    next_attempt_at_ms: int
    isolated: bool

    def wire_value(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "run_id": self.run_id,
            "message_id": self.message_id,
            "succeeded": self.succeeded,
        }
        if self.error_text is not None:
            value["error_text"] = self.error_text
        return value


@dataclass(frozen=True)
class ProcessingReportResult:
    state: str
    count: int = 0
    requested_pending: bool = False


def _normalize_report(value: dict[str, Any]) -> tuple[str, str, int, str | None]:
    run_id = value["run_id"]
    message_id = value["message_id"]
    succeeded = value["succeeded"]
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 1024:
        raise ValueError("processing run_id must be 1-1024 characters")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("processing report ids must be non-empty")
    if not isinstance(succeeded, bool):
        raise ValueError("processing report succeeded must be a boolean")
    error_text = value.get("error_text")
    return (
        run_id,
        message_id,
        1 if succeeded else 0,
        None if error_text is None else str(error_text)[:1024],
    )


class ProcessingReportStoreMixin:
    """Persistence operations owned by the per-Agent ``MessageStore``."""

    async def enqueue_processing_reports(
        self, reports: Iterable[dict[str, Any]]
    ) -> None:
        normalized = tuple(_normalize_report(value) for value in reports)
        if not normalized:
            return
        expected: dict[str, tuple[str, int, str | None]] = {}
        for run_id, message_id, succeeded, error_text in normalized:
            identity = (message_id, succeeded, error_text)
            if run_id in expected and expected[run_id] != identity:
                raise ValueError(f"processing run {run_id} has conflicting results")
            expected[run_id] = identity
        async with self._inbox_lock:
            db = await self._ensure_db()
            now_ms = self._now_ms()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.executemany(
                    """INSERT INTO processing_report_outbox
                       (run_id, message_id, succeeded, error_text,
                        created_at_ms, next_attempt_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(run_id) DO UPDATE SET
                           succeeded = excluded.succeeded,
                           error_text = excluded.error_text,
                           created_at_ms = excluded.created_at_ms,
                           retry_count = 0,
                           next_attempt_at_ms = excluded.next_attempt_at_ms,
                           isolated = 0
                       WHERE processing_report_outbox.message_id = excluded.message_id
                         AND (
                             processing_report_outbox.succeeded
                                 IS NOT excluded.succeeded
                             OR processing_report_outbox.error_text
                                 IS NOT excluded.error_text
                         )""",
                    ((*value, now_ms, now_ms) for value in normalized),
                )
                placeholders = ",".join("?" for _ in expected)
                async with db.execute(
                    f"""SELECT run_id, message_id, succeeded, error_text
                        FROM processing_report_outbox
                        WHERE run_id IN ({placeholders})""",
                    tuple(expected),
                ) as rows:
                    actual = {
                        str(row["run_id"]): (
                            str(row["message_id"]),
                            int(row["succeeded"]),
                            row["error_text"],
                        )
                        for row in await rows.fetchall()
                    }
                if actual != expected:
                    raise ValueError("processing run is bound to another result")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def due_processing_reports(
        self,
        *,
        now_ms: int,
        force_all: bool = False,
    ) -> list[ProcessingReport]:
        async with self._inbox_lock:
            db = await self._ensure_db()
            await self._cleanup_processing_reports_unlocked(db, now_ms)
            due_clause = "(? = 1 OR next_attempt_at_ms <= ?)"
            params = (1 if force_all else 0, now_ms)
            async with db.execute(
                f"""SELECT run_id, message_id, succeeded, error_text,
                            created_at_ms, retry_count, next_attempt_at_ms,
                            isolated
                     FROM processing_report_outbox
                     WHERE isolated = 1 AND {due_clause}
                     ORDER BY created_at_ms, run_id LIMIT 1""",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                async with db.execute(
                    f"""SELECT run_id, message_id, succeeded, error_text,
                                created_at_ms, retry_count, next_attempt_at_ms,
                                isolated
                         FROM processing_report_outbox
                         WHERE isolated = 0 AND {due_clause}
                         ORDER BY created_at_ms, run_id LIMIT ?""",
                    (*params, PROCESSING_REPORT_BATCH_SIZE),
                ) as cursor:
                    rows = await cursor.fetchall()
            return [self._processing_report_from_row(row) for row in rows]

    async def next_processing_report_at(self) -> int | None:
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                """SELECT MIN(next_attempt_at_ms)
                   FROM processing_report_outbox"""
            ) as cursor:
                row = await cursor.fetchone()
            return None if row is None or row[0] is None else int(row[0])

    async def has_processing_reports(self, run_ids: Sequence[str]) -> bool:
        if not run_ids:
            return False
        async with self._inbox_lock:
            db = await self._ensure_db()
            placeholders = ",".join("?" for _ in run_ids)
            async with db.execute(
                f"""SELECT 1 FROM processing_report_outbox
                    WHERE run_id IN ({placeholders}) LIMIT 1""",
                tuple(run_ids),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def acknowledge_processing_reports(self, run_ids: Sequence[str]) -> int:
        if not run_ids:
            return 0
        async with self._inbox_lock:
            db = await self._ensure_db()
            placeholders = ",".join("?" for _ in run_ids)
            cursor = await db.execute(
                f"DELETE FROM processing_report_outbox WHERE run_id IN ({placeholders})",
                tuple(run_ids),
            )
            await db.commit()
            return max(cursor.rowcount, 0)

    async def retry_processing_reports(
        self,
        run_ids: Sequence[str],
        *,
        now_ms: int,
    ) -> None:
        if not run_ids:
            return
        async with self._inbox_lock:
            db = await self._ensure_db()
            placeholders = ",".join("?" for _ in run_ids)
            await db.execute(
                f"""UPDATE processing_report_outbox
                    SET next_attempt_at_ms = ? + CASE retry_count
                            WHEN 0 THEN ? WHEN 1 THEN ? WHEN 2 THEN ? ELSE ? END,
                        retry_count = retry_count + 1
                    WHERE run_id IN ({placeholders})""",
                (
                    now_ms,
                    *PROCESSING_REPORT_RETRY_DELAYS_MS,
                    PROCESSING_REPORT_PARKED_RETRY_MS,
                    *run_ids,
                ),
            )
            await db.commit()

    async def isolate_processing_reports(
        self, run_ids: Sequence[str], *, now_ms: int
    ) -> None:
        if not run_ids:
            return
        async with self._inbox_lock:
            db = await self._ensure_db()
            placeholders = ",".join("?" for _ in run_ids)
            await db.execute(
                f"""UPDATE processing_report_outbox
                    SET isolated = 1, next_attempt_at_ms = ?
                    WHERE run_id IN ({placeholders})""",
                (now_ms, *run_ids),
            )
            await db.commit()

    async def _cleanup_processing_reports_unlocked(self, db, now_ms: int) -> int:
        cursor = await db.execute(
            "DELETE FROM processing_report_outbox WHERE created_at_ms < ?",
            (now_ms - PROCESSING_REPORT_RETENTION_MS,),
        )
        await db.commit()
        return max(cursor.rowcount, 0)

    @staticmethod
    def _processing_report_from_row(row) -> ProcessingReport:
        return ProcessingReport(
            run_id=str(row["run_id"]),
            message_id=str(row["message_id"]),
            succeeded=bool(row["succeeded"]),
            error_text=row["error_text"],
            created_at_ms=int(row["created_at_ms"]),
            retry_count=int(row["retry_count"]),
            next_attempt_at_ms=int(row["next_attempt_at_ms"]),
            isolated=bool(row["isolated"]),
        )


class ProcessingReportDispatcher:
    """Serial, bounded uploader for one Agent's processing reports."""

    def __init__(
        self,
        store: Any,
        http: Any,
        *,
        now_ms: Any | None = None,
        monotonic: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
        startup_jitter: Any | None = None,
    ) -> None:
        self._store = store
        self._http = http
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._monotonic = monotonic
        self._sleep = sleep
        self._startup_jitter = startup_jitter or (
            lambda: random.uniform(0.0, PROCESSING_REPORT_STARTUP_JITTER_S)
        )
        self._wake: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._run_stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()
        self._force_all = False
        self._next_request_at = 0.0
        self._status_refresh: Callable[[], Awaitable[None]] | None = None

    async def enqueue(
        self, reports: Iterable[dict[str, Any]], *, immediate: bool = False
    ) -> ProcessingReportResult:
        values = tuple(reports)
        if not values:
            return ProcessingReportResult("idle")
        await self._store.enqueue_processing_reports(values)
        if immediate:
            result = await self.flush_once()
            requested_pending = await self._store.has_processing_reports(
                tuple(str(value["run_id"]) for value in values)
            )
            result = ProcessingReportResult(
                result.state,
                result.count,
                requested_pending,
            )
        else:
            result = ProcessingReportResult("queued", len(values))
        self._signal()
        return result

    async def on_transport_connected(self) -> None:
        self._force_all = True
        self._signal()

    def set_status_refresh(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._status_refresh = callback

    def stop(self) -> None:
        if self._run_stop is not None:
            self._run_stop.set()
        self._signal()

    async def run(self) -> None:
        if self._run_stop is not None:
            raise RuntimeError("processing report dispatcher is already running")
        stop = asyncio.Event()
        self._run_stop = stop
        try:
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=max(0.0, float(self._startup_jitter())),
                )
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                return
            self._drain_signal()
            self._force_all = True
            while not stop.is_set():
                force_all = self._force_all
                self._force_all = False
                try:
                    result = await self.flush_once(force_all=force_all)
                    if result.state == "uploaded":
                        await self._refresh_status()
                    continue_drain = result.state == "uploaded" or (
                        force_all and result.state in {"isolating", "discarded"}
                    )
                    if continue_drain and (
                        await self._store.next_processing_report_at() is not None
                    ):
                        self._force_all = True
                        # ``flush_once`` owns request pacing. Loop immediately
                        # so a reconnect drains parked batches instead of using
                        # their hour-out due time.
                        delay = 0.0
                    else:
                        delay = await self._next_delay()
                except Exception:  # noqa: BLE001 - persistence loop must survive
                    logger.warning("processing report replay failed", exc_info=True)
                    delay = PROCESSING_REPORT_RETRY_DELAYS_MS[0] / 1000
                await self._wait(delay, stop)
        finally:
            if self._run_stop is stop:
                self._run_stop = None

    async def flush_once(self, *, force_all: bool = False) -> ProcessingReportResult:
        async with self._lock:
            now_ms = self._now_ms()
            reports = await self._store.due_processing_reports(
                now_ms=now_ms,
                force_all=force_all,
            )
            if not reports:
                return ProcessingReportResult("idle")
            await self._respect_request_interval()
            return await self._send(reports, now_ms=now_ms)

    async def _send(
        self, reports: list[ProcessingReport], *, now_ms: int
    ) -> ProcessingReportResult:
        run_ids = tuple(report.run_id for report in reports)
        try:
            response = await self._http.post(
                PROCESSING_REPORT_PATH,
                {"runs": [report.wire_value() for report in reports]},
            )
        except HttpError as exc:
            return await self._handle_http_error(reports, exc, now_ms=now_ms)
        except Exception as exc:  # noqa: BLE001 - transport failures are retryable
            logger.warning(
                "processing report transport failed category=%s",
                type(exc).__name__,
            )
            return await self._retry(reports, now_ms=now_ms, error_code="transport")
        try:
            self._validate_acknowledgement(response, run_ids)
        except ValueError:
            return await self._retry(reports, now_ms=now_ms, error_code="malformed_ack")
        await self._store.acknowledge_processing_reports(run_ids)
        log_runtime_event(
            logger,
            "processing_report.acknowledged",
            message_count=len(reports),
            retry_count=max(report.retry_count for report in reports),
            outcome="acknowledged",
        )
        return ProcessingReportResult("uploaded", len(reports))

    async def _handle_http_error(
        self,
        reports: list[ProcessingReport],
        exc: HttpError,
        *,
        now_ms: int,
    ) -> ProcessingReportResult:
        run_ids = tuple(report.run_id for report in reports)
        error_code = f"http_{exc.status}"
        if exc.status in {401, 408, 425, 429} or exc.status >= 500:
            return await self._retry(reports, now_ms=now_ms, error_code=error_code)
        if len(reports) > 1:
            await self._store.isolate_processing_reports(run_ids, now_ms=now_ms)
            log_runtime_event(
                logger,
                "processing_report.isolated",
                message_count=len(reports),
                error_code=error_code,
                outcome="isolating",
            )
            return ProcessingReportResult("isolating")
        report = reports[0]
        await self._store.acknowledge_processing_reports((report.run_id,))
        log_runtime_event(
            logger,
            "processing_report.discarded",
            message_id=report.message_id,
            run_id=report.run_id,
            error_code=error_code,
            outcome="discarded",
        )
        return ProcessingReportResult("discarded", 1)

    async def _retry(
        self,
        reports: list[ProcessingReport],
        *,
        now_ms: int,
        error_code: str,
    ) -> ProcessingReportResult:
        await self._store.retry_processing_reports(
            tuple(report.run_id for report in reports),
            now_ms=now_ms,
        )
        self._log_retry(reports, error_code)
        return ProcessingReportResult("retry")

    async def _refresh_status(self) -> None:
        if self._status_refresh is None:
            return
        try:
            await self._status_refresh()
        except Exception:  # noqa: BLE001 - upload is already acknowledged
            logger.warning("processing report status refresh failed", exc_info=True)

    async def _respect_request_interval(self) -> None:
        delay = self._next_request_at - self._monotonic()
        if delay > 0:
            await self._sleep(delay)
        self._next_request_at = self._monotonic() + PROCESSING_REPORT_REQUEST_INTERVAL_S

    async def _next_delay(self) -> float:
        next_at = await self._store.next_processing_report_at()
        if next_at is None:
            return PROCESSING_REPORT_PARKED_RETRY_MS / 1000
        return max(
            PROCESSING_REPORT_REQUEST_INTERVAL_S,
            min(
                PROCESSING_REPORT_PARKED_RETRY_MS / 1000,
                (next_at - self._now_ms()) / 1000,
            ),
        )

    async def _wait(self, delay: float, stop: asyncio.Event) -> None:
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(self._wake.get(), timeout=max(0.0, delay))
        except asyncio.TimeoutError:
            return

    def _signal(self) -> None:
        if self._wake.empty():
            self._wake.put_nowait(None)

    def _drain_signal(self) -> None:
        while not self._wake.empty():
            self._wake.get_nowait()

    @staticmethod
    def _validate_acknowledgement(response: Any, expected: Sequence[str]) -> None:
        if not isinstance(response, dict) or not isinstance(response.get("runs"), list):
            raise ValueError("processing report response has no runs")
        acknowledged = [str(row.get("run_id", "")) for row in response["runs"]]
        if len(acknowledged) != len(expected) or set(acknowledged) != set(expected):
            raise ValueError(
                "processing report response does not acknowledge exact batch"
            )

    @staticmethod
    def _log_retry(reports: list[ProcessingReport], error_code: str) -> None:
        log_runtime_event(
            logger,
            "processing_report.retry",
            message_count=len(reports),
            retry_count=max(report.retry_count for report in reports) + 1,
            error_code=error_code,
            outcome="scheduled",
        )
