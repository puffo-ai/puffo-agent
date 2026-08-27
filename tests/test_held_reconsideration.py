from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from puffo_agent.agent.send_coordinator import (
    CHANNEL_SEND_PATH,
    KEYLESS_CHANNEL_SEND_PATH,
    _MAX_HELD_RECORDS,
    SemanticSendRequest,
    SendCoordinator,
    _HeldEvidence,
)
from puffo_agent.agent.shared_content import HELD_SEND_RECONSIDERATION_GUIDANCE


async def _bridge_held_runtime(tmp_path):
    from puffo_agent.agent.global_inbox_runtime import (
        ActiveBoundaryAdapter,
        GlobalInboxRuntime,
    )
    from .test_cloud_bridge_msgflow import FakeBridge, _bridge_client
    from .test_global_inbox_runtime import ToolReturnAdapter

    bridge = FakeBridge()
    client = _bridge_client(tmp_path, bridge, db="held-bridge.db")
    base = {
        "type": "message",
        "sender_slug": "alice-0001",
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "plaintext": "context",
    }
    await client._dispatch_bridge_frame({
        **base, "seq": 2, "envelope_id": "already-admitted",
    })
    await client.store.admit_messages(
        ["already-admitted"],
        turn_id="turn-a",
        provider_session_id="session-a",
    )
    adapter = ToolReturnAdapter()
    adapter.session = "session-a"
    runtime = GlobalInboxRuntime(
        store=client.store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn-a"
    runtime.active.message_ids[:] = ["already-admitted"]
    runtime.active.provider_session_id = "session-a"
    client.global_runtime = runtime
    boundary = ActiveBoundaryAdapter(client.store, runtime.active)
    return client, runtime, boundary, base


async def _bridge_held_coordinator(client, runtime, boundary):
    cfg, http, unused_store = _setup_keyless()
    freshness = Freshness(baseline=2)
    coordinator = SendCoordinator(
        slug=cfg.slug, keystore=cfg.keystore, http_client=http,
        data_client=client.store, baseline_source=freshness,
        active_turn_source=boundary,
        held_recovery_source=runtime.held_recovery_source,
        provider_session_id="session-a",
    )
    calls = []

    async def post_unsigned(path, body=None):
        calls.append((path, body))
        if len(calls) == 1:
            return {
                "state": "held", "seen_seq": body["freshness"]["seen_seq"],
                "context_baseline_seq": body["freshness"]["context_baseline_seq"],
                "latest_seq": 5, "latest_envelope_id": "held-exact",
            }
        return {
            "state": "sent", "envelope_id": "explicit-retry", "seq": 7,
            "replay": False, "missing_devices": [],
            "freshness": {
                **body["freshness"],
                "latest_seq_before_send": body["freshness"]["seen_seq"],
            },
        }

    http.post_unsigned = post_unsigned
    return coordinator, calls, unused_store


async def _drive_exact_bridge_pair(client, coordinator, calls, base):
    held_task = asyncio.create_task(coordinator.send(
        SemanticSendRequest(destination="ch_1", text="held draft")
    ))
    while not calls:
        await asyncio.sleep(0)
    for frame in (
        {**base, "seq": 4, "envelope_id": "unrelated"},
        {**base, "envelope_id": "legacy"},
        {**base, "seq": 6, "envelope_id": "wrong-envelope"},
    ):
        await client._dispatch_bridge_frame(frame)
    await asyncio.sleep(0)
    assert not held_task.done()
    assert len(calls) == 1
    assert await client.store.get_message_by_envelope("held-exact") is None
    await client._dispatch_bridge_frame({
        **base, "seq": 5, "envelope_id": "held-exact",
    })
    held = await held_task
    assert held["state"] == "held"
    assert held["synchronized"] is True
    assert len(calls) == 1
    exact = await client.store.get_message_by_envelope("held-exact")
    assert exact is not None and exact.server_seq == 5


async def _assert_bridge_reconsideration(runtime, coordinator, boundary, calls):
    # Unchanged draft, so the probe reaches the admission gate rather than
    # stopping at the draft-identity gate.
    staged = await coordinator.send(SemanticSendRequest(
        destination="ch_1", text="held draft", send_anyway=True,
    ))
    assert staged["error_kind"] == "reconsideration_ineligible"
    assert staged["_reconsideration_audit"]["decision_reason"] == (
        "admission_before_held"
    )
    assert len(calls) == 1
    read = await runtime.stage_model_visible_read(
        space_id="sp_1", channel_id="ch_1", through_seq=5,
        through_envelope_id="held-exact", tool_name="get_channel_history",
        tool_arguments={"channel": "ch_1"},
    )
    assert read["state"] == "admitted"
    assert await boundary.get_active_turn_through_seq("sp_1", "ch_1") == 2
    sent = await coordinator.send(SemanticSendRequest(
        destination="ch_1", text="held draft", send_anyway=True,
    ))
    assert sent["state"] == "failed"
    assert sent["_reconsideration_audit"]["decision_reason"] == (
        "admission_before_held"
    )
    assert len(calls) == 1
    assert calls[-1][1]["freshness"] == {
        "context_baseline_seq": 2, "seen_seq": 2, "mode": "require_current",
    }


@pytest.mark.asyncio
async def test_bridge_releases_held_wait_only_after_exact_durable_pair(tmp_path):
    client, runtime, boundary, base = await _bridge_held_runtime(tmp_path)
    coordinator, calls, unused_store = await _bridge_held_coordinator(
        client, runtime, boundary,
    )
    await _drive_exact_bridge_pair(client, coordinator, calls, base)
    await _assert_bridge_reconsideration(runtime, coordinator, boundary, calls)
    await unused_store.close()
    await client.store.close()

from .test_puffo_core_tools import _setup_keyless
from .test_send_coordinator import Freshness, coordinator_fixture


class Recovery:
    def __init__(self, rows=None, available=True):
        self.rows = rows or []
        self.available = available
        self.waits = []
        self.queries = []

    async def wait_for_held_delivery(self, space_id, channel_id, seq, envelope_id):
        self.waits.append((space_id, channel_id, seq, envelope_id))
        return self.available

    async def query_held_messages(
        self, space_id, channel_id, seq, envelope_id, provider_session_id,
    ):
        self.queries.append((space_id, channel_id, seq, envelope_id, provider_session_id))
        return self.rows


def exact_row(
    *, seq=5, envelope_id="msg_latest", session_id="session-a",
):
    return {
        "envelope_id": envelope_id,
        "server_seq": seq,
        "latest_seq": seq,
        "latest_envelope_id": envelope_id,
        "provider_session_id": session_id,
    }


def bind_turn(coordinator, freshness, *, boundary=2):
    coordinator.provider_session_id = "session-a"
    freshness.active = SimpleNamespace(
        turn_id="turn-a", provider_session_id="session-a"
    )
    coordinator.held_recovery_source = Recovery(rows=[exact_row()])

    async def active_boundary(_space_id, _channel_id):
        return active_boundary.value

    active_boundary.value = boundary
    freshness.get_active_turn_through_seq = active_boundary
    return active_boundary


def basis_row(envelope_id, *, seq, thread_root_id=""):
    return SimpleNamespace(
        space_id="sp_1", channel_id="ch_a", thread_root_id=thread_root_id,
        envelope_id=envelope_id, server_seq=seq, sender_slug="alice",
        envelope_kind="channel", sent_at=1_700_000_000_000,
        is_encrypted=False, content="context",
    )


def attach_visible_basis(recovery, rows):
    """Give the recovery source the runtime shape ``_visible_draft_basis`` reads."""
    by_id = {row.envelope_id: row for row in rows}

    async def get_message_by_envelope(envelope_id):
        return by_id.get(envelope_id)

    recovery.runtime = SimpleNamespace(
        active=SimpleNamespace(visible_message_ids=tuple(by_id)),
        store=SimpleNamespace(get_message_by_envelope=get_message_by_envelope),
    )
    return recovery


def held_response(body, *, latest_seq=5, latest_envelope_id="msg_latest"):
    return {
        "state": "held",
        "envelope_id": body["envelope"]["envelope_id"],
        "context_baseline_seq": body["freshness"]["context_baseline_seq"],
        "seen_seq": body["freshness"]["seen_seq"],
        "latest_seq": latest_seq,
        "latest_envelope_id": latest_envelope_id,
    }


@pytest.mark.asyncio
async def test_held_recovery_keeps_channel_wide_terminal_pair_for_thread_send():
    """A thread draft still needs the channel's actual latest pair."""
    coordinator, freshness, _http = await coordinator_fixture()
    coordinator.provider_session_id = "session-a"
    freshness.active = SimpleNamespace(
        turn_id="turn-a", provider_session_id="session-a"
    )
    recovery = Recovery(rows=[
        {
            **exact_row(envelope_id="other-thread-latest"),
            "thread_root_id": "other-root", "content": "other thread",
        },
        {
            "envelope_id": "requested-root", "server_seq": 4,
            "latest_seq": 5, "latest_envelope_id": "other-thread-latest",
            "provider_session_id": "session-a", "thread_root_id": "",
            "content": "requested thread",
        },
    ])
    coordinator.held_recovery_source = recovery
    key = ("session-a", "turn-a", "sp_1", "ch_a")
    coordinator._held_evidence[key] = _HeldEvidence(
        latest_seq=5,
        latest_envelope_id="other-thread-latest",
        thread_root_id="requested-root",
    )

    assert await coordinator._recover_held(
        "sp_1", "ch_a", 5, "other-thread-latest"
    )
    assert [row["envelope_id"] for row in coordinator._held_evidence[key].recovered_messages] == [
        "other-thread-latest", "requested-root",
    ]


@pytest.mark.asyncio
async def test_held_thread_basis_overrides_only_its_presentation_target():
    coordinator, _freshness, _http = await coordinator_fixture()
    key = ("session-a", "turn-a", "sp_1", "ch_a")
    root = {
        "envelope_id": "thread-root", "server_seq": 4, "sent_at": 1_700_000_000_000,
        "sender_slug": "alice", "space_id": "sp_1", "channel_id": "ch_a",
        "thread_root_id": "", "envelope_kind": "channel", "content": "root",
    }
    reply = {**root, "envelope_id": "thread-reply", "server_seq": 5,
             "thread_root_id": "thread-root", "content": "reply"}
    unrelated = {**root, "envelope_id": "other-route", "server_seq": 6,
                 "channel_id": "ch_other", "thread_root_id": "other-root",
                 "content": "other"}
    coordinator._held_evidence[key] = _HeldEvidence(
        latest_seq=6, latest_envelope_id="other-route", synchronized=True,
        thread_root_id="thread-root", visible_draft_basis=[root, reply],
        recovered_messages=[root, reply, unrelated],
    )
    output = await coordinator._held_context_output(key, "sp_1", "ch_a")
    reconsideration = output["reconsideration"]
    assert reconsideration["context_version"] == 1
    assert reconsideration["guidance"] == HELD_SEND_RECONSIDERATION_GUIDANCE
    assert reconsideration["target"] == {
        "target_type": "thread",
        "target_ref": "channel:sp_1:ch_a:thread:thread-root",
        "space_id": "sp_1",
        "channel_id": "ch_a",
        "thread_root_id": "thread-root",
    }
    assert reconsideration["visible_draft_basis"].count(
        'target_ref="channel:sp_1:ch_a:thread:thread-root"'
    ) == 1
    latest = reconsideration["new_channel_context"]
    assert 'target_ref="channel:sp_1:ch_a"' in latest
    assert 'target_ref="channel:sp_1:ch_a:thread:thread-root"' in latest
    assert 'target_ref="channel:sp_1:ch_other:thread:other-root"' in latest
    assert latest.index('message_id="thread-root"') < latest.index(
        'message_id="thread-reply"'
    ) < latest.index('message_id="other-route"')


@pytest.mark.asyncio
async def test_held_context_makes_returned_participation_facts_explicit():
    coordinator, _freshness, http = await coordinator_fixture()
    http.responses["/spaces/sp_1/channels/ch_a/members"] = {
        "members": [
            {
                "slug": coordinator.slug,
                "identity_type": "agent",
            },
            {
                "slug": "peer-agent",
                "identity_type": "agent",
            },
            {
                "slug": "human-1",
                "identity_type": "human",
            },
        ]
    }
    key = ("session-a", "turn-a", "sp_1", "ch_a")
    origin = {
        "envelope_id": "origin",
        "server_seq": 1,
        "sent_at": 1_700_000_000_000,
        "sender_slug": "human-1",
        "sender_type": "human",
        "space_id": "sp_1",
        "channel_id": "ch_a",
        "envelope_kind": "channel",
        "content": "count",
    }
    peer = {
        **origin,
        "envelope_id": "peer-count",
        "server_seq": 2,
        "sender_slug": "peer-agent",
        "sender_type": "agent",
        "content": "1",
    }
    own = {
        **origin,
        "envelope_id": "own-count",
        "server_seq": 3,
        "sender_slug": coordinator.slug,
        "sender_type": "agent",
        "content": "2",
    }
    coordinator._held_evidence[key] = _HeldEvidence(
        latest_seq=3,
        latest_envelope_id="own-count",
        synchronized=True,
        visible_draft_basis=[origin, peer],
        recovered_messages=[own, peer],
    )

    output = await coordinator._held_context_output(key, "sp_1", "ch_a")

    assert output["reconsideration"]["participation_snapshot"] == {
        "current_agent_identity": f"@{coordinator.slug}",
        "current_agent_has_visible_message": True,
        "current_agent_visible_message_ids": ["own-count"],
        "other_visible_agent_count": 1,
        "other_visible_agent_identities": ["@peer-agent"],
        "channel_membership": {
            "context_ready": True,
            "member_count": 3,
            "agent_member_count": 2,
            "agent_member_identities": [
                f"@{coordinator.slug}",
                "@peer-agent",
            ],
            "current_agent_is_member": True,
        },
    }


async def _native_held_contract():
    coordinator, freshness, http = await coordinator_fixture(baseline=2, active=2)
    boundary = bind_turn(coordinator, freshness)
    calls, paths = [], []

    async def post(path, body):
        paths.append(path)
        calls.append(body)
        if len(calls) == 1:
            return held_response(body)
        return {
            "state": "sent", "envelope_id": body["envelope"]["envelope_id"],
            "seq": 6, "replay": False, "missing_devices": [],
            "freshness": {
                "mode": "send_anyway", "context_baseline_seq": 2,
                "seen_seq": 5, "latest_seq_before_send": 5,
            },
        }

    http.post = post
    return SimpleNamespace(
        coordinator=coordinator, freshness=freshness, boundary=boundary,
        calls=calls, paths=paths, destination="ch_a",
        expected_path=CHANNEL_SEND_PATH, store=None,
    )


async def _keyless_held_contract():
    cfg, http, store = _setup_keyless()
    await store.mark_channel_space("ch_abc", "sp_test")
    freshness = Freshness(baseline=2)
    freshness.active = SimpleNamespace(
        turn_id="turn-a", provider_session_id="session-a"
    )

    async def active_boundary(_space_id, _channel_id):
        return active_boundary.value

    active_boundary.value = 2
    freshness.get_active_turn_through_seq = active_boundary
    coordinator = SendCoordinator(
        slug=cfg.slug, keystore=cfg.keystore, http_client=http,
        data_client=store, baseline_source=freshness,
        active_turn_source=freshness,
        held_recovery_source=Recovery(rows=[exact_row()]),
        provider_session_id="session-a",
    )
    calls, paths = [], []

    async def post_unsigned(path, body=None):
        paths.append(path)
        calls.append(body)
        if len(calls) == 1:
            return {
                "state": "held", "seen_seq": body["freshness"]["seen_seq"],
                "context_baseline_seq": body["freshness"]["context_baseline_seq"],
                "latest_seq": 5, "latest_envelope_id": "msg_latest",
            }
        return {
            "state": "sent", "envelope_id": "keyless-sent", "seq": 6,
            "replay": False, "missing_devices": [],
            "freshness": {
                "mode": "send_anyway", "context_baseline_seq": 2,
                "seen_seq": 5, "latest_seq_before_send": 5,
            },
        }

    http.post_unsigned = post_unsigned
    return SimpleNamespace(
        coordinator=coordinator, freshness=freshness, boundary=active_boundary,
        calls=calls, paths=paths, destination="ch_abc",
        expected_path=KEYLESS_CHANNEL_SEND_PATH, store=store,
    )


async def _assert_initial_held_contract(case):
    held = await case.coordinator.send(SemanticSendRequest(
        destination=case.destination, text="draft",
    ))
    assert held["state"] == "held"
    assert held["latest_seq"] == 5
    assert held["latest_envelope_id"] == "msg_latest"
    assert held["synchronized"] is True
    # Unchanged draft, so the probe reaches the admission gate rather than
    # stopping at the draft-identity gate.
    staged = await case.coordinator.send(SemanticSendRequest(
        destination=case.destination, text="draft", send_anyway=True,
    ))
    assert staged["error_kind"] == "reconsideration_ineligible"
    assert staged["_reconsideration_audit"]["decision_reason"] == (
        "admission_before_held"
    )
    assert len(case.calls) == 1


async def _assert_wrong_turn_and_pair(case):
    case.boundary.value = 5
    case.freshness.active = SimpleNamespace(
        turn_id="wrong-turn", provider_session_id="session-a",
    )
    wrong_turn = await case.coordinator.send(SemanticSendRequest(
        destination=case.destination, text="wrong turn", send_anyway=True,
    ))
    assert wrong_turn["error_kind"] == "reconsideration_ineligible"
    assert len(case.calls) == 1
    case.freshness.active = SimpleNamespace(
        turn_id="turn-a", provider_session_id="session-a",
    )
    assert len(case.coordinator._held_evidence) == 1
    evidence = next(iter(case.coordinator._held_evidence.values()))
    snapshot = (evidence.latest_seq, evidence.latest_envelope_id, evidence.synchronized)
    evidence.latest_envelope_id = "wrong-envelope"
    evidence.synchronized = False
    wrong_pair = await case.coordinator.send(SemanticSendRequest(
        destination=case.destination, text="draft", send_anyway=True,
    ))
    assert wrong_pair["error_kind"] == "reconsideration_ineligible"
    assert wrong_pair["_reconsideration_audit"]["decision_reason"] == (
        "held_not_synchronized"
    )
    assert len(case.calls) == 1
    evidence.latest_seq, evidence.latest_envelope_id, evidence.synchronized = snapshot


async def _assert_held_identity_mismatches(case):
    mismatches = [
        ("wrong coordinator session", lambda: setattr(
            case.coordinator, "provider_session_id", "session-b",
        )),
        ("wrong active session", lambda: setattr(
            case.freshness, "active", SimpleNamespace(
                turn_id="turn-a", provider_session_id="session-b",
            ),
        )),
        ("wrong turn", lambda: setattr(
            case.freshness, "active", SimpleNamespace(
                turn_id="wrong-turn", provider_session_id="session-a",
            ),
        )),
    ]
    for label, mutate in mismatches:
        original_session = case.coordinator.provider_session_id
        original_active = case.freshness.active
        mutate()
        blocked = await case.coordinator.send(SemanticSendRequest(
            destination=case.destination, text=label, send_anyway=True,
        ))
        assert blocked["error_kind"] == "reconsideration_ineligible"
        assert len(case.calls) == 1
        case.coordinator.provider_session_id = original_session
        case.freshness.active = original_active


async def _assert_one_shot_held_send(case):
    revised = await case.coordinator.send(SemanticSendRequest(
        destination=case.destination, text="revised draft", send_anyway=True,
    ))
    assert revised["state"] == "failed"
    assert revised["error_kind"] == "reconsideration_ineligible"
    assert revised["_reconsideration_audit"]["decision_reason"] == (
        "held_draft_mismatch"
    )
    assert len(case.calls) == 1
    # Only the unchanged draft the model reconsidered may override.
    sent = await case.coordinator.send(SemanticSendRequest(
        destination=case.destination, text="draft", send_anyway=True,
    ))
    assert sent["state"] == "sent"
    assert len(case.calls) == 2
    assert case.calls[-1]["freshness"] == {
        "context_baseline_seq": 2, "seen_seq": 5, "mode": "send_anyway",
    }
    reused = await case.coordinator.send(SemanticSendRequest(
        destination=case.destination, text="reuse", send_anyway=True,
    ))
    assert reused["error_kind"] == "reconsideration_ineligible"
    assert len(case.calls) == 2
    assert case.paths == [case.expected_path, case.expected_path]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["native", "keyless"])
