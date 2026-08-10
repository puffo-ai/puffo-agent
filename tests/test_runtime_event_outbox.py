from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from puffo_agent.agent.runtime_event_outbox import (
    APPEND_PATH,
    OutboxCapacityError,
    RuntimeEventOutbox,
    RuntimeEventProjectingSink,
    RuntimeEventUploader,
    UploadResult,
    runtime_event_outbox_path,
)
from puffo_agent.agent.harness.driver import HarnessEvent, SessionRef, TurnRef
from puffo_agent.agent.runtime_events import RuntimeEvent, RuntimeEventProjector
from puffo_agent.portal import worker_run as worker_run_module
from puffo_agent.portal.worker_run import StandardWorkerRun


def event(index: int, type_: str = "turn.started") -> RuntimeEvent:
    return RuntimeEvent(
        agent_id="agent", session_ref="session", turn_ref=f"turn_{index}",
        type=type_, payload={} if type_ == "turn.started" else {
            "outcome": "succeeded"
        },
        event_id=f"evt_{index}_{type_}",
    )


@pytest.mark.asyncio
async def test_restart_retry_order_and_append(tmp_path):
    path = tmp_path / "runtime_events.db"
    first = RuntimeEventOutbox(path)
    await first.enqueue(event(1))
    first.close()
    second = RuntimeEventOutbox(path)
    attempts = []

    async def transport(path, body):
        attempts.append((path, body))
        if len(attempts) == 1:
            raise OSError("lost response")
        ids = [value["event_id"] for value in json.loads(body)["events"]]
        return 200, {"accepted": [
            {"event_id": value, "cursor": value} for value in ids
        ]}

    uploader = RuntimeEventUploader(second, transport)
    assert (await uploader.upload_once()).state == "retry"
    assert second.prefix()[0].retry_count == 1
    assert (await uploader.upload_once()).state == "uploaded"
    assert attempts[0] == attempts[1]
    assert attempts[0][0] == APPEND_PATH
    assert second.prefix() == []
    second.close()


@pytest.mark.asyncio
async def test_duplicate_event_id_is_idempotent_only_for_exact_content(tmp_path):
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    original = event(1)
    first = await outbox.enqueue(original)
    second = await outbox.enqueue(original)
    assert second == first
    assert outbox.usage()[0] == 1
    with pytest.raises(ValueError, match="different content"):
        await outbox.enqueue(RuntimeEvent(
            agent_id="agent",
            session_ref="session",
            turn_ref="different-turn",
            type="turn.started",
            payload={},
            event_id=original.event_id,
        ))
    outbox.close()


@pytest.mark.asyncio
async def test_successful_exact_prefix_acknowledgement_leaves_suffix(tmp_path):
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    await outbox.enqueue(event(1))
    await outbox.enqueue(event(2))
    prefix = outbox.prefix(max_rows=1)
    outbox.acknowledge(prefix, [prefix[0].event_id])
    remaining = outbox.prefix()
    assert [row.event_id for row in remaining] == [event(2).event_id]
    outbox.close()


@pytest.mark.asyncio
async def test_capacity_reserves_terminal_and_backpressures(tmp_path):
    outbox = RuntimeEventOutbox(
        tmp_path / "runtime_events.db", max_rows=2,
        terminal_reserve_rows=1, terminal_reserve_bytes=0,
    )
    await outbox.enqueue(event(1))
    assert not outbox.can_start_turn()
    with pytest.raises(OutboxCapacityError):
        await outbox.enqueue(event(2))
    await outbox.enqueue(event(1, "turn.finished"), terminal=True)
    assert outbox.usage()[0] == 2
    outbox.close()


def _initial_pair(*, occurred_at="9999-12-31T23:59:59.999999+00:00"):
    projector = RuntimeEventProjector(agent_id="agent", session_ref="session")
    return projector.project_all(HarnessEvent(
        type="turn.started", driver="fake", session_ref=SessionRef("native"),
        turn_ref=TurnRef("turn_" + "f" * 32), occurred_at=occurred_at,
    ))


