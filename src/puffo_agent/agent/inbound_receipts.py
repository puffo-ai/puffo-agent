"""Inbound receipt processing behind ``PuffoCoreMessageClient.listen``."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..crypto.message import MessagePayload, decrypt_message, read_plaintext_message
from ..crypto.primitives import KemKeyPair
from ..crypto.ws_client import ServerDelivery, TransportOutcome
from ._logging import log_runtime_event
from .client_support import DM_GATE_PROMPT_PLACEHOLDER
from .contact_cache import BlocklistUnavailable
from .ingress_policy import (
    BLOCKED_MESSAGE_PLACEHOLDER,
    GateVerdict,
    blocked_gate,
    foreign_dm_gate,
    operator_control_gate,
)
from .message_context import (
    MENTION_RE,
    content_with_visibility,
    maybe_redact_long_text,
)
from .message_store import ReceiptDisposition, ReceiptWriteStatus

if TYPE_CHECKING:
    from .puffo_core_client import PuffoCoreMessageClient


__all__ = ["BLOCKED_MESSAGE_PLACEHOLDER", "InboundReceiptHandler"]


@dataclass
class _ReceiptCommitter:
    client: PuffoCoreMessageClient
    payload: MessagePayload
    server_seq: int
    stored_payload: dict[str, Any]

    async def commit(
        self,
        disposition: ReceiptDisposition,
        reason: str,
        *,
        content: Any | None = None,
    ) -> TransportOutcome:
        self._log_received()
        row = dict(self.stored_payload)
        if content is not None:
            row["content"] = content
            row["content_type"] = "text/plain"

        result = None
        if disposition is ReceiptDisposition.ELIGIBLE:
            promoted = await self.client.store.promote_gated_receipt(
                self.payload.envelope_id,
                self.server_seq,
                reason=reason,
                approved_payload=row,
            )
            if promoted.status is not ReceiptWriteStatus.CONFLICT:
                result = promoted
        if result is None:
            result = await self.client.store.store_receipt(
                row,
                server_seq=self.server_seq,
                disposition=disposition,
                reason=reason,
            )
        if result.status is ReceiptWriteStatus.CONFLICT:
            return TransportOutcome.HOLD

        self._notify_runtime(result)
        try:
            await self._log_commit(result)
        except Exception:
            self.client._log.exception(
                "receipt observability failed after durable commit "
                "(envelope_id=%s)",
                self.payload.envelope_id,
            )
        if result.acknowledge:
            return TransportOutcome.ACK
        if (
            result.disposition is ReceiptDisposition.FOREIGN_DM_GATED
            and result.status
            in (ReceiptWriteStatus.COMMITTED, ReceiptWriteStatus.IDEMPOTENT)
        ):
            return TransportOutcome.DEFER
        return TransportOutcome.HOLD

    def _log_received(self) -> None:
        log_runtime_event(
            self.client._log,
            "inbox.received",
            agent_id=self.client.agent_id,
            agent_slug=self.client.slug,
            message_id=self.payload.envelope_id,
            server_seq=self.server_seq,
            space_id=self.payload.space_id,
            channel_id=self.payload.channel_id,
            outcome="received",
        )

    async def _log_commit(self, result) -> None:
        if result.status is not ReceiptWriteStatus.COMMITTED:
            return
        fields = dict(
            agent_id=self.client.agent_id,
            agent_slug=self.client.slug,
            message_id=self.payload.envelope_id,
            server_seq=self.server_seq,
            space_id=self.payload.space_id,
            channel_id=self.payload.channel_id,
        )
        log_runtime_event(
            self.client._log,
            "inbox.receipt_committed",
            level=logging.INFO,
            envelope_id=self.payload.envelope_id,
            seq=self.server_seq,
            mode="transport_receipt",
            state=result.disposition.value,
            **fields,
        )
        log_runtime_event(
            self.client._log,
            "inbox.persisted",
            outcome=result.disposition.value,
            **fields,
        )
        notice = await self.client.store.get_notice_state()
        if notice.delivery_pending:
            log_runtime_event(
                self.client._log,
                "notice.armed",
                agent_id=self.client.agent_id,
                agent_slug=self.client.slug,
                notice_generation=notice.generation,
                message_count=notice.pending_count,
                outcome="armed",
            )

    def _notify_runtime(self, result) -> None:
        runtime = getattr(self.client, "global_runtime", None)
        if runtime is None:
            return
        if self.payload.envelope_kind == "channel" and result.status in (
            ReceiptWriteStatus.COMMITTED,
            ReceiptWriteStatus.IDEMPOTENT,
        ):
            runtime.notify_delivery()
        if (
            result.disposition is ReceiptDisposition.ELIGIBLE
            and result.status
            in (ReceiptWriteStatus.COMMITTED, ReceiptWriteStatus.IDEMPOTENT)
        ):
            runtime.notify()


class InboundReceiptHandler:
    """Turn one durable server delivery into one local receipt decision."""

    def __init__(
        self,
        client: PuffoCoreMessageClient,
        kem_keypair: KemKeyPair,
        *,
        decrypt=decrypt_message,
        read_plaintext=read_plaintext_message,
    ) -> None:
        self.client = client
        self.kem_keypair = kem_keypair
        self._decrypt = decrypt
        self._read_plaintext = read_plaintext

    async def handle(self, delivery: ServerDelivery) -> TransportOutcome:
        opened = await self._open_payload(delivery)
        if opened is None:
            return TransportOutcome.HOLD
        payload, is_plaintext = opened
        committer = await self._build_committer(
            payload,
            delivery["seq"],
            is_plaintext=is_plaintext,
        )

        outcome = await self._blocked_outcome(committer)
        if outcome is not None:
            return outcome
        outcome = await self._self_echo_outcome(committer)
        if outcome is not None:
            return outcome
        outcome = await self._operator_control_outcome(committer)
        if outcome is not None:
            return outcome
        # The gate decides on the text alone. Materializing attachments first
        # would hand an unapproved stranger a file write into the workspace
        # the model reads, which no later approval outcome undoes — the same
        # split the bridge lane already makes. It also runs *before* the
        # stale arm: catch-up staleness is a freshness judgement, not an
        # admission one, and terminalizing first would store an unapproved
        # stranger's body as acked readable plaintext that a later denial
        # can no longer tombstone (``tombstone_gated_dms_from`` only matches
        # still-gated rows).
        raw_text = self._raw_text(payload)
        outcome = await self._foreign_dm_outcome(committer, raw_text)
        if outcome is not None:
            return outcome
        outcome = await self._stale_outcome(committer)
        if outcome is not None:
            return outcome
        attachment_paths = await self._save_attachments(payload)
        self._cache_channel_space(payload)
        committer.stored_payload["content"] = await self._prompt_content(
            payload,
            raw_text,
            attachment_paths,
        )
        return await committer.commit(
            ReceiptDisposition.ELIGIBLE,
            "eligible message",
        )

    async def _open_payload(
        self,
        delivery: ServerDelivery,
    ) -> tuple[MessagePayload, bool] | None:
        envelope = delivery["envelope"]
        is_plaintext = envelope.get("type") == "plaintext_message_envelope"
        sender_slug = self._sender_slug(envelope, is_plaintext)
        try:
            sender_keys = await self.client._key_cache.get_signing_keys(sender_slug)
        except Exception as exc:
            self.client._log.warning(
                "could not fetch signing keys for %s — skipping (%s)",
                sender_slug,
                exc,
            )
            return None

        payload = self._decode_with_keys(envelope, sender_keys, is_plaintext)
        if payload is None:
            self.client._key_cache.invalidate(sender_slug)
            try:
                sender_keys = await self.client._key_cache.get_signing_keys(sender_slug)
                payload = self._decode_with_keys(
                    envelope,
                    sender_keys,
                    is_plaintext,
                )
            except Exception:
                pass
        if payload is None:
            self.client._log.warning(
                "decryption failed for %s (%d sender keys tried) — skipping",
                envelope.get("envelope_id"),
                len(sender_keys),
            )
            return None
        return payload, is_plaintext

    @staticmethod
    def _sender_slug(envelope: dict[str, Any], is_plaintext: bool) -> str:
        if is_plaintext:
            return (
                envelope.get("signed_payload", {})
                .get("payload", {})
                .get("sender_slug", "")
            )
        return envelope.get("sender_slug", "")

    def _decode_with_keys(
        self,
        envelope: dict[str, Any],
        sender_keys: list[bytes],
        is_plaintext: bool,
    ) -> MessagePayload | None:
        for signing_key in sender_keys:
            try:
                if is_plaintext:
                    return self._read_plaintext(envelope, signing_key)
                return self._decrypt(
                    envelope,
                    self.client.device_id,
                    self.kem_keypair,
                    signing_key,
                )
            except Exception:
                continue
        return None

    async def _build_committer(
        self,
        payload: MessagePayload,
        server_seq: int,
        *,
        is_plaintext: bool,
    ) -> _ReceiptCommitter:
        dm_peer = (
            payload.recipient_slug
            if payload.sender_slug == self.client.slug
            else payload.sender_slug
        ) if payload.envelope_kind == "dm" else ""
        thread_root_id, thread_root_unverified = (
            await self.client._resolve_incoming_thread_root(
                payload.thread_root_id,
                payload.channel_id,
                payload.space_id,
                expected_envelope_kind=payload.envelope_kind,
                expected_dm_peer=dm_peer,
            )
        )
        reply_to_id = await self.client._validate_incoming_parent_id(
            payload.reply_to_id,
            payload.channel_id,
            payload.space_id,
            expected_envelope_kind=payload.envelope_kind,
            expected_dm_peer=dm_peer,
        )
        stored_payload = {
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
            "reply_to_id": reply_to_id,
            "thread_root_unverified": thread_root_unverified,
            "is_encrypted": not is_plaintext,
        }
        return _ReceiptCommitter(
            client=self.client,
            payload=payload,
            server_seq=server_seq,
            stored_payload=stored_payload,
        )

    async def _commit_verdict(
        self,
        committer: _ReceiptCommitter,
        verdict: GateVerdict | None,
    ) -> TransportOutcome | None:
        """Persist one shared-gate verdict through the native lane."""
        if verdict is None:
            return None
        return await committer.commit(
            verdict.disposition,
            verdict.reason,
            content=verdict.content,
        )

    async def _blocked_outcome(
        self,
        committer: _ReceiptCommitter,
    ) -> TransportOutcome | None:
        try:
            verdict = await blocked_gate(self.client, committer.payload)
        except BlocklistUnavailable as exc:
            # Hold rather than guess. The delivery stays unacked and the
            # server sends it again; admitting it would put a possibly
            # blocked sender in front of the model on the strength of an
            # empty cache.
            self.client._log.warning(
                "delivery held: %s (envelope_id=%s sender=%s)",
                exc,
                committer.payload.envelope_id,
                committer.payload.sender_slug,
            )
            return TransportOutcome.HOLD
        return await self._commit_verdict(committer, verdict)

    async def _self_echo_outcome(
        self,
        committer: _ReceiptCommitter,
    ) -> TransportOutcome | None:
        payload = committer.payload
        if payload.sender_slug != self.client.slug:
            return None
        if payload.envelope_kind == "dm":
            await self.client._maybe_allowlist_outbound_dm(payload.recipient_slug)
        replacement = self._echo_redaction(payload)
        committer.stored_payload["content"] = content_with_visibility(
            payload.content if replacement is None else replacement,
            is_visible_to_human=payload.is_visible_to_human,
        )
        if replacement is not None:
            committer.stored_payload["content_type"] = "text/plain"
        return await committer.commit(
            ReceiptDisposition.TERMINAL,
            "self echo",
        )

    def _echo_redaction(self, payload: MessagePayload) -> str | None:
        """Replacement body for a self-echo that must not be stored verbatim.

        The foreign-DM approval prompt quotes up to 280 characters of the
        stranger's message so the operator can judge it. The server echoes
        that DM back and the echo is stored terminal — and prior context
        selects terminal rows, so the operator's next ordinary DM would
        carry the withheld body into the model's context, gate approved or
        not. The prompt's own envelope id identifies it.
        """
        pending = getattr(self.client, "_pending_dm_approvals", None) or {}
        if payload.envelope_id in pending:
            return DM_GATE_PROMPT_PLACEHOLDER
        return None

    async def _operator_control_outcome(
        self,
        committer: _ReceiptCommitter,
    ) -> TransportOutcome | None:
        payload = committer.payload
        return await self._commit_verdict(
            committer,
            await operator_control_gate(
                self.client,
                payload,
                thread_root_id=committer.stored_payload["thread_root_id"] or "",
                text=str(payload.content) if payload.content else "",
            ),
        )

    async def _stale_outcome(
        self,
        committer: _ReceiptCommitter,
    ) -> TransportOutcome | None:
        payload = committer.payload
        if not self.client._is_stale_for_catchup(payload.sent_at):
            return None
        root_id = committer.stored_payload["thread_root_id"]
        self.client._log.info(
            "handle_envelope: staleness-gate-skipped envelope=%s "
            "(sent_at=%d, threshold_ms=%d, root=%s) — stored, no LLM",
            payload.envelope_id,
            payload.sent_at,
            self.client._catchup_stale_ms,
            root_id or payload.envelope_id,
        )
        await self.client._report_stale_processed(payload.envelope_id)
        return await committer.commit(
            ReceiptDisposition.TERMINAL,
            "stale catch-up",
        )

    @staticmethod
    def _raw_text(payload: MessagePayload) -> str:
        """The message body as plain text, with no side effects."""
        if payload.content_type == "puffo/message+attachments/v1" and isinstance(
            payload.content, dict
        ):
            return str(payload.content.get("text") or "")
        return str(payload.content) if payload.content else ""

    async def _save_attachments(self, payload: MessagePayload) -> list[str]:
        """Download and decrypt this message's attachments to the workspace.

        Only ever called for an admitted message: the files land under
        ``<workspace>/.puffo/inbox/<envelope_id>/``, which the harness's
        ordinary file tools can read.
        """
        if payload.content_type != "puffo/message+attachments/v1" or not isinstance(
            payload.content, dict
        ):
            return []
        raw_attachments = payload.content.get("attachments") or []
        if not isinstance(raw_attachments, list):
            return []
        return await self.client._save_inbound_attachments(
            envelope_id=payload.envelope_id,
            metas_raw=raw_attachments,
        )

    async def _foreign_dm_outcome(
        self,
        committer: _ReceiptCommitter,
        raw_text: str,
    ) -> TransportOutcome | None:
        return await self._commit_verdict(
            committer,
            await foreign_dm_gate(
                self.client,
                committer.payload,
                raw_text,
                bool(committer.stored_payload.get("is_encrypted")),
            ),
        )

    def _cache_channel_space(self, payload: MessagePayload) -> None:
        if payload.envelope_kind != "dm" and payload.channel_id and payload.space_id:
            self.client._channel_space[payload.channel_id] = payload.space_id

    async def _prompt_content(
        self,
        payload: MessagePayload,
        raw_text: str,
        attachment_paths: list[str],
    ) -> dict[str, Any]:
        mentions, is_mention = await self._mentions(payload, raw_text)
        clean_text = (
            raw_text.replace(
                f"@{self.client.slug}",
                f"@you({self.client.slug})",
            ).strip()
            if is_mention
            else raw_text
        )
        names = await self._display_context(payload)
        llm_text = maybe_redact_long_text(
            clean_text,
            envelope_id=payload.envelope_id,
            sender_slug=payload.sender_slug,
            sender_display_name=names["sender_display_name"],
            max_inline_chars=self.client._max_inline_chars,
            segment_chars=self.client._segment_chars,
            agent_slug=self.client.slug,
        )
        content: dict[str, Any] = {
            "text": llm_text,
            "attachment_paths": attachment_paths,
            "mentions": mentions,
            "sender_display_name": names["sender_display_name"],
            "sender_owner_slug": names["sender_owner_slug"],
            "is_from_operator": names["is_from_operator"],
            "sender_is_agent": names["sender_is_agent"],
            "is_visible_to_human": payload.is_visible_to_human,
            "channel_name": names["channel_name"],
            "space_name": names["space_name"],
        }
        if raw_text != llm_text:
            # Keep a durable source when the prompt view was bounded or
            # normalized. Unchanged short strings need no duplicate copy.
            content["original_content"] = payload.content
        # Only carried when authenticated facts actually classified the
        # sender; absent means projection falls back to ``unknown``.
        if names.get("sender_type"):
            content["sender_type"] = names["sender_type"]
        return content

    async def _mentions(
        self,
        payload: MessagePayload,
        raw_text: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        self_slug = self.client.slug.lower()
        parsed: list[str] = []
        seen: set[str] = set()
        for match in MENTION_RE.finditer(raw_text):
            slug = match.group(1).lower()
            if slug not in seen:
                seen.add(slug)
                parsed.append(slug)

        space_members = (
            await self.client._get_space_members(payload.space_id)
            if payload.space_id
            else {}
        )
        mentions: list[dict[str, Any]] = []
        for slug in parsed:
            if slug == self_slug:
                mentions.append(
                    {"username": self.client.slug, "is_agent": True, "is_self": True}
                )
            elif not space_members or slug in space_members:
                mentions.append(
                    {
                        "username": slug,
                        "is_agent": space_members.get(slug) == "agent",
                        "is_self": False,
                    }
                )
        return mentions, self_slug in seen

    async def _display_context(self, payload: MessagePayload) -> dict[str, Any]:
        space_id = payload.space_id or ""
        space_name = await self.client._resolve_space_name(space_id) if space_id else ""
        if payload.envelope_kind == "dm":
            channel_name = "Direct message"
        elif payload.channel_id:
            channel_name = await self.client._resolve_channel_name(
                space_id,
                payload.channel_id,
            )
        else:
            channel_name = payload.channel_id or ""

        sender_display_name = await self.client._fetch_display_name(payload.sender_slug)
        sender_owner_slug = await self.client._fetch_owner_slug(payload.sender_slug)
        is_from_operator = bool(
            self.client.operator_slug
            and payload.sender_slug == self.client.operator_slug
        )
        context = {
            "channel_name": channel_name,
            "space_name": space_name,
            "sender_display_name": sender_display_name,
            "sender_owner_slug": sender_owner_slug,
            "is_from_operator": is_from_operator,
            "sender_is_agent": bool(sender_owner_slug),
        }
        sender_type = await self._sender_type(
            payload,
            sender_owner_slug=sender_owner_slug,
            is_from_operator=is_from_operator,
        )
        if sender_type:
            context["sender_type"] = sender_type
        return context

    async def _sender_type(
        self,
        payload: MessagePayload,
        *,
        sender_owner_slug: str,
        is_from_operator: bool,
    ) -> str:
        """Classify the sender from authenticated facts only.

        An owner slug is server-issued and only agents carry one, so it
        is the ground truth for ``agent``. The operator is an
        authenticated human by configuration. Any other sender is
        classified from the space roster's ``identity_type``, which the
        server supplies via ``/spaces/{id}/members``. Nothing else is
        knowable — ``/identities/profiles`` returns no identity-type
        field — so an unclassifiable sender stays unset and projects as
        ``unknown`` rather than being guessed at.
        """
        if sender_owner_slug:
            return "agent"
        if is_from_operator:
            return "human"
        if not payload.space_id:
            return ""
        members = await self.client._get_space_members(payload.space_id)
        roster_type = members.get(payload.sender_slug, "")
        return roster_type if roster_type in {"human", "agent"} else ""
