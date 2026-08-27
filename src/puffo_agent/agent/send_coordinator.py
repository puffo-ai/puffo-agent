"""Daemon-owned semantic message send coordination.

The coordinator is deliberately independent of provider/model state.  Its
freshness inputs are injected so the daemon can keep one coordinator alive
while Package 4 owns the sources that implement those protocols.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from ..crypto.attachments import (
    ATTACHMENT_CONTENT_TYPE,
    AttachmentMeta,
    encrypt_attachment,
)
from ..crypto.http_client import HttpError
from ..crypto.keystore import decode_secret
from ..crypto.message import (
    EncryptInput,
    build_plaintext_message,
    build_supplementation_envelope,
    encrypt_message_with_content_key,
)
from ..crypto.primitives import Ed25519KeyPair
from .channel_format import is_channel_format_mismatch
from .held_context import build_held_context_output
from .message_projection import CONTEXT_VERSION
from .send_models import SemanticSendRequest, SendResult
from .send_response_validation import (
    BASELINE_PERSISTENCE_WARNING,
    coordinator_config,
    http_error_detail,
    optional_response_int,
    persist_baseline,
    validate_channel_response,
    validate_keyless_response,
)
from .shared_content import HELD_SEND_RECONSIDERATION_GUIDANCE

logger = logging.getLogger(__name__)
CHANNEL_SEND_PATH = "/v2/agent-runtime/messages:send"
KEYLESS_CHANNEL_SEND_PATH = "/v2/cloud-agents/agent-runtime/messages:send"
_KNOWN_ERROR_STATUSES = {400, 401, 403, 404, 405, 409, 413, 429, 500, 503}
# Held evidence holds decrypted context, so retention is bounded even when a
# turn is abandoned without any further coordinated send in that channel.
_MAX_HELD_RECORDS = 8


@dataclass
class _HeldEvidence:
    latest_seq: int
    latest_envelope_id: str
    synchronized: bool = False
    attempt_fingerprint: str = ""
    content_digest: str = ""
    draft: str = ""
    based_on_through_seq: int | None = None
    thread_root_id: str = ""
    recovered_messages: list[dict[str, Any]] = field(default_factory=list)
    visible_draft_basis: list[dict[str, Any]] = field(default_factory=list)
    diagnostic: str = ""


@dataclass(frozen=True)
class _ReconsiderationDecision:
    eligible: bool
    reason: str
    provider_session_id: str
    turn_id: str
    latest_seq: int | None = None
    latest_envelope_id: str | None = None
    admitted_seq: int | None = None

    def audit_fields(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "decision_reason": self.reason,
            "provider_session_id": self.provider_session_id,
            "turn_id": self.turn_id,
            "latest_seq": self.latest_seq,
            "latest_envelope_id": self.latest_envelope_id,
            "admitted_seq": self.admitted_seq,
        }


@dataclass(frozen=True)
class _ChannelSendBoundary:
    baseline: int | None
    seen_seq: int
    held_key: tuple[str, str, str, str]
    attempt_fingerprint: str
    reconsideration: _ReconsiderationDecision | None
    held_generation: int = 0  # discard generation seen before the transport
    # Bytes read once, before the reconsideration decision, and reused for the
    # actual upload — so the approved payload is the transmitted payload.
    materialized: tuple[tuple[str, bytes], ...] = ()
    content_digest: str = ""


@dataclass(frozen=True)
class _HeldRecoveryAttempt:
    key: tuple[str, str, str, str]
    session_id: str
    turn_id: str
    space_id: str
    channel_id: str
    latest_seq: int
    latest_envelope_id: str
    attempt_fingerprint: str = ""


@runtime_checkable
class ContextBaselineSource(Protocol):
    async def get_context_baseline_seq(
        self, space_id: str, channel_id: str
    ) -> Optional[int]: ...
    async def set_context_baseline_seq(self, space_id, channel_id, context_baseline_seq: int) -> None: ...


@runtime_checkable
class ActiveTurnBoundarySource(Protocol):
    async def get_active_turn_through_seq(
        self, space_id: str, channel_id: str
    ) -> Optional[int]: ...
    async def advance_active_turn_through_seq(
        self, space_id: str, channel_id: str, seq: int
    ) -> None: ...


@runtime_checkable
class HeldRecoverySource(Protocol):
    async def wait_for_held_delivery(
        self, space_id: str, channel_id: str, latest_seq: int, latest_envelope_id: str
    ) -> Any: ...
    async def query_held_messages(
        self,
        space_id: str,
        channel_id: str,
        latest_seq: int,
        latest_envelope_id: str,
        provider_session_id: Optional[str],
    ) -> Sequence[Mapping[str, Any]]: ...


def failed_result(
    message: str, *, kind: str = "unavailable", status: int | None = None
) -> dict[str, Any]:
    return SendResult(
        state="failed",
        error=message,
        error_kind=kind,
        status=status,
    ).to_dict()


async def _call_first(obj: Any, names: Sequence[str], *args: Any) -> Any:
    if obj is None:
        return None
    for name in names:
        fn = getattr(obj, name, None)
        if fn is not None:
            return await fn(*args)
    return None


class SendCoordinator:
    """Persistent per-worker coordinator for all model-authored sends."""

    def __init__(
        self,
        *,
        slug: str,
        keystore: Any,
        http_client: Any,
        data_client: Any,
        workspace: str | None = None,
        shared_workspace: str | None = None,
        baseline_source: ContextBaselineSource | Any | None = None,
        active_turn_source: ActiveTurnBoundarySource | Any | None = None,
        held_recovery_source: HeldRecoverySource | Any | None = None,
        provider_session_id: str | None = None,
        channel_policy_source: Any | None = None,
    ) -> None:
        self.slug = slug
        self.keystore = keystore
        self.http_client = http_client
        self.data_client = data_client
        self.workspace = workspace
        self.shared_workspace = shared_workspace
        self.baseline_source = baseline_source
        self.active_turn_source = active_turn_source
        self.held_recovery_source = held_recovery_source
        self.provider_session_id = provider_session_id
        # ensure_channel_policy/refresh_channel_policy provider (the message
        # client). None -> always encrypt (keyless never routes through here).
        self.channel_policy_source = channel_policy_source
        self._channel_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._held_lock = asyncio.Lock()
        self._held_evidence: dict[tuple[str, str, str, str], _HeldEvidence] = {}
        self._held_generation = 0  # bumped by every teardown discard

    def _turn_identity(self) -> tuple[str, str]:
        active = getattr(self.active_turn_source, "active", None)
        turn_id = str(getattr(active, "turn_id", "") or "")
        configured_session_id = str(self.provider_session_id or "")
        active_session_id = str(getattr(active, "provider_session_id", "") or "")
        if (
            configured_session_id
            and active_session_id
            and configured_session_id != active_session_id
        ):
            return "", turn_id
        session_id = configured_session_id or active_session_id
        return session_id, turn_id

    async def send(
        self,
        request: SemanticSendRequest | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            if request is None:
                request = SemanticSendRequest.from_mapping(kwargs)
            elif isinstance(request, Mapping):
                request = SemanticSendRequest.from_mapping(request)
            elif kwargs:
                raise ValueError("pass either a request or keyword fields, not both")
            if not isinstance(request, SemanticSendRequest):
                raise ValueError("invalid semantic send request")
            return await self._send_request(request)
        except Exception as exc:  # semantic facade never leaks tool exceptions
            logger.exception("semantic send failed before transport")
            return failed_result(str(exc), kind="validation")

    async def send_message(self, **kwargs: Any) -> dict[str, Any]:
        return await self.send(kwargs)

    async def _send_request(self, request: SemanticSendRequest) -> dict[str, Any]:
        destination = request.destination.strip()
        if not destination:
            return failed_result("channel is required", kind="validation")
        try:
            self._validate_attachment_targets(request)
        except Exception as exc:
            return failed_result(str(exc), kind="validation")
        if destination.startswith("#"):
            return failed_result(
                "'#<name>' channel addressing isn't supported; use a channel id",
                kind="validation",
            )
        if getattr(self.http_client, "keyless", False):
            return await self._send_keyless(request, destination)
        if destination.startswith("@"):
            return await self._send_dm(request, destination[1:])

        from ..mcp.puffo_core_tools import _resolve_channel_space

        try:
            space_id = await _resolve_channel_space(
                coordinator_config(self), destination
            )
        except Exception as exc:
            return failed_result(str(exc), kind="routing")
        key = (space_id, destination)
        lock = self._channel_locks.setdefault(key, asyncio.Lock())
        async with lock:
            result = await self._send_channel(request, space_id, destination)
        if result.get("state") == "held":
            fingerprint = request.attempt_fingerprint()
            synchronized = await self._recover_held(
                space_id,
                destination,
                result.get("latest_seq"),
                result.get("latest_envelope_id"),
                fingerprint,
            )
            result["synchronized"] = synchronized
            session_id, turn_id = self._turn_identity()
            result.update(
                await self._held_context_output(
                    (session_id, turn_id, space_id, destination),
                    space_id,
                    destination,
                    fingerprint,
                )
            )
        return result

    async def _send_keyless(
        self,
        request: SemanticSendRequest,
        destination: str,
    ) -> dict[str, Any]:
        """Use coordinated Runtime-v2 channels and preserve legacy keyless DMs."""
        from ..mcp.puffo_core_tools import (
            _resolve_channel_space,
            _resolve_outgoing_root,
        )
        from ._visibility import resolve_visibility

        if not destination.startswith("@"):
            return await self._send_keyless_channel_route(
                request,
                destination,
                _resolve_channel_space,
            )
        dm_peer = destination[1:]
        if not dm_peer:
            return failed_result(
                "DM recipient slug is required after '@'",
                kind="validation",
            )
        return await self._send_keyless_dm(
            request,
            destination,
            dm_peer,
            _resolve_outgoing_root,
            resolve_visibility,
        )

    async def _send_keyless_channel_route(
        self,
        request: SemanticSendRequest,
        destination: str,
        resolve_space,
    ) -> dict[str, Any]:
        try:
            space_id = await resolve_space(coordinator_config(self), destination)
        except Exception as exc:
            return failed_result(str(exc), kind="routing")
        lock = self._channel_locks.setdefault((space_id, destination), asyncio.Lock())
        async with lock:
            return await self._send_keyless_channel(request, space_id, destination)

    async def _send_keyless_dm(
        self,
        request: SemanticSendRequest,
        destination: str,
        dm_peer: str,
        resolve_root,
        resolve_visibility,
    ) -> dict[str, Any]:
        try:
            root_id, root_note = await resolve_root(
                request.root_id,
                self.data_client,
                self_slug=self.slug,
                channel_id=None,
                space_id=None,
                dm_peer=dm_peer,
            )
            visible, visibility_note = await resolve_visibility(
                request.visibility_level,
                destination,
                request.caption if request.attachment_paths else request.text,
                root_id or "",
                self.http_client,
            )
            body = {
                "plaintext": request.caption
                if request.attachment_paths
                else request.text,
                "is_visible_to_human": visible,
                "recipient_slug": dm_peer,
            }
            if root_id:
                body["thread_root_id"] = root_id
                if request.root_id:
                    body["reply_to_id"] = request.root_id
            refs, total_bytes = await self._upload_keyless_legacy_attachments(request)
            if refs:
                body["attachments"] = refs
            raw = (
                await self.http_client.post_unsigned(
                    "/v2/cloud-agents/messages",
                    body,
                )
                or {}
            )
            return self._keyless_legacy_result(
                raw,
                destination,
                root_note,
                visibility_note,
                refs,
                total_bytes,
            )
        except HttpError as exc:
            return SendResult(
                state="failed",
                error=http_error_detail(exc.body),
                error_kind="http",
                status=exc.status,
            ).to_dict()
        except Exception as exc:
            return failed_result(str(exc), kind="validation")

    async def _upload_keyless_legacy_attachments(
        self,
        request: SemanticSendRequest,
    ) -> tuple[list[dict[str, Any]], int]:
        prepared: list[tuple[Path, bytes, str]] = []
        total_bytes = 0
        for target in self._validate_attachment_targets(request):
            plaintext = target.read_bytes()
            mime_type = (
                mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            )
            prepared.append((target, plaintext, mime_type))
            total_bytes += len(plaintext)
        refs: list[dict[str, Any]] = []
        for target, plaintext, mime_type in prepared:
            upload = await self.http_client.post_bytes_unsigned(
                "/v2/cloud-agents/blobs/upload",
                plaintext,
            )
            blob_id = upload.get("blob_id") if isinstance(upload, Mapping) else None
            if not blob_id:
                raise RuntimeError(
                    f"keyless upload returned no blob_id for {target.name!r}"
                )
            refs.append(
                {
                    "blob_id": blob_id,
                    "filename": target.name,
                    "mime_type": mime_type,
                    "size_bytes": len(plaintext),
                }
            )
        return refs, total_bytes

    def _keyless_legacy_result(
        self,
        raw: Any,
        destination: str,
        root_note: str,
        visibility_note: str,
        refs: list[dict[str, Any]],
        total_bytes: int,
    ) -> dict[str, Any]:
        envelope_id = raw.get("envelope_id") if isinstance(raw, Mapping) else None
        if not envelope_id:
            return failed_result(
                "keyless message send returned no envelope_id",
                kind="protocol",
            )
        seq = optional_response_int(raw, "seq", "keyless response")
        queued = optional_response_int(raw, "devices_queued", "keyless response")
        missing = raw.get("missing_devices", []) if isinstance(raw, Mapping) else []
        if not isinstance(missing, list) or not all(
            isinstance(v, str) for v in missing
        ):
            raise ValueError("keyless response has invalid missing_devices")
        replay = raw.get("replay") if isinstance(raw, Mapping) else None
        if replay is not None and not isinstance(replay, bool):
            raise ValueError("keyless response has invalid replay")
        attachment_note = (
            f"\nuploaded {len(refs)} file(s) ({total_bytes} bytes total)"
            if refs
            else ""
        )
        return SendResult(
            state="sent",
            envelope_id=str(envelope_id),
            seq=seq,
            replay=replay,
            devices_queued=queued,
            missing_devices=list(missing),
            note=(
                f"{'uploaded' if refs else 'posted'} {envelope_id} to "
                f"{destination}{root_note}{visibility_note}{attachment_note}"
            ),
        ).to_dict()

    async def _send_keyless_channel(
        self,
        request: SemanticSendRequest,
        space_id: str,
        channel_id: str,
    ) -> dict[str, Any]:
        boundary = await self._resolve_send_boundary(
            request,
            space_id,
            channel_id,
            combined_error=True,
        )
        if isinstance(boundary, dict):
            return boundary
        try:
            prepared = await self._prepare_keyless_channel_send(
                request,
                space_id,
                channel_id,
                boundary,
            )
            if isinstance(prepared, dict) and "error" in prepared:
                return prepared
            body, root_id, root_note, visible_draft_basis = prepared
            raw = await self._post_keyless_exact(body)
            result = self._validate_keyless_response(raw, body)
            return await self._finish_keyless_channel_send(
                request,
                space_id,
                channel_id,
                boundary,
                root_id,
                root_note,
                visible_draft_basis,
                result,
            )
        except HttpError as exc:
            return failed_result(
                http_error_detail(exc.body),
                kind="freshness_unavailable"
                if exc.status in (404, 405, 503)
                else "http",
                status=exc.status,
            )
        except Exception as exc:
            return failed_result(str(exc), kind="validation")

    async def _prepare_keyless_channel_send(
        self,
        request: SemanticSendRequest,
        space_id: str,
        channel_id: str,
        boundary: _ChannelSendBoundary,
    ) -> tuple[dict[str, Any], str, str, list[dict[str, Any]]] | dict[str, Any]:
        from ..mcp.puffo_core_tools import _resolve_outgoing_root
        from ._visibility import resolve_visibility

        root_id, root_note = await _resolve_outgoing_root(
            request.root_id,
            self.data_client,
            self_slug=self.slug,
            channel_id=channel_id,
            space_id=space_id,
            dm_peer=None,
        )
        visible, _ = await resolve_visibility(
            request.visibility_level,
            channel_id,
            request.caption if request.attachment_paths else request.text,
            root_id or "",
            self.http_client,
        )
        body: dict[str, Any] = {
            "client_ref": f"send_{uuid.uuid4().hex}",
            "space_id": space_id,
            "channel_id": channel_id,
            "plaintext": request.caption if request.attachment_paths else request.text,
            "is_visible_to_human": visible,
            "freshness": {
                "context_baseline_seq": boundary.baseline,
                "seen_seq": boundary.seen_seq,
                "mode": "send_anyway" if request.send_anyway else "require_current",
            },
        }
        if root_id:
            body["thread_root_id"] = root_id
            if request.root_id:
                body["reply_to_id"] = request.root_id
        refs = await self._upload_keyless_channel_attachments(
            request, boundary.materialized
        )
        if isinstance(refs, dict):
            return refs
        if refs:
            body["attachments"] = refs
        basis = await self._visible_draft_basis(space_id, channel_id, root_id or "")
        return body, root_id or "", root_note, basis

    async def _upload_keyless_channel_attachments(
        self,
        request: SemanticSendRequest,
        materialized: Sequence[tuple[str, bytes]] | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        # Same binding as the native lane: upload the digested bytes, not a
        # fresh read of the path.
        pairs = (
            list(materialized)
            if materialized is not None
            else [
                (target.name, target.read_bytes())
                for target in self._validate_attachment_targets(request)
            ]
        )
        refs: list[dict[str, Any]] = []
        for name, plaintext in pairs:
            upload = await self.http_client.post_bytes_unsigned(
                "/v2/cloud-agents/blobs/upload",
                plaintext,
            )
            blob_id = upload.get("blob_id") if isinstance(upload, Mapping) else None
            if not blob_id:
                return failed_result(
                    "keyless attachment upload returned no blob_id",
                    kind="protocol",
                )
            refs.append(
                {
                    "blob_id": blob_id,
                    "filename": name,
                    "mime_type": mimetypes.guess_type(name)[0]
                    or "application/octet-stream",
                    "size_bytes": len(plaintext),
                }
            )
        return refs

    async def _finish_keyless_channel_send(
        self,
        request: SemanticSendRequest,
        space_id: str,
        channel_id: str,
        boundary: _ChannelSendBoundary,
        root_id: str,
        root_note: str,
        visible_draft_basis: list[dict[str, Any]],
        result: SendResult,
    ) -> dict[str, Any]:
        synchronized = False
        if result.state == "held":
            if result.latest_seq is not None and result.latest_envelope_id:
                await self._record_held(
                    boundary.held_key,
                    result.latest_seq,
                    result.latest_envelope_id,
                    attempt_fingerprint=boundary.attempt_fingerprint,
                    draft=request.caption if request.attachment_paths else request.text,
                    based_on_through_seq=boundary.seen_seq,
                    thread_root_id=root_id,
                    visible_draft_basis=visible_draft_basis,
                    content_digest=boundary.content_digest,
                    held_generation=boundary.held_generation,
                )
            result.note = (
                "No channel message was committed; inspect newer Inbox context."
            )
            synchronized = await self._recover_held(
                space_id,
                channel_id,
                result.latest_seq,
                result.latest_envelope_id,
                boundary.attempt_fingerprint,
            )
        elif result.state == "sent":
            result.note = f"posted {result.envelope_id} to {channel_id}{root_note}"
            try:
                await self._consume_held(boundary.held_key)
            except Exception:
                logger.exception(
                    "sent keyless message but could not clear held state"
                )
            baseline_saved = await persist_baseline(
                self.baseline_source,
                space_id,
                channel_id,
                boundary.baseline,
                result.context_baseline_seq,
            )
            if not baseline_saved:
                result.note = f"{result.note} {BASELINE_PERSISTENCE_WARNING}"
            acknowledged = max(result.seen_seq or 0, result.context_baseline_seq or 0)
            if (
                result.latest_seq_before_send == acknowledged
                and result.seq is not None
            ):
                try:
                    await self._advance(space_id, channel_id, result.seq)
                except Exception:
                    logger.exception(
                        "sent keyless message but could not advance local boundary"
                    )
        output = result.to_dict()
        if result.state == "held":
            output["synchronized"] = synchronized
            output.update(
                await self._held_context_output(
                    boundary.held_key,
                    space_id,
                    channel_id,
                    boundary.attempt_fingerprint,
                )
            )
        if boundary.reconsideration is not None:
            output["_reconsideration_audit"] = boundary.reconsideration.audit_fields()
        return output

    async def _post_keyless_exact(self, body: dict[str, Any]) -> Any:
        for attempt in range(2):
            try:
                return await self.http_client.post_unsigned(
                    KEYLESS_CHANNEL_SEND_PATH, body
                )
            except HttpError:
                raise
            except (TimeoutError, ConnectionError, OSError):
                if attempt == 0:
                    continue
                return SendResult(
                    state="failed",
                    error="coordinated keyless send outcome is unknown",
                    error_kind="transport_unknown",
                )
        raise AssertionError("unreachable")

    def _validate_keyless_response(
        self, raw: Any, request_body: Mapping[str, Any]
    ) -> SendResult:
        return validate_keyless_response(raw, request_body)

    async def _baseline(self, space_id: str, channel_id: str) -> Any:
        return await _call_first(
            self.baseline_source,
            (
                "get_context_baseline_seq",
                "context_baseline_seq",
                "get_baseline",
                "baseline_for",
            ),
            space_id,
            channel_id,
        )

    async def _active_boundary(self, space_id: str, channel_id: str) -> Any:
        return await _call_first(
            self.active_turn_source,
            (
                "get_active_turn_through_seq",
                "active_turn_through_seq",
                "get_boundary",
                "boundary_for",
            ),
            space_id,
            channel_id,
        )

    async def _advance(self, space_id: str, channel_id: str, seq: int) -> None:
        await _call_first(
            self.active_turn_source,
            ("advance_active_turn_through_seq", "advance_boundary", "advance"),
            space_id,
            channel_id,
            seq,
        )

    async def _resolve_send_boundary(
        self,
        request: SemanticSendRequest,
        space_id: str,
        channel_id: str,
        *,
        combined_error: bool,
    ) -> _ChannelSendBoundary | dict[str, Any]:
        held_generation = self._held_generation  # read before any await
        baseline = await self._baseline(space_id, channel_id)
        baseline_invalid = baseline is not None and (
            isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 0
        )
        if baseline_invalid and not combined_error:
            return failed_result(
                "context baseline is invalid for channel send",
                kind="freshness_unavailable",
            )
        active = await self._active_boundary(space_id, channel_id)
        active_invalid = isinstance(active, bool) or (
            active is not None and (not isinstance(active, int) or active < 0)
        )
        if combined_error and (baseline_invalid or active_invalid):
            return failed_result(
                "channel freshness is unavailable",
                kind="freshness_unavailable",
            )
        if active_invalid:
            return failed_result(
                "active-turn boundary is invalid",
                kind="freshness_unavailable",
            )
        fingerprint = request.attempt_fingerprint()
        try:
            materialized = self._materialize_attachments(request)
        except Exception as exc:
            return failed_result(str(exc), kind="validation")
        content_digest = self._content_digest(materialized)
        reconsideration: _ReconsiderationDecision | None = None
        if request.send_anyway:
            reconsideration = await self._reconsideration_decision(
                space_id, channel_id, fingerprint, content_digest
            )
            if not reconsideration.eligible:
                if reconsideration.reason == "missing_active_identity":
                    # No admitted daemon turn is bound (background-task
                    # wakeup) or the provider session changed under the
                    # coordinator; either way there is no identity to
                    # validate catch-up against, and pointing at the normal
                    # held procedure would send the model in circles.
                    message = (
                        "this send is not bound to an admitted daemon turn "
                        "(background-task wakeup, or the provider session "
                        "changed); send_anyway is unavailable here — the "
                        "draft can be resent from a newly admitted turn"
                    )
                else:
                    message = (
                        "send_anyway requires exact held catch-up and an "
                        "admitted same-Turn read through that boundary"
                    )
                result = failed_result(
                    message,
                    kind="reconsideration_ineligible",
                )
                result["_reconsideration_audit"] = reconsideration.audit_fields()
                return result
            active = reconsideration.admitted_seq
        seen_seq = max(baseline or 0, active or 0)
        session_id, turn_id = self._turn_identity()
        return _ChannelSendBoundary(
            baseline=baseline,
            seen_seq=seen_seq,
            held_key=(session_id, turn_id, space_id, channel_id),
            attempt_fingerprint=fingerprint,
            reconsideration=reconsideration,
            held_generation=held_generation,
            materialized=materialized,
            content_digest=content_digest,
        )

    def _materialize_attachments(
        self,
        request: SemanticSendRequest,
    ) -> tuple[tuple[str, bytes], ...]:
        """Read every validated attachment once, before any authorization.

        ``attempt_fingerprint`` only covers the tool arguments, so a file
        mutated at an unchanged path used to reuse a held approval. Reading
        here — ahead of the reconsideration decision — makes the bytes that
        were approved the bytes that are uploaded.
        """
        return tuple(
            (target.name, target.read_bytes())
            for target in self._validate_attachment_targets(request)
        )

    @staticmethod
    def _content_digest(materialized: Sequence[tuple[str, bytes]]) -> str:
        """Order-sensitive digest over the (filename, bytes) pairs."""
        if not materialized:
            return ""
        digest = hashlib.sha256()
        for name, payload in materialized:
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(payload).digest())
        return digest.hexdigest()

    async def _send_channel(
        self,
        request: SemanticSendRequest,
        space_id: str,
        channel_id: str,
    ) -> dict[str, Any]:
        boundary = await self._resolve_send_boundary(
            request,
            space_id,
            channel_id,
            combined_error=False,
        )
        if isinstance(boundary, dict):
            return boundary
        encrypt = await self._channel_encrypt_policy(channel_id, space_id)
        try:
            resolved = await self._resolve_route_and_content(
                request,
                space_id=space_id,
                channel_id=channel_id,
                dm_peer=None,
                materialized=boundary.materialized,
                encrypt=encrypt,
            )
        except Exception as exc:
            return failed_result(str(exc), kind="validation")

        freshness = {
            "context_baseline_seq": boundary.baseline,
            "seen_seq": boundary.seen_seq,
            "mode": "send_anyway" if request.send_anyway else "require_current",
        }
        visible_draft_basis = await self._visible_draft_basis(
            space_id,
            channel_id,
            str(resolved.get("root_id") or request.root_id or ""),
        )
        for attempt in (0, 1):
            if encrypt:
                envelope, content_key = encrypt_message_with_content_key(
                    resolved["input"],
                    resolved["signing_key"],
                )
            else:
                envelope = build_plaintext_message(
                    resolved["input"], resolved["signing_key"]
                )
                content_key = b""
            body = {"envelope": envelope, "freshness": freshness}
            response = await self._post_channel_exact(body)
            result = self._validate_channel_response(response, envelope, freshness)
            if (
                attempt == 0
                and result.state == "failed"
                and result.error_kind == "channel_format_mismatch"
            ):
                refreshed = await self._refresh_channel_encrypt_policy(
                    channel_id, encrypt
                )
                if refreshed == encrypt:
                    break
                encrypt = refreshed
                try:
                    resolved = await self._resolve_route_and_content(
                        request,
                        space_id=space_id,
                        channel_id=channel_id,
                        dm_peer=None,
                        materialized=boundary.materialized,
                        encrypt=encrypt,
                        prepared=resolved["prepared_attachments"],
                    )
                except Exception as exc:
                    return failed_result(str(exc), kind="validation")
                continue
            break
        return await self._finish_channel_send(
            request,
            space_id,
            channel_id,
            boundary,
            resolved,
            envelope,
            content_key,
            freshness,
            visible_draft_basis,
            result,
        )

    async def _finish_channel_send(
        self,
        request: SemanticSendRequest,
        space_id: str,
        channel_id: str,
        boundary: _ChannelSendBoundary,
        resolved: Mapping[str, Any],
        envelope: Mapping[str, Any],
        content_key: bytes,
        freshness: Mapping[str, Any],
        visible_draft_basis: list[dict[str, Any]],
        result: SendResult,
    ) -> dict[str, Any]:
        action = "uploaded" if request.attachment_paths else "posted"
        if result.state == "sent":
            await self._finish_sent_channel_send(
                request,
                space_id,
                channel_id,
                boundary,
                resolved,
                envelope,
                content_key,
                freshness,
                result,
                action,
            )
        elif result.state == "held":
            await self._finish_held_channel_send(
                request,
                boundary,
                resolved,
                visible_draft_basis,
                result,
            )
        output = result.to_dict()
        if result.state == "held":
            # Native sends recover in ``_send_request``; return the immutable
            # draft/basis now and enrich it after recovery below.
            output.update(
                await self._held_context_output(
                    boundary.held_key,
                    space_id,
                    channel_id,
                    boundary.attempt_fingerprint,
                )
            )
        if boundary.reconsideration is not None:
            output["_reconsideration_audit"] = boundary.reconsideration.audit_fields()
        return output

    async def _finish_sent_channel_send(
        self,
        request: SemanticSendRequest,
        space_id: str,
        channel_id: str,
        boundary: _ChannelSendBoundary,
        resolved: Mapping[str, Any],
        envelope: Mapping[str, Any],
        content_key: bytes,
        freshness: Mapping[str, Any],
        result: SendResult,
        action: str,
    ) -> None:
        result.note = (
            f"{action} {envelope.get('envelope_id', '?')} to "
            f"{request.destination}{resolved['note']}"
        )
        try:
            await self._consume_held(boundary.held_key)
        except Exception:
            logger.exception("sent message but could not clear held state")
        # A stale send_anyway may cross messages this turn has not seen. The
        # outbound is visible, but the boundary advances only without a gap.
        baseline_saved = await persist_baseline(
            self.baseline_source,
            space_id,
            channel_id,
            boundary.baseline,
            result.context_baseline_seq,
        )
        if not baseline_saved:
            result.note = f"{result.note} {BASELINE_PERSISTENCE_WARNING}"
        acknowledged = max(result.seen_seq or 0, result.context_baseline_seq or 0)
        if result.latest_seq_before_send == acknowledged:
            try:
                await self._advance(
                    space_id,
                    channel_id,
                    result.seq,  # type: ignore[arg-type]
                )
            except Exception:
                logger.exception(
                    "sent message but could not advance local boundary"
                )
        if result.missing_devices:
            asyncio.create_task(
                self._supplement_channel(
                    envelope,
                    content_key,
                    resolved["recipient_slugs"],
                    result.missing_devices,
                    freshness,
                )
            )

    async def _finish_held_channel_send(
        self,
        request: SemanticSendRequest,
        boundary: _ChannelSendBoundary,
        resolved: Mapping[str, Any],
        visible_draft_basis: list[dict[str, Any]],
        result: SendResult,
    ) -> None:
        if result.latest_seq is not None and result.latest_envelope_id:
            await self._record_held(
                boundary.held_key,
                result.latest_seq,
                result.latest_envelope_id,
                attempt_fingerprint=boundary.attempt_fingerprint,
                draft=request.caption if request.attachment_paths else request.text,
                based_on_through_seq=boundary.seen_seq,
                thread_root_id=str(resolved.get("root_id") or request.root_id or ""),
                visible_draft_basis=visible_draft_basis,
                content_digest=boundary.content_digest,
                held_generation=boundary.held_generation,
            )
        result.note = (
            "No message was sent because the channel advanced beyond this "
            "turn's visible boundary. Newer context is returned when "
            "available. If that context can change the draft's correctness, "
            "position, necessity, target, or interpretation, revise and retry "
            "with normal freshness. Use send_anyway only for an unchanged, "
            f"context-independent draft, or leave it unsent.{resolved['note']}"
        )

    async def _channel_encrypt_policy(self, channel_id: str, space_id: str) -> bool:
        source = self.channel_policy_source
        if source is None:
            return True
        try:
            return bool(await source.ensure_channel_policy(channel_id, space_id))
        except Exception:
            logger.exception("channel policy lookup failed; defaulting to encrypted")
            return True

    async def _refresh_channel_encrypt_policy(
        self, channel_id: str, current: bool
    ) -> bool:
        source = self.channel_policy_source
        if source is None:
            return current
        try:
            return bool(await source.refresh_channel_policy(channel_id))
        except Exception:
            logger.exception("channel policy refresh failed")
            return current

    async def _post_channel_exact(self, body: dict[str, Any]) -> Any:
        # The same object is deliberately reused after an uncertain outcome; the
        # signed client serializes it deterministically with json.dumps.
        for attempt in range(2):
            try:
                return await self.http_client.post(CHANNEL_SEND_PATH, body)
            except HttpError as exc:
                if is_channel_format_mismatch(exc):
                    return SendResult(
                        state="failed",
                        error=http_error_detail(exc.body),
                        error_kind="channel_format_mismatch",
                        status=exc.status,
                    )
                kind = (
                    "deployment"
                    if exc.status in (404, 405)
                    else "protocol"
                    if exc.status < 400
                    else "http"
                )
                detail = http_error_detail(exc.body)
                return SendResult(
                    state="failed",
                    error=detail,
                    error_kind=kind,
                    status=exc.status,
                )
            except (TimeoutError, ConnectionError, OSError) as exc:
                if attempt == 0:
                    continue
                return SendResult(
                    state="failed",
                    error=str(exc),
                    error_kind="transport_unknown",
                )
            except Exception as exc:
                # aiohttp and test transports need not share a concrete base type.
                if attempt == 0 and exc.__class__.__name__ in {
                    "ClientError",
                    "ServerDisconnectedError",
                    "ClientConnectionError",
                }:
                    continue
                return SendResult(
                    state="failed", error=str(exc), error_kind="transport"
                )
        raise AssertionError("unreachable")

    def _validate_channel_response(
        self,
        raw: Any,
        envelope: Mapping[str, Any],
        freshness: Mapping[str, Any],
    ) -> SendResult:
        return validate_channel_response(raw, envelope, freshness)

    async def _send_dm(
        self, request: SemanticSendRequest, recipient_slug: str
    ) -> dict[str, Any]:
        if not recipient_slug:
            return failed_result(
                "DM recipient slug is required after '@'", kind="validation"
            )
        try:
            resolved = await self._resolve_route_and_content(
                request,
                space_id=None,
                channel_id=None,
                dm_peer=recipient_slug,
            )
            inp = resolved["input"]
            envelope, content_key = encrypt_message_with_content_key(
                inp,
                resolved["signing_key"],
            )
            raw = await self.http_client.post("/messages", envelope) or {}
            metadata = self._legacy_dm_metadata(raw)
            missing = metadata[3]
            if missing:
                from ..mcp.puffo_core_tools import _supplement_missing_devices

                asyncio.create_task(
                    _supplement_missing_devices(
                        self.http_client,
                        envelope,
                        content_key,
                        resolved["recipient_slugs"],
                        list(missing),
                    )
                )
            return SendResult(
                state="sent",
                envelope_id=envelope.get("envelope_id"),
                seq=metadata[0],
                replay=metadata[1],
                devices_queued=metadata[2],
                missing_devices=metadata[3],
                note=(
                    f"{'uploaded' if request.attachment_paths else 'posted'} "
                    f"{envelope.get('envelope_id', '?')} to {request.destination}"
                    f"{resolved['note']}"
                ),
            ).to_dict()
        except HttpError as exc:
            return SendResult(
                state="failed",
                error=http_error_detail(exc.body),
                error_kind="http",
                status=exc.status,
            ).to_dict()
        except Exception as exc:
            return failed_result(str(exc), kind="validation")

    @staticmethod
    def _legacy_dm_metadata(
        raw: Any,
    ) -> tuple[int | None, bool | None, int | None, list[str]]:
        """Validate optional legacy-DM commit metadata without requiring it."""
        if not isinstance(raw, Mapping):
            return None, None, None, []

        def optional_int(name: str) -> int | None:
            value = raw.get(name)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"DM response has invalid {name}")
            return value

        replay = raw.get("replay")
        if replay is not None and not isinstance(replay, bool):
            raise ValueError("DM response has invalid replay")
        missing = raw.get("missing_devices", [])
        if not isinstance(missing, list) or not all(
            isinstance(value, str) for value in missing
        ):
            raise ValueError("DM response has invalid missing_devices")
        return (
            optional_int("seq"),
            replay,
            optional_int("devices_queued"),
            list(missing),
        )

    async def _channel_recipient_slugs(
        self, space_id: str | None, channel_id: str
    ) -> list[str]:
        quoted_space_id = urllib.parse.quote(str(space_id or ""), safe="")
        quoted_channel_id = urllib.parse.quote(channel_id, safe="")
        members = await self.http_client.get(
            f"/spaces/{quoted_space_id}/channels/{quoted_channel_id}/members"
        )
        recipient_slugs = [
            row.get("slug")
            for row in (members or {}).get("members", [])
            if isinstance(row, Mapping) and row.get("slug")
        ]
        if not recipient_slugs:
            raise RuntimeError(f"channel {channel_id} has no resolvable members")
        return recipient_slugs

    async def _resolve_send_recipients(
        self, *, space_id: str | None, channel_id: str | None,
        dm_peer: str | None, encrypt: bool
    ) -> tuple[list[str], str]:
        if channel_id is not None:
            # Members feed device wraps + supplementation: encrypted only.
            if not encrypt:
                return [], "channel"
            slugs = await self._channel_recipient_slugs(space_id, channel_id)
            return slugs, "channel"
        return [self.slug, dm_peer], "dm"

    async def _resolve_send_devices(
        self,
        kind: str,
        dm_peer: str | None,
        recipient_slugs: list[str],
        encrypt: bool,
    ) -> list[Any]:
        from ..mcp.puffo_core_tools import _fetch_device_keys

        if kind == "dm":
            # Fetch the peer alone: a combined [self, peer] fetch cannot
            # tell "peer unreachable" from "reachable" once the sender's
            # own devices make the list non-empty.
            peer_devices = await _fetch_device_keys(self.http_client, [dm_peer])
            if not peer_devices:
                raise RuntimeError(
                    f"recipient @{dm_peer} has no encryption devices"
                )
            self_devices = await _fetch_device_keys(self.http_client, [self.slug])
            merged: dict[str, Any] = {}
            for device in (*peer_devices, *self_devices):
                merged.setdefault(device.device_id, device)
            return list(merged.values())
        if encrypt:
            devices = await _fetch_device_keys(self.http_client, recipient_slugs)
            if not devices:
                raise RuntimeError("no recipient devices found")
            return devices
        # Plaintext channel send: no per-device key wrap.
        return []

    async def _resolve_route_and_content(
        self,
        request: SemanticSendRequest,
        *,
        space_id: str | None,
        channel_id: str | None,
        dm_peer: str | None,
        materialized: Sequence[tuple[str, bytes]] | None = None,
        encrypt: bool = True,
        prepared: tuple[list[AttachmentMeta], str] | None = None,
    ) -> dict[str, Any]:
        from ..mcp.puffo_core_tools import _resolve_outgoing_root
        from ._visibility import resolve_visibility

        destination = request.destination.strip()
        recipient_slugs, kind = await self._resolve_send_recipients(
            space_id=space_id,
            channel_id=channel_id,
            dm_peer=dm_peer,
            encrypt=encrypt,
        )

        root, root_note = await _resolve_outgoing_root(
            request.root_id,
            self.data_client,
            self_slug=self.slug,
            channel_id=channel_id,
            space_id=space_id,
            dm_peer=dm_peer,
        )
        # Format retry reuses the already-uploaded blobs.
        attachments, attachment_note = (
            prepared
            if prepared is not None
            else await self._prepare_attachments(request, materialized)
        )
        content: Any
        content_type: str
        visible_text = request.caption if request.attachment_paths else request.text
        if request.attachment_paths:
            content = {
                "text": request.caption,
                "attachments": [m.to_dict() for m in attachments],
            }
            content_type = ATTACHMENT_CONTENT_TYPE
        else:
            content = request.text
            content_type = "text/plain"
        devices = await self._resolve_send_devices(
            kind, dm_peer, recipient_slugs, encrypt
        )
        visible, visibility_note = await resolve_visibility(
            request.visibility_level,
            destination,
            visible_text,
            root or "",
            self.http_client,
        )
        sess = self.keystore.load_session(self.slug)
        signing_key = Ed25519KeyPair.from_secret_bytes(
            decode_secret(sess.subkey_secret_key)
        )
        inp = EncryptInput(
            envelope_kind=kind,
            sender_slug=self.slug,
            sender_subkey_id=sess.subkey_id,
            is_visible_to_human=visible,
            space_id=space_id,
            channel_id=channel_id,
            recipient_slug=dm_peer,
            thread_root_id=root,
            reply_to_id=request.root_id if root and request.root_id else None,
            content_type=content_type,
            content=content,
            recipients=devices,
        )
        return {
            "input": inp,
            "signing_key": signing_key,
            "recipient_slugs": recipient_slugs,
            "root_id": root or "",
            "note": f"{visibility_note}{root_note}{attachment_note}",
            "prepared_attachments": (attachments, attachment_note),
        }

    async def _prepare_attachments(
        self,
        request: SemanticSendRequest,
        materialized: Sequence[tuple[str, bytes]] | None = None,
    ) -> tuple[list[AttachmentMeta], str]:
        if not request.attachment_paths:
            return [], ""
        # Reuse the bytes the boundary already read and digested; re-reading
        # would reopen the mutate-between-approval-and-upload window.
        pairs = (
            list(materialized)
            if materialized is not None
            else [
                (target.name, target.read_bytes())
                for target in self._validate_attachment_targets(request)
            ]
        )
        metas: list[AttachmentMeta] = []
        total = 0
        for name, plaintext in pairs:
            mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            ciphertext, meta = encrypt_attachment(
                plaintext=plaintext,
                filename=name,
                mime_type=mime_type,
                blob_id="",
            )
            upload = await self.http_client.post_bytes("/blobs/upload", ciphertext)
            blob_id = upload.get("blob_id") if isinstance(upload, Mapping) else None
            if not blob_id:
                raise RuntimeError(f"server returned no blob_id for {name!r}")
            meta.blob_id = blob_id
            metas.append(meta)
            total += len(plaintext)
        names = ", ".join(name for name, _ in pairs)
        return (
            metas,
            f"\nuploaded {len(pairs)} file(s) [{names}] ({total} bytes total)",
        )

    def _validate_attachment_targets(
        self,
        request: SemanticSendRequest,
    ) -> list[Path]:
        if not request.attachment_paths:
            return []
        if not self.workspace:
            raise RuntimeError(
                "send_message_with_attachments: agent has no configured workspace dir"
            )
        if len(request.attachment_paths) > 10:
            raise RuntimeError(
                "send_message_with_attachments: too many files (> 10 cap)"
            )
        workspace = Path(self.workspace).resolve()
        shared_workspace = (
            Path(self.shared_workspace).resolve() if self.shared_workspace else None
        )
        targets: list[Path] = []
        for raw in request.attachment_paths:
            rel = raw.strip()
            if not rel:
                raise RuntimeError(
                    "send_message_with_attachments: paths contains empty entry"
                )
            rel_path = Path(rel)
            if rel_path.is_absolute():
                raise RuntimeError(f"absolute paths not allowed ({rel!r})")
            target = (workspace / rel_path).resolve()
            inside_workspace = target.is_relative_to(workspace)
            inside_managed_shared = bool(
                shared_workspace
                and rel_path.parts
                and rel_path.parts[0] == "shared"
                and target.is_relative_to(shared_workspace)
            )
            if not (inside_workspace or inside_managed_shared):
                raise RuntimeError(f"{rel!r} escapes the workspace")
            if not target.is_file():
                raise RuntimeError(f"{rel!r} is not a file")
            if target.stat().st_size > 8 * 1024 * 1024:
                raise RuntimeError(f"{target.name!r} exceeds the 8 MiB cap")
            targets.append(target)
        return targets

    async def _supplement_channel(
        self,
        envelope: dict[str, Any],
        content_key: bytes,
        recipient_slugs: list[str],
        missing_ids: list[str],
        freshness: dict[str, Any],
    ) -> None:
        try:
            from ..mcp.puffo_core_tools import _fetch_device_keys

            fresh = await _fetch_device_keys(self.http_client, recipient_slugs)
            wanted = set(missing_ids)
            devices = [device for device in fresh if device.device_id in wanted]
            if not devices:
                return
            supplement = build_supplementation_envelope(envelope, content_key, devices)
            response = await self.http_client.post(
                CHANNEL_SEND_PATH,
                {"envelope": supplement, "freshness": freshness},
            )
            checked = self._validate_channel_response(response, supplement, freshness)
            if checked.state == "failed":
                logger.warning("channel supplementation rejected: %s", checked.error)
        except Exception as exc:
            logger.warning("channel supplementation failed: %s", exc)

    async def _record_held(
        self,
        key: tuple[str, str, str, str],
        latest_seq: int,
        latest_envelope_id: str,
        *,
        attempt_fingerprint: str,
        draft: str = "",
        based_on_through_seq: int | None = None,
        thread_root_id: str = "",
        visible_draft_basis: Sequence[Mapping[str, Any]] = (),
        content_digest: str = "",
        held_generation: int = 0,
    ) -> None:
        async with self._held_lock:
            if held_generation != self._held_generation:
                return  # the owning turn tore down while this send was in flight
            old = self._held_evidence.get(key)
            # The key is per turn/channel, so two different drafts can be held
            # against one Server head. The newest attempt owns the record; the
            # superseded draft's plaintext rows are dropped with it.
            if (
                old is None
                or latest_seq > old.latest_seq
                or (
                    latest_seq == old.latest_seq
                    and latest_envelope_id != old.latest_envelope_id
                )
                or attempt_fingerprint != old.attempt_fingerprint
            ):
                self._held_evidence.pop(key, None)
                self._held_evidence[key] = _HeldEvidence(
                    latest_seq=latest_seq,
                    latest_envelope_id=latest_envelope_id,
                    attempt_fingerprint=attempt_fingerprint,
                    content_digest=content_digest,
                    draft=draft,
                    based_on_through_seq=based_on_through_seq,
                    thread_root_id=thread_root_id,
                    visible_draft_basis=[dict(row) for row in visible_draft_basis],
                )
            self._prune_stale_held_locked()

    def _prune_stale_held_locked(self) -> None:
        """Bound decrypted held context; call under ``_held_lock``."""
        session_id, turn_id = self._turn_identity()
        if session_id and turn_id:
            # Other channels held by the *current* turn stay valid; an
            # abandoned or finished turn's rows are dropped. An incomplete
            # identity proves nothing, so only the cap applies then.
            for stale in [
                key
                for key in self._held_evidence
                if (key[0], key[1]) != (session_id, turn_id)
            ]:
                self._held_evidence.pop(stale, None)
        while len(self._held_evidence) > _MAX_HELD_RECORDS:
            self._held_evidence.pop(next(iter(self._held_evidence)), None)

    async def _held_context_output(
        self,
        key: tuple[str, str, str, str],
        space_id: str,
        channel_id: str,
        attempt_fingerprint: str = "",
    ) -> dict[str, Any]:
        # Validate before the network read, then validate again afterwards so
        # turn teardown cannot return decrypted held evidence after discarding
        # the owning record.
        async with self._held_lock:
            held = self._held_evidence.get(key)
            if held is None or held.attempt_fingerprint != attempt_fingerprint:
                return {
                    "context_version": CONTEXT_VERSION,
                    "context_ready": False,
                }

        channel_members = None
        try:
            from ..mcp.puffo_core_tools import _read_channel_members

            data = await _read_channel_members(
                coordinator_config(self), space_id, channel_id
            )
            members = data.get("members") if isinstance(data, Mapping) else None
            if isinstance(members, list):
                channel_members = [
                    dict(member) for member in members if isinstance(member, Mapping)
                ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("held channel membership unavailable: %s", exc)

        async with self._held_lock:
            held = self._held_evidence.get(key)
            # A record superseded by another draft at the same head must never
            # answer this attempt with the other draft's context.
            if held is None or held.attempt_fingerprint != attempt_fingerprint:
                return {
                    "context_version": CONTEXT_VERSION,
                    "context_ready": False,
                }
            return build_held_context_output(
                held=held,
                current_agent_slug=self.slug,
                space_id=space_id,
                channel_id=channel_id,
                guidance=HELD_SEND_RECONSIDERATION_GUIDANCE,
                channel_members=channel_members,
            )

    def discard_held_evidence(self) -> None:
        """Drop every held record when the owning turn tears down.

        The terminal paths are synchronous but do not wait for the turn's
        sends: ``send_message`` runs in the RPC/ws-local handler task, which
        cancelling the turn task does not reach, so one can still be awaiting
        its transport.  Bumping the generation makes its later ``_record_held``
        drop that record instead of repopulating the map behind this clear.
        """
        self._held_generation += 1
        self._held_evidence.clear()

    async def _consume_held(self, key: tuple[str, str, str, str]) -> None:
        async with self._held_lock:
            self._held_evidence.pop(key, None)
            self._prune_stale_held_locked()

    async def _visible_draft_basis(
        self,
        space_id: str,
        channel_id: str,
        thread_root_id: str,
    ) -> list[dict[str, Any]]:
        """Snapshot only rows visible before transport for this destination."""
        runtime = getattr(self.held_recovery_source, "runtime", None)
        active = getattr(runtime, "active", None)
        store = getattr(runtime, "store", None)
        if active is None or store is None:
            return []
        rows: list[dict[str, Any]] = []
        for envelope_id in tuple(getattr(active, "visible_message_ids", ())):
            row = await store.get_message_by_envelope(envelope_id)
            if row is None or row.space_id != space_id or row.channel_id != channel_id:
                continue
            if thread_root_id:
                if (
                    row.envelope_id != thread_root_id
                    and row.thread_root_id != thread_root_id
                ):
                    continue
            elif row.thread_root_id:
                continue
            rows.append(
                {
                    "space_id": row.space_id,
                    "channel_id": row.channel_id,
                    "thread_root_id": row.thread_root_id or "",
                    "envelope_id": row.envelope_id,
                    "server_seq": row.server_seq,
                    "sender_slug": row.sender_slug,
                    "envelope_kind": row.envelope_kind,
                    "sent_at": row.sent_at,
                    "is_encrypted": row.is_encrypted,
                    "content": row.content,
                }
            )
        return rows

    def _held_snapshot_locked(
        self, key: tuple[str, str, str, str]
    ) -> tuple[tuple[int, str] | None, str, str, bool]:
        """Snapshot the held record; call under ``_held_lock``."""
        held = self._held_evidence.get(key)
        if held is None:
            return None, "", "", False
        return (
            (held.latest_seq, held.latest_envelope_id),
            held.attempt_fingerprint,
            held.content_digest,
            bool(held.synchronized),
        )

    @staticmethod
    def _held_draft_reason(
        held_fingerprint: str,
        held_digest: str,
        attempt_fingerprint: str,
        content_digest: str,
    ) -> str:
        """Reject anything but the exact draft that was actually held."""
        if held_fingerprint != attempt_fingerprint:
            # Only the unchanged draft the model actually reconsidered may
            # override; a revised draft goes through normal freshness.
            return "held_draft_mismatch"
        if held_digest and held_digest != content_digest:
            # Same path and caption, different bytes: the tool arguments match
            # but the payload does not, so the old approval does not carry.
            return "held_content_changed"
        return ""

    @staticmethod
    def _normalize_admitted(admitted: Any) -> int | None:
        if isinstance(admitted, bool) or (
            admitted is not None and (not isinstance(admitted, int) or admitted < 0)
        ):
            return None
        return admitted

    async def _reconsideration_decision(
        self,
        space_id: str,
        channel_id: str,
        attempt_fingerprint: str,
        content_digest: str = "",
    ) -> _ReconsiderationDecision:
        session_id, turn_id = self._turn_identity()
        if not session_id or not turn_id:
            return _ReconsiderationDecision(
                False, "missing_active_identity", session_id, turn_id
            )
        key = (session_id, turn_id, space_id, channel_id)
        async with self._held_lock:
            held_pair, held_fingerprint, held_digest, synchronized = (
                self._held_snapshot_locked(key)
            )
        if held_pair is None:
            return _ReconsiderationDecision(
                False, "missing_held_evidence", session_id, turn_id
            )
        reason = self._held_draft_reason(
            held_fingerprint, held_digest, attempt_fingerprint, content_digest
        )
        if reason:
            return _ReconsiderationDecision(False, reason, session_id, turn_id)
        if not synchronized:
            await self._recover_held(
                space_id,
                channel_id,
                held_pair[0],
                held_pair[1],
                attempt_fingerprint,
            )
        async with self._held_lock:
            current_pair, current_fingerprint, current_digest, synchronized = (
                self._held_snapshot_locked(key)
            )
        if current_pair is None:
            return _ReconsiderationDecision(
                False, "missing_held_evidence", session_id, turn_id
            )
        reason = self._held_draft_reason(
            current_fingerprint, current_digest, attempt_fingerprint, content_digest
        )
        if reason:
            return _ReconsiderationDecision(False, reason, session_id, turn_id)
        return self._admission_verdict(
            session_id=session_id,
            turn_id=turn_id,
            synchronized=synchronized,
            current_pair=current_pair,
            held_pair=held_pair,
            admitted=self._normalize_admitted(
                await self._active_boundary(space_id, channel_id)
            ),
        )

    def _admission_verdict(
        self,
        *,
        session_id: str,
        turn_id: str,
        synchronized: bool,
        current_pair: tuple[int, str],
        held_pair: tuple[int, str],
        admitted: int | None,
    ) -> _ReconsiderationDecision:
        """Grade the recovered held record against the admitted boundary."""
        latest_seq, latest_envelope_id = current_pair
        if not synchronized:
            reason = (
                "held_boundary_superseded"
                if (latest_seq, latest_envelope_id) != held_pair
                else "held_not_synchronized"
            )
            return _ReconsiderationDecision(
                False,
                reason,
                session_id,
                turn_id,
                latest_seq,
                latest_envelope_id,
                admitted,
            )
        if admitted is None:
            return _ReconsiderationDecision(
                False,
                "admission_unavailable",
                session_id,
                turn_id,
                latest_seq,
                latest_envelope_id,
            )
        if admitted < latest_seq:
            return _ReconsiderationDecision(
                False,
                "admission_before_held",
                session_id,
                turn_id,
                latest_seq,
                latest_envelope_id,
                admitted,
            )
        if self._turn_identity() != (session_id, turn_id):
            return _ReconsiderationDecision(
                False,
                "active_identity_changed",
                session_id,
                turn_id,
                latest_seq,
                latest_envelope_id,
                admitted,
            )
        return _ReconsiderationDecision(
            True,
            "synchronized_and_admitted",
            session_id,
            turn_id,
            latest_seq,
            latest_envelope_id,
            admitted,
        )

    async def _recover_held(
        self,
        space_id: str,
        channel_id: str,
        latest_seq: int | None,
        latest_envelope_id: str | None,
        attempt_fingerprint: str = "",
    ) -> bool:
        attempt = await self._begin_held_recovery(
            space_id,
            channel_id,
            latest_seq,
            latest_envelope_id,
            attempt_fingerprint,
        )
        if attempt is None or not await self._wait_for_held_delivery(attempt):
            return False
        if not await self._held_evidence_matches(attempt):
            return False
        rows = await self._query_held_recovery_rows(attempt)
        if rows is None:
            return False
        # Recovery proves freshness for the whole channel, not merely the
        # destination thread.  Keep the bounded channel range intact so the
        # exact Server terminal pair remains inspectable even if it belongs to
        # another thread.  Target filtering belongs only in
        # ``_visible_draft_basis`` above.
        if not self._held_rows_are_synchronized(rows, attempt):
            return False
        if self._turn_identity() != (attempt.session_id, attempt.turn_id):
            return False
        return await self._commit_held_recovery(attempt, rows)

    async def _begin_held_recovery(
        self,
        space_id: str,
        channel_id: str,
        latest_seq: int | None,
        latest_envelope_id: str | None,
        attempt_fingerprint: str = "",
    ) -> _HeldRecoveryAttempt | None:
        if (
            latest_seq is None
            or not latest_envelope_id
            or self.held_recovery_source is None
        ):
            return None
        session_id, turn_id = self._turn_identity()
        if not session_id or not turn_id:
            return None
        attempt = _HeldRecoveryAttempt(
            key=(session_id, turn_id, space_id, channel_id),
            session_id=session_id,
            turn_id=turn_id,
            space_id=space_id,
            channel_id=channel_id,
            latest_seq=latest_seq,
            latest_envelope_id=latest_envelope_id,
            attempt_fingerprint=attempt_fingerprint,
        )
        if not await self._held_evidence_matches(attempt):
            return None
        return attempt

    async def _held_evidence_matches(self, attempt: _HeldRecoveryAttempt) -> bool:
        async with self._held_lock:
            current = self._held_evidence.get(attempt.key)
            return bool(
                current is not None
                and current.latest_seq == attempt.latest_seq
                and current.latest_envelope_id == attempt.latest_envelope_id
                and current.attempt_fingerprint == attempt.attempt_fingerprint
            )

    async def _wait_for_held_delivery(
        self,
        attempt: _HeldRecoveryAttempt,
    ) -> bool:
        try:
            waited = await _call_first(
                self.held_recovery_source,
                ("wait_for_held_delivery", "wait_for_delivery", "wait"),
                attempt.space_id,
                attempt.channel_id,
                attempt.latest_seq,
                attempt.latest_envelope_id,
            )
        except Exception:
            return False
        return waited is True

    async def _query_held_recovery_rows(
        self,
        attempt: _HeldRecoveryAttempt,
    ) -> list[Mapping[str, Any]] | None:
        if self.provider_session_id is None:
            return None
        try:
            rows = await _call_first(
                self.held_recovery_source,
                ("query_held_messages", "query_recovered_messages", "query"),
                attempt.space_id,
                attempt.channel_id,
                attempt.latest_seq,
                attempt.latest_envelope_id,
                self.provider_session_id,
            )
        except Exception:
            return None
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return None
        return [row for row in rows if isinstance(row, Mapping)]

    @staticmethod
    def _held_rows_are_synchronized(
        rows: Sequence[Mapping[str, Any]],
        attempt: _HeldRecoveryAttempt,
    ) -> bool:
        return any(
            row.get("envelope_id") == attempt.latest_envelope_id
            and row.get("server_seq") == attempt.latest_seq
            and row.get("latest_seq") == attempt.latest_seq
            and row.get("latest_envelope_id") == attempt.latest_envelope_id
            and row.get("provider_session_id") == attempt.session_id
            for row in rows
        )

    async def _commit_held_recovery(
        self,
        attempt: _HeldRecoveryAttempt,
        rows: Sequence[Mapping[str, Any]],
    ) -> bool:
        # Exact-pair compare-and-set: a stale recovery completion cannot bless
        # a superseding held head, a superseding draft at the same head, or
        # resurrect consumed evidence.
        async with self._held_lock:
            current = self._held_evidence.get(attempt.key)
            if (
                current is None
                or current.latest_seq != attempt.latest_seq
                or current.latest_envelope_id != attempt.latest_envelope_id
                or current.attempt_fingerprint != attempt.attempt_fingerprint
            ):
                return False
            current.synchronized = True
            # Only locally returned rows with a full plaintext projection are
            # model evidence. The metadata-only sentinel remains sufficient for
            # old callers' synchronization proof but never claims readiness.
            current.recovered_messages = [dict(row) for row in rows if "content" in row]
            if not current.recovered_messages:
                current.diagnostic = (
                    "freshness synchronized; no new model-visible channel messages"
                )
        return True
