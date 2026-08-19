"""PUF-227-A: receiver-side strict cache-validation on admit.

When an incoming envelope arrives, ``handle_envelope`` calls
``_validate_incoming_parent_id`` on both ``thread_root_id`` and
``reply_to_id``. Anything that doesn't point to a same-channel
parent in our local ``message_store`` gets wiped to ``None``
before being stored / queued. Same Scout-class threat-model as the
sender side, enforced symmetrically.

These tests exercise the helper directly via a minimally-
constructed ``PuffoCoreMessageClient`` (the full WS / decryption
stack isn't needed to exercise the validation logic).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.inbound_receipts import InboundReceiptHandler
from puffo_agent.agent.client_support import DM_GATE_PROMPT_PLACEHOLDER
from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.keystore import KeyStore
from puffo_agent.crypto.message import MessagePayload
from puffo_agent.crypto.ws_client import TransportOutcome
from puffo_agent.mcp.core_post_tools import _get_post_segment


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _make_store() -> MessageStore:
    d = tempfile.mkdtemp()
    store = MessageStore(os.path.join(d, "messages.db"))
    await store.open()
    return store


def _bare_client(store: MessageStore) -> PuffoCoreMessageClient:
    """Build a PuffoCoreMessageClient with just enough state to
    exercise ``_validate_incoming_parent_id``. Bypasses __init__
    because the real constructor needs a keystore + http + WS
    bookkeeping we don't need here."""
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.store = store
    return client


async def _seed_parent(
    store: MessageStore,
    *,
    envelope_id: str,
    channel_id: str | None,
    space_id: str | None,
) -> None:
    await store.store({
        "envelope_id": envelope_id,
        "envelope_kind": "channel" if channel_id else "dm",
        "sender_slug": "sam-0001",
        "channel_id": channel_id,
        "space_id": space_id,
        "content_type": "text/plain",
        "content": "parent root post",
        "sent_at": _now_ms(),
        "thread_root_id": None,
        "reply_to_id": None,
    })


# ── pass-through ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_passes_through_when_parent_id_is_none():
    store = await _make_store()
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(None, "ch_x", "sp_1")
    assert out is None
    out = await client._validate_incoming_parent_id("", "ch_x", "sp_1")
    assert out == ""
    await store.close()


@pytest.mark.asyncio
async def test_validate_preserves_id_when_parent_in_same_channel():
    """Valid same-channel parent → id passes through; agent sees
    the thread linkage."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_root", channel_id="ch_gtm", space_id="sp_1",
    )
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(
        "env_root", "ch_gtm", "sp_1",
    )
    assert out == "env_root"
    await store.close()


# ── wipes ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_wipes_when_parent_not_in_local_cache():
    """Strict-(a): parent envelope unknown to this client → wipe.
    Forward-traffic enforcement, no migration of historical state."""
    store = await _make_store()
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(
        "env_unknown", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


@pytest.mark.asyncio
async def test_validate_wipes_when_parent_in_different_channel():
    """Scout's PUF-227 symptom on the receiver side. Local cache
    HAS the parent envelope but it lives in a different channel
    than the incoming envelope claims — strict invariant says wipe.
    Without this wipe, the agent's batch coalescer would inherit
    the parent's channel context for the new envelope (the exact
    Scout-class symptom)."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_root_general",
        channel_id="ch_general", space_id="sp_1",
    )
    client = _bare_client(store)
    # Incoming claims thread_root_id=env_root_general but arrives
    # in channel ch_gtm.
    out = await client._validate_incoming_parent_id(
        "env_root_general", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


@pytest.mark.asyncio
async def test_validate_wipes_when_parent_in_different_space():
    """Cross-space parent → also wiped (belt-and-braces alongside
    channel-mismatch)."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_other_space",
        channel_id="ch_gtm", space_id="sp_OTHER",
    )
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(
        "env_other_space", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


# ── DM context ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_dm_envelope_skips_channel_check_but_keeps_cache_check():
    """Incoming DM envelope (no channel_id). Cache-presence check
    still fires; channel-match check is naturally a no-op when
    expected_channel_id is None."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_dm_root", channel_id=None, space_id=None,
    )
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(
        "env_dm_root", None, None,
    )
    assert out == "env_dm_root"
    # Unknown DM parent → wipe.
    out = await client._validate_incoming_parent_id(
        "env_dm_unknown", None, None,
    )
    assert out is None
    await store.close()


