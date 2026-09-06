"""Device-local presence confirmation.

A web click relayed through the control plane is not presence (design
§2.1): the only thing that authorizes starting an OAuth flow is a
dialog answered on this machine. The dialog text is a fixed constant —
no remote-supplied string is ever interpolated, so a control-plane
command cannot phrase its own consent prompt.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from enum import Enum


logger = logging.getLogger(__name__)

CONNECT_PROMPT = (
    "Connect Gmail for this machine's Puffo agents? "
    "This opens Google sign-in in your browser."
)

_OSASCRIPT_DIALOG = (
    'display dialog "{prompt}" with title "Puffo Agent" '
    'buttons {{"Cancel", "Connect"}} default button "Cancel" '
    "with icon caution"
)


class ConfirmOutcome(Enum):
    """Why the confirmation did or did not happen.

    A bool cannot carry this: cancel, timeout, a host with no dialog
    backend at all, and a backend that failed to run are four different
    facts, and collapsing them told the user "cancelled" on machines
    that can never connect (Boris 188533, ruling Jeff 188535).
    """

    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"        # provably a person declining
    TIMEOUT = "timeout"            # nobody answered in time
    UNAVAILABLE = "unavailable"    # no backend, or the backend failed


# osascript reports an explicit user cancel as AppleScript error -128.
# Matching it is what lets us tell "a person said no" from "the dialog
# never worked"; anything else non-zero is NOT assumed to be a person.
_USER_CANCELLED_MARKER = "(-128)"


async def request_native_confirm(
    prompt: str, *, timeout_s: float = 120.0
) -> ConfirmOutcome:
    """``CONFIRMED`` only on an explicit local confirmation.

    Every other branch names itself, and unknown failures fail closed to
    ``UNAVAILABLE`` rather than being guessed as a cancel — claiming the
    user declined when we do not know is both untrue and unactionable.
    """
    if sys.platform != "darwin":
        logger.warning(
            "gmail-connect: no native confirm backend on %s; refusing",
            sys.platform,
        )
        return ConfirmOutcome.UNAVAILABLE
    script = _OSASCRIPT_DIALOG.format(prompt=prompt.replace('"', "'"))
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.warning("gmail-connect: osascript unavailable; refusing")
        return ConfirmOutcome.UNAVAILABLE
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.info("gmail-connect: confirm dialog timed out; refusing")
        return ConfirmOutcome.TIMEOUT
    if proc.returncode == 0:
        return ConfirmOutcome.CONFIRMED
    # Only the documented cancel signature counts as a person saying no.
    # stderr is inspected for that marker and never propagated further —
    # it is diagnostic text, and Layer B takes none of it.
    if _USER_CANCELLED_MARKER in (stderr or b"").decode("utf-8", "replace"):
        return ConfirmOutcome.CANCELLED
    logger.warning(
        "gmail-connect: confirm backend failed (rc=%s); refusing",
        proc.returncode,
    )
    return ConfirmOutcome.UNAVAILABLE
