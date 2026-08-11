"""Puffo Core message transport, durable receipt storage, and sending."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

from ..crypto import ws_client as _ws_config
from ..crypto.http_client import PuffoCoreHttpClient
from ..crypto.keystore import KeyStore, decode_secret
from ..crypto.message import (
    MessagePayload,
    RecipientDevice,
    decrypt_message,
    read_plaintext_message,
)
from ..crypto.primitives import Ed25519KeyPair, KemKeyPair
from ..crypto.ws_client import (
    PuffoCoreWsClient,
)
from ..limits import (
    DEFAULT_CATCHUP_STALE_HOURS,
    MAX_INLINE_MESSAGE_CHARS,
    MESSAGE_SEGMENT_CHARS,
)
from . import disk_cache as _disk_cache
from . import inbound_attachments as _attachment_helpers
from . import message_context as _message_context

if TYPE_CHECKING:
    from .bridge_client import CloudBridgeClient
from ._invite_strings import format_invite_error, format_leave_error
from .bridge_transport import (
    ack_bridge_envelope,
    dispatch_bridge_frame,
    listen_bridge,
    payload_from_bridge_frame,
    preseed_frame_display_name,
    refresh_bridge_spaces,
    save_inbound_bridge_attachments,
    store_bridge_payload,
)
from .client_support import (
    parse_operator_pubkey as _parse_operator_pubkey,
)
from .directory_cache import (
    bulk_fetch_profiles,
    fetch_and_cache_avatar,
    fetch_owner_slug,
    fetch_user_profile,
    get_space_members,
    resolve_channel_name,
    resolve_space_name,
    warm_channels_for_space,
    warm_member_caches,
)
from .directory_cache import (
    set_profile as cache_profile,
)
from .event_kinds import EventKind
from .events import random_nonce, sign_event
from .inbound_attachments import (
    _DEFAULT_IMAGE_EDGE_PX,
    save_inbound_attachments,
)
from .inbound_attachments import (
    downscale_oversized_image as _downscale_oversized_image,
)
from .inbound_attachments import (
    fetch_blob_with_retry as _fetch_blob_with_retry,
)
from .inbound_attachments import (
    strip_multipart_wrapper as _strip_multipart_wrapper,
)
from .inbound_receipts import InboundReceiptHandler
from .membership_actions import (
    accept_invite,
    apply_invite_replies,
    enqueue_channel_intro_nudge,
    enqueue_membership_system_message,
    find_public_general_channel,
    invite_target_label,
    maybe_announce_membership_change,
    maybe_announce_space_membership_change,
    pick_space_channel,
    resolve_invite_targets,
    send_invite_bulk_summary,
)
from .membership_events import (
    dm_operator_membership_change,
    evict_channel_caches,
    evict_space_caches,
    fetch_inviter_root_pubkey,
    invite_poll_loop,
    inviter_is_operator,
    maybe_cache_channel_space,
    on_invite_canceled,
    on_kicked_from_channel,
    on_kicked_from_space,
    on_left_space,
    poll_pending_invites,
    process_invite,
    report_auto_accepted_channel_invite,
    report_auto_accepted_space_invite,
    still_member_of_space,
)
from .message_store import MessageStore
from .outbound_messages import (
    fetch_device_keys,
    send_bridge_fallback_dm,
    send_direct_message,
    send_native_fallback_dm,
)
from .permission_prompt import format_permission_prompt
from .thread_context import (
    resolve_incoming_thread_root,
    validate_incoming_parent_id,
    validated_parent,
)

logger = logging.getLogger(__name__)

# Keep the former helper surface stable while implementations live in focused
# modules. Production callers and third-party tests still import these names.
_HIGH_RES_IMAGE_EDGE_PX = _attachment_helpers._HIGH_RES_IMAGE_EDGE_PX
max_image_edge_px = _attachment_helpers.max_image_edge_px
_MENTION_RE = _message_context.MENTION_RE
_maybe_redact_long_text = _message_context.maybe_redact_long_text
INITIAL_BACKOFF = _ws_config.INITIAL_BACKOFF
MAX_BACKOFF = _ws_config.MAX_BACKOFF
disk_cache = _disk_cache


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
        auto_accept_dm: bool = False,
        workspace: str = "",
        max_inline_chars: int = MAX_INLINE_MESSAGE_CHARS,
        segment_chars: int = MESSAGE_SEGMENT_CHARS,
        agent_created_at: int = 0,
        image_edge_px: int = _DEFAULT_IMAGE_EDGE_PX,
        catchup_stale_hours: float = DEFAULT_CATCHUP_STALE_HOURS,
        *,
        agent_id: str = "",
        bridge_client: CloudBridgeClient | None = None,
    ):
        from .client_setup import initial_client_state

        self.__dict__.update(
            initial_client_state(
                slug=slug,
                device_id=device_id,
                space_id=space_id,
                keystore=keystore,
                http_client=http_client,
                message_store=message_store,
                operator_slug=operator_slug,
                auto_accept_space_invitations=auto_accept_space_invitations,
                auto_accept_dm=auto_accept_dm,
                workspace=workspace,
                max_inline_chars=max_inline_chars,
                segment_chars=segment_chars,
                agent_created_at=agent_created_at,
                image_edge_px=image_edge_px,
                catchup_stale_hours=catchup_stale_hours,
                agent_id=agent_id,
                bridge_client=bridge_client,
            )
        )
        bridge_connected = getattr(self._bridge, "add_connected_callback", None)
        if callable(bridge_connected):
            bridge_connected(self._notify_connected_callbacks)

    def add_connected_callback(
        self,
        callback: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Register a bounded background signal for native and bridge connects."""
        self._connected_callbacks.append(callback)

    async def _notify_connected_callbacks(self) -> None:
        for callback in tuple(getattr(self, "_connected_callbacks", ())):
            try:
                await callback()
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "transport connected callback failed category=%s",
                    type(exc).__name__,
                )

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
            self._stale_flush_task = asyncio.ensure_future(self._flush_stale_reports())

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
                    {"runs": runs[i : i + 200]},
                )
            except Exception as exc:  # noqa: BLE001
                self._log.debug(
                    "stale-processed flush failed (%d runs): %s",
                    len(runs[i : i + 200]),
                    exc,
                )

    async def listen(
        self,
        on_message: Callable[..., Any] | None = None,
    ) -> None:
        """Listen for durable receipts.

        ``on_message`` is an unused optional positional argument retained for
        the frozen ws-local route. Provider work is scheduled exclusively by
        ``GlobalInboxRuntime``.
        """
        if getattr(self, "_bridge", None) is not None:
            await self._listen_bridge()
            return

        identity = self.keystore.load_identity(self.slug)
        kem_kp = KemKeyPair.from_secret_bytes(decode_secret(identity.kem_secret_key))

        # Cache the operator's root pubkey from our identity_cert.
        # ``declared_operator_public_key`` is base64url of the
        # operator's 32-byte ed25519 root pubkey — set at provision
        # time and immutable.
        self._operator_root_pubkey = _parse_operator_pubkey(
            identity.identity_cert_json,
        )

        receipt_handler = InboundReceiptHandler(
            self,
            kem_kp,
            decrypt=decrypt_message,
            read_plaintext=read_plaintext_message,
        )

        invite_poll_task = asyncio.ensure_future(self._invite_poll_loop())
        self._ws = PuffoCoreWsClient(
            server_url=self.keystore.load_identity(self.slug).server_url,
            keystore=self.keystore,
            slug=self.slug,
            http_client=self.http,
        )
        self._ws.on_message = receipt_handler.handle
        self._ws.on_event = self._handle_event
        # Re-warms caches on every (re)connect, first connect included.
        self._ws.on_connect = self._on_ws_connect
        await self.store.open()
        try:
            await self._ws.run()
        finally:
            invite_poll_task.cancel()
            if self._warm_task is not None:
                self._warm_task.cancel()
            for task in (invite_poll_task, self._warm_task):
                if task is None:
                    continue
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def recover_pending_delivery(self, envelope_id: str) -> bool:
        """Fetch a late held watermark through the signed pending path."""
        if self._bridge is not None or self._ws is None:
            return False
        return await self._ws.recover_pending_until(envelope_id)

    async def _listen_bridge(self) -> None:
        await listen_bridge(self)

    async def _dispatch_bridge_frame(self, frame: dict) -> Any:
        return await dispatch_bridge_frame(self, frame)

    async def _ack_bridge_envelope(self, envelope_ids: list[str]) -> None:
        await ack_bridge_envelope(self, envelope_ids)

    async def _refresh_bridge_spaces(self, trigger_space_id: str = "") -> None:
        await refresh_bridge_spaces(self, trigger_space_id)

    def _preseed_frame_display_name(
        self,
        frame: dict,
        payload: MessagePayload,
    ) -> None:
        preseed_frame_display_name(self, frame, payload)

    async def _store_bridge_payload(
        self,
        payload: MessagePayload,
        *,
        server_seq: int | None = None,
    ) -> bool:
        return await store_bridge_payload(self, payload, server_seq=server_seq)

    def _payload_from_bridge_frame(self, frame: dict) -> MessagePayload | None:
        return payload_from_bridge_frame(self, frame)

    async def _invite_poll_loop(self) -> None:
        await invite_poll_loop(
            poll_invites=self._poll_pending_invites,
            next_interval=self._next_invite_poll_interval,
            sleep=asyncio.sleep,
        )

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
        await self._prepare_membership_event(kind, event, payload)
        if await self._handle_invitation_event(kind, event, payload):
            return
        if await self._handle_channel_accept_event(kind, event, payload):
            return
        if await self._handle_self_exit_event(kind, event, payload):
            return
        if await self._handle_membership_announcement_event(kind, event, payload):
            return
        await self._handle_invite_cancellation_event(kind, payload)

    async def _prepare_membership_event(
        self,
        kind: str | None,
        event: dict,
        payload: dict,
    ) -> None:
        try:
            await self._maybe_cache_channel_space(kind, event, payload)
        except Exception:
            self._log.exception("mark_channel_space from %s failed", kind)
        if kind in (
            EventKind.ACCEPT_SPACE_INVITE,
            EventKind.LEAVE_SPACE,
            EventKind.REMOVE_FROM_SPACE,
        ):
            space_id = payload.get("space_id") or ""
            if space_id:
                self._space_members.pop(space_id, None)

    async def _handle_invitation_event(
        self,
        kind: str | None,
        event: dict,
        payload: dict,
    ) -> bool:
        if kind not in (EventKind.INVITE_TO_SPACE, EventKind.INVITE_TO_CHANNEL):
            return False
        event_id = event.get("event_id") or ""
        inviter_slug = event.get("signer_slug") or ""
        if event_id and inviter_slug:
            self._inviter_by_invitation_event_id[event_id] = inviter_slug
        if payload.get("invitee_slug") == self.slug:
            await self._poll_pending_invites()
        return True

    async def _handle_channel_accept_event(
        self,
        kind: str | None,
        event: dict,
        payload: dict,
    ) -> bool:
        if kind != EventKind.ACCEPT_CHANNEL_INVITE:
            return False
        if event.get("signer_slug") != self.slug:
            await self._maybe_announce_membership_change(kind, event, payload)
            return True
        original_invite = payload.get("original_invite")
        if not isinstance(original_invite, dict):
            return True
        space_id = payload.get("space_id") or ""
        channel_id = payload.get("channel_id") or ""
        if not space_id or not channel_id:
            return True
        try:
            await self._enqueue_channel_intro_nudge(
                space_id=space_id,
                channel_id=channel_id,
            )
        except Exception:
            self._log.exception(
                "failed to enqueue intro nudge for server-auto-accepted "
                "channel (space=%s channel=%s)",
                space_id,
                channel_id,
            )
        await self._report_auto_accepted_channel_invite(
            inviter_slug=original_invite.get("signer_slug") or "",
            space_id=space_id,
            channel_id=channel_id,
        )
        return True

    async def _handle_self_exit_event(
        self,
        kind: str | None,
        event: dict,
        payload: dict,
    ) -> bool:
        if kind == EventKind.LEAVE_SPACE and event.get("signer_slug") == self.slug:
            await self._on_left_space(
                space_id=payload.get("space_id") or "",
                synthetic=str(event.get("signature") or "").startswith(
                    "server-auto:agent-cascade-leave-space"
                ),
            )
            return True
        if (
            kind == EventKind.REMOVE_FROM_SPACE
            and payload.get("removed_slug") == self.slug
        ):
            await self._on_kicked_from_space(
                space_id=payload.get("space_id") or "",
                kicker_slug=event.get("signer_slug") or "",
            )
            return True
        if kind == EventKind.LEAVE_CHANNEL and event.get("signer_slug") == self.slug:
            await self._on_left_channel(channel_id=payload.get("channel_id") or "")
            return True
        if (
            kind == EventKind.REMOVE_FROM_CHANNEL
            and payload.get("removed_slug") == self.slug
        ):
            await self._on_kicked_from_channel(
                channel_id=payload.get("channel_id") or "",
                space_id=payload.get("space_id") or "",
                kicker_slug=event.get("signer_slug") or "",
            )
            return True
        return False

    async def _handle_membership_announcement_event(
        self,
        kind: str | None,
        event: dict,
        payload: dict,
    ) -> bool:
        if kind in (EventKind.LEAVE_CHANNEL, EventKind.REMOVE_FROM_CHANNEL):
            await self._maybe_announce_membership_change(kind, event, payload)
            return True
        if kind in (
            EventKind.LEAVE_SPACE,
            EventKind.REMOVE_FROM_SPACE,
            EventKind.ACCEPT_SPACE_INVITE,
        ):
            await self._maybe_announce_space_membership_change(kind, event, payload)
            return True
        return False

    async def _handle_invite_cancellation_event(
        self,
        kind: str | None,
        payload: dict,
    ) -> None:
        if kind not in (
            EventKind.CANCEL_SPACE_INVITE,
            EventKind.CANCEL_CHANNEL_INVITE,
        ):
            return
        await self._on_invite_canceled(
            invitation_event_id=payload.get("invitation_event_id") or "",
            scope="space" if kind == EventKind.CANCEL_SPACE_INVITE else "channel",
        )

    async def _maybe_cache_channel_space(
        self,
        kind: str | None,
        event: dict,
        payload: dict,
    ) -> None:
        await maybe_cache_channel_space(
            kind=kind,
            event=event,
            payload=payload,
            slug=self.slug,
            store=self.store,
            channel_spaces=self._channel_space,
        )

    async def _evict_space_caches(self, space_id: str) -> None:
        await evict_space_caches(
            space_id=space_id,
            channel_spaces=self._channel_space,
            channel_names=self._channel_name_cache,
            space_names=self._space_name_cache,
            space_members=self._space_members,
            store=self.store,
            log=self._log,
        )

    async def _evict_channel_caches(self, channel_id: str) -> None:
        await evict_channel_caches(
            channel_id=channel_id,
            channel_spaces=self._channel_space,
            channel_names=self._channel_name_cache,
            store=self.store,
            log=self._log,
        )

    async def _dm_operator_membership_change(self, text: str) -> None:
        await dm_operator_membership_change(
            text=text,
            operator_slug=self.operator_slug,
            send_dm=self._send_dm,
            log=self._log,
        )

    async def _on_left_space(self, *, space_id: str, synthetic: bool) -> None:
        await on_left_space(
            space_id=space_id,
            synthetic=synthetic,
            gate_left_spaces=self._gate_left_spaces,
            still_member=self._still_member_of_space,
            resolve_space_name=self._resolve_space_name,
            evict_space=self._evict_space_caches,
            dm_membership_change=self._dm_operator_membership_change,
            log=self._log,
        )

    async def _still_member_of_space(self, space_id: str) -> bool | None:
        return await still_member_of_space(
            space_id=space_id,
            http=self.http,
            log=self._log,
        )

    async def _on_kicked_from_space(
        self,
        *,
        space_id: str,
        kicker_slug: str,
    ) -> None:
        await on_kicked_from_space(
            space_id=space_id,
            kicker_slug=kicker_slug,
            resolve_space_name=self._resolve_space_name,
            evict_space=self._evict_space_caches,
            fetch_display_name=self._fetch_display_name,
            dm_membership_change=self._dm_operator_membership_change,
        )

    async def _on_left_channel(self, *, channel_id: str) -> None:
        if channel_id:
            await self._evict_channel_caches(channel_id)

    async def _on_kicked_from_channel(
        self,
        *,
        channel_id: str,
        space_id: str,
        kicker_slug: str,
    ) -> None:
        await on_kicked_from_channel(
            channel_id=channel_id,
            space_id=space_id,
            kicker_slug=kicker_slug,
            resolve_channel_name=self._resolve_channel_name,
            resolve_space_name=self._resolve_space_name,
            evict_channel=self._evict_channel_caches,
            fetch_display_name=self._fetch_display_name,
            dm_membership_change=self._dm_operator_membership_change,
        )

    async def _on_invite_canceled(
        self,
        *,
        invitation_event_id: str,
        scope: str,
    ) -> None:
        await on_invite_canceled(
            invitation_event_id=invitation_event_id,
            scope=scope,
            pending_invites=self._pending_invite_dms,
            processed_invite_ids=self._processed_invite_ids,
            operator_slug=self.operator_slug,
            fetch_display_name=self._fetch_display_name,
            send_dm=self._send_dm,
            log=self._log,
        )

    async def _poll_pending_invites(self) -> None:
        await poll_pending_invites(
            http=self.http,
            processed_invite_ids=self._processed_invite_ids,
            process_invite=self._process_invite,
            log=self._log,
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
        await process_invite(
            kind=kind,
            invitation_event_id=invitation_event_id,
            inviter_slug=inviter_slug,
            space_id=space_id,
            channel_id=channel_id,
            space_name=space_name,
            channel_name=channel_name,
            auto_accept_space_invitations=self.auto_accept_space_invitations,
            processed_invite_ids=self._processed_invite_ids,
            inviter_is_operator=self._inviter_is_operator,
            accept_invite=self._accept_invite,
            report_auto_accepted_space_invite=(self._report_auto_accepted_space_invite),
            notify_operator_of_invite=self._notify_operator_of_invite,
            log=self._log,
        )

    async def _report_auto_accepted_space_invite(
        self,
        *,
        inviter_slug: str,
        space_id: str,
        space_name: str | None,
    ) -> None:
        await report_auto_accepted_space_invite(
            inviter_slug=inviter_slug,
            space_id=space_id,
            space_name=space_name,
            operator_slug=self.operator_slug,
            fetch_display_name=self._fetch_display_name,
            send_dm=self._send_dm,
            log=self._log,
        )

    async def _report_auto_accepted_channel_invite(
        self,
        *,
        inviter_slug: str,
        space_id: str,
        channel_id: str,
    ) -> None:
        await report_auto_accepted_channel_invite(
            inviter_slug=inviter_slug,
            space_id=space_id,
            channel_id=channel_id,
            operator_slug=self.operator_slug,
            resolve_space_name=self._resolve_space_name,
            resolve_channel_name=self._resolve_channel_name,
            fetch_display_name=self._fetch_display_name,
            send_dm=self._send_dm,
            log=self._log,
        )

    async def _inviter_is_operator(self, inviter_slug: str) -> bool:
        return await inviter_is_operator(
            inviter_slug=inviter_slug,
            operator_slug=self.operator_slug,
            operator_root_pubkey=self._operator_root_pubkey,
            fetch_inviter_root_pubkey=self._fetch_inviter_root_pubkey,
            log=self._log,
        )

    async def _fetch_inviter_root_pubkey(self, slug: str) -> bytes | None:
        return await fetch_inviter_root_pubkey(
            slug=slug,
            cache=self._inviter_root_cache,
            http=self.http,
        )

    async def _fetch_display_name(self, slug: str) -> str:
        """display_name via the unified profile cache. Empty string
        on miss/failure; caller falls back to ``@slug``. Thin wrapper
        kept for source compat with the dozen call sites that only
        care about the name."""
        name, _ = await self._fetch_user_profile(slug)
        return name

    async def _fetch_owner_slug(self, slug: str) -> str:
        """Return the cached or refreshed operator slug for an agent.

        ``/identities/profiles`` is subkey-signed, so the keyless transport
        cannot answer this at all and does not try. It returns the same empty
        answer the doomed request would have produced, but leaves the cache
        alone — a cached blank reads back as the attested fact "this sender
        has no owner" for the whole TTL, which misclassifies a co-owned
        sibling agent as a stranger the moment a keyless profile route exists.
        """
        from .ingress_policy import signed_http_available

        if not signed_http_available(self):
            return self._owner_slug_cache.get(slug, ("", 0.0))[0]
        return await fetch_owner_slug(
            slug=slug,
            owner_cache=self._owner_slug_cache,
            fetch_profile=self._fetch_user_profile,
        )

    def set_profile(self, slug: str, display_name: str, avatar_url: str) -> None:
        """Inject fresh values into the shared profile cache."""
        cache_profile(
            profile_cache=self._profile_cache,
            slug=slug,
            display_name=display_name,
            avatar_url=avatar_url,
        )

    async def _fetch_user_profile(
        self,
        slug: str,
        *,
        force_refresh: bool = False,
    ) -> tuple[str, str]:
        """Return a cached or refreshed ``(display_name, avatar_url)``."""
        return await fetch_user_profile(
            slug=slug,
            force_refresh=force_refresh,
            profile_cache=self._profile_cache,
            owner_cache=self._owner_slug_cache,
            http=self.http,
            log=self._log,
            fetch_avatar=self._fetch_and_cache_avatar,
        )

    async def _validate_incoming_parent_id(
        self,
        parent_id: Optional[str],
        expected_channel_id: Optional[str],
        expected_space_id: Optional[str],
        *,
        expected_envelope_kind: str = "",
        expected_dm_peer: str = "",
    ) -> Optional[str]:
        """Keep a direct parent only when it belongs to this target."""
        return await validate_incoming_parent_id(
            store=self.store,
            log=self._log,
            parent_id=parent_id,
            expected_channel_id=expected_channel_id,
            expected_space_id=expected_space_id,
            expected_envelope_kind=expected_envelope_kind,
            expected_dm_peer=expected_dm_peer,
            expected_self_slug=getattr(self, "slug", ""),
        )

    async def _validated_parent(
        self,
        parent_id: str,
        expected_channel_id: Optional[str],
        expected_space_id: Optional[str],
        *,
        log_label: str,
    ) -> Any:
        return await validated_parent(
            store=self.store,
            log=self._log,
            parent_id=parent_id,
            expected_channel_id=expected_channel_id,
            expected_space_id=expected_space_id,
            log_label=log_label,
        )

    async def _resolve_incoming_thread_root(
        self,
        parent_id: Optional[str],
        expected_channel_id: Optional[str],
        expected_space_id: Optional[str],
        *,
        expected_envelope_kind: str = "",
        expected_dm_peer: str = "",
    ) -> Optional[str]:
        """Resolve an inbound reply reference to its canonical thread root."""
        return await resolve_incoming_thread_root(
            store=self.store,
            log=self._log,
            parent_id=parent_id,
            expected_channel_id=expected_channel_id,
            expected_space_id=expected_space_id,
            expected_envelope_kind=expected_envelope_kind,
            expected_dm_peer=expected_dm_peer,
            expected_self_slug=getattr(self, "slug", ""),
        )

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
        self._warm_task = asyncio.ensure_future(
            asyncio.gather(
                self._warm_member_caches(),
                # Allow/block hydration rides the same tick so a restart
                # doesn't re-gate already-allowlisted senders.
                self._contacts.refresh(),
            )
        )
        await self._notify_connected_callbacks()

    async def _warm_member_caches(self) -> None:
        """Warm member, channel, and profile caches for visible spaces."""
        await warm_member_caches(
            http=self.http,
            log=self._log,
            space_name_cache=self._space_name_cache,
            profile_cache=self._profile_cache,
            get_members=self._get_space_members,
            warm_channels=self._warm_channels_for_space,
            fetch_profiles=self._bulk_fetch_profiles,
        )

    async def _warm_channels_for_space(self, space_id: str) -> None:
        await warm_channels_for_space(
            space_id=space_id,
            http=self.http,
            store=self.store,
            channel_spaces=self._channel_space,
            channel_names=self._channel_name_cache,
        )

    async def _bulk_fetch_profiles(self, slugs: list[str]) -> None:
        await bulk_fetch_profiles(
            slugs=slugs,
            http=self.http,
            profile_cache=self._profile_cache,
            fetch_avatar=self._fetch_and_cache_avatar,
        )

    async def _fetch_and_cache_avatar(self, avatar_url: str) -> None:
        await fetch_and_cache_avatar(
            avatar_url=avatar_url,
            http=self.http,
            log=self._log,
        )

    async def _get_space_members(self, space_id: str) -> dict[str, str]:
        return await get_space_members(
            space_id=space_id,
            http=self.http,
            cache=self._space_members,
        )

    async def _resolve_space_name(self, space_id: str) -> str:
        return await resolve_space_name(
            space_id=space_id,
            http=self.http,
            cache=self._space_name_cache,
        )

    async def _resolve_channel_name(
        self,
        space_id: str,
        channel_id: str,
    ) -> str:
        return await resolve_channel_name(
            space_id=space_id,
            channel_id=channel_id,
            http=self.http,
            cache=self._channel_name_cache,
        )

    async def _accept_invite(
        self,
        kind: str,
        invitation_event_id: str,
        space_id: str,
        channel_id: str,
    ) -> None:
        await accept_invite(
            kind=kind,
            invitation_event_id=invitation_event_id,
            space_id=space_id,
            channel_id=channel_id,
            slug=self.slug,
            device_id=self.device_id,
            keystore=self.keystore,
            http=self.http,
            store=self.store,
            find_general_channel=self._find_public_general_channel,
            enqueue_intro=self._enqueue_channel_intro_nudge,
            log=self._log,
        )

    async def _find_public_general_channel(self, space_id: str) -> str:
        return await find_public_general_channel(
            space_id=space_id,
            http=self.http,
            store=self.store,
            channel_names=self._channel_name_cache,
            log=self._log,
        )

    async def _enqueue_channel_intro_nudge(
        self,
        *,
        space_id: str,
        channel_id: str,
    ) -> None:
        runtime = getattr(self, "global_runtime", None)
        await enqueue_channel_intro_nudge(
            space_id=space_id,
            channel_id=channel_id,
            store=self.store,
            resolve_space_name=self._resolve_space_name,
            resolve_channel_name=self._resolve_channel_name,
            notify_runtime=runtime.notify if runtime is not None else None,
            log=self._log,
        )

    async def _maybe_announce_membership_change(
        self,
        kind: str,
        event: dict,
        payload: dict,
    ) -> None:
        await maybe_announce_membership_change(
            kind=kind,
            event=event,
            payload=payload,
            slug=self.slug,
            channel_spaces=self._channel_space,
            inviter_by_event_id=getattr(self, "_inviter_by_invitation_event_id", {}),
            processed_event_ids=getattr(self, "_processed_membership_event_ids", set()),
            enqueue_message=self._enqueue_membership_system_message,
            log=self._log,
        )

    async def _pick_space_channel(self, space_id: str) -> str:
        return await pick_space_channel(
            space_id=space_id,
            channel_spaces=self._channel_space,
            channel_names=self._channel_name_cache,
            resolve_channel_name=self._resolve_channel_name,
            http=self.http,
        )

    async def _maybe_announce_space_membership_change(
        self,
        kind: str,
        event: dict,
        payload: dict,
    ) -> None:
        await maybe_announce_space_membership_change(
            kind=kind,
            event=event,
            payload=payload,
            slug=self.slug,
            inviter_by_event_id=getattr(self, "_inviter_by_invitation_event_id", {}),
            processed_event_ids=getattr(self, "_processed_membership_event_ids", set()),
            pick_channel=self._pick_space_channel,
            enqueue_message=self._enqueue_membership_system_message,
            log=self._log,
        )

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
        runtime = getattr(self, "global_runtime", None)
        await enqueue_membership_system_message(
            channel_id=channel_id,
            actor_slug=actor_slug,
            action=action,
            kicker_slug=kicker_slug,
            inviter_slug=inviter_slug,
            event_id=event_id,
            channel_spaces=self._channel_space,
            resolve_space_name=self._resolve_space_name,
            resolve_channel_name=self._resolve_channel_name,
            fetch_display_name=self._fetch_display_name,
            store=self.store,
            notify_runtime=runtime.notify if runtime is not None else None,
            log=self._log,
        )

    def _resolve_invite_targets(
        self,
        payload_thread_root_id: str | None,
        text: str,
    ) -> tuple[list[str], bool]:
        return resolve_invite_targets(
            payload_thread_root_id=payload_thread_root_id,
            text=text,
            pending_invites=self._pending_invite_dms,
        )

    async def _apply_invite_replies(
        self,
        roots: list[str],
        text: str,
    ) -> list[str]:
        return await apply_invite_replies(
            roots=roots,
            text=text,
            pending_invites=self._pending_invite_dms,
            handle_reply=self._maybe_handle_invite_reply,
        )

    async def _send_invite_bulk_summary(
        self,
        labels: list[str],
        text: str,
        root_id: str,
    ) -> None:
        await send_invite_bulk_summary(
            labels=labels,
            text=text,
            root_id=root_id,
            operator_slug=self.operator_slug,
            send_dm=self._send_dm,
            log=self._log,
        )

    @staticmethod
    def _invite_target_label(meta: dict) -> str:
        return invite_target_label(meta)

    # ── cli-local command permission (operator-gated) ─────────────────

    async def request_command_permission(
        self,
        *,
        tool_name: str,
        summary: str,
        timeout_s: int,
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
                    f"Timed out after {timeout_s}s — I did NOT run `{tool_name}`.",
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
        self,
        *,
        thread_root_id: str,
        text: str,
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
        self,
        *,
        kind: str,
        space_id: str,
        channel_id: str,
        reason: str,
    ) -> str:
        """DM the operator to approve an agent-requested leave and
        register it as pending. ``kind`` is ``leave_space`` /
        ``leave_channel``. Returns a short status line for the calling
        MCP tool to relay to the agent. The actual leave is signed only
        after the operator replies ``y`` in the DM thread (the gate in
        ``handle_envelope`` → ``_maybe_handle_leave_reply``)."""
        if not self.operator_slug:
            return "No operator is configured, so I can't request approval to leave."
        space_label = await self._resolve_space_name(space_id)
        channel_label = ""
        if kind == EventKind.LEAVE_CHANNEL:
            channel_label = await self._resolve_channel_name(
                space_id=space_id,
                channel_id=channel_id,
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
        self,
        *,
        thread_root_id: str,
        text: str,
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
                    kind=kind,
                    space_id=space_id,
                    channel_id=channel_id,
                )
                # Suppress the WS echo's generic membership DM (space only);
                # the in-thread confirm below is the authoritative report.
                if kind == EventKind.LEAVE_SPACE:
                    self._gate_left_spaces.add(space_id)
                confirm = f"Left {target}. ✓"
                self._log.info(
                    "operator-approved leave of %s (space=%s channel=%s)",
                    kind,
                    space_id,
                    channel_id,
                )
            except Exception as exc:
                self._log.exception(
                    "operator-approved leave of %s failed (space=%s channel=%s)",
                    kind,
                    space_id,
                    channel_id,
                )
                confirm = f"{format_leave_error(exc)} ({target})"
        else:
            confirm = f"Understood — I'll stay in {target}."

        self._pending_leave_dms.pop(thread_root_id, None)
        try:
            await self._send_dm(
                self.operator_slug,
                confirm,
                root_id=thread_root_id,
            )
        except Exception:
            self._log.exception(
                "failed to confirm leave-reply outcome to operator",
            )
        return True

    # ─── auto_accept_dm gate ──────────────────────────────────────

    async def _is_foreign_dm_sender(self, sender_slug: str) -> bool:
        # Operator, self, and co-owned agents are trusted; everyone else
        # is foreign. Owner lookup shares the render-time TTL cache.
        if not sender_slug:
            return False
        if sender_slug == self.operator_slug:
            return False
        if sender_slug == self.slug:
            return False
        owner = await self._fetch_owner_slug(sender_slug)
        if owner and owner == self.operator_slug:
            return False
        return True

    async def _ensure_trusted_contact(self, slug: str) -> None:
        """Operator / co-owned senders become contacts on first DM."""
        if not slug or slug == self.slug:
            return
        if await self._contacts.is_allowed(slug):
            return
        try:
            await self.http.post("/allowlists", {"slugs": [slug]})
        except Exception as exc:
            self._log.warning(
                "dm_gate: contact add for trusted %s failed: %s",
                slug,
                exc,
            )
            return
        self._contacts.note_allowed(slug)

    async def _shares_space_with(self, sender_slug: str) -> bool:
        """Sender shares a space with the agent. Fails closed — an
        unreachable membership API falls through to the approval gate."""
        try:
            data = await self.http.get("/spaces")
        except Exception as exc:
            self._log.warning(
                "dm_gate: /spaces fetch for shared-space check failed: %s",
                exc,
            )
            return False
        for entry in data.get("spaces") or []:
            space_id = entry.get("space_id") or ""
            if not space_id:
                continue
            members = await self._get_space_members(space_id)
            if sender_slug in members:
                return True
        return False

    _DM_NOTICE_INTERVAL_MS = 72 * 3600 * 1000

    async def _maybe_send_dm_notice(self, sender_slug: str) -> None:
        """Operator FYI for every non-trusted sender (contacts included):
        first DM immediately, then one per 72h; persisted."""
        if not self.operator_slug:
            return
        try:
            last = await self.store.get_dm_notice(sender_slug)
        except Exception:
            last = None
        now_ms = int(time.time() * 1000)
        if last is not None and now_ms - last < self._DM_NOTICE_INTERVAL_MS:
            return
        display = await self._fetch_display_name(sender_slug)
        label = f"**{display}** ({sender_slug})" if display else f"@{sender_slug}"
        try:
            await self._send_dm(
                self.operator_slug,
                f"FYI, {label} is sending direct messages to me.",
                root_id="",
            )
        except Exception as exc:
            self._log.warning(
                "dm_notice: failed to notify operator about %s: %s",
                sender_slug,
                exc,
            )
            return
        try:
            await self.store.set_dm_notice(sender_slug, now_ms)
        except Exception as exc:
            self._log.warning("dm_notice: failed to persist ts: %s", exc)

    async def _maybe_allowlist_outbound_dm(self, recipient_slug: str) -> None:
        """Agent DM'd a foreign peer first → allowlist them; best-effort."""
        if not recipient_slug:
            return
        # Never allowlist a sender we're currently gating — the ack DM
        # echoes back here and would pre-empt the operator's y/n.
        if any(
            m.get("sender_slug") == recipient_slug
            for m in self._pending_dm_approvals.values()
        ):
            return
        # Replying is not consent — only a genuinely agent-initiated
        # first DM allowlists. A stored inbound DM means they wrote first.
        try:
            if await self.store.has_dm_from(recipient_slug):
                return
        except Exception:
            return
        # Trusted short-circuit before is_allowed can hit the network
        # (the daemon DMs the operator constantly).
        if not await self._is_foreign_dm_sender(recipient_slug):
            return
        if await self._contacts.is_allowed(recipient_slug):
            return
        try:
            await self.http.post("/allowlists", {"slugs": [recipient_slug]})
        except Exception as exc:
            self._log.warning(
                "dm_gate: outbound allowlist for %s failed: %s",
                recipient_slug,
                exc,
            )
            return
        self._contacts.note_allowed(recipient_slug)
        if not self.operator_slug:
            return
        display = await self._fetch_display_name(recipient_slug)
        label = f"**{display}**(@{recipient_slug})" if display else f"@{recipient_slug}"
        try:
            await self._send_dm(
                self.operator_slug,
                f"Allowlisted {label} — I messaged them first, so their "
                "replies won't need approval.",
                root_id="",
            )
        except Exception:
            self._log.exception(
                "dm_gate: failed to notify operator of outbound allowlist",
            )

    async def _maybe_gate_foreign_dm(
        self, *, sender_slug: str, text: str, trigger_encrypted: bool = False
    ) -> bool:
        from . import dm_gate

        return await dm_gate.maybe_gate_foreign_dm(
            self,
            sender_slug=sender_slug,
            text=text,
            trigger_encrypted=trigger_encrypted,
        )

    async def _maybe_handle_dm_approval_reply(
        self, *, thread_root_id: str, text: str
    ) -> bool:
        from . import dm_gate

        return await dm_gate.maybe_handle_dm_approval_reply(
            self, thread_root_id=thread_root_id, text=text
        )

    async def _drain_pending_from_sender(self, sender_slug: str) -> None:
        from . import dm_gate

        await dm_gate.drain_pending_from_sender(self, sender_slug)

    async def _drop_pending_from_sender(self, sender_slug: str) -> None:
        from . import dm_gate

        await dm_gate.drop_pending_from_sender(self, sender_slug)

    @staticmethod
    def _leave_target_label(meta: dict) -> str:
        """Human label for a leave's destination (space or channel)."""
        space_id = meta.get("space_id") or ""
        space_label = (
            f"**{meta['space_name']}**" if meta.get("space_name") else space_id
        )
        if meta.get("kind") == "leave_channel":
            channel_id = meta.get("channel_id") or ""
            channel_label = (
                f"**{meta['channel_name']}**"
                if meta.get("channel_name")
                else channel_id
            )
            return f"channel {channel_label} in space {space_label}"
        return f"space {space_label}"

    async def _channel_is_public(
        self,
        space_id: str,
        channel_id: str,
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
        self,
        *,
        kind: str,
        space_id: str,
        channel_id: str,
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
        self,
        *,
        thread_root_id: str,
        text: str,
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
            if inviter_display
            else f"@{inviter_slug}"
        )

        if verdict == "accept":
            try:
                await self._accept_invite(
                    kind,
                    invitation_event_id,
                    space_id,
                    channel_id,
                )
                confirm = f"Accepted invite to {target}. ✓"
                self._log.info(
                    "operator-confirmed accept of %s (event_id=%s)",
                    kind,
                    invitation_event_id,
                )
            except Exception as exc:
                self._log.exception(
                    "operator-confirmed accept of %s (event_id=%s) failed",
                    kind,
                    invitation_event_id,
                )
                confirm = f"{format_invite_error(exc, 'accept')} ({target})"
        else:  # reject
            try:
                await self._reject_invite(
                    kind,
                    invitation_event_id,
                    space_id,
                    channel_id,
                )
                confirm = f"Rejected invite from {inviter_label} to {target}."
                self._log.info(
                    "operator-confirmed reject of %s (event_id=%s)",
                    kind,
                    invitation_event_id,
                )
            except Exception as exc:
                self._log.exception(
                    "operator-confirmed reject of %s (event_id=%s) failed",
                    kind,
                    invitation_event_id,
                )
                confirm = f"{format_invite_error(exc, 'reject')} ({target})"

        # Drop from pending so a duplicate ``y`` later in the same
        # thread doesn't re-attempt (server would reject it anyway).
        self._pending_invite_dms.pop(thread_root_id, None)
        try:
            await self._send_dm(
                self.operator_slug,
                confirm,
                root_id=thread_root_id,
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
                kind,
                inviter_slug,
                invitation_event_id,
            )
            return
        # ``**...**`` renders bold in the web client. Apply only to
        # names; bare IDs stay un-styled to avoid noise.
        inviter_display = await self._fetch_display_name(inviter_slug)
        inviter_label = (
            f"**{inviter_display}**(@{inviter_slug})"
            if inviter_display
            else f"@{inviter_slug}"
        )
        space_label = f"**{space_name}**({space_id})" if space_name else space_id
        if kind == EventKind.INVITE_TO_SPACE:
            target = f"space {space_label}"
        else:
            channel_label = (
                f"**{channel_name}**({channel_id})" if channel_name else channel_id
            )
            target = f"channel {channel_label} in space {space_label}"
        # A direct reply applies the same decision for all your pending invites.
        text = format_permission_prompt(
            f"{inviter_label} invited me to {target}. "
            f"They aren't my registered operator — accept?",
            reply_note=(
                "use a direct (non-threaded) `y`/`n` for all your "
                "pending invites at once"
            ),
        )
        try:
            envelope = await self._send_dm(self.operator_slug, text, root_id="")
        except Exception:
            self._log.exception(
                "failed to DM operator about invite from %s (event_id=%s)",
                inviter_slug,
                invitation_event_id,
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
        self,
        *,
        envelope_id: str,
        metas_raw: list,
    ) -> list[str]:
        """Save native encrypted attachments through the attachment service."""
        return await save_inbound_attachments(
            workspace=self.workspace,
            envelope_id=envelope_id,
            metas_raw=metas_raw,
            image_edge_px=self._image_edge_px,
            http=self.http,
            log=self._log,
            fetch_blob=_fetch_blob_with_retry,
            strip_wrapper=_strip_multipart_wrapper,
            scale_image=_downscale_oversized_image,
        )

    async def _save_inbound_bridge_attachments(
        self,
        *,
        envelope_id: str,
        refs: list,
    ) -> list[str]:
        return await save_inbound_bridge_attachments(
            self,
            envelope_id=envelope_id,
            refs=refs,
        )

    async def _send_dm(
        self,
        recipient_slug: str,
        text: str,
        root_id: str,
        require_encryption: bool = False,
    ) -> dict[str, Any] | None:
        return await send_direct_message(
            slug=self.slug,
            recipient_slug=recipient_slug,
            text=text,
            root_id=root_id,
            keystore=self.keystore,
            store=self.store,
            http=self.http,
            fetch_devices=self._fetch_device_keys,
            log=self._log,
            require_encryption=require_encryption,
        )

    async def _fetch_device_keys(
        self,
        slugs: list[str],
    ) -> list[RecipientDevice]:
        return await fetch_device_keys(http=self.http, slugs=slugs)

    async def send_fallback_message(
        self,
        channel_id: str,
        text: str,
        root_id: str = "",
    ) -> dict[str, Any] | None:
        """Send channel output through the coordinator or reply to the DM peer."""
        if channel_id:
            delegate = getattr(self, "send_delegate", None)
            if delegate is None:
                self._log.warning(
                    "send_fallback_message: persistent coordinator unavailable; "
                    "channel output failed closed"
                )
                from .send_coordinator import failed_result

                return failed_result(
                    "persistent send coordinator is unavailable",
                    kind="coordinator_unavailable",
                )
            from .send_coordinator import SemanticSendRequest

            result = await delegate.send(
                SemanticSendRequest(
                    destination=channel_id,
                    text=text,
                    root_id=root_id,
                )
            )
            if not isinstance(result, dict) or result.get("state") not in {
                "sent",
                "held",
            }:
                self._log.warning(
                    "send_fallback_message: coordinated channel output failed"
                )
            return result

        if self._bridge is not None:
            recipient = self._last_dm_sender
            if not recipient:
                self._log.warning(
                    "send_fallback_message[bridge] called with empty channel_id "
                    "but no DM context - dropping reply"
                )
                return None
            await send_bridge_fallback_dm(
                bridge=self._bridge,
                recipient_slug=recipient,
                text=text,
                root_id=root_id,
                log=self._log,
            )
            return None

        recipient = self._legacy_dm_peer
        if not recipient:
            self._log.warning(
                "send_fallback_message called with empty channel_id but no DM "
                "context - dropping reply"
            )
            return None
        await send_native_fallback_dm(
            slug=self.slug,
            recipient_slug=recipient,
            text=text,
            root_id=root_id,
            keystore=self.keystore,
            store=self.store,
            http=self.http,
            fetch_devices=self._fetch_device_keys,
            log=self._log,
        )
        return None

    async def send_typing(self, channel_id: str, parent_id: str) -> None:
        pass

    async def stop(self) -> None:
        if self._ws:
            self._ws.stop()
        bridge = getattr(self, "_bridge", None)
        close_bridge = getattr(bridge, "close", None)
        if callable(close_bridge):
            try:
                await close_bridge()
            except Exception as exc:  # noqa: BLE001
                self._log.warning("bridge close failed category=%s", type(exc).__name__)
        for task in tuple(getattr(self, "_ack_tasks", ())):
            task.cancel()
        for task in tuple(getattr(self, "_ack_tasks", ())):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.store.close()
        close_http = getattr(self.http, "close", None)
        if callable(close_http):
            await close_http()
