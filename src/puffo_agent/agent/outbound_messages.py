"""Outbound DM construction and transport helpers.

Channel output is coordinated by ``send_coordinator`` before it reaches this
module. These helpers own the remaining bridge and native DM wire details so
the public message client can stay a small compatibility facade.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..crypto.encoding import base64url_decode
from ..crypto.http_client import HttpError
from ..crypto.keystore import decode_secret
from ..crypto.message import (
    EncryptInput,
    RecipientDevice,
    encrypt_message,
)
from ..crypto.primitives import Ed25519KeyPair
from ._visibility import resolve_visibility

Log = logging.Logger | logging.LoggerAdapter
DeviceFetcher = Callable[[list[str]], Awaitable[list[RecipientDevice]]]


async def fetch_device_keys(*, http: Any, slugs: list[str]) -> list[RecipientDevice]:
    """Collect every registered encryption device for ``slugs``."""
    if not slugs:
        return []

    devices: list[RecipientDevice] = []
    seen_ids: set[str] = set()
    since = 0
    while True:
        data = await http.get(f"/certs/sync?slugs={','.join(slugs)}&since={since}")
        for entry in data.get("entries", []):
            if entry.get("kind") == "device_cert":
                device = _recipient_device(entry.get("cert", {}), seen_ids)
                if device is not None:
                    devices.append(device)
                    seen_ids.add(device.device_id)
            since = entry.get("seq", since)
        if not data.get("has_more"):
            return devices


def _merge_devices(
    *groups: list[RecipientDevice],
) -> list[RecipientDevice]:
    """Concatenate device lists, dropping duplicate device ids."""
    merged: dict[str, RecipientDevice] = {}
    for group in groups:
        for device in group:
            merged.setdefault(device.device_id, device)
    return list(merged.values())


def _recipient_device(
    cert: Mapping[str, Any],
    seen_ids: set[str],
) -> RecipientDevice | None:
    device_id = str(cert.get("device_id") or "")
    keys = cert.get("keys") or {}
    encryption = keys.get("encryption") or {}
    public_key = encryption.get("public_key") or cert.get("kem_public_key", "")
    if not device_id or not public_key or device_id in seen_ids:
        return None
    try:
        return RecipientDevice(
            device_id=device_id,
            kem_public_key=base64url_decode(public_key),
        )
    except Exception:  # noqa: BLE001 - malformed registry rows are skipped
        return None


async def send_direct_message(
    *,
    slug: str,
    recipient_slug: str,
    text: str,
    root_id: str,
    keystore: Any,
    http: Any,
    fetch_devices: DeviceFetcher,
    log: Log,
) -> dict[str, Any] | None:
    """Send one always-visible operator-facing DM."""
    if recipient_slug == slug:
        return None
    try:
        envelope, path = await _build_native_dm(
            slug=slug,
            recipient_slug=recipient_slug,
            text=text,
            root_id=root_id,
            is_visible_to_human=True,
            keystore=keystore,
            fetch_devices=fetch_devices,
        )
        await http.post(path, envelope)
    except HttpError:
        log.exception("DM send to %s failed", recipient_slug)
        raise
    return envelope


async def send_native_fallback_dm(
    *,
    slug: str,
    recipient_slug: str,
    text: str,
    root_id: str,
    keystore: Any,
    http: Any,
    fetch_devices: DeviceFetcher,
    log: Log,
) -> None:
    """Send the native fallback reply to the current implicit DM peer."""
    visible, _ = await resolve_visibility(
        "default",
        f"@{recipient_slug}",
        text,
        root_id or "",
        http,
    )
    envelope, path = await _build_native_dm(
        slug=slug,
        recipient_slug=recipient_slug,
        text=text,
        root_id=root_id,
        is_visible_to_human=visible,
        keystore=keystore,
        fetch_devices=fetch_devices,
    )
    try:
        response = await http.post(path, envelope)
    except Exception:  # noqa: BLE001 - retain transport diagnostics before bubbling
        log.exception("send_fallback_message: POST %s failed", path)
        raise
    log.info(
        "send_fallback_message sent: envelope_id=%s queued=%s",
        envelope.get("envelope_id"),
        (response or {}).get("devices_queued"),
    )


async def send_bridge_fallback_dm(
    *,
    bridge: Any,
    recipient_slug: str,
    text: str,
    root_id: str,
    log: Log,
) -> None:
    """Send a keyless plaintext DM through the cloud bridge."""
    acknowledgement = await bridge.send_send(
        plaintext=text,
        recipient_slug=recipient_slug,
        thread_root_id=root_id or None,
        reply_to_id=root_id or None,
    )
    log.info(
        "send_fallback_message[bridge] sent: kind=dm recipient=%s envelope_id=%s",
        recipient_slug,
        (acknowledgement or {}).get("envelope_id"),
    )


async def _build_native_dm(
    *,
    slug: str,
    recipient_slug: str,
    text: str,
    root_id: str,
    is_visible_to_human: bool,
    keystore: Any,
    fetch_devices: DeviceFetcher,
) -> tuple[dict[str, Any], str]:
    session = keystore.load_session(slug)
    signing_key = Ed25519KeyPair.from_secret_bytes(
        decode_secret(session.subkey_secret_key)
    )
    # The recipient is fetched alone: a combined [sender, recipient] fetch
    # cannot tell "recipient unreachable" from "reachable" once the sender's
    # own devices make the list non-empty.
    recipient_devices = await fetch_devices([recipient_slug])
    if not recipient_devices:
        raise RuntimeError(
            f"recipient @{recipient_slug} has no encryption devices - "
            f"DM not sent"
        )
    devices = _merge_devices(recipient_devices, await fetch_devices([slug]))

    message = EncryptInput(
        envelope_kind="dm",
        sender_slug=slug,
        sender_subkey_id=session.subkey_id,
        is_visible_to_human=is_visible_to_human,
        recipient_slug=recipient_slug,
        thread_root_id=root_id or None,
        content_type="text/plain",
        content=text,
        recipients=devices,
    )
    return encrypt_message(message, signing_key), "/messages"
