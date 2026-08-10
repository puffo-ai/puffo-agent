"""Encrypted, local-first v1 synchronization for one-shot reminders.

The Server sees only immutable occurrence metadata and one opaque Agent-owned
envelope.  This module deliberately keeps crypto, HTTP, retry, and snapshot
decisions outside MCP tools and ``GlobalInboxRuntime`` turn policy.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from ..crypto.http_client import HttpError, PuffoCoreHttpClient
from ..crypto.keystore import KeyStore
from ..crypto.primitives import aead_decrypt, aead_encrypt
from .message_store import (
    LifecycleConflict,
    MessageStore,
    ReminderSyncRecord,
    parse_reminder_target,
    reminder_time_to_rfc3339,
)
from .reminder_scheduler import (
    ReminderDeliveryAuthorization,
    ReminderScheduler,
)

logger = logging.getLogger(__name__)

# This token is intentionally an Agent/Server transport format rather than a
# key-derivation label.  The one MessageBackupDEK is used directly with AEAD.
REMINDER_PAYLOAD_FORMAT = "puffo-reminder-aead-v1"
ENVELOPE_VERSION = 1
ENVELOPE_ALGORITHM = "chacha20-poly1305"
MAX_ENVELOPE_BYTES = 32_768
SNAPSHOT_PAGE_LIMIT = 100
MAX_SNAPSHOT_PAGES = 10_000
RETRY_BASE_MS = 1_000
RETRY_CAP_MS = 60_000
DELIVERY_CLAIM_RETRY_MS = 5_000
SYNC_IDLE_SECONDS = 30.0

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_RFC3339_OFFSET_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class ReminderEnvelopeError(ValueError):
    """A safe, payload-free envelope validation failure."""


class ReminderContractError(ValueError):
    """A safe, payload-free Server DTO validation failure."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: object) -> bytes:
    if not isinstance(value, str) or not value or not _B64URL_RE.fullmatch(value):
        raise ReminderEnvelopeError("invalid opaque envelope")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        # ``binascii.Error`` is raised for some impossible base64 lengths;
        # normalize every decoder implementation error to the same safe
        # envelope failure rather than exposing decoder details upstream.
        raise ReminderEnvelopeError("invalid opaque envelope") from exc
    if _base64url_encode(decoded) != value:
        raise ReminderEnvelopeError("invalid opaque envelope")
    return decoded


def _validate_id(value: object) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ReminderContractError("invalid reminder metadata")
    return value


def _validate_owner(value: object) -> str:
    # Owner slugs share the Server's conservative ASCII identifier shape but
    # are not placed in either request namespace/body.
    return _validate_id(value)


def _canonical_utc(value_ms: int) -> str:
    if isinstance(value_ms, bool) or not isinstance(value_ms, int):
        raise ReminderContractError("invalid reminder metadata")
    try:
        return reminder_time_to_rfc3339(value_ms)
    except (OverflowError, OSError, ValueError) as exc:
        raise ReminderContractError("invalid reminder metadata") from exc


def _parse_rfc3339_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not _RFC3339_OFFSET_RE.fullmatch(value):
        raise ReminderContractError("invalid reminder metadata")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as exc:
        raise ReminderContractError("invalid reminder metadata") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReminderContractError("invalid reminder metadata")
    return parsed


def _parse_rfc3339_ms(value: object) -> int:
    parsed = _parse_rfc3339_timestamp(value)
    if parsed.microsecond % 1000:
        # The Agent's v1 AAD canonically binds millisecond UTC instants.  Do
        # not silently round a Server row into a different authenticated time.
        raise ReminderContractError("invalid reminder metadata")
    try:
        return int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    except (OverflowError, OSError, ValueError) as exc:
        raise ReminderContractError("invalid reminder metadata") from exc


