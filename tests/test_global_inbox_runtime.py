from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from puffo_agent.agent.core import AgentAPIError, PuffoAgent
from puffo_agent.agent.adapters.base import TurnResult
from puffo_agent.agent.context_controller import (
    ContextDecision,
    ContextCapabilities,
    ContextSnapshot,
    DecisionOutcome,
    ProviderAdmissionEvent,
)
from puffo_agent.agent.global_inbox_runtime import (
    ActiveBoundaryAdapter,
    ActiveExactUnion,
    BaselineAdapter,
    GlobalInboxRuntime,
    HeldRecoverySource,
    MessageRoute,
    PlannedTurn,
    RuntimeHealth,
    SendAttemptState,
    TrackingSendDelegate,
    await_listener_with_runtime,
    route_for,
)
from puffo_agent.agent.message_store import (
    MessageStore,
    ReceiptDisposition,
    ReceiptResult,
    ReceiptWriteStatus,
)
from puffo_agent.agent._logging import log_runtime_event
from puffo_agent.agent.runtime_event_outbox import RuntimeEventOutbox
from puffo_agent.agent.runtime_events import RuntimeEvent
from puffo_agent.crypto.message import MessagePayload
from puffo_agent.crypto.ws_client import TransportOutcome


def runtime_events(caplog):
    events = []
    for record in caplog.records:
        message = record.getMessage()
        marker = "runtime_event="
        if marker in message:
            events.append(json.loads(message.split(marker, 1)[1]))
    return events


def projection_metadata(block: str) -> dict[str, object]:
    """Extract stable facts from a context-versioned semantic row."""
    row = next(
        line for line in block.splitlines() if line.startswith("[message ")
    )
    return {
        "envelope_id": re.search(r'\bmessage_id="([^"]+)"', row).group(1),
        "sender_slug": re.search(r'\bsender_identity="@([^"]+)"', row).group(1),
        "is_self": " self=true" in row,
    }


def test_runtime_event_helper_fails_open_and_omits_unavailable(
    caplog, monkeypatch,
):
    caplog.set_level(
        logging.INFO,
        logger=__name__,
    )
    target = logging.getLogger(__name__)
    log_runtime_event(target, "unknown_event", agent_id="agent")
    log_runtime_event(
        target, "batch.planned", unknown_field=["ignored"],
    )
    log_runtime_event(
        target, "batch.planned", agent_id=object(), first_seq=float("nan"),
    )

    import puffo_agent.agent._logging as logging_module
    original_dumps = logging_module.json.dumps
    monkeypatch.setattr(
        logging_module.json,
        "dumps",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError("serialize")),
    )
    log_runtime_event(target, "batch.planned", agent_id="agent")
    monkeypatch.setattr(logging_module.json, "dumps", original_dumps)

    class RaisingHandler(logging.Handler):
        def emit(self, _record):
            raise RuntimeError("handler failed")

    isolated = logging.getLogger("test.runtime-event-fail-open")
    isolated.handlers[:] = [RaisingHandler()]
    isolated.propagate = False
    isolated.setLevel(logging.INFO)
    try:
        log_runtime_event(isolated, "batch.planned", agent_id="agent")
    finally:
        isolated.handlers.clear()

    log_runtime_event(
        target,
        "batch.planned",
        agent_id="agent",
        first_seq=None,
        last_seq=None,
    )
    log_runtime_event(
        target,
        "batch.planned",
        envelope_ids=["env-1"],
        routes=[{
            "space_id": "sp-1",
            "channel_id": "ch-1",
            "count": 1,
            "min_seq": 2,
            "max_seq": 2,
        }],
    )
    log_runtime_event(
        target,
        "batch.planned",
        routes=[{"channel_id": "ch-1", "payload": "rejected"}],
    )
    assert runtime_events(caplog) == [
        {"event": "batch.planned"},
        {"event": "batch.planned"},
        {
            "event": "batch.planned",
            "agent_id": "agent",
        },
        {
            "event": "batch.planned",
            "envelope_ids": ["env-1"],
            "routes": [{
                "channel_id": "ch-1",
                "count": 1,
                "max_seq": 2,
                "min_seq": 2,
                "space_id": "sp-1",
            }],
        },
        {"event": "batch.planned"},
    ]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "runtime observability degraded" in record.getMessage()
    ]
    assert warnings
    assert all(
        "unknown_event" not in warning
        and "unknown_field" not in warning
        for warning in warnings
    )


class Adapter:
    def __init__(self):
        self.callback = None
        self.key = ""
        self.session = "provider-1"
        self.inputs = []

    async def get_context_snapshot(self):
        return ContextSnapshot(0, 200_000, "test", datetime.now(timezone.utc))

    def get_context_capabilities(self):
        return ContextCapabilities()

    async def compact_context(self):
        raise AssertionError("not expected")

    async def rollover_context(self):
        raise AssertionError("not expected")

    def get_provider_session_id(self):
        return self.session

    def register_admission_callback(self, callback, planning_cycle_key=""):
        self.callback = callback
        self.key = planning_cycle_key

    async def admit(
        self,
        session: str | None = "provider-1",
        provider_turn_id: str = "provider-turn",
    ):
        callback, self.callback = self.callback, None
        assert callback is not None
        await callback(ProviderAdmissionEvent(
            planning_cycle_key=self.key,
            provider_session_id=session,
            provider_turn_id=provider_turn_id,
            admitted_at=datetime.now(timezone.utc),
        ))


class ToolReturnAdapter(Adapter):
    tool_result_admission_boundary = "tool_return"

    def register_continuation_callback(self, *_args, **_kwargs):
        raise AssertionError("tool-return admission must not await provider completion")


