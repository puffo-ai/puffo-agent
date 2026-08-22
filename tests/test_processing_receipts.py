from __future__ import annotations

import asyncio
from typing import Any

import pytest

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.processing_receipts import (
    PROCESSING_REPORT_RETENTION_MS,
    ProcessingReportDispatcher,
)
from puffo_agent.crypto.http_client import HttpError


class _Clock:
    def __init__(self, now_ms: int = 1_000_000) -> None:
        self.now_ms = now_ms
        self._monotonic = 0.0

    def wall(self) -> int:
        return self.now_ms

    def monotonic(self) -> float:
        self._monotonic += 2.0
        return self._monotonic

    async def sleep(self, delay: float) -> None:
        self._monotonic += max(0.0, delay)


class _Http:
    def __init__(self, behavior=None) -> None:
        self.behavior = behavior
        self.calls: list[list[dict[str, Any]]] = []

    async def post(self, _path: str, body: dict[str, Any]) -> dict[str, Any]:
        runs = body["runs"]
        self.calls.append(runs)
        if self.behavior is not None:
            await self.behavior(runs)
        return {"runs": [{"run_id": run["run_id"]} for run in runs]}


def _runs(count: int, *, prefix: str = "run") -> list[dict[str, Any]]:
    return [
        {
            "run_id": f"{prefix}_{index:03}",
            "message_id": f"msg_{index:03}",
            "succeeded": True,
        }
        for index in range(count)
    ]


def _dispatcher(store: MessageStore, http: _Http, clock: _Clock):
    return ProcessingReportDispatcher(
        store,
        http,
        now_ms=clock.wall,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        startup_jitter=lambda: 0,
    )


@pytest.mark.asyncio
async def test_retry_schedule_survives_store_reopen(tmp_path):
    clock = _Clock()
    attempts = 0

    async def offline(_runs):
        nonlocal attempts
        attempts += 1
        if attempts <= 4:
            raise RuntimeError("offline")

    db_path = tmp_path / "messages.db"
    first_store = MessageStore(db_path, now_ms=clock.wall)
    http = _Http(offline)
    result = await _dispatcher(first_store, http, clock).enqueue(
        _runs(1), immediate=True
    )
    assert result.state == "retry"
    await first_store.close()

    reopened = MessageStore(db_path, now_ms=clock.wall)
    queued = await reopened.due_processing_reports(now_ms=clock.wall(), force_all=True)
    assert queued[0].retry_count == 1
    assert queued[0].next_attempt_at_ms == clock.wall() + 60_000

    dispatcher = _dispatcher(reopened, http, clock)
    assert (await dispatcher.flush_once(force_all=True)).state == "retry"
    queued = await reopened.due_processing_reports(now_ms=clock.wall(), force_all=True)
    assert queued[0].retry_count == 2
    assert queued[0].next_attempt_at_ms == clock.wall() + 300_000

    clock.now_ms += 300_000
    assert (await dispatcher.flush_once()).state == "retry"
    clock.now_ms += 600_000
    assert (await dispatcher.flush_once()).state == "retry"
    queued = await reopened.due_processing_reports(now_ms=clock.wall(), force_all=True)
    assert queued[0].retry_count == 4
    assert queued[0].next_attempt_at_ms == clock.wall() + 3_600_000

    clock.now_ms += 3_600_000
    assert (await dispatcher.flush_once()).state == "uploaded"
    assert await reopened.next_processing_report_at() is None

    replacement = _runs(1, prefix="replacement")[0]
    await reopened.enqueue_processing_reports((replacement,))
    await reopened.retry_processing_reports(
        (replacement["run_id"],), now_ms=clock.wall()
    )
    replacement["succeeded"] = False
    replacement["error_text"] = "replacement result"
    await reopened.enqueue_processing_reports((replacement,))
    replaced = await reopened.due_processing_reports(
        now_ms=clock.wall(), force_all=True
    )
    assert replaced[0].succeeded is False
    assert replaced[0].retry_count == 0
    await reopened.acknowledge_processing_reports((replacement["run_id"],))

    await dispatcher.enqueue(_runs(1, prefix="expired"))
    clock.now_ms += PROCESSING_REPORT_RETENTION_MS + 1
    assert await reopened.due_processing_reports(now_ms=clock.wall()) == []
    await reopened.close()


@pytest.mark.asyncio
async def test_dispatcher_caps_each_request_at_fifty(tmp_path):
    clock = _Clock()
    store = MessageStore(tmp_path / "messages.db", now_ms=clock.wall)
    http = _Http()
    dispatcher = _dispatcher(store, http, clock)

    await dispatcher.enqueue(_runs(50))
    clock.now_ms += 1
    immediate = await dispatcher.enqueue(_runs(1, prefix="current"), immediate=True)
    assert immediate.count == 50
    assert immediate.requested_pending is True
    assert (await dispatcher.flush_once()).count == 1
    assert [len(call) for call in http.calls] == [50, 1]

    parked = _runs(51, prefix="parked")
    await dispatcher.enqueue(parked)
    await store.retry_processing_reports(
        tuple(run["run_id"] for run in parked), now_ms=clock.wall()
    )
    task = asyncio.create_task(dispatcher.run())
    async with asyncio.timeout(2):
        while len(http.calls) < 4:
            await asyncio.sleep(0)
    dispatcher.stop()
    await task
    assert [len(call) for call in http.calls[-2:]] == [50, 1]

    # ws-local reuses its reporter across attach cycles. A stopped dispatcher
    # must therefore accept a later run instead of latching permanently off.
    parked_again = _runs(1, prefix="reattach")
    await dispatcher.enqueue(parked_again)
    await store.retry_processing_reports(
        (parked_again[0]["run_id"],), now_ms=clock.wall()
    )
    task = asyncio.create_task(dispatcher.run())
    async with asyncio.timeout(2):
        while len(http.calls) < 5:
            await asyncio.sleep(0)
    dispatcher.stop()
    await task
    assert len(http.calls[-1]) == 1
    await store.close()


@pytest.mark.asyncio
async def test_permanent_batch_error_isolates_poison_report(tmp_path):
    clock = _Clock()

    async def reject_bad(runs):
        if len(runs) > 1 or runs[0]["message_id"] == "msg_bad":
            raise HttpError(404, "message not found")

    store = MessageStore(tmp_path / "messages.db", now_ms=clock.wall)
    http = _Http(reject_bad)
    dispatcher = _dispatcher(store, http, clock)
    reports = [
        {"run_id": "run_a", "message_id": "msg_bad", "succeeded": True},
        {"run_id": "run_b", "message_id": "msg_ok_1", "succeeded": True},
        {"run_id": "run_c", "message_id": "msg_ok_2", "succeeded": True},
    ]

    await dispatcher.enqueue(reports)
    assert (await dispatcher.flush_once()).state == "isolating"
    assert (await dispatcher.flush_once()).state == "discarded"
    assert (await dispatcher.flush_once()).state == "uploaded"
    assert (await dispatcher.flush_once()).state == "uploaded"
    assert await store.next_processing_report_at() is None
    assert [len(call) for call in http.calls] == [3, 1, 1, 1]
    await store.close()