async def test_complete_exact_held_identity_and_one_shot_contract(transport):
    case = (
        await _native_held_contract()
        if transport == "native"
        else await _keyless_held_contract()
    )
    await _assert_initial_held_contract(case)
    await _assert_wrong_turn_and_pair(case)
    await _assert_held_identity_mismatches(case)
    await _assert_one_shot_held_send(case)
    if case.store is not None:
        await case.store.close()


@pytest.mark.asyncio
async def test_held_attempted_no_automatic_resend_and_preserves_watermarks():
    coordinator, freshness, http = await coordinator_fixture(baseline=2)

    async def post(path, body):
        http.calls.append(("POST", path, body))
        return held_response(body)

    http.post = post
    result = await coordinator.send(SemanticSendRequest(destination="ch_a", text="draft"))
    assert result["state"] == "held"
    assert result["synchronized"] is False
    reconsideration = result["reconsideration"]
    assert reconsideration["context_ready"] is False
    assert reconsideration["draft"] == "draft"
    assert reconsideration["based_on_through_seq"] == 2
    assert reconsideration["target"] == {
        "target_type": "channel",
        "target_ref": "channel:sp_1:ch_a",
        "space_id": "sp_1",
        "channel_id": "ch_a",
        "thread_root_id": "",
    }
    assert len([c for c in http.calls if c[0] == "POST"]) == 1
    assert freshness.advances == []


