"""Foreign-DM approval operations delegated by PuffoCoreMessageClient."""

from __future__ import annotations

import time

from ..crypto.ws_client import TransportOutcome
from .client_support import DM_GATE_SENDER_ACK
from .permission_prompt import format_permission_prompt


async def maybe_send_dm_notice(client, sender_slug: str) -> None:
    """Operator FYI for every non-trusted sender (contacts included):
    first DM immediately, then one per 72h; persisted."""
    if not client.operator_slug:
        return
    try:
        last = await client.store.get_dm_notice(sender_slug)
    except Exception:
        last = None
    now_ms = int(time.time() * 1000)
    if last is not None and now_ms - last < client._DM_NOTICE_INTERVAL_MS:
        return
    display = await client._fetch_display_name(sender_slug)
    label = f"**{display}** ({sender_slug})" if display else f"@{sender_slug}"
    try:
        await client._send_dm(
            client.operator_slug,
            f"FYI, {label} is sending direct messages to me.",
            root_id="",
        )
    except Exception as exc:
        client._log.warning(
            "dm_notice: failed to notify operator about %s: %s",
            sender_slug,
            exc,
        )
        return
    try:
        await client.store.set_dm_notice(sender_slug, now_ms)
    except Exception as exc:
        client._log.warning("dm_notice: failed to persist ts: %s", exc)


async def maybe_allowlist_outbound_dm(client, recipient_slug: str) -> None:
    """Agent DM'd a foreign peer first → allowlist them; best-effort."""
    if not recipient_slug:
        return
    # Never allowlist a sender we're currently gating — the ack DM
    # echoes back here and would pre-empt the operator's y/n.
    if any(
        m.get("sender_slug") == recipient_slug
        for m in client._pending_dm_approvals.values()
    ):
        return
    # Replying is not consent — only a genuinely agent-initiated
    # first DM allowlists. A stored inbound DM means they wrote first.
    try:
        if await client.store.has_dm_from(recipient_slug):
            return
    except Exception:
        return
    # Trusted short-circuit before is_allowed can hit the network
    # (the daemon DMs the operator constantly).
    if not await client._is_foreign_dm_sender(recipient_slug):
        return
    if await client._contacts.is_allowed(recipient_slug):
        return
    try:
        await client.http.post("/allowlists", {"slugs": [recipient_slug]})
    except Exception as exc:
        client._log.warning(
            "dm_gate: outbound allowlist for %s failed: %s",
            recipient_slug,
            exc,
        )
        return
    client._contacts.note_allowed(recipient_slug)
    if not client.operator_slug:
        return
    display = await client._fetch_display_name(recipient_slug)
    label = f"**{display}**(@{recipient_slug})" if display else f"@{recipient_slug}"
    try:
        await client._send_dm(
            client.operator_slug,
            f"Allowlisted {label} — I messaged them first, so their "
            "replies won't need approval.",
            root_id="",
        )
    except Exception:
        client._log.exception(
            "dm_gate: failed to notify operator of outbound allowlist",
        )


async def maybe_gate_foreign_dm(
    client,
    *,
    sender_slug: str,
    text: str,
) -> bool:
    """Prompt the operator for a held foreign DM. True = gated (caller
    skips the ack; the DM waits in /messages/pending). One prompt per
    sender — further DMs ride the same y/n.
    """
    for entry in client._pending_dm_approvals.values():
        if entry.get("sender_slug") == sender_slug:
            client._log.info(
                "auto_accept_dm: already prompted for %s; holding "
                "further DMs in pending",
                sender_slug,
            )
            return True
    if not client.operator_slug:
        client._log.warning(
            "auto_accept_dm=False but no operator_slug configured; "
            "delivering DM from %s without approval",
            sender_slug,
        )
        return False
    sender_display, _ = await client._fetch_user_profile(sender_slug)
    label = sender_display or sender_slug
    preview = text if len(text) <= 280 else text[:277] + "…"
    prompt = format_permission_prompt(
        f"**{label}** ({sender_slug}) is DM-ing me — allow "
        f"(allowlist + deliver) or block?",
        detail=preview,
    )
    try:
        envelope = await client._send_dm(
            client.operator_slug,
            prompt,
            root_id="",
        )
    except Exception as exc:
        # Approval is a control plane: a prompt that cannot reach the
        # operator must not open the gate. No pending entry is recorded,
        # so the sender's next DM retries the prompt.
        client._log.error(
            "auto_accept_dm: failed to send approval prompt for %s: %s; "
            "holding the DM gated (fail-closed), prompt retries on their "
            "next DM",
            sender_slug,
            exc,
        )
        return True
    prompt_env_id = envelope.get("envelope_id", "") if envelope else ""
    if not prompt_env_id:
        client._log.error(
            "auto_accept_dm: approval prompt for %s got no envelope_id; "
            "holding the DM gated (fail-closed), prompt retries on their "
            "next DM",
            sender_slug,
        )
        return True
    client._pending_dm_approvals[prompt_env_id] = {
        "sender_slug": sender_slug,
        "sender_display_name": sender_display,
    }
    from .dm_approvals import save_pending_dm_approvals

    try:
        save_pending_dm_approvals(client.slug, client._pending_dm_approvals)
    except OSError as exc:
        client._log.warning(
            "auto_accept_dm: failed to persist pending state: %s",
            exc,
        )
    # Ack the sender so they're not waiting on silence. Once per
    # sender; best-effort — never blocks the gate.
    try:
        await client._send_dm(sender_slug, DM_GATE_SENDER_ACK, root_id="")
    except Exception as exc:
        client._log.warning(
            "auto_accept_dm: failed to ack sender %s: %s",
            sender_slug,
            exc,
        )
    client._log.info(
        "auto_accept_dm: prompted operator for %s (thread %s); DM held "
        "in /messages/pending",
        sender_slug,
        prompt_env_id,
    )
    return True


