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
from .data_client import DataClient

logger = logging.getLogger(__name__)


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
    # Workspace root used by ``upload_file`` to safety-resolve LLM-
    # supplied relative paths (no ``..`` escape, no absolutes).
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
    async def send_message(channel: str, text: str, root_id: str = "") -> str:
        """Post a message to a Puffo.ai channel or DM a user.

        channel: '@<slug>' for a DM (e.g. '@alice-1234'), or a raw
            channel id (e.g. 'ch_<uuid>'). Use ``list_channels`` to
            discover ids — '#name' shortcuts are not supported.
        text: message body. Markdown preserved verbatim.
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
            # Look up the space this channel lives in. Inbound
            # envelopes are tagged with ``space_id`` in the local
            # message store, so any prior message on this channel
            # gives us the mapping. Falls back to the agent's home
            # space when we've never seen the channel — server
            # returns 403/400 for channels we have no rights on.
            send_space_id: str | None = (
                await cfg.data_client.lookup_channel_space(channel_id)
                or cfg.space_id
            )
            if not send_space_id:
                raise RuntimeError(
                    f"channel {channel_id} not seen before and the agent "
                    "has no configured `puffo_core.space_id` — can't "
                    "resolve which space to send into."
                )
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

        inp = EncryptInput(
            envelope_kind=envelope_kind,
            sender_slug=cfg.slug,
            sender_subkey_id=sess.subkey_id,
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
        return f"posted {envelope.get('envelope_id', '?')} to {channel}"

    @mcp.tool()
    async def get_channel_history(channel: str, limit: int = 20) -> str:
        """Fetch the last N messages in a channel from local storage.

        Each line shows timestamp, sender, and text. Read from the
        local store — no server round-trip. Pass a raw channel id
        (``ch_<uuid>``).
        """
        limit = max(1, min(int(limit), 200))
        channel_ref = channel.strip()
        if channel_ref.startswith("#"):
            raise RuntimeError(
                "'#<name>' channel addressing isn't supported; pass the "
                "channel id directly."
            )
        channel_id = channel_ref

        msgs = await cfg.data_client.get_channel_history(channel_id, limit)
        if not msgs:
            return "(no messages in local history)"
        rows = []
        for m in msgs:
            ts = _ts_to_iso(m.sent_at)
            text = str(m.content).replace("\n", " ") if m.content else ""
            thread = f" [thread:{m.thread_root_id[:10]}...]" if m.thread_root_id else ""
            rows.append(f"{ts}  @{m.sender_slug}: {text}{thread}")
        return "\n".join(rows)

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
            if cursor is None:
                break
            # Defensive: cursor must strictly advance whenever
            # ``has_more`` is set. A repeated value means the server
            # isn't making progress; bail instead of spinning.
            if cursor == prev_cursor:
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
        if not cfg.space_id:
            raise RuntimeError(
                "agent has no configured space_id — set "
                "`puffo_core.space_id` in agent.yml."
            )

        data = await cfg.http_client.get(
            f"/spaces/{cfg.space_id}/channels/{channel_id}/members"
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
    async def upload_file(
        paths: list[str],
        channel: str,
        caption: str = "",
        root_id: str = "",
    ) -> str:
        """Upload one or more workspace files to a channel or DM.

        All files ride in a single envelope — recipients see one
        bubble with N attachments.

        paths: workspace-relative file paths. ``..`` and absolute
            paths are rejected.
        channel: same syntax as ``send_message`` (``@<slug>`` or a
            raw channel id).
        caption: optional text alongside the files.
        root_id: optional thread reply, same semantics as
            ``send_message``'s ``root_id``.
        """
        import mimetypes
        from pathlib import Path

        if not cfg.workspace:
            raise RuntimeError(
                "upload_file: agent has no configured workspace dir"
            )
        if not paths or not isinstance(paths, list):
            raise RuntimeError("upload_file: paths is required (non-empty list)")
        if len(paths) > 10:
            raise RuntimeError(
                f"upload_file: too many files ({len(paths)} > 10 cap)"
            )
        workspace_dir = Path(cfg.workspace).resolve()

        # Validate all paths up front so a late failure doesn't
        # leave orphan blob uploads on the server.
        targets: list[Path] = []
        for raw in paths:
            rel = (raw or "").strip()
            if not rel:
                raise RuntimeError("upload_file: paths contains empty entry")
            rel_path = Path(rel)
            if rel_path.is_absolute():
                raise RuntimeError(
                    f"upload_file: absolute paths not allowed ({rel!r})"
                )
            try:
                target = (workspace_dir / rel_path).resolve()
                target.relative_to(workspace_dir)
            except (OSError, ValueError):
                raise RuntimeError(
                    f"upload_file: {rel!r} escapes the workspace"
                )
            if not target.is_file():
                raise RuntimeError(f"upload_file: {rel!r} is not a file")
            targets.append(target)

        # Encrypt + upload each file. ``blob_id`` is patched in
        # after /blobs/upload returns — AAD doesn't depend on it.
        attachment_metas: list[AttachmentMeta] = []
        total_bytes = 0
        for target in targets:
            plaintext = target.read_bytes()
            if len(plaintext) > 8 * 1024 * 1024:
                raise RuntimeError(
                    f"upload_file: {target.name!r} is {len(plaintext)} bytes "
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
                    f"upload_file: server returned no blob_id for "
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
            send_space_id = (
                await cfg.data_client.lookup_channel_space(channel_id)
                or cfg.space_id
            )
            if not send_space_id:
                raise RuntimeError(
                    f"upload_file: channel {channel_id} not seen before "
                    "and the agent has no configured space_id"
                )
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
                    f"upload_file: channel {channel_id} has no resolvable members"
                )

        devices = await _fetch_device_keys(cfg.http_client, recipient_slugs)
        if not devices:
            raise RuntimeError("upload_file: no recipient devices found")

        sess = cfg.keystore.load_session(cfg.slug)
        signing_key = Ed25519KeyPair.from_secret_bytes(
            decode_secret(sess.subkey_secret_key)
        )
        body_content = {
            "text": caption,
            "attachments": [m.to_dict() for m in attachment_metas],
        }
        inp = EncryptInput(
            envelope_kind=envelope_kind,
            sender_slug=cfg.slug,
            sender_subkey_id=sess.subkey_id,
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
        )

    @mcp.tool()
    async def fetch_channel_files(channel: str, limit: int = 20) -> str:
        """Back-fill file attachments from recent channel history.

        Note: blob query API integration is pending.
        """
        return "(fetch_channel_files: blob query API not yet implemented)"