@pytest.mark.asyncio
async def test_turn_capacity_requires_complete_initial_pair_and_terminal(tmp_path):
    start, activity = _initial_pair()
    terminal = event(1, "turn.finished")
    start_bytes = len(RuntimeEventOutbox.canonical_bytes(start))
    pair_bytes = sum(len(RuntimeEventOutbox.canonical_bytes(value)) for value in (start, activity))
    terminal_bytes = len(RuntimeEventOutbox.canonical_bytes(terminal))

    exact = RuntimeEventOutbox(
        tmp_path / "exact.db", max_rows=3,
        max_bytes=pair_bytes + terminal_bytes, terminal_reserve_bytes=terminal_bytes,
    )
    assert exact.can_start_turn(estimated_start_bytes=pair_bytes)
    await exact.arequire_turn_capacity(estimated_start_bytes=pair_bytes)
    await exact.enqueue(start)
    await exact.enqueue(activity)
    await exact.enqueue(terminal, terminal=True)
    assert [row.event_type for row in exact.prefix()] == [
        "turn.started", "activity.updated", "turn.finished",
    ]
    exact.close()

    # This is sufficient for the superseded one-row calculation, but is one
    # byte short of the lifecycle pair required by the public contract.
    short = RuntimeEventOutbox(
        tmp_path / "short.db", max_rows=3,
        max_bytes=pair_bytes + terminal_bytes - 1,
        terminal_reserve_bytes=terminal_bytes,
    )
    assert start_bytes + terminal_bytes <= short.max_bytes
    assert not short.can_start_turn(estimated_start_bytes=pair_bytes)
    with pytest.raises(OutboxCapacityError):
        short.require_turn_capacity(estimated_start_bytes=pair_bytes)
    # The hook the daemon loop awaits must refuse identically while reading
    # usage off the loop thread, so admission never blocks on SQLite.
    reading_threads: set[int] = set()
    short._db.set_trace_callback(
        lambda _statement: reading_threads.add(threading.get_ident())
    )
    with pytest.raises(OutboxCapacityError):
        await short.arequire_turn_capacity(estimated_start_bytes=pair_bytes)
    short._db.set_trace_callback(None)
    assert reading_threads and threading.get_ident() not in reading_threads
    assert short.usage() == (0, 0)
    await short.enqueue(terminal, terminal=True)
    assert [row.event_type for row in short.prefix()] == ["turn.finished"]
    short.close()


def test_maximum_timestamp_initial_estimate_covers_zero_microsecond_form():
    maximum = sum(
        len(RuntimeEventOutbox.canonical_bytes(value)) for value in _initial_pair()
    )
    zero_microseconds = sum(
        len(RuntimeEventOutbox.canonical_bytes(value))
        for value in _initial_pair(occurred_at="2026-07-30T12:00:00+00:00")
    )
    assert maximum >= zero_microseconds


@pytest.mark.asyncio
async def test_buffered_output_pressure_discards_nonterminal_but_keeps_terminal(
    tmp_path,
):
    outbox = RuntimeEventOutbox(
        tmp_path / "runtime_events.db", max_rows=3,
        terminal_reserve_rows=1, terminal_reserve_bytes=0,
    )
    sink = RuntimeEventProjectingSink(
        outbox,
        RuntimeEventProjector(agent_id="agent", session_ref="session"),
        delta_bytes=1024,
    )

    def native(type_, data=None):
        return HarnessEvent(
            type=type_, driver="fake", session_ref=SessionRef("native"),
            turn_ref=TurnRef("turn"), data=data or {},
        )

    await sink(native("turn.started"))
    await sink(native(
        "turn.assistant_delta", {"text": "buffered", "block_id": "result"}
    ))
    await sink(native("turn.completed", {"outcome": "succeeded"}))
    values = [row.event for row in outbox.prefix()]
    assert [value["type"] for value in values] == [
        "turn.started", "activity.updated", "turn.finished"
    ]
    assert values[-1]["payload"] == {"outcome": "succeeded"}
    outbox.close()