async def make_store(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    return store


async def receipt(
    store,
    envelope_id,
    seq,
    *,
    kind="channel",
    channel="ch-1",
    space="sp-1",
    sender="alice",
    disposition=ReceiptDisposition.ELIGIBLE,
    content=None,
    is_encrypted=True,
):
    return await store.store_receipt(
        {
            "envelope_id": envelope_id,
            "envelope_kind": kind,
            "sender_slug": sender,
            "recipient_slug": "agent" if kind == "dm" else None,
            "channel_id": channel if kind != "dm" else None,
            "space_id": space if kind != "dm" else None,
            "content": content if content is not None else f"text-{envelope_id}",
            "content_type": "text/plain",
            "sent_at": seq,
            "is_encrypted": is_encrypted,
        },
        server_seq=seq,
        disposition=disposition,
        reason="test",
    )


class _ListenContacts:
    def __init__(self, blocked):
        self.blocked = blocked

    async def is_blocked(self, _slug):
        return self.blocked

    async def is_allowed(self, _slug):
        return False


class _ListenKeyCache:
    async def get_signing_keys(self, _slug):
        return [b"key"]

    def invalidate(self, _slug):
        return None


class _ListenKeyStore:
    def __init__(self):
        self.identity = SimpleNamespace(
            kem_secret_key="ignored", identity_cert_json="{}",
            server_url="https://example.test",
        )

    def load_identity(self, _slug):
        return self.identity


def _listen_ws_type(delivery, events):
    class FakeWs:
        def __init__(self, **_kwargs):
            self.on_message = None
            self.on_event = None
            self.on_connect = None

        async def run(self):
            outcome = await self.on_message(delivery)
            assert events
            events.append(outcome.value)

        async def dispatch_delivery(self, item):
            outcome = await self.on_message(item)
            return SimpleNamespace(outcome=outcome)

    return FakeWs


def _configure_listen_client(client, store, tmp_path, events, blocked):
    client.agent_id = "agent-id"
    client.slug = "agent"
    client.device_id = "dev"
    client.operator_slug = "operator"
    client.auto_accept_dm = False
    client.workspace = str(tmp_path)
    client.store = store
    client.keystore = _ListenKeyStore()
    client.http = SimpleNamespace()
    client._key_cache = _ListenKeyCache()
    client._contacts = _ListenContacts(blocked)
    client._log = logging.getLogger("receipt-listen")
    client._warm_task = None
    client._operator_root_pubkey = None
    client._channel_space = {}
    client._catchup_stale_ms = 0
    client._max_inline_chars = 100_000
    client._segment_chars = 10_000
    client._pending_dm_approvals = {}
    client.global_runtime = SimpleNamespace(
        notify=lambda: events.append("work-wake"),
        notify_delivery=lambda: events.append("delivery-wake"),
    )
    client._processed_invite_ids = set()
    client._processed_membership_event_ids = set()


def _install_listen_stubs(client, gate_foreign_dm):
    async def none(*_args, **_kwargs):
        return None

    async def false(*_args, **_kwargs):
        return False

    async def empty(*_args, **_kwargs):
        return {}

    client._resolve_incoming_thread_root = none
    client._validate_incoming_parent_id = none
    client._maybe_allowlist_outbound_dm = none
    client._apply_invite_replies = empty
    client._maybe_handle_leave_reply = false
    client._maybe_handle_permission_reply = false
    client._maybe_handle_dm_approval_reply = false
    client._is_stale_for_catchup = lambda _sent_at: False
    client._get_space_members = empty
    client._resolve_space_name = lambda _space: asyncio.sleep(0, result="Space")
    client._resolve_channel_name = lambda *_a, **_k: asyncio.sleep(0, result="Channel")
    client._fetch_display_name = lambda slug: asyncio.sleep(0, result=slug.title())
    client._fetch_owner_slug = lambda _slug: asyncio.sleep(0, result="")
    client._is_foreign_dm_sender = lambda _slug: asyncio.sleep(
        0, result=gate_foreign_dm,
    )
    client._ensure_trusted_contact = none
    client._maybe_send_dm_notice = none
    client._shares_space_with = false
    client._maybe_gate_foreign_dm = lambda **_kwargs: asyncio.sleep(
        0, result=gate_foreign_dm,
    )
    client._invite_poll_loop = lambda: asyncio.sleep(3600)
    client._on_ws_connect = none
    client._handle_event = none


async def listen_delivery(
    monkeypatch, tmp_path, *, payload: MessagePayload, seq: int,
    blocked: bool = False, gate_foreign_dm: bool = False, setup=None,
):
    """Drive the production listen callback with a complete wrapper."""
    import puffo_agent.agent.puffo_core_client as client_mod
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient

    store = await make_store(tmp_path)
    events: list[str] = []
    original_store_receipt = store.store_receipt

    async def ordered_store(*args, **kwargs):
        result = await original_store_receipt(*args, **kwargs)
        events.append("committed")
        return result

    store.store_receipt = ordered_store
    delivery = {"seq": seq, "envelope": {
        "envelope_id": payload.envelope_id, "sender_slug": payload.sender_slug,
        "type": "encrypted_message_envelope",
    }}
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    _configure_listen_client(client, store, tmp_path, events, blocked)
    _install_listen_stubs(client, gate_foreign_dm)
    monkeypatch.setattr(client_mod, "PuffoCoreWsClient", _listen_ws_type(delivery, events))
    monkeypatch.setattr(client_mod, "decrypt_message", lambda *_args: payload)
    monkeypatch.setattr(client_mod, "decode_secret", lambda _value: b"secret")
    monkeypatch.setattr(client_mod.KemKeyPair, "from_secret_bytes", lambda _v: object())
    setup_result = await setup(client, store, events, delivery) if setup else None
    await client.listen(lambda *_args: asyncio.sleep(0))
    result = (client, store, events, delivery)
    return result if setup is None else (*result, setup_result)


def payload_for(
    envelope_id: str,
    *,
    kind: str = "channel",
    sender: str = "alice",
    content: str = "hello",
) -> MessagePayload:
    return MessagePayload(
        payload_type="message_payload",
        version=2,
        envelope_id=envelope_id,
        envelope_kind=kind,
        sender_slug=sender,
        sender_subkey_id="subkey",
        sent_at=1,
        message_nonce="nonce",
        content_type="text/plain",
        content=content,
        is_visible_to_human=True,
        space_id="sp-1" if kind == "channel" else None,
        channel_id="ch-1" if kind == "channel" else None,
        recipient_slug="agent" if kind == "dm" else None,
    )


@pytest.mark.asyncio
async def test_receipt_commit_before_ack_and_wake_without_admission(tmp_path):
    store = await make_store(tmp_path)
    result = await receipt(store, "m1", 1)
    assert result.acknowledge
    pending = await store.get_pending()
    assert [item.envelope_id for item in pending] == ["m1"]
    assert pending[0].model_visible_at is None
    await store.close()
@pytest.mark.asyncio
async def test_receipt_listen_eligible_commit_before_ack_and_scheduler_wake(
    monkeypatch, tmp_path, caplog,
):
    caplog.set_level(
        logging.DEBUG,
        logger="receipt-listen",
    )
    _client, store, events, _delivery = await listen_delivery(
        monkeypatch, tmp_path, payload=payload_for("eligible"), seq=11,
    )
    assert events == [
        "committed",
        "delivery-wake",
        "work-wake",
        TransportOutcome.ACK.value,
    ]
    row = (await store.get_pending())[0]
    assert row.envelope_id == "eligible"
    assert row.model_visible_at is None
    receipt_event = next(
        item for item in runtime_events(caplog)
        if item["event"] == "inbox.receipt_committed"
    )
    assert receipt_event == {
        "event": "inbox.receipt_committed",
        "agent_id": "agent-id",
        "agent_slug": "agent",
        "envelope_id": "eligible",
        "space_id": "sp-1",
        "channel_id": "ch-1",
            "seq": 11,
            "server_seq": 11,
            "message_id": "eligible",
            "mode": "transport_receipt",
        "state": "eligible",
    }
    await store.close()


@pytest.mark.asyncio
async def test_receipt_listen_blocked_tombstone_never_persists_plaintext(
    monkeypatch, tmp_path,
):
    secret = "blocked-secret-that-must-not-persist"
    _client, store, events, _delivery = await listen_delivery(
        monkeypatch,
        tmp_path,
        payload=payload_for("blocked", content=secret),
        seq=12,
        blocked=True,
    )
    assert events == [
        "committed",
        "delivery-wake",
        TransportOutcome.ACK.value,
    ]
    raw = (tmp_path / "messages.db").read_bytes()
    assert secret.encode() not in raw
    assert await store.get_pending() == ()
    await store.close()


@pytest.mark.asyncio
async def test_terminal_channel_delivery_wake_releases_exact_watermark_waiter(
    monkeypatch, tmp_path,
):
    async def setup(client, store, _events, _delivery):
        runtime = GlobalInboxRuntime(
            store=store,
            adapter=Adapter(),
            run_turn=lambda _planned: None,
            workspace=tmp_path,
        )
        runtime.held_recovery_source.wait_timeout_s = 0.5
        client.global_runtime = runtime
        return asyncio.create_task(
            runtime.held_recovery_source.wait_for_held_delivery(
                "sp-1", "ch-1", 14, "terminal"
            )
        )

    _client, store, events, _delivery, waiter = await listen_delivery(
        monkeypatch,
        tmp_path,
        payload=payload_for("terminal", content="secret"),
        seq=14,
        blocked=True,
        setup=setup,
    )
    assert await waiter
    assert "delivery-wake" not in events  # real runtime wake, not the event spy
    assert await store.get_pending() == ()
    await store.close()


@pytest.mark.asyncio
async def test_idempotent_terminal_channel_delivery_wake_releases_waiter(
    monkeypatch, tmp_path,
):
    async def setup(client, store, events, _delivery):
        runtime = GlobalInboxRuntime(
            store=store,
            adapter=Adapter(),
            run_turn=lambda _planned: None,
            workspace=tmp_path,
        )
        runtime.held_recovery_source.wait_timeout_s = 0.5
        client.global_runtime = runtime
        waiter = asyncio.create_task(
            runtime.held_recovery_source.wait_for_held_delivery(
                "sp-1", "ch-1", 15, "idempotent-terminal"
            )
        )
        await asyncio.sleep(0)
        await store.store_receipt(
            {
                "envelope_id": "idempotent-terminal",
                "envelope_kind": "channel",
                "sender_slug": "agent",
                "channel_id": "ch-1",
                "space_id": "sp-1",
                "recipient_slug": None,
                "content_type": "text/plain",
                "content": "hello",
                "sent_at": 1,
                "thread_root_id": None,
                "reply_to_id": None,
                "is_encrypted": True,
            },
            server_seq=15,
            disposition=ReceiptDisposition.TERMINAL,
            reason="self echo",
        )
        events.clear()
        assert not waiter.done()
        return waiter

    _client, store, events, _delivery, waiter = await listen_delivery(
        monkeypatch,
        tmp_path,
        payload=payload_for(
            "idempotent-terminal", sender="agent", content="hello"
        ),
        seq=15,
        setup=setup,
    )
    assert await waiter
    assert events == ["committed", TransportOutcome.ACK.value]
    assert await store.get_pending() == ()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_events"),
    [
        (
            "eligible_dm",
            ["committed", "work-wake", TransportOutcome.ACK.value],
        ),
        (
            "conflict",
            ["write-conflict", TransportOutcome.HOLD.value],
        ),
    ],
)
async def test_notification_matrix_dm_and_conflict(
    monkeypatch, tmp_path, kind, expected_events,
):
    async def setup(_client, store, events, _delivery):
        if kind == "conflict":
            async def conflict(*_args, **_kwargs):
                events.append("write-conflict")
                return ReceiptResult(
                    ReceiptWriteStatus.CONFLICT,
                    ReceiptDisposition.ELIGIBLE,
                    "conflict",
                    False,
                )

            store.store_receipt = conflict

    payload = payload_for(
        f"matrix-{kind}",
        kind="dm" if kind == "eligible_dm" else "channel",
    )
    _client, store, events, _delivery, _setup = await listen_delivery(
        monkeypatch,
        tmp_path,
        payload=payload,
        seq=16,
        setup=setup,
    )
    assert events == expected_events
    await store.close()


