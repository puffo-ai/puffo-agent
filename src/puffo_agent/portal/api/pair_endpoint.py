"""Identity verification and persistence for ``POST /v1/pair``."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from ...crypto.encoding import base64url_encode
from ...crypto.http_auth import VerifyError, is_timestamp_fresh, verify_request
from .certs import (
    CertError,
    verify_device_cert,
    verify_identity_cert,
    verify_slug_binding,
)
from .pairing import Pairing, load_pairing, now_ms, save_pairing

logger = logging.getLogger(__name__)


class PairError(Exception):
    def __init__(self, reason: str, **fields: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fields = fields


@dataclass(frozen=True)
class VerifiedPair:
    identity_cert: dict
    device_cert: dict
    root_public_key: bytes
    device_signing_public_key: bytes
    slug: str
    device_id: str


def _reject(error: PairError) -> web.Response:
    extra = " ".join(f"{key}={value!r}" for key, value in error.fields.items())
    logger.warning(
        "bridge: pair rejected: %s%s",
        error.reason,
        f" {extra}" if extra else "",
    )
    return web.json_response({"error": error.reason}, status=400)


def _certificate_payload(payload: Any) -> tuple[dict, dict, dict]:
    if not isinstance(payload, dict):
        raise PairError("body must be JSON")
    identity_cert = payload.get("identity_cert")
    device_cert = payload.get("device_cert")
    slug_binding = payload.get("slug_binding")
    if not all(
        isinstance(value, dict) for value in (identity_cert, device_cert, slug_binding)
    ):
        raise PairError(
            "identity_cert, device_cert, and slug_binding all required",
            identity_cert_type=type(identity_cert).__name__,
            device_cert_type=type(device_cert).__name__,
            slug_binding_type=type(slug_binding).__name__,
        )
    return identity_cert, device_cert, slug_binding


def _verify_certificates(payload: Any) -> VerifiedPair:
    identity_cert, device_cert, slug_binding = _certificate_payload(payload)
    try:
        root_public_key = verify_identity_cert(identity_cert)
    except CertError as exc:
        raise PairError(
            f"identity_cert: {exc}",
            cert_keys=sorted(identity_cert.keys()),
        ) from exc
    try:
        device_signing_key = verify_device_cert(device_cert, root_public_key)
    except CertError as exc:
        raise PairError(
            f"device_cert: {exc}",
            cert_keys=sorted(device_cert.keys()),
        ) from exc
    try:
        slug = verify_slug_binding(slug_binding, root_public_key)
    except CertError as exc:
        raise PairError(
            f"slug_binding: {exc}",
            binding_keys=sorted(slug_binding.keys()),
        ) from exc
    if identity_cert.get("identity_type") != "human":
        raise PairError(
            "identity_type must be 'human'",
            got=identity_cert.get("identity_type"),
        )
    return VerifiedPair(
        identity_cert=identity_cert,
        device_cert=device_cert,
        root_public_key=root_public_key,
        device_signing_public_key=device_signing_key,
        slug=slug,
        device_id=device_cert.get("device_id"),
    )


def _verify_headers(request: web.Request, body: bytes, pair: VerifiedPair) -> None:
    slug = request.headers.get("x-puffo-slug", "")
    signer_id = request.headers.get("x-puffo-signer-id", "")
    timestamp = request.headers.get("x-puffo-timestamp", "")
    nonce = request.headers.get("x-puffo-nonce", "")
    signature = request.headers.get("x-puffo-signature", "")
    if pair.slug != slug:
        raise PairError(
            "slug_binding.slug does not match x-puffo-slug",
            cert_slug=pair.slug,
            hdr_slug=slug,
        )
    if pair.device_id != signer_id:
        raise PairError(
            "device_cert.device_id does not match x-puffo-signer-id",
            cert_device_id=pair.device_id,
            hdr_signer_id=signer_id,
        )
    if not is_timestamp_fresh(timestamp):
        raise PairError("stale x-puffo-timestamp", ts=timestamp)
    if not nonce or not signature:
        raise PairError("missing x-puffo-nonce or x-puffo-signature")
    try:
        verify_request(
            public_key=pair.device_signing_public_key,
            method=request.method,
            path=request.path_qs,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
            signature_b64=signature,
        )
    except VerifyError as exc:
        raise PairError(
            f"signature: {exc}",
            method=request.method,
            path=request.path_qs,
        ) from exc


def _persist_pairing(verified: VerifiedPair) -> Pairing:
    existing = load_pairing()
    pairing = Pairing(
        slug=verified.slug,
        device_id=verified.device_id,
        root_public_key=base64url_encode(verified.root_public_key),
        device_signing_public_key=base64url_encode(verified.device_signing_public_key),
        identity_cert=verified.identity_cert,
        device_cert=verified.device_cert,
        paired_at=now_ms(),
    )
    save_pairing(pairing)
    replaced = existing is not None and (
        existing.slug != pairing.slug or existing.device_id != pairing.device_id
    )
    if replaced:
        logger.info(
            "bridge: replaced pairing prev_slug=%s prev_device_id=%s -> "
            "slug=%s device_id=%s",
            existing.slug,
            existing.device_id,
            pairing.slug,
            pairing.device_id,
        )
    else:
        logger.info(
            "bridge: paired with slug=%s device_id=%s",
            pairing.slug,
            pairing.device_id,
        )
    return pairing


async def pair(request: web.Request) -> web.Response:
    """Verify the signed identity bundle and pair this daemon."""
    body = await request.read()
    try:
        payload = await request.json()
    except Exception:
        return _reject(PairError("body must be JSON"))
    try:
        verified = _verify_certificates(payload)
        _verify_headers(request, body, verified)
    except PairError as exc:
        return _reject(exc)
    pairing = _persist_pairing(verified)
    return web.json_response(
        {
            "paired_slug": pairing.slug,
            "paired_device_id": pairing.device_id,
            "paired_at": pairing.paired_at,
        }
    )
