"""Bridge between puffo-core WS/HTTP and the worker's on_message
interface. Handles message reception, decryption, local storage,
and encrypted reply posting.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable, Coroutine, Optional

from ..crypto.encoding import base64url_decode
from ..crypto.http_client import HttpError, PuffoCoreHttpClient
from ..crypto.keystore import KeyStore, decode_secret
from ..crypto.message import (
    EncryptInput,
    RecipientDevice,
    decrypt_message,
    encrypt_message,
)
from ..crypto.primitives import Ed25519KeyPair, KemKeyPair
from ..crypto.ws_client import PuffoCoreWsClient
from .core import AgentAPIError
from .events import random_nonce, sign_event
from .message_store import MessageStore

logger = logging.getLogger(__name__)


# Lower number = higher priority — drained first by the consumer loop.
PRIORITY_MENTIONED_HUMAN = 1
PRIORITY_MENTIONED_BOT = 2
PRIORITY_HUMAN = 3
PRIORITY_BOT = 4
PRIORITY_SYSTEM = 5


async def _fetch_blob_with_retry(
    http: PuffoCoreHttpClient, blob_id: str,
) -> bytes | None:
    """GET /blobs/<id> with retry on transient 404. The WS event
    sometimes races the blob row's visibility on the recipient's
    connection; retry a few times before giving up. Non-404 errors
    abort immediately.
    """
    delays = (0, 5.0, 5.0, 5.0)
    last: Exception | None = None
    for delay in delays:
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            return await http.get_bytes(f"/blobs/{blob_id}")
        except HttpError as exc:
            last = exc
            if exc.status != 404:
                # Permanent failure — don't burn 15s on retries.
                logger.warning(
                    "attachment download failed (%s): %s", blob_id, exc,
                )
                return None
            # else: try again after the next delay
        except Exception as exc:
            last = exc
            logger.warning(
                "attachment download failed (%s): %s", blob_id, exc,
            )
            return None
    logger.warning(
        "attachment download still 404 after retries (%s): %s",
        blob_id, last,
    )
    return None


def _strip_multipart_wrapper(data: bytes) -> bytes:
    """Detect a multipart/form-data envelope and return just the file
    part's body. Returns ``data`` unchanged when the bytes don't look
    like multipart (the common case, where the plaintext is already
    the raw file). Legacy senders sometimes encrypt the form-data
    body rather than the file bytes; this unwraps that case.
    """
    if not data.startswith(b"--"):
        return data
    # Boundary token = bytes between ``--`` and the first \r\n.
    nl = data.find(b"\r\n")
    if nl == -1 or nl > 256:
        return data
    boundary = data[2:nl]
    if not boundary or any(b in boundary for b in (b"\r", b"\n")):
        return data
    sep = b"--" + boundary
    parts = data.split(sep)
    candidates = [p for p in parts[1:] if p and not p.startswith(b"--")]
    best: bytes | None = None
    for part in candidates:
        # Each part: \r\n<headers>\r\n\r\n<body>\r\n
        if part.startswith(b"\r\n"):
            part = part[2:]
        head_end = part.find(b"\r\n\r\n")
        if head_end == -1:
            continue
        body = part[head_end + 4 :]
        if body.endswith(b"\r\n"):
            body = body[:-2]
        # Pick the largest body — the file part is virtually always
        # bigger than the small text fields.
        if best is None or len(body) > len(best):
            best = body
    if best is None:
        return data
    return best


def _parse_operator_pubkey(identity_cert_json: Optional[str]) -> Optional[bytes]:
    """Extract the 32-byte operator root pubkey from our identity_cert.
    Returns ``None`` when the cert is missing, isn't an agent cert
    (no ``declared_operator_public_key``), or the field doesn't decode
    to exactly 32 bytes. Caller treats ``None`` as "no operator".
    """
    if not identity_cert_json:
        return None
    import json
    try:
        cert = json.loads(identity_cert_json)
    except Exception:
        return None
    op_pk_b64 = cert.get("declared_operator_public_key")
    if not isinstance(op_pk_b64, str) or not op_pk_b64:
        return None
    try:
        op_pk = base64url_decode(op_pk_b64)
    except Exception:
        return None
    if len(op_pk) != 32:
        return None
    return op_pk


def _compute_priority(direct: bool, sender_is_bot: bool) -> int:
    """Map (direct, sender_is_bot) to one of the PRIORITY_* bands.
    PRIORITY_SYSTEM is reserved for a future service-message envelope.
    """
    if direct and not sender_is_bot:
        return PRIORITY_MENTIONED_HUMAN
    if direct and sender_is_bot:
        return PRIORITY_MENTIONED_BOT
    if not sender_is_bot:
        return PRIORITY_HUMAN
    return PRIORITY_BOT


class DeviceKeyCache:
    """Caches sender signing public keys.

    The wire envelope only carries ``sender_slug`` — the actual
    ``sender_subkey_id`` is inside the encrypted payload, so we can't
    look up by (slug, subkey_id) before decrypt. Instead we paginate
    ``GET /certs/sync?slugs=<slug>`` to collect every ``subkey_cert``
    issued for the sender; the caller tries each one until
    verification succeeds.
    """

    def __init__(self, http_client: PuffoCoreHttpClient):
        self._http = http_client
        self._cache: dict[str, list[bytes]] = {}

    async def get_signing_keys(self, slug: str) -> list[bytes]:
        if slug in self._cache:
            return self._cache[slug]

        pks: list[bytes] = []
        since = 0
        while True:
            data = await self._http.get(f"/certs/sync?slugs={slug}&since={since}")
            for entry in data.get("entries", []):
                if entry.get("kind") == "subkey_cert":
                    pk_b64 = entry.get("cert", {}).get("subkey_public_key", "")
                    if pk_b64:
                        try:
                            pks.append(base64url_decode(pk_b64))
                        except Exception:
                            # Bad base64 in registry — skip, don't fail.
                            pass
                since = entry.get("seq", since)
            if not data.get("has_more"):
                break

        if not pks:
            raise ValueError(f"no subkey_cert entries for {slug}")
        self._cache[slug] = pks
        return pks

    def invalidate(self, slug: str) -> None:
        # Call when verification fails on a known sender to pick up a
        # freshly-rotated subkey on the next fetch.
        self._cache.pop(slug, None)


class PuffoCoreMessageClient:
    """Receives encrypted envelopes via WebSocket, decrypts them,
    stores in the local MessageStore, and invokes ``on_message`` with
    the worker's expected parameter signature.
    """

    def __init__(
        self,
        slug: str,
        device_id: str,
        space_id: str,
        keystore: KeyStore,
        http_client: PuffoCoreHttpClient,
        message_store: MessageStore,
        operator_slug: str = "",
        workspace: str = "",
    ):
        self.slug = slug
        self.device_id = device_id
        self.space_id = space_id
        # Operator's slug — used to DM them on non-auto-acceptable
        # invites. Empty string falls back to log-only handling.
        self.operator_slug = operator_slug
        # Absolute path to the agent's workspace. Inbound attachments
        # are decrypted into ``<workspace>/.puffo/inbox/<envelope_id>/``.
        self.workspace = workspace
        self.keystore = keystore
        self.http = http_client
        self.store = message_store
        self._key_cache = DeviceKeyCache(http_client)
        self._ws: Optional[PuffoCoreWsClient] = None
        # Most recent DM sender. ``post_message(channel_id="")`` means
        # "reply to whoever just DMed me". Single-slot is fine since
        # the worker handles one envelope at a time; concurrent DM
        # handlers would need a per-turn lookup keyed by envelope_id.
        self._last_dm_sender: str = ""

        # Operator root pubkey from our identity_cert (cached at
        # listen() time). None if unreadable — falls back to log-only
        # invite handling.
        self._operator_root_pubkey: bytes | None = None
        # slug → root_pubkey for inviters, to avoid hammering
        # /certs/sync on bursts of invites from the same person.
        self._inviter_root_cache: dict[str, bytes] = {}
        # slug → display_name from /identities/profiles. Empty strings
        # are cached too so unset display_names don't trigger re-fetch.
        self._display_name_cache: dict[str, str] = {}
        # Invitation event_ids the worker has already processed; per-
        # listen() (cleared on reconnect). Server-side state is the
        # durable record — this cache just avoids repeating work
        # within a session.
        self._processed_invite_ids: set[str] = set()
        # When the worker DMs the operator about a non-auto-acceptable
        # invite, the DM's envelope_id lives here so a ``y``/``n``
        # reply in that thread can be intercepted inside the daemon.
        # In-memory only — on restart we re-DM from the next poll.
        self._pending_invite_dms: dict[str, dict[str, Any]] = {}

        # channel_id → space_id learned from inbound envelopes. The
        # agent's config carries one "home" space_id, but cross-space
        # channel invites work too; we route replies through the space
        # the message arrived from. Falls back to ``self.space_id``
        # when no inbound envelope on this channel has been seen.
        self._channel_space: dict[str, str] = {}

        # Lazy caches for human-readable space + channel names; names
        # aren't on the WS payload so we resolve via ``GET /spaces``
        # and ``GET /spaces/<id>/events`` on first reference. Bare-id
        # fallback when lookup fails so the LLM never sees a blank.
        self._space_name_cache: dict[str, str] = {}
        self._channel_name_cache: dict[str, str] = {}

    async def listen(
        self,
        on_message: Callable[..., Coroutine[Any, Any, Any]],
    ) -> None:
        identity = self.keystore.load_identity(self.slug)
        kem_kp = KemKeyPair.from_secret_bytes(
            decode_secret(identity.kem_secret_key)
        )

        # Cache the operator's root pubkey from our identity_cert.
        # ``declared_operator_public_key`` is base64url of the
        # operator's 32-byte ed25519 root pubkey — set at provision
        # time and immutable.
        self._operator_root_pubkey = _parse_operator_pubkey(
            identity.identity_cert_json,
        )

        async def handle_envelope(envelope: dict) -> None:
            if envelope.get("sender_slug") == self.slug:
                return

            sender_slug = envelope.get("sender_slug", "")
            try:
                sender_pks = await self._key_cache.get_signing_keys(sender_slug)
            except Exception as e:
                logger.warning(
                    "could not fetch signing keys for %s — skipping (%s)",
                    sender_slug, e,
                )
                return

            # Try each pubkey until one decrypts. On total failure,
            # invalidate + retry once with a fresh pull to handle
            # subkey rotation since the cache was populated.
            payload = None
            for pk in sender_pks:
                try:
                    payload = decrypt_message(
                        envelope, self.device_id, kem_kp, pk,
                    )
                    break
                except Exception:
                    continue

            if payload is None:
                self._key_cache.invalidate(sender_slug)
                try:
                    sender_pks = await self._key_cache.get_signing_keys(sender_slug)
                    for pk in sender_pks:
                        try:
                            payload = decrypt_message(
                                envelope, self.device_id, kem_kp, pk,
                            )
                            break
                        except Exception:
                            continue
                except Exception:
                    pass

            if payload is None:
                logger.warning(
                    "decryption failed for %s (%d sender keys tried) — skipping",
                    envelope.get("envelope_id"), len(sender_pks),
                )
                return

            await self.store.store({
                "envelope_id": payload.envelope_id,
                "envelope_kind": payload.envelope_kind,
                "sender_slug": payload.sender_slug,
                "channel_id": payload.channel_id,
                "space_id": payload.space_id,
                "recipient_slug": payload.recipient_slug,
                "content_type": payload.content_type,
                "content": payload.content,
                "sent_at": payload.sent_at,
                "thread_root_id": payload.thread_root_id,
                "reply_to_id": payload.reply_to_id,
            })

            # Daemon-side intercept: ``y``/``n`` in the thread of an
            # outstanding invite-DM accepts/rejects the invite without
            # waking the LLM. Other text falls through to the queue.
            if (
                payload.envelope_kind == "dm"
                and payload.sender_slug == self.operator_slug
                and payload.thread_root_id
                and payload.thread_root_id in self._pending_invite_dms
            ):
                handled = await self._maybe_handle_invite_reply(
                    thread_root_id=payload.thread_root_id,
                    text=str(payload.content) if payload.content else "",
                )
                if handled:
                    # Handled inline — don't queue for the LLM.
                    self._last_dm_sender = payload.sender_slug
                    return

            channel_id = payload.channel_id or ""
            is_dm = payload.envelope_kind == "dm"
            # ``puffo/message+attachments/v1`` carries
            # ``{ text, attachments: [...] }``; other content types
            # use the plain-string path.
            attachment_paths: list[str] = []
            if payload.content_type == "puffo/message+attachments/v1" and isinstance(
                payload.content, dict,
            ):
                raw_text = str(payload.content.get("text") or "")
                metas_raw = payload.content.get("attachments") or []
                if isinstance(metas_raw, list):
                    attachment_paths = await self._save_inbound_attachments(
                        envelope_id=payload.envelope_id, metas_raw=metas_raw,
                    )
            else:
                raw_text = str(payload.content) if payload.content else ""

            # Stash the sender so `post_message("")` can route replies.
            # Always overwrite — first-write would pin replies to a
            # stale peer when a different person DMs us.
            if is_dm:
                self._last_dm_sender = payload.sender_slug
            elif payload.channel_id and payload.space_id:
                # Remember which space owns this channel so replies
                # resolve members in the right space (cross-space
                # invites would otherwise fail).
                self._channel_space[payload.channel_id] = payload.space_id

            # puffo-core has no structural mention objects yet, so
            # synthesise the dict shape core.py:_append_user expects
            # (``username``/``is_bot``/``is_self``) when the text
            # contains `@<our slug>` literally.
            is_mention = f"@{self.slug}" in raw_text
            mentions: list[dict] = []
            if is_mention:
                mentions.append({
                    "username": self.slug,
                    "is_bot": True,
                    "is_self": True,
                })

            # Self-mention rewrite: `@<our-slug>` → `@you(<our-slug>)`,
            # the unambiguous "you are being addressed" signal documented
            # in the shared primer. Other handles stay un-rewrapped so
            # peer addressing reads naturally.
            clean_text = raw_text.replace(
                f"@{self.slug}", f"@you({self.slug})",
            ).strip() if is_mention else raw_text

            # Resolve human-readable names so the LLM sees ``space:``/
            # ``channel:`` instead of bare ids. Cached per session. DMs
            # render an explicit "Direct message" label.
            space_id = payload.space_id or ""
            space_name = (
                await self._resolve_space_name(space_id) if space_id else ""
            )
            if is_dm:
                channel_name = "Direct message"
            elif channel_id:
                channel_name = await self._resolve_channel_name(
                    space_id, channel_id,
                )
            else:
                channel_name = channel_id

            # Monotonic ``seq`` tiebreaker keeps PriorityQueue from
            # comparing the args dict on ties (dicts aren't orderable
            # so a same-priority pair would TypeError).
            direct = is_dm or is_mention
            sender_is_bot = False  # puffo-core has no is_bot flag yet
            priority = _compute_priority(direct, sender_is_bot)
            self._queue_seq += 1
            await self._queue.put((priority, self._queue_seq, {
                "channel_id": channel_id,
                "channel_name": channel_name,
                "space_id": space_id,
                "space_name": space_name,
                "sender_slug": payload.sender_slug,
                "sender_email": "",
                "text": clean_text,
                "root_id": payload.thread_root_id or "",
                "is_dm": is_dm,
                "attachments": attachment_paths,
                "sender_is_bot": sender_is_bot,
                "mentions": mentions,
                "envelope_id": payload.envelope_id,
                "sent_at": payload.sent_at,
            }))

        # Per-listen() queue. A reconnect drops any envelopes not yet
        # drained; the server redelivers via /messages/pending on the
        # next subscribe.
        self._queue = asyncio.PriorityQueue()
        self._queue_seq = 0
        # Reset on every (re)connect — the auto-accept path is
        # idempotent against server-side state.
        self._processed_invite_ids = set()
        consumer_task = asyncio.ensure_future(self._consume_queue(on_message))
        invite_poll_task = asyncio.ensure_future(self._invite_poll_loop())

        self._ws = PuffoCoreWsClient(
            server_url=self.keystore.load_identity(self.slug).server_url,
            keystore=self.keystore,
            slug=self.slug,
            http_client=self.http,
        )
        self._ws.on_message = handle_envelope
        self._ws.on_event = self._handle_event
        await self.store.open()
        try:
            await self._ws.run()
        finally:
            consumer_task.cancel()
            invite_poll_task.cancel()
            for task in (consumer_task, invite_poll_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _consume_queue(
        self,
        on_message: Callable[..., Coroutine[Any, Any, Any]],
    ) -> None:
        """Drain the priority queue serially. One turn at a time so
        the underlying session keeps a coherent conversation history;
        concurrent turns would interleave context.
        """
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                return
            _priority, _seq, args = item
            try:
                await on_message(
                    args["channel_id"], args["channel_name"],
                    args["sender_slug"], args["sender_email"],
                    args["text"], args["root_id"], args["is_dm"],
                    args["attachments"], args["sender_is_bot"],
                    args["mentions"],
                    args["envelope_id"], args["sent_at"], [],
                    space_id=args.get("space_id", ""),
                    space_name=args.get("space_name", ""),
                )
            except AgentAPIError:
                # Adapter surfaced "API Error". Re-enqueue the same
                # tuple so the message keeps its priority band slot
                # (later arrivals have strictly larger ``seq``), then
                # back off 15-45s randomised to avoid a thundering
                # herd across a fleet hit by the same outage.
                await self._queue.put(item)
                delay = random.uniform(15.0, 45.0)
                logger.warning(
                    "agent reply contained 'API Error' for envelope %s; "
                    "re-queued and pausing %.1fs before next message",
                    args.get("envelope_id"), delay,
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "on_message handler failed for envelope %s",
                    args.get("envelope_id"),
                )

    async def _invite_poll_loop(self) -> None:
        """Poll ``/invites`` to catch invites the WS can't reach (the
        server only fans events to existing space members, which the
        invitee isn't yet).
        """
        INTERVAL = 30
        # Brief grace period so first-poll output doesn't interleave
        # with the WS handshake log on startup.
        try:
            await asyncio.sleep(2)
            while True:
                await self._poll_pending_invites()
                await asyncio.sleep(INTERVAL)
        except asyncio.CancelledError:
            return

    async def _handle_event(self, scope: str, event: dict) -> None:
        """WS event router for space + channel invites.

        WS payloads carry bare IDs but not the space/channel name
        snapshots (those only live on the ``pending_invites`` row).
        To avoid bare-ID DMs we use the WS push as a trigger and
        defer to ``_poll_pending_invites``; the processed-id cache
        prevents the next periodic poll from double-acting.
        """
        kind = event.get("kind")
        if kind not in ("invite_to_space", "invite_to_channel"):
            return

        payload = event.get("payload") or {}
        if payload.get("invitee_slug") != self.slug:
            return  # Server fans the event to space members too.

        await self._poll_pending_invites()

    async def _poll_pending_invites(self) -> None:
        """Pull pending invites the agent hasn't acted on. Space
        invites only arrive via this poll (WS doesn't fan to non-
        members); channel invites also benefit from the catch-up.
        """
        try:
            data = await self.http.get("/invites?direction=received")
        except Exception:
            logger.exception("invite poll failed")
            return
        invites = data.get("invites") or []
        if not invites:
            return
        logger.info("invite poll: %d pending invite(s)", len(invites))
        for entry in invites:
            invitation_event_id = entry.get("invitation_event_id", "")
            if not invitation_event_id:
                continue
            if invitation_event_id in self._processed_invite_ids:
                continue
            scope = entry.get("scope", "")
            space_id = entry.get("space_id", "")
            channel_id = entry.get("channel_id") or ""
            inviter_slug = entry.get("inviter_slug", "")
            kind = (
                "invite_to_channel" if scope == "channel"
                else "invite_to_space" if scope == "space"
                else ""
            )
            if not kind:
                logger.warning(
                    "unknown invite scope %r (event_id=%s) — skipping",
                    scope, invitation_event_id,
                )
                continue
            # /invites snapshots space + channel names at invite time
            # so the operator DM can render them without a round-trip.
            space_name = entry.get("space_name") or None
            channel_name = entry.get("channel_name") or None
            await self._process_invite(
                kind=kind,
                invitation_event_id=invitation_event_id,
                inviter_slug=inviter_slug,
                space_id=space_id,
                channel_id=channel_id,
                space_name=space_name,
                channel_name=channel_name,
            )

    async def _process_invite(
        self,
        *,
        kind: str,
        invitation_event_id: str,
        inviter_slug: str,
        space_id: str,
        channel_id: str,
        space_name: str | None = None,
        channel_name: str | None = None,
    ) -> None:
        """Auto-accept if the inviter's pubkey matches our operator;
        otherwise DM the operator. Idempotent on
        ``_processed_invite_ids``."""
        if not invitation_event_id or not inviter_slug or not space_id:
            logger.warning(
                "invite missing required fields: kind=%s event_id=%s "
                "signer=%s space=%s",
                kind, invitation_event_id, inviter_slug, space_id,
            )
            return
        if kind == "invite_to_channel" and not channel_id:
            logger.warning(
                "channel invite missing channel_id: event_id=%s",
                invitation_event_id,
            )
            return
        if invitation_event_id in self._processed_invite_ids:
            return

        is_from_operator = await self._inviter_is_operator(inviter_slug)
        if is_from_operator:
            try:
                await self._accept_invite(
                    kind, invitation_event_id, space_id, channel_id,
                )
                logger.info(
                    "auto-accepted %s from operator %s (event_id=%s)",
                    kind, inviter_slug, invitation_event_id,
                )
                self._processed_invite_ids.add(invitation_event_id)
            except Exception:
                logger.exception(
                    "failed to auto-accept %s from operator %s (event_id=%s)",
                    kind, inviter_slug, invitation_event_id,
                )
        else:
            try:
                await self._notify_operator_of_invite(
                    kind=kind,
                    inviter_slug=inviter_slug,
                    space_id=space_id,
                    channel_id=channel_id,
                    invitation_event_id=invitation_event_id,
                    space_name=space_name,
                    channel_name=channel_name,
                )
            finally:
                # Mark processed even on DM failure — the operator
                # still sees the invite in their chat client.
                self._processed_invite_ids.add(invitation_event_id)

    async def _inviter_is_operator(self, inviter_slug: str) -> bool:
        """True iff ``inviter_slug``'s root pubkey matches our
        operator pubkey. Fails closed (returns ``False``) when either
        side is unavailable so a missing key doesn't auto-accept.
        """
        if self._operator_root_pubkey is None:
            return False
        # Fast path: skip /certs/sync when slugs match. Pubkey check
        # still runs for unfamiliar slugs in case the operator invites
        # under a different name.
        if inviter_slug and inviter_slug == self.operator_slug:
            return True
        try:
            inviter_pk = await self._fetch_inviter_root_pubkey(inviter_slug)
        except Exception as e:
            logger.warning(
                "could not look up identity_cert for inviter %s: %s",
                inviter_slug, e,
            )
            return False
        return inviter_pk is not None and inviter_pk == self._operator_root_pubkey

    async def _fetch_inviter_root_pubkey(self, slug: str) -> bytes | None:
        """Resolve a slug's root_public_key via ``/certs/sync``,
        cached per slug.
        """
        if not slug:
            return None
        if slug in self._inviter_root_cache:
            return self._inviter_root_cache[slug]
        since = 0
        root_pk_b64: str | None = None
        while True:
            data = await self.http.get(f"/certs/sync?slugs={slug}&since={since}")
            for entry in data.get("entries", []):
                if entry.get("kind") == "identity_cert":
                    cert = entry.get("cert", {})
                    candidate = cert.get("root_public_key")
                    if isinstance(candidate, str) and candidate:
                        root_pk_b64 = candidate
                since = entry.get("seq", since)
            if not data.get("has_more"):
                break
        if root_pk_b64 is None:
            return None
        try:
            root_pk = base64url_decode(root_pk_b64)
        except Exception:
            return None
        if len(root_pk) != 32:
            return None
        self._inviter_root_cache[slug] = root_pk
        return root_pk

    async def _fetch_display_name(self, slug: str) -> str:
        """display_name via /identities/profiles, cached per session.
        Empty string on miss/failure; caller falls back to ``@slug``."""
        if not slug:
            return ""
        if slug in self._display_name_cache:
            return self._display_name_cache[slug]
        try:
            data = await self.http.get(
                f"/identities/profiles?slugs={slug}",
            )
        except Exception:
            self._display_name_cache[slug] = ""
            return ""
        for entry in data.get("profiles") or []:
            if entry.get("slug") == slug:
                name = (entry.get("display_name") or "").strip()
                self._display_name_cache[slug] = name
                return name
        self._display_name_cache[slug] = ""
        return ""

    async def _resolve_space_name(self, space_id: str) -> str:
        """Space name via ``GET /spaces``, cached per session. Returns
        bare ``space_id`` on miss/failure so callers can still render."""
        if not space_id:
            return ""
        if space_id in self._space_name_cache:
            return self._space_name_cache[space_id]
        try:
            data = await self.http.get("/spaces")
        except Exception:
            self._space_name_cache[space_id] = space_id
            return space_id
        name = space_id
        for entry in data.get("spaces") or []:
            if entry.get("space_id") == space_id:
                name = (entry.get("name") or "").strip() or space_id
                break
        self._space_name_cache[space_id] = name
        return name

    async def _resolve_channel_name(
        self, space_id: str, channel_id: str,
    ) -> str:
        """Channel name by replaying ``create_channel`` events under
        ``/spaces/<space_id>/events``. Cached per session; returns
        bare ``channel_id`` on miss/failure or when ``space_id`` is
        absent (DMs)."""
        if not channel_id or not space_id:
            return channel_id
        if channel_id in self._channel_name_cache:
            return self._channel_name_cache[channel_id]
        cursor: str | None = None
        prev_cursor: str | None = None
        name = channel_id
        try:
            while True:
                # Server's query parameter is ``since`` (see
                # ``SpaceEventsParams`` in puffo-server
                # membership.rs). Earlier copies of this client used
                # ``cursor=``, which axum's Query extractor silently
                # ignored — every paginated request came back with
                # the first page, ``has_more: true``, and the same
                # cursor, looping forever (the web client had the
                # same bug; see commit 5b031cc in puffo-core-han-group).
                path = f"/spaces/{space_id}/events"
                if cursor:
                    path += f"?since={cursor}"
                page = await self.http.get(path)
                for ev in page.get("events") or []:
                    if ev.get("kind") != "create_channel":
                        continue
                    payload = ev.get("payload") or {}
                    if payload.get("channel_id") == channel_id:
                        name = (payload.get("name") or "").strip() or channel_id
                        break
                if name != channel_id or not page.get("has_more"):
                    break
                prev_cursor = cursor
                cursor = page.get("next_cursor")
                if not cursor:
                    break
                # Defensive: a server-side regression that ignores
                # ``since`` would echo the same cursor back and wedge
                # this loop. Bail rather than spin.
                if cursor == prev_cursor:
                    break
        except Exception:
            pass
        self._channel_name_cache[channel_id] = name
        return name

    async def _accept_invite(
        self,
        kind: str,
        invitation_event_id: str,
        space_id: str,
        channel_id: str,
    ) -> None:
        """Build + post an accept event. The server matches it to
        the pending invite by ``invitation_event_id``."""
        sess = self.keystore.load_session(self.slug)
        signing_key = Ed25519KeyPair.from_secret_bytes(
            decode_secret(sess.subkey_secret_key)
        )
        now_ms = int(__import__("time").time() * 1000)
        if kind == "invite_to_space":
            payload: dict[str, Any] = {
                "space_id": space_id,
                "invitation_event_id": invitation_event_id,
                "accepted_at": now_ms,
                "nonce": random_nonce(),
            }
            accept_kind = "accept_space_invite"
        else:  # invite_to_channel
            payload = {
                "space_id": space_id,
                "channel_id": channel_id,
                "invitation_event_id": invitation_event_id,
                "accepted_at": now_ms,
                "nonce": random_nonce(),
            }
            accept_kind = "accept_channel_invite"
        signed = sign_event(
            kind=accept_kind,
            payload=payload,
            signer_slug=self.slug,
            signer_device_id=self.device_id,
            signer_subkey_id=sess.subkey_id,
            signing_key=signing_key,
        )
        await self.http.post(
            "/spaces/events",
            {"space_id": space_id, "events": [signed]},
        )

    async def _maybe_handle_invite_reply(
        self, *, thread_root_id: str, text: str,
    ) -> bool:
        """Match the operator's DM reply against the ``y``/``n``
        contract for outstanding invite-DMs. Returns ``True`` when
        consumed (caller skips the LLM); ``False`` for anything that
        isn't an accept/reject directive. Whitespace-tolerant; both
        ``y``/``yes`` and ``n``/``no`` accepted in either case.
        """
        meta = self._pending_invite_dms.get(thread_root_id)
        if meta is None:
            return False
        normalized = text.strip().lower()
        if normalized in ("y", "yes"):
            verdict = "accept"
        elif normalized in ("n", "no"):
            verdict = "reject"
        else:
            return False

        kind = meta["kind"]
        invitation_event_id = meta["invitation_event_id"]
        space_id = meta["space_id"]
        channel_id = meta.get("channel_id") or ""
        inviter_slug = meta.get("inviter_slug") or "?"
        space_name = meta.get("space_name") or None
        channel_name = meta.get("channel_name") or None
        space_label = (
            f"**{space_name}**({space_id})" if space_name else space_id
        )
        if kind == "invite_to_channel":
            channel_label = (
                f"**{channel_name}**({channel_id})" if channel_name else channel_id
            )
            target = f"channel {channel_label} in space {space_label}"
        else:
            target = f"space {space_label}"
        # Pretty inviter label for confirmation; cached from the
        # original DM lookup.
        inviter_display = await self._fetch_display_name(inviter_slug)
        inviter_label = (
            f"**{inviter_display}**(@{inviter_slug})"
            if inviter_display else f"@{inviter_slug}"
        )

        if verdict == "accept":
            try:
                await self._accept_invite(
                    kind, invitation_event_id, space_id, channel_id,
                )
                confirm = f"Accepted invite to {target}. ✓"
                logger.info(
                    "operator-confirmed accept of %s (event_id=%s)",
                    kind, invitation_event_id,
                )
            except Exception as exc:
                logger.exception(
                    "operator-confirmed accept of %s (event_id=%s) failed",
                    kind, invitation_event_id,
                )
                confirm = f"Couldn't accept invite to {target}: {exc}"
        else:  # reject
            try:
                await self._reject_invite(
                    kind, invitation_event_id, space_id, channel_id,
                )
                confirm = f"Rejected invite from {inviter_label} to {target}."
                logger.info(
                    "operator-confirmed reject of %s (event_id=%s)",
                    kind, invitation_event_id,
                )
            except Exception as exc:
                logger.exception(
                    "operator-confirmed reject of %s (event_id=%s) failed",
                    kind, invitation_event_id,
                )
                confirm = f"Couldn't reject invite to {target}: {exc}"

        # Drop from pending so a duplicate ``y`` later in the same
        # thread doesn't re-attempt (server would reject it anyway).
        self._pending_invite_dms.pop(thread_root_id, None)
        try:
            await self._send_dm(
                self.operator_slug, confirm, root_id=thread_root_id,
            )
        except Exception:
            logger.exception(
                "failed to confirm invite-reply outcome to operator",
            )
        return True

    async def _reject_invite(
        self,
        kind: str,
        invitation_event_id: str,
        space_id: str,
        channel_id: str,
    ) -> None:
        """Build + post a reject event. Mirrors ``_accept_invite``
        with ``reject_*`` payload kinds."""
        sess = self.keystore.load_session(self.slug)
        signing_key = Ed25519KeyPair.from_secret_bytes(
            decode_secret(sess.subkey_secret_key)
        )
        now_ms = int(__import__("time").time() * 1000)
        if kind == "invite_to_space":
            payload: dict[str, Any] = {
                "space_id": space_id,
                "invitation_event_id": invitation_event_id,
                "rejected_at": now_ms,
                "nonce": random_nonce(),
            }
            reject_kind = "reject_space_invite"
        else:  # invite_to_channel
            payload = {
                "space_id": space_id,
                "channel_id": channel_id,
                "invitation_event_id": invitation_event_id,
                "rejected_at": now_ms,
                "nonce": random_nonce(),
            }
            reject_kind = "reject_channel_invite"
        signed = sign_event(
            kind=reject_kind,
            payload=payload,
            signer_slug=self.slug,
            signer_device_id=self.device_id,
            signer_subkey_id=sess.subkey_id,
            signing_key=signing_key,
        )
        await self.http.post(
            "/spaces/events",
            {"space_id": space_id, "events": [signed]},
        )

    async def _notify_operator_of_invite(
        self,
        *,
        kind: str,
        inviter_slug: str,
        space_id: str,
        channel_id: str,
        invitation_event_id: str,
        space_name: str | None = None,
        channel_name: str | None = None,
    ) -> None:
        """DM the operator about an invite we won't auto-accept.
        Falls back to logging when no ``operator_slug`` is configured.
        ``space_name``/``channel_name`` are best-effort; missing
        labels degrade to bare IDs.
        """
        if not self.operator_slug:
            logger.warning(
                "received %s from non-operator %s but no operator_slug "
                "configured — leaving invite pending (event_id=%s)",
                kind, inviter_slug, invitation_event_id,
            )
            return
        # ``**...**`` renders bold in the web client. Apply only to
        # names; bare IDs stay un-styled to avoid noise.
        inviter_display = await self._fetch_display_name(inviter_slug)
        inviter_label = (
            f"**{inviter_display}**(@{inviter_slug})"
            if inviter_display else f"@{inviter_slug}"
        )
        space_label = f"**{space_name}**({space_id})" if space_name else space_id
        if kind == "invite_to_space":
            text = (
                f"{inviter_label} invited me to space {space_label}. "
                f"They aren't my registered operator. "
                f"Reply `y` to accept or `n` to reject in this thread."
            )
        else:
            channel_label = (
                f"**{channel_name}**({channel_id})" if channel_name else channel_id
            )
            text = (
                f"{inviter_label} invited me to channel {channel_label} in "
                f"space {space_label}. They aren't my registered operator. "
                f"Reply `y` to accept or `n` to reject in this thread."
            )
        try:
            envelope = await self._send_dm(self.operator_slug, text, root_id="")
        except Exception:
            logger.exception(
                "failed to DM operator about invite from %s (event_id=%s)",
                inviter_slug, invitation_event_id,
            )
            return
        # Track DM by envelope_id so a later ``y``/``n`` thread reply
        # can be intercepted before reaching the LLM. envelope_id is
        # what the operator's client copies into ``thread_root_id``.
        if envelope is not None:
            env_id = envelope.get("envelope_id", "")
            if env_id:
                self._pending_invite_dms[env_id] = {
                    "kind": kind,
                    "invitation_event_id": invitation_event_id,
                    "inviter_slug": inviter_slug,
                    "space_id": space_id,
                    "channel_id": channel_id,
                    "space_name": space_name,
                    "channel_name": channel_name,
                }

    async def _save_inbound_attachments(
        self, *, envelope_id: str, metas_raw: list,
    ) -> list[str]:
        """Decrypt + save each attachment to ``<workspace>/.puffo/
        inbox/<envelope_id>/<filename>`` and return absolute paths.
        Per-attachment failures are logged and skipped, not fatal.

        Strips a multipart/form-data wrapper when a legacy sender
        encrypted the form body instead of the file bytes.
        """
        if not self.workspace or not metas_raw:
            return []
        from pathlib import Path
        from ..crypto.attachments import (
            AttachmentMeta, decrypt_attachment,
        )
        inbox = Path(self.workspace) / ".puffo" / "inbox" / envelope_id
        inbox.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for raw in metas_raw:
            if not isinstance(raw, dict):
                continue
            try:
                meta = AttachmentMeta.from_dict(raw)
            except Exception:
                logger.warning("attachment meta parse failed: %r", raw)
                continue
            ciphertext = await _fetch_blob_with_retry(
                self.http, meta.blob_id,
            )
            if ciphertext is None:
                continue
            try:
                plaintext = decrypt_attachment(ciphertext, meta)
            except Exception as exc:
                logger.warning(
                    "attachment decrypt failed (%s/%s): %s",
                    meta.blob_id, meta.filename, exc,
                )
                continue
            plaintext = _strip_multipart_wrapper(plaintext)
            # Sanitise filename to block ``../`` / absolute-path
            # write-outside-inbox via a malicious sender.
            safe_name = Path(meta.filename).name or meta.blob_id
            target = inbox / safe_name
            try:
                target.write_bytes(plaintext)
            except OSError as exc:
                logger.warning(
                    "attachment save failed (%s): %s", target, exc,
                )
                continue
            paths.append(str(target))
        return paths

    async def _send_dm(
        self, recipient_slug: str, text: str, root_id: str,
    ) -> dict | None:
        """Send a DM to a specific slug (rather than to
        ``_last_dm_sender`` like ``post_message`` does). Returns the
        encrypted envelope on success (caller can read its
        envelope_id), or ``None`` when the recipient has no
        resolvable devices.
        """
        if recipient_slug == self.slug:
            return None
        sess = self.keystore.load_session(self.slug)
        signing_key = Ed25519KeyPair.from_secret_bytes(
            decode_secret(sess.subkey_secret_key)
        )
        devices = await self._fetch_device_keys([self.slug, recipient_slug])
        if not devices:
            logger.warning(
                "no recipient devices for DM to %s — dropping", recipient_slug,
            )
            return None
        inp = EncryptInput(
            envelope_kind="dm",
            sender_slug=self.slug,
            sender_subkey_id=sess.subkey_id,
            recipient_slug=recipient_slug,
            thread_root_id=root_id if root_id else None,
            content_type="text/plain",
            content=text,
            recipients=devices,
        )
        envelope = encrypt_message(inp, signing_key)
        try:
            await self.http.post("/messages", envelope)
        except HttpError:
            logger.exception("DM send to %s failed", recipient_slug)
            raise
        return envelope

    async def _fetch_device_keys(
        self, slugs: list[str],
    ) -> list[RecipientDevice]:
        """Paginate /certs/sync to collect ``(device_id, kem_pk)``
        for every device of every slug. Uses the comma-joined slug
        filter so one round returns device_certs for the whole set.
        """
        if not slugs:
            return []
        slugs_param = ",".join(slugs)
        devices: list[RecipientDevice] = []
        seen_ids: set[str] = set()
        since = 0
        while True:
            data = await self.http.get(
                f"/certs/sync?slugs={slugs_param}&since={since}"
            )
            for entry in data.get("entries", []):
                if entry.get("kind") == "device_cert":
                    cert = entry.get("cert", {})
                    dev_id = cert.get("device_id", "")
                    # v2 nests the encryption pubkey under
                    # ``keys.encryption.public_key``; v1 had a flat
                    # ``kem_public_key``. Prefer v2, fall back for
                    # legacy registry rows.
                    keys_block = cert.get("keys") or {}
                    enc_block = keys_block.get("encryption") or {}
                    kem_b64 = enc_block.get("public_key") or cert.get("kem_public_key", "")
                    if dev_id and kem_b64 and dev_id not in seen_ids:
                        try:
                            devices.append(RecipientDevice(
                                device_id=dev_id,
                                kem_public_key=base64url_decode(kem_b64),
                            ))
                            seen_ids.add(dev_id)
                        except Exception:
                            # Bad base64 in registry — skip rather than abort.
                            pass
                since = entry.get("seq", since)
            if not data.get("has_more"):
                break
        return devices

    async def post_message(
        self, channel_id: str, text: str, root_id: str = "",
    ) -> None:
        """Post a reply. Empty ``channel_id`` ⇒ DM back to
        ``_last_dm_sender``.

        Non-empty ``channel_id``: channel reply; recipients are the
        channel members resolved via /spaces/.../members + /certs/sync.
        Empty ``channel_id``: DM reply; recipients are me and the peer
        so the agent's other devices see the fan-out.
        """
        sess = self.keystore.load_session(self.slug)
        signing_key = Ed25519KeyPair.from_secret_bytes(
            decode_secret(sess.subkey_secret_key)
        )

        if channel_id:
            # Channel reply — prefer the space learned from the inbound
            # envelope (so cross-space channels work) and fall back to
            # the configured home space.
            target_space_id = self._channel_space.get(channel_id, self.space_id)
            members_resp = await self.http.get(
                f"/spaces/{target_space_id}/channels/{channel_id}/members"
            )
            member_slugs = [
                m.get("slug", "")
                for m in members_resp.get("members", [])
                if m.get("slug")
            ]
            if not member_slugs:
                logger.warning(
                    "channel %s has no members — dropping reply", channel_id,
                )
                return
            devices = await self._fetch_device_keys(member_slugs)
            envelope_kind = "channel"
            recipient_slug: Optional[str] = None
            send_space_id: Optional[str] = target_space_id
            send_channel_id: Optional[str] = channel_id
        else:
            # DM reply — route to whoever just DMed us.
            recipient = self._last_dm_sender
            if not recipient:
                logger.warning(
                    "post_message called with empty channel_id but no DM "
                    "context — dropping reply",
                )
                return
            devices = await self._fetch_device_keys([self.slug, recipient])
            envelope_kind = "dm"
            recipient_slug = recipient
            send_space_id = None
            send_channel_id = None

        if not devices:
            logger.warning(
                "no recipient devices found (kind=%s target=%s) — dropping",
                envelope_kind, recipient_slug or channel_id,
            )
            return

        logger.info(
            "post_message: kind=%s target=%s devices=%d",
            envelope_kind, recipient_slug or channel_id, len(devices),
        )

        inp = EncryptInput(
            envelope_kind=envelope_kind,
            sender_slug=self.slug,
            sender_subkey_id=sess.subkey_id,
            space_id=send_space_id,
            channel_id=send_channel_id,
            recipient_slug=recipient_slug,
            thread_root_id=root_id if root_id else None,
            content_type="text/plain",
            content=text,
            recipients=devices,
        )
        envelope = encrypt_message(inp, signing_key)
        # POST /messages takes the envelope at the top level, not
        # wrapped in ``{"envelope": ...}``.
        try:
            resp = await self.http.post("/messages", envelope)
            logger.info(
                "post_message sent: envelope_id=%s queued=%s",
                envelope.get("envelope_id"),
                (resp or {}).get("devices_queued"),
            )
        except Exception:
            logger.exception("post_message: POST /messages failed")
            raise

        # Persist the outbound message locally so the agent's own
        # replies appear in ``get_channel_history``. The WS echo path
        # can't do this (it drops ``sender_slug == self.slug``
        # envelopes to avoid retrigger loops, and the wire payload is
        # encrypted) so we mirror the inbound write here with the
        # plaintext we already have.
        try:
            await self.store.store({
                "envelope_id": envelope["envelope_id"],
                "envelope_kind": envelope_kind,
                "sender_slug": self.slug,
                "channel_id": send_channel_id,
                "space_id": send_space_id,
                "recipient_slug": recipient_slug,
                "content_type": "text/plain",
                "content": text,
                "sent_at": envelope.get("sent_at"),
                "thread_root_id": root_id if root_id else None,
            })
        except Exception:
            # Local-write failure must not fail the send — the
            # recipient already has the message.
            logger.exception(
                "post_message: failed to persist outbound envelope %s",
                envelope.get("envelope_id"),
            )

    async def send_typing(self, channel_id: str, parent_id: str) -> None:
        pass

    async def stop(self) -> None:
        if self._ws:
            self._ws.stop()
        await self.store.close()