@pytest.mark.asyncio
async def test_durable_recovery_same_provider_session_only():
    coordinator, _, http = await coordinator_fixture(baseline=2)
    recovery = Recovery(rows=[
        {
            "envelope_id": "msg_latest", "latest_seq": 5,
            "latest_envelope_id": "msg_latest", "provider_session_id": "session-a",
        },
        {
            "envelope_id": "wrong-session", "latest_seq": 5,
            "latest_envelope_id": "msg_latest", "provider_session_id": "session-b",
        },
    ])
    coordinator.held_recovery_source = recovery
    coordinator.provider_session_id = "session-a"

    async def post(path, body):
        return held_response(body)

    http.post = post
    result = await coordinator.send(SemanticSendRequest(destination="ch_a", text="draft"))
    assert "recovered_messages" not in result
    assert result["synchronized"] is False


@pytest.mark.asyncio
async def test_recovery_unavailable_exposes_nothing_and_does_not_advance():
    coordinator, freshness, http = await coordinator_fixture(baseline=2)
    coordinator.held_recovery_source = Recovery(
        rows=[{"provider_session_id": "session-a"}], available=False,
    )

    async def post(path, body):
        return held_response(body)

    http.post = post
    result = await coordinator.send(SemanticSendRequest(destination="ch_a", text="draft"))
    assert "recovered_messages" not in result
    assert freshness.advances == []


