"""Bridge between puffo-core WS/HTTP and the worker's on_message
interface. Handles message reception, decryption, local storage,
and encrypted reply posting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

from ..crypto.encoding import base64url_decode
from ..limits import (
    DEFAULT_CATCHUP_STALE_HOURS,
    MAX_INLINE_MESSAGE_CHARS,
    MESSAGE_SEGMENT_CHARS,
)
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
from . import disk_cache
from ._invite_strings import format_invite_error, format_leave_error
from .core import AgentAPIError
from .permission_prompt import format_permission_prompt
from .events import random_nonce, sign_event
from .event_kinds import EventKind
from .message_store import MessageStore

logger = logging.getLogger(__name__)


class _AgentLogger(logging.LoggerAdapter):
    """Prepends ``[<agent_slug>]`` to every message so multi-agent
    daemon logs can be grepped per agent."""

    def process(self, msg, kwargs):
        return f"[{self.extra['agent']}] {msg}", kwargs


# Mirrors the web client's remark-mentions pattern; non-word
# leader so ``foo@bar-1234`` (an email) doesn't match.
_MENTION_RE = re.compile(
    r"(?:^|\W)@([a-z][a-z0-9-]*-[a-f0-9]{4})", re.IGNORECASE,
)


# Lower number = higher priority — drained first by the consumer loop.
PRIORITY_MENTIONED_HUMAN = 1
PRIORITY_MENTIONED_BOT = 2
PRIORITY_HUMAN = 3
PRIORITY_BOT = 4
PRIORITY_SYSTEM = 5

# Per-slug profile cache TTL — keeps display_name + avatar_url
# fresh enough that a rename / avatar change on puffo-server
# propagates within ~10 min on the next render, without paying
# a /identities/profiles HTTP round-trip on every inbound msg.
# Operators wanting "right now" can fire the MCP get_user_profile
# tool which force-refreshes regardless of TTL.
_PROFILE_CACHE_TTL_SECONDS = 10 * 60

# Mirrors ``adapters/cli_session.MAX_USER_MESSAGE_BYTES``; a test pins them.
DEFAULT_MAX_INPUT_BYTES = 180 * 1000

# Deliberately over-counts the ``_format_user_block`` metadata header so a
# near-boundary split lands one turn early, never over the cap.
_BLOCK_METADATA_OVERHEAD_BYTES = 2048


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


def _compute_priority(direct: bool, sender_is_agent: bool) -> int:
    """Map (direct, sender_is_agent) to one of the PRIORITY_* bands.
    PRIORITY_SYSTEM is reserved for a future service-message envelope.
    """
    if direct and not sender_is_agent:
        return PRIORITY_MENTIONED_HUMAN
    if direct and sender_is_agent:
        return PRIORITY_MENTIONED_BOT
    if not sender_is_agent:
        return PRIORITY_HUMAN
    return PRIORITY_BOT


# Hard cap on the preview length embedded in the redaction
# placeholder. Big enough to convey the message's flavour to the
# agent (so it knows whether to bother fetching segments) but
# small enough that pasting a 100kB log doesn't itself blow the
# prompt budget through the preview alone.
_LONG_MESSAGE_PREVIEW_CHARS = 240


def _maybe_redact_long_text(
    text: str,
    *,
    envelope_id: str,
    sender_slug: str,
    sender_display_name: str,
    max_inline_chars: int,
    segment_chars: int,
    agent_slug: str,
) -> str:
    """Substitute oversize message bodies with a placeholder that
    points the agent at ``get_post_segment``. Anything ≤
    ``max_inline_chars`` is returned untouched.

    The placeholder lives in the *prompt-facing* view of the message
    only — the original ``text`` is still persisted into
    ``messages.db`` via ``store.store()`` upstream of this call, so
    the segment tool can paginate the full body back to the agent
    on demand.

    A daemon log is emitted whenever redaction fires so an operator
    debugging "my agent didn't see my paste" can grep for the
    envelope_id without tailing turn output.
    """
    if not text:
        return text
    total = len(text)
    if total <= max_inline_chars:
        return text

    # Segment count is ceil(total / segment_chars), at least 1.
    seg_count = (total + segment_chars - 1) // segment_chars

    # Preview: the first ~240 chars of the (already-mention-rewritten)
    # text, with line breaks normalised to spaces so the placeholder
    # stays one tidy block. Truncated with an ellipsis so the agent
    # doesn't mistake the cut for the end of the message.
    raw_preview = text[:_LONG_MESSAGE_PREVIEW_CHARS].replace("\n", " ").strip()
    if total > _LONG_MESSAGE_PREVIEW_CHARS:
        raw_preview = raw_preview + "…"

    sender_label = (
        f"@{sender_display_name} ({sender_slug})"
        if sender_display_name
        else f"@{sender_slug}"
    )
    placeholder = (
        "[puffo-agent system message] inbound message was too long "
        "to embed inline and has been redacted from this prompt for "
        "context-budget reasons.\n"
        f"  envelope_id: {envelope_id}\n"
        f"  total_chars: {total}\n"
        f"  segments: {seg_count} (0-indexed, up to {segment_chars} chars each)\n"
        f"  sender: {sender_label}\n"
        f"  preview: {raw_preview}\n"
        "Retrieve the full body one chunk at a time with "
        "mcp__puffo__get_post_segment("
        f"envelope_id=\"{envelope_id}\", segment=N, "
        f"segment_size={segment_chars}) where N runs "
        f"0..{seg_count - 1}. Fetch only the segments you actually "
        "need — the placeholder above already tells you what kind "
        "of content it is."
    )

    logger.info(
        "agent %s: inlined message %s truncated (%d → %d chars, %d segments) "
        "for prompt budget",
        agent_slug, envelope_id, total, max_inline_chars, seg_count,
    )
    return placeholder


# Inbound images are downscaled to the model's native vision resolution at
# save time. The Claude API resizes a single oversized image on its own, but
# a request carrying >20 images rejects any whose longest edge tops 2000px
# ("many-image requests"), and a full-res image costs ~4784 visual tokens on
# Opus 4.7+ — capping on disk avoids both. The native long-edge cap is
# model-specific: Opus 4.7+ resolves 2576px, all other models 1568px.
_DEFAULT_IMAGE_EDGE_PX = 1568
_HIGH_RES_IMAGE_EDGE_PX = 2576
# Model-id substrings that resolve high-resolution vision. Unknown models
# default to 1568 — safe (over-shrinks at worst); add new ones here.
_HIGH_RES_MODEL_MARKERS = ("opus-4-7", "opus-4-8")


def max_image_edge_px(model: str) -> int:
    """Native vision long-edge cap for ``model`` — 2576px for models with
    high-resolution input, else the conservative 1568px default."""
    m = (model or "").lower()
    if any(marker in m for marker in _HIGH_RES_MODEL_MARKERS):
        return _HIGH_RES_IMAGE_EDGE_PX
    return _DEFAULT_IMAGE_EDGE_PX


def _downscale_oversized_image(
    path, original_path=None, max_edge_px: int = _DEFAULT_IMAGE_EDGE_PX,
) -> bool:
    """Resize ``path`` in place when it's an image whose longest edge tops
    ``max_edge_px``; return whether a resize happened. When ``original_path``
    is supplied and the resize fires, the pre-resize bytes are copied there
    first so the agent's file-access tools can reach the full-fidelity
    version. Returns False (no-op) for non-images, small images, or anything
    Pillow can't open. Best-effort — never raises."""
    try:
        from PIL import Image
    except ImportError:
        logger.warning(
            "Pillow missing — inbound images aren't dimension-checked; "
            "a many-image request can then reject an oversized one "
            "(pip install pillow)",
        )
        return False
    try:
        with Image.open(path) as img:
            img.load()
            w, h = img.size
            longest = max(w, h)
            if longest <= max_edge_px:
                return False
            if original_path is not None:
                import shutil
                from pathlib import Path
                try:
                    op = Path(original_path)
                    op.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, op)
                except Exception as exc:  # noqa: BLE001
                    # A failed original-copy must not block the resize — the
                    # in-bounds version is what the agent actually Reads.
                    logger.warning(
                        "could not preserve original image %s: %s",
                        original_path, exc,
                    )
            scale = max_edge_px / longest
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            fmt = img.format or "PNG"
            img.resize(new_size, Image.LANCZOS).save(path, format=fmt)
        logger.info(
            "downscaled inbound image %s: %dx%d -> %dx%d (cap %dpx)",
            getattr(path, "name", path), w, h, new_size[0], new_size[1],
            max_edge_px,
        )
        return True
    except Exception as exc:
        logger.warning("could not dimension-check image %s: %s", path, exc)
        return False


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

    # Class-level fallback so ``__new__``-built test fixtures (which
    # skip ``__init__``) still have a working logger. Real instances
    # override this in ``__init__`` with the agent-slug-prefixed
    # ``_AgentLogger``.
    _log: logging.Logger | logging.LoggerAdapter = logger

    def __init__(
        self,
        slug: str,
        device_id: str,
        space_id: str,
        keystore: KeyStore,
        http_client: PuffoCoreHttpClient,
        message_store: MessageStore,
        operator_slug: str = "",
        auto_accept_space_invitations: bool = False,
        workspace: str = "",
        max_inline_chars: int = MAX_INLINE_MESSAGE_CHARS,
        segment_chars: int = MESSAGE_SEGMENT_CHARS,
        agent_created_at: int = 0,
        image_edge_px: int = _DEFAULT_IMAGE_EDGE_PX,
        max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
        catchup_stale_hours: float = DEFAULT_CATCHUP_STALE_HOURS,
    ):
        self.slug = slug
        self.device_id = device_id
        self.space_id = space_id
        # 0 = legacy pre-created_at agent → skip the fast-phase warm-up.
        self._agent_created_at = int(agent_created_at)
        # Operator's slug — used to DM them on non-auto-acceptable
        # invites. Empty string falls back to log-only handling.
        self.operator_slug = operator_slug
        self.auto_accept_space_invitations = bool(auto_accept_space_invitations)
        # Absolute path to the agent's workspace. Inbound attachments
        # are decrypted into ``<workspace>/.puffo/inbox/<envelope_id>/``.
        self.workspace = workspace
        # Long-message redaction thresholds. When an inbound message's
        # ``text`` field exceeds ``max_inline_chars`` the LLM sees a
        # placeholder pointing at ``get_post_segment`` instead of the
        # raw body. ``segment_chars`` is the page size that tool
        # returns. Both come from ``DaemonConfig`` so the operator can
        # tune per host; we keep generous defaults that survive a
        # 200k-context model with a verbose system prompt.
        self._max_inline_chars = max(1, int(max_inline_chars))
        self._segment_chars = max(1, int(segment_chars))
        self._max_input_bytes = max(1, int(max_input_bytes))
        # Catch-up older than this skips the LLM (still stored); <= 0 disables.
        self._catchup_stale_ms = (
            int(catchup_stale_hours * 3600 * 1000) if catchup_stale_hours > 0 else 0
        )
        self._image_edge_px = int(image_edge_px) or _DEFAULT_IMAGE_EDGE_PX
        self.keystore = keystore
        self.http = http_client
        self.store = message_store
        self._key_cache = DeviceKeyCache(http_client)
        self._ws: Optional[PuffoCoreWsClient] = None
        # Most recent DM sender. ``send_fallback_message(channel_id="")`` means
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
        # slug → (display_name, avatar_url, fetched_at_monotonic) from
        # /identities/profiles. TTL'd at _PROFILE_CACHE_TTL_SECONDS so
        # operator name / avatar changes propagate without daemon
        # restart (was previously session-lifetime, leaving the agent
        # rendering the stale name forever). Empty fields are cached
        # under the same TTL — transient lookup failures self-heal at
        # the next tick instead of pinning a permanent "" miss.
        self._profile_cache: dict[str, tuple[str, str, float]] = {}
        # slug → (owner_slug, fetched_at_monotonic). Populated by the
        # same ``/identities/profiles`` call as ``_profile_cache``; empty
        # for humans, the operator for agents. Same TTL so re-ownership
        # propagates without a daemon restart.
        self._owner_slug_cache: dict[str, tuple[str, float]] = {}
        # Invitation event_ids the worker has already processed.
        # Lifetime-scoped — the operator-DM branch isn't idempotent
        # against server-side state, so resetting on reconnect would
        # re-emit the operator-confirm prompt for every still-pending
        # invite. Lost on daemon restart (acceptable: re-DM rate is
        # naturally rare).
        self._processed_invite_ids: set[str] = set()
        self._processed_membership_event_ids: set[str] = set()
        # Fallback for manual-accept events that omit ``original_invite``.
        self._inviter_by_invitation_event_id: dict[str, str] = {}
        # When the worker DMs the operator about a non-auto-acceptable
        # invite, the DM's envelope_id lives here so a ``y``/``n``
        # reply in that thread can be intercepted inside the daemon.
        # In-memory only — on restart we re-DM from the next poll.
        self._pending_invite_dms: dict[str, dict[str, Any]] = {}
        # Agent-initiated leaves awaiting operator y/n, keyed by approval-DM
        # envelope_id. ``_gate_left_spaces`` marks gate-approved space leaves
        # so the WS echo's ``_on_left_space`` skips its now-duplicate DM.
        self._pending_leave_dms: dict[str, dict[str, Any]] = {}
        self._gate_left_spaces: set[str] = set()
        # cli-local command-permission prompts awaiting operator y/n,
        # keyed by prompt-DM envelope_id. In-memory only.
        self._pending_command_permissions: dict[str, asyncio.Future[bool]] = {}

        # channel_id → space_id learned from inbound envelopes. The
        # agent's config carries one "home" space_id, but cross-space
        # channel invites work too; we route replies through the space
        # the message arrived from. Falls back to ``self.space_id``
        # when no inbound envelope on this channel has been seen.
        self._channel_space: dict[str, str] = {}
        # Serialize + debounce on-demand cache re-warms (no stampede).
        self._rewarm_lock = asyncio.Lock()
        self._last_rewarm = 0.0
        self._warm_task: asyncio.Future | None = None
        self._stale_report_buf: list[str] = []
        self._stale_flush_task: asyncio.Future | None = None

        # Lazy caches for human-readable space + channel names; names
        # aren't on the WS payload so we resolve via ``GET /spaces``
        # and ``GET /spaces/<id>/events`` on first reference. Bare-id
        # fallback when lookup fails so the LLM never sees a blank.
        self._space_name_cache: dict[str, str] = {}
        self._channel_name_cache: dict[str, str] = {}

        # Per-space member cache (slug → identity_type) for mention
        # scoping + bot-vs-human labelling. Lazy, session-lifetime.
        self._space_members: dict[str, dict[str, str]] = {}

        # All ``self._log.X(...)`` calls in this class get an
        # ``[<agent_slug>]`` prefix so multi-agent daemon logs are
        # filterable per agent.
        self._log = _AgentLogger(logger, {"agent": self.slug})

    def _is_stale_for_catchup(self, sent_at: int, now_ms: int | None = None) -> bool:
        """Past the staleness threshold → store but skip the LLM.
        <= 0 disables so a mis-set config can't skip live traffic."""
        if self._catchup_stale_ms <= 0:
            return False
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        return sent_at < now_ms - self._catchup_stale_ms

    def _report_stale_processed(self, envelope_id: str) -> None:
        """Batched best-effort processing report; never blocks catch-up."""
        self._stale_report_buf.append(envelope_id)
        if self._stale_flush_task is None or self._stale_flush_task.done():
            self._stale_flush_task = asyncio.ensure_future(
                self._flush_stale_reports()
            )

    async def _flush_stale_reports(self) -> None:
        await asyncio.sleep(1.0)  # coalesce the burst
        # re-sweeps mid-flush arrivals
        while self._stale_report_buf:
            buf, self._stale_report_buf = self._stale_report_buf, []
            await self._post_stale_runs(buf)

    async def _post_stale_runs(self, buf: list[str]) -> None:
        runs = [
            {
                "run_id": f"run_{uuid.uuid4().hex}",
                "message_id": mid,
                "succeeded": True,
            }
            for mid in buf
        ]
        for i in range(0, len(runs), 200):  # request-size cap
            try:
                await self.http.post(
                    "/messages/processing/end:batch",
                    {"runs": runs[i:i + 200]},
                )
            except Exception as exc:  # noqa: BLE001
                self._log.debug(
                    "stale-processed flush failed (%d runs): %s",
                    len(runs[i:i + 200]), exc,
                )

    async def listen(
        self,
        on_message: Callable[..., Coroutine[Any, Any, Any]],
        on_api_error_retry: Callable[..., Coroutine[Any, Any, Any]] | None = None,
        on_api_error_abandon: Callable[..., Coroutine[Any, Any, Any]] | None = None,
        on_turn_success: Callable[..., Coroutine[Any, Any, Any]] | None = None,
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

        PUF-252: ``on_api_error_abandon`` is the state-honesty
        hook fired exactly once when kick-retries have all failed
        and the batch is being abandoned. The worker uses this to
        flip ``runtime.health`` + ``runtime.error`` so the
        discoverable-action affordances on Nova's lane (FB-197
        status dot + FB-198 restart lever, both in the Operator
        Action Panel cluster) have a signal to surface — Sam's
        "agent went silent without warning" symptom stops being
        invisible. Invoked as ``on_api_error_abandon(root_id,
        batch, channel_meta, attempts)``. When omitted, the
        abandon stays silent (pre-PUF-252 behaviour).

        ``on_turn_success`` is the recovery-side matched-pair
        for ``on_api_error_abandon``. Fires on every successful
        turn exit (fresh dispatch AND kick-retry recovery).
        Invoked as ``on_turn_success(root_id, batch, channel_meta)``.
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
                self._log.warning(
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
                self._log.warning(
                    "decryption failed for %s (%d sender keys tried) — skipping",
                    envelope.get("envelope_id"), len(sender_pks),
                )
                return

            # PUF-227-A: strict cache-validation invariant for incoming
            # ids. Any thread_root_id / reply_to_id that doesn't point
            # to a same-channel parent in our local message_store gets
            # wiped to None before storage — the agent's local view
            # never honors a thread linkage that can't be resolved here.
            # Catches the Scout-class symptom: a server / UI / sender
            # that stamps a cross-channel id can't poison the recipient
            # daemon's thread state.
            validated_thread_root_id = await self._validate_incoming_parent_id(
                payload.thread_root_id, payload.channel_id, payload.space_id,
            )
            validated_reply_to_id = await self._validate_incoming_parent_id(
                payload.reply_to_id, payload.channel_id, payload.space_id,
            )
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
                "thread_root_id": validated_thread_root_id,
                "reply_to_id": validated_reply_to_id,
            })
            # Rebind for downstream code (root_id resolution at the
            # batch-coalesce step, channel_meta construction, etc.) so
            # admit-time wipes propagate through the agent prompt.
            payload_thread_root_id = validated_thread_root_id
            payload_reply_to_id = validated_reply_to_id

            # Self-echo lands here too now (see ``handle_envelope``'s
            # opening comment). Persist it — so ``get_channel_history``
            # / ``get_thread_history`` show the agent's own posts —
            # then stop before any of the LLM-facing pipeline below
            # runs. The agent already produced this message; queueing
            # it again would feed the agent its own words and trip a
            # turn-by-turn echo loop.
            if payload.sender_slug == self.slug:
                return

            # Daemon-side intercept: ``y``/``n`` from the operator on an
            # outstanding invite-DM accepts/rejects without waking the
            # LLM. A threaded reply answers just that invite; a direct
            # (top-level) reply answers all pending invites at once.
            if (
                payload.envelope_kind == "dm"
                and payload.sender_slug == self.operator_slug
            ):
                text_raw = str(payload.content) if payload.content else ""
                targets, is_direct = self._resolve_invite_targets(
                    payload_thread_root_id, text_raw,
                )
                handled_labels = (
                    await self._apply_invite_replies(targets, text_raw)
                    if targets else []
                )
                if handled_labels:
                    # Handled inline — don't queue for the LLM.
                    self._last_dm_sender = payload.sender_slug
                    if is_direct:
                        await self._send_invite_bulk_summary(
                            handled_labels, text_raw, payload_thread_root_id or "",
                        )
                    return
                # Same gate for agent-initiated leave requests, but
                # threaded-only — each leave is confirmed in its own
                # thread (no direct/bulk path).
                if await self._maybe_handle_leave_reply(
                    thread_root_id=payload_thread_root_id or "", text=text_raw,
                ):
                    self._last_dm_sender = payload.sender_slug
                    return
                # Same gate for cli-local command-permission prompts.
                if await self._maybe_handle_permission_reply(
                    thread_root_id=payload_thread_root_id or "", text=text_raw,
                ):
                    self._last_dm_sender = payload.sender_slug
                    return

            # Stale catch-up backlog: stored above, skips the LLM.
            if self._is_stale_for_catchup(payload.sent_at):
                self._log.info(
                    "handle_envelope: staleness-gate-skipped envelope=%s "
                    "(sent_at=%d, threshold_ms=%d, root=%s) — stored, no LLM",
                    payload.envelope_id, payload.sent_at,
                    self._catchup_stale_ms,
                    payload_thread_root_id or payload.envelope_id,
                )
                self._report_stale_processed(payload.envelope_id)
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

            # Stash the sender so `send_fallback_message("")` can route replies.
            # Always overwrite — first-write would pin replies to a
            # stale peer when a different person DMs us.
            if is_dm:
                self._last_dm_sender = payload.sender_slug
            elif payload.channel_id and payload.space_id:
                # Remember which space owns this channel so replies
                # resolve members in the right space (cross-space
                # invites would otherwise fail).
                self._channel_space[payload.channel_id] = payload.space_id

            # Parse all ``@<slug>`` and scope to space members
            # (matches the web client). Self is always kept.
            self_slug_lower = self.slug.lower()
            seen: set[str] = set()
            parsed: list[str] = []
            for m in _MENTION_RE.finditer(raw_text):
                slug = m.group(1).lower()
                if slug in seen:
                    continue
                seen.add(slug)
                parsed.append(slug)
            is_mention = self_slug_lower in seen
            space_members = (
                await self._get_space_members(payload.space_id)
                if payload.space_id
                else {}
            )
            mentions: list[dict] = []
            for slug in parsed:
                if slug == self_slug_lower:
                    mentions.append({"username": self.slug, "is_agent": True, "is_self": True})
                    continue
                if space_members and slug not in space_members:
                    continue
                is_agent = space_members.get(slug) == "agent"
                mentions.append({"username": slug, "is_agent": is_agent, "is_self": False})

            # Self-mention rewrite: `@<our-slug>` → `@you(<our-slug>)`
            # (the documented "addressed to you" signal).
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

            # Thread-batched queue: every message coalesces under
            # its ``root_id`` (the envelope's ``thread_root_id``, or
            # the message itself when it's a top-level post). The
            # PriorityQueue holds one slot per root; new arrivals on
            # the same thread either join the existing batch
            # (priority same or lower) or bump the slot to the new
            # higher priority. The agent processes one whole thread
            # at a time in ``on_message_batch``.
            # PUF-227-A: route on the VALIDATED thread_root_id. If
            # admit-time validation wiped it (parent not in cache or
            # cross-channel), the message gets a fresh per-envelope
            # root_id and lands in its own batch — never inheriting a
            # stale channel_meta from an unrelated thread.
            root_id = payload_thread_root_id or payload.envelope_id

            # Cross-restart dedup: after a daemon restart the server
            # redelivers anything in /messages/pending. If we already
            # dispatched a batch that covers ``payload.sent_at``,
            # skip — the agent has seen this.
            last_processed = await self.store.get_last_processed_sent_at(root_id)
            if payload.sent_at <= last_processed:
                self._log.info(
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
            # Cache-hit off ``_fetch_display_name`` above — no extra HTTP.
            sender_owner_slug = await self._fetch_owner_slug(
                payload.sender_slug,
            )
            is_from_operator = bool(
                self.operator_slug
                and payload.sender_slug == self.operator_slug
            )

            # ``owner_slug`` is agent-only — the is-agent signal the
            # priority bands were designed around.
            direct = is_dm or is_mention
            sender_is_agent = bool(sender_owner_slug)
            priority = _compute_priority(direct, sender_is_agent)

            # Long-message redaction. Operators paste big chunks of
            # code or transcripts that, combined with the agent's
            # system prompt + thread history, can blow past the LLM
            # context window — historically observable as the agent
            # getting stuck in a "Prompt is too long" retry loop the
            # restart path didn't recover from. The full envelope
            # stays in messages.db; only the in-prompt view collapses
            # to a placeholder pointing at ``get_post_segment``.
            llm_text = _maybe_redact_long_text(
                clean_text,
                envelope_id=payload.envelope_id,
                sender_slug=payload.sender_slug,
                sender_display_name=sender_display_name,
                max_inline_chars=self._max_inline_chars,
                segment_chars=self._segment_chars,
                agent_slug=self.slug,
            )

            msg_dict = {
                "channel_id": channel_id,
                "channel_name": channel_name,
                "space_id": space_id,
                "space_name": space_name,
                "sender_slug": payload.sender_slug,
                "sender_display_name": sender_display_name,
                "sender_owner_slug": sender_owner_slug,
                "is_from_operator": is_from_operator,
                "sender_email": "",
                "text": llm_text,
                "root_id": payload_thread_root_id or "",
                "is_dm": is_dm,
                "attachments": attachment_paths,
                "sender_is_agent": sender_is_agent,
                "mentions": mentions,
                "envelope_id": payload.envelope_id,
                "sent_at": payload.sent_at,
                "is_visible_to_human": payload.is_visible_to_human,
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
        consumer_task = asyncio.ensure_future(
            self._consume_queue(on_message, on_api_error_retry, on_api_error_abandon, on_turn_success),
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
        # Re-warms caches on every (re)connect, first connect included.
        self._ws.on_connect = self._on_ws_connect
        await self.store.open()
        try:
            await self._ws.run()
        finally:
            consumer_task.cancel()
            invite_poll_task.cancel()
            if self._warm_task is not None:
                self._warm_task.cancel()
            for task in (consumer_task, invite_poll_task, self._warm_task):
                if task is None:
                    continue
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
            self._log.info(
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
            self._log.info(
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
        on_api_error_abandon: Optional[Callable[..., Coroutine[Any, Any, Any]]],
        on_turn_success: Optional[Callable[..., Coroutine[Any, Any, Any]]] = None,
        last_envelope: str,
        is_auth: bool = False,
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

        if is_auth:
            # Retrying is pointless until re-login (worker already set
            # auth_failed + DMed). Don't fire the abandon callback — it
            # would overwrite auth_failed; cursor stays un-advanced so
            # the batch redelivers on recovery.
            self._log.warning(
                "agent reply was an auth error for thread %s (last envelope "
                "%s); skipping kick-retries until operator re-login",
                root_id, last_envelope,
            )
            return

        if on_api_error_retry is None:
            self._log.warning(
                "agent reply contained 'API Error' for thread %s "
                "(last envelope %s); no retry callback wired, "
                "abandoning batch",
                root_id, last_envelope,
            )
            await self._fire_api_error_abandon(
                on_api_error_abandon=on_api_error_abandon,
                root_id=root_id,
                batch=batch,
                channel_meta=channel_meta,
                attempts=0,
            )
            return

        for attempt in range(1, self.MAX_API_ERROR_RETRIES + 1):
            delay = random.uniform(15.0, 45.0)
            self._log.warning(
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
                        self._log.exception(
                            "mark_thread_processed(%s, %d) failed "
                            "after kick-retry; agent may re-process "
                            "after restart",
                            root_id, tail_sent_at,
                        )
                self._log.info(
                    "agent thread %s recovered after kick-retry %d/%d",
                    root_id, attempt, self.MAX_API_ERROR_RETRIES,
                )
                await self._fire_turn_success(
                    on_turn_success=on_turn_success,
                    root_id=root_id,
                    batch=batch,
                    channel_meta=channel_meta,
                )
                return
            except AgentAPIError:
                # Still rate-limited; loop with another backoff.
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.exception(
                    "kick-retry %d/%d for thread %s raised; abandoning",
                    attempt, self.MAX_API_ERROR_RETRIES, root_id,
                )
                await self._fire_api_error_abandon(
                    on_api_error_abandon=on_api_error_abandon,
                    root_id=root_id,
                    batch=batch,
                    channel_meta=channel_meta,
                    attempts=attempt,
                )
                return
        self._log.warning(
            "agent thread %s exhausted %d kick-retries (last envelope %s); "
            "abandoning the batch — agent will see these messages via "
            "get_channel_history on the next dispatch",
            root_id, self.MAX_API_ERROR_RETRIES, last_envelope,
        )
        await self._fire_api_error_abandon(
            on_api_error_abandon=on_api_error_abandon,
            root_id=root_id,
            batch=batch,
            channel_meta=channel_meta,
            attempts=self.MAX_API_ERROR_RETRIES,
        )

    async def _fire_api_error_abandon(
        self,
        *,
        on_api_error_abandon: Optional[Callable[..., Coroutine[Any, Any, Any]]],
        root_id: str,
        batch: list[dict],
        channel_meta: dict,
        attempts: int,
    ) -> None:
        """PUF-252: state-honesty hook fired exactly once per
        abandoned batch. ``attempts`` is the number of kick-retries
        that actually fired (0 when no retry callback was wired, or
        an internal raise short-circuited the loop before any
        attempt completed). The worker uses this to flip
        ``runtime.health`` so FB-197 (status dot) + FB-198 (restart
        lever) on Nova's lane can surface the "agent went silent"
        signal."""
        if on_api_error_abandon is None:
            return
        try:
            await on_api_error_abandon(root_id, batch, channel_meta, attempts)
        except Exception:
            self._log.exception(
                "on_api_error_abandon callback raised for thread %s; "
                "the abandon itself stands -- the callback's job is "
                "purely observational",
                root_id,
            )

    async def _fire_turn_success(
        self,
        *,
        on_turn_success: Optional[Callable[..., Coroutine[Any, Any, Any]]],
        root_id: str,
        batch: list[dict],
        channel_meta: dict,
    ) -> None:
        """Recovery-side matched-pair for ``_fire_api_error_abandon``."""
        if on_turn_success is None:
            return
        try:
            await on_turn_success(root_id, batch, channel_meta)
        except Exception:
            self._log.exception(
                "on_turn_success callback raised for thread %s",
                root_id,
            )

    def _message_block_bytes(self, msg: dict) -> int:
        """Conservative byte estimate of one formatted block — over-counts
        so greedy-fill never lets an over-cap block through."""
        n = len((msg.get("text") or "").encode("utf-8"))
        for att in (msg.get("attachments") or []):
            n += len(str(att).encode("utf-8")) + 16
        for mention in (msg.get("mentions") or []):
            n += len(str(mention).encode("utf-8")) + 8
        return n + _BLOCK_METADATA_OVERHEAD_BYTES

    def _greedy_fit_prefix(self, messages: list[dict]) -> int:
        """Largest K where ``messages[:K]`` fits ``_max_input_bytes``.
        Always >= 1 — a lone over-budget message dispatches alone (the
        adapter cap is the backstop) instead of stalling the thread."""
        budget = self._max_input_bytes
        total = 0
        for i, msg in enumerate(messages):
            size = self._message_block_bytes(msg)
            sep = 2 if i > 0 else 0  # blocks join with a blank line "\n\n"
            if i > 0 and total + sep + size > budget:
                return i
            total += sep + size
        return len(messages)

    async def _consume_queue(
        self,
        on_message_batch: Callable[..., Coroutine[Any, Any, Any]],
        on_api_error_retry: Callable[..., Coroutine[Any, Any, Any]] | None = None,
        on_api_error_abandon: Callable[..., Coroutine[Any, Any, Any]] | None = None,
        on_turn_success: Callable[..., Coroutine[Any, Any, Any]] | None = None,
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
            all_msgs = entry.messages
            channel_meta = entry.channel_meta

            # Paranoid in-batch dedup (before the split, so byte accounting
            # sees the real set). Admit-time dedup should make this a no-op;
            # the warning exposes any upstream race that slips one through.
            seen_ids: set[str] = set()
            deduped: list[dict] = []
            dropped: list[str] = []
            for m in all_msgs:
                mid = m.get("envelope_id", "")
                if mid and mid in seen_ids:
                    dropped.append(mid)
                    continue
                if mid:
                    seen_ids.add(mid)
                deduped.append(m)
            if dropped:
                self._log.warning(
                    "consumer dropped %d in-batch duplicate envelope_id(s) "
                    "for thread %s before dispatch: %s",
                    len(dropped), root_id, dropped,
                )

            # Greedy-fill: dispatch the FIFO prefix that fits the byte
            # budget; the remainder stays queued.
            split = self._greedy_fit_prefix(deduped)
            batch = deduped[:split]
            deferred = deduped[split:]
            entry.dispatching_ids = {
                m.get("envelope_id") for m in batch if m.get("envelope_id")
            }
            if deferred:
                # Slot stays OPEN so a mid-dispatch arrival appends after the
                # deferred tail instead of overwriting via the reopen branch;
                # the cursor covers only ``batch``, so deferred survive restart.
                entry.messages = deferred
                entry.in_queue = True
                self._queue_seq += 1
                entry.current_seq = self._queue_seq
                await self._queue.put(
                    (entry.current_priority, entry.current_seq, root_id)
                )
                self._log.info(
                    "greedy-fill: thread %s dispatching %d/%d msgs, "
                    "deferring %d to next turn (budget=%d bytes)",
                    root_id, len(batch), len(deduped), len(deferred),
                    self._max_input_bytes,
                )
            else:
                entry.messages = []
                entry.in_queue = False

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
            except AgentAPIError as exc:
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
                # work from. Auth errors skip retries entirely (the
                # worker already flipped auth_failed + DMed).
                last_envelope = batch[-1].get("envelope_id", "") if batch else ""
                await self._do_api_error_retries(
                    root_id=root_id,
                    entry=entry,
                    batch=batch,
                    channel_meta=channel_meta,
                    on_api_error_retry=on_api_error_retry,
                    on_api_error_abandon=on_api_error_abandon,
                    on_turn_success=on_turn_success,
                    last_envelope=last_envelope,
                    is_auth=getattr(exc, "is_auth", False),
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.exception(
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
                    self._log.exception(
                        "mark_thread_processed(%s, %d) failed; agent "
                        "may re-process this thread after a restart",
                        root_id, tail_sent_at,
                    )
            # Cursor now covers the dispatched batch — duplicates of
            # any of its envelopes will be caught by the handle_envelope
            # cursor check from this point on, so the in-memory
            # dispatching_ids set has done its job and can be released.
            entry.dispatching_ids = set()

            await self._fire_turn_success(
                on_turn_success=on_turn_success,
                root_id=root_id,
                batch=batch,
                channel_meta=channel_meta,
            )

    async def _invite_poll_loop(self) -> None:
        """Poll ``/invites`` to catch invites the WS can't reach (the
        server only fans events to existing space members, which the
        invitee isn't yet).
        """
        FAST_INTERVAL = 10
        STEADY_INTERVAL = 30
        FAST_PHASE_SECONDS = 300

        # Brief grace period so first-poll output doesn't interleave
        # with the WS handshake log on startup.
        try:
            await asyncio.sleep(2)
            while True:
                await self._poll_pending_invites()
                interval = self._next_invite_poll_interval(
                    fast=FAST_INTERVAL,
                    steady=STEADY_INTERVAL,
                    fast_phase_seconds=FAST_PHASE_SECONDS,
                )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    def _next_invite_poll_interval(
        self,
        *,
        fast: int,
        steady: int,
        fast_phase_seconds: int,
    ) -> int:
        """Pick the next ``_invite_poll_loop`` sleep based on agent age."""
        if self._agent_created_at <= 0:
            return steady
        age = time.time() - self._agent_created_at
        return fast if age < fast_phase_seconds else steady

    async def _handle_event(self, scope: str, event: dict) -> None:
        """WS event router for space + channel invites.

        WS payloads carry bare IDs but not the space/channel name
        snapshots (those only live on the ``pending_invites`` row).
        To avoid bare-ID DMs we use the WS push as a trigger and
        defer to ``_poll_pending_invites``; the processed-id cache
        prevents the next periodic poll from double-acting.

        Side effect: any event carrying a (channel_id, space_id)
        pair gets recorded via ``store.mark_channel_space``. This
        keeps the channel→space cache populated even for channels
        the agent has just been added to but hasn't received a
        message in yet — MCP tools (``send_message``,
        ``list_channel_members``) read that cache to construct the
        space-scoped server URLs and bail loudly on miss rather
        than walking ``/spaces`` as a fallback (the FB-76 era
        resolver was removed once events became authoritative).
        """
        kind = event.get("kind")
        payload = event.get("payload") or {}

        # Cache channel→space from any membership event that carries
        # the pair, before falling through to the kind-specific
        # handlers. The conditions on which kinds + signers we trust
        # are deliberately narrow: ``invite_to_channel`` is recorded
        # only when WE are the invitee (the WS fans invites to every
        # space member, but other people's invites tell us nothing
        # about channels we can access); ``accept_channel_invite`` is
        # recorded only when WE are the signer (the agent's own
        # accept, or the server-emitted auto-accept synthetic which
        # signs-as-us); ``create_channel`` is recorded unconditionally
        # — the server only fans create_channel to space members, so
        # if we see it we have access to the space.
        try:
            await self._maybe_cache_channel_space(kind, event, payload)
        except Exception:
            # Never let cache bookkeeping break the rest of event
            # routing — invite polling / intro nudges must still run.
            self._log.exception("mark_channel_space from %s failed", kind)

        # Drop the per-space member cache when anyone joins / leaves /
        # is removed so the next mention extraction re-fetches; without
        # this the cache misses the new joiner and their @-mention is
        # silently dropped from the metadata.
        if kind in (
            EventKind.ACCEPT_SPACE_INVITE,
            EventKind.LEAVE_SPACE,
            EventKind.REMOVE_FROM_SPACE,
        ):
            evict_space_id = payload.get("space_id") or ""
            if evict_space_id:
                self._space_members.pop(evict_space_id, None)

        if kind in (EventKind.INVITE_TO_SPACE, EventKind.INVITE_TO_CHANNEL):
            invite_event_id = event.get("event_id") or ""
            inviter_slug = event.get("signer_slug") or ""
            if invite_event_id and inviter_slug:
                self._inviter_by_invitation_event_id[invite_event_id] = (
                    inviter_slug
                )
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
        if kind == EventKind.ACCEPT_CHANNEL_INVITE:
            if event.get("signer_slug") != self.slug:
                # Another member joined a channel we may also be in
                # — fall through to the announce-membership path.
                await self._maybe_announce_membership_change(
                    kind, event, payload,
                )
                return
            # Distinguish the server-emitted synthetic from a
            # real signed accept that's bouncing back over WS.
            # ``original_invite`` is the canonical marker — the
            # operator-signed path never embeds the source invite.
            original_invite = payload.get("original_invite")
            if not isinstance(original_invite, dict):
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
                self._log.exception(
                    "failed to enqueue intro nudge for server-auto-"
                    "accepted channel (space=%s channel=%s)",
                    space_id, channel_id,
                )
            # No /permission ask on auto-accept — report, don't join silently.
            await self._report_auto_accepted_channel_invite(
                inviter_slug=original_invite.get("signer_slug") or "",
                space_id=space_id,
                channel_id=channel_id,
            )
            return

        # Membership-exit events. Pair-wise: the kick path
        # (RemoveFromSpace / RemoveFromChannel) is target-addressed
        # via ``removed_slug``; the leave path (LeaveSpace /
        # LeaveChannel) is signer-addressed. The synthetic LeaveSpace
        # the server emits when an operator leaves (puffo-server #74
        # agent-cascade) is signed-as-the-agent — same predicate as
        # a real self-signed leave, so it lands in the same branch.
        if kind == EventKind.LEAVE_SPACE and event.get("signer_slug") == self.slug:
            await self._on_left_space(
                space_id=payload.get("space_id") or "",
                synthetic=str(event.get("signature") or "").startswith(
                    "server-auto:agent-cascade-leave-space"
                ),
            )
            return

        if kind == EventKind.REMOVE_FROM_SPACE and payload.get("removed_slug") == self.slug:
            await self._on_kicked_from_space(
                space_id=payload.get("space_id") or "",
                kicker_slug=event.get("signer_slug") or "",
            )
            return

        if kind == EventKind.LEAVE_CHANNEL and event.get("signer_slug") == self.slug:
            await self._on_left_channel(
                channel_id=payload.get("channel_id") or "",
            )
            return

        if kind == EventKind.REMOVE_FROM_CHANNEL and payload.get("removed_slug") == self.slug:
            await self._on_kicked_from_channel(
                channel_id=payload.get("channel_id") or "",
                space_id=payload.get("space_id") or "",
                kicker_slug=event.get("signer_slug") or "",
            )
            return

        if kind in (EventKind.LEAVE_CHANNEL, EventKind.REMOVE_FROM_CHANNEL):
            await self._maybe_announce_membership_change(
                kind, event, payload,
            )
            return

        # Space cascades silently into channel_memberships server-side
        # — surface here so #general transcript stays current.
        if kind in (
            EventKind.LEAVE_SPACE,
            EventKind.REMOVE_FROM_SPACE,
            EventKind.ACCEPT_SPACE_INVITE,
        ):
            await self._maybe_announce_space_membership_change(
                kind, event, payload,
            )
            return

        # Invite-withdrawn paths. The server emits the cancel to the
        # invitee's session too (puffo-server review/events: invitee
        # added to ``extra_ws_targets`` for cancel/reject). If the
        # operator is still holding an un-answered y/n DM, follow up
        # so they don't reply ``y`` to a dead invite.
        if kind in (EventKind.CANCEL_SPACE_INVITE, EventKind.CANCEL_CHANNEL_INVITE):
            await self._on_invite_canceled(
                invitation_event_id=payload.get("invitation_event_id") or "",
                scope="space" if kind == EventKind.CANCEL_SPACE_INVITE else "channel",
            )
            return

    async def _maybe_cache_channel_space(
        self, kind: str | None, event: dict, payload: dict,
    ) -> None:
        """Record (channel_id, space_id) from events that authoritatively
        prove the agent can reach the channel. See ``_handle_event``
        for the rationale per kind."""
        if not kind:
            return
        channel_id = payload.get("channel_id") or ""
        space_id = payload.get("space_id") or ""
        if not channel_id or not space_id:
            return
        if kind == EventKind.INVITE_TO_CHANNEL:
            if payload.get("invitee_slug") != self.slug:
                return
        elif kind == EventKind.ACCEPT_CHANNEL_INVITE:
            if event.get("signer_slug") != self.slug:
                return
        elif kind == EventKind.CREATE_CHANNEL:
            pass  # always cache; server only fans to space members
        else:
            return
        await self.store.mark_channel_space(channel_id, space_id)
        # Mirror to the in-memory dict that ``send_fallback_message``
        # reads. Without this, an agent that's just joined a channel
        # via a synthetic accept_channel_invite (operator-trust auto-
        # accept) would drop fallback replies until the first real
        # inbound envelope in that channel populated this dict via
        # ``handle_envelope`` — including the intro nudge's own
        # outbound, which never goes through handle_envelope.
        self._channel_space[channel_id] = space_id

    async def _evict_space_caches(self, space_id: str) -> None:
        """Drop every cached entry tied to a space we've left/been
        kicked from. Two layers:

          1. In-memory ``_channel_space`` / name caches consumed by
             the daemon's send paths (e.g. ``send_fallback_message``).
          2. The persistent ``channel_space_map`` table consumed by
             the MCP subprocess via ``lookup_channel_space`` — without
             evicting this too, the LLM's ``send_message`` would
             happily resolve a channel we're no longer in and pay
             one round-trip just to get the server's 403.

        Reverse-scans ``_channel_space`` for the in-memory side;
        the persistent layer takes a ``DELETE WHERE space_id = ?``."""
        if not space_id:
            return
        for cid in [c for c, s in self._channel_space.items() if s == space_id]:
            self._channel_space.pop(cid, None)
            self._channel_name_cache.pop(cid, None)
        self._space_name_cache.pop(space_id, None)
        self._space_members.pop(space_id, None)
        try:
            await self.store.unmark_channel_space_for_space(space_id)
        except Exception:
            self._log.exception(
                "unmark_channel_space_for_space failed for sp=%s "
                "(non-fatal — in-memory eviction already ran)",
                space_id,
            )

    async def _evict_channel_caches(self, channel_id: str) -> None:
        """Smaller-scope twin of ``_evict_space_caches`` for the
        per-channel kick paths. Same two-layer eviction (in-memory
        + persistent map)."""
        if not channel_id:
            return
        self._channel_space.pop(channel_id, None)
        self._channel_name_cache.pop(channel_id, None)
        try:
            await self.store.unmark_channel_space(channel_id)
        except Exception:
            self._log.exception(
                "unmark_channel_space failed for ch=%s "
                "(non-fatal — in-memory eviction already ran)",
                channel_id,
            )

    async def _dm_operator_membership_change(self, text: str) -> None:
        """Best-effort operator notification on membership exit. No-op
        when ``operator_slug`` isn't configured (early provisioning,
        smoke fixtures); exception suppression matches the existing
        invite-DM helpers so a failed DM never crashes the WS handler."""
        if not self.operator_slug:
            self._log.info(
                "membership change but no operator_slug; not DMing: %s", text,
            )
            return
        try:
            await self._send_dm(self.operator_slug, text, root_id="")
        except Exception:
            self._log.exception(
                "failed to DM operator about membership change: %s", text,
            )

    async def _on_left_space(self, *, space_id: str, synthetic: bool) -> None:
        """Agent exited a space — either signed LeaveSpace itself, or
        was cascaded out by the server when its operator left
        (puffo-server #74 emits a synthetic ``LeaveSpace`` per agent
        with ``signature = "server-auto:agent-cascade-leave-space"``).
        Either way, clean caches and tell the operator why.

        Synthetic events aren't cryptographically signed — the
        ``signature`` field is a server-set marker — so before
        applying the visible side effect (operator DM), verify the
        cascade actually happened by asking the server's authoritative
        membership API. Defends against buggy server emits, WS
        redelivery on reconnect, and a malicious server crafting a
        cascade event the agent has no way to refute on the wire.
        Real signed LeaveSpace events skip the check (the agent itself
        signed it; server-side engine already verified the signer)."""
        if not space_id:
            return
        # ``_still_member_of_space`` returns True / False / None.
        # We only ignore the cascade on a definitive "still a member";
        # ``None`` (network error) falls through to the permissive path
        # so a transient /spaces flake can't block legitimate cleanup.
        if synthetic and await self._still_member_of_space(space_id) is True:
            self._log.warning(
                "synthetic LeaveSpace for sp=%s but /spaces still lists "
                "us — ignoring (likely server bug or WS redelivery)",
                space_id,
            )
            return
        space_label = await self._resolve_space_name(space_id)
        await self._evict_space_caches(space_id)
        # Leaves the operator already approved via the leave-request gate
        # are reported in that DM thread; don't also send the generic one.
        if not synthetic and space_id in self._gate_left_spaces:
            self._gate_left_spaces.discard(space_id)
            return
        reason = (
            "your space exit cascaded to me"
            if synthetic else "I signed a LeaveSpace"
        )
        await self._dm_operator_membership_change(
            f"Removed from space **{space_label}**({space_id}) — {reason}."
        )

    async def _still_member_of_space(self, space_id: str) -> bool | None:
        """Authoritative membership check via ``GET /spaces``.

        Returns:
          * ``True``  — server still lists us as a member (cascade event
                        contradicts authoritative state, caller should ignore).
          * ``False`` — server does NOT list us (cascade matches reality,
                        caller should proceed with cleanup).
          * ``None``  — request failed; caller falls through to the
                        permissive path rather than blocking on a flake.
        """
        try:
            data = await self.http.get("/spaces")
        except Exception:
            self._log.exception(
                "membership re-check for sp=%s failed — falling through",
                space_id,
            )
            return None
        for entry in data.get("spaces") or []:
            if entry.get("space_id") == space_id:
                return True
        return False

    async def _on_kicked_from_space(
        self, *, space_id: str, kicker_slug: str,
    ) -> None:
        """Owner (or owner-cascade synthetic from puffo-server review/events)
        removed us from a space. Different wording from
        ``_on_left_space`` so the operator can tell apart "I left it"
        from "they kicked me"."""
        if not space_id:
            return
        space_label = await self._resolve_space_name(space_id)
        await self._evict_space_caches(space_id)
        kicker_display = (
            await self._fetch_display_name(kicker_slug) if kicker_slug else ""
        )
        kicker_label = (
            f"**{kicker_display}**(@{kicker_slug})"
            if kicker_display else f"@{kicker_slug}" if kicker_slug else "the space owner"
        )
        await self._dm_operator_membership_change(
            f"Removed from space **{space_label}**({space_id}) by {kicker_label}."
        )

    async def _on_left_channel(self, *, channel_id: str) -> None:
        """Voluntary channel exit (agent signed LeaveChannel itself).
        Operator-initiated tooling already knows; skip the DM to
        avoid noise. Cache cleanup still runs."""
        if not channel_id:
            return
        await self._evict_channel_caches(channel_id)

    async def _on_kicked_from_channel(
        self, *, channel_id: str, space_id: str, kicker_slug: str,
    ) -> None:
        """Owner kicked us out of a private channel."""
        if not channel_id:
            return
        channel_label = await self._resolve_channel_name(
            space_id=space_id, channel_id=channel_id,
        )
        space_label = await self._resolve_space_name(space_id) if space_id else ""
        await self._evict_channel_caches(channel_id)
        kicker_display = (
            await self._fetch_display_name(kicker_slug) if kicker_slug else ""
        )
        kicker_label = (
            f"**{kicker_display}**(@{kicker_slug})"
            if kicker_display else f"@{kicker_slug}" if kicker_slug else "the space owner"
        )
        location = f"channel **{channel_label}**({channel_id})"
        if space_id:
            location += f" in space **{space_label}**({space_id})"
        await self._dm_operator_membership_change(
            f"Removed from {location} by {kicker_label}."
        )

    async def _on_invite_canceled(
        self, *, invitation_event_id: str, scope: str,
    ) -> None:
        """A pending invite for us was withdrawn. If we DM'd the
        operator a y/n prompt for it, follow up so they know not to
        reply ``y`` (would 400 against an InviteNotFound at the
        server). No-op when we either auto-accepted, never DM'd,
        or already cleaned up — matched by ``_pending_invite_dms``
        absence."""
        if not invitation_event_id:
            return
        target_env_id: str | None = None
        for env_id, meta in self._pending_invite_dms.items():
            if meta.get("invitation_event_id") == invitation_event_id:
                target_env_id = env_id
                break
        if target_env_id is None:
            return
        meta = self._pending_invite_dms.pop(target_env_id)
        # Add to processed-set too so a stale pending-invite poll
        # response (server-side cache lag) can't re-fire the DM.
        self._processed_invite_ids.add(invitation_event_id)

        inviter_slug = meta.get("inviter_slug") or ""
        space_id = meta.get("space_id") or ""
        channel_id = meta.get("channel_id") or ""
        space_name = meta.get("space_name") or None
        channel_name = meta.get("channel_name") or None
        # Label = name only; bare ID only as fallback. The operator
        # already saw the IDs in the original invite-DM, so repeating
        # them here is noise.
        space_label = f"**{space_name}**" if space_name else space_id
        inviter_display = (
            await self._fetch_display_name(inviter_slug) if inviter_slug else ""
        )
        inviter_label = (
            f"**{inviter_display}** (@{inviter_slug})"
            if inviter_display else f"@{inviter_slug}" if inviter_slug else "the inviter"
        )
        if scope == "channel":
            channel_label = (
                f"**{channel_name}**" if channel_name else channel_id
            )
            target = f"channel {channel_label} in space {space_label}"
        else:
            target = f"space {space_label}"
        text = (
            f"{inviter_label} withdrew the invite to {target} — "
            f"ignore my earlier prompt."
        )
        try:
            await self._send_dm(
                self.operator_slug, text, root_id=target_env_id,
            )
        except Exception:
            self._log.exception(
                "failed to DM operator about canceled invite (event_id=%s)",
                invitation_event_id,
            )

    async def _poll_pending_invites(self) -> None:
        """Pull pending invites the agent hasn't acted on. Space
        invites only arrive via this poll (WS doesn't fan to non-
        members); channel invites also benefit from the catch-up.
        """
        try:
            data = await self.http.get("/invites?direction=received")
        except Exception:
            self._log.exception("invite poll failed")
            return
        invites = data.get("invites") or []
        if not invites:
            return
        self._log.info("invite poll: %d pending invite(s)", len(invites))
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
                self._log.warning(
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
            self._log.warning(
                "invite missing required fields: kind=%s event_id=%s "
                "signer=%s space=%s",
                kind, invitation_event_id, inviter_slug, space_id,
            )
            return
        if kind == EventKind.INVITE_TO_CHANNEL and not channel_id:
            self._log.warning(
                "channel invite missing channel_id: event_id=%s",
                invitation_event_id,
            )
            return
        if invitation_event_id in self._processed_invite_ids:
            return

        is_from_operator = await self._inviter_is_operator(inviter_slug)
        # Flag-driven auto-accept covers space invites from non-operators;
        # unlike the (silent) operator path it DMs a report afterwards.
        flag_accept = (
            kind == EventKind.INVITE_TO_SPACE and self.auto_accept_space_invitations
        )
        if is_from_operator or flag_accept:
            try:
                await self._accept_invite(
                    kind, invitation_event_id, space_id, channel_id,
                )
                self._log.info(
                    "auto-accepted %s from %s (event_id=%s)",
                    kind, inviter_slug, invitation_event_id,
                )
                self._processed_invite_ids.add(invitation_event_id)
            except Exception:
                self._log.exception(
                    "failed to auto-accept %s from %s (event_id=%s)",
                    kind, inviter_slug, invitation_event_id,
                )
                return
            if not is_from_operator:
                await self._report_auto_accepted_space_invite(
                    inviter_slug=inviter_slug,
                    space_id=space_id,
                    space_name=space_name,
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

    async def _report_auto_accepted_space_invite(
        self, *, inviter_slug: str, space_id: str, space_name: str | None,
    ) -> None:
        """Tell the operator we auto-accepted a non-operator space invite
        on their behalf (the auto_accept_space_invitations flag is on).
        Best-effort."""
        if not self.operator_slug:
            return
        space_label = f"**{space_name}**({space_id})" if space_name else space_id
        inviter_display = await self._fetch_display_name(inviter_slug)
        inviter_label = (
            f"**{inviter_display}**(@{inviter_slug})"
            if inviter_display else f"@{inviter_slug}"
        )
        text = (
            f"Auto-accepted an invite to space {space_label} from "
            f"{inviter_label} (auto-accept-space-invitations is on)."
        )
        try:
            await self._send_dm(self.operator_slug, text, root_id="")
        except Exception:
            self._log.exception(
                "failed to report auto-accepted space invite to operator",
            )

    async def _report_auto_accepted_channel_invite(
        self, *, inviter_slug: str, space_id: str, channel_id: str,
    ) -> None:
        """Best-effort operator report for a server-auto-accepted
        owner channel invite."""
        if not self.operator_slug:
            return
        # Names only — raw ids are operator noise.
        space_name = await self._resolve_space_name(space_id)
        channel_name = await self._resolve_channel_name(
            space_id=space_id, channel_id=channel_id,
        )
        space_label = f"**{space_name or space_id}**"
        channel_label = f"**{channel_name or channel_id}**"
        inviter_label = f"@{inviter_slug}" if inviter_slug else "the space owner"
        if inviter_slug:
            inviter_display = await self._fetch_display_name(inviter_slug)
            if inviter_display:
                inviter_label = f"**{inviter_display}**"
        text = (
            f"Auto-accepted {inviter_label}'s invite to channel "
            f"{channel_label} in space {space_label} "
            f"(space-owner invites are auto-accepted)."
        )
        try:
            await self._send_dm(self.operator_slug, text, root_id="")
        except Exception:
            self._log.exception(
                "failed to report auto-accepted channel invite to operator",
            )

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
            self._log.warning(
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
        """display_name via the unified profile cache. Empty string
        on miss/failure; caller falls back to ``@slug``. Thin wrapper
        kept for source compat with the dozen call sites that only
        care about the name."""
        name, _ = await self._fetch_user_profile(slug)
        return name

    async def _fetch_owner_slug(self, slug: str) -> str:
        """Sender's operator slug (agents only; ``""`` for humans /
        revoked attestation). Inbound path resolves the display_name
        just before this so the TTL'd cache is warm — no extra HTTP."""
        if not slug:
            return ""
        now = time.monotonic()
        cached = self._owner_slug_cache.get(slug)
        if cached is not None and now - cached[1] < _PROFILE_CACHE_TTL_SECONDS:
            return cached[0]
        await self._fetch_user_profile(slug, force_refresh=True)
        fresh = self._owner_slug_cache.get(slug)
        return fresh[0] if fresh else ""

    def set_profile(self, slug: str, display_name: str, avatar_url: str) -> None:
        """Inject fresh values into the profile cache, bypassing TTL.
        Used by the MCP ``get_user_info`` tool to share its just-
        fetched values with the daemon's render path so the next
        inbound envelope renders with the new display_name + avatar
        without waiting for the cache to expire."""
        if not slug:
            return
        self._profile_cache[slug] = (display_name, avatar_url, time.monotonic())

    async def _fetch_user_profile(
        self, slug: str, *, force_refresh: bool = False,
    ) -> tuple[str, str]:
        """``(display_name, avatar_url)`` via /identities/profiles,
        cached for ``_PROFILE_CACHE_TTL_SECONDS``. Empty strings on
        miss/failure; same TTL applies so a transient lookup
        failure doesn't pin a permanent miss.

        ``force_refresh=True`` bypasses the cache for the read (the
        MCP ``get_user_profile`` tool's path) but still writes back
        so subsequent reads see the fresh value.
        """
        if not slug:
            return ("", "")
        now = time.monotonic()
        if not force_refresh:
            cached = self._profile_cache.get(slug)
            if cached is not None and now - cached[2] < _PROFILE_CACHE_TTL_SECONDS:
                return (cached[0], cached[1])
        name = ""
        avatar_url = ""
        owner_slug = ""
        try:
            data = await self.http.get(
                f"/identities/profiles?slugs={slug}",
            )
            for entry in data.get("profiles") or []:
                if entry.get("slug") == slug:
                    name = (entry.get("display_name") or "").strip()
                    avatar_url = (entry.get("avatar_url") or "").strip()
                    # Non-empty only for agents (their operator).
                    owner_slug = (entry.get("owner_slug") or "").strip()
                    break
        except Exception as exc:
            self._log.debug(
                "_fetch_user_profile: lookup failed for %s: %s",
                slug, exc,
            )
        self._profile_cache[slug] = (name, avatar_url, now)
        self._owner_slug_cache[slug] = (owner_slug, now)
        disk_cache.persist_profile(slug, name, avatar_url)
        if avatar_url:
            asyncio.create_task(self._fetch_and_cache_avatar(avatar_url))
        return (name, avatar_url)

    async def _validate_incoming_parent_id(
        self,
        parent_id: Optional[str],
        expected_channel_id: Optional[str],
        expected_space_id: Optional[str],
    ) -> Optional[str]:
        """PUF-227-A: strict cache-validation for incoming thread_root_id
        / reply_to_id. Returns the original id when the referenced
        parent envelope is in our local message store AND lives in
        the same channel/space as the incoming envelope; otherwise
        returns ``None`` (admit-time wipe). Caller stores the wiped
        value so all downstream reads — admit-batch routing, prompt
        render, history queries — see the validated id.
        """
        if not parent_id:
            return parent_id
        try:
            parent = await self.store.get_message_by_envelope(parent_id)
        except Exception as exc:
            self._log.warning(
                "_validate_incoming_parent_id: lookup failed for %s: %s",
                parent_id, exc,
            )
            return None
        if parent is None:
            self._log.info(
                "_validate_incoming_parent_id: wiped %s — parent not in local cache",
                parent_id,
            )
            return None
        if expected_channel_id and parent.channel_id != expected_channel_id:
            self._log.info(
                "_validate_incoming_parent_id: wiped %s — parent channel "
                "%r != incoming channel %r",
                parent_id, parent.channel_id, expected_channel_id,
            )
            return None
        if (
            expected_space_id
            and parent.space_id
            and parent.space_id != expected_space_id
        ):
            self._log.info(
                "_validate_incoming_parent_id: wiped %s — parent space "
                "%r != incoming space %r",
                parent_id, parent.space_id, expected_space_id,
            )
            return None
        return parent_id

    async def rewarm_channel_caches(self) -> None:
        """On-miss re-warm; serialized + 5s-debounced (no stampede)."""
        async with self._rewarm_lock:
            now = time.monotonic()
            if now - self._last_rewarm < 5.0:
                return
            await self._warm_member_caches()
            self._last_rewarm = now

    async def _on_ws_connect(self) -> None:
        """Fire-and-forget re-warm; handle kept (asyncio weak-refs tasks)."""
        self._warm_task = asyncio.ensure_future(self._warm_member_caches())

    async def _warm_member_caches(self) -> None:
        """Background prefetch on ``listen()`` startup: walks ``GET
        /spaces`` and fans out parallel member + channel fetches per
        space so first-message lazy fills don't pay the round trip
        or miss recently-joined members. Non-blocking; per-fetch
        failures are logged + skipped (the existing lazy paths re-
        try on demand)."""
        started = time.monotonic()
        try:
            spaces_resp = await self.http.get("/spaces")
        except Exception as exc:
            self._log.debug("warm_member_caches: /spaces failed: %s", exc)
            return
        space_entries = spaces_resp.get("spaces") or []
        for entry in space_entries:
            sid = entry.get("space_id") or ""
            name = (entry.get("name") or "").strip()
            if sid and name:
                self._space_name_cache.setdefault(sid, name)
                disk_cache.persist_space(sid, name)
        space_ids = [
            e.get("space_id") or ""
            for e in space_entries
            if e.get("space_id")
        ]
        if not space_ids:
            return

        async def warm_one(space_id: str) -> set[str]:
            members_task = asyncio.create_task(
                self._get_space_members(space_id),
            )
            channels_task = asyncio.create_task(
                self._warm_channels_for_space(space_id),
            )
            members = await members_task
            await channels_task
            return set(members.keys())

        all_slugs: set[str] = set()
        for result in await asyncio.gather(
            *(warm_one(sid) for sid in space_ids),
            return_exceptions=True,
        ):
            if isinstance(result, set):
                all_slugs |= result
        new_slugs = [s for s in all_slugs if s not in self._profile_cache]
        if new_slugs:
            await self._bulk_fetch_profiles(new_slugs)
        self._log.info(
            "warm_member_caches: %d spaces, %d members, %d new profiles in %.2fs",
            len(space_ids), len(all_slugs), len(new_slugs),
            time.monotonic() - started,
        )

    async def _warm_channels_for_space(self, space_id: str) -> None:
        # Invariant: this endpoint is membership-filtered server-side;
        # the cache self-heal rests on that (a test pins it).
        try:
            resp = await self.http.get(f"/spaces/{space_id}/channels")
        except Exception:
            return
        for ch in resp.get("channels", []) or []:
            cid = ch.get("channel_id") or ""
            name = (ch.get("name") or "").strip()
            if not cid:
                continue
            # ``setdefault`` so a faster-arriving WS event that
            # populated these isn't clobbered with potentially older
            # data we just fetched.
            self._channel_space.setdefault(cid, space_id)
            if name:
                self._channel_name_cache.setdefault(cid, name)
                disk_cache.persist_channel(cid, name, space_id)
            try:
                await self.store.mark_channel_space(cid, space_id)
            except Exception:
                pass

    async def _bulk_fetch_profiles(self, slugs: list[str]) -> None:
        """Batch ``/identities/profiles?slugs=...``; chunked so a
        many-member space doesn't blow the URL length."""
        now = time.monotonic()
        CHUNK = 50
        for i in range(0, len(slugs), CHUNK):
            chunk = slugs[i:i + CHUNK]
            try:
                data = await self.http.get(
                    f"/identities/profiles?slugs={','.join(chunk)}",
                )
            except Exception:
                continue
            for entry in data.get("profiles") or []:
                slug = entry.get("slug") or ""
                if not slug:
                    continue
                name = (entry.get("display_name") or "").strip()
                avatar_url = (entry.get("avatar_url") or "").strip()
                self._profile_cache[slug] = (name, avatar_url, now)
                disk_cache.persist_profile(slug, name, avatar_url)
                if avatar_url:
                    asyncio.create_task(self._fetch_and_cache_avatar(avatar_url))

    async def _fetch_and_cache_avatar(self, avatar_url: str) -> None:
        """Signed GET on the blob; the UI falls back to its initial
        circle if this never lands."""
        if not avatar_url:
            return
        cache_path = disk_cache.avatar_cache_path(avatar_url)
        if cache_path.exists():
            return
        base = self.http.server_url.rstrip("/")
        if not avatar_url.startswith(base + "/"):
            # Foreign host — signing key wouldn't be honoured there.
            return
        path = avatar_url[len(base):]
        try:
            data = await self.http.get_bytes(path)
        except Exception as exc:
            self._log.debug("avatar fetch failed for %s: %s", avatar_url, exc)
            return
        disk_cache.write_avatar_bytes(avatar_url, data)

    async def _get_space_members(self, space_id: str) -> dict[str, str]:
        """``slug -> identity_type`` for ``space_id``. Cached per
        session; empty dict on miss/failure (caller treats an unknown
        space as "no scope")."""
        if not space_id:
            return {}
        cached = self._space_members.get(space_id)
        if cached is not None:
            return cached
        try:
            resp = await self.http.get(f"/spaces/{space_id}/members")
        except Exception:
            self._space_members[space_id] = {}
            return {}
        members = {
            m["slug"]: m.get("identity_type") or "human"
            for m in resp.get("members", [])
            if m.get("slug")
        }
        self._space_members[space_id] = members
        return members

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
        # Populate every returned space so the next unknown-space
        # resolve is a cache hit.
        for entry in data.get("spaces") or []:
            sid = entry.get("space_id")
            if not sid or sid in self._space_name_cache:
                continue
            entry_name = (entry.get("name") or "").strip() or sid
            self._space_name_cache[sid] = entry_name
            if entry_name != sid:
                disk_cache.persist_space(sid, entry_name)
        if space_id not in self._space_name_cache:
            self._space_name_cache[space_id] = space_id
        return self._space_name_cache[space_id]

    async def _resolve_channel_name(
        self, space_id: str, channel_id: str,
    ) -> str:
        """Channel name via ``/spaces/<sp>/channels`` (returns every
        name in one shot), falling back to a ``create_channel``
        event-replay only on miss. Cached per session; returns bare
        ``channel_id`` on miss/failure or for DMs (no space_id)."""
        if not channel_id or not space_id:
            return channel_id
        if channel_id in self._channel_name_cache:
            return self._channel_name_cache[channel_id]
        name = channel_id
        try:
            ch_data = await self.http.get(f"/spaces/{space_id}/channels")
            for entry in ch_data.get("channels") or []:
                cid = entry.get("channel_id")
                if not cid or cid in self._channel_name_cache:
                    continue
                entry_name = (entry.get("name") or "").strip() or cid
                self._channel_name_cache[cid] = entry_name
                if entry_name != cid:
                    disk_cache.persist_channel(cid, entry_name, space_id)
            if channel_id in self._channel_name_cache:
                return self._channel_name_cache[channel_id]
        except Exception:
            pass
        cursor: str | None = None
        prev_cursor: str | None = None
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
        if name and name != channel_id:
            disk_cache.persist_channel(channel_id, name, space_id)
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
        if kind == EventKind.INVITE_TO_SPACE:
            payload: dict[str, Any] = {
                "space_id": space_id,
                "invitation_event_id": invitation_event_id,
                "accepted_at": now_ms,
                "nonce": random_nonce(),
            }
            accept_kind = EventKind.ACCEPT_SPACE_INVITE
        else:  # invite_to_channel
            payload = {
                "space_id": space_id,
                "channel_id": channel_id,
                "invitation_event_id": invitation_event_id,
                "accepted_at": now_ms,
                "nonce": random_nonce(),
            }
            accept_kind = EventKind.ACCEPT_CHANNEL_INVITE
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

        # Belt + suspenders: record the channel→space mapping
        # synchronously here. The server fans the accept event back
        # over WS and ``_handle_event`` records it too, but that's
        # async — without this immediate write, the intro nudge's
        # ``send_message`` call (queued right below) could race the
        # WS echo and hit a cache miss on a channel the agent has
        # just provably joined.
        if kind == EventKind.INVITE_TO_CHANNEL and channel_id and space_id:
            try:
                await self.store.mark_channel_space(channel_id, space_id)
            except Exception:
                self._log.exception(
                    "mark_channel_space after manual accept failed "
                    "(space=%s channel=%s)", space_id, channel_id,
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
        if kind == EventKind.INVITE_TO_CHANNEL and channel_id:
            intro_channel_id = channel_id
        elif kind == EventKind.INVITE_TO_SPACE:
            try:
                intro_channel_id = await self._find_public_general_channel(
                    space_id,
                )
            except Exception:
                self._log.exception(
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
                self._log.exception(
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
                self._log.info(
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
        self._log.warning(
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
            "sender_is_agent": False,
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
            self._log.warning(
                "intro-nudge: failed to persist envelope=%s to messages.db: %s",
                envelope_id, exc,
            )

        await self._admit_thread_message(
            root_id=envelope_id,
            priority=PRIORITY_SYSTEM,
            msg_dict=msg_dict,
            channel_meta=channel_meta,
        )
        self._log.info(
            "enqueued channel-intro nudge for channel=%s (space=%s)",
            channel_id, space_id,
        )

    async def _maybe_announce_membership_change(
        self, kind: str, event: dict, payload: dict,
    ) -> None:
        """System row for other-actor channel membership. Dedup'd by event_id."""
        channel_id = payload.get("channel_id") or ""
        if not channel_id or channel_id not in self._channel_space:
            return

        inviter_slug = ""
        if kind == EventKind.ACCEPT_CHANNEL_INVITE:
            actor_slug = event.get("signer_slug") or ""
            action = "joined"
            kicker_slug = ""
            original_invite = payload.get("original_invite")
            if isinstance(original_invite, dict):
                inviter_slug = original_invite.get("signer_slug") or ""
            if not inviter_slug:
                invitation_event_id = payload.get("invitation_event_id") or ""
                if invitation_event_id:
                    inviter_slug = self._inviter_by_invitation_event_id.get(
                        invitation_event_id, "",
                    )
        elif kind == EventKind.LEAVE_CHANNEL:
            actor_slug = event.get("signer_slug") or ""
            action = "left"
            kicker_slug = ""
        elif kind == EventKind.REMOVE_FROM_CHANNEL:
            actor_slug = payload.get("removed_slug") or ""
            action = "removed"
            kicker_slug = event.get("signer_slug") or ""
        else:
            return

        if not actor_slug or actor_slug == self.slug:
            return

        event_id = event.get("event_id") or ""
        if event_id and event_id in self._processed_membership_event_ids:
            return

        try:
            await self._enqueue_membership_system_message(
                channel_id=channel_id,
                actor_slug=actor_slug,
                action=action,
                kicker_slug=kicker_slug,
                inviter_slug=inviter_slug,
                event_id=event_id,
            )
        except Exception:
            self._log.exception(
                "failed to enqueue membership system-message "
                "(kind=%s channel=%s actor=%s)",
                kind, channel_id, actor_slug,
            )
            return

        if event_id:
            self._processed_membership_event_ids.add(event_id)

    async def _pick_space_channel(self, space_id: str) -> str:
        """Pick #general for the space-event announce target, falling
        back to lex-first. Tries ``_channel_space`` first (in-memory
        from prior per-channel events); if that's empty (e.g. the agent
        cascade-joined via ``accept_space_invite``), fetches the
        channel list and warms ``_channel_space`` for next time.
        Returns "" if nothing usable found."""
        known = sorted(
            cid for cid, sid in self._channel_space.items()
            if sid == space_id
        )
        if known:
            for cid in known:
                name = await self._resolve_channel_name(
                    space_id=space_id, channel_id=cid,
                )
                if (name or "").strip().lower() == "general":
                    return cid
            return known[0]
        try:
            data = await self.http.get(f"/spaces/{space_id}/channels")
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        channels = data.get("channels") or []
        if not isinstance(channels, list) or not channels:
            return ""
        general = ""
        first = ""
        for c in channels:
            if not isinstance(c, dict):
                continue
            cid = (c.get("channel_id") or "").strip()
            if not cid:
                continue
            self._channel_space[cid] = space_id
            name = (c.get("name") or "").strip()
            if name:
                self._channel_name_cache.setdefault(cid, name)
            if not first:
                first = cid
            if not general and name.lower() == "general":
                general = cid
        return general or first

    async def _maybe_announce_space_membership_change(
        self, kind: str, event: dict, payload: dict,
    ) -> None:
        """System row for other-actor space membership. Renders in
        #general (lex-first visible channel as fallback)."""
        space_id = payload.get("space_id") or ""
        if not space_id:
            return

        # ``accept_space_invite`` cascades the agent into public channels
        # server-side without per-channel events, so ``_channel_space``
        # stays empty for those. Lazy-fetch the channel list so we can
        # still announce.
        target_channel_id = await self._pick_space_channel(space_id)
        if not target_channel_id:
            return

        inviter_slug = ""
        if kind == EventKind.ACCEPT_SPACE_INVITE:
            actor_slug = event.get("signer_slug") or ""
            action = "joined_space"
            kicker_slug = ""
            original_invite = payload.get("original_invite")
            if isinstance(original_invite, dict):
                inviter_slug = original_invite.get("signer_slug") or ""
            if not inviter_slug:
                invitation_event_id = payload.get("invitation_event_id") or ""
                if invitation_event_id:
                    inviter_slug = self._inviter_by_invitation_event_id.get(
                        invitation_event_id, "",
                    )
        elif kind == EventKind.LEAVE_SPACE:
            actor_slug = event.get("signer_slug") or ""
            action = "left_space"
            kicker_slug = ""
        elif kind == EventKind.REMOVE_FROM_SPACE:
            actor_slug = payload.get("removed_slug") or ""
            action = "removed_from_space"
            kicker_slug = event.get("signer_slug") or ""
        else:
            return

        if not actor_slug or actor_slug == self.slug:
            return

        event_id = event.get("event_id") or ""
        if event_id and event_id in self._processed_membership_event_ids:
            return

        try:
            await self._enqueue_membership_system_message(
                channel_id=target_channel_id,
                actor_slug=actor_slug,
                action=action,
                kicker_slug=kicker_slug,
                inviter_slug=inviter_slug,
                event_id=event_id,
            )
        except Exception:
            self._log.exception(
                "failed to enqueue space membership system-message "
                "(kind=%s space=%s actor=%s)",
                kind, space_id, actor_slug,
            )
            return

        if event_id:
            self._processed_membership_event_ids.add(event_id)

    async def _enqueue_membership_system_message(
        self,
        *,
        channel_id: str,
        actor_slug: str,
        action: str,
        kicker_slug: str = "",
        inviter_slug: str = "",
        event_id: str = "",
    ) -> None:
        """Non-replyable system row for a membership change."""
        space_id = self._channel_space.get(channel_id) or ""
        space_name = (
            await self._resolve_space_name(space_id) if space_id else ""
        )
        channel_name = await self._resolve_channel_name(
            space_id=space_id, channel_id=channel_id,
        )
        actor_display = await self._fetch_display_name(actor_slug)
        actor_label = (
            f"**{actor_display}**(@{actor_slug})"
            if actor_display else f"@{actor_slug}"
        )
        space_label = (
            f"**{space_name}**" if space_name else (space_id or "the space")
        )

        async def _invited_by_suffix() -> str:
            if not inviter_slug:
                return ""
            inviter_display = await self._fetch_display_name(inviter_slug)
            inviter_label = (
                f"**{inviter_display}**(@{inviter_slug})"
                if inviter_display else f"@{inviter_slug}"
            )
            return f" (invited by {inviter_label})"

        if action == "joined":
            suffix = await _invited_by_suffix()
            body = f"{actor_label} joined channel #{channel_name}{suffix}."
        elif action == "left":
            body = f"{actor_label} left channel #{channel_name}."
        elif action == "removed":
            kicker_display = (
                await self._fetch_display_name(kicker_slug)
                if kicker_slug else ""
            )
            kicker_label = (
                f"**{kicker_display}**(@{kicker_slug})"
                if kicker_display
                else f"@{kicker_slug}" if kicker_slug else "an operator"
            )
            body = (
                f"{actor_label} was removed from channel "
                f"#{channel_name} by {kicker_label}."
            )
        elif action == "joined_space":
            suffix = await _invited_by_suffix()
            body = f"{actor_label} joined space {space_label}{suffix}."
        elif action == "left_space":
            body = f"{actor_label} left space {space_label}."
        elif action == "removed_from_space":
            kicker_display = (
                await self._fetch_display_name(kicker_slug)
                if kicker_slug else ""
            )
            kicker_label = (
                f"**{kicker_display}**(@{kicker_slug})"
                if kicker_display
                else f"@{kicker_slug}" if kicker_slug else "an operator"
            )
            body = (
                f"{actor_label} was removed from space "
                f"{space_label} by {kicker_label}."
            )
        else:
            return

        now_ms = int(time.time() * 1000)
        # Deterministic suffix → INSERT OR IGNORE dedups reconnect-replay.
        envelope_id_suffix = event_id or str(now_ms)
        envelope_id = (
            f"membership-{action}-{channel_id}-{actor_slug}-{envelope_id_suffix}"
        )
        prompt_text = (
            f"[puffo-agent system message] Channel membership update: "
            f"{body} This is an announcement, for your context."
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
            "sender_is_agent": False,
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
            self._log.warning(
                "membership system-message: failed to persist "
                "envelope=%s to messages.db: %s",
                envelope_id, exc,
            )

        await self._admit_thread_message(
            root_id=envelope_id,
            priority=PRIORITY_SYSTEM,
            msg_dict=msg_dict,
            channel_meta=channel_meta,
        )
        self._log.info(
            "enqueued membership system-message channel=%s "
            "actor=%s action=%s",
            channel_id, actor_slug, action,
        )

    def _resolve_invite_targets(
        self, payload_thread_root_id: str | None, text: str,
    ) -> tuple[list[str], bool]:
        """Which pending invites the operator's strict-Y/N reply targets,
        and whether it's a *direct* (top-level) reply. A threaded reply
        matching a registered invite answers just that one; a direct
        strict-Y/N answers all pending invites. Empty list = nothing to
        act on (caller falls through to the LLM). Routing only —
        ``_maybe_handle_invite_reply`` still does the strict body-parse.
        """
        if (
            payload_thread_root_id
            and payload_thread_root_id in self._pending_invite_dms
        ):
            return ([payload_thread_root_id], False)
        if text.strip().lower() not in ("y", "yes", "n", "no"):
            return ([], False)
        return (list(self._pending_invite_dms.keys()), True)

    async def _apply_invite_replies(
        self, roots: list[str], text: str,
    ) -> list[str]:
        """Accept/reject each invite (per-invite confirm in its own
        thread); return the target labels actually handled."""
        labels: list[str] = []
        for root in roots:
            meta = self._pending_invite_dms.get(root)
            handled = await self._maybe_handle_invite_reply(
                thread_root_id=root, text=text,
            )
            if handled and meta:
                labels.append(self._invite_target_label(meta))
        return labels

    async def _send_invite_bulk_summary(
        self, labels: list[str], text: str, root_id: str,
    ) -> None:
        """Consolidated accept/reject summary in the direct reply's own
        thread, on top of the per-invite confirmations."""
        accepted = text.strip().lower() in ("y", "yes")
        verb = "Accepted" if accepted else "Rejected"
        mark = " ✓" if accepted else ""
        if len(labels) == 1:
            summary = f"{verb} invite to {labels[0]}.{mark}"
        else:
            summary = f"{verb} {len(labels)} invites: {', '.join(labels)}.{mark}"
        try:
            await self._send_dm(self.operator_slug, summary, root_id=root_id)
        except Exception:
            self._log.exception("failed to send bulk invite summary to operator")

    @staticmethod
    def _invite_target_label(meta: dict) -> str:
        """Human label for an invite's destination (space or channel)."""
        space_id = meta.get("space_id") or ""
        space_label = f"**{meta['space_name']}**" if meta.get("space_name") else space_id
        if meta.get("kind") == "invite_to_channel":
            channel_id = meta.get("channel_id") or ""
            channel_label = (
                f"**{meta['channel_name']}**" if meta.get("channel_name") else channel_id
            )
            return f"channel {channel_label} in space {space_label}"
        return f"space {space_label}"

    # ── cli-local command permission (operator-gated) ─────────────────

    async def request_command_permission(
        self, *, tool_name: str, summary: str, timeout_s: int,
    ) -> str:
        """Block on the operator's y/n for a hook-intercepted tool
        call. Returns ``allow`` / ``deny`` / ``timeout``."""
        if not self.operator_slug:
            raise RuntimeError("no operator_slug configured")
        text = format_permission_prompt(
            f"I want to run **{tool_name}** — allow it?",
            detail=summary,
        )
        envelope = await self._send_dm(self.operator_slug, text, root_id="")
        env_id = envelope.get("envelope_id", "") if envelope else ""
        if not env_id:
            raise RuntimeError("could not deliver the permission DM")
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_command_permissions[env_id] = fut
        try:
            approved = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            # Register before the notice send — a racing reply must
            # see the timeout.
            self._timed_out_command_permissions[env_id] = time.time()
            while len(self._timed_out_command_permissions) > 64:
                self._timed_out_command_permissions.pop(
                    next(iter(self._timed_out_command_permissions)),
                )
            try:
                await self._send_dm(
                    self.operator_slug,
                    f"Timed out after {timeout_s}s — I did NOT run "
                    f"`{tool_name}`.",
                    root_id=env_id,
                )
            except Exception:
                self._log.exception(
                    "permission: failed to send timeout notice",
                )
            return "timeout"
        finally:
            self._pending_command_permissions.pop(env_id, None)
        return "allow" if approved else "deny"

    async def _maybe_handle_permission_reply(
        self, *, thread_root_id: str, text: str,
    ) -> bool:
        """Operator ``y``/``n`` on a pending command-permission DM.
        Threaded only. Returns ``True`` when consumed."""
        normalized = text.strip().lower()
        if normalized in ("y", "yes"):
            approved = True
        elif normalized in ("n", "no"):
            approved = False
        else:
            return False
        # Late answer to a timed-out prompt: never claim it ran.
        if thread_root_id in self._timed_out_command_permissions:
            try:
                await self._send_dm(
                    self.operator_slug,
                    "That request already timed out — I did NOT run it. "
                    "Ask me to try again if you still want it.",
                    root_id=thread_root_id,
                )
            except Exception:
                self._log.exception("permission: failed to send stale note")
            return True
        fut = self._pending_command_permissions.get(thread_root_id)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        confirm = "Approved ✓ — running it." if approved else "Denied — I won't run it."
        try:
            await self._send_dm(self.operator_slug, confirm, root_id=thread_root_id)
        except Exception:
            self._log.exception("permission: failed to confirm decision")
        return True

    # ── Agent-initiated leave (operator-gated, mirrors invite) ────────

    async def request_leave_approval(
        self, *, kind: str, space_id: str, channel_id: str, reason: str,
    ) -> str:
        """DM the operator to approve an agent-requested leave and
        register it as pending. ``kind`` is ``leave_space`` /
        ``leave_channel``. Returns a short status line for the calling
        MCP tool to relay to the agent. The actual leave is signed only
        after the operator replies ``y`` in the DM thread (the gate in
        ``handle_envelope`` → ``_maybe_handle_leave_reply``)."""
        if not self.operator_slug:
            return (
                "No operator is configured, so I can't request approval "
                "to leave."
            )
        space_label = await self._resolve_space_name(space_id)
        channel_label = ""
        if kind == EventKind.LEAVE_CHANNEL:
            channel_label = await self._resolve_channel_name(
                space_id=space_id, channel_id=channel_id,
            )
            # Public channels can't be left on their own; tell the agent
            # up front instead of asking the operator for a doomed approval.
            if await self._channel_is_public(space_id, channel_id):
                return (
                    f"**{channel_label}** is a public channel, which can't "
                    f"be left on its own. To leave it, request to leave the "
                    f"whole space instead with `leave_space`."
                )
            target = (
                f"channel **{channel_label}**({channel_id}) in space "
                f"**{space_label}**({space_id})"
            )
        else:
            target = f"space **{space_label}**({space_id})"
        text = format_permission_prompt(
            f"I'd like to leave {target} — approve, or keep me there?",
            detail=f"Reason: {reason.strip()}" if reason.strip() else "",
        )
        envelope = await self._send_dm(self.operator_slug, text, root_id="")
        env_id = envelope.get("envelope_id", "") if envelope else ""
        if not env_id:
            return (
                "I couldn't reach your operator to ask — no approval DM "
                "was sent. Try again later."
            )
        self._pending_leave_dms[env_id] = {
            "kind": kind,
            "space_id": space_id,
            "channel_id": channel_id,
            "space_name": space_label,
            "channel_name": channel_label or None,
            "reason": reason,
        }
        plain = self._leave_target_label(self._pending_leave_dms[env_id])
        return (
            f"Asked your operator to approve leaving {plain}. "
            f"I'll act once they reply `y` in that thread."
        )

    async def _maybe_handle_leave_reply(
        self, *, thread_root_id: str, text: str,
    ) -> bool:
        """Operator ``y``/``n`` on a pending leave-request DM. Threaded
        only — the reply must land in the approval DM's own thread.
        Returns ``True`` when consumed (caller skips the LLM)."""
        meta = self._pending_leave_dms.get(thread_root_id)
        if meta is None:
            return False
        normalized = text.strip().lower()
        if normalized in ("y", "yes"):
            approved = True
        elif normalized in ("n", "no"):
            approved = False
        else:
            return False

        kind = meta["kind"]
        space_id = meta["space_id"]
        channel_id = meta.get("channel_id") or ""
        target = self._leave_target_label(meta)
        if approved:
            try:
                await self._sign_and_post_leave(
                    kind=kind, space_id=space_id, channel_id=channel_id,
                )
                # Suppress the WS echo's generic membership DM (space only);
                # the in-thread confirm below is the authoritative report.
                if kind == EventKind.LEAVE_SPACE:
                    self._gate_left_spaces.add(space_id)
                confirm = f"Left {target}. ✓"
                self._log.info(
                    "operator-approved leave of %s (space=%s channel=%s)",
                    kind, space_id, channel_id,
                )
            except Exception as exc:
                self._log.exception(
                    "operator-approved leave of %s failed (space=%s channel=%s)",
                    kind, space_id, channel_id,
                )
                confirm = f"{format_leave_error(exc)} ({target})"
        else:
            confirm = f"Understood — I'll stay in {target}."

        self._pending_leave_dms.pop(thread_root_id, None)
        try:
            await self._send_dm(
                self.operator_slug, confirm, root_id=thread_root_id,
            )
        except Exception:
            self._log.exception(
                "failed to confirm leave-reply outcome to operator",
            )
        return True

    @staticmethod
    def _leave_target_label(meta: dict) -> str:
        """Human label for a leave's destination (space or channel)."""
        space_id = meta.get("space_id") or ""
        space_label = f"**{meta['space_name']}**" if meta.get("space_name") else space_id
        if meta.get("kind") == "leave_channel":
            channel_id = meta.get("channel_id") or ""
            channel_label = (
                f"**{meta['channel_name']}**" if meta.get("channel_name") else channel_id
            )
            return f"channel {channel_label} in space {space_label}"
        return f"space {space_label}"

    async def _channel_is_public(
        self, space_id: str, channel_id: str,
    ) -> bool | None:
        """Whether ``channel_id`` is public (and so can't be left on its
        own). ``True``/``False`` from ``GET /spaces/<id>/channels``;
        ``None`` when undeterminable, so callers fall through to the
        normal approval path rather than blocking on a flake."""
        if not space_id or not channel_id:
            return None
        try:
            data = await self.http.get(f"/spaces/{space_id}/channels")
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        for entry in data.get("channels") or []:
            if (entry.get("channel_id") or "") == channel_id:
                return bool(entry.get("is_public"))
        return None

    async def _sign_and_post_leave(
        self, *, kind: str, space_id: str, channel_id: str,
    ) -> None:
        """Sign + POST a ``leave_space`` / ``leave_channel`` event.
        Mirrors ``_accept_invite``'s signing; the leave payload uses
        ``effective_from`` (not ``accepted_at``) and the server rejects
        unknown fields, so the shapes are exact."""
        sess = self.keystore.load_session(self.slug)
        signing_key = Ed25519KeyPair.from_secret_bytes(
            decode_secret(sess.subkey_secret_key)
        )
        now_ms = int(__import__("time").time() * 1000)
        if kind == EventKind.LEAVE_CHANNEL:
            payload: dict[str, Any] = {
                "space_id": space_id,
                "channel_id": channel_id,
                "effective_from": now_ms,
                "nonce": random_nonce(),
            }
        else:
            payload = {
                "space_id": space_id,
                "effective_from": now_ms,
                "nonce": random_nonce(),
            }
        signed = sign_event(
            kind=kind,
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
        target = self._invite_target_label(meta)
        # Pretty inviter label for confirmation; cached from the
        # original DM lookup.
        inviter_display = await self._fetch_display_name(inviter_slug)
        inviter_label = (
            f"**{inviter_display}** (@{inviter_slug})"
            if inviter_display else f"@{inviter_slug}"
        )

        if verdict == "accept":
            try:
                await self._accept_invite(
                    kind, invitation_event_id, space_id, channel_id,
                )
                confirm = f"Accepted invite to {target}. ✓"
                self._log.info(
                    "operator-confirmed accept of %s (event_id=%s)",
                    kind, invitation_event_id,
                )
            except Exception as exc:
                self._log.exception(
                    "operator-confirmed accept of %s (event_id=%s) failed",
                    kind, invitation_event_id,
                )
                confirm = f"{format_invite_error(exc, 'accept')} ({target})"
        else:  # reject
            try:
                await self._reject_invite(
                    kind, invitation_event_id, space_id, channel_id,
                )
                confirm = f"Rejected invite from {inviter_label} to {target}."
                self._log.info(
                    "operator-confirmed reject of %s (event_id=%s)",
                    kind, invitation_event_id,
                )
            except Exception as exc:
                self._log.exception(
                    "operator-confirmed reject of %s (event_id=%s) failed",
                    kind, invitation_event_id,
                )
                confirm = f"{format_invite_error(exc, 'reject')} ({target})"

        # Drop from pending so a duplicate ``y`` later in the same
        # thread doesn't re-attempt (server would reject it anyway).
        self._pending_invite_dms.pop(thread_root_id, None)
        try:
            await self._send_dm(
                self.operator_slug, confirm, root_id=thread_root_id,
            )
        except Exception:
            self._log.exception(
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
        if kind == EventKind.INVITE_TO_SPACE:
            payload: dict[str, Any] = {
                "space_id": space_id,
                "invitation_event_id": invitation_event_id,
                "rejected_at": now_ms,
                "nonce": random_nonce(),
            }
            reject_kind = EventKind.REJECT_SPACE_INVITE
        else:  # invite_to_channel
            payload = {
                "space_id": space_id,
                "channel_id": channel_id,
                "invitation_event_id": invitation_event_id,
                "rejected_at": now_ms,
                "nonce": random_nonce(),
            }
            reject_kind = EventKind.REJECT_CHANNEL_INVITE
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
            self._log.warning(
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
        if kind == EventKind.INVITE_TO_SPACE:
            target = f"space {space_label}"
        else:
            channel_label = (
                f"**{channel_name}**({channel_id})" if channel_name else channel_id
            )
            target = f"channel {channel_label} in space {space_label}"
        text = format_permission_prompt(
            f"{inviter_label} invited me to {target}. "
            f"They aren't my registered operator — accept?",
            reply_note=(
                "a direct (non-threaded) `y`/`n` answers all your "
                "pending invites at once"
            ),
        )
        try:
            envelope = await self._send_dm(self.operator_slug, text, root_id="")
        except Exception:
            self._log.exception(
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
                self._log.warning("attachment meta parse failed: %r", raw)
                continue
            ciphertext = await _fetch_blob_with_retry(
                self.http, meta.blob_id,
            )
            if ciphertext is None:
                continue
            try:
                plaintext = decrypt_attachment(ciphertext, meta)
            except Exception as exc:
                self._log.warning(
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
                self._log.warning(
                    "attachment save failed (%s): %s", target, exc,
                )
                continue
            # Shrink oversized images so the agent can Read them without
            # the image dominating context (or being rejected in a
            # >20-image request). Off-loop: Pillow decode/resize is
            # blocking. When a resize fires, the in-place file becomes
            # ``<stem>.compressed<ext>`` and the pre-resize bytes are kept
            # alongside as ``<stem>.origin<ext>`` for full-fidelity access.
            origin = target.with_name(f"{target.stem}.origin{target.suffix}")
            resized = await asyncio.to_thread(
                _downscale_oversized_image, target, origin, self._image_edge_px,
            )
            if resized:
                compressed = target.with_name(
                    f"{target.stem}.compressed{target.suffix}"
                )
                try:
                    target.rename(compressed)
                    paths.append(str(compressed))
                except OSError as exc:
                    self._log.warning(
                        "could not rename compressed image %s: %s", target, exc,
                    )
                    paths.append(str(target))
            else:
                paths.append(str(target))
        return paths

    async def _send_dm(
        self, recipient_slug: str, text: str, root_id: str,
    ) -> dict | None:
        """Send a DM to a specific slug (rather than to
        ``_last_dm_sender`` like ``send_fallback_message`` does). Returns the
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
            self._log.warning(
                "no recipient devices for DM to %s — dropping", recipient_slug,
            )
            return None
        inp = EncryptInput(
            envelope_kind="dm",
            sender_slug=self.slug,
            sender_subkey_id=sess.subkey_id,
            # Operator-facing notices (invite approvals) — always visible.
            is_visible_to_human=True,
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
            self._log.exception("DM send to %s failed", recipient_slug)
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

    async def send_fallback_message(
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
            # Channel reply — resolve the space from the in-memory
            # channel→space map (populated by inbound envelopes +
            # membership events). No silent fallback to
            # ``self.space_id``: the configured "home" space is
            # legacy metadata that shouldn't decide where outbound
            # messages route. If the cache misses, the agent either
            # hasn't seen this channel yet or has been evicted from
            # it; either way, sending blindly to a guessed space
            # gets a 403 or worse (wrong-space cross-talk).
            target_space_id = self._channel_space.get(channel_id)
            if not target_space_id:
                # In-memory miss — try the persistent channel_space_map
                # before giving up. Catches the post-daemon-restart
                # case (in-memory dict empty until first inbound
                # envelope) and any membership-event path that
                # forgot to mirror to the dict. Backfill on hit so
                # subsequent calls go through the fast path.
                target_space_id = await self.store.lookup_channel_space(channel_id) or ""
                if target_space_id:
                    self._channel_space[channel_id] = target_space_id
                    self._log.info(
                        "send_fallback_message: hydrated in-memory "
                        "channel_space for %s from persistent store "
                        "(sp=%s)",
                        channel_id, target_space_id,
                    )
            if not target_space_id:
                self._log.warning(
                    "send_fallback_message: no known space for channel %s — "
                    "dropping (agent may have been removed, or the channel "
                    "id is stale)",
                    channel_id,
                )
                return
            members_resp = await self.http.get(
                f"/spaces/{target_space_id}/channels/{channel_id}/members"
            )
            member_slugs = [
                m.get("slug", "")
                for m in members_resp.get("members", [])
                if m.get("slug")
            ]
            if not member_slugs:
                self._log.warning(
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
                self._log.warning(
                    "send_fallback_message called with empty channel_id but no DM "
                    "context — dropping reply",
                )
                return
            devices = await self._fetch_device_keys([self.slug, recipient])
            envelope_kind = "dm"
            recipient_slug = recipient
            send_space_id = None
            send_channel_id = None

        if not devices:
            self._log.warning(
                "no recipient devices found (kind=%s target=%s) — dropping",
                envelope_kind, recipient_slug or channel_id,
            )
            return

        self._log.info(
            "send_fallback_message: kind=%s target=%s devices=%d",
            envelope_kind, recipient_slug or channel_id, len(devices),
        )

        # Fallback shares the visibility_level="default" floor; the
        # note is dropped (no MCP return channel).
        from ._visibility import resolve_visibility
        channel_ref = (
            f"@{recipient_slug}" if envelope_kind == "dm" else (channel_id or "")
        )
        effective_visible, _ = await resolve_visibility(
            "default", channel_ref, text, root_id or "", self.http,
        )

        inp = EncryptInput(
            envelope_kind=envelope_kind,
            sender_slug=self.slug,
            sender_subkey_id=sess.subkey_id,
            is_visible_to_human=effective_visible,
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
            self._log.info(
                "send_fallback_message sent: envelope_id=%s queued=%s",
                envelope.get("envelope_id"),
                (resp or {}).get("devices_queued"),
            )
        except Exception:
            self._log.exception("send_fallback_message: POST /messages failed")
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
