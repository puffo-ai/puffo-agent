"""Operator-gated redemption of a group invite link (keyed/local agents).

Anyone can hand the agent a ``/i/<short_code>`` link, but the agent never
joins on its own: it asks its own operator with the same y/n permission
prompt the non-operator direct-invite path uses, and redeems only on
approval — so a link grants no more access than a direct invite would. No
operator configured ⇒ refuse (fail-closed), never a silent join. Keyless/
cloud is out of scope: redeem needs subkey-signed HTTP the bridge can't do.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..crypto.keystore import decode_secret
from ..crypto.primitives import Ed25519KeyPair
from .event_kinds import EventKind
from .events import random_event_id, random_nonce, sign_event
from .permission_prompt import format_permission_prompt

# ``/i/<code>`` is the shareable link form. The server mints a base64url code;
# accept a permissive run so a longer code still parses.
_SHORT_CODE_RE = re.compile(r"/i/([A-Za-z0-9_-]{6,64})")

# Re-asking the operator for the same (link, sender) inside this window is the
# spam bound on "anyone can send a link" — repeats are dropped, not re-prompted.
_APPROVAL_DEDUP_WINDOW_S = 300.0


def extract_invite_short_code(text: str) -> str | None:
    """Return the short code from the first ``/i/<code>`` in text, or None."""
    if not text:
        return None
    match = _SHORT_CODE_RE.search(text)
    return match.group(1) if match else None


def is_bare_invite_link(text: str) -> bool:
    """True when text is essentially just the invite link — dropping the
    link-bearing tokens leaves nothing the model needs. A mixed message (link
    plus a real request) is not bare, so it is never silently swallowed."""
    remainder = " ".join(tok for tok in text.split() if "/i/" not in tok)
    return remainder.strip(" \t\r\n.,!?;:") == ""


def recently_asked(
    seen: dict[tuple[str, str], float],
    key: tuple[str, str],
    now: float,
    window_s: float = _APPROVAL_DEDUP_WINDOW_S,
) -> bool:
    """Dedup check: True when this (code, sender) was asked inside ``window_s``.
    Prunes stale keys; does NOT record — the caller records only after the
    operator was actually asked, so a failed preview/DM can't block retries."""
    for stale in [k for k, seen_at in seen.items() if now - seen_at > window_s]:
        del seen[stale]
    return key in seen


async def request_link_redeem_approval(
    client: Any,
    *,
    short_code: str,
    source_slug: str,
) -> None:
    """Fail-closed with no operator (log, don't join); else dedup, fetch the
    link preview, and DM the operator a y/n prompt keyed for this redeem."""
    if not client.operator_slug:
        client._log.warning(
            "invite-link redeem from %s refused: no operator_slug configured — "
            "not joining (short_code=%s)",
            source_slug,
            short_code,
        )
        return
    key = (short_code, source_slug)
    if recently_asked(client._redeem_approval_seen, key, time.time()):
        client._log.info(
            "invite-link redeem from %s deduped within window (short_code=%s)",
            source_slug,
            short_code,
        )
        return
    try:
        preview = await client.http.get(f"/v2/invitations/links/{short_code}")
    except Exception:
        client._log.exception(
            "invite-link redeem: preview fetch failed (short_code=%s)", short_code
        )
        return
    space_id = (preview or {}).get("space_id") or ""
    invite_id = (preview or {}).get("invite_id") or ""
    if not space_id or not invite_id:
        client._log.warning(
            "invite-link redeem: preview missing ids (short_code=%s)", short_code
        )
        return
    space_name = (preview or {}).get("space_name")
    text = format_permission_prompt(
        f"@{source_slug} sent me an invite link to space "
        f"{_space_label(space_id, space_name)}. Join?",
    )
    try:
        envelope = await client._send_dm(client.operator_slug, text, root_id="")
    except Exception:
        client._log.exception(
            "invite-link redeem: operator DM failed (short_code=%s)", short_code
        )
        return
    env_id = (envelope or {}).get("envelope_id", "") if envelope else ""
    if env_id:
        client._pending_redeem_dms[env_id] = {
            "short_code": short_code,
            "invite_id": invite_id,
            "space_id": space_id,
            "space_name": space_name,
            "source_slug": source_slug,
        }
        # Record the dedup key only now — after the operator was actually
        # asked — so a failed preview/DM above never blocks a legit retry.
        client._redeem_approval_seen[key] = time.time()


async def handle_redeem_reply(
    client: Any,
    *,
    thread_root_id: str,
    text: str,
) -> bool:
    """Operator y/n for a pending link-redeem prompt: y → redeem (join), n →
    drop. Returns True when consumed (caller skips the LLM)."""
    meta = client._pending_redeem_dms.get(thread_root_id)
    if meta is None:
        return False
    normalized = text.strip().lower()
    if normalized in ("y", "yes"):
        approved = True
    elif normalized in ("n", "no"):
        approved = False
    else:
        return False

    client._pending_redeem_dms.pop(thread_root_id, None)
    label = _space_label(meta["space_id"], meta.get("space_name"))
    if approved:
        try:
            await redeem_invite_capability(
                client,
                short_code=meta["short_code"],
                invite_id=meta["invite_id"],
                space_id=meta["space_id"],
            )
            confirm = f"Joined space {label}. ✓"
            client._log.info(
                "operator-approved invite-link redeem (space=%s short_code=%s)",
                meta["space_id"],
                meta["short_code"],
            )
        except Exception:
            # Keep the raw error (server body/status) in the log only; the
            # operator-facing line stays a mapped summary (soul: no raw errors).
            client._log.exception(
                "operator-approved invite-link redeem failed (space=%s)",
                meta["space_id"],
            )
            confirm = (
                f"Couldn't join space {label} — the link may be expired or "
                "already used (details in logs)."
            )
    else:
        confirm = f"Won't join space {label}."
        client._log.info(
            "operator-rejected invite-link redeem (space=%s short_code=%s)",
            meta["space_id"],
            meta["short_code"],
        )
    try:
        await client._send_dm(client.operator_slug, confirm, root_id=thread_root_id)
    except Exception:
        client._log.exception("invite-link redeem: confirm DM failed")
    return True


async def redeem_invite_capability(
    client: Any,
    *,
    short_code: str,
    invite_id: str,
    space_id: str,
) -> None:
    """Build + subkey-sign a RedeemInviteCapability and POST it to join."""
    sess = client.keystore.load_session(client.slug)
    signing_key = Ed25519KeyPair.from_secret_bytes(
        decode_secret(sess.subkey_secret_key)
    )
    payload = {
        "redemption_id": random_event_id(),
        "invite_id": invite_id,
        "space_id": space_id,
        "redeemer_slug": client.slug,
        "redeemer_device_id": client.device_id,
        "redeemer_subkey_id": sess.subkey_id,
        "redeemed_at": int(time.time() * 1000),
        "nonce": random_nonce(),
    }
    signed = sign_event(
        kind=EventKind.REDEEM_INVITE_CAPABILITY,
        payload=payload,
        signer_slug=client.slug,
        signer_device_id=client.device_id,
        signer_subkey_id=sess.subkey_id,
        signing_key=signing_key,
    )
    await client.http.post(
        f"/v2/invitations/links/{short_code}/redeem", {"event": signed}
    )


def _space_label(space_id: str, space_name: str | None) -> str:
    return f"**{space_name}**({space_id})" if space_name else space_id
