"""Machine-level control-plane ops for the Gmail connector.

Ordering constraint: the native confirmation is the gate — until it
returns True, nothing is spawned and no OAuth endpoint is contacted
(no confirm → zero S4 calls). The executor entrypoint comes only from
local daemon config, never from command params: a control-plane
command that could name its own binary would be remote code execution.
"""

from __future__ import annotations

import logging

from ..state import DaemonConfig
from .executor import ExecutorRefused, run_gmail_executor
from .native_confirm import CONNECT_PROMPT, request_native_confirm
from .status_store import (
    GmailConnectStatus,
    load_status,
    mask_account,
    store_status,
)


logger = logging.getLogger(__name__)


def _config() -> DaemonConfig:
    return DaemonConfig.load()


async def gmail_connect_initiate(params: dict) -> dict:
    gc = _config().gmail_connect
    if not gc.enabled or not gc.executor_path:
        return {"ok": False, "error": "gmail_connect not configured"}
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
            {"op": "connect", "callback_host": "127.0.0.1"},
            flow_timeout_s=gc.flow_timeout_seconds,
        )
    except ExecutorRefused as exc:
        store_status(GmailConnectStatus(state="failed", reason="refused"))
        logger.warning("gmail-connect: refused before spawn: %s", exc)
        return {"ok": False, "error": "refused", "state": "failed"}
    if outcome.status == "connected":
        store_status(
            GmailConnectStatus(
                state="connected",
                account_masked=mask_account(outcome.account),
                expires_at=outcome.expires_at,
            )
        )
        return {"ok": True, "state": "connected"}
    store_status(GmailConnectStatus(state="failed", reason=outcome.reason))
    return {"ok": False, "error": outcome.reason or "failed", "state": "failed"}


async def gmail_disconnect_token(params: dict) -> dict:
    """Token-only sub-step of Disconnect: best-effort remote revoke,
    unconditional local clear.

    This op never produces the ``revoked`` projection state — whatever
    the executor claims, the cap here is ``disconnected``. ``revoked``
    is reserved for the composite Disconnect (grant revocation first,
    then token; four-outcome semantics, design §2.3/§4) that only
    exists once the grant axis is wired.

    Disconnect never requires presence and never blocks on Google:
    fail-open toward revocation is the safe direction, but an
    unconfirmed remote revoke is recorded honestly instead of
    pretending the token is gone everywhere.
    """
    if load_status().state == "disconnected":
        return {
            "ok": True,
            "scope": "token_only",
            "state": "disconnected",
            "reason": "",
        }
    gc = _config().gmail_connect
    reason = ""
    if gc.enabled and gc.executor_path:
        try:
            outcome = await run_gmail_executor(
                gc.executor_path,
                {"op": "revoke", "callback_host": "127.0.0.1"},
                flow_timeout_s=min(gc.flow_timeout_seconds, 60.0),
            )
            if outcome.status == "failed":
                reason = "revoke_unconfirmed"
        except ExecutorRefused:
            reason = "revoke_unconfirmed"
    else:
        reason = "revoke_unconfirmed"
    # Cap at "disconnected" regardless of what the executor claimed:
    # token-only success alone must never read as the composite
    # ``revoked`` (runbook v6 §7 negative control).
    store_status(GmailConnectStatus(state="disconnected", reason=reason))
    return {
        "ok": True,
        "scope": "token_only",
        "state": "disconnected",
        "reason": reason,
    }