@pytest.mark.asyncio
async def test_unsynchronized_held_stays_blocked_after_admitted_boundary_advance():
    coordinator, freshness, http = await coordinator_fixture(
        baseline=2, active=2
    )
    boundary = bind_turn(coordinator, freshness)
    coordinator.held_recovery_source = Recovery(available=False)
    calls = []

    async def post(path, body):
        calls.append(body)
        if len(calls) == 1:
            return held_response(body)
        return {
            "state": "sent",
            "envelope_id": body["envelope"]["envelope_id"],
            "seq": 6,
            "replay": False,
            "missing_devices": [],
            "freshness": {
                "mode": "send_anyway",
                "context_baseline_seq": 2,
                "seen_seq": 2,
                "latest_seq_before_send": 5,
            },
        }

    http.post = post
    first = await coordinator.send(SemanticSendRequest(destination="ch_a", text="draft"))
    boundary.value = 5
    second = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="new draft", send_anyway=True,
    ))
    assert first["state"] == "held"
    assert second["state"] == "failed"
    assert second["error_kind"] == "reconsideration_ineligible"
    assert len(calls) == 1
    assert freshness.advances == []
    assert all(
        path == CHANNEL_SEND_PATH
        for method, path, _body in http.calls
        if method == "POST"
    )


@pytest.mark.asyncio
async def test_same_session_turn_admitted_boundary_makes_override_eligible():
    coordinator, freshness, http = await coordinator_fixture(baseline=2, active=2)
    boundary = bind_turn(coordinator, freshness)
    coordinator.held_recovery_source = Recovery(rows=[exact_row()])
    calls = []

    async def post(path, body):
        calls.append(body)
        if len(calls) == 1:
            return held_response(body)
        return {
            "state": "sent",
            "envelope_id": body["envelope"]["envelope_id"],
            "seq": 6,
            "replay": False,
            "missing_devices": [],
            "freshness": {
                "mode": "send_anyway",
                "context_baseline_seq": 2,
                "seen_seq": 5,
                "latest_seq_before_send": 5,
            },
        }

    http.post = post
    first = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="draft")
    )
    assert first["state"] == "held"
    assert first["synchronized"] is True
    # Unchanged draft, so the probe reaches the admission gate rather than
    # stopping at the draft-identity gate.
    blocked = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft", send_anyway=True
    ))
    assert blocked["error_kind"] == "reconsideration_ineligible"
    assert blocked["_reconsideration_audit"]["decision_reason"] == (
        "admission_before_held"
    )
    assert len(calls) == 1
    boundary.value = 5
    revised = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="reconsidered", send_anyway=True
    ))
    assert revised["state"] == "failed"
    assert revised["error_kind"] == "reconsideration_ineligible"
    assert revised["_reconsideration_audit"]["decision_reason"] == (
        "held_draft_mismatch"
    )
    assert len(calls) == 1
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="draft", send_anyway=True)
    )
    assert result["state"] == "sent"
    assert len(calls) == 2
    assert calls[-1]["freshness"]["mode"] == "send_anyway"
    reused = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="reuse", send_anyway=True
    ))
    assert reused["error_kind"] == "reconsideration_ineligible"
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_change", ["target", "session", "active_session", "turn"]
)
async def test_override_wrong_target_session_or_turn_is_blocked_without_request(
    identity_change,
):
    coordinator, freshness, http = await coordinator_fixture(
        baseline=2, active=2
    )
    coordinator.provider_session_id = "session-a"
    freshness.active = SimpleNamespace(
        turn_id="turn-a", provider_session_id="session-a"
    )
    coordinator.held_recovery_source = Recovery(rows=[exact_row()])

    async def boundary(_space_id, _channel_id):
        return boundary.value

    boundary.value = 2
    freshness.get_active_turn_through_seq = boundary
    calls = []

    async def post(_path, body):
        calls.append(body)
        return held_response(body)

    http.post = post
    assert (await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="draft")
    ))["synchronized"] is True
    boundary.value = 5
    destination = "ch_a"
    if identity_change == "target":
        destination = "ch_b"
    elif identity_change == "session":
        coordinator.provider_session_id = "session-b"
    elif identity_change == "active_session":
        freshness.active = SimpleNamespace(
            turn_id="turn-a", provider_session_id="session-b"
        )
    else:
        freshness.active = SimpleNamespace(
            turn_id="turn-b", provider_session_id="session-a"
        )
    result = await coordinator.send(SemanticSendRequest(
        destination=destination,
        text="override",
        send_anyway=True,
    ))
    assert result["state"] == "failed"
    assert result["error_kind"] == "reconsideration_ineligible"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_staged_but_unadmitted_held_context_cannot_authorize_override():
    coordinator, freshness, http = await coordinator_fixture(
        baseline=2, active=2
    )
    coordinator.provider_session_id = "session-a"
    freshness.active = SimpleNamespace(
        turn_id="turn-a", provider_session_id="session-a"
    )
    coordinator.held_recovery_source = Recovery(rows=[exact_row()])

    async def boundary(_space_id, _channel_id):
        return 2

    freshness.get_active_turn_through_seq = boundary
    calls = []

    async def post(_path, body):
        calls.append(body)
        return held_response(body)

    http.post = post
    assert (await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="draft")
    ))["synchronized"] is True
    # Catch-up may have made the row durable/readable, but the admitted
    # active boundary intentionally remains below the held watermark.
    result = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft", send_anyway=True
    ))
    assert result["state"] == "failed"
    assert result["error_kind"] == "reconsideration_ineligible"
    assert result["_reconsideration_audit"]["decision_reason"] == (
        "admission_before_held"
    )
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recovery",
    [
        Recovery(available=False),
        Recovery(rows=[]),
        Recovery(rows=[{
            "latest_seq": 5,
            "latest_envelope_id": "msg_latest",
            "provider_session_id": "session-a",
        }]),
        Recovery(rows=[exact_row(seq=4)]),
        Recovery(rows=[exact_row(envelope_id="wrong")]),
        Recovery(rows=[exact_row(session_id="session-b")]),
    ],
)
async def test_incomplete_or_mismatched_recovery_fails_closed(recovery):
    coordinator, freshness, http = await coordinator_fixture(
        baseline=2, active=2
    )
    boundary = bind_turn(coordinator, freshness)
    coordinator.held_recovery_source = recovery
    calls = []

    async def post(_path, body):
        calls.append(body)
        return held_response(body)

    http.post = post
    held = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="draft")
    )
    assert held["state"] == "held"
    assert held["synchronized"] is False
    boundary.value = 5
    # Unchanged draft, so the probe reaches the synchronization gate rather
    # than stopping at the draft-identity gate.
    result = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft", send_anyway=True
    ))
    assert result["error_kind"] == "reconsideration_ineligible"
    assert result["_reconsideration_audit"]["decision_reason"] == (
        "held_not_synchronized"
    )
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("late"), RuntimeError("boom")])
async def test_recovery_timeout_or_exception_fails_closed(failure):
    coordinator, freshness, http = await coordinator_fixture(
        baseline=2, active=2
    )
    boundary = bind_turn(coordinator, freshness)

    class FailingRecovery:
        async def wait_for_held_delivery(self, *_args):
            raise failure

    coordinator.held_recovery_source = FailingRecovery()
    calls = []

    async def post(_path, body):
        calls.append(body)
        return held_response(body)

    http.post = post
    await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft"
    ))
    boundary.value = 5
    # Unchanged draft, so the probe reaches the synchronization gate rather
    # than stopping at the draft-identity gate.
    result = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft", send_anyway=True
    ))
    assert result["error_kind"] == "reconsideration_ineligible"
    assert result["_reconsideration_audit"]["decision_reason"] == (
        "held_not_synchronized"
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_later_exact_recovery_can_unlock_only_current_held_record():
    coordinator, freshness, http = await coordinator_fixture(
        baseline=2, active=2
    )
    boundary = bind_turn(coordinator, freshness)
    recovery = Recovery(available=False)
    coordinator.held_recovery_source = recovery
    calls = []

    async def post(_path, body):
        calls.append(body)
        if len(calls) == 1:
            return held_response(body)
        return {
            "state": "sent",
            "envelope_id": body["envelope"]["envelope_id"],
            "seq": 6,
            "replay": False,
            "missing_devices": [],
            "freshness": {
                "mode": "send_anyway",
                "context_baseline_seq": 2,
                "seen_seq": 5,
                "latest_seq_before_send": 5,
            },
        }

    http.post = post
    first = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft"
    ))
    assert first["synchronized"] is False
    boundary.value = 5
    # Unchanged draft, so the probe reaches the synchronization gate rather
    # than stopping at the draft-identity gate.
    blocked = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft", send_anyway=True
    ))
    assert blocked["error_kind"] == "reconsideration_ineligible"
    assert blocked["_reconsideration_audit"]["decision_reason"] == (
        "held_not_synchronized"
    )
    assert len(calls) == 1
    recovery.available = True
    recovery.rows = [exact_row()]
    sent = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft", send_anyway=True
    ))
    assert sent["state"] == "sent"
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda response, body: {
            **response,
            "context_baseline_seq": body["freshness"]["context_baseline_seq"] + 1,
        },
        lambda response, body: {
            **response,
            "latest_seq": body["freshness"]["seen_seq"],
        },
    ],
)
async def test_invalid_held_baseline_or_nonadvancing_watermark_fails_closed(
    mutate,
):
    coordinator, _, http = await coordinator_fixture(baseline=2)
    calls = []

    async def post(_path, body):
        calls.append(body)
        return mutate(held_response(body), body)

    http.post = post
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="draft")
    )
    assert result["state"] == "failed"
    assert result["error_kind"] == "protocol"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_held_attachment_is_uploaded_once_and_not_resent(tmp_path):
    coordinator, _, http = await coordinator_fixture(baseline=2)
    coordinator.workspace = str(tmp_path)
    (tmp_path / "report.txt").write_text("report", encoding="utf-8")
    uploads = []
    posts = []

    async def upload(path, body):
        uploads.append((path, body))
        return {"blob_id": "blob_held"}

    async def post(path, body):
        posts.append((path, body))
        return held_response(body)

    http.post_bytes = upload
    http.post = post
    result = await coordinator.send(SemanticSendRequest(
        destination="ch_a",
        attachment_paths=("report.txt",),
        caption="attached",
    ))
    assert result["state"] == "held"
    assert len(uploads) == 1
    assert len(posts) == 1
    assert not any(path in ("/messages", "/blobs/delete") for path, _ in posts)


