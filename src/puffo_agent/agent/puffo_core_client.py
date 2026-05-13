"""Bridge between puffo-core WS/HTTP and the worker's on_message
interface. Handles message reception, decryption, local storage,
and encrypted reply posting.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
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


@dataclass
class _ThreadEntry:
    """Per-thread queue state.

    The PriorityQueue itself stores ``(priority, seq, root_id)``
    tuples so the heap can order by priority and break ties on
    monotonic seq (dicts aren't orderable). The real per-thread
    state lives here, keyed by ``root_id``:

    - ``messages`` holds every decoded message dict for this
      thread that has been enqueued but not yet dispatched. The
      consumer drains the whole list in a single ``on_message_batch``
      call, so messages that arrived between the first enqueue and
      the eventual pop all reach the agent as one turn.
    - ``current_priority`` / ``current_seq`` are the priority and
      seq currently active in the queue. When a new arrival on the
      same root bumps priority, we DON'T remove the stale heap
      entry (``asyncio.PriorityQueue`` doesn't support that); we
      push a fresh tuple and let the consumer drop the old one when
      it pops and notices ``seq`` no longer matches the entry's
      ``current_seq``.
    - ``in_queue`` flips False between successful dispatch and the
      next arrival on this root, so a later message reopens the
      entry cleanly instead of stacking into a stale batch.
    - ``channel_meta`` is captured on the first enqueue and reused
      on dispatch; the thread/channel/space context is invariant
      for a given root.
    - ``dispatching_ids`` is the set of envelope_ids currently being
      sent to the agent (the batch the consumer just claimed but
      hasn't finished). A duplicate WS delivery that lands during
      that window can't be caught by the handle_envelope cursor
      (cursor advances only after dispatch succeeds) or by the
      in-memory dedup (the consumer just emptied ``messages`` to
      claim the batch), so the reopen branch of
      ``_admit_thread_message`` would otherwise put the duplicate
      into a fresh batch — causing the agent to see the same post
      across two turns. This set is checked at admit time and
      cleared on successful cursor advance.
    """
    current_priority: int
    current_seq: int
    messages: list[dict] = field(default_factory=list)
    in_queue: bool = True
    channel_meta: dict = field(default_factory=dict)
    dispatching_ids: set[str] = field(default_factory=set)


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
        on_api_error_retry: Callable[..., Coroutine[Any, Any, Any]] | None = None,
    ) -> None:
        """``on_message`` is the thread-batch callback. Despite the
        legacy parameter name kept for caller compatibility, it's
        invoked as ``on_message(root_id, batch, channel_meta)`` —
        the consumer collapses every arrival on the same thread
        into a single dispatch (see ``_consume_queue``).

        ``on_api_error_retry`` is the kick-retry callback invoked
        after the consumer catches an ``AgentAPIError``. Same
        signature, but the implementation is expected to nudge
        claude-code via ``--resume`` rather than re-sending the
        ``batch`` payload (so the agent's transcript doesn't pick up
        a duplicate of the original user input on every retry). When
        omitted, the consumer abandons the batch on first failure.
        """
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
            # Self-envelopes are NOT dropped at the door anymore (Han
            # 2026-05-13). The server fans out every recipient device
            # in ``envelope.recipients``, which always includes the
            # agent's own device (the MCP send_message tool puts it
            # in the recipient list for both DMs and channels), so
            # the WS echo IS the canonical "this message was actually
            # delivered" signal. We persist it through the same path
            # every other message goes through; the dispatch-to-
            # worker step below short-circuits on ``sender_slug ==
            # self.slug`` to prevent a retrigger loop.
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

            # Self-echo lands here too now (see ``handle_envelope``'s
            # opening comment). Persist it — so ``get_channel_history``
            # / ``get_thread_history`` show the agent's own posts —
            # then stop before any of the LLM-facing pipeline below
            # runs. The agent already produced this message; queueing
            # it again would feed the agent its own words and trip a
            # turn-by-turn echo loop.
            if payload.sender_slug == self.slug:
                return

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

            direct = is_dm or is_mention
            sender_is_bot = False  # puffo-core has no is_bot flag yet
            priority = _compute_priority(direct, sender_is_bot)

            # Thread-batched queue: every message coalesces under
            # its ``root_id`` (the envelope's ``thread_root_id``, or
            # the message itself when it's a top-level post). The
            # PriorityQueue holds one slot per root; new arrivals on
            # the same thread either join the existing batch
            # (priority same or lower) or bump the slot to the new
            # higher priority. The agent processes one whole thread
            # at a time in ``on_message_batch``.
            root_id = payload.thread_root_id or payload.envelope_id

            # Cross-restart dedup: after a daemon restart the server
            # redelivers anything in /messages/pending. If we already
            # dispatched a batch that covers ``payload.sent_at``,
            # skip — the agent has seen this.
            last_processed = await self.store.get_last_processed_sent_at(root_id)
            if payload.sent_at <= last_processed:
                logger.info(
                    "handle_envelope: cursor-rejected duplicate envelope=%s "
                    "(sent_at=%d <= last_processed=%d, root=%s)",
                    payload.envelope_id, payload.sent_at,
                    last_processed, root_id,
                )
                return

            # Per-session display-name cache turns this into ~1 HTTP
            # call per distinct sender per session; same helper the
            # invite-DM flow already uses. Empty string on miss.
            sender_display_name = await self._fetch_display_name(
                payload.sender_slug,
            )

            msg_dict = {
                "channel_id": channel_id,
                "channel_name": channel_name,
                "space_id": space_id,
                "space_name": space_name,
                "sender_slug": payload.sender_slug,
                "sender_display_name": sender_display_name,
                "sender_email": "",
                "text": clean_text,
                "root_id": payload.thread_root_id or "",
                "is_dm": is_dm,
                "attachments": attachment_paths,
                "sender_is_bot": sender_is_bot,
                "mentions": mentions,
                "envelope_id": payload.envelope_id,
                "sent_at": payload.sent_at,
            }
            channel_meta = {
                "channel_id": channel_id,
                "channel_name": channel_name,
                "space_id": space_id,
                "space_name": space_name,
                "is_dm": is_dm,
            }

            await self._admit_thread_message(
                root_id=root_id,
                priority=priority,
                msg_dict=msg_dict,
                channel_meta=channel_meta,
            )

        # Per-listen() queue. A reconnect drops any envelopes not yet
        # drained; the server redelivers via /messages/pending on the
        # next subscribe, and the sqlite ``thread_processing_state``
        # cursor keeps the agent from re-running on threads it already
        # handled before the restart.
        self._queue = asyncio.PriorityQueue()
        self._queue_seq = 0
        self._thread_state: dict[str, _ThreadEntry] = {}
        # Reset on every (re)connect — the auto-accept path is
        # idempotent against server-side state.
        self._processed_invite_ids = set()
        consumer_task = asyncio.ensure_future(
            self._consume_queue(on_message, on_api_error_retry),
        )
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

    async def _admit_thread_message(
        self,
        *,
        root_id: str,
        priority: int,
        msg_dict: dict,
        channel_meta: dict,
    ) -> None:
        """Add (or coalesce) a message into the thread-batched queue.

        Cases (all keyed on ``root_id``):

        - **New root** (no entry yet): build a fresh ``_ThreadEntry``
          with this message as the only element and push a heap tuple.
        - **Reopen** (entry exists but ``in_queue`` is False — a
          previous batch was already dispatched): reset the entry with
          this message as the new cursor and push a fresh heap tuple.
        - **In-queue, same-or-lower priority** (priority numeric value
          unchanged or larger): append to the existing batch. The
          cursor (``messages[0]``) stays pinned; the heap tuple
          doesn't move.
        - **In-queue, higher priority** (smaller numeric value): append
          to the batch AND push a new heap tuple with the upgraded
          priority and a fresh seq. The old tuple is left in the heap
          and gets skipped on pop via the ``current_seq`` mismatch
          check in ``_consume_queue``.

        Caller must have already filtered out messages whose
        ``sent_at`` is at or below
        ``store.get_last_processed_sent_at(root_id)``; this method
        doesn't re-check the durable cursor.
        """
        entry = self._thread_state.get(root_id)
        incoming_id = msg_dict.get("envelope_id", "")

        # Cross-batch dedup: skip a duplicate of an envelope the
        # consumer just claimed and is sending to the agent right
        # now. Without this, the reopen branch below would create a
        # fresh ``entry.messages = [dup]`` slot and the duplicate
        # gets dispatched as the next batch — agent sees the same
        # envelope across two turns. See ``_ThreadEntry`` docstring.
        if entry is not None and incoming_id and incoming_id in entry.dispatching_ids:
            logger.info(
                "_admit_thread_message: dispatching_ids-rejected duplicate "
                "envelope=%s root=%s", incoming_id, root_id,
            )
            return

        if entry is None or not entry.in_queue:
            self._queue_seq += 1
            if entry is None:
                entry = _ThreadEntry(
                    current_priority=priority,
                    current_seq=self._queue_seq,
                    messages=[msg_dict],
                    in_queue=True,
                    channel_meta=channel_meta,
                )
                self._thread_state[root_id] = entry
            else:
                entry.current_priority = priority
                entry.current_seq = self._queue_seq
                entry.messages = [msg_dict]
                entry.in_queue = True
                entry.channel_meta = channel_meta
            await self._queue.put((priority, entry.current_seq, root_id))
            return

        # Dedup by envelope_id within the live batch. The same wire
        # envelope can land here twice when the server's pending-
        # message redelivery overlaps with live WS delivery (e.g.
        # after a daemon restart or brief WS reconnect). sqlite-side
        # INSERT OR IGNORE in MessageStore.store already covers the
        # durable table, but the in-memory batch is independent and
        # would otherwise feed the agent the same post N times.
        if incoming_id and any(
            m.get("envelope_id") == incoming_id for m in entry.messages
        ):
            logger.info(
                "_admit_thread_message: in-batch-rejected duplicate "
                "envelope=%s root=%s (pending batch=%d)",
                incoming_id, root_id, len(entry.messages),
            )
            return

        entry.messages.append(msg_dict)
        if priority < entry.current_priority:
            self._queue_seq += 1
            entry.current_priority = priority
            entry.current_seq = self._queue_seq
            await self._queue.put((priority, entry.current_seq, root_id))

    # Upper bound on consecutive AgentAPIError retries for a single
    # batch before we give up. claude-code's rate limit usually lifts
    # well inside 3 × (15-45s) of backoff; staying any longer is
    # bad for everything else queued behind this thread.
    MAX_API_ERROR_RETRIES = 3

    async def _do_api_error_retries(
        self,
        *,
        root_id: str,
        entry: "_ThreadEntry",
        batch: list[dict],
        channel_meta: dict,
        on_api_error_retry: Optional[Callable[..., Coroutine[Any, Any, Any]]],
        last_envelope: str,
    ) -> None:
        """Loop the API-Error kick-retry path until the agent
        succeeds, the retry cap is hit, or a non-retry exception
        bubbles up. The original ``batch`` doesn't go back into
        ``entry.messages`` — the kick path tells claude-code (via
        --resume) to re-attempt its previous turn; if --resume is
        gone, the adapter falls back to the payload internally.

        Mid-dispatch arrivals: any new messages that landed on the
        same thread during the failed dispatch were admitted via
        ``_admit_thread_message``'s reopen branch and are now in
        ``entry.messages`` with a fresh queue tuple. We don't touch
        them; the consumer picks them up after this retry loop
        exits.

        Cursor is NOT advanced on the failed batch — if a later
        message succeeds on this thread it'll mark a higher
        ``sent_at`` past the failed one, effectively leaving the
        failed envelope readable via ``get_channel_history`` for the
        agent if it wants to backfill.
        """
        # Reset the in-flight set for this slot. The failed batch is
        # no longer "currently dispatching"; future duplicates of
        # those envelopes need to be caught by the cursor (advanced
        # by the kick's successful dispatch) or by in-batch dedup.
        entry.dispatching_ids = set()

        if on_api_error_retry is None:
            logger.warning(
                "agent reply contained 'API Error' for thread %s "
                "(last envelope %s); no retry callback wired, "
                "abandoning batch",
                root_id, last_envelope,
            )
            return

        for attempt in range(1, self.MAX_API_ERROR_RETRIES + 1):
            delay = random.uniform(15.0, 45.0)
            logger.warning(
                "agent reply contained 'API Error' for thread %s "
                "(last envelope %s); kick-retry %d/%d in %.1fs",
                root_id, last_envelope, attempt,
                self.MAX_API_ERROR_RETRIES, delay,
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            try:
                await on_api_error_retry(root_id, batch, channel_meta)
                # Success path: no exception. The agent processed
                # the retry. Advance cursor past the failed batch
                # so we don't re-trigger on redelivery.
                if batch:
                    tail_sent_at = batch[-1].get("sent_at", 0)
                    try:
                        await self.store.mark_thread_processed(
                            root_id, tail_sent_at,
                        )
                    except Exception:
                        logger.exception(
                            "mark_thread_processed(%s, %d) failed "
                            "after kick-retry; agent may re-process "
                            "after restart",
                            root_id, tail_sent_at,
                        )
                logger.info(
                    "agent thread %s recovered after kick-retry %d/%d",
                    root_id, attempt, self.MAX_API_ERROR_RETRIES,
                )
                return
            except AgentAPIError:
                # Still rate-limited; loop with another backoff.
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "kick-retry %d/%d for thread %s raised; abandoning",
                    attempt, self.MAX_API_ERROR_RETRIES, root_id,
                )
                return
        logger.warning(
            "agent thread %s exhausted %d kick-retries (last envelope %s); "
            "abandoning the batch — agent will see these messages via "
            "get_channel_history on the next dispatch",
            root_id, self.MAX_API_ERROR_RETRIES, last_envelope,
        )

    async def _consume_queue(
        self,
        on_message_batch: Callable[..., Coroutine[Any, Any, Any]],
        on_api_error_retry: Callable[..., Coroutine[Any, Any, Any]] | None = None,
    ) -> None:
        """Drain the priority queue serially. One turn at a time so
        the underlying session keeps a coherent conversation history;
        concurrent turns would interleave context.

        Each pop yields a ``root_id``. We look up the per-thread
        entry, drop the message batch out (claim it), and dispatch
        the whole list as one agent invocation. While the agent is
        running, new arrivals on the same thread create a fresh
        entry that will be picked up on the next pop — the agent
        won't re-see messages it already processed.
        """
        while True:
            try:
                _priority, popped_seq, root_id = await self._queue.get()
            except asyncio.CancelledError:
                return

            entry = self._thread_state.get(root_id)
            if entry is None or not entry.in_queue or entry.current_seq != popped_seq:
                # Stale heap entry: a higher-priority arrival
                # already pushed a fresh tuple for this root, or
                # the slot has been closed since we popped. Drop
                # and continue.
                continue

            # Claim the batch atomically — mark the slot closed
            # before dispatch so concurrent arrivals open a fresh
            # entry instead of stacking into a batch the agent is
            # already chewing through. Record the batch's
            # envelope_ids in ``dispatching_ids`` so that a duplicate
            # arriving mid-dispatch can be rejected at admit time
            # (the durable cursor hasn't advanced yet, so it can't
            # catch it).
            batch = entry.messages
            channel_meta = entry.channel_meta
            entry.messages = []
            entry.in_queue = False
            entry.dispatching_ids = {
                m.get("envelope_id") for m in batch if m.get("envelope_id")
            }

            # Safety net: paranoid in-batch dedup right before
            # dispatch. ``_admit_thread_message``'s in-queue dedup
            # plus ``dispatching_ids`` should already guarantee
            # ``batch`` is duplicate-free, but if some upstream race
            # we haven't characterised slips a duplicate envelope_id
            # past both, we must NOT hand the same envelope to the
            # agent twice in one turn. The warning log lets us spot
            # the offending path if it ever fires.
            seen_ids: set[str] = set()
            deduped: list[dict] = []
            dropped: list[str] = []
            for m in batch:
                mid = m.get("envelope_id", "")
                if mid and mid in seen_ids:
                    dropped.append(mid)
                    continue
                if mid:
                    seen_ids.add(mid)
                deduped.append(m)
            if dropped:
                logger.warning(
                    "consumer dropped %d in-batch duplicate envelope_id(s) "
                    "for thread %s before dispatch: %s",
                    len(dropped), root_id, dropped,
                )
                batch = deduped

            # Pre-dispatch jitter. When several agents on the same
            # host get activated by the same message (e.g. a channel
            # broadcast), unblocked dispatch sends them all into the
            # claude-code API at once and trips its rate limit. A
            # 0.0–1.5s random sleep here desynchronises them; each
            # agent picks independently. Messages that arrive during
            # the sleep land in the next batch because in_queue is
            # already False.
            try:
                await asyncio.sleep(random.uniform(0.0, 1.5))
            except asyncio.CancelledError:
                raise

            try:
                await on_message_batch(root_id, batch, channel_meta)
            except AgentAPIError:
                # Adapter surfaced "API Error" — most commonly
                # claude-code's transient rate-limit error. The
                # original user input is already in claude-code's
                # ``--resume``-backed transcript from the failed
                # dispatch; the retry just needs to nudge the agent
                # to try again, NOT re-send the payload (which would
                # accumulate visible duplicates in the agent's
                # transcript on every retry).
                #
                # The retry path uses a small kick message ("session
                # errored on rate limiting, please resume processing")
                # via ``on_api_error_retry``; if ``--resume`` is no
                # longer valid the adapter falls back to the original
                # payload on its own so the agent has something to
                # work from.
                last_envelope = batch[-1].get("envelope_id", "") if batch else ""
                await self._do_api_error_retries(
                    root_id=root_id,
                    entry=entry,
                    batch=batch,
                    channel_meta=channel_meta,
                    on_api_error_retry=on_api_error_retry,
                    last_envelope=last_envelope,
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "on_message_batch handler failed for thread %s (%d messages)",
                    root_id, len(batch),
                )
                # Treat poison-message style failures as terminal for
                # the batch — don't loop forever. The sqlite cursor
                # stays untouched so a restart can retry, but we mark
                # the slot done in-memory so live arrivals keep
                # flowing for other threads.
                continue

            # Success: persist the cursor so a restart-then-redeliver
            # doesn't re-trigger this thread. ``batch[-1].sent_at`` is
            # the high-water mark we just covered.
            if batch:
                tail_sent_at = batch[-1].get("sent_at", 0)
                try:
                    await self.store.mark_thread_processed(root_id, tail_sent_at)
                except Exception:
                    logger.exception(
                        "mark_thread_processed(%s, %d) failed; agent "
                        "may re-process this thread after a restart",
                        root_id, tail_sent_at,
                    )
            # Cursor now covers the dispatched batch — duplicates of
            # any of its envelopes will be caught by the handle_envelope
            # cursor check from this point on, so the in-memory
            # dispatching_ids set has done its job and can be released.
            entry.dispatching_ids = set()

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
        payload = event.get("payload") or {}

        if kind in ("invite_to_space", "invite_to_channel"):
            if payload.get("invitee_slug") != self.slug:
                return  # Server fans the event to space members too.
            await self._poll_pending_invites()
            return

        # Server-side auto-accept synthetic event. When the server
        # short-circuits an InviteToChannel because this agent's
        # ``auto_accept_owner_invite`` flag is on AND the inviter is
        # the space owner, it emits an ``accept_channel_invite``
        # signed-as-the-invitee with the original signed invite
        # nested under ``payload.original_invite``. There's no
        # pending_invites row to poll and ``_accept_invite`` never
        # runs, so the self-intro nudge that path normally fires
        # would otherwise be silently dropped on auto-accept.
        # Mirror just the nudge here.
        if kind == "accept_channel_invite":
            if event.get("signer_slug") != self.slug:
                return  # Someone else's accept — not our business.
            # Distinguish the server-emitted synthetic from a
            # real signed accept that's bouncing back over WS.
            # ``original_invite`` is the canonical marker — the
            # operator-signed path never embeds the source invite.
            if not isinstance(payload.get("original_invite"), dict):
                return
            space_id = payload.get("space_id") or ""
            channel_id = payload.get("channel_id") or ""
            if not space_id or not channel_id:
                return
            try:
                await self._enqueue_channel_intro_nudge(
                    space_id=space_id,
                    channel_id=channel_id,
                )
            except Exception:
                logger.exception(
                    "failed to enqueue intro nudge for server-auto-"
                    "accepted channel (space=%s channel=%s)",
                    space_id, channel_id,
                )
            return

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
                # Server expects ``?since=``, not ``?cursor=``.
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
                if not cursor or cursor == prev_cursor:
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

        # Accepting an invite triggers a one-shot self-introduction
        # nudge: we enqueue a synthetic ``[puffo-agent system message]``
        # so the agent posts a short intro using its existing
        # ``send_message`` MCP tool. Channel invites intro into the
        # invited channel; space invites intro into the space's public
        # ``General`` channel (the only channel an accepting member is
        # auto-fanned-out into per puffo-server's space-invite redeem
        # logic — any other channel needs its own invite_to_channel).
        intro_channel_id = ""
        if kind == "invite_to_channel" and channel_id:
            intro_channel_id = channel_id
        elif kind == "invite_to_space":
            try:
                intro_channel_id = await self._find_public_general_channel(
                    space_id,
                )
            except Exception:
                logger.exception(
                    "failed to look up General channel for intro nudge "
                    "(space=%s)", space_id,
                )
        if intro_channel_id:
            try:
                await self._enqueue_channel_intro_nudge(
                    space_id=space_id,
                    channel_id=intro_channel_id,
                )
            except Exception:
                # Intro is best-effort; never block the accept.
                logger.exception(
                    "failed to enqueue channel intro nudge "
                    "(space=%s channel=%s)", space_id, intro_channel_id,
                )

    async def _find_public_general_channel(self, space_id: str) -> str:
        """Return the ``channel_id`` of the space's auto-created public
        General channel (the first ``is_public=true`` row from
        ``GET /spaces/<id>/channels``), or ``""`` if none.

        Used after accepting a space invite to pick the one channel
        the agent now has auto-fanned-out membership in. Side effect:
        every channel returned by the server is folded into
        ``self._channel_name_cache`` so the immediately-following
        ``_resolve_channel_name`` inside the intro nudge becomes a
        cache hit instead of a separate events replay.

        Accept POST → channels GET is a tight race: the server has
        the accept event applied, but the ``channel_memberships``
        rows that gate this endpoint may not be committed yet (the
        endpoint returns 200 + the SPA index when the caller isn't
        yet a member, which the http client decodes as a raw
        ``str``). Sleep-first retry on a generous schedule;
        past ~70s give up silently rather than spinning forever."""
        if not space_id:
            return ""
        retry_delays = (0.5, 1.0, 3.0, 6.0, 12.0, 24.0, 24.0)
        for attempt_idx, delay in enumerate(retry_delays):
            await asyncio.sleep(delay)
            data = await self.http.get(f"/spaces/{space_id}/channels")
            if not isinstance(data, dict):
                logger.info(
                    "channels endpoint not ready for space=%s "
                    "(attempt %d/%d) — retrying",
                    space_id,
                    attempt_idx + 1,
                    len(retry_delays),
                )
                continue
            found_cid = ""
            for entry in data.get("channels") or []:
                cid = entry.get("channel_id") or ""
                name = (entry.get("name") or "").strip()
                if cid and cid not in self._channel_name_cache:
                    self._channel_name_cache[cid] = name or cid
                # Persist the channel→space mapping so the MCP
                # subprocess's send_message can resolve this channel
                # BEFORE the first inbound message lands. Without
                # this, lookup_channel_space falls through to the
                # /messages table (empty) and then to agent.yml's
                # space_id, which is the WRONG space when the agent
                # has just joined a different one.
                if cid:
                    await self.store.mark_channel_space(cid, space_id)
                if not found_cid and entry.get("is_public") and cid:
                    found_cid = cid
            return found_cid
        logger.warning(
            "gave up looking up General channel for space=%s after %d "
            "attempts — intro nudge will be skipped",
            space_id, len(retry_delays),
        )
        return ""

    async def _enqueue_channel_intro_nudge(
        self,
        *,
        space_id: str,
        channel_id: str,
    ) -> None:
        """Inject a synthetic system-message envelope into the thread
        queue asking the agent to post a brief self-introduction in
        ``channel_id``. Idempotent against the
        ``channel_intro_prompted`` sqlite table so a redelivered
        accept can't fire a second intro."""
        if await self.store.has_channel_intro_been_prompted(channel_id):
            return

        space_name = (
            await self._resolve_space_name(space_id) if space_id else ""
        )
        channel_name = await self._resolve_channel_name(space_id, channel_id)

        now_ms = int(__import__("time").time() * 1000)
        envelope_id = f"intro-prompt-{channel_id}-{now_ms}"
        # The prefix is documented in the agent's CLAUDE.md primer as
        # a recognised control-message marker (see 0.7.3 notes); the
        # agent treats it as a directive rather than user chatter.
        prompt_text = (
            "[puffo-agent system message] You've just been added to "
            f"channel #{channel_name} (channel_id: {channel_id}) in "
            f"space {space_name or space_id}. Post a brief "
            "self-introduction (2-3 sentences, in English by default) "
            f"using mcp__puffo__send_message with channel=\"{channel_id}\" "
            "so existing members know who you are and how you can help. "
            "Don't include a thread root_id — this should be a new "
            "top-level post in the channel."
        )

        msg_dict = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "space_id": space_id,
            "space_name": space_name,
            "sender_slug": "system",
            "sender_display_name": "",
            "sender_email": "",
            "text": prompt_text,
            "root_id": "",
            "is_dm": False,
            "attachments": [],
            "sender_is_bot": False,
            "mentions": [],
            "envelope_id": envelope_id,
            "sent_at": now_ms,
        }
        channel_meta = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "space_id": space_id,
            "space_name": space_name,
            "is_dm": False,
        }

        # Mark prompted BEFORE admitting so a crash between admit and
        # commit can't leave us re-prompting on restart. Worst case
        # the agent doesn't get the nudge — preferable to spamming.
        await self.store.mark_channel_intro_prompted(channel_id)

        # Persist the synthetic envelope to ``messages.db`` so the
        # agent can resolve it through its normal data-service paths
        # (``mcp__puffo__get_channel_history`` /
        # ``get_message_by_envelope``). Without this, the envelope
        # only existed in the in-memory thread queue — the agent
        # would see it in the turn prompt, but a follow-up
        # ``get_channel_history`` would return an inconsistent view
        # (intro is missing) and ``send_message(root_id=<intro id>)``
        # would surface as a broken thread reference. Side benefit:
        # ``lookup_channel_space`` learns the channel→space mapping
        # off this envelope automatically.
        store_payload = {
            "envelope_id": envelope_id,
            "envelope_kind": "channel",
            "sender_slug": "system",
            "channel_id": channel_id,
            "space_id": space_id,
            "content_type": "text/plain",
            "content": prompt_text,
            "sent_at": now_ms,
            "thread_root_id": envelope_id,
            "reply_to_id": None,
        }
        try:
            await self.store.store(store_payload)
        except Exception as exc:  # noqa: BLE001
            # Persistence is best-effort — the in-memory queue still
            # delivers the prompt even if sqlite fails (disk full,
            # permission, etc.). Log loud so the operator can spot
            # the inconsistency between prompt + history.
            logger.warning(
                "intro-nudge: failed to persist envelope=%s to messages.db: %s",
                envelope_id, exc,
            )

        await self._admit_thread_message(
            root_id=envelope_id,
            priority=PRIORITY_SYSTEM,
            msg_dict=msg_dict,
            channel_meta=channel_meta,
        )
        logger.info(
            "enqueued channel-intro nudge for channel=%s (space=%s)",
            channel_id, space_id,
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

        # No mirror-write here anymore. The WS echo path now persists
        # self-envelopes through the same handler every other message
        # uses (Han 2026-05-13). Keeping a parallel mirror would double-
        # insert (``INSERT OR IGNORE`` makes it idempotent, but the
        # two write paths diverging is exactly the bug class we just
        # fixed for the MCP ``send_message`` tool — better to have one
        # canonical path).

    async def send_typing(self, channel_id: str, parent_id: str) -> None:
        pass

    async def stop(self) -> None:
        if self._ws:
            self._ws.stop()
        await self.store.close()
