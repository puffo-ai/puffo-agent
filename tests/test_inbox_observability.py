from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from puffo_agent.agent._logging import log_runtime_event
from puffo_agent.agent.context_controller import ProviderAdmissionEvent
from puffo_agent.agent.global_inbox_runtime import (
    GlobalInboxRuntime,
    SendAttemptState,
    TrackingSendDelegate,
)
from puffo_agent.agent.harness.driver import (
    HarnessEvent,
    SessionRef,
    TurnRef,
)
from puffo_agent.agent.runtime_event_outbox import (
    RuntimeEventOutbox,
    RuntimeEventProjectingSink,
    RuntimeEventUploader,
)
from puffo_agent.agent.runtime_events import RuntimeEventProjector

from .test_global_inbox_runtime import Adapter, listen_delivery, payload_for


def _records(caplog):
    return [
        json.loads(record.message.split("runtime_event=", 1)[1])
        for record in caplog.records
        if "runtime_event=" in record.message
    ]


def test_info_lifecycle_chain_is_joinable_and_redacts_adversarial_values(caplog):
    caplog.set_level(logging.INFO)
    target = logging.getLogger("inbox-observability")
    common = {
        "agent_id": "agent-1",
        "message_id": "message-1",
        "server_seq": 7,
        "notice_generation": 3,
        "turn_id": "turn-1",
        "provider_session_id": "session-1",
        "send_attempt_id": "turn-1:1",
        "target": "channel:space-1:channel-1",
    }
    for event, outcome in (
        ("inbox.received", "received"),
        ("inbox.persisted", "pending"),
        ("notice.armed", "armed"),
        ("notice.admitted", "admitted"),
        ("inbox.read_staged", "staged"),
        ("inbox.row_in_turn", "in_turn"),
        ("send.attempted", "attempting"),
        ("send.committed", "committed"),
        ("turn.finalized", "processed"),
    ):
        log_runtime_event(target, event, outcome=outcome, **common)
    log_runtime_event(
        target,
        "send.failed",
        agent_id="Bearer secret-credential-sentinel",
        outcome="failed",
        # Unsupported content-bearing fields must be discarded entirely.
        content="plaintext-sentinel",
        payload="encrypted-payload-sentinel",
        reasoning="reasoning-sentinel",
        tool_arguments="tool-argument-sentinel",
        tool_result="tool-result-sentinel",
    )

    records = _records(caplog)
    assert len(records) == 10
    for record in records[:9]:
        assert record["agent_id"] == "agent-1"
        assert record["turn_id"] == "turn-1"
        assert record["notice_generation"] == 3
        assert record["send_attempt_id"] == "turn-1:1"
    serialized = json.dumps(records)
    for sentinel in (
        "secret-credential-sentinel",
        "plaintext-sentinel",
        "encrypted-payload-sentinel",
        "reasoning-sentinel",
        "tool-argument-sentinel",
        "tool-result-sentinel",
    ):
        assert sentinel not in serialized
    assert "[REDACTED]" in serialized


def _production_trace_sentinels():
    return (
        "plaintext-production-sentinel",
        "attachment-path-production-sentinel",
        "attachment-content-production-sentinel",
        "ciphertext-production-sentinel",
        "Bearer secret-credential-production-sentinel",
        "raw-provider-frame-production-sentinel",
        "reasoning-production-sentinel",
        "tool-argument-production-sentinel",
        "tool-result-production-sentinel",
    )


def _production_trace_payload(sentinels):
    return payload_for(
        "trace-message",
        content={
            "text": sentinels[0],
            "attachments": [{
                "path": sentinels[1],
                "content": sentinels[2],
                "ciphertext": sentinels[3],
            }],
            "raw_frame": sentinels[5],
            "reasoning": sentinels[6],
        },
    )