@pytest.mark.asyncio
async def test_stale_recovery_completion_cannot_synchronize_superseded_pair():
    coordinator, freshness, http = await coordinator_fixture(
        baseline=2, active=2
    )
    boundary = bind_turn(coordinator, freshness)
    started = asyncio.Event()
    release = asyncio.Event()

    class CoalescingRecovery:
        def __init__(self):
            self.waits = []
            self.queries = []

        async def wait_for_held_delivery(self, space_id, channel_id, seq, envelope_id):
            self.waits.append((seq, envelope_id))
            if seq == 5:
                started.set()
                await release.wait()
                return True
            return False

        async def query_held_messages(
            self, space_id, channel_id, seq, envelope_id, provider_session_id,
        ):
            self.queries.append((seq, envelope_id))
            return [exact_row(
                seq=seq, envelope_id=envelope_id,
                session_id=provider_session_id,
            )]

    recovery = CoalescingRecovery()
    coordinator.held_recovery_source = recovery
    attempts = 0

    async def post(path, body):
        nonlocal attempts
        attempts += 1
        return held_response(
            body, latest_seq=4 + attempts,
            latest_envelope_id=f"msg_latest_{attempts}",
        )

    http.post = post
    first_task = asyncio.create_task(coordinator.send(
        SemanticSendRequest(destination="ch_a", text="one")
    ))
    await started.wait()
    second = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="two")
    )
    release.set()
    first = await first_task
    assert recovery.queries == []
    assert first["synchronized"] is False
    assert second["synchronized"] is False
    boundary.value = 6
    # "two" is the surviving held draft, so the probe reaches the
    # synchronization gate rather than stopping at the draft-identity gate.
    blocked = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="two", send_anyway=True
    ))
    assert blocked["error_kind"] == "reconsideration_ineligible"
    assert blocked["_reconsideration_audit"]["decision_reason"] == (
        "held_not_synchronized"
    )
    assert attempts == 2