@pytest.mark.asyncio
async def test_notification_matrix_raised_write_wakes_neither(
    monkeypatch, tmp_path,
):
    captured = {}

    async def setup(_client, store, events, _delivery):
        captured["store"] = store
        captured["events"] = events

        async def fail(*_args, **_kwargs):
            events.append("write-raised")
            raise OSError("disk full")

        store.store_receipt = fail

    with pytest.raises(OSError, match="disk full"):
        await listen_delivery(
            monkeypatch,
            tmp_path,
            payload=payload_for("matrix-raised"),
            seq=17,
            setup=setup,
        )
    assert captured["events"] == ["write-raised"]
    await captured["store"].close()


@pytest.mark.asyncio
async def test_gated_receipt_listen_holds_then_exact_wrapper_promotion_acks_once(
    monkeypatch, tmp_path,
):
    client, store, events, delivery = await listen_delivery(
        monkeypatch,
        tmp_path,
        payload=payload_for("gated", kind="dm", sender="foreign"),
        seq=13,
        gate_foreign_dm=True,
    )
    assert events == ["committed", TransportOutcome.DEFER.value]
    assert await store.get_pending() == ()
    gated = await store.get_message_by_envelope("gated")
    assert gated is not None and gated.content == "hello"

    client._is_foreign_dm_sender = lambda _slug: asyncio.sleep(0, result=True)
    client._maybe_gate_foreign_dm = lambda **_kwargs: asyncio.sleep(0, result=False)
    client._contacts.is_allowed = lambda _slug: asyncio.sleep(0, result=True)
    posts = []

    class ApprovalHttp:
        async def get(self, _path):
            return {"messages": [delivery]}

        async def post(self, path, body):
            posts.append((path, body))
            return {}

    client.http = ApprovalHttp()
    await client._drain_pending_from_sender("foreign")
    pending = await store.get_pending()
    assert [row.envelope_id for row in pending] == ["gated"]
    assert pending[0].content["text"] == "hello"
    assert pending[0].content["sender_display_name"] == "Foreign"
    assert pending[0].content["attachment_paths"] == []
    assert posts == [("/messages/ack", {"envelope_ids": ["gated"]})]
    await client._drain_pending_from_sender("foreign")
    assert [row.envelope_id for row in await store.get_pending()] == ["gated"]
    await store.close()


@pytest.mark.asyncio
async def test_legacy_gated_receipt_backfills_then_promotes_and_acks(
    monkeypatch, tmp_path,
):
    payload = payload_for("legacy-gated", kind="dm", sender="foreign")

    async def setup(_client, store, _events, _delivery):
        await store.store(payload)

    client, store, events, delivery, _ = await listen_delivery(
        monkeypatch,
        tmp_path,
        payload=payload,
        seq=16,
        gate_foreign_dm=True,
        setup=setup,
    )
    assert events == ["committed", TransportOutcome.DEFER.value]
    legacy = await store.get_message_by_envelope("legacy-gated")
    assert legacy is not None
    assert legacy.server_seq == 16
    assert legacy.receipt_disposition is ReceiptDisposition.FOREIGN_DM_GATED
    assert legacy.processing_state is None

    client._is_foreign_dm_sender = lambda _slug: asyncio.sleep(0, result=True)
    client._maybe_gate_foreign_dm = lambda **_kwargs: asyncio.sleep(
        0, result=False
    )
    client._contacts.is_allowed = lambda _slug: asyncio.sleep(0, result=True)
    posts = []

    class ApprovalHttp:
        async def get(self, _path):
            return {"messages": [delivery]}

        async def post(self, path, body):
            posts.append((path, body))
            return {}

    client.http = ApprovalHttp()
    await client._drain_pending_from_sender("foreign")

    assert [row.envelope_id for row in await store.get_pending()] == [
        "legacy-gated"
    ]
    assert posts == [
        ("/messages/ack", {"envelope_ids": ["legacy-gated"]}),
    ]
    await store.close()


@pytest.mark.asyncio
async def test_local_event_introduction_has_no_server_seq_and_wakes_scheduler(
    tmp_path,
):
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient

    store = await make_store(tmp_path)
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.store = store
    client._log = logging.getLogger("local-event")
    wakes = []
    client.global_runtime = SimpleNamespace(notify=lambda: wakes.append("wake"))
    client._resolve_space_name = lambda _space: asyncio.sleep(0, result="Space")
    client._resolve_channel_name = (
        lambda *_args, **_kwargs: asyncio.sleep(0, result="General")
    )
    await client._enqueue_channel_intro_nudge(space_id="sp", channel_id="ch")
    rows = await store.get_pending()
    assert len(rows) == 1 and rows[0].server_seq is None
    assert wakes == ["wake"]
    await store.close()


@pytest.mark.asyncio
async def test_gated_promotion_exact_wrapper_ack_only_after_promotion(tmp_path):
    store = await make_store(tmp_path)
    gated = await receipt(
        store, "dm1", 7, kind="dm",
        disposition=ReceiptDisposition.FOREIGN_DM_GATED,
    )
    assert not gated.acknowledge
    assert await store.get_pending() == ()
    promoted = await store.promote_gated_receipt("dm1", 7, reason="approved")
    assert promoted.acknowledge
    assert [m.envelope_id for m in await store.get_pending()] == ["dm1"]
    await store.close()


