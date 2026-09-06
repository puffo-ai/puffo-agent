"""Machine-level control-plane ops for the Gmail connector.

Ordering constraint: the native confirmation is the gate — until it
returns True, nothing is spawned and no OAuth endpoint is contacted
(no confirm → zero S4 calls). The executor entrypoint and its config
(data_root / bundle pin) come only from local daemon config, never
from command params: a control-plane command that could name its own
binary or point it at its own bundle would be remote code execution.
"""

from __future__ import annotations

import logging

from ..state import DaemonConfig
from .executor import ExecutorRefused, run_gmail_executor
from .native_confirm import CONNECT_PROMPT, request_native_confirm
from .status_store import GmailConnectStatus, load_status, store_status


logger = logging.getLogger(__name__)


def _config() -> DaemonConfig:
    return DaemonConfig.load()


def _connect_request(gc) -> dict:
    """Non-secret invoke config per EXECUTOR_INVOKE_SCHEMA v1 §2."""
    request = {
        "data_root": gc.data_root,
        "expected_sha256": gc.client_bundle_sha256,
        "timeout": gc.flow_timeout_seconds,
    }
    if gc.client_bundle:
        request["client_bundle"] = gc.client_bundle
    return request


async def gmail_connect_initiate(params: dict) -> dict:
    gc = _config().gmail_connect
    if not gc.enabled or not gc.executor_path:
        return {"ok": False, "error": "gmail_connect not configured"}
    if not gc.data_root or not gc.client_bundle_sha256:
        return {"ok": False, "error": "gmail_connect missing data_root/bundle pin"}
    if load_status().state == "pending":
        return {"ok": False, "error": "already pending"}
    confirmed = await request_native_confirm(
        CONNECT_PROMPT, timeout_s=gc.confirm_timeout_seconds
    )
    if not confirmed:
        return {"ok": False, "error": "user_declined"}
    store_status(GmailConnectStatus(state="pending"))
    try:
        outcome = await run_gmail_executor(
            gc.executor_path,
            _connect_request(gc),
            flow_timeout_s=gc.flow_timeout_seconds,
        )
    except ExecutorRefused as exc:
        store_status(GmailConnectStatus(state="failed", reason="refused"))
        logger.warning("gmail-connect: refused before spawn: %s", exc)
        return {"ok": False, "error": "refused", "state": "failed"}
    if outcome.status == "connected":
        store_status(GmailConnectStatus(state="connected"))
        return {"ok": True, "state": "connected"}
    store_status(GmailConnectStatus(state="failed", reason=outcome.reason))
    return {"ok": False, "error": outcome.reason or "failed", "state": "failed"}


async def gmail_disconnect_token(params: dict) -> dict:
    """Token-only sub-step of Disconnect: local projection clear.

    This op never produces the ``revoked`` projection state — that is
    reserved for the composite Disconnect (grant revocation first,
    then token; four-outcome semantics, design §2.3/§4) once the grant
    axis is wired. Remote token revocation is also not performed yet:
    invoke schema v1 is connect-axis only, so until the revoke op
    exists the honest record is ``revoke_unconfirmed`` — we never
    pretend the token is gone everywhere.
    """
    if load_status().state == "disconnected":
        return {
            "ok": True,
            "scope": "token_only",
            "state": "disconnected",
            "reason": "",
        }
    store_status(
        GmailConnectStatus(state="disconnected", reason="revoke_unconfirmed")
    )
    return {
        "ok": True,
        "scope": "token_only",
        "state": "disconnected",
        "reason": "revoke_unconfirmed",
    }
