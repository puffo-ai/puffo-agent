"""Keyless cloud-bridge transport strategy for Puffo message delivery.

The public message client remains the compatibility facade. These functions
own bridge wire decoding, persistence, ACK scheduling, and blob handling while
calling the facade for shared policy such as directory and thread resolution.

Ingress policy contract
-----------------------
The bridge is a *transport*, not a policy layer. For a keyless agent the
server guarantees only sender authentication / cert-chain validation,
device targeting, revocation, replay dedupe, and a DM-only blocklist row
keyed on the recipient's own slug. It applies no foreign-DM approval, has
no operator-control concept on the wire, never checks a blocklist
channel-side, and does not filter at relay/backfill time.

Blocked contacts (DM and channel), operator control messages, and
foreign-DM approval are therefore Agent-enforced here, through the very
same gates the native path runs — see ``ingress_policy``, which holds the
single implementation and the cited server evidence. ``store_bridge_payload``
runs them in the native order (blocked → self echo → operator control →
foreign DM → stale → eligible) and only maps each verdict onto the bridge's
own storage lane.

Known keyless DM gaps: outbound allowlisting on self-echo still needs a
signed ``POST /allowlists`` this transport cannot make, so an approved
foreign DM is admitted via local ``ContactCache`` state only. The foreign-DM
operator approval prompt itself is a plain bridge ``send_send`` DM, and the
same local contact state records the decision; there is no signed approval
control plane involved.

Keyless invitations: each real keyless bridge client constructs exactly one
durable ``KeylessInvitationFlow``. ``listen_bridge`` owns one
connection-scoped poller that reads the Server's authoritative pending list
with ``send_list_invites``, reconciles it into the durable flow, and resumes
any persisted prompt/decision work. The configured operator is prompted only
through ``send_send``; exact threaded ``y``/``yes``/``n``/``no`` replies are
consumed as tracked work, and no signed HTTP invitation route is used.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from ..crypto.message import MessagePayload
from ..crypto.ws_client import INITIAL_BACKOFF, MAX_BACKOFF
from ..limits import (
    MAX_INBOUND_ATTACHMENTS,
    MAX_INBOUND_ATTACHMENT_BYTES,
    MAX_INBOUND_ATTACHMENT_TOTAL_BYTES,
)
from . import disk_cache
from .client_support import (
    DM_GATE_PROMPT_PLACEHOLDER,
    INVITE_PROMPT_PLACEHOLDER,
)
from .inbound_attachments import (
    downscale_oversized_image,
    is_safe_path_component,
    normalize_saved_image,
)
from .contact_cache import BlocklistUnavailable
from .ingress_policy import (
    GateVerdict,
    blocked_gate,
    foreign_dm_gate,
    operator_control_gate,
)
from .keyless_dm_approval_flow import (
    is_keyless_operator_approval_reply,
    is_keyless_prompt_envelope,
    maybe_handle_operator_reply,
    record_gated_dm,
    resume_pending_approvals,
)
from .membership_events import invite_poll_loop
from .message_context import (
    MENTION_RE,
    content_with_visibility,
    maybe_redact_long_text,
)
from .message_store import ReceiptDisposition, ReceiptWriteStatus

KEYLESS_DM_REPLY_REASON = "handled keyless dm approval reply"
KEYLESS_INVITATION_REPLY_REASON = "handled keyless invitation reply"


async def listen_bridge(client) -> None:
    """Bridge-transport lifecycle loop: connect → fetch_pending →
    drain frames → reconnect, with the same backoff schedule as the
    native ``PuffoCoreWsClient.run()`` (start at INITIAL_BACKOFF,
    double on failure, cap at MAX_BACKOFF, reset on successful
    connect).

    Each successfully decoded message is persisted before its bridge ACK
    is scheduled. A fresh ``connect()`` drives ``send_fetch_pending()``
    once so a cold sandbox drains its server-side queue.

    Owns exactly one keyless invitation poller per connection: armed when
    the first ``pending_delivered`` marker proves the frame pump is live
    enough to resolve correlated replies, and cancelled and awaited in the
    connection's ``finally`` so a reconnect creates one fresh poller and
    never leaks or duplicates background work.

    Deliberately does NOT start the native ``_invite_poll_loop`` /
    ``_warm_member_caches`` — they drive signed HTTP endpoints that
    can't work keyless. Current Server message frames pre-seed sender identity;
    other uncached directory entries degrade to their stable slugs.
    """
    bridge = client._bridge
    assert bridge is not None  # guarded by listen()

    await client.store.open()
    backoff = INITIAL_BACKOFF
    while True:
        try:
            try:
                await bridge.connect()
                backoff = INITIAL_BACKOFF
                await bridge.send_fetch_pending()
                client._keyless_invite_poll_task = None
                async for frame in bridge.frames():
                    if frame.get("type") == "pending_delivered":
                        _arm_keyless_invite_poller(client)
                    await client._dispatch_bridge_frame(frame)
            finally:
                await _stop_keyless_invite_poller(client)
                await bridge.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect on transport failure
            client._log.warning(
                "agent %s: bridge reconnect category=bridge_transport "
                "exception=%s retry_delay=%ds",
                client.slug,
                type(exc).__name__,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


def _arm_keyless_invite_poller(client) -> None:
    """Start the connection-owned keyless invitation poller exactly once.

    Called when a ``pending_delivered`` marker establishes that ``frames()``
    is actively dispatching and can resolve correlated replies. Repeated
    markers (or a poller that already finished) never spawn a duplicate; the
    per-connection ``finally`` cancels and awaits it before reconnect.
    """
    if getattr(client, "_keyless_invitation_flow", None) is None:
        return
    task = getattr(client, "_keyless_invite_poll_task", None)
    if task is not None and not task.done():
        return
    client._keyless_invite_poll_task = asyncio.create_task(
        keyless_invite_poll_loop(client)
    )


async def _stop_keyless_invite_poller(client) -> None:
    """Cancel and await the current connection's invitation poller, then
    clear its ownership slot so the next connect creates one fresh task."""
    task = getattr(client, "_keyless_invite_poll_task", None)
    if task is None:
        return
    client._keyless_invite_poll_task = None
    if task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - cleanup only
        pass


async def keyless_invite_poll_loop(client) -> None:
    """The connection-owned keyless invitation poller (fast-then-steady).

    Reuses the native cadence helper: a 2-second initial delay, then 10
    seconds during the first five minutes and 30 seconds after. Every poll
    reads the Server's authoritative pending invites, reconciles them into
    the durable flow, and resumes any persisted prompt/decision work.
    """
    flow = getattr(client, "_keyless_invitation_flow", None)
    if flow is None:
        return
    await invite_poll_loop(
        poll_invites=lambda: poll_keyless_invites(client, flow),
        next_interval=client._next_invite_poll_interval,
        sleep=asyncio.sleep,
    )


async def poll_keyless_invites(client, flow) -> None:
    """One authoritative keyless invitation poll round.

    Fail-soft: a poll failure is logged and retried on the next cadence; it
    must never escape the poller and kill bridge delivery.
    """
    try:
        invites = await client._bridge.send_list_invites()
        await flow.reconcile(invites)
        await flow.resume()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a poll failure must not stop delivery
        client._log.warning(
            "keyless_invitation: poll failed (%s); retrying on next cadence",
            exc,
        )


async def dispatch_bridge_frame(
    client,
    frame: dict,
) -> Any:
    """Route one inbound bridge frame off ``frames()``.

    ``message`` handling is wrapped so one poison frame logs and is
    skipped rather than killing the listen loop (which would drop
    every subsequent live message until the next reconnect).
    Correlated ``error`` frames are already routed to the send
    futures inside ``frames()``; anything surfacing here is
    uncorrelated, so it only warns.
    """
    kind = frame.get("type", "")
    if kind == "message":
        await _handle_bridge_message_frame(client, frame)
    elif kind == "pending_delivered":
        client._log.info(
            "bridge backfill complete: pending_delivered (count=%s)",
            frame.get("count"),
        )
        # Reconnect replay: resend missing approval prompts, resume local
        # receipt transitions, and ACK completed envelopes. It is
        # response-waiting, so it runs as a tracked task now that the frame
        # pump is live — never from ``connect()`` or a connected callback.
        _track_bridge_task(
            client,
            asyncio.create_task(resume_pending_approvals(client)),
        )
        # Seed the channel→space map for every channel we're already a
        # member of, so a proactive post to a pre-existing channel works
        # from boot (not only after the first inbound message there).
        # Scheduled async — awaiting send_list_spaces inline would deadlock
        # frames(), which must keep receiving to deliver the 'spaces' reply.
        task = asyncio.create_task(client._refresh_bridge_spaces())
        client._ack_tasks.add(task)
        task.add_done_callback(client._ack_tasks.discard)
        # Keep draining pages on this connection. Awaiting a correlated
        # request here would deadlock frames(), so it is deliberately a
        # tracked background task.
        more, count = frame.get("more", False), frame.get("count", 0)
        if more is True and isinstance(count, int) and not isinstance(count, bool):
            if count > 0:
                client._bridge_pending_nonprogress = False
                task = asyncio.create_task(client._bridge.send_fetch_pending())
                client._ack_tasks.add(task)
                task.add_done_callback(client._ack_tasks.discard)
            elif not client._bridge_pending_nonprogress:
                client._bridge_pending_nonprogress = True
                client._log.warning(
                    "bridge pending delivery made no progress; stopping drain"
                )
    elif kind == "runtime_command":
        return await _bridge_runtime_command(client, frame)
    elif kind == "added_to_space":
        # Server push (AgentServerMsg::AddedToSpace) the moment this
        # agent is added to a Space — refresh the known-spaces view
        # eagerly instead of waiting for the next lazy list_spaces.
        # Scheduled async, same shape as the F1 ack: awaiting
        # send_list_spaces inline would deadlock the frames() loop,
        # which must keep receiving to deliver the 'spaces' reply
        # that resolves the request future.
        space_id = frame.get("space_id") or ""
        client._log.info(
            "bridge: added to space %s — refreshing spaces",
            space_id or "<missing space_id>",
        )
        task = asyncio.create_task(client._refresh_bridge_spaces(space_id))
        client._ack_tasks.add(task)
        task.add_done_callback(client._ack_tasks.discard)
    elif kind == "error":
        code = frame.get("code")
        category = (
            code
            if isinstance(code, str)
            and code
            in {
                "NO_SUBKEY",
                "NOT_AUTHORIZED",
                "DECRYPT_FAILED",
                "BAD_FRAME",
                "INTERNAL",
            }
            else "UNKNOWN_BRIDGE_ERROR"
        )
        client._log.warning(
            "agent %s: bridge error category=%s",
            client.slug,
            category,
        )
    else:
        client._log.debug("bridge frame ignored: type=%s", kind)


async def _bridge_runtime_command(client, frame: dict) -> Any:
    """Execute one cloud-wire ``runtime_command`` frame.

    The claimed ``operator_slug`` is checked against this client's
    configured operator; an agent with no operator configured has nobody
    who could grant runtime permissions over this wire, so it matches no
    claim. See ``harness.runtime_commands`` for why the comparison is the
    client's to make.
    """
    from .harness.runtime_commands import execute_runtime_command

    command = frame.get("command")
    if not isinstance(command, dict):
        return {
            "ok": False,
            "error": "invalid_command",
            "error_code": "invalid_command",
        }
    return await execute_runtime_command(
        command,
        expected_agent_id=client.slug,
        manager_agent_id=client.agent_id,
        require_version=True,
        permission_turn_from_active=True,
        cloud_wire=True,
        expected_operator_slug=getattr(client, "operator_slug", "") or "",
    )


async def _handle_bridge_message_frame(client, frame: dict) -> None:
    try:
        sequence = _bridge_frame_sequence(client, frame)
        if sequence is _INVALID_SEQUENCE:
            return
        payload = client._payload_from_bridge_frame(frame)
        if payload is None:
            return
        client._preseed_frame_display_name(frame, payload)
        acknowledge = await client._store_bridge_payload(payload, server_seq=sequence)
        if acknowledge:
            _track_bridge_task(
                client,
                asyncio.create_task(client._ack_bridge_envelope([payload.envelope_id])),
            )
    except Exception:  # noqa: BLE001 - poison frames must not stop delivery
        client._log.exception(
            "bridge message frame handling failed (envelope_id=%s)",
            frame.get("envelope_id"),
        )


_INVALID_SEQUENCE = object()


def _bridge_frame_sequence(client, frame: dict) -> int | None | object:
    if "seq" not in frame:
        return None
    sequence = frame.get("seq")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        client._log.warning(
            "bridge message rejected: invalid sequence (envelope_id=%s)",
            frame.get("envelope_id"),
        )
        return _INVALID_SEQUENCE
    return sequence


def _track_bridge_task(client, task: asyncio.Task) -> None:
    client._ack_tasks.add(task)
    task.add_done_callback(client._ack_tasks.discard)


async def ack_bridge_envelope(client, envelope_ids: list[str]) -> None:
    """Fail-soft ack of handled bridge envelopes (F1).

    A failed ack must never crash the listen loop — the server
    redelivers unacked envelopes, so a lost ack is at worst a
    redelivery, not a dropped message. Native transport never calls
    this (no ``_bridge``).
    """
    bridge = client._bridge
    if bridge is None or not envelope_ids:
        return
    try:
        await bridge.send_ack(envelope_ids)
    except Exception:  # noqa: BLE001 — a failed ack must not crash the loop
        client._log.warning(
            "bridge ack failed (envelope_ids=%s)",
            envelope_ids,
            exc_info=True,
        )


async def refresh_bridge_spaces(client, trigger_space_id: str = "") -> None:
    """Re-issue ``list_spaces`` over the bridge WS so the agent's known
    spaces + channels enter its caches eagerly rather than lazily on the
    next tool call. Called after an ``added_to_space`` push and once on
    startup (after ``pending_delivered``).

    As well as the space name/disk caches, this seeds the channel→space
    map for **every channel the agent can see** — so it can post to a
    channel it belongs to WITHOUT first receiving a message there. Without
    this, the daemon's ``lookup_channel_space`` 404s for a member channel
    until an inbound message arrives, and the post is rejected as "no
    record of channel".

    Idempotent (``setdefault`` / upserting ``mark_channel_space``).
    Fail-soft: a failed refresh logs and drops — the MCP ``list_spaces``
    tool reads the authoritative route live, so a lost refresh is at worst
    a stale cache, never a missed membership. Native transport never calls
    this (no ``_bridge``).
    """
    bridge = client._bridge
    if bridge is None:
        return
    try:
        resp = await bridge.send_list_spaces()
        entries = resp.get("spaces") or []
        seeded_channels = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # The wire spec names the id field ``id``; the signed
            # HTTP route uses ``space_id`` — read both defensively.
            sid = entry.get("space_id") or entry.get("id") or ""
            name = (entry.get("name") or "").strip()
            if sid and name:
                client._space_name_cache.setdefault(sid, name)
                disk_cache.persist_space(sid, name)
            # Seed channel→space for every channel the agent can see
            # (public + private it's a member of), so a proactive post to
            # a member channel resolves without an inbound message first.
            if sid:
                for ch in entry.get("channels") or []:
                    if not isinstance(ch, dict):
                        continue
                    cid = ch.get("channel_id") or ch.get("id") or ""
                    if not cid:
                        continue
                    client._channel_space[cid] = sid
                    await client.store.mark_channel_space(cid, sid)
                    seeded_channels += 1
        client._log.info(
            "bridge spaces refresh: %d spaces listed, %d channels seeded "
            "(trigger space_id=%s)",
            len(entries),
            seeded_channels,
            trigger_space_id or "<missing>",
        )
    except Exception:  # noqa: BLE001 — a failed refresh must not crash the loop
        client._log.warning(
            "bridge spaces refresh failed (trigger space_id=%s)",
            trigger_space_id,
            exc_info=True,
        )


def preseed_frame_display_name(
    client,
    frame: dict,
    payload: MessagePayload,
) -> None:
    """If a bridge ``message`` frame carries the sender's display
    name, prime the profile cache so the render path uses it with no
    HTTP lookup.

    This is the one enrichment the keyless wire permits today: the
    Message frame has ``sender_slug`` but the signed profile route
    (`/identities/profiles`) can't authenticate over the sandbox
    token, so a name that isn't on the frame degrades to ``@slug``.
    Reads defensively — ``sender_display_name`` first, then
    ``display_name`` — and only seeds a non-empty name for a known
    slug, so an absent/blank field leaves the cache untouched (never
    pinning a false miss). ``avatar_url`` rides along when present.
    Fail-soft: any error is swallowed so a malformed frame can't
    break inbound delivery.
    """
    try:
        slug = payload.sender_slug
        if not slug:
            return
        name = frame.get("sender_display_name") or frame.get("display_name") or ""
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            return
        avatar_url = frame.get("avatar_url") or ""
        avatar_url = avatar_url.strip() if isinstance(avatar_url, str) else ""
        client.set_profile(slug, name, avatar_url)
        owner_slug = frame.get("sender_owner_slug") or frame.get("owner_slug") or ""
        owner_slug = owner_slug.strip() if isinstance(owner_slug, str) else ""
        if owner_slug:
            # Same guard as the display name above, for the same reason: the
            # Message frame carries no owner field today, so seeding the blank
            # would cache "this sender has no owner" as an attested fact and
            # ``fetch_owner_slug`` would serve that false miss for the whole
            # TTL — misclassifying a co-owned sibling agent as a stranger.
            client._owner_slug_cache[slug] = (owner_slug, time.monotonic())
    except Exception:  # noqa: BLE001 - optional frame metadata is fail-soft
        client._log.debug(
            "bridge display-name pre-seed skipped (envelope_id=%s)",
            frame.get("envelope_id"),
            exc_info=True,
        )


async def store_bridge_payload(
    client,
    payload: MessagePayload,
    *,
    server_seq: int | None = None,
) -> bool:
    """Persist one keyless bridge message into the global durable inbox.

    Current Server bridge frames carry an authoritative positive ``seq`` and
    use ``store_receipt``. Older bridge senders and daemon-local callers may
    omit it; those messages use the explicit local-ordinal lane instead of
    inventing a freshness boundary.

    Every shared policy gate runs before persistence, in the native
    order — see the module's ingress policy contract.
    """
    if server_seq is None:
        stored = await client.store.get_message_by_envelope(payload.envelope_id)
        if stored is not None:
            _reschedule_keyless_gated_record(client, payload, stored)
            if _reschedule_sequence_less_control_reply(client, payload, stored):
                return False
            return _redelivery_ack(stored)
    row = await _bridge_storage_row(client, payload)
    try:
        verdict = await _bridge_gate_verdict(client, payload, row)
    except BlocklistUnavailable as exc:
        # No blocklist answer means no safe admit decision. Leave the
        # envelope unacked and unstored; the server redelivers it once the
        # blocklist can be read.
        client._log.warning(
            "bridge delivery held: %s (envelope_id=%s sender=%s)",
            exc,
            payload.envelope_id,
            payload.sender_slug,
        )
        return False
    if verdict is not None:
        if verdict.disposition is ReceiptDisposition.FOREIGN_DM_GATED:
            # A held receipt must already contain the same model-facing
            # context an admitted DM would expose. Approval may happen after
            # a restart, when the original bridge frame is no longer present.
            await _populate_bridge_prompt_content(client, payload, row)
        return await _commit_bridge_verdict(client, payload, row, server_seq, verdict)
    await _populate_bridge_prompt_content(client, payload, row)
    return await _persist_eligible_bridge_payload(client, payload, row, server_seq)


def _redelivery_ack(stored) -> bool:
    """Whether a redelivered envelope we already hold may be acked.

    The server redelivers anything unacked, so this answer has to come from
    what the first delivery decided. Acking unconditionally is how a held
    foreign DM used to be dropped server-side while still unreleasable
    locally — the message would then exist nowhere it could be read.
    """
    if stored.receipt_disposition is ReceiptDisposition.FOREIGN_DM_GATED:
        return False
    return True


def _reschedule_sequence_less_control_reply(client, payload, stored) -> bool:
    """Resume a deferred keyless control reply from its terminal receipt.

    Sequence-less compatibility deliveries bypass receipt classification on
    redelivery. Re-run only the matching durable control handler while its
    decision is still pending; otherwise the terminal receipt may be ACKed.
    """
    row = {"thread_root_id": stored.thread_root_id or payload.thread_root_id or ""}
    if stored.receipt_reason == KEYLESS_DM_REPLY_REASON:
        pending = getattr(client, "_pending_dm_approvals", None) or {}
        thread_root_id = row["thread_root_id"]
        text = str(payload.content) if payload.content else ""
        if not is_keyless_operator_approval_reply(
            pending,
            thread_root_id=thread_root_id,
            text=text,
        ):
            return False
        _track_bridge_task(
            client,
            asyncio.create_task(
                _finish_keyless_dm_operator_reply(
                    client,
                    thread_root_id=thread_root_id,
                    text=text,
                    envelope_id=payload.envelope_id,
                )
            ),
        )
        return True
    if stored.receipt_reason == KEYLESS_INVITATION_REPLY_REASON:
        flow = getattr(client, "_keyless_invitation_flow", None)
        thread_root_id = row["thread_root_id"]
        text = str(payload.content) if payload.content else ""
        if flow is None or not flow.is_operator_reply(
            thread_root_id=thread_root_id,
            text=text,
        ):
            return False
        _track_bridge_task(
            client,
            asyncio.create_task(
                _finish_keyless_invitation_reply(
                    client,
                    flow,
                    thread_root_id=thread_root_id,
                    text=text,
                    envelope_id=payload.envelope_id,
                )
            ),
        )
        return True
    return False


def _reschedule_keyless_gated_record(client, payload, stored) -> None:
    """Re-schedule durable recording for a redelivered gated envelope.

    The compatibility lane answers a redelivery before any gate runs, so the
    durable record must be re-scheduled here when it is missing — the first
    attempt may have crashed or failed to persist. Without it the held
    receipt would look ACKable to ``_redelivery_ack`` while carrying no
    resumable approval state. Idempotent when the record already exists.
    """
    if stored.receipt_disposition is not ReceiptDisposition.FOREIGN_DM_GATED:
        return
    _track_bridge_task(
        client,
        asyncio.create_task(
            record_gated_dm(
                client,
                envelope_id=payload.envelope_id,
                sender_slug=payload.sender_slug,
                server_seq=None,
            )
        ),
    )


async def _bridge_storage_row(client, payload: MessagePayload) -> dict[str, Any]:
    dm_peer = (
        payload.recipient_slug
        if payload.sender_slug == client.slug
        else payload.sender_slug
    ) if payload.envelope_kind == "dm" else ""
    thread_root_id, thread_root_unverified = (
        await client._resolve_incoming_thread_root(
            payload.thread_root_id,
            payload.channel_id,
            payload.space_id,
            expected_envelope_kind=payload.envelope_kind,
            expected_dm_peer=dm_peer,
        )
    )
    return {
        "envelope_id": payload.envelope_id,
        "envelope_kind": payload.envelope_kind,
        "sender_slug": payload.sender_slug,
        "channel_id": payload.channel_id,
        "space_id": payload.space_id,
        "recipient_slug": payload.recipient_slug,
        "content_type": payload.content_type,
        "content": payload.content,
        "sent_at": payload.sent_at,
        "thread_root_id": thread_root_id,
        "thread_root_unverified": thread_root_unverified,
        "reply_to_id": await client._validate_incoming_parent_id(
            payload.reply_to_id,
            payload.channel_id,
            payload.space_id,
            expected_envelope_kind=payload.envelope_kind,
            expected_dm_peer=dm_peer,
        ),
        "is_encrypted": False,
    }


async def _bridge_gate_verdict(client, payload, row) -> GateVerdict | None:
    """Run the shared ingress gates in the native order.

    Blocked → self echo → operator control → foreign DM → stale. All but
    the foreign-DM arm keep a message off the model entirely; that one
    holds it for operator approval, and it runs ahead of the stale arm so
    an unapproved stranger's DM arriving during catch-up is held (unacked,
    still tombstonable on denial) instead of terminalized as acked
    plaintext. Only the two transport-local checks (self echo, catch-up
    staleness) are decided here — everything with a policy meaning comes
    from ``ingress_policy``.
    """
    verdict = await blocked_gate(client, payload)
    if verdict is not None:
        return verdict
    if payload.sender_slug == client.slug:
        content = None
        if is_keyless_prompt_envelope(
            getattr(client, "_pending_dm_approvals", None) or {},
            envelope_id=payload.envelope_id,
        ):
            # The echo of the agent's own keyless approval prompt quotes the
            # withheld stranger's body; storing it verbatim would leak the
            # plaintext the gate is holding. Same redaction the native lane
            # applies, keyed on the durable prompt envelope id here.
            content = DM_GATE_PROMPT_PLACEHOLDER
        elif _is_keyless_invite_prompt_echo(client, payload.envelope_id):
            # The echo of the agent's own invitation prompt is operator
            # control, not model context; store a short placeholder instead
            # of the operator-visible invitation wording.
            content = INVITE_PROMPT_PLACEHOLDER
        return GateVerdict(
            gate="self_echo",
            disposition=ReceiptDisposition.TERMINAL,
            reason="keyless bridge client echo",
            content=content,
        )
    verdict = await operator_control_gate(
        client,
        payload,
        thread_root_id=row["thread_root_id"] or "",
        text=str(payload.content) if payload.content else "",
    )
    if verdict is not None:
        return verdict
    verdict = await _keyless_operator_reply_gate(client, payload, row)
    if verdict is not None:
        return verdict
    verdict = await _keyless_invitation_reply_gate(client, payload, row)
    if verdict is not None:
        return verdict
    verdict = await foreign_dm_gate(client, payload, _bridge_raw_text(payload))
    if verdict is not None:
        return verdict
    if client._is_stale_for_catchup(payload.sent_at):
        return GateVerdict(
            gate="stale",
            disposition=ReceiptDisposition.TERMINAL,
            reason="keyless bridge stale history",
        )
    return None


async def _keyless_operator_reply_gate(client, payload, row) -> GateVerdict | None:
    """Consume a keyless operator's exact threaded approval reply.

    Recognition is synchronous against validated durable records only, so it
    runs on the frame-pump stack without awaiting anything response-waiting;
    ``maybe_handle_operator_reply`` — which calls bridge ``send_ack`` and can
    block — is scheduled as a tracked task the pump keeps feeding. Any other
    operator control (invite / leave / permission / signed DM approval) has
    already been decided by ``operator_control_gate`` before this runs.
    """
    if not (
        payload.envelope_kind == "dm"
        and payload.sender_slug == client.operator_slug
    ):
        return None
    # A fast operator reply can arrive before the prompt self-echo has been
    # persisted locally. The operator identity plus a durable prompt-id match
    # is sufficient to authenticate this control reference, so retain the raw
    # wire root as a fallback instead of silently losing the reply.
    thread_root_id = row["thread_root_id"] or payload.thread_root_id or ""
    text = str(payload.content) if payload.content else ""
    pending = getattr(client, "_pending_dm_approvals", None) or {}
    if not is_keyless_operator_approval_reply(
        pending,
        thread_root_id=thread_root_id,
        text=text,
    ):
        return None
    _track_bridge_task(
        client,
        asyncio.create_task(
            _finish_keyless_dm_operator_reply(
                client,
                thread_root_id=thread_root_id,
                text=text,
                envelope_id=payload.envelope_id,
            )
        ),
    )
    return GateVerdict(
        gate="operator_control",
        disposition=ReceiptDisposition.TERMINAL,
        reason=KEYLESS_DM_REPLY_REASON,
        defer_ack=True,
    )


async def _finish_keyless_dm_operator_reply(
    client, *, thread_root_id: str, text: str, envelope_id: str,
) -> None:
    """ACK an operator reply only after its approval decision is durable."""
    durable = await maybe_handle_operator_reply(
        client,
        thread_root_id=thread_root_id,
        text=text,
    )
    if durable:
        await ack_bridge_envelope(client, [envelope_id])


def _is_keyless_invite_prompt_echo(client, envelope_id: str) -> bool:
    """Whether ``envelope_id`` is the agent's own durable invitation prompt.

    The server echoes the prompt back to its sender; that self-echo is
    operator control and must be stored as a short placeholder, not
    delivered to model context.
    """
    flow = getattr(client, "_keyless_invitation_flow", None)
    if flow is None:
        return False
    return flow.is_prompt_envelope(envelope_id)


async def _keyless_invitation_reply_gate(client, payload, row) -> GateVerdict | None:
    """Consume an exact threaded keyless invitation decision reply.

    Recognition is synchronous against the durable flow's in-memory mirror,
    so it runs on the frame-pump stack without awaiting anything
    response-waiting; the locked ``handle_operator_reply`` — which can call
    ``send_decide_invitation`` and block on correlation — is scheduled as a
    tracked task the pump keeps feeding. Any other operator control (invite /
    leave / permission / signed DM approval) has already been decided by
    ``operator_control_gate`` before this runs.
    """
    flow = getattr(client, "_keyless_invitation_flow", None)
    if flow is None:
        return None
    if not (
        payload.envelope_kind == "dm"
        and payload.sender_slug == client.operator_slug
    ):
        return None
    # A fast operator reply can arrive before the prompt self-echo has been
    # persisted locally. The operator identity plus a durable prompt-id match
    # is sufficient to authenticate this control reference, so retain the raw
    # wire root as a fallback instead of silently losing the reply.
    thread_root_id = row["thread_root_id"] or payload.thread_root_id or ""
    text = str(payload.content) if payload.content else ""
    if not flow.is_operator_reply(thread_root_id=thread_root_id, text=text):
        return None
    _track_bridge_task(
        client,
        asyncio.create_task(
            _finish_keyless_invitation_reply(
                client,
                flow,
                thread_root_id=thread_root_id,
                text=text,
                envelope_id=payload.envelope_id,
            )
        ),
    )
    return GateVerdict(
        gate="operator_control",
        disposition=ReceiptDisposition.TERMINAL,
        reason=KEYLESS_INVITATION_REPLY_REASON,
        defer_ack=True,
    )


async def _finish_keyless_invitation_reply(
    client, flow, *, thread_root_id: str, text: str, envelope_id: str,
) -> None:
    """ACK an operator reply only after its invitation decision is durable."""
    durable = await flow.handle_operator_reply(
        thread_root_id=thread_root_id,
        text=text,
    )
    if durable:
        await ack_bridge_envelope(client, [envelope_id])


async def _commit_bridge_verdict(
    client, payload, row, server_seq: int | None, verdict: GateVerdict
) -> bool:
    """Persist a gate verdict through the bridge's storage lane.

    A verdict carrying replacement ``content`` (the blocked tombstone)
    overwrites the body first, so the original plaintext never reaches
    the database. Returns whether the envelope may be acked — a gated
    foreign DM must not be, since it stays queued server-side until the
    operator answers.
    """
    if verdict.gate == "self_echo":
        row = dict(row)
        row["content"] = content_with_visibility(
            payload.content if verdict.content is None else verdict.content,
            is_visible_to_human=payload.is_visible_to_human,
        )
        if verdict.content is not None:
            row["content_type"] = "text/plain"
    elif verdict.content is not None:
        row = dict(row)
        row["content"] = verdict.content
        row["content_type"] = "text/plain"
    if server_seq is None:
        # Compatibility/local-event frames may omit a delivery sequence. This
        # lane must still persist the verdict: a plain ``store()`` drops it,
        # leaving a gated DM unpromotable and a terminal row invisible to prior
        # context.
        result = await client.store.store_local_receipt(
            row,
            disposition=verdict.disposition,
            reason=verdict.reason,
        )
    else:
        result = await client.store.store_receipt(
            row,
            server_seq=server_seq,
            disposition=verdict.disposition,
            reason=verdict.reason,
        )
    if (
        verdict.disposition is ReceiptDisposition.FOREIGN_DM_GATED
        and result.status
        in {ReceiptWriteStatus.COMMITTED, ReceiptWriteStatus.IDEMPOTENT}
    ):
        _track_bridge_task(
            client,
            asyncio.create_task(
                record_gated_dm(
                    client,
                    envelope_id=payload.envelope_id,
                    sender_slug=payload.sender_slug,
                    server_seq=server_seq,
                )
            ),
        )
    runtime = getattr(client, "global_runtime", None)
    if (
        runtime is not None
        and payload.envelope_kind == "channel"
        and result.status
        in {
            ReceiptWriteStatus.COMMITTED,
            ReceiptWriteStatus.IDEMPOTENT,
        }
    ):
        runtime.notify_delivery()
    return result.acknowledge and not verdict.defer_ack


def _bridge_raw_text(payload) -> str:
    """The message body as plain text, with no side effects.

    Split out of ``_bridge_text_and_attachments`` so the foreign-DM gate
    can quote a preview without triggering the attachment downloads that
    only an admitted message needs.
    """
    content = payload.content
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text, caption = content.get("text"), content.get("caption")
        if isinstance(text, str):
            return text
        return caption if isinstance(caption, str) else ""
    return ""


async def _bridge_text_and_attachments(client, payload) -> tuple[str, list[str]]:
    attachment_paths: list[str] = []
    raw_text = _bridge_raw_text(payload)
    content = payload.content
    if isinstance(content, dict):
        metas = content.get("attachments") or []
        if payload.content_type == "puffo/message+attachments/v1" and isinstance(
            metas, list
        ):
            attachment_paths = await client._save_inbound_attachments(
                envelope_id=payload.envelope_id,
                metas_raw=metas,
            )
    if isinstance(payload.attachments, list) and payload.attachments:
        attachment_paths.extend(
            await client._save_inbound_bridge_attachments(
                envelope_id=payload.envelope_id,
                refs=payload.attachments,
            )
        )
    return raw_text, attachment_paths


async def _bridge_mentions(
    client, raw_text: str, space_id: str
) -> tuple[bool, list[dict[str, Any]]]:
    seen: set[str] = set()
    slugs = [
        slug
        for match in MENTION_RE.finditer(raw_text)
        if (slug := match.group(1).lower()) not in seen and not seen.add(slug)
    ]
    self_slug = client.slug.lower()
    members = await client._get_space_members(space_id) if space_id else {}
    mentions = [
        {"username": client.slug, "is_agent": True, "is_self": True}
        if slug == self_slug
        else {
            "username": slug,
            "is_agent": members.get(slug) == "agent",
            "is_self": False,
        }
        for slug in slugs
        if slug == self_slug or not members or slug in members
    ]
    return self_slug in seen, mentions


async def _populate_bridge_prompt_content(client, payload, row: dict[str, Any]) -> None:
    channel_id, space_id = payload.channel_id or "", payload.space_id or ""
    is_dm = payload.envelope_kind == "dm"
    raw_text, paths = await _bridge_text_and_attachments(client, payload)
    if is_dm:
        client._legacy_dm_peer = client._last_dm_sender = payload.sender_slug
    elif channel_id and space_id:
        client._channel_space[channel_id] = space_id
    is_mention, mentions = await _bridge_mentions(client, raw_text, space_id)
    clean_text = (
        raw_text.replace(f"@{client.slug}", f"@you({client.slug})").strip()
        if is_mention
        else raw_text
    )
    row["content"] = await _bridge_prompt_content(
        client,
        payload,
        clean_text,
        paths,
        mentions,
        space_id,
        channel_id,
        is_dm,
    )


async def _bridge_prompt_content(
    client, payload, text, paths, mentions, space_id, channel_id, is_dm
):
    space_name = await client._resolve_space_name(space_id) if space_id else ""
    channel_name = (
        "Direct message"
        if is_dm
        else (
            await client._resolve_channel_name(space_id, channel_id)
            if channel_id
            else ""
        )
    )
    display = await client._fetch_display_name(payload.sender_slug)
    owner = payload.sender_owner_slug
    if owner is None:
        owner = await client._fetch_owner_slug(payload.sender_slug)
    return {
        "text": maybe_redact_long_text(
            text,
            envelope_id=payload.envelope_id,
            sender_slug=payload.sender_slug,
            sender_display_name=display,
            max_inline_chars=client._max_inline_chars,
            segment_chars=client._segment_chars,
            agent_slug=client.slug,
        ),
        "original_content": payload.content,
        "content_type": payload.content_type,
        "attachment_paths": paths,
        "mentions": mentions,
        "sender_display_name": display,
        "sender_owner_slug": owner,
        "is_from_operator": bool(
            client.operator_slug and payload.sender_slug == client.operator_slug
        ),
        "sender_is_agent": payload.sender_type == "agent" or bool(owner),
        "sender_type": payload.sender_type,
        "is_visible_to_human": payload.is_visible_to_human,
        "channel_name": channel_name,
        "space_name": space_name,
    }


async def _persist_eligible_bridge_payload(
    client, payload, row, server_seq: int | None
) -> bool:
    committed = True
    if server_seq is None:
        await client.store.store_local_event(
            row, reason="keyless bridge delivery without server sequence"
        )
    else:
        receipt = await client.store.store_receipt(
            row,
            server_seq=server_seq,
            disposition=ReceiptDisposition.ELIGIBLE,
            reason="keyless bridge sequenced delivery",
        )
        if receipt.status is ReceiptWriteStatus.CONFLICT:
            return False
        committed = receipt.status is ReceiptWriteStatus.COMMITTED
    runtime = getattr(client, "global_runtime", None)
    if runtime is not None:
        if server_seq is not None and payload.envelope_kind == "channel":
            runtime.notify_delivery()
        if committed:
            runtime.notify()
    return True


def payload_from_bridge_frame(client, frame: dict) -> MessagePayload | None:
    """Map a plaintext bridge ``message`` frame to ``MessagePayload``.

    The server holds all crypto on the bridge transport, so the
    frame body is already plaintext. ``_store_bridge_payload`` then
    projects it into the same durable inbox schema as native delivery.
    Every routing field is read with ``.get()`` and a benign
    fallback so a wire-field rename or a sparse frame degrades to a
    skip (bad ``envelope_id``) rather than a ``KeyError`` mid-loop.
    The exact wire field names live in
    ``puffo-server/roadmap/cloud-agent/BRIDGE-WIRE-PROTOCOL.md``;
    only ``envelope_id`` / ``sender_slug`` / ``plaintext`` are
    anchored by tests, so a rename is a one-line change confined
    here.
    """
    envelope_id = frame.get("envelope_id")
    # This is the only ingress lane with no server-side id-format check, and
    # the id becomes both the store's primary key and a local directory
    # name, so a traversal-shaped id is rejected here rather than deeper in.
    if not envelope_id or not is_safe_path_component(envelope_id):
        client._log.warning(
            "bridge message frame missing or unsafe envelope_id (%r) — "
            "skipping (keys=%s)",
            envelope_id,
            sorted(frame.keys()),
        )
        return None
    recipient_slug = frame.get("recipient_slug")
    envelope_kind = frame.get("envelope_kind") or (
        "dm" if recipient_slug else "channel"
    )
    # The additive content member wins by presence, including falsey JSON.
    content = frame["content"] if "content" in frame else frame.get("plaintext", "")
    content_type = frame.get("content_type", "text/plain")
    visible = frame.get("is_visible_to_human", True)
    owner = frame.get("sender_owner_slug")
    sender_type = frame.get("sender_type")
    if (
        not isinstance(content_type, str)
        or not isinstance(visible, bool)
        or (owner is not None and not isinstance(owner, str))
        or (sender_type is not None and sender_type not in {"human", "agent"})
    ):
        client._log.warning(
            "bridge message rejected: invalid additive field types (envelope_id=%s)",
            envelope_id,
        )
        return None
    return MessagePayload(
        payload_type=frame.get("payload_type", "message"),
        version=frame.get("version", 1),
        envelope_id=envelope_id,
        envelope_kind=envelope_kind,
        sender_slug=frame.get("sender_slug", ""),
        sender_subkey_id=frame.get("sender_subkey_id", ""),
        sent_at=frame.get("sent_at") or int(time.time() * 1000),
        message_nonce=frame.get("message_nonce", ""),
        content_type=content_type,
        content=content,
        is_visible_to_human=visible,
        space_id=frame.get("space_id"),
        channel_id=frame.get("channel_id"),
        recipient_slug=recipient_slug,
        thread_root_id=frame.get("thread_root_id"),
        reply_to_id=frame.get("reply_to_id"),
        # Keyless-bridge attachments ride the frame top level as
        # ``[{ blob_id, filename, mime_type, size_bytes }]``; storage
        # downloads each blob by id without a decrypt step.
        # Native frames never carry this key, so it stays None there.
        attachments=frame.get("attachments"),
        sender_owner_slug=owner,
        sender_type=sender_type,
    )


async def save_inbound_bridge_attachments(
    client,
    *,
    envelope_id: str,
    refs: list,
) -> list[str]:
    """Keyless-bridge sibling of ``_save_inbound_attachments``:
    download each blob by ``blob_id`` (no decrypt — the server holds
    the blob store) and save it to ``<workspace>/.puffo/inbox/
    <envelope_id>/<filename>``, returning absolute paths. Per-ref
    failures (bad shape, missing/oversized/unfetchable blob, save
    error) are logged and skipped so one bad ref never crashes the
    turn or the listen loop.

    Mirrors the native saver's filename sanitisation + oversized-
    image downscale so a bridge-delivered image is treated
    identically to a native one.
    """
    if not client.workspace or not refs or client._bridge is None:
        return []
    if not is_safe_path_component(envelope_id):
        client._log.warning(
            "bridge attachments skipped: envelope_id is not a safe path "
            "component (%r)",
            envelope_id,
        )
        return []
    inbox = Path(client.workspace) / ".puffo" / "inbox" / envelope_id
    inbox.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    total_bytes = 0
    if len(refs) > MAX_INBOUND_ATTACHMENTS:
        client._log.warning(
            "bridge attachment count exceeds cap; processing first %d of %d",
            MAX_INBOUND_ATTACHMENTS,
            len(refs),
        )
    for raw in refs[:MAX_INBOUND_ATTACHMENTS]:
        saved = await _save_one_bridge_attachment(
            client,
            raw=raw,
            inbox=inbox,
            remaining_bytes=MAX_INBOUND_ATTACHMENT_TOTAL_BYTES - total_bytes,
        )
        if saved is not None:
            path, size = saved
            paths.append(str(path))
            total_bytes += size
    return paths


async def _save_one_bridge_attachment(
    client,
    *,
    raw: Any,
    inbox: Path,
    remaining_bytes: int,
) -> tuple[Path, int] | None:
    if not isinstance(raw, dict):
        return None
    blob_id = raw.get("blob_id")
    if not blob_id:
        client._log.warning("bridge attachment ref missing blob_id: %r", raw)
        return None
    declared = raw.get("size_bytes")
    if declared is not None and (
        isinstance(declared, bool)
        or not isinstance(declared, int)
        or declared < 0
        or declared > MAX_INBOUND_ATTACHMENT_BYTES
        or declared > remaining_bytes
    ):
        client._log.warning(
            "bridge attachment declared size exceeds inbound limit (%s: %r bytes)",
            blob_id,
            declared,
        )
        return None
    try:
        data = await client._bridge.download_blob(blob_id)
    except Exception as exc:  # noqa: BLE001 — one bad ref never fatal
        client._log.warning("bridge attachment download raised (%s): %s", blob_id, exc)
        return None
    if data is None:
        return None
    if len(data) > MAX_INBOUND_ATTACHMENT_BYTES or len(data) > remaining_bytes:
        client._log.warning(
            "bridge attachment plaintext exceeds inbound limit (%s: %d bytes)",
            blob_id,
            len(data),
        )
        return None
    safe_name = (
        Path(str(raw.get("filename") or "")).name
        or Path(str(blob_id)).name
        or "attachment"
    )
    target = inbox / safe_name
    try:
        target.write_bytes(data)
    except OSError as exc:
        client._log.warning("bridge attachment save failed (%s): %s", target, exc)
        return None
    normalized = await normalize_saved_image(
        target=target,
        image_edge_px=client._image_edge_px,
        log=client._log,
        scale_image=downscale_oversized_image,
    )
    return (normalized, len(data)) if normalized is not None else None