def _envelope_aad(
    *,
    owner_slug: str,
    reminder_id: str,
    occurrence_id: str,
    intended_at_ms: int,
    envelope_version: int = ENVELOPE_VERSION,
) -> bytes:
    """Canonical AAD binds an otherwise opaque payload to its owner/intent."""
    if envelope_version != ENVELOPE_VERSION:
        raise ReminderEnvelopeError("invalid opaque envelope")
    return _canonical_json({
        "envelope_version": envelope_version,
        "intended_at": _canonical_utc(intended_at_ms),
        "occurrence_id": _validate_id(occurrence_id),
        "owner_slug": _validate_owner(owner_slug),
        "reminder_id": _validate_id(reminder_id),
    })


def encrypt_reminder_payload(
    *,
    dek: bytes,
    owner_slug: str,
    reminder_id: str,
    occurrence_id: str,
    intended_at_ms: int,
    target: str,
    content: str,
) -> bytes:
    """Build a fresh-nonce, canonical v1 envelope for one occurrence.

    Callers persist this return value once.  Retrying an occurrence must use
    the persisted bytes rather than call this function again.
    """
    if not isinstance(dek, bytes) or len(dek) != 32:
        raise ReminderEnvelopeError("invalid reminder encryption key")
    if not isinstance(target, str) or not target or not isinstance(content, str) or not content:
        raise ReminderEnvelopeError("invalid reminder plaintext")
    try:
        parse_reminder_target(target)
    except Exception as exc:
        raise ReminderEnvelopeError("invalid reminder plaintext") from exc
    aad = _envelope_aad(
        owner_slug=owner_slug,
        reminder_id=reminder_id,
        occurrence_id=occurrence_id,
        intended_at_ms=intended_at_ms,
    )
    nonce = os.urandom(12)
    ciphertext = aead_encrypt(
        dek,
        nonce,
        _canonical_json({"content": content, "target": target}),
        aad,
    )
    envelope = _canonical_json({
        "algorithm": ENVELOPE_ALGORITHM,
        "ciphertext": _base64url_encode(ciphertext),
        "nonce": _base64url_encode(nonce),
        "version": ENVELOPE_VERSION,
    })
    if len(envelope) > MAX_ENVELOPE_BYTES:
        raise ReminderEnvelopeError("reminder envelope exceeds v1 limit")
    return envelope


def decrypt_reminder_payload(
    *,
    dek: bytes,
    owner_slug: str,
    reminder_id: str,
    occurrence_id: str,
    intended_at_ms: int,
    envelope: bytes,
) -> tuple[str, str]:
    """Strictly validate/decrypt one v1 envelope without exposing plaintext."""
    try:
        if not isinstance(dek, bytes) or len(dek) != 32:
            raise ReminderEnvelopeError("invalid opaque envelope")
        if not isinstance(envelope, bytes) or not 1 <= len(envelope) <= MAX_ENVELOPE_BYTES:
            raise ReminderEnvelopeError("invalid opaque envelope")
        decoded = json.loads(envelope.decode("utf-8"))
        if not isinstance(decoded, dict) or set(decoded) != {
            "algorithm", "ciphertext", "nonce", "version",
        }:
            raise ReminderEnvelopeError("invalid opaque envelope")
        if decoded["version"] != ENVELOPE_VERSION or isinstance(decoded["version"], bool):
            raise ReminderEnvelopeError("invalid opaque envelope")
        if decoded["algorithm"] != ENVELOPE_ALGORITHM:
            raise ReminderEnvelopeError("invalid opaque envelope")
        if envelope != _canonical_json(decoded):
            raise ReminderEnvelopeError("invalid opaque envelope")
        nonce = _base64url_decode(decoded["nonce"])
        ciphertext = _base64url_decode(decoded["ciphertext"])
        if len(nonce) != 12 or len(ciphertext) < 16:
            raise ReminderEnvelopeError("invalid opaque envelope")
        plaintext = aead_decrypt(
            dek,
            nonce,
            ciphertext,
            _envelope_aad(
                owner_slug=owner_slug,
                reminder_id=reminder_id,
                occurrence_id=occurrence_id,
                intended_at_ms=intended_at_ms,
            ),
        )
        payload = json.loads(plaintext.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"content", "target"}
            or not isinstance(payload["target"], str)
            or not payload["target"]
            or not isinstance(payload["content"], str)
            or not payload["content"]
        ):
            raise ReminderEnvelopeError("invalid opaque envelope")
        if plaintext != _canonical_json(payload):
            raise ReminderEnvelopeError("invalid opaque envelope")
        parse_reminder_target(payload["target"])
        return payload["target"], payload["content"]
    except ReminderEnvelopeError:
        raise
    except Exception as exc:  # AEAD/tag/UTF-8/JSON failures are all private.
        raise ReminderEnvelopeError("invalid opaque envelope") from exc