@pytest.mark.asyncio
async def test_validate_dm_parent_requires_same_authenticated_peer():
    store = await _make_store()
    await store.store({
        "envelope_id": "other-dm-root",
        "envelope_kind": "dm",
        "sender_slug": "stranger-0003",
        "recipient_slug": SELF_SLUG,
        "content_type": "text/plain",
        "content": "other conversation",
        "sent_at": _now_ms(),
    })
    client = _bare_client(store)
    client.slug = SELF_SLUG

    out = await client._validate_incoming_parent_id(
        "other-dm-root",
        None,
        None,
        expected_envelope_kind="dm",
        expected_dm_peer=FRIEND_SLUG,
    )

    assert out is None
    await store.close()


# ── lookup transport error ────────────────────────────────────────


# ── dual-call shape (reply_to_id symmetry) ────────────────────────


@pytest.mark.asyncio
async def test_validate_reply_to_id_uses_same_helper():
    """PUF-227-A handle_envelope calls _validate_incoming_parent_id
    on BOTH thread_root_id AND reply_to_id with the same channel /
    space expectations. This test pins that dual-call shape: the
    same helper, the same args, applied to both fields, with the
    same wipe semantics. Operator's review #1 ask — without this
    test a future refactor could accidentally drop the reply_to_id
    side and the regression wouldn't surface until a customer hit
    a cross-channel reply chain."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_other_chan",
        channel_id="ch_other", space_id="sp_1",
    )
    client = _bare_client(store)

    # Same parent envelope, same outbound channel/space args, same
    # cross-channel mismatch — applied to both id roles. Both return
    # None (wiped) under the strict invariant.
    thread_wiped = await client._validate_incoming_parent_id(
        "env_other_chan", "ch_gtm", "sp_1",
    )
    reply_wiped = await client._validate_incoming_parent_id(
        "env_other_chan", "ch_gtm", "sp_1",
    )
    assert thread_wiped is None
    assert reply_wiped is None

    # And the same helper preserves both when same-channel.
    await _seed_parent(
        store, envelope_id="env_same_chan",
        channel_id="ch_gtm", space_id="sp_1",
    )
    thread_kept = await client._validate_incoming_parent_id(
        "env_same_chan", "ch_gtm", "sp_1",
    )
    reply_kept = await client._validate_incoming_parent_id(
        "env_same_chan", "ch_gtm", "sp_1",
    )
    assert thread_kept == "env_same_chan"
    assert reply_kept == "env_same_chan"
    await store.close()


@pytest.mark.asyncio
async def test_validate_wipes_on_store_lookup_exception(monkeypatch):
    """Sqlite hiccup mid-lookup → strict mode wipes rather than
    shipping an unverifiable id."""
    store = await _make_store()
    client = _bare_client(store)

    async def boom(envelope_id):
        raise RuntimeError("simulated sqlite hiccup")

    monkeypatch.setattr(store, "get_message_by_envelope", boom)
    out = await client._validate_incoming_parent_id(
        "env_anything", "ch_x", "sp_1",
    )
    assert out is None
    await store.close()


# ── native ingress boundary ───────────────────────────────────────
#
# Everything below drives the real ``InboundReceiptHandler.handle``
# sequence — blocked gate → self echo → operator control → stale →
# foreign DM → eligible — with the crypto seam stubbed out. It is one
# scenario because the failures it pins are one failure: content from a
# sender the gate has not cleared reaching somewhere the model can read
# it, or the gate answering without the facts to answer.

SELF_SLUG = "bot-0001"
OPERATOR_SLUG = "ops-0001"
STRANGER_SLUG = "mallory-0009"
FRIEND_SLUG = "friend-0002"


class _ScriptedHttp:
    """Signed HTTP whose ``/blocklists`` reachability the test controls."""

    def __init__(self) -> None:
        self.keyless = False
        self.blocklist_reachable = False

    async def get(self, path, *a, **k):
        if path in ("/allowlists", "/blocklists"):
            if not self.blocklist_reachable:
                raise ConnectionError("simulated Puffo Server incident")
            return {"entries": [], "blocks": []}
        return {}

    async def post(self, path, body=None, *a, **k):
        return {}


def _native_client(
    tmp_path, *, catchup_stale_hours: float = 0,
) -> PuffoCoreMessageClient:
    """A signed (non-bridge) client whose workspace is on disk."""
    ks = KeyStore(str(tmp_path / "keys"))
    real_http = PuffoCoreHttpClient("http://127.0.0.1:1", ks, SELF_SLUG)
    client = PuffoCoreMessageClient(
        slug=SELF_SLUG,
        device_id="dev_test",
        space_id="sp_home",
        keystore=ks,
        http_client=real_http,
        message_store=MessageStore(str(tmp_path / "messages.db")),
        catchup_stale_hours=catchup_stale_hours,
        operator_slug=OPERATOR_SLUG,
        workspace=str(tmp_path / "ws"),
    )
    client.http = _ScriptedHttp()
    client._contacts._http = client.http
    return client


def _handler(client, payloads: dict[str, MessagePayload]) -> InboundReceiptHandler:
    """Handler with the decrypt seam replaced by a lookup table."""

    async def _keys(_slug):
        return [b"signing-key"]

    client._key_cache.get_signing_keys = _keys  # type: ignore[method-assign]
    return InboundReceiptHandler(
        client,
        kem_keypair=None,
        decrypt=lambda envelope, *a, **k: payloads[envelope["envelope_id"]],
        read_plaintext=lambda envelope, *a, **k: payloads[envelope["envelope_id"]],
    )


def _dm_payload(sender: str, content, *, envelope_id: str, content_type="text/plain"):
    return MessagePayload(
        payload_type="message",
        version=1,
        envelope_id=envelope_id,
        envelope_kind="dm",
        sender_slug=sender,
        sender_subkey_id="sk_1",
        sent_at=1_700_000_000_000,
        message_nonce="n",
        content_type=content_type,
        content=content,
        is_visible_to_human=True,
        recipient_slug=OPERATOR_SLUG if sender == SELF_SLUG else SELF_SLUG,
    )


def _delivery(envelope_id: str, sender: str, seq: int) -> dict:
    return {
        "seq": seq,
        "envelope": {"envelope_id": envelope_id, "sender_slug": sender},
    }


def _stub_side_effects(client, inbox_root: Path):
    """Replace the gate's outbound side effects with recorders.

    Returns ``(saved, sent_dms)``, which the assertions read to tell "the
    gate decided before this ran" from "it decided after".
    """
    saved: list[str] = []
    sent_dms: list[tuple[str, str]] = []

    async def _record_save(*, envelope_id: str, metas_raw):
        # Mirrors the real saver's first act — the directory exists from the
        # moment it is called, before any blob is fetched.
        (inbox_root / envelope_id).mkdir(parents=True, exist_ok=True)
        saved.append(envelope_id)
        return []

    async def _send_dm(recipient, text, root_id="", require_encryption=False):
        sent_dms.append((recipient, text))
        return {"envelope_id": "env_prompt"}

    async def _no_shared_space(_slug):
        return False

    client._save_inbound_attachments = _record_save  # type: ignore[method-assign]
    client._send_dm = _send_dm  # type: ignore[method-assign]
    client._shares_space_with = _no_shared_space  # type: ignore[method-assign]
    return saved, sent_dms


def _attachment_content(text: str) -> dict:
    return {
        "text": text,
        "attachments": [
            {
                "blob_id": "blob_1",
                "filename": "payload.txt",
                "mime_type": "text/plain",
                "size_bytes": 4,
                "nonce": "n",
                "key": "k",
            }
        ],
    }


@pytest.mark.asyncio
async def test_native_ingress_withholds_uncleared_sender_content(tmp_path, monkeypatch):
    """The native gate decides before, and reveals nothing after.

    Four production failures, one boundary:

      * ``handle`` extracted content — which downloads, decrypts, and
        writes every attachment into the model-readable workspace —
        *before* running the foreign-DM gate, so an unapproved stranger
        got a file write no approval outcome ever undid;
      * the approval prompt the gate sends quotes 280 characters of the
        withheld body, and the Server's echo of it was stored verbatim
        and terminal, which prior context selects — so the gate leaked
        through its own mechanism;
      * ``blocked_gate`` answered "not blocked" from a cache that had
        never been read, so a restart during a server incident admitted
        blocked senders;
      * and once the blocklist is readable the same delivery must resolve
        normally, or the hold becomes its own outage.
    """
    marker = "stranger-body-the-operator-has-not-approved"
    # The gate persists pending approvals under the agent home; keep them
    # in the tmp tree so a run neither reads nor leaves real state.
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "home"))
    client = _native_client(tmp_path)
    await client.store.open()
    inbox_root = Path(client.workspace) / ".puffo" / "inbox"
    saved, sent_dms = _stub_side_effects(client, inbox_root)

    payloads = {
        "env_stranger": _dm_payload(
            STRANGER_SLUG,
            _attachment_content(marker),
            envelope_id="env_stranger",
            content_type="puffo/message+attachments/v1",
        ),
    }
    handler = _handler(client, payloads)
    # ── cold-start blocklist: unreadable ⇒ hold, not admit ────────────
    outcome = await handler.handle(_delivery("env_stranger", STRANGER_SLUG, 1))
    assert outcome is TransportOutcome.HOLD
    assert await client.store.get_message_by_envelope("env_stranger") is None
    assert saved == []

    # ── blocklist readable ⇒ the same delivery resolves normally ─────
    client.http.blocklist_reachable = True
    outcome = await handler.handle(_delivery("env_stranger", STRANGER_SLUG, 1))

    # Gated, and — the point — nothing of the stranger's reached disk.
    stored = await client.store.get_message_by_envelope("env_stranger")
    assert stored is not None
    assert stored.receipt_disposition == "foreign_dm_gated"
    assert outcome is not TransportOutcome.ACK
    assert saved == []
    assert not (inbox_root / "env_stranger").exists()

    # ── the prompt's own echo must not carry the withheld body ───────
    # The operator gets an FYI notice first, then the approval prompt.
    prompt_text = [text for slug, text in sent_dms if slug == OPERATOR_SLUG][-1]
    assert marker in prompt_text, "the operator does see the preview"
    assert "env_prompt" in client._pending_dm_approvals

    payloads["env_prompt"] = _dm_payload(
        SELF_SLUG, prompt_text, envelope_id="env_prompt"
    )
    await handler.handle(_delivery("env_prompt", SELF_SLUG, 2))

    echo = await client.store.get_message_by_envelope("env_prompt")
    assert echo is not None and marker not in str(echo.content)
    assert echo.content == {"text": DM_GATE_PROMPT_PLACEHOLDER, "is_visible_to_human": True}

    operator_anchor = await client.store.store_local_event(
        {
            "envelope_id": "env_op_anchor",
            "envelope_kind": "dm",
            "sender_slug": OPERATOR_SLUG,
            "recipient_slug": SELF_SLUG,
            "content": "anything at all",
            "sent_at": 1_700_000_100_000,
        },
        reason="operator anchor",
    )
    page = await client.store.get_prior_context_page(operator_anchor)
    assert marker not in str([item.content for item in page.items])

    # ── admitted path still materializes attachments ─────────────────
    client._contacts.note_allowed(FRIEND_SLUG)
    payloads["env_friend"] = _dm_payload(
        FRIEND_SLUG,
        _attachment_content("hello from a cleared sender"),
        envelope_id="env_friend",
        content_type="puffo/message+attachments/v1",
    )
    outcome = await handler.handle(_delivery("env_friend", FRIEND_SLUG, 3))
    assert outcome is TransportOutcome.ACK
    assert saved == ["env_friend"]
    assert (inbox_root / "env_friend").exists()
    stored = await client.store.get_visible_message_by_envelope("env_friend")
    assert stored is not None
    assert "original_content" not in stored.content
    await client.store.close()


@pytest.mark.asyncio
async def test_native_long_message_keeps_segment_source_after_prompt_redaction(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "home"))
    client = _native_client(tmp_path)
    client._max_inline_chars = 100
    client._segment_chars = 80
    await client.store.open()
    client.http.blocklist_reachable = True
    client._contacts.note_allowed(FRIEND_SLUG)

    original = "0123456789" * 25
    payloads = {
        "env_long": _dm_payload(
            FRIEND_SLUG,
            original,
            envelope_id="env_long",
        ),
    }
    outcome = await _handler(client, payloads).handle(
        _delivery("env_long", FRIEND_SLUG, 1)
    )
    assert outcome is TransportOutcome.ACK

    stored = await client.store.get_visible_message_by_envelope("env_long")
    assert stored is not None
    assert "inbound message was too long" in stored.content["text"]
    assert stored.content["original_content"] == original

    class _StoreDataClient:
        async def get_message_by_envelope(self, envelope_id):
            return await client.store.get_visible_message_by_envelope(envelope_id)

    segment = await _get_post_segment(
        SimpleNamespace(
            slug=SELF_SLUG,
            agent_id=SELF_SLUG,
            data_client=_StoreDataClient(),
        ),
        "env_long",
        1,
        80,
    )
    assert segment["segment"]["count"] == 4
    assert segment["segment"]["text"] == original[80:160]
    await client.store.close()


@pytest.mark.asyncio
async def test_catchup_stale_stranger_dm_is_gated_before_terminalization(
    tmp_path, monkeypatch
):
    """Staleness is a freshness judgement, not an admission one.

    A stranger's DM arriving during catch-up used to hit the stale arm
    first and land TERMINAL: acked away server-side, body stored as
    model-readable plaintext, and out of reach of
    ``tombstone_gated_dms_from``, which only matches still-gated rows — so
    a later denial could no longer withdraw what the operator refused. The
    gate now decides first, while a cleared sender's stale message still
    terminalizes exactly as before.
    """
    marker = "stale-stranger-body-the-operator-has-not-approved"
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "home"))
    client = _native_client(tmp_path, catchup_stale_hours=1)
    await client.store.open()
    client.http.blocklist_reachable = True
    _stub_side_effects(client, Path(client.workspace) / ".puffo" / "inbox")

    payloads = {
        "env_stale_stranger": _dm_payload(
            STRANGER_SLUG, marker, envelope_id="env_stale_stranger",
        ),
        "env_stale_friend": _dm_payload(
            FRIEND_SLUG, "old but cleared", envelope_id="env_stale_friend",
        ),
    }
    handler = _handler(client, payloads)
    assert client._is_stale_for_catchup(payloads["env_stale_stranger"].sent_at)

    outcome = await handler.handle(
        _delivery("env_stale_stranger", STRANGER_SLUG, 1)
    )
    assert outcome is not TransportOutcome.ACK
    held = await client.store.get_message_by_envelope("env_stale_stranger")
    assert held is not None
    assert held.receipt_disposition == "foreign_dm_gated"
    assert await client.store.get_visible_message_by_envelope(
        "env_stale_stranger"
    ) is None

    # A cleared sender's stale message still terminalizes and still acks.
    client._contacts.note_allowed(FRIEND_SLUG)
    friend_outcome = await handler.handle(
        _delivery("env_stale_friend", FRIEND_SLUG, 2)
    )
    assert friend_outcome is TransportOutcome.ACK
    friend_row = await client.store.get_message_by_envelope("env_stale_friend")
    assert friend_row is not None
    assert friend_row.receipt_disposition == "terminal"

    # Denial still reaches the held row — the guarantee terminalizing lost.
    assert await client.store.tombstone_gated_dms_from(STRANGER_SLUG) == 1
    tombstoned = await client.store.get_message_by_envelope("env_stale_stranger")
    assert tombstoned is not None
    assert marker not in str(tombstoned.content)
    await client.store.close()