class _CorrelatingAdapter(Adapter):
    def __init__(self):
        super().__init__()
        self.continuation = None
        self.continuation_key = ""

    def register_continuation_callback(
        self, callback, planning_cycle_key, **_metadata
    ):
        self.continuation = callback
        self.continuation_key = planning_cycle_key

    async def admit_continuation(self):
        callback, self.continuation = self.continuation, None
        await callback(ProviderAdmissionEvent(
            planning_cycle_key=self.continuation_key,
            provider_session_id=self.session,
            provider_turn_id="trace-provider-turn",
            tool_call_id="trace-tool-call",
            admitted_at=datetime.now(timezone.utc),
        ))


class _TraceCoordinator:
    def __init__(self, sentinels):
        self.provider_session_id = None
        self.calls = []
        self.sentinels = sentinels

    async def send(self, request=None, **kwargs):
        self.calls.append((request, kwargs))
        return {
            "state": "sent", "envelope_id": "trace-send", "seq": 8,
            "seen_seq": 7, "latest_seq_before_send": 7,
            "note": self.sentinels[8], "ciphertext": self.sentinels[3],
            "raw_frame": self.sentinels[5], "reasoning": self.sentinels[6],
        }


async def _run_production_trace(tmp_path, store, client, sentinels):
    adapter = _CorrelatingAdapter()
    coordinator = _TraceCoordinator(sentinels)

    async def run(_planned):
        await adapter.admit(provider_turn_id="trace-provider-turn")
        page = await runtime.read_inbox(
            limit=1,
            tool_arguments={"limit": 1, "ignored": sentinels[7]},
        )
        assert page["messages"]
        await adapter.admit_continuation()
        result = await runtime.send_delegate.send({
            "destination": "ch-1",
            "text": f"{sentinels[0]} {sentinels[4]}",
        })
        assert result["state"] == "sent"

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=run,
        workspace=tmp_path,
        coordinator=coordinator,
        agent_id=client.agent_id,
    )
    runtime.send_delegate = TrackingSendDelegate(
        coordinator, runtime.attempts, runtime
    )
    assert await runtime.process_once()


def _assert_production_trace(records, sentinels):
    events = [record["event"] for record in records]
    required = [
        "inbox.received", "inbox.persisted", "notice.armed", "notice.due",
        "notice.admitted", "inbox.read_staged", "inbox.row_in_turn",
        "send.attempted", "send.committed", "inbox.row_processed",
        "turn.finalized",
    ]
    positions = [events.index(event) for event in required]
    assert positions == sorted(positions)
    message_records = [
        record for record in records if record.get("message_id") == "trace-message"
    ]
    assert message_records
    assert all(record.get("server_seq") == 7 for record in message_records)
    turn_records = [record for record in records if record.get("turn_id")]
    assert len({record["turn_id"] for record in turn_records}) == 1
    assert {
        record.get("provider_session_id") for record in turn_records
        if record.get("provider_session_id")
    } == {"provider-1"}
    attempts = [record for record in records if record["event"] == "send.attempted"]
    commits = [record for record in records if record["event"] == "send.committed"]
    assert attempts[0]["send_attempt_id"] == commits[0]["send_attempt_id"]
    serialized = json.dumps(records)
    for sentinel in sentinels:
        assert sentinel not in serialized


@pytest.mark.asyncio
async def test_production_receive_read_send_finalize_trace_is_joinable_and_safe(
    tmp_path, monkeypatch, caplog,
):
    caplog.set_level(logging.INFO)
    sentinels = _production_trace_sentinels()
    client, store, _events, _delivery = await listen_delivery(
        monkeypatch, tmp_path, payload=_production_trace_payload(sentinels), seq=7,
    )
    await _run_production_trace(tmp_path, store, client, sentinels)

    records = _records(caplog)
    _assert_production_trace(records, sentinels)
    await store.close()


