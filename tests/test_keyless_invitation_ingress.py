"""Keyless invitation bridge ingress integration.

The durable ``KeylessInvitationFlow`` is constructed per real bridge client
and fed by a connection-owned poller armed on ``pending_delivered``. These
three tests guard the wiring boundary that the component suites cannot: that
the flow really reads the authoritative ``send_list_invites`` list through a
live frame pump, prompts exactly once via ``send_send``, consumes an exact
threaded operator reply as tracked work without blocking the pump, stores its
own prompt echo as a control placeholder, and owns exactly one poller per
connection that is cancelled on reconnect.

Regression each guards:

  1. ``test_invitation_lifecycle_...`` — pagination/repeated polls would
     prompt twice; malformed channel invites would be adopted; the prompt
     echo would reach model context; the operator decision would never reach
     a terminal outcome; native clients would gain a keyless flow.
  2. ``test_operator_reply_is_tracked_...`` — awaiting ``send_decide_`` on
     the frame-pump stack would freeze delivery; the decision work would
     never be resolved by the pump; a nonmatching operator DM would be
     swallowed.
  3. ``test_poller_arms_once_per_connection_...`` — a duplicate poller (or a
     leaked one) would double-prompt; a poll failure would kill delivery; a
     reconnect would reuse a dead poller slot.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from puffo_agent.agent import bridge_transport as bridge_transport_mod
from puffo_agent.agent.client_support import INVITE_PROMPT_PLACEHOLDER
from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.keystore import KeyStore

SELF = "bot-0001"
OPERATOR = "ops-0001"


@pytest.fixture(autouse=True)
def _isolated_agent_home(tmp_path, monkeypatch):
    home = str(tmp_path / "agent-home")
    monkeypatch.setenv("PUFFO_AGENT_HOME", home)
    monkeypatch.setenv("PUFFO_HOME", home)


class _FakeBridge:
    """Offline ``CloudBridgeClient`` stand-in whose ``frames()`` routes
    correlated replies exactly like the real client (ack → ``send_send``,
    invites → ``send_list_invites``, decide_invitation_result →
    ``send_decide_invitation``, ack_result → ``send_ack``) and yields every
    other frame for dispatch. A ``None`` queue item ends the stream so the
    test can drive a reconnect."""

    def __init__(self) -> None:
        self._out: asyncio.Queue = asyncio.Queue()
        self._acks: dict[str, asyncio.Future] = {}
        self._decides: dict[str, asyncio.Future] = {}
        self._invites_waiters: asyncio.Queue = asyncio.Queue()
        self._ack_result_waiters: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.decisions: list[dict] = []
        self.acked: list[list[str]] = []
        self.list_invites_calls = 0
        self.invites_payload: list[dict] = []
        self.connect_count = 0
        self.fetch_pending_count = 0
        self.close_count = 0
        self.decide_gate: asyncio.Event | None = None
        self.decide_outcomes: dict[str, str] = {}
        self.list_error: Exception | None = None
        self.connected: asyncio.Event = asyncio.Event()

    async def connect(self) -> None:
        self.connect_count += 1
        self.connected.set()

    async def close(self) -> None:
        self.close_count += 1

    async def send_fetch_pending(self, *, limit=None) -> None:
        self.fetch_pending_count += 1

    async def send_send(self, **kwargs) -> dict:
        client_ref = kwargs.get("client_ref")
        fut = asyncio.get_running_loop().create_future()
        self._acks[client_ref] = fut
        self.sent.append(kwargs)
        await self._out.put({
            "type": "ack",
            "client_ref": client_ref,
            "envelope_id": f"env_{client_ref}",
            "thread_root_id": f"thread_{client_ref}",
        })
        return await asyncio.wait_for(fut, timeout=5)

    async def send_list_invites(self, *, timeout: float = 30.0) -> dict:
        self.list_invites_calls += 1
        if self.list_error is not None:
            raise self.list_error
        fut = asyncio.get_running_loop().create_future()
        await self._invites_waiters.put(fut)
        await self._out.put({"type": "invites", "invites": list(self.invites_payload)})
        return await asyncio.wait_for(fut, timeout=5)

    async def send_decide_invitation(self, **kwargs) -> dict:
        client_ref = kwargs["client_ref"]
        fut = asyncio.get_running_loop().create_future()
        self._decides[client_ref] = fut
        self.decisions.append(kwargs)
        if self.decide_gate is not None:
            await self.decide_gate.wait()
        outcome = self.decide_outcomes.get(kwargs["invitation_event_id"], "applied")
        await self._out.put({
            "type": "decide_invitation_result",
            "client_ref": client_ref,
            "invitation_event_id": kwargs["invitation_event_id"],
            "decision": kwargs["decision"],
            "outcome": outcome,
        })
        return await asyncio.wait_for(fut, timeout=5)

    async def send_ack(self, envelope_ids: list[str], *, timeout: float = 30.0) -> dict:
        fut = asyncio.get_running_loop().create_future()
        await self._ack_result_waiters.put(fut)
        self.acked.append(list(envelope_ids))
        await self._out.put({"type": "ack_result", "acked": list(envelope_ids)})
        return await asyncio.wait_for(fut, timeout=5)

    async def frames(self):
        while True:
            frame = await self._out.get()
            if frame is None:
                return
            kind = frame.get("type")
            if kind == "ack":
                ref = frame.get("client_ref")
                if ref in self._acks:
                    self._acks.pop(ref).set_result(frame)
                continue
            if kind == "invites":
                if not self._invites_waiters.empty():
                    self._invites_waiters.get_nowait().set_result(frame)
                continue
            if kind == "decide_invitation_result":
                ref = frame.get("client_ref")
                if ref in self._decides:
                    self._decides.pop(ref).set_result(frame)
                continue
            if kind == "ack_result":
                if not self._ack_result_waiters.empty():
                    self._ack_result_waiters.get_nowait().set_result(frame)
                continue
            yield frame


def _client(
    tmp_path,
    bridge,
    *,
    operator_slug: str = OPERATOR,
    auto_accept_space_invitations: bool = False,
    keyless: bool = True,
) -> PuffoCoreMessageClient:
    ks = KeyStore(str(tmp_path / "keys"))
    http = PuffoCoreHttpClient("http://127.0.0.1:1", ks, SELF, keyless=keyless)
    client = PuffoCoreMessageClient(
        slug=SELF,
        device_id="dev_test",
        space_id="sp_home",
        keystore=ks,
        http_client=http,
        message_store=MessageStore(str(tmp_path / "messages.db")),
        operator_slug=operator_slug,
        auto_accept_space_invitations=auto_accept_space_invitations,
        catchup_stale_hours=0,
        bridge_client=bridge,
    )
    return client


def _invite(
    event_id: str,
    *,
    scope: str,
    space_id: str,
    inviter: str = "alice-1",
    channel_id: str | None = None,
) -> dict:
    invite: dict = {
        "invitation_event_id": event_id,
        "scope": scope,
        "space_id": space_id,
        "inviter_slug": inviter,
    }
    if channel_id is not None:
        invite["channel_id"] = channel_id
    return invite


def _dm_frame(sender: str, content: str, *, seq: int, **extra) -> dict:
    frame = {
        "type": "message",
        "seq": seq,
        "envelope_id": f"env_{seq}",
        "envelope_kind": "dm",
        "sender_slug": sender,
        "recipient_slug": SELF,
        "content": content,
        "content_type": "text/plain",
        "sent_at": 1_700_000_000_000,
    }
    frame.update(extra)
    return frame


def _chan_frame(sender: str, content: str, *, seq: int) -> dict:
    return {
        "type": "message",
        "seq": seq,
        "envelope_id": f"env_{seq}",
        "envelope_kind": "channel",
        "sender_slug": sender,
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "content": content,
        "content_type": "text/plain",
        "sent_at": 1_700_000_000_000,
    }


async def _await_true(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached within timeout")
        await asyncio.sleep(0.01)


async def _await_envelope(client, envelope_id: str, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        row = await client.store.get_message_by_envelope(envelope_id)
        if row is not None:
            return row
        if loop.time() > deadline:
            raise AssertionError(f"envelope {envelope_id} never stored")
        await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def _run_listen(client):
    task = asyncio.ensure_future(client._listen_bridge())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


@pytest.mark.asyncio
async def test_invitation_lifecycle_reconciles_dedupes_and_stores_placeholder(
    tmp_path,
):
    """Guarded regression: repeated authoritative polls (pagination) and a
    correlated prompt/decision through the live pump must not prompt twice,
    adopt a channel invite without ``channel_id``, leak the prompt echo into
    model context, call a signed HTTP route, or give a native client a
    keyless flow."""
    bridge = _FakeBridge()
    client = _client(tmp_path, bridge=bridge)
    assert client._keyless_invitation_flow is not None
    await client.store.open()

    http_calls: list[str] = []

    async def _spy_get(path, *a, **k):
        http_calls.append(f"GET {path}")
        return {}

    async def _spy_post(path, *a, **k):
        http_calls.append(f"POST {path}")
        return {}

    client.http.get = _spy_get  # type: ignore[method-assign]
    client.http.post = _spy_post  # type: ignore[method-assign]

    async with _run_listen(client):
        await asyncio.wait_for(bridge.connected.wait(), timeout=5)
        flow = client._keyless_invitation_flow

        # One authoritative round: a valid space invite prompts once; a
        # channel invite missing channel_id is skipped entirely.
        bridge.invites_payload = [
            _invite("iv_1", scope="space", space_id="sp_1"),
            _invite("iv_bad", scope="channel", space_id="sp_1"),
        ]
        await bridge_transport_mod.poll_keyless_invites(client, flow)
        assert len(bridge.sent) == 1
        assert bridge.sent[0]["recipient_slug"] == OPERATOR
        assert "iv_bad" not in flow._pending

        # Pagination: the same authoritative list again must not re-prompt.
        await bridge_transport_mod.poll_keyless_invites(client, flow)
        assert len(bridge.sent) == 1
        assert flow._pending["iv_1"]["phase"] == "awaiting_reply"

        # The agent's own prompt echo is a control placeholder, not context.
        prompt_env = flow._pending["iv_1"]["prompt_envelope_id"]
        await bridge._out.put(
            _dm_frame(SELF, "echo of prompt", seq=1, envelope_id=prompt_env)
        )
        echo = await _await_envelope(client, prompt_env)
        assert echo.content == {
            "text": INVITE_PROMPT_PLACEHOLDER,
            "is_visible_to_human": True,
        }

        # An exact threaded operator reply is consumed to a terminal outcome.
        await bridge._out.put(
            _dm_frame(OPERATOR, " y ", seq=2, thread_root_id=prompt_env)
        )
        await _await_true(lambda: bool(bridge.decisions))
        await asyncio.gather(*tuple(client._ack_tasks))
        assert flow._pending["iv_1"]["phase"] == "terminal"
        assert bridge.decisions[0]["decision"] == "accept"

        # An operator-sent invite auto-accepts with no second prompt.
        bridge.invites_payload = [
            _invite("iv_1", scope="space", space_id="sp_1"),
            _invite("iv_op", scope="space", space_id="sp_2", inviter=OPERATOR),
        ]
        await bridge_transport_mod.poll_keyless_invites(client, flow)
        await _await_true(
            lambda: any(d["invitation_event_id"] == "iv_op" for d in bridge.decisions)
        )
        assert len(bridge.sent) == 1
        assert flow._pending["iv_op"]["phase"] == "terminal"

    # No signed HTTP invitation route was ever called.
    assert not any("spaces/events" in call for call in http_calls)
    await client.store.close()

    # A native client keeps no keyless flow.
    native_bridge = None
    native = _client(tmp_path, native_bridge, keyless=False)
    assert native._keyless_invitation_flow is None
    await native.store.close()


@pytest.mark.asyncio
async def test_operator_reply_is_tracked_and_pump_stays_live(tmp_path):
    """Guarded regression: the frame pump must never await the decision work
    inline. The reply dispatch returns while ``send_decide_invitation`` is
    parked, ordinary delivery continues, the pump resolves the decision to
    terminal, and a nonmatching operator DM keeps ordinary ingress."""
    bridge = _FakeBridge()
    client = _client(tmp_path, bridge=bridge)
    await client.store.open()

    async with _run_listen(client):
        await asyncio.wait_for(bridge.connected.wait(), timeout=5)
        flow = client._keyless_invitation_flow

        bridge.invites_payload = [_invite("iv_1", scope="space", space_id="sp_1")]
        await bridge_transport_mod.poll_keyless_invites(client, flow)
        prompt_env = flow._pending["iv_1"]["prompt_envelope_id"]

        bridge.decide_gate = asyncio.Event()
        reply = _dm_frame(OPERATOR, "yes", seq=1, thread_root_id=prompt_env)
        del reply["seq"]  # exercise the legacy sequence-less redelivery lane
        await bridge._out.put(reply)
        await _await_true(lambda: bool(bridge.decisions))
        assert any(not t.done() for t in tuple(client._ack_tasks))
        assert ["env_1"] not in bridge.acked

        # A redelivery while the decision is not yet durable must remain
        # unacked and rejoin the same serialized flow.
        await bridge._out.put(dict(reply))
        await asyncio.sleep(0.01)
        assert ["env_1"] not in bridge.acked

        # The pump still resolves ordinary delivery while the decision work
        # is parked inside send_decide_invitation.
        await bridge._out.put(_chan_frame("alice-1", "still alive", seq=2))
        row = await _await_envelope(client, "env_2")
        assert row.receipt_disposition == "eligible"

        # Releasing the decision lets the live pump resolve it to terminal.
        bridge.decide_gate.set()
        await asyncio.gather(*tuple(client._ack_tasks))
        assert flow._pending["iv_1"]["phase"] == "terminal"
        assert bridge.decisions[0]["decision"] == "accept"
        assert ["env_1"] in bridge.acked

        # A nonmatching operator DM (unknown thread) keeps ordinary ingress:
        # it is not consumed as a control reply and stays queued for the model.
        await bridge._out.put(
            _dm_frame(OPERATOR, "y", seq=3, thread_root_id="unknown_thread")
        )
        row3 = await _await_envelope(client, "env_3")
        assert row3.receipt_disposition == "eligible"
        pending_ids = [m.envelope_id for m in await client.store.get_pending()]
        assert "env_3" in pending_ids

    await client.store.close()


@pytest.mark.asyncio
async def test_poller_arms_once_per_connection_and_fails_soft(tmp_path):
    """Guarded regression: repeated ``pending_delivered`` markers must never
    spawn a second poller, the connection finally must cancel and await the
    poller, a reconnect must create exactly one fresh poller, and a failing
    poll round must be logged and retried without killing delivery."""
    bridge = _FakeBridge()
    client = _client(tmp_path, bridge=bridge)
    await client.store.open()

    async with _run_listen(client):
        await asyncio.wait_for(bridge.connected.wait(), timeout=5)
        assert client._keyless_invite_poll_task is None

        await bridge._out.put({"type": "pending_delivered", "count": 1})
        await _await_true(lambda: client._keyless_invite_poll_task is not None)
        poller = client._keyless_invite_poll_task
        assert not poller.done()

        # A repeated marker must not replace the slot with a second poller.
        await bridge._out.put({"type": "pending_delivered", "count": 2})
        await asyncio.sleep(0.01)
        assert client._keyless_invite_poll_task is poller

        # A failing poll round is fail-soft: it returns, the poller keeps
        # its cadence (still pending), and delivery continues.
        bridge.list_error = RuntimeError("list invites boom")
        await bridge_transport_mod.poll_keyless_invites(
            client, client._keyless_invitation_flow
        )
        assert not poller.done()
        await bridge._out.put(_chan_frame("alice-1", "still alive", seq=1))
        await _await_envelope(client, "env_1")

        # Ending the connection cancels and awaits the poller, clearing the
        # ownership slot for the reconnect. The native cadence helper absorbs
        # the cancellation and returns, so the task is done (never leaked),
        # not flagged cancelled.
        await bridge._out.put(None)
        await _await_true(lambda: client._keyless_invite_poll_task is None)
        await _await_true(lambda: poller.done())
        await _await_true(lambda: bridge.close_count == 1)
        await _await_true(lambda: bridge.connect_count == 2)

        # The fresh connection arms exactly one fresh poller.
        await bridge._out.put({"type": "pending_delivered", "count": 1})
        await _await_true(lambda: client._keyless_invite_poll_task is not None)
        assert client._keyless_invite_poll_task is not poller

    await client.store.close()