@pytest.mark.asyncio
async def test_local_event_has_no_fabricated_server_seq_and_global_order(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "m1", 1)
    local = await store.store_local_event(
        {
            "envelope_id": "local-1",
            "envelope_kind": "channel",
            "sender_slug": "runtime",
            "channel_id": "ch-1",
            "space_id": "sp-1",
            "content": "membership changed",
            "sent_at": 2,
        },
        reason="local event",
    )
    await receipt(store, "m2", 2)
    assert local.server_seq is None
    assert [m.envelope_id for m in await store.get_pending()] == [
        "m1", "local-1", "m2",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_turn_send_mode_tracks_encrypted_bundle_and_clears(tmp_path):
    from puffo_agent.agent import send_mode

    store = await make_store(tmp_path)
    await receipt(store, "encrypted", 1, is_encrypted=True)
    adapter = Adapter()

    async def run(_planned):
        assert await send_mode.encryption_required(
            "agent-send-mode", store, None
        )
        await adapter.admit()

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=run,
        workspace=tmp_path,
        send_mode_keys=("agent-send-mode",),
    )
    assert await runtime.process_once()
    assert not await send_mode.encryption_required(
        "agent-send-mode", store, None
    )
    await store.close()


@pytest.mark.asyncio
async def test_turn_send_mode_plaintext_bundle_does_not_require_encryption(tmp_path):
    from puffo_agent.agent import send_mode

    store = await make_store(tmp_path)
    await receipt(store, "plaintext", 1, is_encrypted=False)
    adapter = Adapter()

    async def run(_planned):
        assert not await send_mode.encryption_required(
            "plaintext-agent", store, None
        )
        await adapter.admit()

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=run,
        workspace=tmp_path,
        send_mode_keys=("plaintext-agent",),
    )
    assert await runtime.process_once()
    await store.close()


@pytest.mark.asyncio
async def test_listener_guard_stops_transport_when_runtime_crashes():
    listener_started = asyncio.Event()
    listener_stopped = asyncio.Event()

    async def listen_forever():
        listener_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            listener_stopped.set()

    async def crash_runtime():
        await listener_started.wait()
        raise ValueError("runtime boom")

    runtime_task = asyncio.create_task(crash_runtime())
    with pytest.raises(RuntimeError, match="global inbox crashed: runtime boom"):
        await await_listener_with_runtime(
            listen_forever(),
            runtime_task,
            label="global inbox",
        )

    assert listener_stopped.is_set()


@pytest.mark.asyncio
async def test_listener_guard_observes_simultaneous_listener_failure():
    release = asyncio.Event()
    listener_started = asyncio.Event()

    async def fail_listener():
        listener_started.set()
        await release.wait()
        raise OSError("listener boom")

    async def fail_runtime():
        await listener_started.wait()
        await release.wait()
        raise ValueError("runtime boom")

    runtime_task = asyncio.create_task(fail_runtime())
    guarded = asyncio.create_task(
        await_listener_with_runtime(
            fail_listener(),
            runtime_task,
            label="global inbox",
        )
    )
    await listener_started.wait()
    release.set()

    with pytest.raises(
        RuntimeError,
        match="runtime boom; listener also failed: listener boom",
    ):
        await guarded


@pytest.mark.asyncio
async def test_global_notice_turns_are_ephemeral_to_the_agent_log(tmp_path):
    received = []

    class RecordingAdapter:
        async def run_turn(self, ctx):
            received.append(list(ctx.messages))
            return TurnResult(
                reply="[SILENT]",
                metadata={"assistant_text_parts": ["[SILENT]"]},
            )

    agent = PuffoAgent(
        adapter=RecordingAdapter(),
        system_prompt="system",
        memory_dir=str(tmp_path / "memory"),
        workspace_dir=str(tmp_path),
        agent_id="test",
    )
    ordinary = {"role": "user", "content": "ordinary conversation input"}
    agent.log.append(ordinary)
    notices = [
        "<global_inbox_notice>first-current-notice</global_inbox_notice>",
        "<global_inbox_notice>second-current-notice</global_inbox_notice>",
    ]

    for notice in notices:
        await agent.handle_global_inbox_turn(SimpleNamespace(
            provider_input=notice,
            targets=(),
        ))

    assert [messages[-1]["content"] for messages in received] == notices
    assert all(messages[:-1] == [ordinary] for messages in received)
    assert agent.log == [ordinary]
    assert all("<global_inbox_notice>" not in entry["content"] for entry in agent.log)


@pytest.mark.asyncio
async def test_pre_admission_failure_leaves_pending_without_self_retry(
    tmp_path, caplog,
):
    caplog.set_level(logging.INFO)
    store = await make_store(tmp_path)
    await receipt(store, "m1", 1)
    adapter = Adapter()
    calls = 0

    async def fail(_planned):
        nonlocal calls
        calls += 1
        assert (tmp_path / ".puffo-agent/current_turn.json").exists()
        raise RuntimeError("provider failed before admission")

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=fail, workspace=tmp_path,
    )
    assert await runtime.process_once()
    assert not await runtime.process_once()
    assert calls == 1
    assert runtime.health == RuntimeHealth(
        "degraded", "turn failed before durable admission"
    )
    assert runtime._degraded is True
    assert [m.envelope_id for m in await store.get_pending()] == ["m1"]
    assert not (tmp_path / ".puffo-agent/current_turn.json").exists()
    failures = [
        event for event in runtime_events(caplog) if event["event"] == "turn.failed"
    ]
    assert len(failures) == 1
    assert failures[0]["error_type"] == "RuntimeError"
    assert failures[0]["outcome"] == "degraded"
    await store.close()


@pytest.mark.asyncio
async def test_admission_failure_requeues_exact_union_and_provider_session_clears(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "m1", 1)
    adapter = Adapter()

    class Coordinator:
        provider_session_id = None

    coordinator = Coordinator()

    async def fail(_planned):
        await adapter.admit("actual-session")
        assert coordinator.provider_session_id == "actual-session"
        raise RuntimeError("unsafe recovery")

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=fail, workspace=tmp_path,
        coordinator=coordinator,
    )
    await runtime.process_once()
    assert coordinator.provider_session_id is None
    assert [m.envelope_id for m in await store.get_pending()] == ["m1"]
    await store.close()


@pytest.mark.asyncio
async def test_baseline_boundary_stateless_and_same_channel_advance(tmp_path):
    store = await make_store(tmp_path)
    assert await BaselineAdapter(store).get_context_baseline_seq("sp", "ch") is None
    await store.set_context_baseline("sp", "ch", 4)
    assert await BaselineAdapter(store).get_context_baseline_seq("sp", "ch") == 4
    active = ActiveExactUnion(turn_id="turn")
    boundary = ActiveBoundaryAdapter(store, active)
    assert await boundary.get_active_turn_through_seq("sp", "ch") is None
    await boundary.advance_active_turn_through_seq("sp", "ch", 8)
    await boundary.advance_active_turn_through_seq("sp2", "other", 3)
    assert await boundary.get_active_turn_through_seq("sp", "ch") == 8
    assert await boundary.get_active_turn_through_seq("sp2", "other") == 3
    assert await store.get_context_baseline("sp", "ch") == 4
    assert await store.get_context_baseline("sp2", "other") is None
    await store.close()