@pytest.mark.asyncio
async def test_driver_projector_outbox_upload_trace_joins_event_ids_without_native_data(
    tmp_path, caplog,
):
    caplog.set_level(logging.INFO)
    target = logging.getLogger("runtime-projector-observability")
    sentinels = (
        "driver-plaintext-sentinel",
        "driver-attachment-path-sentinel",
        "driver-attachment-content-sentinel",
        "driver-ciphertext-sentinel",
        "driver-credential-sentinel",
        "driver-raw-frame-sentinel",
        "driver-reasoning-sentinel",
        "driver-tool-argument-sentinel",
        "driver-tool-result-sentinel",
    )
    outbox = RuntimeEventOutbox(
        tmp_path / "runtime_events.db", logger=target
    )
    sink = RuntimeEventProjectingSink(
        outbox,
        RuntimeEventProjector(
            agent_id="agent-safe", session_ref="session-safe"
        ),
    )

    def native(type_, data=None):
        return HarnessEvent.normalized(
            type=type_,
            driver="codex",
            session_ref=SessionRef("native-session"),
            turn_ref=TurnRef("turn-safe"),
            native_session_id="native-session",
            native_turn_id="native-turn",
            data=data or {},
            native_payload={
                "plaintext": sentinels[0],
                "attachment_path": sentinels[1],
                "attachment_content": sentinels[2],
                "ciphertext": sentinels[3],
                "credential": sentinels[4],
                "raw_frame": sentinels[5],
                "reasoning": sentinels[6],
                "arguments": sentinels[7],
                "result": sentinels[8],
            },
        )

    await sink(native("turn.started"))
    await sink(native("turn.tool_completed", {
        "tool_call_ref": "tool-safe",
        "label": "read_inbox",
        "outcome": "succeeded",
    }))
    await sink(native("turn.completed", {"outcome": "succeeded"}))
    uploaded = []

    async def transport(_path, body):
        uploaded.append(body)
        ids = [item["event_id"] for item in json.loads(body)["events"]]
        return 200, {"accepted": [{"event_id": value} for value in ids]}

    result = await RuntimeEventUploader(outbox, transport).upload_once()
    assert result.state == "uploaded"
    records = _records(caplog)
    projected = [
        record for record in records
        if record["event"] == "runtime.projected"
    ]
    enqueued = [
        record for record in records
        if record["event"] == "runtime.enqueued"
    ]
    assert projected
    assert {record["event_id"] for record in projected} == {
        record["event_id"] for record in enqueued
    }
    assert {
        (
            record["agent_id"],
            record["session_ref"],
            record["turn_ref"],
        )
        for record in projected
    } == {("agent-safe", "session-safe", "turn-safe")}
    serialized = json.dumps(records) + b"".join(uploaded).decode()
    assert all(sentinel not in serialized for sentinel in sentinels)
    outbox.close()


def _runtime_privacy_sentinels():
    return {
        "provider": "provider-identity-sentinel",
        "driver": "driver-sentinel",
        "data": "nonallowlisted-data-sentinel",
        "native": "native-payload-sentinel",
        "reasoning": "reasoning-sentinel",
        "raw_frame": "raw-frame-sentinel",
        "tool_argument": "tool-argument-sentinel",
        "tool_result": "tool-result-sentinel",
        "path": "path-sentinel",
        "credential": "credential-sentinel",
        "native_session": "native-session-sentinel",
        "native_turn": "native-turn-sentinel",
        "remote_error": "remote-error-sentinel",
        "exception": "exception-sentinel",
    }


def _raw_runtime_privacy_event(sentinels):
    return HarnessEvent.normalized(
        type="turn.started", driver=sentinels["driver"],
        session_ref=SessionRef(sentinels["native_session"]),
        turn_ref=TurnRef("turn-public"),
        native_session_id=sentinels["native_session"],
        native_turn_id=sentinels["native_turn"],
        data={
            "provider": sentinels["provider"], "data": sentinels["data"],
            "reasoning": sentinels["reasoning"], "raw_frame": sentinels["raw_frame"],
            "tool_argument": sentinels["tool_argument"],
            "tool_result": sentinels["tool_result"], "path": sentinels["path"],
            "credential": sentinels["credential"],
            "remote_error": sentinels["remote_error"],
            "exception": sentinels["exception"],
        },
        native_payload={"payload": sentinels["native"]},
    )


