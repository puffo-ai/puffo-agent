"""Resumable keyless DM operator-approval component.

One durable :class:`~puffo_agent.agent.dm_approvals.KeylessDmApproval` per
held foreign-DM envelope, one operator prompt per ``(operator_slug,
sender_slug)`` group. The three async entry points are scheduled by the
bridge ingress integration in ``bridge_transport``: ``record_gated_dm`` on a
held DM, ``maybe_handle_operator_reply`` as tracked reply work, and
``resume_pending_approvals`` on each reconnect's ``pending_delivered``.

Grouping: every held envelope from one sender against one operator shares a
single stable ``prompt_client_ref``, prompt thread, and decision. The local
application phase is per-envelope and always starts ``pending`` — a later
envelope that joins a decided group is finalized through its own local
transition and bridge ACK, never by inheriting an older member's phase.

Replay: the record is persisted before the prompt is sent, and
``phase=applied`` is persisted before the bridge ACK, so
``resume_pending_approvals`` can re-send a missing prompt, resume a local
transition, or complete an ACK after every reconnect. A crash between the
remote prompt send and the local persistence of the returned ids is
explicitly at-least-once; no server-side dedupe is assumed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from typing import Any

from .dm_approvals import (
    KEYLESS_APPROVAL_KIND,
    KeylessDmApproval,
    parse_keyless_approval,
    save_pending_dm_approvals,
)
from .message_store import ReceiptWriteStatus
from .permission_prompt import format_permission_prompt

logger = logging.getLogger(__name__)

APPROVED_REASON = "foreign dm approved by operator"

_YES = frozenset({"y", "yes"})
_NO = frozenset({"n", "no"})

# Approvals only ever ride these two receipt results; anything else is a
# fail-closed hold that keeps the record resumable without an ACK.
_APPLIED_STATUSES = frozenset(
    {ReceiptWriteStatus.COMMITTED, ReceiptWriteStatus.IDEMPOTENT}
)


def _prompt_client_ref() -> str:
    return f"prompt_{uuid.uuid4().hex[:12]}"


def _keyless_records(
    pending: dict[str, dict[str, Any]],
) -> list[KeylessDmApproval]:
    return [
        parsed
        for value in pending.values()
        if value.get("kind") == KEYLESS_APPROVAL_KIND
        and (parsed := parse_keyless_approval(value)) is not None
    ]


def is_keyless_operator_approval_reply(
    pending: dict[str, dict[str, Any]],
    *,
    thread_root_id: str,
    text: str,
) -> bool:
    """Whether ``text`` is an exact threaded operator decision for a durable
    keyless approval prompt.

    Pure and synchronous: bridge ingress runs this on the frame-pump stack to
    recognise the reply before scheduling the response-waiting
    :func:`maybe_handle_operator_reply` as a tracked task. It mirrors the
    handler's exact ``y``/``yes``/``n``/``no`` vocabulary and thread match but
    never mutates or persists anything.
    """
    if not thread_root_id:
        return False
    if text.strip().lower() not in _YES | _NO:
        return False
    return any(
        record.prompt_envelope_id == thread_root_id
        or record.prompt_thread_id == thread_root_id
        for record in _keyless_records(pending)
    )


def is_keyless_prompt_envelope(
    pending: dict[str, dict[str, Any]],
    *,
    envelope_id: str,
) -> bool:
    """Whether ``envelope_id`` is a durable keyless approval-prompt envelope.

    The server echoes the agent's own approval prompt back; that self-echo
    must be stored with ``DM_GATE_PROMPT_PLACEHOLDER`` rather than as the
    operator-visible prompt text, which quotes the withheld stranger's body.
    """
    return any(
        record.prompt_envelope_id == envelope_id
        for record in _keyless_records(pending)
    )


def _persist(client, pending: dict[str, dict[str, Any]]) -> bool:
    """Save the whole pending dict; legacy native entries pass through
    unchanged. Returns whether the write landed."""
    try:
        save_pending_dm_approvals(client.slug, pending)
    except OSError as exc:
        client._log.warning(
            "keyless_dm_approval: pending state persist failed: %s",
            exc,
        )
        return False
    return True


def _persist_or_restore(
    client,
    pending: dict[str, dict[str, Any]],
    before: dict[str, dict[str, Any]],
) -> bool:
    """Keep the in-memory mirror aligned with the durable file on failure."""
    if _persist(client, pending):
        return True
    pending.clear()
    pending.update(before)
    return False


def _approval_lock(client) -> asyncio.Lock:
    lock = getattr(client, "_keyless_dm_approval_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        client._keyless_dm_approval_lock = lock
    return lock


def _notify_runtime(client) -> None:
    runtime = getattr(client, "global_runtime", None)
    if runtime is not None:
        runtime.notify()


async def record_gated_dm(
    client,
    *,
    envelope_id: str,
    sender_slug: str,
    server_seq: int | None,
) -> None:
    """Durably record one held foreign-DM envelope for operator approval.

    A new ``(operator_slug, sender_slug)`` group gets one stable
    ``prompt_client_ref``; the record is persisted before the prompt is
    sent, and the correlated ACK's returned prompt envelope/thread ids are
    persisted across every current group member. A later envelope joins the
    group's prompt and decision with its own ``phase=pending``. A missing
    operator fails closed: the held record is retained, logged, and nothing
    is sent.
    """
    async with _approval_lock(client):
        await _record_gated_dm_locked(
            client,
            envelope_id=envelope_id,
            sender_slug=sender_slug,
            server_seq=server_seq,
        )


async def _record_gated_dm_locked(
    client,
    *,
    envelope_id: str,
    sender_slug: str,
    server_seq: int | None,
) -> None:
    pending = client._pending_dm_approvals
    if envelope_id in pending:
        return
    operator_slug = getattr(client, "operator_slug", "") or ""
    record = KeylessDmApproval(
        envelope_id=envelope_id,
        sender_slug=sender_slug,
        operator_slug=operator_slug,
        server_seq=server_seq,
        prompt_client_ref=_prompt_client_ref(),
        prompt_envelope_id=None,
        prompt_thread_id=None,
        decision="pending",
        phase="pending",
    )
    if not operator_slug:
        before = dict(pending)
        pending[envelope_id] = record.to_dict()
        if not _persist_or_restore(client, pending, before):
            return
        client._log.warning(
            "keyless_dm_approval: no operator_slug configured; held DM %s "
            "from %s retained with no prompt",
            envelope_id,
            sender_slug,
        )
        return
    group = [
        member
        for member in _keyless_records(pending)
        if member.envelope_id != envelope_id
        and member.operator_slug == operator_slug
        and member.sender_slug == sender_slug
    ]
    if not group:
        before = dict(pending)
        pending[envelope_id] = record.to_dict()
        if not _persist_or_restore(client, pending, before):
            return
        await _send_prompt(client, pending, record, operator_slug)
        return
    base = group[0]
    joined = replace(
        record,
        prompt_client_ref=base.prompt_client_ref,
        prompt_envelope_id=base.prompt_envelope_id,
        prompt_thread_id=base.prompt_thread_id,
        decision=base.decision,
    )
    before = dict(pending)
    pending[envelope_id] = joined.to_dict()
    if not _persist_or_restore(client, pending, before):
        return
    if base.decision == "approved":
        await _finalize_record(client, pending, envelope_id, approved=True)
    elif base.decision == "denied":
        await _finalize_record(client, pending, envelope_id, approved=False)


async def _send_prompt(
    client,
    pending: dict[str, dict[str, Any]],
    record: KeylessDmApproval,
    operator_slug: str,
) -> None:
    """Send the group's root-level permission prompt to the operator and
    persist the returned prompt envelope/thread ids across the group.

    The pre-send record is already durable, so any failure keeps the group
    resumable: ``resume_pending_approvals`` re-sends with the same
    ``prompt_client_ref``. The ACK envelope id is the prompt's thread root;
    the operator's reply frame carries it as ``thread_root_id``.
    """
    prompt = format_permission_prompt(
        f"{record.sender_slug} is DM-ing me — allow (allowlist + deliver) or block?"
    )
    bridge = client._bridge
    if bridge is None:
        client._log.warning(
            "keyless_dm_approval: no bridge transport; prompt for %s not "
            "sent (record retained)",
            record.sender_slug,
        )
        return
    try:
        ack = await bridge.send_send(
            plaintext=prompt,
            recipient_slug=operator_slug,
            client_ref=record.prompt_client_ref,
        )
    except Exception as exc:  # noqa: BLE001 - resumable transport failure
        client._log.warning(
            "keyless_dm_approval: prompt send for %s failed (%s); record "
            "retained for resume",
            record.sender_slug,
            exc,
        )
        return
    prompt_envelope = ack.get("envelope_id") or ""
    if not prompt_envelope:
        client._log.warning(
            "keyless_dm_approval: prompt ack for %s carried no envelope_id; "
            "record retained for resume",
            record.sender_slug,
        )
        return
    prompt_thread = ack.get("thread_root_id") or prompt_envelope
    before = dict(pending)
    changed = False
    for member in _keyless_records(pending):
        if (
            member.operator_slug == operator_slug
            and member.sender_slug == record.sender_slug
        ):
            pending[member.envelope_id] = replace(
                member,
                prompt_envelope_id=prompt_envelope,
                prompt_thread_id=prompt_thread,
            ).to_dict()
            changed = True
    if changed:
        _persist_or_restore(client, pending, before)


async def maybe_handle_operator_reply(
    client,
    *,
    thread_root_id: str,
    text: str,
) -> bool:
    """Consume an exact threaded ``y``/``yes``/``n``/``no`` operator reply.

    Persists the decision across every record in the matched prompt group
    before any local transition, then finalizes each member. ``True`` means
    the reply is represented by a durable decision and may be acknowledged;
    unknown replies and failed decision persistence return ``False``.
    """
    async with _approval_lock(client):
        return await _maybe_handle_operator_reply_locked(
            client,
            thread_root_id=thread_root_id,
            text=text,
        )


async def _maybe_handle_operator_reply_locked(
    client,
    *,
    thread_root_id: str,
    text: str,
) -> bool:
    normalized = text.strip().lower()
    if normalized in _YES:
        approved = True
    elif normalized in _NO:
        approved = False
    else:
        return False
    pending = client._pending_dm_approvals
    matched = [
        record
        for record in _keyless_records(pending)
        if record.prompt_envelope_id == thread_root_id
        or record.prompt_thread_id == thread_root_id
    ]
    if not matched:
        return False
    decision = "approved" if approved else "denied"
    terminal = {record.decision for record in matched if record.decision != "pending"}
    if len(terminal) > 1 or (terminal and decision not in terminal):
        client._log.warning(
            "keyless_dm_approval: ignored conflicting %s reply for already "
            "decided prompt %s (%s)",
            decision,
            thread_root_id,
            ", ".join(sorted(terminal)),
        )
        return True
    if any(record.decision == "pending" for record in matched):
        before = dict(pending)
        for record in matched:
            if record.decision == "pending":
                pending[record.envelope_id] = replace(
                    record,
                    decision=decision,
                ).to_dict()
        if not _persist_or_restore(client, pending, before):
            return False
    resolved_approved = next(iter(terminal), decision) == "approved"
    for record in matched:
        await _finalize_record(
            client,
            pending,
            record.envelope_id,
            approved=resolved_approved,
        )
    return True


async def resume_pending_approvals(client) -> None:
    """Deterministic replay of persisted keyless approvals after a reconnect.

    Fail-closed groups (no configured operator) are retained with a log.
    Pending groups without a known prompt id have their missing prompt
    re-sent with the persisted ``prompt_client_ref``; pending groups with a
    known prompt id are left waiting for the operator reply; persisted
    approved/denied records resume at application or ACK by phase.
    """
    async with _approval_lock(client):
        await _resume_pending_approvals_locked(client)


async def _resume_pending_approvals_locked(client) -> None:
    pending = client._pending_dm_approvals
    for record in sorted(
        _keyless_records(pending),
        key=lambda r: r.envelope_id,
    ):
        if record.decision == "approved":
            await _finalize_record(client, pending, record.envelope_id, approved=True)
        elif record.decision == "denied":
            await _finalize_record(client, pending, record.envelope_id, approved=False)
    operator_slug = getattr(client, "operator_slug", "") or ""
    groups: dict[tuple[str, str], list[KeylessDmApproval]] = {}
    for record in _keyless_records(pending):
        if record.decision != "pending":
            continue
        if not operator_slug or record.operator_slug != operator_slug:
            client._log.warning(
                "keyless_dm_approval: resume fail-closed — no operator for "
                "held DM %s from %s; retained",
                record.envelope_id,
                record.sender_slug,
            )
            continue
        groups.setdefault((record.operator_slug, record.sender_slug), []).append(record)
    for (group_operator, _), members in groups.items():
        if any(member.prompt_envelope_id is not None for member in members):
            # Prompt known — still waiting for the operator's exact reply.
            continue
        await _send_prompt(client, pending, members[0], group_operator)


async def _finalize_record(
    client,
    pending: dict[str, dict[str, Any]],
    envelope_id: str,
    *,
    approved: bool,
) -> None:
    """Advance one decided record through local application and bridge ACK.

    Re-reads the record from ``pending`` so a caller's just-persisted
    decision is never clobbered. ``phase=applied`` is saved before the ACK;
    the record is removed only after the ACK succeeds. Any failure retains
    the record in its current resumable state (pre-apply at ``pending``, or
    ACK-only retry at ``applied``) and never sends the ACK.
    """
    record = parse_keyless_approval(pending.get(envelope_id))
    if record is None:
        return
    if record.phase == "pending":
        if not await _apply_local_transition(client, record, approved=approved):
            return
        before = dict(pending)
        pending[envelope_id] = replace(record, phase="applied").to_dict()
        if not _persist_or_restore(client, pending, before):
            return
        if approved:
            _notify_runtime(client)
    try:
        await client._bridge.send_ack([envelope_id])
    except Exception as exc:  # noqa: BLE001 - ack failure retains applied state
        client._log.warning(
            "keyless_dm_approval: bridge ack for %s failed (%s); kept at "
            "phase=applied for ack-only retry",
            envelope_id,
            exc,
        )
        return
    before = dict(pending)
    pending.pop(envelope_id, None)
    _persist_or_restore(client, pending, before)


async def _apply_local_transition(
    client,
    record: KeylessDmApproval,
    *,
    approved: bool,
) -> bool:
    """Apply one decided record's local mutation. Returns whether it is
    applied (``COMMITTED`` or ``IDEMPOTENT``). ``CONFLICT``, an exception,
    or any other result retains the record and never ACKs it."""
    if approved:
        try:
            client._contacts.note_allowed(record.sender_slug)
        except OSError as exc:
            client._log.warning(
                "keyless_dm_approval: contact allow persistence for %s "
                "failed (%s); retained resumable",
                record.sender_slug,
                exc,
            )
            return False
        try:
            result = await client.store.promote_gated_receipt(
                record.envelope_id,
                record.server_seq,
                reason=APPROVED_REASON,
            )
        except Exception as exc:  # noqa: BLE001 - resumable store failure
            client._log.warning(
                "keyless_dm_approval: promote for %s failed (%s); retained resumable",
                record.envelope_id,
                exc,
            )
            return False
    else:
        try:
            client._contacts.note_blocked(record.sender_slug, True)
        except OSError as exc:
            client._log.warning(
                "keyless_dm_approval: contact block persistence for %s "
                "failed (%s); retained resumable",
                record.sender_slug,
                exc,
            )
            return False
        try:
            result = await client.store.tombstone_gated_dm(
                record.envelope_id,
                record.server_seq,
            )
        except Exception as exc:  # noqa: BLE001 - resumable store failure
            client._log.warning(
                "keyless_dm_approval: tombstone for %s failed (%s); retained resumable",
                record.envelope_id,
                exc,
            )
            return False
    if result.status not in _APPLIED_STATUSES:
        client._log.warning(
            "keyless_dm_approval: %s for %s returned %s; retained, no ACK",
            "promotion" if approved else "tombstone",
            record.envelope_id,
            result.status,
        )
        return False
    return True