async def maybe_handle_dm_approval_reply(
    client,
    *,
    thread_root_id: str,
    text: str,
) -> bool:
    meta = client._pending_dm_approvals.get(thread_root_id)
    if meta is None:
        return False
    normalized = text.strip().lower()
    if normalized in ("y", "yes"):
        approved = True
    elif normalized in ("n", "no"):
        approved = False
    else:
        return False

    sender_slug = meta.get("sender_slug", "")
    sender_label = meta.get("sender_display_name") or sender_slug
    try:
        if approved:
            await client.http.post(
                "/allowlists",
                {"slugs": [sender_slug]},
            )
            client._contacts.note_allowed(sender_slug)
            await client._drain_pending_from_sender(sender_slug)
            confirm = f"Allowed DMs from **{sender_label}** ({sender_slug}). ✓"
        else:
            await client.http.post(
                "/blocklists",
                {"target": "user", "id": sender_slug},
            )
            client._contacts.note_blocked(sender_slug, True)
            await client._drop_pending_from_sender(sender_slug)
            confirm = (
                f"Blocked **{sender_label}** ({sender_slug}). "
                "Server will drop future messages from this sender."
            )
    except Exception as exc:
        client._log.exception(
            "auto_accept_dm: %s reply for %s failed",
            "approve" if approved else "block",
            sender_slug,
        )
        confirm = (
            f"{'Approval' if approved else 'Block'} for "
            f"**{sender_label}** failed: {exc}. The pending entry is "
            "kept; reply again once the server is reachable."
        )
        try:
            await client._send_dm(
                client.operator_slug,
                confirm,
                root_id=thread_root_id,
            )
        except Exception:
            client._log.exception(
                "auto_accept_dm: failed to send error confirmation",
            )
        return True

    client._pending_dm_approvals.pop(thread_root_id, None)
    from .dm_approvals import save_pending_dm_approvals

    try:
        save_pending_dm_approvals(client.slug, client._pending_dm_approvals)
    except OSError as exc:
        client._log.warning(
            "auto_accept_dm: failed to persist pending state: %s",
            exc,
        )
    try:
        await client._send_dm(
            client.operator_slug,
            confirm,
            root_id=thread_root_id,
        )
    except Exception:
        client._log.exception(
            "auto_accept_dm: failed to confirm outcome to operator",
        )
    return True


async def drain_pending_from_sender(client, sender_slug: str) -> None:
    # Re-fed through on_message so gated DMs take the same decrypt +
    # priority-queue path as live arrivals, then acked.
    if not sender_slug or client._ws is None or client._ws.on_message is None:
        return
    try:
        data = await client.http.get(f"/messages/pending?kind=dm&sender={sender_slug}")
    except Exception as exc:
        client._log.warning(
            "auto_accept_dm: drain fetch for %s failed: %s",
            sender_slug,
            exc,
        )
        return
    messages = data.get("messages", []) if isinstance(data, dict) else []
    acked: list[str] = []
    for item in messages:
        try:
            result = await client._ws.dispatch_delivery(item)
        except Exception:
            client._log.exception("auto_accept_dm: drain handle failed")
            continue
        envelope = item.get("envelope", item)
        eid = envelope.get("envelope_id")
        if eid and result.outcome is TransportOutcome.ACK:
            acked.append(eid)
    if acked:
        try:
            await client.http.post("/messages/ack", {"envelope_ids": acked})
        except Exception as exc:
            client._log.warning(
                "auto_accept_dm: drain ack for %s failed: %s",
                sender_slug,
                exc,
            )
    client._log.info(
        "auto_accept_dm: drained %d pending DM(s) from %s",
        len(acked),
        sender_slug,
    )


async def drop_pending_from_sender(client, sender_slug: str) -> None:
    # Ack-discard a blocked sender's pending DMs; never seen by the agent.
    if not sender_slug:
        return
    # Acking the server's copy leaves the locally stored gated rows behind.
    # ``cleanup()`` never expires a gated row — correct while the hold is
    # open, but the operator has now said no, so the refused body would be
    # retained forever and stay readable. Discard it here.
    try:
        discarded = await client.store.tombstone_gated_dms_from(sender_slug)
    except Exception:  # noqa: BLE001 — never block the block itself
        client._log.exception(
            "auto_accept_dm: could not discard held DMs from denied %s",
            sender_slug,
        )
    else:
        if discarded:
            client._log.info(
                "auto_accept_dm: discarded %d held DM body/bodies from "
                "denied %s",
                discarded,
                sender_slug,
            )
    try:
        data = await client.http.get(f"/messages/pending?kind=dm&sender={sender_slug}")
    except Exception as exc:
        client._log.warning(
            "auto_accept_dm: drop fetch for %s failed: %s",
            sender_slug,
            exc,
        )
        return
    messages = data.get("messages", []) if isinstance(data, dict) else []
    eids = [(item.get("envelope", item)).get("envelope_id") for item in messages]
    eids = [e for e in eids if e]
    if eids:
        try:
            await client.http.post("/messages/ack", {"envelope_ids": eids})
        except Exception as exc:
            client._log.warning(
                "auto_accept_dm: drop ack for %s failed: %s",
                sender_slug,
                exc,
            )
    client._log.info(
        "auto_accept_dm: dropped %d pending DM(s) from blocked %s",
        len(eids),
        sender_slug,
    )
