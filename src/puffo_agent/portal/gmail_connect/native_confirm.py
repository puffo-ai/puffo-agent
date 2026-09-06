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


async def request_native_confirm(prompt: str, *, timeout_s: float = 120.0) -> bool:
    """True only on an explicit local confirmation; everything else —
    cancel, timeout, unsupported platform, osascript failure — is False."""
    if sys.platform != "darwin":
        logger.warning(
            "gmail-connect: no native confirm backend on %s; refusing",
            sys.platform,
        )
        return False
    script = _OSASCRIPT_DIALOG.format(prompt=prompt.replace('"', "'"))
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        logger.warning("gmail-connect: osascript unavailable; refusing")
        return False
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.info("gmail-connect: confirm dialog timed out; refusing")
        return False
    # osascript exits non-zero when the user cancels the dialog.
    return code == 0