@dataclass(frozen=True)
class _RemoteOccurrence:
    reminder_id: str
    occurrence_id: str
    intended_at_ms: int
    lifecycle_at_ms: int
    revision: int
    lifecycle: str
    payload_format: str
    opaque_payload: bytes | None
    target: str | None
    content: str | None


class ReminderSync:
    """Small provider-neutral outbox + snapshot state machine."""

    def __init__(
        self,
        *,
        store: MessageStore,
        keystore: KeyStore,
        owner_slug: str,
        http_client: PuffoCoreHttpClient | Any,
        scheduler: ReminderScheduler,
        now_ms: Callable[[], int] | None = None,
        idle_seconds: float = SYNC_IDLE_SECONDS,
    ) -> None:
        self.store = store
        self.owner_slug = _validate_owner(owner_slug)
        self.http_client = http_client
        self.scheduler = scheduler
        self._now_ms = now_ms or (lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
        self._idle_seconds = max(0.1, float(idle_seconds))
        # This is the sole custody call.  Nothing in this object logs or
        # returns these bytes; only the AEAD helpers receive them.
        self._dek = keystore.load_or_create_message_backup_dek(self.owner_slug)
        self._wakeup = asyncio.Event()
        self._stopping = False
        self._snapshot_requested = False
        # A remote-coordinated scheduler is fail-closed until an authenticated
        # complete current-state snapshot has reconciled this local database.
        self._delivery_ready = False

    @property
    def route_prefix(self) -> str:
        if bool(getattr(self.http_client, "keyless", False)):
            return "/v2/cloud-agents/agent-runtime/reminder-occurrences"
        return "/v2/agent-runtime/reminder-occurrences"

    def signal_snapshot(self) -> None:
        """Request a bounded full snapshot from a startup/reconnect seam."""
        self._snapshot_requested = True
        self._wakeup.set()

    def signal_lifecycle_committed(self) -> None:
        """Wake the outbox after create, cancel, or delivery commits locally."""
        self._wakeup.set()

    async def on_transport_connected(self) -> None:
        """Provider-neutral callback: signal only, never do HTTP in WS code."""
        self._delivery_ready = False
        self.signal_snapshot()

    def stop(self) -> None:
        self._stopping = True
        self._wakeup.set()

    @staticmethod
    def _safe_error_code(status: int) -> str:
        return f"http_{status}" if 100 <= status <= 599 else "http_error"

    @staticmethod
    def _retry_delay_ms(retry_count: int) -> int:
        exponent = min(max(0, int(retry_count)), 6)
        return min(RETRY_CAP_MS, RETRY_BASE_MS * (2 ** exponent))

    async def _ensure_envelope(
        self, record: ReminderSyncRecord,
    ) -> ReminderSyncRecord:
        if record.opaque_payload is not None or record.payload_format is not None:
            if (
                record.opaque_payload is None
                or record.payload_format != REMINDER_PAYLOAD_FORMAT
            ):
                raise ReminderEnvelopeError("invalid opaque envelope")
            # Validate a pre-existing envelope before letting it cross the
            # network.  This also catches an unsafe local file/database edit.
            decrypt_reminder_payload(
                dek=self._dek,
                owner_slug=self.owner_slug,
                reminder_id=record.reminder_id,
                occurrence_id=record.occurrence_id,
                intended_at_ms=record.intended_at_ms,
                envelope=record.opaque_payload,
            )
            return record
        envelope = encrypt_reminder_payload(
            dek=self._dek,
            owner_slug=self.owner_slug,
            reminder_id=record.reminder_id,
            occurrence_id=record.occurrence_id,
            intended_at_ms=record.intended_at_ms,
            target=record.target,
            content=record.content,
        )
        return await self.store.persist_reminder_envelope(
            occurrence_id=record.occurrence_id,
            payload_format=REMINDER_PAYLOAD_FORMAT,
            opaque_payload=envelope,
        )

    def _put_body(self, record: ReminderSyncRecord) -> dict[str, object]:
        if record.opaque_payload is None or record.payload_format != REMINDER_PAYLOAD_FORMAT:
            raise ReminderEnvelopeError("invalid opaque envelope")
        body: dict[str, object] = {
            "revision": record.revision,
            "reminder_id": record.reminder_id,
            "due_at": _canonical_utc(record.intended_at_ms),
            "lifecycle": record.server_lifecycle,
            "lifecycle_at": _canonical_utc(record.lifecycle_at_ms),
            "payload_format": record.payload_format,
            "opaque_payload": _base64url_encode(record.opaque_payload),
        }
        if record.server_lifecycle == "delivered" and record.delivery_claim_id is not None:
            body["delivery_claim_id"] = record.delivery_claim_id
        return body

    async def _put(self, path: str, body: dict[str, object]) -> Any:
        if bool(getattr(self.http_client, "keyless", False)):
            method = getattr(self.http_client, "put_unsigned", None)
            if not callable(method):
                raise ReminderContractError("missing keyless reminder transport")
            return await method(path, body)
        return await self.http_client.put(path, body)

    async def _get(self, path: str) -> Any:
        if bool(getattr(self.http_client, "keyless", False)):
            return await self.http_client.get_unsigned(path)
        return await self.http_client.get(path)

    async def _post(self, path: str, body: dict[str, object]) -> Any:
        if bool(getattr(self.http_client, "keyless", False)):
            method = getattr(self.http_client, "post_unsigned", None)
            if not callable(method):
                raise ReminderContractError("missing keyless reminder transport")
            return await method(path, body)
        return await self.http_client.post(path, body)

    @staticmethod
    def _validate_delivery_claim_response(
        response: Any, record: ReminderSyncRecord,
    ) -> str:
        required = {
            "occurrence_id", "revision", "lifecycle", "status",
        }
        if not isinstance(response, Mapping) or set(response) not in {
            frozenset(required),
            frozenset(required | {"lease_expires_at"}),
        }:
            raise ReminderContractError("invalid reminder delivery claim")
        if response.get("occurrence_id") != record.occurrence_id:
            raise ReminderContractError("invalid reminder delivery claim")
        revision = response.get("revision")
        if type(revision) is not int:
            raise ReminderContractError("invalid reminder delivery claim")
        lifecycle = response.get("lifecycle")
        status = response.get("status")
        if status not in {"acquired", "held", "terminal"}:
            raise ReminderContractError("invalid reminder delivery claim")
        if "lease_expires_at" in response:
            if status != "acquired":
                raise ReminderContractError("invalid reminder delivery claim")
            _parse_rfc3339_timestamp(response.get("lease_expires_at"))
        if status == "terminal":
            if lifecycle not in {"cancelled", "delivered"} or revision < record.revision:
                raise ReminderContractError("invalid reminder delivery claim")
        elif revision != record.revision or lifecycle != "scheduled":
            raise ReminderContractError("invalid reminder delivery claim")
        return status

    async def authorize_due_delivery(
        self, candidates: tuple[ReminderSyncRecord, ...],
    ) -> ReminderDeliveryAuthorization:
        """Authorize untouched local work or acquire Server delivery custody."""
        retry_after = int(self._now_ms()) + DELIVERY_CLAIM_RETRY_MS
        blocked = False
        authorized: list[str] = []
        for candidate in candidates:
            # A row cannot have crossed the remote boundary until its
            # immutable envelope exists.  That lets untouched local work fire
            # while offline without treating a prepared unknown-ack row as
            # safe to deliver locally.
            if (
                candidate.server_ack_revision == 0
                and candidate.payload_format is None
                and candidate.opaque_payload is None
            ):
                authorized.append(candidate.occurrence_id)
                continue
            if not self._delivery_ready:
                blocked = True
                continue
            if candidate.state not in {"scheduled", "claimed"}:
                blocked = True
                continue
            # Server claims are renewable leases. The persisted flag records a
            # prior response but is never sufficient authorization after a
            # restart or suspend; retrying the same claim ID renews and fences
            # this concrete delivery attempt.
            claim_id = candidate.delivery_claim_id or str(uuid.uuid4())
            try:
                persisted = await self.store.persist_reminder_delivery_claim_id(
                    occurrence_id=candidate.occurrence_id,
                    revision=candidate.revision,
                    claim_id=claim_id,
                )
                if persisted is None:
                    continue
                claim_id = persisted.delivery_claim_id
                if claim_id is None:
                    continue
                response = await self._post(
                    f"{self.route_prefix}/{persisted.occurrence_id}/delivery-claim",
                    {"revision": persisted.revision, "claim_id": claim_id},
                )
                status = self._validate_delivery_claim_response(response, persisted)
            except asyncio.CancelledError:
                raise
            except Exception:
                blocked = True
                continue
            if status == "acquired":
                if await self.store.acquire_reminder_delivery_claim(
                    occurrence_id=persisted.occurrence_id,
                    revision=persisted.revision,
                    claim_id=claim_id,
                ):
                    authorized.append(persisted.occurrence_id)
            elif status == "terminal":
                blocked = True
                self._delivery_ready = False
                self.signal_snapshot()
                try:
                    await self.reconcile_snapshot()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            else:
                blocked = True
        return ReminderDeliveryAuthorization(
            occurrence_ids=tuple(authorized),
            retry_after_ms=retry_after if blocked else None,
        )

    def _validate_put_response(
        self, response: Any, record: ReminderSyncRecord, body: Mapping[str, object],
    ) -> None:
        if not isinstance(response, Mapping):
            raise ReminderContractError("invalid reminder response")
        required = {
            "occurrence_id", "revision", "reminder_id", "due_at", "lifecycle",
            "lifecycle_at", "payload_format",
        }
        allowed = required | {"opaque_payload"}
        if frozenset(response) not in {frozenset(required), frozenset(allowed)}:
            raise ReminderContractError("invalid reminder response")
        if response.get("occurrence_id") != record.occurrence_id:
            raise ReminderContractError("invalid reminder response")
        response_revision = response.get("revision")
        # JSON revisions are integer protocol values.  ``bool`` and floats
        # compare equal to ``1`` in Python, so equality alone would let a
        # malformed response acknowledge a local revision.
        if type(response_revision) is not int or response_revision != record.revision:
            raise ReminderContractError("invalid reminder response")
        if response.get("reminder_id") != record.reminder_id:
            raise ReminderContractError("invalid reminder response")
        if _parse_rfc3339_ms(response.get("due_at")) != record.intended_at_ms:
            raise ReminderContractError("invalid reminder response")
        if response.get("lifecycle") != record.server_lifecycle:
            raise ReminderContractError("invalid reminder response")
        if _parse_rfc3339_ms(response.get("lifecycle_at")) != record.lifecycle_at_ms:
            raise ReminderContractError("invalid reminder response")
        if response.get("payload_format") != REMINDER_PAYLOAD_FORMAT:
            raise ReminderContractError("invalid reminder response")
        returned_payload = response.get("opaque_payload")
        if record.server_lifecycle == "scheduled":
            if returned_payload != body["opaque_payload"]:
                raise ReminderContractError("invalid reminder response")
        elif returned_payload is not None:
            # Current Server terminals omit this field.  If a compatible
            # implementation does return it, it must be the immutable bytes.
            raise ReminderContractError("invalid reminder response")

    async def _record_retry(
        self, record: ReminderSyncRecord, category: str,
    ) -> None:
        retry_after = int(self._now_ms()) + self._retry_delay_ms(record.sync_retry_count)
        await self.store.schedule_reminder_sync_retry(
            occurrence_id=record.occurrence_id,
            revision=record.revision,
            retry_after_ms=retry_after,
        )
        logger.warning(
            "reminder sync retry category=%s revision=%d",
            category, record.revision,
        )

    async def _record_permanent(
        self, record: ReminderSyncRecord, code: str,
    ) -> None:
        await self.store.record_reminder_sync_permanent_failure(
            occurrence_id=record.occurrence_id,
            revision=record.revision,
            code=code,
        )
        logger.warning("reminder sync permanent category=%s revision=%d", code, record.revision)

    async def upload_pending_once(
        self,
        *,
        force: bool = False,
        retry_permanent: bool = False,
    ) -> int:
        """Upload one bounded scan; local work survives every failure path."""
        uploaded = 0
        records = await self.store.pending_reminder_sync_records(
            now_ms=int(self._now_ms()),
            force=force,
            retry_permanent=retry_permanent,
        )
        for original in records:
            try:
                record = await self._ensure_envelope(original)
                body = self._put_body(record)
            except (
                ReminderEnvelopeError,
                ReminderContractError,
                LifecycleConflict,
                ValueError,
            ):
                await self._record_permanent(original, "invalid_envelope")
                continue
            path = f"{self.route_prefix}/{record.occurrence_id}"
            try:
                response = await self._put(path, body)
            except ReminderContractError:
                # A missing/invalid transport capability is a local contract
                # failure, not a connectivity failure.  Suppress this
                # revision until a newer local transition or reconciliation.
                await self._record_permanent(record, "transport_contract")
                continue
            except HttpError as exc:
                if exc.status == 429 or exc.status >= 500:
                    await self._record_retry(record, "http_retryable")
                else:
                    await self._record_permanent(record, self._safe_error_code(exc.status))
                continue
            except (asyncio.TimeoutError, TimeoutError, OSError):
                await self._record_retry(record, "transport")
                continue
            except Exception:
                # HTTP client implementations may wrap connector failures in
                # a transport-specific exception; do not retain its text.
                await self._record_retry(record, "transport")
                continue
            try:
                self._validate_put_response(response, record, body)
            except ReminderContractError:
                await self._record_permanent(record, "response_contract")
                continue
            if await self.store.acknowledge_reminder_sync_revision(
                occurrence_id=record.occurrence_id,
                revision=record.revision,
            ):
                uploaded += 1
        return uploaded

    def _validate_snapshot_row(self, value: object) -> _RemoteOccurrence:
        if not isinstance(value, Mapping):
            raise ReminderContractError("invalid reminder snapshot")
        required = {
            "occurrence_id", "revision", "reminder_id", "due_at", "lifecycle",
            "lifecycle_at", "payload_format",
        }
        lifecycle = value.get("lifecycle")
        expected = required | ({"opaque_payload"} if lifecycle == "scheduled" else set())
        if set(value) != expected or lifecycle not in {
            "scheduled", "cancelled", "delivered",
        }:
            raise ReminderContractError("invalid reminder snapshot")
        occurrence_id = _validate_id(value.get("occurrence_id"))
        reminder_id = _validate_id(value.get("reminder_id"))
        revision = value.get("revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision <= 0
            or (lifecycle == "scheduled" and revision != 1)
        ):
            raise ReminderContractError("invalid reminder snapshot")
        payload_format = value.get("payload_format")
        if payload_format != REMINDER_PAYLOAD_FORMAT:
            raise ReminderContractError("invalid reminder snapshot")
        intended_at_ms = _parse_rfc3339_ms(value.get("due_at"))
        lifecycle_at_ms = _parse_rfc3339_ms(value.get("lifecycle_at"))
        opaque_payload: bytes | None = None
        target: str | None = None
        content: str | None = None
        if lifecycle == "scheduled":
            opaque_payload = _base64url_decode(value.get("opaque_payload"))
            if not 1 <= len(opaque_payload) <= MAX_ENVELOPE_BYTES:
                raise ReminderContractError("invalid reminder snapshot")
            try:
                target, content = decrypt_reminder_payload(
                    dek=self._dek,
                    owner_slug=self.owner_slug,
                    reminder_id=reminder_id,
                    occurrence_id=occurrence_id,
                    intended_at_ms=intended_at_ms,
                    envelope=opaque_payload,
                )
            except ReminderEnvelopeError as exc:
                raise ReminderContractError("invalid reminder snapshot") from exc
        return _RemoteOccurrence(
            reminder_id=reminder_id,
            occurrence_id=occurrence_id,
            intended_at_ms=intended_at_ms,
            lifecycle_at_ms=lifecycle_at_ms,
            revision=revision,
            lifecycle=lifecycle,
            payload_format=payload_format,
            opaque_payload=opaque_payload,
            target=target,
            content=content,
        )

    async def reconcile_snapshot(self) -> int:
        """Exhaust the Server current-state snapshot and apply changed rows once."""
        after: str | None = None
        pages = 0
        changed = 0
        seen_after: set[str] = set()
        while True:
            query: dict[str, object] = {"limit": SNAPSHOT_PAGE_LIMIT}
            if after is not None:
                query["after"] = after
            response = await self._get(f"{self.route_prefix}?{urlencode(query)}")
            if not isinstance(response, Mapping) or set(response) != {
                "occurrences", "next_after",
            }:
                raise ReminderContractError("invalid reminder snapshot")
            occurrences = response.get("occurrences")
            next_after = response.get("next_after")
            if not isinstance(occurrences, list) or len(occurrences) > 200:
                raise ReminderContractError("invalid reminder snapshot")
            if next_after is not None:
                next_after = _validate_id(next_after)
            remote_rows = [self._validate_snapshot_row(value) for value in occurrences]
            page_occurrence_ids = [remote.occurrence_id for remote in remote_rows]
            if (
                page_occurrence_ids != sorted(page_occurrence_ids)
                or len(set(page_occurrence_ids)) != len(page_occurrence_ids)
                or (after is not None and page_occurrence_ids and page_occurrence_ids[0] <= after)
            ):
                raise ReminderContractError("invalid reminder snapshot")
            if next_after is not None and (
                not page_occurrence_ids
                or next_after != page_occurrence_ids[-1]
            ):
                raise ReminderContractError("invalid reminder snapshot")
            for remote in remote_rows:
                if remote.lifecycle == "scheduled":
                    assert remote.target is not None
                    assert remote.content is not None
                    assert remote.opaque_payload is not None
                    result = await self.store.materialize_remote_scheduled_reminder(
                        reminder_id=remote.reminder_id,
                        occurrence_id=remote.occurrence_id,
                        target=remote.target,
                        content=remote.content,
                        intended_at_ms=remote.intended_at_ms,
                        lifecycle_at_ms=remote.lifecycle_at_ms,
                        revision=remote.revision,
                        payload_format=remote.payload_format,
                        opaque_payload=remote.opaque_payload,
                    )
                else:
                    result = await self.store.materialize_remote_terminal_reminder(
                        reminder_id=remote.reminder_id,
                        occurrence_id=remote.occurrence_id,
                        intended_at_ms=remote.intended_at_ms,
                        lifecycle=remote.lifecycle,
                        lifecycle_at_ms=remote.lifecycle_at_ms,
                        revision=remote.revision,
                        payload_format=remote.payload_format,
                    )
                if result.changed:
                    changed += 1
                if result.conflict:
                    # A local immutable conflict is non-destructive.  Do not
                    # include IDs, payload, or decryption detail in logs.
                    logger.warning("reminder snapshot conflict")
            if next_after is None:
                break
            # The frozen snapshot contract is ordered by occurrence ID and
            # advances with the final ID of the page.  Validate that cursor
            # relation before accepting a subsequent page, so a malformed
            # server response cannot silently skip active occurrences.
            if next_after in seen_after or next_after == after:
                raise ReminderContractError("invalid reminder snapshot")
            seen_after.add(next_after)
            after = next_after
            pages += 1
            if pages >= MAX_SNAPSHOT_PAGES:
                raise ReminderContractError("invalid reminder snapshot")
        if changed:
            # One changed batch gives the existing scheduler a chance to
            # process every overdue reconstruction through its normal path.
            self.scheduler.signal()
        self._delivery_ready = True
        return changed

    async def run(self, *, request_snapshot_on_start: bool = True) -> None:
        """Own the bounded background cadence; never own provider turns."""
        if request_snapshot_on_start:
            self.signal_snapshot()
        while not self._stopping:
            reconciliation_completed = False
            if self._snapshot_requested:
                self._snapshot_requested = False
                try:
                    await self.reconcile_snapshot()
                    reconciliation_completed = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("reminder snapshot failed category=transport_or_contract")
                    self._delivery_ready = False
                    self._snapshot_requested = True
            try:
                await self.upload_pending_once(
                    retry_permanent=reconciliation_completed,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Store failures are unexpected, but must not take down the
                # scheduler or global Inbox/provider runtime.
                logger.warning("reminder sync pass failed category=local")
            if self._stopping:
                break
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._idle_seconds)
            except asyncio.TimeoutError:
                pass
            finally:
                self._wakeup.clear()


async def prepare_reminder_sync(
    client: Any,
    runtime: Any,
    *,
    agent_id: str = "",
    register_connected: Callable[[Callable[[], Any]], None] | None = None,
) -> ReminderSync:
    """Wire one durable reminder sync onto ``runtime``'s scheduler.

    Both runtime owners (the worker and the ws-local owned runtime) must
    expose the *same* server-sync contract behind the reminder tools, so
    the authorizer/lifecycle/connected wiring lives here rather than being
    re-derived per call site. ``register_connected`` overrides how the
    transport-connected callback is registered — the ws-local site keeps a
    single per-client relay because ``add_connected_callback`` has no
    removal API and attach/detach cycles would otherwise accumulate.
    """
    reminder_sync = ReminderSync(
        store=client.store,
        keystore=client.keystore,
        owner_slug=client.slug,
        http_client=client.http,
        scheduler=runtime.reminder_scheduler,
    )
    runtime.reminder_scheduler.set_delivery_authorizer(
        reminder_sync.authorize_due_delivery
    )
    runtime.reminder_scheduler.set_lifecycle_committed_callback(
        reminder_sync.signal_lifecycle_committed
    )
    register = register_connected or client.add_connected_callback
    register(reminder_sync.on_transport_connected)
    try:
        await reminder_sync.reconcile_snapshot()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "agent %s: startup reminder snapshot failed; delivery remains blocked",
            agent_id,
        )
        reminder_sync.signal_snapshot()
    return reminder_sync
