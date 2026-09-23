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
from typing import Any

from ...crypto.http_session import create_remote_http_session
from ..control import machine_auth
from ..control.store import MachineControlIdentity, load_or_create_machine
from ..host_assets import _ensure_private_directory
from ..state import home_dir
from .claim import ClaimFailed, claim_connection
from .store import SkeletonConnectionStore

logger = logging.getLogger(__name__)

# PROVISIONAL: path and response field names are awaiting Bob's server
# contract. Everything above this line is contract-independent; only
# ``_CLAIM_PATH`` and ``_read_claimed`` change when it lands.
_CLAIM_PATH = "/v2/machines/connector/claims"


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
    path = f"{_CLAIM_PATH}/{request_ref}"
    headers = machine_auth.signed_headers(machine, "POST", path)
    try:
        async with create_remote_http_session(base) as session:
            async with session.post(f"{base}{path}", headers=headers) as response:
                if response.status == 404:
                    raise ClaimFailed("server has no such claim")
                if response.status == 410:
                    raise ClaimFailed("the prepared credential is gone; start again")
                if response.status != 200:
                    raise ClaimFailed(
                        f"server refused the claim ({response.status})"
                    )
                return _read_claimed(await response.json())
    except ClaimFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - transport and decoding both end the claim
        # Reported, not raised: the page has to stop waiting either way, and
        # whether the server already handed the credential out is unknown here.
        raise ClaimFailed(f"claim call did not complete: {type(exc).__name__}") from exc


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