@pytest.mark.asyncio
async def test_second_draft_held_at_one_head_never_reuses_the_first_draft():
    """Held evidence is keyed per turn/channel, so two drafts held against the
    same Server head shared one record: the second draft was answered with the
    first draft's text, thread target, and visible basis, and the superseded
    first draft kept override authorization it no longer owned."""
    coordinator, freshness, http = await coordinator_fixture(baseline=2, active=2)
    boundary = bind_turn(coordinator, freshness)
    attach_visible_basis(coordinator.held_recovery_source, [
        basis_row("root-a", seq=3),
        basis_row("root-b", seq=4),
    ])
    calls = []

    async def post(_path, body):
        calls.append(body)
        return held_response(body)

    http.post = post
    first = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft one", root_id="root-a",
    ))
    second = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft two", root_id="root-b",
    ))
    assert first["reconsideration"]["draft"] == "draft one"
    assert first["reconsideration"]["target"]["thread_root_id"] == "root-a"
    assert second["reconsideration"]["draft"] == "draft two"
    assert second["reconsideration"]["target"]["thread_root_id"] == "root-b"
    key = ("session-a", "turn-a", "sp_1", "ch_a")
    evidence = coordinator._held_evidence[key]
    assert evidence.draft == "draft two"
    assert [row["envelope_id"] for row in evidence.visible_draft_basis] == ["root-b"]
    boundary.value = 5
    superseded = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="draft one", root_id="root-a", send_anyway=True,
    ))
    assert superseded["state"] == "failed"
    assert superseded["error_kind"] == "reconsideration_ineligible"
    assert superseded["_reconsideration_audit"]["decision_reason"] == (
        "held_draft_mismatch"
    )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_stale_recovery_cannot_synchronize_a_superseding_draft_at_one_head():
    """A recovery in flight for one draft compared only the Server pair, so it
    could mark a different draft's superseding record synchronized and attach
    the abandoned draft's rows to it."""
    coordinator, freshness, http = await coordinator_fixture(baseline=2, active=2)
    boundary = bind_turn(coordinator, freshness)
    started = asyncio.Event()
    release = asyncio.Event()

    class OneShotRecovery:
        def __init__(self):
            self.waits = []
            self.queries = []

        async def wait_for_held_delivery(self, space_id, channel_id, seq, envelope_id):
            self.waits.append((seq, envelope_id))
            if len(self.waits) == 1:
                started.set()
                await release.wait()
                return True
            return False

        async def query_held_messages(
            self, space_id, channel_id, seq, envelope_id, provider_session_id,
        ):
            self.queries.append((seq, envelope_id))
            return [exact_row(
                seq=seq, envelope_id=envelope_id, session_id=provider_session_id,
            )]

    recovery = OneShotRecovery()
    coordinator.held_recovery_source = recovery
    calls = []

    async def post(_path, body):
        calls.append(body)
        return held_response(body)

    http.post = post
    first_task = asyncio.create_task(coordinator.send(
        SemanticSendRequest(destination="ch_a", text="one")
    ))
    await started.wait()
    second = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="two")
    )
    release.set()
    first = await first_task
    assert recovery.queries == []
    assert first["synchronized"] is False
    assert second["synchronized"] is False
    evidence = coordinator._held_evidence[("session-a", "turn-a", "sp_1", "ch_a")]
    assert evidence.draft == "two"
    assert evidence.synchronized is False
    boundary.value = 5
    blocked = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="two", send_anyway=True,
    ))
    assert blocked["error_kind"] == "reconsideration_ineligible"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_abandoned_turn_held_context_is_dropped_and_records_stay_bounded():
    """Abandoned held evidence kept recovered plaintext for the process
    lifetime because nothing dropped records once their turn moved on."""
    coordinator, freshness, http = await coordinator_fixture(baseline=2, active=2)
    bind_turn(coordinator, freshness)
    coordinator.held_recovery_source = Recovery(
        rows=[{**exact_row(), "content": "recovered"}],
    )

    async def post(_path, body):
        return held_response(body)

    http.post = post
    await coordinator.send(SemanticSendRequest(destination="ch_a", text="draft"))
    abandoned = ("session-a", "turn-a", "sp_1", "ch_a")
    assert coordinator._held_evidence[abandoned].recovered_messages
    freshness.active = SimpleNamespace(
        turn_id="turn-b", provider_session_id="session-a",
    )
    await coordinator.send(SemanticSendRequest(destination="ch_b", text="next turn"))
    assert abandoned not in coordinator._held_evidence
    assert list(coordinator._held_evidence) == [
        ("session-a", "turn-b", "sp_1", "ch_b"),
    ]
    for index in range(_MAX_HELD_RECORDS + 4):
        await coordinator._record_held(
            ("session-a", "turn-b", "sp_1", f"ch_{index}"),
            5,
            "msg_latest",
            attempt_fingerprint=f"fingerprint-{index}",
        )
    assert len(coordinator._held_evidence) == _MAX_HELD_RECORDS


