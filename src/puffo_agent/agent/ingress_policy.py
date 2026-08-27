"""The one inbound-policy contract, shared by both ingress transports.

Puffo has two inbound paths — native signed WebSocket delivery
(``inbound_receipts.InboundReceiptHandler.handle``) and the keyless
cloud bridge (``bridge_transport.store_bridge_payload``). They persist
through different lanes, but they must reach the *same* admit/drop
decision, so every policy gate lives here exactly once and each
transport only maps the verdict onto its own storage call.

Server-guaranteed vs Agent-enforced
-----------------------------------
Established from current ``puffo-server`` source, not assumed. For a
keyless agent the server filters **only**:

* sender authentication / cert-chain, device targeting, revocation,
  and replay dedupe;
* a DM-only blocklist row keyed on the recipient's own slug
  (``server/src/messages.rs``).

It does **not** apply foreign-DM approval (``server/src/allowlists.rs``
— the allowlist is advisory and is never consulted on the send path),
carries **no** operator-control concept on the bridge wire
(``server/src/cloud_agent/bridge.rs`` — ``Message`` frames have no
operator flag), never checks a blocklist channel-side, and does not
filter at relay/backfill time (``server/src/cloud_agent/repo.rs``).
There is also no keyless blocklist route and blocklist writes require
subkey auth, so a keyless agent's blocklist is local state only.

Everything else — blocked contacts (DM **and** channel), operator
control messages, and foreign-DM approval — is therefore Agent-owned
and is enforced here for both transports.

Keyless capability envelope
---------------------------
A keyless agent cannot call subkey-signed routes. Gate *dispositions*
are enforced locally in every case; gate *side effects* that need
signed HTTP (allowlist writes, the operator approval prompt, the
first-DM notice) are skipped with an explicit log line — a documented
degrade, never a silent one. A keyless foreign DM with no configured
``operator_slug`` is retained fail-closed; native keeps its existing
no-operator behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .message_store import ReceiptDisposition

BLOCKED_MESSAGE_PLACEHOLDER = "[message from a blocked account was dropped]"


@dataclass(frozen=True)
class GateVerdict:
    """A gate's decision to stop a message before it becomes eligible.

    ``content`` replaces the stored body when the gate must not persist
    the original plaintext (the blocked-sender tombstone).
    """

    gate: str
    disposition: ReceiptDisposition
    reason: str
    content: str | None = None
    defer_ack: bool = False


def signed_http_available(client: Any) -> bool:
    """Whether this client can make subkey-signed HTTP calls.

    False for the keyless cloud-bridge transport, which authenticates
    via the sandbox egress token and has no keystore identity to sign
    with. Callers use it to skip a signed side effect, never to skip a
    gate decision.
    """
    if getattr(client, "_bridge", None) is not None:
        return False
    return not bool(getattr(getattr(client, "http", None), "keyless", False))


async def blocked_gate(client: Any, payload: Any) -> GateVerdict | None:
    """Drop anything from a blocked sender, DM or channel.

    The server's blocklist filter is DM-only and inert for keyless
    agents, so this is the only check standing between a blocked
    account and the model. A tombstone row is stored in place of the
    body: the message is accounted for without its plaintext ever
    reaching the database.

    Propagates ``BlocklistUnavailable`` when the list cannot be read at
    all (see ``ContactCache.is_blocked``). Each transport turns that into
    a hold — an unacked delivery the server will send again — because
    neither admitting nor tombstoning is a decision this gate has the
    facts to make.
    """
    sender = payload.sender_slug
    if sender == client.slug:
        return None
    if not await client._contacts.is_blocked(sender):
        return None

    if payload.envelope_kind == "dm":
        client._log.info("dm_gate: dropped DM from blocked %s", sender)
        reason = "blocked dm tombstone"
    else:
        client._log.info(
            "contact: dropped channel message from blocked %s "
            "(redacted placeholder stored)",
            sender,
        )
        reason = "blocked channel tombstone"
    return GateVerdict(
        gate="blocked",
        disposition=ReceiptDisposition.TERMINAL,
        reason=reason,
        content=BLOCKED_MESSAGE_PLACEHOLDER,
    )


async def operator_control_gate(
    client: Any,
    payload: Any,
    *,
    thread_root_id: str,
    text: str,
) -> GateVerdict | None:
    """Consume an operator control DM (invite / leave / permission / DM
    approval reply) so a bare ``y`` never reaches the LLM as chat.

    Every handler keys on in-process pending state and is fail-soft, so
    the sequence runs identically on both transports. Only the bulk
    invite summary needs signed HTTP; when it is unavailable the reply
    is still consumed terminally, with the skipped notification logged.
    """
    if not (
        payload.envelope_kind == "dm"
        and payload.sender_slug == client.operator_slug
    ):
        return None

    targets, is_direct = client._resolve_invite_targets(thread_root_id, text)
    handled_labels = (
        await client._apply_invite_replies(targets, text) if targets else []
    )
    if handled_labels:
        if is_direct:
            if signed_http_available(client):
                await client._send_invite_bulk_summary(
                    handled_labels,
                    text,
                    thread_root_id,
                )
            else:
                client._log.info(
                    "invite: keyless transport cannot DM the bulk-reply "
                    "summary for %d invite(s); reply still consumed",
                    len(handled_labels),
                )
        return GateVerdict(
            gate="operator_control",
            disposition=ReceiptDisposition.TERMINAL,
            reason="handled invite reply",
        )

    controls = (
        (client._maybe_handle_leave_reply, "handled leave reply"),
        (client._maybe_handle_permission_reply, "handled permission reply"),
        (client._maybe_handle_dm_approval_reply, "handled dm approval reply"),
    )
    for handler, reason in controls:
        if await handler(thread_root_id=thread_root_id, text=text):
            return GateVerdict(
                gate="operator_control",
                disposition=ReceiptDisposition.TERMINAL,
                reason=reason,
            )
    return None


async def foreign_dm_gate(
    client: Any,
    payload: Any,
    raw_text: str,
) -> GateVerdict | None:
    """Hold a DM from an untrusted stranger until the operator approves.

    The server never applies this gate — it is entirely Agent-owned on
    both transports. Keyless holds an untrusted DM gated until its local
    approval control plane resolves it, including when no operator is
    configured. The signed/native no-operator behavior remains unchanged.
    """
    if payload.envelope_kind != "dm":
        return None
    sender = payload.sender_slug
    signed = signed_http_available(client)

    if not await client._is_foreign_dm_sender(sender):
        if signed:
            await client._ensure_trusted_contact(sender)
        else:
            client._log.debug(
                "dm_gate: keyless transport cannot allowlist trusted "
                "sender %s (signed /allowlists unavailable)",
                sender,
            )
        return None

    if signed:
        await client._maybe_send_dm_notice(sender)
    trusted = (
        client.auto_accept_dm
        or await client._contacts.is_allowed(sender)
        # /spaces is subkey-signed; keyless would only fail closed here.
        or (signed and await client._shares_space_with(sender))
    )
    if trusted:
        return None

    if not signed:
        if not client.operator_slug:
            client._log.warning(
                "auto_accept_dm=False but no operator_slug configured; "
                "holding keyless DM from %s fail-closed",
                sender,
            )
            return GateVerdict(
                gate="foreign_dm",
                disposition=ReceiptDisposition.FOREIGN_DM_GATED,
                reason="foreign dm gated (keyless: no operator configured)",
            )
        client._log.info(
            "dm_gate: keyless transport cannot send the operator approval "
            "prompt for %s — holding the DM gated rather than delivering "
            "it unapproved",
            sender,
        )
        return GateVerdict(
            gate="foreign_dm",
            disposition=ReceiptDisposition.FOREIGN_DM_GATED,
            reason="foreign dm gated (keyless: no operator prompt)",
        )

    if not await client._maybe_gate_foreign_dm(
        sender_slug=sender,
        text=raw_text,
    ):
        return None
    return GateVerdict(
        gate="foreign_dm",
        disposition=ReceiptDisposition.FOREIGN_DM_GATED,
        reason="foreign dm awaiting approval",
    )