@pytest.mark.asyncio
async def test_active_boundary_safe_prefix_matrix(tmp_path):
    """The active proof is current-turn scoped and independent of baseline."""
    async def visible(case, setup):
        store = await make_store(tmp_path / case)
        try:
            active = ActiveExactUnion(turn_id="active")
            boundary = ActiveBoundaryAdapter(store, active)
            return await setup(store, boundary)
        finally:
            await store.close()

    async def lower_pending(store, boundary):
        await receipt(store, "pending", 11)
        await receipt(store, "current", 30)
        await store.admit_messages(["current"], turn_id="active", provider_session_id=None)
        # A lower same-channel pending row blocks a later current-turn row.
        assert await boundary.get_active_turn_through_seq("sp-1", "ch-1") is None
    await visible("lower-pending", lower_pending)

    async def foreign_turn(store, boundary):
        await receipt(store, "foreign", 11)
        await receipt(store, "current", 30)
        await store.admit_messages(["foreign"], turn_id="foreign", provider_session_id=None)
        await store.admit_messages(["current"], turn_id="active", provider_session_id=None)
        # A foreign turn likewise cannot establish this active boundary.
        assert await boundary.get_active_turn_through_seq("sp-1", "ch-1") is None
    await visible("foreign-turn", foreign_turn)

    async def sparse_other_channel(store, boundary):
        await receipt(store, "other-sparse", 12, channel="other")
        await receipt(store, "current", 30)
        await store.admit_messages(["current"], turn_id="active", provider_session_id=None)
        # Globally sparse sequences in another channel are irrelevant.
        assert await boundary.get_active_turn_through_seq("sp-1", "ch-1") == 30
    await visible("sparse-other-channel", sparse_other_channel)

    async def resolution(store, boundary):
        await receipt(store, "earlier", 11)
        await receipt(store, "current", 30)
        await store.admit_messages(["current"], turn_id="active", provider_session_id=None)
        assert await boundary.get_active_turn_through_seq("sp-1", "ch-1") is None
        await store.admit_messages(["earlier"], turn_id="active", provider_session_id=None)
        # Resolving the blocker advances by observed sequence, not N+1 adjacency.
        assert await boundary.get_active_turn_through_seq("sp-1", "ch-1") == 30
    await visible("resolution", resolution)

    async def candidate_ceiling(store, boundary):
        await receipt(store, "history", 12)
        await receipt(store, "current", 30)
        await store.admit_messages(["current"], turn_id="active", provider_session_id=None)
        await boundary.advance_active_turn_through_seq("sp-1", "ch-1", 12)
        # Candidate N is exact history evidence and cannot prove > N.
        assert await boundary.get_active_turn_through_seq("sp-1", "ch-1") == 12
    await visible("candidate-ceiling", candidate_ceiling)

    async def trusted_baseline(store, boundary):
        await store.set_context_baseline("sp-1", "ch-1", 10)
        await receipt(store, "old-pending", 5)
        await receipt(store, "current", 30)
        await store.admit_messages(["current"], turn_id="active", provider_session_id=None)
        # Retained pending/history rows at or below baseline never block again.
        assert await boundary.get_active_turn_through_seq("sp-1", "ch-1") == 30
    await visible("trusted-baseline", trusted_baseline)


class _ContinuationAdapter(Adapter):
    def __init__(self):
        super().__init__()
        self.continuations = []

    def register_continuation_callback(
        self, callback, planning_cycle_key="", *, channel_id="",
        tool_names=(), tool_arguments=None, correlation_receipt="",
    ):
        self.continuations.append((
            callback, planning_cycle_key, channel_id, tool_names,
            tool_arguments, correlation_receipt,
        ))

    async def admit_continuation(self):
        callback, key, *_ = self.continuations[0]
        await callback(ProviderAdmissionEvent(
            planning_cycle_key=key, provider_session_id="provider-1",
            provider_turn_id="provider-turn", tool_call_id="tool-call-history",
            admitted_at=datetime.now(timezone.utc),
        ))


def _visible_read_runtime(store, tmp_path):
    adapter = _ContinuationAdapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn"
    runtime.active.provider_session_id = "provider-1"
    runtime.active.provider_turn_id = "provider-turn"
    runtime.active.routes[:] = [
        MessageRoute("history-2", "channel", "sp-1", "ch-1"),
    ]
    boundary = ActiveBoundaryAdapter(store, runtime.active)
    return adapter, runtime, boundary


async def _stage_visible_read(runtime, boundary, adapter, caplog):
    result = await runtime.stage_model_visible_read(
        space_id="sp-1", channel_id="ch-1", through_seq=2,
        through_envelope_id="history-2", tool_name="get_channel_history",
        tool_arguments={"channel": "ch-1"}, visible_message_ids=["history-2"],
    )
    assert result["state"] == "staged"
    assert await boundary.get_active_turn_through_seq("sp-1", "ch-1") is None
    assert runtime.active.visible_message_ids == []
    assert adapter.continuations[0][3] == ("get_channel_history",)
    assert adapter.continuations[0][4] == {"channel": "ch-1"}
    assert adapter.continuations[0][5] == result["correlation_receipt"]
    staged_events = runtime_events(caplog)
    assert [event["event"] for event in staged_events].count("history.read_staged") == 1
    assert not any(event["event"] == "history.read_admitted" for event in staged_events)
    return result


async def _admit_and_assert_visible_read(runtime, boundary, adapter, caplog):
    await adapter.admit_continuation()
    assert runtime.active.visible_message_ids == ["history-2"]
    assert await boundary.get_active_turn_through_seq("sp-1", "ch-1") == 2
    await adapter.admit_continuation()
    assert runtime.active.visible_message_ids == ["history-2"]
    assert await boundary.get_active_turn_through_seq("sp-1", "ch-1") == 2
    history_events = [
        event for event in runtime_events(caplog) if event["event"].startswith("history.")
    ]
    assert [event["event"] for event in history_events] == [
        "history.read_staged", "history.read_admitted",
    ]
    assert history_events[0]["correlation_key"] == history_events[1]["correlation_key"]
    assert history_events[1]["provider_turn_id"] == "provider-turn"
    assert history_events[1]["tool_call_id"] == "tool-call-history"


async def _assert_visible_read_send_correlation(runtime, boundary, caplog):
    class BoundaryCoordinator:
        async def send(self, _request):
            seen_seq = await boundary.get_active_turn_through_seq("sp-1", "ch-1")
            return {"state": "held", "seen_seq": seen_seq, "latest_seq": 3}

    delegate = TrackingSendDelegate(BoundaryCoordinator(), SendAttemptState(), runtime=runtime)
    await delegate.send({"destination": "ch-1"})
    correlated = [
        event for event in runtime_events(caplog)
        if event["event"] in {"history.read_staged", "history.read_admitted", "send.attempted"}
    ]
    assert [event["event"] for event in correlated] == [
        "history.read_staged", "history.read_admitted", "send.attempted",
    ]
    attempted = correlated[-1]
    assert attempted["turn_id"] == "turn"
    assert attempted["provider_session_id"] == "provider-1"
    assert attempted["provider_turn_id"] == "provider-turn"
    assert attempted["space_id"] == "sp-1"
    assert attempted["channel_id"] == "ch-1"


async def _assert_invalid_visible_watermark(runtime, caplog):
    with pytest.raises(RuntimeError, match="does not match local storage"):
        await runtime.stage_model_visible_read(
            space_id="sp-1", channel_id="ch-1", through_seq=3,
            through_envelope_id="history-2", tool_name="get_channel_history",
            tool_arguments={"channel": "ch-1"},
        )
    assert any(
        event["event"] == "history.read_staged"
        and event.get("latest_seq") == 3
        and event.get("state") == "invalid_watermark"
        for event in runtime_events(caplog)
    )


@pytest.mark.asyncio
async def test_model_visible_read_advances_only_after_exact_tool_result_admission(
    tmp_path, caplog,
):
    caplog.set_level(logging.DEBUG, logger="puffo_agent.agent.global_inbox_runtime")
    store = await make_store(tmp_path)
    await receipt(store, "history-2", 2)
    adapter, runtime, boundary = _visible_read_runtime(store, tmp_path)
    await _stage_visible_read(runtime, boundary, adapter, caplog)
    await _admit_and_assert_visible_read(runtime, boundary, adapter, caplog)
    await _assert_visible_read_send_correlation(runtime, boundary, caplog)
    await _assert_invalid_visible_watermark(runtime, caplog)
    await store.close()