@pytest.mark.asyncio
async def test_held_override_is_bound_to_the_transmitted_attachment_bytes(tmp_path):
    """A mutated file at an unchanged path cannot reuse a held approval."""
    case = await _keyless_held_contract()
    case.coordinator.workspace = str(tmp_path)
    target = tmp_path / "evidence.txt"
    target.write_bytes(b"original-draft-bytes")
    http = case.coordinator.http_client
    request = dict(
        destination=case.destination,
        attachment_paths=("evidence.txt",),
        caption="caption",
    )

    held = await case.coordinator.send(SemanticSendRequest(**request))
    assert held["state"] == "held"
    assert http.uploaded == [b"original-draft-bytes"]
    case.boundary.value = 5

    # Same path, same caption, different bytes: the tool-argument fingerprint
    # still matches, so only the content digest can reject this.
    target.write_bytes(b"revised-draft-bytes!")
    blocked = await case.coordinator.send(
        SemanticSendRequest(**request, send_anyway=True)
    )
    assert blocked["state"] == "failed"
    assert blocked["error_kind"] == "reconsideration_ineligible"
    assert blocked["_reconsideration_audit"]["decision_reason"] == (
        "held_content_changed"
    )
    assert len(case.calls) == 1
    assert http.uploaded == [b"original-draft-bytes"]

    # The unchanged draft stays eligible, and what is uploaded is what was
    # digested and approved.
    target.write_bytes(b"original-draft-bytes")
    sent = await case.coordinator.send(
        SemanticSendRequest(**request, send_anyway=True)
    )
    assert sent["state"] == "sent", sent
    assert len(case.calls) == 2
    assert http.uploaded == [b"original-draft-bytes", b"original-draft-bytes"]
    await case.store.close()


