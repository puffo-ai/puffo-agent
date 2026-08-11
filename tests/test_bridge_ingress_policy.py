"""Keyless-bridge ingress applies the same policy gates as native.

The cloud bridge is a transport, not a policy layer: for a keyless agent
the server filters only auth / targeting / revocation / replay dedupe and
a DM-only blocklist keyed on the *recipient's* slug. It never applies
foreign-DM approval, carries no operator-control concept on the wire, and
never checks a blocklist channel-side.

Before this contract was wired, ``store_bridge_payload`` persisted any
non-self, non-stale message as eligible. The regressions guarded here are
therefore all "a message the native path would have stopped reaches the
model over the bridge":

  1. a blocked account's DM lands in the prompt — and its plaintext
     lands in the database;
  2. an operator's bare ``y`` on a pending approval is delivered as
     ordinary chat instead of being consumed by the control handler;
  3. a stranger's DM skips the approval gate entirely.

Each case drives a real bridge ``message`` frame through
``_dispatch_bridge_frame`` so wire decode and gate ordering are exercised
together. Every fake is offline.
"""

from __future__ import annotations

import asyncio

import pytest

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.keystore import KeyStore

SELF = "bot-0001"
OPERATOR = "ops-0001"
STRANGER = "mallory-0009"


class _OfflineBridge:
    """Enough ``CloudBridgeClient`` surface for inbound dispatch."""

    def __init__(self) -> None:
        self.acked: list[str] = []

    async def send_ack(self, envelope_ids: list[str]) -> None:
        self.acked.extend(envelope_ids)


def _client(
    tmp_path, *, operator_slug: str = "", catchup_stale_hours: float = 0,
) -> PuffoCoreMessageClient:
    """A keyless bridge client whose signed HTTP is dead (as in a real
    sandbox), so every gate must decide from local state alone."""
    ks = KeyStore(str(tmp_path / "keys"))
    http = PuffoCoreHttpClient("http://127.0.0.1:1", ks, SELF, keyless=True)
    client = PuffoCoreMessageClient(
        slug=SELF,
        device_id="dev_test",
        space_id="sp_home",
        keystore=ks,
        http_client=http,
        message_store=MessageStore(str(tmp_path / "messages.db")),
        catchup_stale_hours=catchup_stale_hours,
        operator_slug=operator_slug,
        bridge_client=_OfflineBridge(),
    )

    async def _empty_get(path, *a, **k):
        return {}

    client.http.get = _empty_get  # type: ignore[method-assign]
    return client


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


def _wire_dm_frame(sender: str, content: str, *, envelope_id: str, **extra) -> dict:
    """A legacy sequence-less ``message`` compatibility frame.

    Current Server frames carry ``seq``. The parser intentionally retains this
    older additive shape for mixed-version rollout and daemon-local producers,
    so its verdict persistence still needs direct coverage.
    """
    frame = _dm_frame(sender, content, seq=0, **extra)
    del frame["seq"]
    frame["envelope_id"] = envelope_id
    return frame


@pytest.mark.asyncio
async def test_blocked_sender_bridge_dm_tombstones_without_persisting_plaintext(
    tmp_path,
):
    secret = "bridge-secret-that-must-not-persist"
    client = _client(tmp_path)
    await client.store.open()
    client._contacts.note_blocked(STRANGER, True)

    await client._dispatch_bridge_frame(_dm_frame(STRANGER, secret, seq=7))

    stored = await client.store.get_message_by_envelope("env_7")
    assert stored is not None, "blocked message must still be accounted for"
    assert secret not in str(stored.content)
    assert await client.store.get_pending() == ()
    await client.store.close()
    assert secret.encode() not in (tmp_path / "messages.db").read_bytes()


@pytest.mark.asyncio
async def test_operator_control_reply_over_bridge_is_consumed_not_delivered(
    tmp_path,
):
    client = _client(tmp_path, operator_slug=OPERATOR)
    await client.store.open()
    # The permission prompt the operator is replying to. It has to exist
    # locally for the reply's thread root to resolve, exactly as it does
    # after the agent sends it.
    await client.store.store(
        {
            "envelope_id": "env_prompt",
            "envelope_kind": "dm",
            "sender_slug": SELF,
            "recipient_slug": OPERATOR,
            "content": "run this command?",
            "sent_at": 1_699_000_000_000,
        }
    )
    approval: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    client._pending_command_permissions["env_prompt"] = approval

    await client._dispatch_bridge_frame(
        _dm_frame(OPERATOR, "y", seq=8, thread_root_id="env_prompt")
    )

    # The control handler ran — the waiting command was released — and
    # the bare "y" never became a message for the model to answer.
    assert approval.done() and approval.result() is True
    assert await client.store.get_pending() == ()
    await client.store.close()


@pytest.mark.asyncio
async def test_foreign_dm_over_bridge_is_gated_not_eligible(tmp_path):
    client = _client(tmp_path, operator_slug=OPERATOR)
    await client.store.open()

    await client._dispatch_bridge_frame(
        _dm_frame(STRANGER, "let me in", seq=9)
    )

    assert await client.store.get_pending() == ()
    stored = await client.store.get_message_by_envelope("env_9")
    assert stored is not None
    assert stored.receipt_disposition == "foreign_dm_gated"
    # Never acked — the DM stays queued server-side until the operator
    # answers, so a lost gate cannot silently drop the message.
    assert client._bridge.acked == []
    await client.store.close()