@pytest.mark.asyncio
async def test_initial_admission_visibility_is_exact_deduplicated_and_memory_only(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "initial-visible-1", 1)
    await receipt(store, "initial-visible-2", 2)
    rows = tuple(await store.get_pending())
    runtime = GlobalInboxRuntime(
        store=store, adapter=Adapter(), run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    planned = PlannedTurn(
        turn_id="initial-visible-turn", planning_cycle_key="initial-visible-key",
        message_ids=("initial-visible-1", "initial-visible-2"), items=rows,
        routes=tuple(route_for(row) for row in rows), targets=(), pending_targets=(),
        target_summary="", formatted_blocks=(), provider_input="", formatted_tokens=0,
        wrapper_overhead_tokens=0, formatted_bytes=0, wrapper_overhead_bytes=0,
    )
    await runtime._admit(planned, ProviderAdmissionEvent(
        planning_cycle_key="initial-visible-key", provider_session_id="provider-1",
        provider_turn_id="provider-turn", admitted_at=datetime.now(timezone.utc),
    ))
    assert runtime.active.message_ids == ["initial-visible-1", "initial-visible-2"]
    assert runtime.active.visible_message_ids == ["initial-visible-1", "initial-visible-2"]
    await runtime._add_visible_message_ids([
        "initial-visible-2", "initial-visible-1", "initial-visible-2",
    ])
    assert runtime.active.visible_message_ids == ["initial-visible-1", "initial-visible-2"]
    with pytest.raises(RuntimeError, match="does not resolve locally"):
        await runtime._add_visible_message_ids(["initial-visible-1", "unknown-visible"])
    assert runtime.active.visible_message_ids == ["initial-visible-1", "initial-visible-2"]
    runtime.active.clear()
    assert runtime.active.visible_message_ids == []
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_ids", [
    ["unknown-visible"], ["visible-dm"], ["visible-cross-channel"],
    ["visible-beyond-watermark"],
])
async def test_model_visible_read_rejects_invalid_local_ids_before_callback_registration(
    tmp_path, visible_ids,
):
    store = await make_store(tmp_path)
    await receipt(store, "visible-watermark", 2)
    await receipt(store, "visible-cross-channel", 1, channel="other-channel")
    await receipt(store, "visible-beyond-watermark", 3)
    await receipt(store, "visible-dm", 1, kind="dm", channel="", space="")

    class RecordingAdapter(Adapter):
        def __init__(self):
            super().__init__()
            self.callbacks = []

        def register_continuation_callback(self, *args, **kwargs):
            self.callbacks.append((args, kwargs))

    adapter = RecordingAdapter()
    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "visible-validation-turn"
    runtime.active.provider_session_id = "provider-1"
    with pytest.raises(RuntimeError, match="visible message ID"):
        await runtime.stage_model_visible_read(
            space_id="sp-1", channel_id="ch-1", through_seq=2,
            through_envelope_id="visible-watermark",
            tool_name="get_channel_history", tool_arguments={"channel": "ch-1"},
            visible_message_ids=visible_ids,
        )
    assert adapter.callbacks == []
    assert runtime.active.visible_message_ids == []
    assert runtime.active.through_by_channel == {}
    await store.close()


@pytest.mark.asyncio
async def test_model_visible_read_admits_at_runtime_tool_return(tmp_path, caplog):
    caplog.set_level(
        logging.DEBUG,
        logger="puffo_agent.agent.global_inbox_runtime",
    )
    store = await make_store(tmp_path)
    await receipt(store, "history-tool-return", 7)
    adapter = ToolReturnAdapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn-tool-return"
    runtime.active.provider_session_id = adapter.session
    runtime.active.provider_turn_id = "provider-turn-tool-return"

    result = await runtime.stage_model_visible_read(
        space_id="sp-1",
        channel_id="ch-1",
        through_seq=7,
        through_envelope_id="history-tool-return",
        tool_name="get_channel_history",
        tool_arguments={"channel": "ch-1"},
        visible_message_ids=["history-tool-return"],
    )

    assert result["state"] == "admitted"
    assert runtime.active.through_by_channel[("sp-1", "ch-1")] == 7
    assert runtime.active.visible_message_ids == ["history-tool-return"]
    history_events = [
        event for event in runtime_events(caplog)
        if event["event"].startswith("history.")
    ]
    assert [event["event"] for event in history_events] == [
        "history.read_staged", "history.read_admitted",
    ]
    assert history_events[0]["state"] == "tool_return"
    assert history_events[1]["provider_turn_id"] == (
        "provider-turn-tool-return"
    )
    await store.close()


@pytest.mark.asyncio
async def test_coordinator_exception_is_tracked_and_re_raised(caplog):
    caplog.set_level(
        logging.DEBUG,
        logger="puffo_agent.agent.global_inbox_runtime",
    )

    class Coordinator:
        async def send(self, *_args, **_kwargs):
            raise RuntimeError("coordinator failed")

    from puffo_agent.agent.global_inbox_runtime import SendAttemptState
    attempts = SendAttemptState()
    delegate = TrackingSendDelegate(Coordinator(), attempts)
    with pytest.raises(RuntimeError, match="coordinator failed"):
        await delegate.send({"destination": "ch-failed"})

    assert attempts.states == ["failed"]
    assert [event["event"] for event in runtime_events(caplog)] == [
        "send.attempted",
        "send.failed",
    ]
    assert runtime_events(caplog)[-1]["error_category"] == "delegate_exception"


@pytest.mark.asyncio
async def test_send_events_use_only_active_resolved_route(caplog, tmp_path):
    caplog.set_level(
        logging.DEBUG,
        logger="puffo_agent.agent.global_inbox_runtime",
    )

    class Coordinator:
        async def send(self, request=None, **_kwargs):
            return {"state": "sent", "envelope_id": "sent", "seq": 9}

    runtime = GlobalInboxRuntime(
        store=await make_store(tmp_path),
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.routes[:] = [
        MessageRoute("incoming", "channel", "sp-1", "resolved-channel"),
    ]
    delegate = TrackingSendDelegate(
        Coordinator(), SendAttemptState(), runtime=runtime,
    )
    await delegate.send({"destination": "resolved-channel"})
    await delegate.send({"destination": "model-only-destination"})

    attempted = [
        event for event in runtime_events(caplog)
        if event["event"] == "send.attempted"
    ]
    assert attempted[0]["space_id"] == "sp-1"
    assert attempted[0]["channel_id"] == "resolved-channel"
    assert "channel_id" not in attempted[1]
    assert "space_id" not in attempted[1]
    await runtime.store.close()


@pytest.mark.asyncio
async def test_coordinator_worker_host_context_shares_failed_held_attachment_attempts(
    tmp_path, monkeypatch,
):
    from puffo_agent.agent.global_inbox_runtime import SendAttemptState
    from puffo_agent.portal.worker import Worker
    import puffo_agent.portal.state as state_mod

    class Coordinator:
        async def send(self, request=None, **_kwargs):
            destination = request.get("destination", "")
            return {"state": "held" if destination == "held" else "failed"}

    attempts = SendAttemptState()
    delegate = TrackingSendDelegate(Coordinator(), attempts)
    client = SimpleNamespace(
        slug="agent",
        operator_slug="operator",
        keystore=object(),
        http=object(),
        send_delegate=delegate,
    )
    worker = Worker.__new__(Worker)
    worker._client = client
    worker.agent_cfg = SimpleNamespace(
        id="agent-id", runtime=SimpleNamespace(harness="claude-code"),
    )
    monkeypatch.setattr(state_mod, "agent_home_dir", lambda _id: tmp_path)
    context = worker.host_mcp_context()
    assert context.send_coordinator is delegate
    await context.send_coordinator.send({
        "destination": "failed", "text": "text",
    })
    await context.send_coordinator.send({
        "destination": "held", "attachment_paths": ["/tmp/file"],
    })
    assert context.send_coordinator.attempts is attempts
    assert attempts.states == ["failed", "held"]


@pytest.mark.asyncio
async def test_crash_join_mismatched_or_stateless_session_requeues(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "m1", 1)
    await store.admit_messages(
        ["m1"], turn_id="turn-1", provider_session_id="old-session",
    )
    path = tmp_path / ".puffo-agent/current_turn.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "version": 2, "turn_id": "turn-1", "message_ids": ["m1"],
        "targets": [["channel", "sp-1", "ch-1"]], "routes": [],
    }))
    adapter = Adapter()
    adapter.session = None
    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=lambda _p: None,
        workspace=tmp_path,
    )
    assert not await runtime.recover_current_turn()
    assert [m.envelope_id for m in await store.get_pending()] == ["m1"]
    assert not path.exists()
    await store.close()