def test_location_is_daemon_agent_state_beside_messages_not_workspace(tmp_path):
    agent_state = tmp_path / "daemon-state" / "agents" / "agent"
    workspace = tmp_path / "user-workspace"
    messages = agent_state / "messages.db"
    path = runtime_event_outbox_path(agent_state)
    assert path == messages.with_name("runtime_events.db")
    assert workspace not in path.parents
    outbox = RuntimeEventOutbox(path)
    assert outbox.path == path
    outbox.close()


@pytest.mark.asyncio
async def test_byte_bound_and_terminal_capacity_reservation(tmp_path):
    start = event(1)
    terminal = event(1, "turn.finished")
    start_bytes = len(RuntimeEventOutbox.canonical_bytes(start))
    terminal_bytes = len(RuntimeEventOutbox.canonical_bytes(terminal))
    outbox = RuntimeEventOutbox(
        tmp_path / "runtime_events.db",
        max_rows=3,
        max_bytes=start_bytes + terminal_bytes,
        terminal_reserve_rows=1,
        terminal_reserve_bytes=terminal_bytes,
    )
    assert outbox.can_start_turn(estimated_start_bytes=start_bytes)
    await outbox.enqueue(start)
    assert outbox.usage() == (1, start_bytes)
    with pytest.raises(OutboxCapacityError):
        await outbox.enqueue(event(2))
    await outbox.enqueue(terminal, terminal=True)
    rows, bytes_ = outbox.usage()
    assert rows == 2
    assert bytes_ <= outbox.max_bytes
    outbox.close()


@pytest.mark.asyncio
async def test_coalescing_sink_projects_normalized_lifecycle(tmp_path):
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    sink = RuntimeEventProjectingSink(
        outbox,
        RuntimeEventProjector(agent_id="agent", session_ref="session"),
        delta_bytes=4,
    )

    def native(type_, data=None):
        return HarnessEvent(
            type=type_, driver="fake", session_ref=SessionRef("native"),
            turn_ref=TurnRef("turn"), data=data or {},
        )

    await sink(native("turn.started"))
    for token in "abcdefghij":
        await sink(native(
            "turn.assistant_delta", {"text": token, "block_id": "result"}
        ))
    await sink(native(
        "turn.assistant_completed", {"block_id": "result"}
    ))
    await sink(native("turn.completed", {"outcome": "succeeded"}))
    values = [row.event for row in outbox.prefix()]
    deltas = [
        item["payload"]["delta"] for item in values
        if item["type"] == "output.updated"
        and item["payload"]["phase"] == "delta"
    ]
    assert "".join(deltas) == "abcdefghij"
    assert len(deltas) < 10
    assert values[0]["type"] == "turn.started"
    assert values[-1] == {
        **values[-1],
        "type": "turn.finished",
        "payload": {"outcome": "succeeded"},
    }
    outbox.close()


@pytest.mark.asyncio
async def test_order_uses_sequence_not_occurred_at(tmp_path):
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    later_clock = RuntimeEvent(
        agent_id="agent", session_ref="session", turn_ref="turn",
        type="turn.started", payload={}, event_id="evt_inserted_first",
        occurred_at="2099-01-01T00:00:00Z",
    )
    earlier_clock = RuntimeEvent(
        agent_id="agent", session_ref="session", turn_ref="turn",
        type="turn.finished", payload={"outcome": "succeeded"},
        event_id="evt_inserted_second", occurred_at="2000-01-01T00:00:00Z",
    )
    await outbox.enqueue(later_clock)
    await outbox.enqueue(earlier_clock, terminal=True)
    assert [row.event_id for row in outbox.prefix()] == [
        "evt_inserted_first", "evt_inserted_second",
    ]
    outbox.close()


