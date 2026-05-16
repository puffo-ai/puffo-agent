"""MCP tools for puffo-core: signed API + E2E encrypted messages.

Wire calls follow puffo-cli's conventions: ``/certs/sync`` for
device certs, ``/spaces/<sp>/channels/<ch>/members`` for channel
members, event-stream replay for channel discovery. Host-side /
local tools live in ``host_tools.py``.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from ..crypto.attachments import (
    ATTACHMENT_CONTENT_TYPE,
    AttachmentMeta,
    encrypt_attachment,
)
from ..crypto.encoding import base64url_decode, base64url_encode
from ..crypto.http_client import PuffoCoreHttpClient
from ..crypto.keystore import KeyStore, decode_secret
from ..crypto.message import EncryptInput, RecipientDevice, encrypt_message
from ..crypto.primitives import Ed25519KeyPair
from .data_client import DataClient, DataNotFound

logger = logging.getLogger(__name__)


async def _resolve_channel_space(cfg: Any, channel_id: str) -> str:
    """Resolve ``channel_id`` → ``space_id`` from the local cache.

    The cache is filled by ``puffo_core_client._handle_event`` for
    every membership event the agent receives that carries both ids
    (``invite_to_channel`` where we're the invitee,
    ``accept_channel_invite`` where we're the signer, and
    ``create_channel`` — server only fans those to space members).
    ``mark_channel_space`` is also written synchronously inside
    ``_accept_invite`` to close the WS-echo race.

    Raises ``RuntimeError`` (which propagates to the LLM as an MCP
    tool error) on miss — that's the signal "agent has no way to
    reach this channel; you may not be a member, or the id is
    wrong." Earlier code walked ``GET /spaces`` as a fallback
    resolver, but with events feeding the cache that fallback is
    redundant and silently misleading (a hit there only proved
    access to the space, not membership in the channel).
    """
    space_id = await cfg.data_client.lookup_channel_space(channel_id)
    if not space_id:
        raise RuntimeError(
            f"agent has no record of channel {channel_id} — it may not "
            f"be a channel the agent belongs to, or the id may be "
            f"wrong. Membership events are cached as they arrive over "
            f"the WS, so a fresh channel becomes addressable as soon "
            f"as the invite/accept/create event lands."
        )
    return space_id


def _ts_to_iso(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class PuffoCoreToolsConfig:
    slug: str
    device_id: str
    keystore: KeyStore
    http_client: PuffoCoreHttpClient
    data_client: DataClient
    space_id: Optional[str] = None
    # Workspace root used by ``send_message_with_attachments`` to
    # safety-resolve LLM-supplied relative paths (no ``..`` escape,
    # no absolutes).
    workspace: Optional[str] = None


async def _fetch_device_keys(
    http_client: PuffoCoreHttpClient,
    slugs: list[str],
) -> list[RecipientDevice]:
    """Paginate ``/certs/sync?slugs=...`` and collect
    ``(device_id, kem_pk)`` for every returned device_cert.
    """
    if not slugs:
        return []
    slugs_param = ",".join(slugs)
    devices: list[RecipientDevice] = []
    seen_ids: set[str] = set()
    since = 0
    while True:
        data = await http_client.get(
            f"/certs/sync?slugs={slugs_param}&since={since}"
        )
        for entry in data.get("entries", []):
            if entry.get("kind") == "device_cert":
                cert = entry.get("cert", {})
                dev_id = cert.get("device_id", "")
                # v2 nests under ``keys.encryption.public_key``; fall
                # back to the v1 flat field for legacy entries.
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
                        # Skip malformed entry; don't abort the fetch.
                        pass
            since = entry.get("seq", since)
        if not data.get("has_more"):
            break
    return devices


def _coerce_root_visibility(
    is_visible_to_human: bool, root_id: str,
) -> tuple[bool, str]:
    """Root-level (non-threaded) messages can't fold in the human UI —
    only threaded replies do. When an agent marks a root-level message
    ``is_visible_to_human=false`` we coerce it back to visible (so the
    message still goes out) and return a note to splice into the tool
    response. The agent learns from the tool result on the spot,
    rather than depending on the primer being current.

    Returns ``(effective_visibility, note)`` — ``note`` is empty
    unless a coercion happened.
    """
    if is_visible_to_human is False and not root_id.strip():
        return True, (
            "\nnote: is_visible_to_human=false ignored — root-level "
            "messages can't fold; sent as visible. Use false only on "
            "threaded replies (pass root_id)."
        )
    return is_visible_to_human, ""


def register_core_tools(mcp: FastMCP, cfg: PuffoCoreToolsConfig) -> None:

    @mcp.tool()
    async def whoami() -> str:
        """Return your own identity: slug, device_id, and subkey info."""
        identity = cfg.keystore.load_identity(cfg.slug)
        lines = [
            f"slug:      {identity.slug}",
            f"device_id: {identity.device_id}",
            f"server:    {identity.server_url}",
        ]
        try:
            sess = cfg.keystore.load_session(cfg.slug)
            lines.append(f"subkey_id: {sess.subkey_id}")
            lines.append(f"expires:   {_ts_to_iso(sess.expires_at)}")
        except FileNotFoundError:
            lines.append("subkey:    (no active session)")
        return "\n".join(lines)

    @mcp.tool()
    async def send_message(
        channel: str,
        text: str,
        is_visible_to_human: bool,
        root_id: str = "",
    ) -> str:
        """Post a message to a Puffo.ai channel or DM a user.

        channel: '@<slug>' for a DM (e.g. '@alice-1234'), or a raw
            channel id (e.g. 'ch_<uuid>'). Use ``list_channels`` to
            discover ids — '#name' shortcuts are not supported.
        text: message body. Markdown preserved verbatim.
        is_visible_to_human: REQUIRED — decide whether a human should
            see this message inline. ``true`` for anything a person
            needs to read; ``false`` for agent-to-agent coordination
            chatter, which human clients fold away. There is no
            default — judge every message. NOTE: ``false`` only takes
            effect on threaded replies (when ``root_id`` is set) —
            root-level messages can't fold, so ``false`` on one is
            ignored and the message is sent visible.
        root_id: optional — reply inside a thread; pass the
            envelope_id of the message you're replying to.
        """
        channel_ref = channel.strip()
        if not channel_ref:
            raise RuntimeError("channel is required")
        if channel_ref.startswith("#"):
            raise RuntimeError(
                "'#<name>' channel addressing isn't supported; "
                "use the channel id (e.g. 'ch_<uuid>') or call "
                "list_channels to look one up."
            )

        if channel_ref.startswith("@"):
            recipient_slug = channel_ref[1:]
            if not recipient_slug:
                raise RuntimeError("DM recipient slug is required after '@'")
            envelope_kind = "dm"
            channel_id: Optional[str] = None
            send_space_id: Optional[str] = None
            # Fan to the recipient AND our own other devices so any
            # other logged-in clients see the DM too.
            recipient_slugs = [cfg.slug, recipient_slug]
        else:
            channel_id = channel_ref
            envelope_kind = "channel"
            recipient_slug = None
            # The local cache (filled by membership events landing
            # over the WS — see puffo_core_client._handle_event /
            # _maybe_cache_channel_space) is authoritative for
            # channels the agent can reach. Miss → bail loud.
            send_space_id = await _resolve_channel_space(cfg, channel_id)
            members_resp = await cfg.http_client.get(
                f"/spaces/{send_space_id}/channels/{channel_id}/members"
            )
            recipient_slugs = [
                m.get("slug", "")
                for m in members_resp.get("members", [])
                if m.get("slug")
            ]
            if not recipient_slugs:
                raise RuntimeError(
                    f"channel {channel_id} has no resolvable members "
                    f"(searched space {send_space_id})"
                )

        devices = await _fetch_device_keys(cfg.http_client, recipient_slugs)
        if not devices:
            raise RuntimeError("no recipient devices found")

        sess = cfg.keystore.load_session(cfg.slug)
        signing_key = Ed25519KeyPair.from_secret_bytes(
            decode_secret(sess.subkey_secret_key)
        )

        effective_visible, fold_note = _coerce_root_visibility(
            is_visible_to_human, root_id,
        )
        inp = EncryptInput(
            envelope_kind=envelope_kind,
            sender_slug=cfg.slug,
            sender_subkey_id=sess.subkey_id,
            is_visible_to_human=effective_visible,
            space_id=send_space_id,
            channel_id=channel_id,
            recipient_slug=recipient_slug,
            thread_root_id=root_id if root_id else None,
            content_type="text/plain",
            content=text,
            recipients=devices,
        )
        envelope = encrypt_message(inp, signing_key)
        # Server expects the envelope at the top level, not wrapped.
        await cfg.http_client.post("/messages", envelope)
        return (
            f"posted {envelope.get('envelope_id', '?')} to {channel}"
            f"{fold_note}"
        )

    @mcp.tool()
    async def get_channel_history(
        channel: str,
        limit: int = 20,
        since: str = "",
        before: int = 0,
        after: int = 0,
    ) -> str:
        """List recent **root posts** in a channel from local storage,
        with the reply count for each thread.

        Replies are NOT inlined — call ``get_thread_history`` if you
        want to drill into a specific thread. This keeps a single
        ``get_channel_history`` call from dragging hundreds of replies
        into your context just because one thread is active.

        Filters (optional, can be combined):
        - ``since`` — an envelope_id (``msg_<uuid>``). Results have
          ``sent_at >`` that envelope's ``sent_at``. Use this when
          you remember the latest root you already saw.
        - ``after`` — ms-epoch timestamp; exclusive lower bound.
        - ``before`` — ms-epoch timestamp; exclusive upper bound.

        Output lines: ``<ts>  @<sender>: <text>  (N replies)`` where
        ``N`` is the current reply count (omitted for 0). Oldest-
        first inside the returned window. Channel id is a raw
        ``ch_<uuid>`` (no ``#name`` shortcut)."""
        limit = max(1, min(int(limit), 200))
        channel_ref = channel.strip()
        if channel_ref.startswith("#"):
            raise RuntimeError(
                "'#<name>' channel addressing isn't supported; pass the "
                "channel id directly."
            )
        channel_id = channel_ref

        try:
            roots = await cfg.data_client.get_channel_roots(
                channel_id,
                limit=limit,
                since_envelope_id=since or None,
                before_ts=int(before) if before else None,
                after_ts=int(after) if after else None,
            )
        except DataNotFound:
            return f"(no such channel: {channel_id})"
        if not roots:
            return "(no root posts in the requested window)"
        lines = []
        for entry in roots:
            m = entry.message
            ts = _ts_to_iso(m.sent_at)
            text = str(m.content).replace("\n", " ") if m.content else ""
            suffix = (
                f"  ({entry.reply_count} repl{'y' if entry.reply_count == 1 else 'ies'})"
                if entry.reply_count > 0 else ""
            )
            lines.append(
                f"{ts}  post:{m.envelope_id}  @{m.sender_slug}: {text}{suffix}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def get_thread_history(
        root_id: str,
        limit: int = 50,
        since: str = "",
        before: int = 0,
        after: int = 0,
    ) -> str:
        """List messages in one thread (the root post + every reply
        that points at it) from local storage.

        Used after ``get_channel_history`` shows a thread you want
        to read into. Same filter semantics as
        ``get_channel_history``: ``since`` is an envelope_id whose
        ``sent_at`` becomes the exclusive lower bound; ``after`` /
        ``before`` are ms-epoch bounds. All filters optional.

        ``root_id`` is the thread root envelope_id (``msg_<uuid>``).
        For a top-level post that has no replies, this returns just
        that post.

        Output lines: ``<ts>  post:<envelope_id>  @<sender>: <text>``,
        oldest-first."""
        if not root_id.strip():
            raise RuntimeError("root_id required")
        limit = max(1, min(int(limit), 200))
        try:
            msgs = await cfg.data_client.get_thread_messages(
                root_id.strip(),
                limit=limit,
                since_envelope_id=since or None,
                before_ts=int(before) if before else None,
                after_ts=int(after) if after else None,
            )
        except DataNotFound:
            return f"(no such thread: {root_id.strip()})"
        if not msgs:
            return "(no messages in this thread for the requested window)"
        lines = []
        for m in msgs:
            ts = _ts_to_iso(m.sent_at)
            text = str(m.content).replace("\n", " ") if m.content else ""
            lines.append(
                f"{ts}  post:{m.envelope_id}  @{m.sender_slug}: {text}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def list_channels() -> str:
        """List channels in the agent's configured space (id + name).

        Channels are derived by replaying ``/spaces/<sp>/events`` and
        surfacing every ``create_channel`` payload — there is no
        direct ``/spaces/<sp>/channels`` endpoint.
        """
        if not cfg.space_id:
            return "(no space configured)"
        space_id = cfg.space_id
        # cursor is ``<issued_at>:<signer_slug>:<event_id>``. Colons
        # are legal in query strings but encode anyway for safety.
        cursor: Optional[str] = None
        prev_cursor: Optional[str] = None
        channels: list[tuple[str, str]] = []
        while True:
            if cursor is not None:
                path = (
                    f"/spaces/{space_id}/events"
                    f"?since={urllib.parse.quote(cursor, safe='')}"
                )
            else:
                path = f"/spaces/{space_id}/events"
            data = await cfg.http_client.get(path)
            for entry in data.get("events", []):
                if entry.get("kind") == "create_channel":
                    payload = entry.get("payload", {}) or {}
                    cid = payload.get("channel_id", "")
                    name = payload.get("name", "")
                    if cid:
                        channels.append((cid, name))
            if not data.get("has_more"):
                break
            prev_cursor = cursor
            cursor = data.get("next_cursor")
            if cursor is None or cursor == prev_cursor:
                break
        if not channels:
            return "(no channels in this space)"
        return "\n".join(f"- {cid}  {name}" for cid, name in channels)

    @mcp.tool()
    async def list_channel_members(channel: str) -> str:
        """List the members of a channel as ``- <slug>  (<role>)``.
        Role is one of owner / admin / member.
        """
        channel_ref = channel.strip()
        if channel_ref.startswith("#"):
            raise RuntimeError(
                "'#<name>' channel addressing isn't supported; pass the "
                "channel id directly."
            )
        channel_id = channel_ref
        # Resolve from the local channel→space cache (populated by
        # membership events). Misses raise — the previous version
        # silently used ``cfg.space_id`` (home space), which broke
        # for any channel not in the agent's home space.
        space_id = await _resolve_channel_space(cfg, channel_id)

        data = await cfg.http_client.get(
            f"/spaces/{space_id}/channels/{channel_id}/members"
        )
        rows = []
        for m in data.get("members", []):
            slug = m.get("slug", "?")
            role = m.get("role") or "member"
            rows.append(f"- {slug}  ({role})")
        return "\n".join(rows) or "(empty channel)"

    @mcp.tool()
    async def get_user_info(username: str) -> str:
        """Look up a user by slug or @-handle.
        Returns slug, display name, bio, and avatar URL when set.
        """
        slug = (username or "").lstrip("@").strip()
        if not slug:
            raise RuntimeError("username is required")
        # ``/identities/profiles?slugs=`` accepts a comma-separated
        # list; we read back the first entry. Empty list means the
        # slug isn't registered.
        data = await cfg.http_client.get(
            f"/identities/profiles?slugs={urllib.parse.quote(slug, safe='')}"
        )
        profiles = data.get("profiles", []) if isinstance(data, dict) else []
        if not profiles:
            return f"(no profile for {slug})"
        p = profiles[0]
        lines = [f"slug: {p.get('slug', slug)}"]
        if p.get("username"):
            lines.append(f"display: {p['username']}")
        if p.get("bio"):
            lines.append(f"bio: {p['bio']}")
        if p.get("avatar_url"):
            lines.append(f"avatar: {p['avatar_url']}")
        return "\n".join(lines)

    @mcp.tool()
    async def get_post(post_ref: str) -> str:
        """Fetch one message by its envelope_id from local storage.

        post_ref: an envelope_id (e.g. 'env_...'). Returns sender,
        timestamp, and message text.
        """
        envelope_id = (post_ref or "").strip()
        if not envelope_id:
            raise RuntimeError("post_ref (envelope_id) is required")

        msg = await cfg.data_client.get_message_by_envelope(envelope_id)
        if msg is None:
            return f"message {envelope_id} not found in local storage"

        ts = _ts_to_iso(msg.sent_at)
        content_str = str(msg.content) if msg.content else ""
        lines = [
            f"envelope_id: {msg.envelope_id}",
            f"sender: @{msg.sender_slug}",
            f"timestamp: {ts}",
            f"kind: {msg.envelope_kind}",
        ]
        if msg.channel_id:
            lines.append(f"channel_id: {msg.channel_id}")
        if msg.thread_root_id:
            lines.append(f"thread_root_id: {msg.thread_root_id}")
        lines.append(f"message:\n{content_str}")
        return "\n".join(lines)

    @mcp.tool()
    async def get_post_segment(
        envelope_id: str,
        segment: int,
        segment_size: int = 2000,
    ) -> str:
        """Page a long message body back in chunks.

        When the daemon redacts an oversize inbound message it
        replaces the in-prompt body with a ``[puffo-agent system
        message]`` placeholder citing this tool's name plus the
        envelope_id and total segment count. Call this tool with
        ``segment=N`` (zero-indexed) and the same ``segment_size``
        the placeholder reported to retrieve chunk ``N`` of the
        full body. Only fetch the segments you actually need —
        the placeholder preview usually tells you whether the
        content is worth paging through.

        Returns: ``segment <i>/<total> (chars <start>..<end> of <total>):
        \n<chunk body>``. Out-of-range segment numbers return
        ``segment out of range`` so the agent knows it overshot.

        Special cases:
          * unknown envelope_id → "message <id> not found in local storage"
          * empty content       → "message <id> has no text body"

        ``segment_size`` defaults to 2000 to match the daemon's
        default redaction page size; pass the value the placeholder
        cited if the operator has overridden it on their host.
        """
        envelope_id = (envelope_id or "").strip()
        if not envelope_id:
            raise RuntimeError("envelope_id is required")
        if segment < 0:
            raise RuntimeError("segment must be >= 0")
        if segment_size <= 0:
            raise RuntimeError("segment_size must be > 0")

        msg = await cfg.data_client.get_message_by_envelope(envelope_id)
        if msg is None:
            return f"message {envelope_id} not found in local storage"

        # ``content`` carries either a bare string (plain message)
        # or the ``puffo/message+attachments/v1`` dict shape; pull
        # the text out of the latter so segmenting works on the
        # human-readable portion in both cases.
        content = msg.content
        if isinstance(content, dict):
            text = str(content.get("text") or "")
        else:
            text = str(content) if content else ""

        if not text:
            return f"message {envelope_id} has no text body"

        total = len(text)
        # ceil(total / segment_size); at least 1 when total > 0.
        seg_count = (total + segment_size - 1) // segment_size
        if segment >= seg_count:
            return (
                f"segment {segment} out of range (envelope_id={envelope_id} "
                f"has {seg_count} segment(s) at segment_size={segment_size}, "
                "indexed 0..{0})".format(seg_count - 1)
            )
        start = segment * segment_size
        end = min(start + segment_size, total)
        chunk = text[start:end]
        return (
            f"segment {segment}/{seg_count - 1} "
            f"(chars {start}..{end - 1} of {total}):\n{chunk}"
        )

    @mcp.tool()
    async def send_message_with_attachments(
        paths: list[str],
        channel: str,
        is_visible_to_human: bool,
        caption: str = "",
        root_id: str = "",
    ) -> str:
        """Send a message carrying one or more workspace files to a
        channel or DM.

        All files ride in a single envelope — recipients see one
        message bubble with N attachments.

        paths: workspace-relative file paths. ``..`` and absolute
            paths are rejected.
        channel: same syntax as ``send_message`` (``@<slug>`` or a
            raw channel id).
        is_visible_to_human: REQUIRED — same semantics as
            ``send_message``: ``true`` when a person should see this,
            ``false`` for agent-to-agent chatter that human clients
            fold away. No default — judge every send. NOTE: ``false``
            only takes effect on threaded replies (when ``root_id`` is
            set); on a root-level message it's ignored and the
            message is sent visible.
        caption: optional text alongside the files.
        root_id: optional thread reply, same semantics as
            ``send_message``'s ``root_id``.
        """
        import mimetypes
        from pathlib import Path

        if not cfg.workspace:
            raise RuntimeError(
                "send_message_with_attachments: agent has no configured "
                "workspace dir"
            )
        if not paths or not isinstance(paths, list):
            raise RuntimeError(
                "send_message_with_attachments: paths is required "
                "(non-empty list)"
            )
        if len(paths) > 10:
            raise RuntimeError(
                f"send_message_with_attachments: too many files "
                f"({len(paths)} > 10 cap)"
            )
        workspace_dir = Path(cfg.workspace).resolve()

        # Validate all paths up front so a late failure doesn't
        # leave orphan blob uploads on the server.
        targets: list[Path] = []
        for raw in paths:
            rel = (raw or "").strip()
            if not rel:
                raise RuntimeError(
                    "send_message_with_attachments: paths contains empty entry"
                )
            rel_path = Path(rel)
            if rel_path.is_absolute():
                raise RuntimeError(
                    f"send_message_with_attachments: absolute paths not "
                    f"allowed ({rel!r})"
                )
            try:
                target = (workspace_dir / rel_path).resolve()
                target.relative_to(workspace_dir)
            except (OSError, ValueError):
                raise RuntimeError(
                    f"send_message_with_attachments: {rel!r} escapes the "
                    f"workspace"
                )
            if not target.is_file():
                raise RuntimeError(
                    f"send_message_with_attachments: {rel!r} is not a file"
                )
            targets.append(target)

        # Encrypt + upload each file. ``blob_id`` is patched in
        # after /blobs/upload returns — AAD doesn't depend on it.
        attachment_metas: list[AttachmentMeta] = []
        total_bytes = 0
        for target in targets:
            plaintext = target.read_bytes()
            if len(plaintext) > 8 * 1024 * 1024:
                raise RuntimeError(
                    f"send_message_with_attachments: {target.name!r} is {len(plaintext)} bytes "
                    "(server caps at 8 MiB)"
                )
            mime_type, _ = mimetypes.guess_type(target.name)
            mime_type = mime_type or "application/octet-stream"
            ciphertext, meta = encrypt_attachment(
                plaintext=plaintext,
                filename=target.name,
                mime_type=mime_type,
                blob_id="",
            )
            upload = await cfg.http_client.post_bytes(
                "/blobs/upload", ciphertext,
            )
            blob_id = upload.get("blob_id") if isinstance(upload, dict) else None
            if not blob_id:
                raise RuntimeError(
                    f"send_message_with_attachments: server returned no blob_id for "
                    f"{target.name!r} ({upload!r})"
                )
            meta.blob_id = blob_id
            attachment_metas.append(meta)
            total_bytes += len(plaintext)

        # Compose one envelope carrying all attachments, reusing
        # ``send_message``'s routing logic.
        channel_ref = channel.strip()
        if channel_ref.startswith("#"):
            raise RuntimeError(
                "'#<name>' channel addressing isn't supported; pass the "
                "channel id directly."
            )
        if channel_ref.startswith("@"):
            recipient_slug = channel_ref[1:]
            if not recipient_slug:
                raise RuntimeError("DM recipient slug is required after '@'")
            envelope_kind = "dm"
            channel_id: Optional[str] = None
            send_space_id: Optional[str] = None
            recipient_slugs = [cfg.slug, recipient_slug]
        else:
            channel_id = channel_ref
            envelope_kind = "channel"
            recipient_slug = None
            # Same cache-only resolution as ``send_message`` — no
            # silent fallback to ``cfg.space_id``, because targeting
            # the wrong space produced FB-76 mismaps.
            send_space_id = await _resolve_channel_space(cfg, channel_id)
            members_resp = await cfg.http_client.get(
                f"/spaces/{send_space_id}/channels/{channel_id}/members"
            )
            recipient_slugs = [
                m.get("slug", "")
                for m in members_resp.get("members", [])
                if m.get("slug")
            ]
            if not recipient_slugs:
                raise RuntimeError(
                    f"send_message_with_attachments: channel {channel_id} has no resolvable members"
                )

        devices = await _fetch_device_keys(cfg.http_client, recipient_slugs)
        if not devices:
            raise RuntimeError("send_message_with_attachments: no recipient devices found")

        sess = cfg.keystore.load_session(cfg.slug)
        signing_key = Ed25519KeyPair.from_secret_bytes(
            decode_secret(sess.subkey_secret_key)
        )
        body_content = {
            "text": caption,
            "attachments": [m.to_dict() for m in attachment_metas],
        }
        effective_visible, fold_note = _coerce_root_visibility(
            is_visible_to_human, root_id,
        )
        inp = EncryptInput(
            envelope_kind=envelope_kind,
            sender_slug=cfg.slug,
            sender_subkey_id=sess.subkey_id,
            is_visible_to_human=effective_visible,
            space_id=send_space_id,
            channel_id=channel_id,
            recipient_slug=recipient_slug,
            thread_root_id=root_id if root_id else None,
            content_type=ATTACHMENT_CONTENT_TYPE,
            content=body_content,
            recipients=devices,
        )
        envelope = encrypt_message(inp, signing_key)
        await cfg.http_client.post("/messages", envelope)
        names = ", ".join(t.name for t in targets)
        thread_note = f" in thread {root_id}" if root_id else ""
        return (
            f"uploaded {len(targets)} file(s) [{names}] ({total_bytes} bytes "
            f"total) to {channel}{thread_note} "
            f"(envelope_id {envelope.get('envelope_id', '?')})"
            f"{fold_note}"
        )

    @mcp.tool()
    async def fetch_channel_files(channel: str, limit: int = 20) -> str:
        """Back-fill file attachments from recent channel history.

        Note: blob query API integration is pending.
        """
        return "(fetch_channel_files: blob query API not yet implemented)"