@pytest.mark.asyncio
async def test_driver_recovery_abandons_before_exact_union_replacement(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "first", 1, sender="alice")
    await receipt(store, "second", 2, sender="bob")

    class RecoveryAdapter(Adapter):
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
                provider_turn_id="provider-turn",
                tool_call_id="tool-recovery",
                admitted_at=datetime.now(timezone.utc),
            ))

    seed_adapter = RecoveryAdapter()
    seed = GlobalInboxRuntime(
        store=store, adapter=seed_adapter, run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    planned = await seed.plan_pending()
    assert planned is not None
    seed._write_current_turn(planned)
    seed_adapter.register_admission_callback(
        lambda event: seed._admit(planned, event),
        planned.planning_cycle_key,
    )
    await seed_adapter.admit()
    page = await seed.read_inbox(limit=2, tool_arguments={"limit": 2})
    assert len(page["messages"]) == 2
    await seed_adapter.admit_continuation()
    persisted = json.loads(seed.current_turn_path.read_text(encoding="utf-8"))
    assert persisted["message_ids"] == ["first", "second"]

    outbox = RuntimeEventOutbox(tmp_path / "state" / "runtime_events.db")
    outbox.set_active_turn(
        "public_old_turn", session_ref="logical_session",
        native_session_id=str(seed_adapter.session),
    )
    crashed_adapter = Adapter()
    crashed_adapter.session = None
    runtime = GlobalInboxRuntime(
        store=store, adapter=crashed_adapter,
        run_turn=lambda _planned: None, workspace=tmp_path,
        agent_id="agent", runtime_event_outbox=outbox,
    )
    assert not await runtime.recover_current_turn()
    rows = outbox.prefix()
    assert len(rows) == 1
    assert rows[0].event["type"] == "turn.finished"
    assert rows[0].event["payload"]["outcome"] == "abandoned"
    assert [item.envelope_id for item in await store.get_pending()] == [
        "first", "second",
    ]
    await outbox.enqueue(RuntimeEvent(
        agent_id="agent", session_ref="logical_session",
        turn_ref="public_replacement_turn", type="turn.started", payload={},
    ))
    rows = outbox.prefix()
    assert rows[0].sequence < rows[1].sequence
    assert rows[1].event["turn_ref"] == "public_replacement_turn"
    assert rows[0].event["payload"]["outcome"] != "cancelled"
    outbox.close()
    await store.close()


class ScriptedContext:
    def __init__(self, adapter, outcomes, on_replan=None):
        self.adapter = adapter
        self.outcomes = list(outcomes)
        self.on_replan = on_replan
        self.calls = 0
        self.snapshot = ContextSnapshot(
            0, 200_000, "scripted", datetime.now(timezone.utc),
        )

    async def decide(self, candidate, replan):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        replacement = candidate
        if outcome is DecisionOutcome.REPLAN:
            if self.on_replan is not None:
                await self.on_replan()
            replacement = await replan(candidate)
        if outcome is DecisionOutcome.ROLLOVER:
            self.adapter.session = "rolled-session"
        return ContextDecision(
            outcome=outcome,
            candidate=replacement,
            snapshot=self.snapshot,
            projected_tokens=replacement.projected_tokens,
            diagnostic=outcome.value,
        )


class RetryingRunner:
    def __init__(self, adapter, retry_effects):
        self.adapter = adapter
        self.retry_effects = list(retry_effects)
        self.initial_calls = 0
        self.retry_calls = 0

    async def __call__(self, _planned):
        self.initial_calls += 1
        await self.adapter.admit()
        raise AgentAPIError("rate limit", is_auth=False)

    async def handle_global_inbox_retry(self, _planned):
        self.retry_calls += 1
        effect = self.retry_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect


@pytest.mark.asyncio
async def test_retry_core_uses_exact_provider_input_without_duplicate_log_append(
    tmp_path,
):
    captured = {}

    class RetryAdapter:
        async def run_retry_turn(self, kick, fallback, ctx):
            captured.update(kick=kick, fallback=fallback, messages=list(ctx.messages))
            return TurnResult(
                reply="[SILENT]",
                metadata={"assistant_text_parts": ["[SILENT]"]},
            )

    planned = SimpleNamespace(provider_input="<exact-global-input>")
    agent = PuffoAgent(
        adapter=RetryAdapter(),
        system_prompt="system",
        memory_dir=str(tmp_path / "memory"),
    )
    agent.log.append({"role": "user", "content": "<exact-global-input>"})
    before = list(agent.log)
    await agent.handle_global_inbox_retry(planned)
    assert agent.log == before
    assert captured["fallback"] == "<exact-global-input>"
    assert captured["messages"] == before


@pytest.mark.asyncio
async def test_global_turn_plain_output_is_internal_not_an_implicit_message(tmp_path):
    class PlainAdapter:
        async def run_turn(self, _ctx):
            return TurnResult(
                reply="No further reply is needed.",
                metadata={"assistant_text_parts": ["No further reply is needed."]},
            )

    planned = SimpleNamespace(
        provider_input="<exact-global-input>",
        targets=(object(),),
    )
    agent = PuffoAgent(
        adapter=PlainAdapter(),
        system_prompt="system",
        memory_dir=str(tmp_path / "memory"),
    )

    reply = await agent.handle_global_inbox_turn(planned)

    assert reply is None
    assert all(entry["role"] != "assistant" for entry in agent.log)


@pytest.mark.asyncio
async def test_global_retry_plain_output_is_internal_not_an_implicit_message(tmp_path):
    class PlainRetryAdapter:
        async def run_retry_turn(self, _kick, _fallback, _ctx):
            return TurnResult(
                reply="No further reply is needed.",
                metadata={"assistant_text_parts": ["No further reply is needed."]},
            )

    planned = SimpleNamespace(provider_input="<exact-global-input>")
    agent = PuffoAgent(
        adapter=PlainRetryAdapter(),
        system_prompt="system",
        memory_dir=str(tmp_path / "memory"),
    )
    agent.log.append({"role": "user", "content": "<exact-global-input>"})

    reply = await agent.handle_global_inbox_retry(planned)

    assert reply is None
    assert all(entry["role"] != "assistant" for entry in agent.log)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        AgentAPIError("auth", is_auth=True),
        RuntimeError("unsafe"),
        AgentAPIError("rate", is_auth=False),
    ],
)
async def test_admission_retry_auth_unsafe_or_exhaustion_requeues(
    tmp_path, error,
):
    store = await make_store(tmp_path)
    await receipt(store, "retry", 1)
    adapter = Adapter()
    if isinstance(error, AgentAPIError) and error.is_auth:
        class Runner(RetryingRunner):
            async def __call__(self, _planned):
                await self.adapter.admit()
                raise error
        runner = Runner(adapter, [])
    elif isinstance(error, RuntimeError):
        class Runner(RetryingRunner):
            async def __call__(self, _planned):
                await self.adapter.admit()
                raise error
        runner = Runner(adapter, [])
    else:
        runner = RetryingRunner(adapter, [error, error])
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=runner,
        workspace=tmp_path,
        max_api_retries=2,
        retry_sleep=lambda _delay: asyncio.sleep(0),
    )
    await runtime.process_once()
    assert [row.envelope_id for row in await store.get_pending()] == ["retry"]
    assert not runtime.current_turn_path.exists()
    await store.close()


