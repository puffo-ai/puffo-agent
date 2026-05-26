"""PUF-247: human-facing copy for invite-accept/reject failures.

Lives in its own module (not in ``puffo_core_client.py``) because the
function is a pure string-formatter with no client state. Future
operator-facing invite copy lands here — keeping the helper next to
the 2900-line client file mixed the "puffo-core client" surface
(API + WS handling) with "user-facing error text" (UX copy), which
is a different responsibility.
"""

from __future__ import annotations

import json

from ..crypto.http_client import HttpError


def format_invite_error(exc: Exception, verb: str) -> str:
    """Translate an invite-accept/reject failure into a user-facing
    message safe to surface in the operator-DM confirm. Raw ``exc`` is
    preserved in the caller's ``log.exception`` for diagnostic; this
    helper produces ONLY the human-readable text.

    ``verb`` is ``"accept"`` or ``"reject"`` — used to compose the
    verb-correct prefix ("Couldn't accept invite", "Couldn't reject
    invite") so a single helper covers both catch sites.

    PUF-247 symptom (Sam's tier-1 screenshot): pre-fix this returned
    ``"HTTP 400: {\\"error\\": \\"INVALID_PAYLOAD\\", \\"message\\":
    \\"channel not found: ch_...\\"}"`` -- raw JSON dumped to chat as
    the agent's reply. Post-fix the typed error class becomes a
    short friendly sentence, never echoing the body.
    """
    prefix = f"Couldn't {verb} invite"
    if isinstance(exc, HttpError):
        # ``HttpError.body`` is the raw response text; try JSON-parse
        # to extract structured error + message codes the server sends.
        # Non-JSON bodies fall through to the generic status-class branch.
        error_code = ""
        message_text = ""
        try:
            parsed = json.loads(exc.body)
            if isinstance(parsed, dict):
                error_code = str(parsed.get("error") or "")
                message_text = str(parsed.get("message") or "")
        except (ValueError, TypeError):
            pass

        # Specific known mappings BEFORE the status-class fallbacks
        # below -- the order is intentional. A 403 with message
        # ``channel not found`` lands on the channel-specific branch
        # first by design (the specific reason is more useful to the
        # operator than the bare "no permission" framing). Future
        # reviewer: if you flip the order, you change which branch a
        # 403+message-shaped response hits.
        #
        # PUF-247 bug-1 (alpha/beta/gamma root cause) is still open at
        # the time of this PR: the server's ``channel not found``
        # response may turn out to be misreporting an envelope-
        # corruption symptom rather than a true stale invite. Until
        # bug-1 confirms (alpha), the channel/space copy is
        # deliberately ambiguous ("isn't reachable right now") so the
        # operator doesn't get a confident "no longer available"
        # reading on a channel that actually still exists. Promote to
        # definitive language once bug-1 lands.
        lower_msg = message_text.lower()
        if "channel not found" in lower_msg:
            return (
                f"{prefix}: the server says that channel isn't reachable "
                "right now. Try again later."
            )
        if "space not found" in lower_msg:
            return (
                f"{prefix}: the server says that space isn't reachable "
                "right now. Try again later."
            )
        if exc.status == 403 or error_code == "FORBIDDEN":
            return f"{prefix}: you don't have permission for this one."
        if exc.status == 409 or error_code == "CONFLICT":
            return f"{prefix}: looks like it's already been handled."

        # Status-class fallbacks. Never echo ``exc.body`` to the user
        # -- that's the leak this whole helper exists to prevent.
        if 400 <= exc.status < 500:
            return f"{prefix}: please try again."
        if exc.status >= 500:
            return (
                f"{prefix}: Puffo server hit an issue. "
                "Please try again in a moment."
            )

    return f"{prefix}: unexpected error. Please try again."