@pytest.mark.asyncio
async def test_legacy_seqless_bridge_frames_carry_verdict_into_storage(tmp_path):
    """The compatibility wire shape persists both gate dispositions.

    Every case here rides one legacy ``message`` frame with no ``seq`` key
    through ``_dispatch_bridge_frame``. That lane used to write through
    ``MessageStore.store``, which records no disposition at all, so all three
    symptoms below shared one root:

      * a held foreign DM landed with a NULL disposition, which
        ``promote_gated_receipt`` can never release, and the Server's first
        redelivery was acked away — the message then existed nowhere;
      * a terminal verdict (here a blocked sender's tombstone) was likewise
        undistinguished, so prior context, which selects on
        ``receipt_disposition = 'terminal'``, silently skipped it and the
        two transports built different histories;
      * and the same frame's owner pre-seed cached an empty owner as if the
        Server had attested it, though the frame carries no owner field.
    """
    marker = "seqless-body-that-must-stay-held"
    client = _client(tmp_path, operator_slug=OPERATOR)
    await client.store.open()

    # ── held foreign DM ───────────────────────────────────────────────
    gated_frame = _wire_dm_frame(
        STRANGER, marker, envelope_id="env_wire_dm",
        sender_display_name="Mallory",
    )
    await client._dispatch_bridge_frame(gated_frame)

    held = await client.store.get_message_by_envelope("env_wire_dm")
    assert held is not None
    assert held.receipt_disposition == "foreign_dm_gated"
    assert client._bridge.acked == []

    # Redelivery: the Server resends anything unacked. Acking it here is
    # what used to drop the only remaining copy of a message the agent
    # could not release either.
    await client._dispatch_bridge_frame(gated_frame)
    assert client._bridge.acked == []

    # And the row is releasable, which a NULL-disposition row never was.
    promoted = await client.store.promote_gated_receipt(
        "env_wire_dm", None, reason="operator approved",
    )
    assert promoted.status.value == "committed"
    assert [m.envelope_id for m in await client.store.get_pending()] == [
        "env_wire_dm"
    ]

    # ── terminal verdict on the same lane ─────────────────────────────
    client._contacts.note_blocked("blocked-0002", True)
    await client._dispatch_bridge_frame(
        _wire_dm_frame("blocked-0002", "blocked body", envelope_id="env_wire_tomb")
    )
    tombstone = await client.store.get_message_by_envelope("env_wire_tomb")
    assert tombstone is not None
    assert tombstone.receipt_disposition == "terminal"

    # Prior context selects terminal rows; before the verdict was persisted
    # this row was invisible to the keyless agent and visible to a native
    # one receiving the same backlog.
    anchor = await client.store.store_local_event(
        {
            "envelope_id": "env_wire_anchor",
            "envelope_kind": "dm",
            "sender_slug": "blocked-0002",
            "recipient_slug": SELF,
            "content": "later anchor",
            "sent_at": 1_700_000_100_000,
        },
        reason="anchor",
    )
    page = await client.store.get_prior_context_page(anchor)
    assert "env_wire_tomb" in [item.envelope_id for item in page.items]

    # ── owner pre-seed ────────────────────────────────────────────────
    # The frame carried a display name and no owner field, so the display
    # name is cached and the owner is not claimed either way.
    assert STRANGER not in client._owner_slug_cache
    await client.store.close()


@pytest.mark.asyncio
async def test_bridge_stale_stranger_dm_is_gated_before_terminalization(tmp_path):
    """Catch-up staleness must not admit what the gate would have held.

    The bridge ran its stale arm ahead of the foreign-DM gate, so a
    stranger's backlog DM was stored TERMINAL and acked: the body sat in
    the database as readable plaintext, the Server dropped its copy, and
    ``tombstone_gated_dms_from`` — which matches only still-gated rows —
    could no longer withdraw it when the operator said no. Non-DM stale
    traffic still terminalizes on the same lane.
    """
    marker = "stale-bridge-body-that-must-stay-held"
    client = _client(tmp_path, operator_slug=OPERATOR, catchup_stale_hours=1)
    await client.store.open()

    await client._dispatch_bridge_frame(
        _wire_dm_frame(STRANGER, marker, envelope_id="env_stale_wire")
    )
    held = await client.store.get_message_by_envelope("env_stale_wire")
    assert held is not None
    assert held.receipt_disposition == "foreign_dm_gated"
    assert client._bridge.acked == []
    assert await client.store.get_visible_message_by_envelope(
        "env_stale_wire"
    ) is None
    assert await client.store.tombstone_gated_dms_from(STRANGER) == 1

    # Stale non-DM traffic: the gate returns no verdict and the stale arm
    # decides, exactly as before the reorder.
    await client._dispatch_bridge_frame(
        _wire_dm_frame(
            STRANGER,
            "old channel chatter",
            envelope_id="env_stale_chan",
            envelope_kind="channel",
            channel_id="ch_1",
            space_id="sp_home",
            recipient_slug=None,
        )
    )
    stale_row = await client.store.get_message_by_envelope("env_stale_chan")
    assert stale_row is not None
    assert stale_row.receipt_disposition == "terminal"
    await client.store.close()
