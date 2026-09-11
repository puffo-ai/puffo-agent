"""Resumable keyless invitation operator-decision component.

One durable :class:`~puffo_agent.agent.keyless_invitation_state.KeylessInvitation`
per authoritative ``invitation_event_id``. The three async entry points
(``reconcile``, ``handle_operator_reply``, ``resume``) are scheduled by the
bridge ingress integration in ``bridge_transport``, which owns the
connection-scoped invitation poller that feeds this flow.

Reconcile: merge the Server's authoritative pending list by exact event id
without duplicating a prompt, retire/log active records that vanished from
the list, preserve the existing space-only auto-accept policy (space-scope
invites when the flag is set, plus the native path's operator auto-accept),
and fail closed with a clear warning when ``operator_slug`` is missing.

Progression: a new external invite is prompted once via the configured
operator; only an exact threaded ``y``/``yes``/``n``/``no`` reply is
consumed; the decision (and its stable ``decision_client_ref``) is persisted
before the decision frame is sent; ``applied`` / ``already_applied`` are
terminal success while ``conflict`` / ``not_found`` resolve terminal and
logged without a retry loop.

Replay: the record is persisted before the prompt and before the decision
send, so ``resume`` can re-send a missing prompt with its stable prompt ref
or retry a decided-but-nonterminal record with its stable decision ref after
every reconnect, while leaving a known prompt awaiting its exact reply.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from typing import Any, Awaitable, Callable

from .keyless_invitation_state import (
    INVITATION_OUTCOMES,
    KeylessInvitation,
    load_keyless_invitations,
    parse_keyless_invitation,
    save_keyless_invitations,
)
from .permission_prompt import format_permission_prompt

logger = logging.getLogger(__name__)

_YES = frozenset({"y", "yes"})
_NO = frozenset({"n", "no"})

# Auto-accept mirrors the native ``process_invite`` path: an invite sent by
# the configured operator is inherently authorized, and the
# ``auto_accept_space_invitations`` flag applies to space scope only (no new
# channel default).
_SCOPE_LABELS = {"space": "space", "channel": "channel"}


def _prompt_client_ref() -> str:
    return f"invite_prompt_{uuid.uuid4().hex[:12]}"


def _decision_client_ref() -> str:
    return f"invite_dec_{uuid.uuid4().hex[:12]}"


class KeylessInvitationFlow:
    """Schedulable, resumable keyless invitation decision component.

    Owns the in-memory mirror of the durable per-Agent invitation state
    plus the async lock that serializes reconcile / prompt / reply / resume
    transitions, so a caller can schedule it outside the bridge frame pump
    without corrupting state or duplicating a prompt.
    """

    def __init__(
        self,
        *,
        slug: str,
        bridge: Any,
        operator_slug: str,
        send_dm: Callable[..., Awaitable[dict]],
        auto_accept_space_invitations: bool = False,
        log: Any = None,
        load_state: Callable[[str], dict[str, dict[str, Any]]] = load_keyless_invitations,
        save_state: Callable[[str, dict[str, dict[str, Any]]], None] = save_keyless_invitations,
    ) -> None:
        self._slug = slug
        self._bridge = bridge
        self._operator_slug = operator_slug or ""
        self._send_dm = send_dm
        self._auto_accept_space = bool(auto_accept_space_invitations)
        self._log = log or logging.getLogger(__name__)
        self._load_state = load_state
        self._save_state = save_state
        self._pending = load_state(slug)
        self._lock = asyncio.Lock()

    async def reconcile(self, invites: Any) -> None:
        """Merge the authoritative pending invite list into durable state."""
        async with self._lock:
            await self._reconcile_locked(invites)

    async def handle_operator_reply(
        self, *, thread_root_id: str, text: str,
    ) -> bool:
        """Consume an exact threaded ``y``/``yes``/``n``/``no`` operator
        reply for one prompted invite. ``True`` means its decision is durable
        and the reply may be acknowledged; unknown replies and failed decision
        persistence return ``False``."""
        async with self._lock:
            return await self._handle_reply_locked(thread_root_id, text)

    async def resume(self) -> None:
        """Deterministic replay of persisted keyless invitations after a
        reconnect: re-send an unsent prompt with its stable ref, retry a
        decided-but-nonterminal decision with its stable ref, and leave a
        known prompt awaiting its exact reply."""
        async with self._lock:
            await self._resume_locked()

    def is_prompt_envelope(self, envelope_id: str) -> bool:
        """Whether ``envelope_id`` is a durable invitation-prompt envelope.

        Pure and synchronous: bridge ingress runs this on the frame-pump
        stack so the echo of the agent's own operator prompt is stored as a
        short control placeholder rather than delivered to model context.
        Terminal records still count — a late echo of an already-decided
        prompt must not resurface as operator-visible prompt text.
        """
        if not envelope_id:
            return False
        return any(
            (record := parse_keyless_invitation(value)) is not None
            and record.prompt_envelope_id == envelope_id
            for value in self._pending.values()
        )

    def is_operator_reply(self, *, thread_root_id: str, text: str) -> bool:
        """Whether ``text`` is an exact threaded operator decision for a
        durable invitation prompt.

        Pure and synchronous: bridge ingress recognizes the reply on the
        frame-pump stack before scheduling the response-waiting
        :meth:`handle_operator_reply` as a tracked task. It mirrors the
        handler's exact ``y``/``yes``/``n``/``no`` vocabulary and thread
        match but never mutates or persists anything.
        """
        if not thread_root_id:
            return False
        if text.strip().lower() not in _YES | _NO:
            return False
        return self._matching_record(thread_root_id) is not None

    async def _reconcile_locked(self, invites: Any) -> None:
        if isinstance(invites, dict) and isinstance(invites.get("invites"), list):
            invites = invites["invites"]
        if not isinstance(invites, list):
            self._log.warning(
                "keyless_invitation: reconcile expected an invites list, "
                "got %r; ignoring",
                type(invites).__name__,
            )
            return
        valid = self._valid_invites(invites)
        seen: set[str] = set()
        for invite in valid:
            event_id = invite["invitation_event_id"]
            seen.add(event_id)
            existing = parse_keyless_invitation(self._pending.get(event_id))
            if existing is not None:
                # Known (active or terminal) — never prompt twice.
                continue
            await self._adopt_new_invite(invite)
        before = dict(self._pending)
        retired = False
        for event_id, value in list(self._pending.items()):
            record = parse_keyless_invitation(value)
            if record is None or record.phase == "terminal":
                continue
            if record.invitation_event_id not in seen:
                self._log.warning(
                    "keyless_invitation: retiring active invite %s absent "
                    "from authoritative list",
                    event_id,
                )
                del self._pending[event_id]
                retired = True
        if retired:
            self._persist_or_restore(before)

    def _valid_invites(self, invites: list[Any]) -> list[dict[str, Any]]:
        valid: list[dict[str, Any]] = []
        for invite in invites:
            if not isinstance(invite, dict):
                self._log.warning(
                    "keyless_invitation: skipping malformed invite %r", invite,
                )
                continue
            event_id = invite.get("invitation_event_id")
            scope = invite.get("scope")
            space_id = invite.get("space_id")
            if not isinstance(event_id, str) or not event_id:
                self._log.warning(
                    "keyless_invitation: skipping invite without an event id",
                )
                continue
            if scope not in _SCOPE_LABELS:
                self._log.warning(
                    "keyless_invitation: skipping invite %s with bad scope %r",
                    event_id,
                    scope,
                )
                continue
            if not isinstance(space_id, str) or not space_id:
                self._log.warning(
                    "keyless_invitation: skipping invite %s without space_id",
                    event_id,
                )
                continue
            if scope == "channel":
                channel_id = invite.get("channel_id")
                if not isinstance(channel_id, str) or not channel_id:
                    self._log.warning(
                        "keyless_invitation: skipping channel invite %s "
                        "without channel_id",
                        event_id,
                    )
                    continue
            valid.append(invite)
        return valid

    async def _adopt_new_invite(self, invite: dict[str, Any]) -> None:
        event_id = invite["invitation_event_id"]
        record = KeylessInvitation(
            invitation_event_id=event_id,
            invite=invite,
            prompt_client_ref=_prompt_client_ref(),
            prompt_envelope_id=None,
            prompt_thread_id=None,
            decision="pending",
            decision_client_ref=None,
            phase="unsent",
            outcome=None,
        )
        if not self._operator_slug:
            before = dict(self._pending)
            self._pending[event_id] = record.to_dict()
            if not self._persist_or_restore(before):
                return
            self._log.warning(
                "keyless_invitation: no operator_slug configured; invite %s "
                "retained pending locally and on Server",
                event_id,
            )
            return
        if self._auto_accept(invite):
            before = dict(self._pending)
            self._pending[event_id] = record.to_dict()
            if not self._persist_or_restore(before):
                return
            await self._send_decision_locked(event_id, decision="accept")
            return
        before = dict(self._pending)
        self._pending[event_id] = record.to_dict()
        if not self._persist_or_restore(before):
            return
        await self._send_prompt_locked(event_id)

    def _auto_accept(self, invite: dict[str, Any]) -> bool:
        if invite.get("inviter_slug") == self._operator_slug:
            return True
        return invite.get("scope") == "space" and self._auto_accept_space

    async def _send_prompt_locked(self, event_id: str) -> None:
        record = parse_keyless_invitation(self._pending.get(event_id))
        if record is None or record.phase != "unsent":
            return
        scope = record.invite.get("scope", "space")
        intent = (
            f"{record.invite.get('inviter_slug') or 'someone'} invited me "
            f"to the {_SCOPE_LABELS.get(scope, 'space')} "
            f"{record.invite.get('space_id')} — accept or reject?"
        )
        prompt = format_permission_prompt(intent)
        try:
            ack = await self._send_dm(prompt, client_ref=record.prompt_client_ref)
        except Exception as exc:  # noqa: BLE001 - resumable transport failure
            self._log.warning(
                "keyless_invitation: prompt send for %s failed (%s); record "
                "retained unsent for resume",
                event_id,
                exc,
            )
            return
        envelope = (ack or {}).get("envelope_id") or ""
        if not envelope:
            self._log.warning(
                "keyless_invitation: prompt ack for %s carried no "
                "envelope_id; record retained unsent for resume",
                event_id,
            )
            return
        thread = (ack or {}).get("thread_root_id") or envelope
        before = dict(self._pending)
        self._pending[event_id] = replace(
            record,
            prompt_envelope_id=envelope,
            prompt_thread_id=thread,
            phase="awaiting_reply",
        ).to_dict()
        self._persist_or_restore(before)

    async def _handle_reply_locked(
        self, thread_root_id: str, text: str,
    ) -> bool:
        normalized = text.strip().lower()
        if normalized in _YES:
            decision = "accept"
        elif normalized in _NO:
            decision = "reject"
        else:
            return False
        record = self._matching_record(thread_root_id)
        if record is None:
            return False
        if record.phase == "terminal":
            return True
        if record.decision != "pending":
            if record.decision != decision:
                self._log.warning(
                    "keyless_invitation: ignored conflicting %s reply for "
                    "already decided invite %s (%s)",
                    decision,
                    record.invitation_event_id,
                    record.decision,
                )
            return True
        decision_client_ref = record.decision_client_ref or _decision_client_ref()
        before = dict(self._pending)
        self._pending[record.invitation_event_id] = replace(
            record,
            decision=decision,
            decision_client_ref=decision_client_ref,
            phase="decided",
        ).to_dict()
        if not self._persist_or_restore(before):
            return False
        await self._send_decision_locked(record.invitation_event_id)
        return True

    def _matching_record(self, thread_root_id: str) -> KeylessInvitation | None:
        for value in self._pending.values():
            record = parse_keyless_invitation(value)
            if record is None:
                continue
            if (
                record.prompt_envelope_id == thread_root_id
                or record.prompt_thread_id == thread_root_id
            ):
                return record
        return None

    async def _send_decision_locked(
        self, event_id: str, *, decision: str | None = None,
    ) -> None:
        record = parse_keyless_invitation(self._pending.get(event_id))
        if record is None or record.phase == "terminal":
            return
        resolved = decision or record.decision
        if resolved not in ("accept", "reject"):
            return
        decision_client_ref = record.decision_client_ref
        if decision_client_ref is None:
            decision_client_ref = _decision_client_ref()
            before = dict(self._pending)
            self._pending[event_id] = replace(
                record,
                decision=resolved,
                decision_client_ref=decision_client_ref,
                phase="decided",
            ).to_dict()
            if not self._persist_or_restore(before):
                return
            record = parse_keyless_invitation(self._pending.get(event_id))
        try:
            result = await self._bridge.send_decide_invitation(
                invitation_event_id=event_id,
                decision=resolved,
                client_ref=decision_client_ref,
            )
        except Exception as exc:  # noqa: BLE001 - resumable; no retry loop
            self._log.warning(
                "keyless_invitation: decision send for %s failed (%s); "
                "record retained decided for resume",
                event_id,
                exc,
            )
            return
        outcome = (result or {}).get("outcome") or ""
        if outcome not in INVITATION_OUTCOMES:
            self._log.warning(
                "keyless_invitation: unexpected decision outcome %r for %s; "
                "record retained decided for resume",
                outcome,
                event_id,
            )
            return
        before = dict(self._pending)
        self._pending[event_id] = replace(
            record,
            phase="terminal",
            outcome=outcome,
        ).to_dict()
        self._persist_or_restore(before)
        if outcome in ("conflict", "not_found"):
            self._log.warning(
                "keyless_invitation: invite %s resolved terminal %s",
                event_id,
                outcome,
            )
        else:
            self._log.info(
                "keyless_invitation: invite %s decision %s -> %s",
                event_id,
                resolved,
                outcome,
            )

    async def _resume_locked(self) -> None:
        decided: list[str] = []
        for record in self._active_records():
            if record.phase == "decided":
                decided.append(record.invitation_event_id)
            elif record.phase == "unsent":
                await self._resume_prompt(record)
            elif record.phase == "awaiting_reply":
                if record.prompt_envelope_id is None and record.prompt_thread_id is None:
                    await self._resume_prompt(record)
                # A known prompt keeps awaiting its exact reply.
        for event_id in decided:
            await self._send_decision_locked(event_id)

    async def _resume_prompt(self, record: KeylessInvitation) -> None:
        if not self._operator_slug:
            self._log.warning(
                "keyless_invitation: resume fail-closed — no operator_slug "
                "for invite %s; retained pending",
                record.invitation_event_id,
            )
            return
        await self._send_prompt_locked(record.invitation_event_id)

    def _active_records(self) -> list[KeylessInvitation]:
        records = []
        for value in self._pending.values():
            record = parse_keyless_invitation(value)
            if record is not None and record.phase != "terminal":
                records.append(record)
        return records

    def _persist(self) -> bool:
        try:
            self._save_state(self._slug, self._pending)
        except OSError as exc:
            self._log.warning(
                "keyless_invitation: state persist failed: %s", exc,
            )
            return False
        return True

    def _persist_or_restore(
        self, before: dict[str, dict[str, Any]],
    ) -> bool:
        """Keep the in-memory mirror aligned with the durable file on failure."""
        if self._persist():
            return True
        self._pending.clear()
        self._pending.update(before)
        return False
