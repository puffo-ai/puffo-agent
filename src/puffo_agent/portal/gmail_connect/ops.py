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
from .native_confirm import CONNECT_PROMPT, ConfirmOutcome, request_native_confirm
from .status_store import REASONS, GmailConnectStatus, load_status, store_status


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


def _reply(reason: str, *, ok: bool, state: str) -> dict:
    """The only shape a control-plane reply may take (Jeff 188515).

    Exactly ``{ok, state, reason}`` — ``ok`` is the transport envelope
    and ``reason`` is always a roster member, so §5.1, the roster
    control and the two clamps are one contract with no exceptions to
    explain. There is no ``error`` key: free text has nowhere to go.

    ``state`` describes THIS CALL's result and is required (Jeff
    188607). It used to default to ``load_status().state``, which
    silently handed three early-exit branches whatever was on disk —
    so a pre-flight refusal reported ``connected`` on a machine that
    was already connected (Boris 188604). A required argument is why a
    branch added later cannot inherit that mistake without saying so.

    The machine's durable connection state lives in the status
    projection, and is read separately; a command reply is never
    written back as status.
    """
    if reason not in REASONS:
        reason = "internal_error"
    return {"ok": ok, "state": state, "reason": reason}


# Jeff 188539 (final): the non-confirming facts are NOT one outcome. Only
# a provable human cancel reports as a transient user choice; the rest are
# failures of this machine and are persisted, so the UI can say something
# true and actionable instead of "cancelled".
#
# ``confirm_timeout`` is deliberately NOT the executor's ``timeout``:
# nobody answering the dialog (never contacted Google, just retry) and the
# executor going silent after start (consent page may already have opened)
# are different facts, and one value for both is the same alias bug we
# just removed (Boris 188538).
_CONFIRM_REFUSALS = {
    ConfirmOutcome.CANCELLED: ("disconnected", "refused"),
    ConfirmOutcome.TIMEOUT: ("failed", "confirm_timeout"),
    ConfirmOutcome.UNAVAILABLE: ("failed", "confirm_unavailable"),
}


def _confirm_refusal(outcome: ConfirmOutcome) -> dict:
    state, reason = _CONFIRM_REFUSALS[outcome]
    if state == "failed":
        # A cancel is the user's transient choice and is not recorded as
        # a failure; everything else is a real failure of this host.
        store_status(GmailConnectStatus(state=state, reason=reason))
    return _reply(reason, ok=False, state=state)


async def gmail_connect_initiate(params: dict) -> dict:
    gc = _config().gmail_connect
    # The three pre-flight refusals report THIS CALL as failed and
    # deliberately do not persist: a broken config did not disconnect
    # anything, so overwriting a real ``connected`` record would
    # destroy the truth rather than report it (Jeff 188607 §3).
    if not gc.enabled or not gc.executor_path:
        return _reply("executor_unavailable", ok=False, state="failed")
    if not gc.data_root or not gc.client_bundle_sha256:
        return _reply("executor_unavailable", ok=False, state="failed")
    if load_status().state == "pending":
        return _reply("protocol", ok=False, state="failed")
    confirm = await request_native_confirm(
        CONNECT_PROMPT, timeout_s=gc.confirm_timeout_seconds
    )
    if confirm is not ConfirmOutcome.CONFIRMED:
        return _confirm_refusal(confirm)
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
        return _reply("refused", ok=False, state="failed")
    if outcome.status == "connected":
        store_status(GmailConnectStatus(state="connected"))
        return _reply("", ok=True, state="connected")
    store_status(GmailConnectStatus(state="failed", reason=outcome.reason))
    return _reply(outcome.reason, ok=False, state="failed")


async def gmail_disconnect_token(params: dict) -> dict:
    """Token-only sub-step of Disconnect: local projection clear.

    This op never produces the ``revoked`` projection state — that is
    reserved for the composite Disconnect (grant revocation first,
    then token; design §2.3/§4) once the grant axis is wired. The
    token-only scope is carried by the op NAME, not by a returned
    discriminator: a ``scope`` field here would both exceed the §5.1
    whitelist and collide with Layer-A's ``scope`` (the OAuth grant
    scope), which is a different thing entirely (Jeff 188515).

    Remote token revocation is also not performed yet, so a success is
    recorded ``revoke_unconfirmed`` — we never pretend the token died
    everywhere.
    """
    if load_status().state == "disconnected":
        return _reply("", ok=True, state="disconnected")
    store_status(
        GmailConnectStatus(state="disconnected", reason="revoke_unconfirmed")
    )
    return _reply("revoke_unconfirmed", ok=True, state="disconnected")