@pytest.mark.asyncio
async def test_silence_without_correlated_admission_degrades_without_self_wake(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(store, "silent", 1)
    adapter = Adapter()
    calls = 0

    async def run(_planned):
        nonlocal calls
        calls += 1
        return None

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run, workspace=tmp_path,
    )
    await runtime.process_once()
    assert runtime.health.state == "degraded"
    assert [row.envelope_id for row in await store.get_pending()] == ["silent"]
    assert calls == 1
    assert not runtime.current_turn_path.exists()
    await store.close()


@pytest.mark.asyncio
async def test_held_watermark_sync_proof_returns_local_semantic_rows_without_admission(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(store, "initial", 1)
    await store.admit_messages(
        ["initial"], turn_id="turn", provider_session_id="provider-1",
    )
    adapter = Adapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn"
    runtime.active.message_ids[:] = ["initial"]
    runtime.active.provider_session_id = "provider-1"
    source = HeldRecoverySource(runtime, wait_timeout_s=0.5)
    waiter = asyncio.create_task(
        source.wait_for_held_delivery("sp-1", "ch-1", 3, "watermark"),
    )
    source.notify_delivery()
    await asyncio.sleep(0)
    await receipt(store, "watermark", 3)
    source.notify_delivery()
    assert await waiter

    rows = await source.query_held_messages(
        "sp-1", "ch-1", 3, "watermark", "provider-1",
    )
    assert [row["envelope_id"] for row in rows] == ["watermark"]
    assert rows[0]["content"] == "text-watermark"
    assert rows[0]["thread_root_id"] == ""
    assert [row.envelope_id for row in await store.get_pending()] == ["watermark"]
    staged = runtime.held[("sp-1", "ch-1")]
    assert staged.synchronized
    assert staged.message_ids == ("watermark",)
    assert await ActiveBoundaryAdapter(
        store, runtime.active
    ).get_active_turn_through_seq("sp-1", "ch-1") == 1
    await store.close()


@pytest.mark.asyncio
async def test_terminal_held_watermark_is_valid_sync_evidence(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "initial", 1)
    await store.admit_messages(
        ["initial"], turn_id="turn", provider_session_id="provider-1"
    )
    await receipt(
        store,
        "terminal-watermark",
        2,
        disposition=ReceiptDisposition.TERMINAL,
    )
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn"
    runtime.active.provider_session_id = "provider-1"

    rows = await runtime.held_recovery_source.query_held_messages(
        "sp-1", "ch-1", 2, "terminal-watermark", "provider-1"
    )

    assert [row["envelope_id"] for row in rows] == ["terminal-watermark"]
    assert "content" not in rows[0]
    assert runtime.held[("sp-1", "ch-1")].synchronized
    assert runtime.held[("sp-1", "ch-1")].message_ids == ()
    await store.close()


@pytest.mark.asyncio
async def test_held_sync_proof_cannot_mutate_a_replacement_turn(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "initial", 1)
    await receipt(store, "held", 2)
    await store.admit_messages(
        ["initial"], turn_id="old-turn", provider_session_id="provider-1",
    )
    adapter = Adapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "old-turn"
    runtime.active.message_ids[:] = ["initial"]
    runtime.active.provider_session_id = "provider-1"
    runtime.active.provider_turn_id = "old-provider-turn"
    rows = await runtime.held_recovery_source.query_held_messages(
        "sp-1", "ch-1", 2, "held", "provider-1",
    )
    assert [row["envelope_id"] for row in rows] == ["held"]

    runtime.active.clear()
    runtime.active.turn_id = "new-turn"
    runtime.active.provider_session_id = "provider-2"
    runtime.active.provider_turn_id = "new-provider-turn"

    assert runtime.active.turn_id == "new-turn"
    assert runtime.active.message_ids == []
    assert [row.envelope_id for row in await store.get_pending()] == ["held"]
    assert [
        row.envelope_id
        for row in await store.get_in_turn_messages(
            "old-turn", "provider-1"
        )
    ] == ["initial"]
    assert not runtime.current_turn_path.exists()
    await store.close()


@pytest.mark.asyncio
async def test_held_timeout_mismatch_and_context_pressure_stage_nothing(
    tmp_path,
):
    store = await make_store(tmp_path)
    adapter = Adapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn"
    runtime.active.provider_session_id = "provider-1"
    source = HeldRecoverySource(runtime, wait_timeout_s=0.01)
    assert not await source.wait_for_held_delivery("sp", "ch", 9, "missing")
    assert await source.query_held_messages(
        "sp", "ch", 9, "missing", None,
    ) == ()
    assert runtime.held[("sp", "ch")].message_ids == ()
    await receipt(store, "rejected", 10)
    runtime.active.provider_session_id = "provider-1"
    metadata = await source.query_held_messages(
        "sp-1", "ch-1", 10, "rejected", "provider-1",
    )
    assert metadata
    staged = runtime.held[("sp-1", "ch-1")]
    assert staged.synchronized is True
    assert staged.message_ids == ("rejected",)
    assert [row.envelope_id for row in await store.get_pending()] == ["rejected"]
    await store.close()


@pytest.mark.asyncio
async def test_held_timeout_uses_signed_pending_catchup_before_failing(tmp_path):
    store = await make_store(tmp_path)

    async def catchup(envelope_id):
        assert envelope_id == "late-watermark"
        await receipt(store, envelope_id, 9)
        return True

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        held_catchup=catchup,
    )
    source = HeldRecoverySource(
        runtime,
        wait_timeout_s=0,
        catchup_pending=catchup,
    )
    assert await source.wait_for_held_delivery(
        "sp-1", "ch-1", 9, "late-watermark",
    )
    assert runtime.held == {}
    await store.close()


@pytest.mark.asyncio
async def test_ordinary_human_channel_sender_projects_as_human(
    monkeypatch, tmp_path,
):
    """An ordinary human in a channel used to reach the model as
    ``sender_type: unknown`` — only the operator was ever classified
    human, so the model could not tell a teammate from an unidentified
    account. The space roster's server-supplied ``identity_type`` is the
    authenticated fact that fixes it."""
    from puffo_agent.agent.core import _user_metadata_lines

    async def setup(client, _store, _events, _delivery):
        async def members(_space_id):
            return {"alice": "human", "helper-bot": "agent"}

        client._get_space_members = members

    _client, store, _events, _delivery, _ = await listen_delivery(
        monkeypatch,
        tmp_path,
        payload=payload_for("human-1", sender="alice"),
        seq=31,
        setup=setup,
    )
    pending = await store.get_pending()
    content = pending[0].content
    assert content["sender_type"] == "human"
    assert not content["sender_owner_slug"]
    assert not content["is_from_operator"]

    lines = _user_metadata_lines(
        channel_name="Channel", channel_id="ch-1", root_id="", post_id="human-1",
        space_id="sp-1", space_name="Space", create_at=1, sender="alice",
        sender_display_name=content["sender_display_name"],
        sender_is_agent=content["sender_is_agent"],
        sender_owner_slug=content["sender_owner_slug"],
        sender_type=content.get("sender_type"),
        is_from_operator=content["is_from_operator"],
        is_encrypted=True,
    )
    assert "- sender_type: human" in lines
    await store.close()
