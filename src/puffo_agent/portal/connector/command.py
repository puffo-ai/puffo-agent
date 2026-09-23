"""The ``connector.claim`` computer command: wiring, not policy.

Machine-level, like ``refresh_usage`` — a claim is about this computer's
connection, not about one agent. The command carries only the request
reference; which computer is claiming is NOT taken from it. That travels in the
machine signature the server verifies (``machine_auth.signed_headers``), so the
server reads the claimant from a verified header rather than from a field the
caller filled in.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote
from typing import Any

from ...crypto.http_session import create_remote_http_session
from ..control import machine_auth
from ..control.store import MachineControlIdentity, load_or_create_machine
from ..host_assets import _ensure_private_directory
from ..state import home_dir
from .claim import ClaimFailed, claim_connection
from .store import SkeletonConnectionStore

logger = logging.getLogger(__name__)

# Server contract: claim interface v1 (Bob 219652) §2.
#
# The request carries NO body. `signed_headers` signs
# ``POST\n{path}\n{timestamp}\n{nonce}\n`` with zero body bytes appended, and
# the POST is sent without one — an empty body and a literal ``{}`` are
# different bytes and would verify differently, so this matches the eight
# existing machine-signed calls rather than inventing a third convention.
_CLAIM_ROOT = "/v2/machines/me/oauth-requests"


def connection_path() -> Path:
    directory = home_dir() / "connector"
    _ensure_private_directory(directory)
    return directory / "connection.json"


async def run_claim_command(params: dict, server_url: str) -> dict:
    """Entry point for the dispatcher. Always returns a command result."""
    request_ref = str(params.get("request_ref") or "").strip()
    if not request_ref:
        # Nothing to correlate a failure to, so this cannot be logged usefully
        # against a request; it is a malformed command, not a failed claim.
        logger.error("connector: claim command carried no request_ref")
        return {"ok": False, "connected": False, "stage": "command", "reason": "no request_ref"}

    machine = load_or_create_machine()
    base = server_url.rstrip("/")

    async def fetch(reference: str) -> tuple[str, Any]:
        return await _fetch_from_server(base, machine, reference)

    return await claim_connection(
        request_ref, fetch=fetch, store=SkeletonConnectionStore(connection_path())
    )


async def _fetch_from_server(
    base: str, machine: MachineControlIdentity, request_ref: str
) -> tuple[str, Any]:
    """Ask the server to hand over the credential prepared for this request."""
    path = f"{_CLAIM_ROOT}/{quote(request_ref, safe='')}/claim"
    headers = machine_auth.signed_headers(machine, "POST", path)
    try:
        async with create_remote_http_session(base) as session:
            async with session.post(f"{base}{path}", headers=headers) as response:
                if response.status != 200:
                    raise _refusal(response.status, await _reason_of(response))
                return _read_claimed(await response.json())
    except ClaimFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - transport and decoding both end the claim
        # Reported, not raised: the page has to stop waiting either way, and
        # whether the server already handed the credential out is unknown here.
        raise ClaimFailed(f"claim call did not complete: {type(exc).__name__}") from exc


async def _reason_of(response: Any) -> str | None:
    """The server's structured ``reason``, when the body carries one."""
    try:
        body = await response.json()
    except Exception:  # noqa: BLE001 - an unparseable error body is just no reason
        return None
    return body.get("reason") if isinstance(body, dict) else None


# Interface v1 §2. 403 is deliberately the same answer for "no such request" and
# "not this machine" — the server refuses to say which, so neither does this.
_REFUSALS = {
    "not_claimable": "this computer cannot claim that request",
    "not_ready": "the credential is not ready yet",
    "already_claimed": "that credential was already claimed",
    "expired": "the prepared credential expired; start again",
}


def _refusal(status: int, reason: str | None) -> ClaimFailed:
    described = _REFUSALS.get(reason or "")
    if described is None:
        return ClaimFailed(f"server refused the claim ({status})", code=reason)
    return ClaimFailed(described, code=reason)


def _read_claimed(payload: Any) -> tuple[str, Any]:
    """Pull provider and credential out of the claim response.

    The credential is passed through untouched. The daemon is not told the
    credential's shape by the design (v0.4 §4), so parsing any field of it here
    would invent a coupling to Google that the generic layer does not have.
    """
    if not isinstance(payload, dict):
        raise ClaimFailed("claim response was not an object")
    provider = payload.get("provider")
    if not isinstance(provider, str) or not provider:
        raise ClaimFailed("claim response named no provider")
    if "credential" not in payload:
        raise ClaimFailed("claim response carried no credential")
    return provider, payload["credential"]