@pytest.mark.asyncio
async def test_malformed_2xx_body_retains_fifo_instead_of_acknowledging(tmp_path):
    """A 2xx whose body does not name this batch is not an acknowledgement.

    HTTP status alone must never authorise deleting durable evidence, so every
    row survives repeated attempts and the channel degrades under a named code
    until a validated acknowledgement arrives.
    """
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    await outbox.enqueue(event(1))
    await outbox.enqueue(event(2))

    async def malformed(_path, _body):
        return 200, {"accepted": [{"event_id": "evt_2"}]}

    uploader = RuntimeEventUploader(outbox, malformed)
    for _ in range(2):
        result = await uploader.upload_once()
        assert result.state == "degraded"
        assert result.error_code == "malformed_response"
    assert [row.event_id for row in outbox.prefix()] == [
        "evt_1_turn.started", "evt_2_turn.started",
    ]
    accepted = []

    async def accept(_path, body):
        accepted.extend(value["event_id"] for value in json.loads(body)["events"])
        return 200, {"accepted": [{"event_id": value} for value in accepted]}

    assert (await RuntimeEventUploader(outbox, accept).upload_once()).state == (
        "uploaded"
    )
    assert accepted == ["evt_1_turn.started", "evt_2_turn.started"]
    assert outbox.prefix() == []
    outbox.close()


@pytest.mark.asyncio
async def test_empty_2xx_body_is_retained_under_the_same_named_code(tmp_path):
    """An undecodable 2xx body is the same fault class as a malformed one.

    The empty body of ``worker_run``'s ``HttpError`` translation must not be
    read as consent to drop the batch, and it must not hide behind the generic
    transport code.
    """
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    await outbox.enqueue(event(1))

    async def empty(_path, _body):
        return 200, ""

    uploader = RuntimeEventUploader(outbox, empty)
    for _ in range(2):
        result = await uploader.upload_once()
        assert result.state == "degraded"
        assert result.error_code == "malformed_response"
    assert [row.event_id for row in outbox.prefix()] == ["evt_1_turn.started"]
    outbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 405])
async def test_channel_level_http_failures_preserve_fifo_and_degrade(tmp_path, status):
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    await outbox.enqueue(event(1))
    await outbox.enqueue(event(2))
    attempted = []

    async def unavailable(_path, body):
        attempted.append([item["event_id"] for item in json.loads(body)["events"]])
        return status, {}

    result = await RuntimeEventUploader(outbox, unavailable).upload_once()

    assert result.state == "degraded"
    assert result.error_code == f"http_{status}"
    assert attempted == [["evt_1_turn.started", "evt_2_turn.started"]]
    assert [row.event_id for row in outbox.prefix()] == [
        "evt_1_turn.started", "evt_2_turn.started",
    ]
    assert [row.retry_count for row in outbox.prefix()] == [0, 0]
    outbox.close()


@pytest.mark.asyncio
async def test_worker_retries_degraded_event_channel_until_recovery(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        worker_run_module, "RUNTIME_EVENT_DEGRADED_RETRY_SECONDS", 0.0
    )
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    await outbox.enqueue(event(1))
    stop = asyncio.Event()
    attempts = 0

    async def recovering_transport(_path, body):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return 404, {}
        stop.set()
        accepted = [
            {"event_id": item["event_id"], "cursor": item["event_id"]}
            for item in json.loads(body)["events"]
        ]
        return 200, {"accepted": accepted}

    uploader = RuntimeEventUploader(outbox, recovering_transport)
    worker = SimpleNamespace(_stop=stop)

    await StandardWorkerRun(worker)._upload_runtime_events(uploader)

    assert attempts == 2
    assert uploader.degraded_error == ""
    assert outbox.prefix() == []
    outbox.close()