async def _assert_cancelled_turn_drops_a_late_held_record(runtime):
    """Teardown can overlap a send the turn's cancellation never reaches.

    ``send_message`` is serviced by the RPC/ws-local handler task, not the
    turn task, so a cancelled turn runs ``_clear_terminal_turn`` while that
    handler is still awaiting its transport.  The record it writes afterwards
    must not repopulate the map behind teardown's back.
    """
    coordinator, freshness, http = await coordinator_fixture(baseline=2, active=2)
    bind_turn(coordinator, freshness)
    runtime.coordinator = coordinator
    posted, release = asyncio.Event(), asyncio.Event()

    async def post(_path, body):
        posted.set()
        await release.wait()
        return held_response(body)

    http.post = post
    send_task = asyncio.create_task(coordinator.send(
        SemanticSendRequest(destination="ch_a", text="secret draft"),
    ))
    await asyncio.wait_for(posted.wait(), timeout=1)
    runtime._clear_terminal_turn()
    release.set()
    assert (await asyncio.wait_for(send_task, timeout=1))["state"] == "held"
    assert coordinator._held_evidence == {}

    # A later turn reusing the same identity must not inherit the late record.
    bind_turn(coordinator, freshness)
    probe = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="secret draft", send_anyway=True,
    ))
    assert probe["_reconsideration_audit"]["decision_reason"] == (
        "missing_held_evidence"
    )


@pytest.mark.asyncio
async def test_turn_teardown_discards_decrypted_held_evidence(tmp_path):
    """Terminal turns drop held plaintext instead of leaking it forward."""
    from .test_global_inbox_runtime import Adapter, make_store

    from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime

    store = await make_store(tmp_path)
    cfg, http, unused = _setup_keyless()
    coordinator = SendCoordinator(
        slug=cfg.slug, keystore=cfg.keystore, http_client=http,
        data_client=store,
    )
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.coordinator = coordinator

    def stage():
        coordinator._held_evidence[("session-a", "turn-a", "sp_1", "ch_1")] = (
            _HeldEvidence(
                latest_seq=5,
                latest_envelope_id="msg_latest",
                draft="secret draft",
                recovered_messages=[{"content": "decrypted row"}],
            )
        )

    planned = SimpleNamespace(turn_id="turn-a", notice_generation=None)
    stage()
    runtime._finalize_process(planned, True)
    assert coordinator._held_evidence == {}

    stage()
    runtime._clear_terminal_turn()
    assert coordinator._held_evidence == {}

    await _assert_cancelled_turn_drops_a_late_held_record(runtime)
    await store.close()
    await unused.close()