@pytest.mark.asyncio
async def test_runtime_start_pair_never_leaks_raw_boundaries_before_ack(
    tmp_path, caplog,
):
    """Exercise the production sink/outbox/uploader before acknowledgement."""
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("runtime-start-privacy")
    sentinels = _runtime_privacy_sentinels()
    outbox = RuntimeEventOutbox(tmp_path / "events.db", logger=logger)
    sink = RuntimeEventProjectingSink(
        outbox, RuntimeEventProjector(agent_id="agent", session_ref="logical"),
    )
    raw = _raw_runtime_privacy_event(sentinels)
    # Positive raw-boundary controls make this a leakage test, not merely a
    # search through values that were never injected.
    assert raw.driver == sentinels["driver"]
    assert raw.native_diagnostic["payload"] == sentinels["native"]
    assert raw.native_session_id == sentinels["native_session"]
    assert raw.native_turn_id == sentinels["native_turn"]
    assert all(value in json.dumps(raw.data) for value in sentinels.values()
               if value not in {sentinels["native"], sentinels["driver"],
                                sentinels["native_session"], sentinels["native_turn"]})
    await sink(raw)
    await sink(HarnessEvent.normalized(
        type="turn.assistant_delta", driver=sentinels["driver"],
        session_ref=SessionRef(sentinels["native_session"]),
        turn_ref=TurnRef("turn-public"),
        data={"text": "safe output", "block_id": "safe-block"},
    ))
    await sink(HarnessEvent.normalized(
        type="turn.tool_completed", driver=sentinels["driver"],
        session_ref=SessionRef(sentinels["native_session"]),
        turn_ref=TurnRef("turn-public"),
        data={"tool_call_ref": "safe-tool", "label": "safe tool"},
    ))
    await sink(HarnessEvent.normalized(
        type="turn.completed", driver=sentinels["driver"],
        session_ref=SessionRef(sentinels["native_session"]),
        turn_ref=TurnRef("turn-public"), data={"outcome": "succeeded"},
    ))
    projected = [row.event for row in outbox.prefix()]
    assert [(row["type"], row["payload"]) for row in projected[:2]] == [
        ("turn.started", {}), ("activity.updated", {"text": "Working"}),
    ]
    assert any(row["payload"].get("delta") == "safe output" for row in projected)
    assert any(row["type"] == "tool.updated" for row in projected)

    entered, release, uploads = asyncio.Event(), asyncio.Event(), []
    async def hold_upload(_path, body):
        uploads.append(body)
        entered.set()
        await release.wait()
        return 200, {"accepted": [
            {"event_id": item["event_id"]} for item in json.loads(body)["events"]
        ]}
    task = asyncio.create_task(RuntimeEventUploader(outbox, hold_upload).upload_once())
    await entered.wait()
    connection = sqlite3.connect(outbox.path)
    sqlite_bytes = b"".join(row[0] for row in connection.execute(
        "SELECT event_json FROM events ORDER BY sequence"
    ))
    connection.close()
    serialized = (
        json.dumps([row for row in projected]).encode() + sqlite_bytes +
        b"".join(uploads) + "\n".join(record.getMessage() for record in caplog.records).encode()
    ).decode()
    assert all(value not in serialized for value in sentinels.values())
    release.set()
    assert (await task).state == "uploaded"
    outbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keyless", "eligible", "event", "outcome", "reason"),
    [
        (
            False, True, "reconsideration.eligible", "accepted",
            "synchronized_and_admitted",
        ),
        (
            True, True, "reconsideration.eligible", "accepted",
            "synchronized_and_admitted",
        ),
        (
            False, False, "reconsideration.blocked", "rejected",
            "held_not_synchronized",
        ),
        (
            True, False, "reconsideration.blocked", "rejected",
            "held_not_synchronized",
        ),
    ],
)
async def test_reconsideration_audit_is_joinable_and_content_free(
    keyless, eligible, event, outcome, reason, caplog,
):
    caplog.set_level(
        logging.INFO,
        logger="puffo_agent.agent.global_inbox_runtime",
    )
    sentinels = (
        "audit-plaintext-sentinel",
        "audit-attachment-sentinel",
        "audit-encrypted-payload-sentinel",
        "audit-recovered-row-sentinel",
        "audit-reasoning-sentinel",
        "audit-credential-sentinel",
        "audit-tool-argument-sentinel",
        "audit-tool-result-sentinel",
    )

    class Coordinator:
        http_client = SimpleNamespace(keyless=keyless)

        async def send(self, _request=None, **_kwargs):
            return {
                "state": "sent" if eligible else "failed",
                "error_kind": (
                    None if eligible else "reconsideration_ineligible"
                ),
                "payload": sentinels[2],
                "recovered_messages": [{"content": sentinels[3]}],
                "reasoning": sentinels[4],
                "tool_result": sentinels[7],
                "_reconsideration_audit": {
                    "eligible": eligible,
                    "decision_reason": reason,
                    "provider_session_id": "session-a",
                    "turn_id": "turn-a",
                    "latest_seq": 5,
                    "latest_envelope_id": "held-envelope-a",
                    "admitted_seq": 5,
                },
            }

    active = SimpleNamespace(
        turn_id="turn-a",
        provider_session_id="session-a",
        provider_turn_id="provider-turn-a",
    )

    class Runtime:
        def __init__(self):
            self.agent_id = "agent-a"
            self.active = active

        @staticmethod
        def resolve_active_send_route(_destination, _request, _kwargs):
            return SimpleNamespace(
                space_id="space-a",
                channel_id="channel-a",
                thread_root_id=None,
                dm_peer=None,
            )

    delegate = TrackingSendDelegate(
        Coordinator(), SendAttemptState(), Runtime()
    )
    result = await delegate.send({
        "destination": "channel-a",
        "text": f"{sentinels[0]} Bearer {sentinels[5]}",
        "attachment_paths": [sentinels[1]],
        "send_anyway": True,
        "tool_arguments": sentinels[6],
    })
    assert "_reconsideration_audit" not in result
    records = [
        record for record in _records(caplog)
        if record["event"] == event
    ]
    assert len(records) == 1
    record = records[0]
    assert record == {
        "agent_id": "agent-a",
        "channel_id": "channel-a",
        "decision_reason": reason,
        "envelope_id": "held-envelope-a",
        "event": event,
        "latest_seq": 5,
        "mode": "send_anyway",
        "outcome": outcome,
        "provider_session_id": "session-a",
        "provider_turn_id": "provider-turn-a",
        "seen_seq": 5,
        "send_attempt_id": "turn-a:1",
        "space_id": "space-a",
        "transport": "keyless" if keyless else "channel",
        "turn_id": "turn-a",
    }
    serialized = json.dumps(_records(caplog))
    assert all(sentinel not in serialized for sentinel in sentinels)


@pytest.mark.asyncio
async def test_keyless_dm_attempt_has_no_freshness_mode(caplog):
    caplog.set_level(
        logging.INFO,
        logger="puffo_agent.agent.global_inbox_runtime",
    )

    class Coordinator:
        http_client = SimpleNamespace(keyless=True)

        async def send(self, *_args, **_kwargs):
            return {"state": "sent", "envelope_id": "dm-envelope"}

    await TrackingSendDelegate(
        Coordinator(), SendAttemptState()
    ).send({"destination": "@alice", "send_anyway": True})
    attempt = next(
        record for record in _records(caplog)
        if record["event"] == "send.attempted"
    )
    assert attempt["transport"] == "keyless"
    assert "mode" not in attempt
