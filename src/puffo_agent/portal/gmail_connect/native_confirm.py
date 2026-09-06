"""Device-local presence confirmation.

A web click relayed through the control plane is not presence (design
§2.1): the only thing that authorizes starting an OAuth flow is a
dialog answered on this machine. The dialog text is a fixed constant —
no remote-supplied string is ever interpolated, so a control-plane
command cannot phrase its own consent prompt.

Two backends, one contract. Both run the dialog in a CHILD PROCESS,
not a thread: the native calls block until answered, and a child is
the only thing we can actually kill when the timeout fires.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from enum import Enum

from ..._proc import no_window_kwargs


logger = logging.getLogger(__name__)

CONNECT_PROMPT = (
    "Connect Gmail for this machine's Puffo agents? "
    "This opens Google sign-in in your browser."
)
DIALOG_TITLE = "Puffo Agent"

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

# MessageBoxW flags. Yes/No with No as the default button mirrors the
# macOS dialog, where "Cancel" is default: if the box is dismissed by
# anything other than a deliberate Yes, the safe answer wins.
_MB_YESNO = 0x00000004
_MB_ICONWARNING = 0x00000030
_MB_DEFBUTTON2 = 0x00000100
_MB_SETFOREGROUND = 0x00010000
_MB_TOPMOST = 0x00040000
_WIN_DIALOG_FLAGS = (
    _MB_YESNO | _MB_ICONWARNING | _MB_DEFBUTTON2 | _MB_SETFOREGROUND | _MB_TOPMOST
)

# Exit codes for the Windows helper. Deliberately NOT 1 or 2: a Python
# that failed to start also exits non-zero with a small code, and a
# startup failure must never be readable as "the user said no".
_WIN_CONFIRMED = 0
_WIN_DECLINED = 7
_WIN_BACKEND_FAILED = 9

# The prompt and title arrive on argv, never interpolated into this
# source: the child parses text as data, so there is no path from a
# dialog string into executed code.
#
# IDYES/IDNO are checked explicitly and everything else — including
# MessageBoxW's 0 return, which means the call itself failed — exits
# as a backend failure. Treating "not Yes" as "the user declined" is
# the same mistake as reading any non-zero osascript status as cancel.
_WINDOWS_DIALOG_SOURCE = f"""\
import ctypes, sys
rc = ctypes.windll.user32.MessageBoxW(
    None, sys.argv[1], sys.argv[2], {_WIN_DIALOG_FLAGS}
)
if rc == 6:
    sys.exit({_WIN_CONFIRMED})
if rc == 7:
    sys.exit({_WIN_DECLINED})
sys.exit({_WIN_BACKEND_FAILED})
"""


def _classify_osascript(returncode: int, stderr: bytes) -> ConfirmOutcome:
    if returncode == 0:
        return ConfirmOutcome.CONFIRMED
    # Only the documented cancel signature counts as a person saying no.
    # stderr is inspected for that marker and never propagated further —
    # it is diagnostic text, and Layer B takes none of it.
    if _USER_CANCELLED_MARKER in (stderr or b"").decode("utf-8", "replace"):
        return ConfirmOutcome.CANCELLED
    logger.warning(
        "gmail-connect: confirm backend failed (rc=%s); refusing", returncode
    )
    return ConfirmOutcome.UNAVAILABLE


def _classify_windows(returncode: int, stderr: bytes) -> ConfirmOutcome:
    if returncode == _WIN_CONFIRMED:
        return ConfirmOutcome.CONFIRMED
    if returncode == _WIN_DECLINED:
        return ConfirmOutcome.CANCELLED
    logger.warning(
        "gmail-connect: confirm backend failed (rc=%s); refusing", returncode
    )
    return ConfirmOutcome.UNAVAILABLE


def _backend(prompt: str):
    """``(argv, classify)`` for this host, or ``None`` if it has none."""
    if sys.platform == "darwin":
        script = _OSASCRIPT_DIALOG.format(prompt=prompt.replace('"', "'"))
        return ["osascript", "-e", script], _classify_osascript
    if sys.platform == "win32":
        return (
            [sys.executable, "-c", _WINDOWS_DIALOG_SOURCE, prompt, DIALOG_TITLE],
            _classify_windows,
        )
    return None


async def request_native_confirm(
    prompt: str, *, timeout_s: float = 120.0
) -> ConfirmOutcome:
    """``CONFIRMED`` only on an explicit local confirmation.

    Every other branch names itself, and unknown failures fail closed to
    ``UNAVAILABLE`` rather than being guessed as a cancel — claiming the
    user declined when we do not know is both untrue and unactionable.
    """
    backend = _backend(prompt)
    if backend is None:
        logger.warning(
            "gmail-connect: no native confirm backend on %s; refusing",
            sys.platform,
        )
        return ConfirmOutcome.UNAVAILABLE
    argv, classify = backend
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **no_window_kwargs(),
        )
    except OSError:
        logger.warning("gmail-connect: confirm backend unavailable; refusing")
        return ConfirmOutcome.UNAVAILABLE
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.info("gmail-connect: confirm dialog timed out; refusing")
        return ConfirmOutcome.TIMEOUT
    return classify(proc.returncode, stderr)
