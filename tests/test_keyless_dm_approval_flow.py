"""Focused contract tests for keyless DM operator approval."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from puffo_agent.agent import keyless_dm_approval_flow as flow
from puffo_agent.agent.client_setup import initial_client_state
from puffo_agent.agent.contact_cache import ContactCache
from puffo_agent.agent.dm_approvals import (
    load_pending_dm_approvals,
    parse_keyless_approval,
    save_pending_dm_approvals,
)
from puffo_agent.agent.keyless_dm_approval_flow import (
    maybe_handle_operator_reply,
    record_gated_dm,
    resume_pending_approvals,
)
from puffo_agent.agent.message_store import ReceiptDisposition, ReceiptWriteStatus
from puffo_agent.agent.message_store_models import ReceiptResult
from _portal_support import isolated_home

log = logging.getLogger("keyless-dm-approval-test")


class _Bridge:
    def __init__(self) -> None:
        self.send_calls: list[dict] = []
        self.ack_calls: list[list[str]] = []
        self.send_snapshots: list[dict] = []
        self.ack_snapshots: list[dict] = []
        self.ack_error: Exception | None = None
        self.snapshot = lambda: {}

    async def send_send(self, **kwargs) -> dict:
        self.send_snapshots.append(dict(self.snapshot()))
        self.send_calls.append(kwargs)
        return {
            "type": "ack",
            "client_ref": kwargs["client_ref"],
            "envelope_id": "prompt-1",
        }

    async def send_ack(self, envelope_ids, *, timeout: float = 30.0) -> dict:
        self.ack_snapshots.append(dict(self.snapshot()))
        if self.ack_error is not None:
            raise self.ack_error
        ids = list(envelope_ids)
        self.ack_calls.append(ids)
        return {"type": "ack_result", "acked": ids}


class _Store:
    def __init__(self) -> None:
        self.promote_calls: list[tuple] = []
        self.tombstone_calls: list[tuple] = []
        self.snapshots: list[dict] = []
        self.status: dict[str, ReceiptWriteStatus] = {}
        self.snapshot = lambda: {}

    async def promote_gated_receipt(
        self,
        envelope_id,
        server_seq,
        *,
        reason,
        approved_payload=None,
    ) -> ReceiptResult:
        self.promote_calls.append((envelope_id, server_seq, reason))
        self.snapshots.append(dict(self.snapshot()))
        status = self.status.get(envelope_id, ReceiptWriteStatus.COMMITTED)
        return ReceiptResult(
            status,
            ReceiptDisposition.ELIGIBLE,
            reason,
            status is not ReceiptWriteStatus.CONFLICT,
        )

    async def tombstone_gated_dm(
        self,
        envelope_id,
        server_seq=None,
    ) -> ReceiptResult:
        self.tombstone_calls.append((envelope_id, server_seq))
        self.snapshots.append(dict(self.snapshot()))
        status = self.status.get(envelope_id, ReceiptWriteStatus.COMMITTED)
        return ReceiptResult(
            status,
            ReceiptDisposition.TERMINAL,
            "foreign dm denied by operator",
            status is not ReceiptWriteStatus.CONFLICT,
        )


class _Runtime:
    def __init__(self) -> None:
        self.notifications = 0

    def notify(self) -> None:
        self.notifications += 1


class _KeylessHttp:
    keyless = True


class _SignedHttp:
    keyless = False


def _client(slug: str, operator: str = "operator-1"):
    from puffo_agent.portal.state import agent_dir

    bridge = _Bridge()
    store = _Store()
    pending = load_pending_dm_approvals(slug)
    client = SimpleNamespace(
        slug=slug,
        operator_slug=operator,
        _bridge=bridge,
        store=store,
        global_runtime=_Runtime(),
        _contacts=ContactCache(
            _KeylessHttp(),
            log,
            local_state_path=(
                agent_dir(slug) / ".puffo-agent" / "keyless_contacts.json"
            ),
        ),
        _pending_dm_approvals=pending,
        _keyless_dm_approval_lock=asyncio.Lock(),
        _log=log,
    )
    bridge.snapshot = lambda: client._pending_dm_approvals
    store.snapshot = lambda: client._pending_dm_approvals
    return client


def _record(client, envelope_id: str):
    return parse_keyless_approval(client._pending_dm_approvals[envelope_id])


@pytest.mark.asyncio
async def test_grouped_approval_is_durable_serial_and_resumable():
    isolated_home()
    client = _client("agent-1")
    client._pending_dm_approvals["native"] = {"sender_slug": "legacy-1"}

    await asyncio.gather(
        record_gated_dm(
            client,
            envelope_id="held-1",
            sender_slug="alice-1",
            server_seq=11,
        ),
        record_gated_dm(
            client,
            envelope_id="held-2",
            sender_slug="alice-1",
            server_seq=12,
        ),
    )
    assert len(client._bridge.send_calls) == 1
    assert client._bridge.send_snapshots[0]["held-1"]["phase"] == "pending"
    assert (
        _record(client, "held-1").prompt_client_ref
        == _record(
            client,
            "held-2",
        ).prompt_client_ref
    )
    assert (
        await maybe_handle_operator_reply(
            client,
            thread_root_id="wrong",
            text="y",
        )
        is False
    )
    assert (
        await maybe_handle_operator_reply(
            client,
            thread_root_id="prompt-1",
            text="later",
        )
        is False
    )

    client.store.status["held-2"] = ReceiptWriteStatus.IDEMPOTENT
    client._bridge.ack_error = RuntimeError("connection dropped")
    assert (
        await maybe_handle_operator_reply(
            client,
            thread_root_id="prompt-1",
            text=" yes ",
        )
        is True
    )
    assert {call[:2] for call in client.store.promote_calls} == {
        ("held-1", 11),
        ("held-2", 12),
    }
    assert all(
        value["decision"] == "approved"
        for key, value in (client._pending_dm_approvals.items())
        if key.startswith("held-")
    )
    assert all(
        value["phase"] == "applied"
        for key, value in (client._pending_dm_approvals.items())
        if key.startswith("held-")
    )
    assert client.store.snapshots[0]["held-1"]["decision"] == "approved"
    assert client._bridge.ack_snapshots[0]["held-1"]["phase"] == "applied"

    # A contradictory retry cannot reverse a durable decision or its effects.
    assert (
        await maybe_handle_operator_reply(
            client,
            thread_root_id="prompt-1",
            text="n",
        )
        is True
    )
    assert client.store.tombstone_calls == []
    assert _record(client, "held-1").decision == "approved"

    # A later group member gets its own transition, not an inherited phase.
    await record_gated_dm(
        client,
        envelope_id="held-3",
        sender_slug="alice-1",
        server_seq=13,
    )
    assert len(client._bridge.send_calls) == 1
    assert _record(client, "held-3").phase == "applied"

    client._bridge.ack_error = None
    reboot = _client("agent-1")
    await resume_pending_approvals(reboot)
    assert reboot.store.promote_calls == []
    assert reboot._bridge.ack_calls == [["held-1"], ["held-2"], ["held-3"]]
    assert reboot._pending_dm_approvals == {"native": {"sender_slug": "legacy-1"}}


@pytest.mark.asyncio
async def test_denial_conflict_missing_operator_and_persist_failure_fail_closed(
    monkeypatch,
):
    isolated_home()
    denied = _client("denied")
    await record_gated_dm(
        denied,
        envelope_id="held-n",
        sender_slug="bob-1",
        server_seq=21,
    )
    assert (
        await maybe_handle_operator_reply(
            denied,
            thread_root_id="prompt-1",
            text="no",
        )
        is True
    )
    assert denied.store.tombstone_calls == [("held-n", 21)]
    assert denied._bridge.ack_calls == [["held-n"]]

    conflict = _client("conflict")
    await record_gated_dm(
        conflict,
        envelope_id="held-c",
        sender_slug="cora-1",
        server_seq=31,
    )
    conflict.store.status["held-c"] = ReceiptWriteStatus.CONFLICT
    await maybe_handle_operator_reply(
        conflict,
        thread_root_id="prompt-1",
        text="y",
    )
    assert _record(conflict, "held-c").phase == "pending"
    assert conflict._bridge.ack_calls == []

    no_operator = _client("no-operator", operator="")
    await record_gated_dm(
        no_operator,
        envelope_id="held-o",
        sender_slug="dana-1",
        server_seq=None,
    )
    assert no_operator._bridge.send_calls == []
    assert _record(no_operator, "held-o").decision == "pending"

    failed = _client("persist-failure")

    def _raise(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(flow, "save_pending_dm_approvals", _raise)
    await record_gated_dm(
        failed,
        envelope_id="held-p",
        sender_slug="erin-1",
        server_seq=41,
    )
    assert "held-p" not in failed._pending_dm_approvals
    assert failed._bridge.send_calls == []

    # A keyless contact write failure fails closed in both directions: the
    # decided record stays pending, nothing transitions or ACKs, and replay
    # retries the durable contact mutation before applying and ACKing the
    # exact held envelope.
    monkeypatch.setattr(flow, "save_pending_dm_approvals", save_pending_dm_approvals)

    def _disk_full():
        raise OSError("disk full")

    for approve, word, seq, sender in ((True, "y", 51, "frank-1"), (False, "n", 61, "grace-1")):
        slug = "approve-fail" if approve else "deny-fail"
        fail = _client(slug)
        await record_gated_dm(fail, envelope_id="held-f", sender_slug=sender, server_seq=seq)
        write = fail._contacts._persist_local_state
        fail._contacts._persist_local_state = _disk_full
        assert await maybe_handle_operator_reply(fail, thread_root_id="prompt-1", text=word) is True
        fail._contacts._persist_local_state = write
        assert _record(fail, "held-f").phase == "pending"
        assert _record(fail, "held-f").decision == ("approved" if approve else "denied")
        assert fail._bridge.ack_calls == []
        assert fail.store.promote_calls == []
        assert fail.store.tombstone_calls == []
        if approve:
            assert fail.global_runtime.notifications == 0
        retry = _client(slug)
        await resume_pending_approvals(retry)
        if approve:
            assert retry.store.promote_calls == [("held-f", seq, flow.APPROVED_REASON)]
        else:
            assert retry.store.tombstone_calls == [("held-f", seq)]
        assert retry._bridge.ack_calls == [["held-f"]]
        assert (await retry._contacts.is_allowed(sender)) == approve
        assert (await retry._contacts.is_blocked(sender)) == (not approve)


def test_contact_construction_seam():
    isolated_home()
    keyless = initial_client_state(
        slug="agent-1",
        device_id="dev-1",
        space_id="space-1",
        keystore=None,
        http_client=_KeylessHttp(),
        message_store=None,
    )
    signed = initial_client_state(
        slug="agent-2",
        device_id="dev-2",
        space_id="space-1",
        keystore=None,
        http_client=_SignedHttp(),
        message_store=None,
    )
    assert keyless["_contacts"]._local_state_path.name == "keyless_contacts.json"
    assert signed["_contacts"]._local_state_path is None
    assert isinstance(keyless["_keyless_dm_approval_lock"], asyncio.Lock)