@pytest.mark.asyncio
async def test_upload_loop_survives_local_store_failure_and_reports_it(
    monkeypatch, caplog,
):
    """A transient local-store error must not silently retire the uploader.

    Without the guard the raised ``OperationalError`` escapes
    ``_upload_runtime_events``, the task dies, and every later event stalls
    with no log line naming the cause.
    """
    monkeypatch.setattr(
        worker_run_module, "RUNTIME_EVENT_DEGRADED_RETRY_SECONDS", 0.0
    )
    caplog.set_level(logging.WARNING)
    stop = asyncio.Event()
    attempts = 0

    class FlakyUploader:
        async def upload_once(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            stop.set()
            return UploadResult("uploaded", 1)

    run = StandardWorkerRun(SimpleNamespace(_stop=stop))
    await run._upload_runtime_events(FlakyUploader())

    assert attempts == 2
    reported = [
        record for record in caplog.records
        if record.levelno >= logging.WARNING and record.exc_info is not None
        and isinstance(record.exc_info[1], sqlite3.OperationalError)
    ]
    assert len(reported) == 1
    assert "database is locked" in str(reported[0].exc_info[1])


@pytest.mark.asyncio
async def test_durable_commits_run_off_loop_and_preserve_submission_order(
    tmp_path,
):
    """``synchronous=FULL`` commits must never run on the shared event loop.

    Moving them off the loop must not reorder concurrent enqueues, so the
    persisted sequence still follows submission order and the batch
    acknowledgement still empties the outbox.
    """
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    loop_thread = threading.get_ident()
    executing_threads: set[int] = set()
    outbox._db.set_trace_callback(
        lambda _statement: executing_threads.add(threading.get_ident())
    )

    async def accept(_path, body):
        return 200, {"accepted": [
            {"event_id": item["event_id"]}
            for item in json.loads(body)["events"]
        ]}

    await asyncio.gather(*(outbox.enqueue(event(index)) for index in range(5)))

    rows = outbox.prefix()
    assert [row.event_id for row in rows] == [
        event(index).event_id for index in range(5)
    ]
    sequences = [row.sequence for row in rows]
    assert sequences == sorted(set(sequences))

    uploader = RuntimeEventUploader(outbox, accept)
    assert (await uploader.upload_once()).state == "uploaded"
    assert outbox.prefix() == []

    assert executing_threads and loop_thread not in executing_threads
    outbox.close()


@pytest.mark.asyncio
async def test_row_level_http_rejection_still_isolates_then_discards_head(tmp_path):
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    await outbox.enqueue(event(1))
    await outbox.enqueue(event(2))

    async def rejected(_path, _body):
        return 422, {}

    uploader = RuntimeEventUploader(outbox, rejected)
    assert (await uploader.upload_once()).state == "retry"
    assert (await uploader.upload_once()).state == "discarded"
    assert [row.event_id for row in outbox.prefix()] == [
        "evt_2_turn.started",
    ]
    outbox.close()


@pytest.mark.asyncio
async def test_runtime_event_transport_enforces_complete_body_limits_and_progress(tmp_path):
    """The actual bytes handed to transport, rather than a row estimate,
    are bounded; bad heads cannot strand valid followers."""
    outbox = RuntimeEventOutbox(tmp_path / "transport.db")
    for index in range(51):
        await outbox.enqueue(event(index))
    bodies = []

    async def accept(_path, body):
        bodies.append(body)
        return 200, {"accepted": [
            {"event_id": value["event_id"]}
            for value in json.loads(body)["events"]
        ]}

    uploader = RuntimeEventUploader(outbox, accept)
    assert (await uploader.upload_once()).state == "uploaded"
    assert (await uploader.upload_once()).state == "uploaded"
    assert [len(json.loads(body)["events"]) for body in bodies] == [50, 1]
    assert all(len(body) <= 256 * 1024 for body in bodies)
    outbox.close()

    # Wrapper bytes make a two-row prefix too large although either row fits.
    wrapped = RuntimeEventOutbox(tmp_path / "wrapped.db")
    await wrapped.enqueue(event(1))
    await wrapped.enqueue(event(2))
    one = len(b'{"events":[' + wrapped.prefix(max_rows=1)[0].event_json + b']}')
    wrapped_bodies = []
    async def capture(_path, body):
        wrapped_bodies.append(body)
        return 200, {"accepted": [{"event_id": json.loads(body)["events"][0]["event_id"]}]}
    narrowed = RuntimeEventUploader(wrapped, capture, batch_rows=50, batch_bytes=one)
    assert (await narrowed.upload_once()).state == "uploaded"
    assert len(json.loads(wrapped_bodies[0])["events"]) == 1
    wrapped.close()

    # A singleton over the complete-frame cap is quarantined without a call.
    oversized = RuntimeEventOutbox(tmp_path / "oversized.db")
    huge = RuntimeEvent(agent_id="agent", session_ref="session", turn_ref="turn",
                        type="output.updated", payload={"block_id": "out", "kind": "result", "phase": "delta", "delta": "x" * (256 * 1024)},
                        event_id="huge")
    await oversized.enqueue(huge)
    calls = []
    async def never(_path, body):
        calls.append(body)
        return 200, {"accepted": []}
    assert (await RuntimeEventUploader(oversized, never).upload_once()).state == "discarded"
    assert calls == [] and oversized.prefix() == []
    oversized.close()


@pytest.mark.asyncio
async def test_log_boundaries_are_json_safe_and_content_free(tmp_path, caplog):
    logger = logging.getLogger("test.runtime.outbox")
    caplog.set_level(logging.INFO, logger=logger.name)
    outbox = RuntimeEventOutbox(
        tmp_path / "runtime_events.db", logger=logger
    )
    secret = "sk-super-secret-payload-123456"
    value = RuntimeEvent(
        agent_id="agent", session_ref="session", turn_ref="turn",
        type="output.updated",
        payload={
            "block_id": "out", "kind": "result", "phase": "delta",
            "delta": secret,
        },
        event_id="evt_log",
    )
    await outbox.enqueue(value)

    attempts = 0

    async def transport(_path, body):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(secret)
        ids = [item["event_id"] for item in json.loads(body)["events"]]
        return 200, {"accepted": [{"event_id": item} for item in ids]}

    uploader = RuntimeEventUploader(outbox, transport)
    assert (await uploader.upload_once()).state == "retry"
    assert (await uploader.upload_once()).state == "uploaded"
    sink = RuntimeEventProjectingSink(
        outbox,
        RuntimeEventProjector(agent_id="agent", session_ref="session"),
    )
    await sink(HarnessEvent(
        type="turn.started", driver="fake",
        session_ref=SessionRef("native"), turn_ref=TurnRef("logged-turn"),
    ))
    await sink(HarnessEvent(
        type="turn.completed", driver="fake",
        session_ref=SessionRef("native"), turn_ref=TurnRef("logged-turn"),
        data={"outcome": "succeeded"},
    ))
    records = []
    for record in caplog.records:
        marker = "runtime_event="
        if marker in record.getMessage():
            records.append(json.loads(record.getMessage().split(marker, 1)[1]))
    names = {record["event"] for record in records}
    assert {
        "runtime.enqueued", "runtime.batch_attempt",
        "runtime.retry", "runtime.acknowledged",
        "runtime.normalized_event", "runtime.projected",
    } <= names
    enqueued = next(
        record for record in records if record["event"] == "runtime.enqueued"
    )
    assert enqueued == {
        "event": "runtime.enqueued", "agent_id": "agent",
        "session_ref": "session", "turn_ref": "turn",
        "event_id": "evt_log", "event_type": "output.updated",
        "outbox_sequence": 1,
    }
    assert any(
        record.get("retry_count") == 1
        for record in records if record["event"] == "runtime.retry"
    )
    assert secret not in "\n".join(record.getMessage() for record in caplog.records)
    outbox.close()
