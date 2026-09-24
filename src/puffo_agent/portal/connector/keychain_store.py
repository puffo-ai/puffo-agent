"""The connection in the login Keychain — the real store on macOS (v0.4 §8).

What this buys, stated narrowly: the credential is no longer a plaintext file
under the connector directory, which is the property the acceptance scan reads
(测试姬 219875). It does not buy protection from another process running as
this user. The item is created without ``-T ""`` on purpose — with no trusted
application every read raises an authorization prompt, and a daemon has nobody
to answer one — so the creating application is trusted and the data is
reachable by anything on this computer that can run ``security`` as this user.
Same UID was never a boundary here anyway; pretending otherwise would be worse
than saying it.

Two shapes of failure matter more than the rest, and both are handled by
refusing rather than guessing:

* ``security`` answers 44 both for "there is no such item" and for a search
  over a set of keychains that does not include the one we mean — measured:
  with no login Keychain in the search list, a lookup for a missing item and a
  lookup that could never have succeeded return the same code. So 44 only
  becomes "no connection" after a separate check that a Keychain is reachable
  at all. Without that, an unreachable Keychain reads as "not connected" and
  the claim above would happily write over a credential still sitting there.

* The password goes in on stdin, never in ``argv``. ``security``'s own usage
  says ``Use of the -p or -w options is insecure. Specify -w as the last
  option to be prompted``, and on this computer another process running as the
  same user can read a full command line out of ``ps``.

  **That stdin form does not work, measured** (Jeff 220726): ``-w`` last
  prompts on a *terminal*, not on stdin, so with the value piped in
  ``security`` stores an EMPTY password and still exits 0 — on both the write
  and the read back. The read-back check below is what caught it, which is the
  only reason this is a known failure rather than a daemon reporting connected
  with nothing stored. Until the write goes through ``SecItemAdd`` instead of
  the CLI, ``_put`` cannot succeed on a real Keychain and this backend stays
  unreachable behind ``_KEYCHAIN_VERIFIED``.
"""

from __future__ import annotations

import logging
import subprocess

from ..._proc import no_window_kwargs
from .store import ConnectionStore

logger = logging.getLogger(__name__)

SECURITY = "/usr/bin/security"

# One item per machine identity. The Keychain belongs to the login user and is
# shared by every puffo home on this computer, while a connection belongs to
# the machine identity that claimed it (``control/machine.json``) — so the
# account has to be that identity, or a staging daemon and a production daemon
# would fight over one item.
SERVICE = "Puffo Agent-connector"

# ``security``'s "the specified item could not be found". Measured on this
# computer for both ``find-generic-password`` and ``delete-generic-password``.
_ITEM_NOT_FOUND = 44

# A single ``security`` call. Generous because an authorization prompt, if one
# ever appears, blocks until it is answered; matches the existing Claude Code
# Keychain path rather than inventing a second number.
TIMEOUT_SECONDS = 60


class KeychainUnavailable(OSError):
    """The Keychain could not be reached, or did not do what it was told.

    An ``OSError`` deliberately: every caller above already treats that as
    "cannot tell" rather than "nothing here" — the claim reports a local-read
    failure and writes nothing — so an unreachable Keychain fails closed
    without a single branch added upstream.
    """


class KeychainConnectionStore(ConnectionStore):
    """One connection per machine identity, in the login Keychain."""

    def __init__(
        self,
        account: str,
        *,
        service: str = SERVICE,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.account = account
        self.service = service
        self.timeout = timeout

    def _read(self) -> bytes | None:
        found = self._run(["find-generic-password", "-s", self.service, "-a", self.account, "-w"])
        if found.returncode == 0:
            # ``security`` prints the password with one newline appended;
            # ``_encode`` never ends in one, so removing exactly one is exact.
            # If that ever stops being true the read-back check in ``_put``
            # turns it into a loud failure rather than a corrupted record.
            return found.stdout.removesuffix(b"\n")
        if found.returncode == _ITEM_NOT_FOUND:
            self._require_a_reachable_keychain()
            return None
        raise KeychainUnavailable(
            f"could not read the connection from the Keychain: {_explain(found)}"
        )

    def _put(self, body: bytes) -> None:
        stored = self._run(
            ["add-generic-password", "-U", "-s", self.service, "-a", self.account, "-w"],
            stdin=body,
        )
        if stored.returncode != 0:
            raise KeychainUnavailable(
                f"could not write the connection to the Keychain: {_explain(stored)}"
            )
        if self._read() != body:
            # Not erased. If the write simply did not take, the item still
            # holds the credential this computer was using, and deleting it
            # would turn a failed refresh into a lost connection. If something
            # unreadable did land, the next load fails to parse and the claim
            # refuses — closed either way, and visible.
            raise KeychainUnavailable(
                "the Keychain did not store what was written; the connection "
                "on this computer is unchanged or unusable, not replaced"
            )

    def _erase(self) -> None:
        removed = self._run(["delete-generic-password", "-s", self.service, "-a", self.account])
        if removed.returncode == 0:
            return
        if removed.returncode == _ITEM_NOT_FOUND:
            # The same 44 as in ``_read``, and it needs the same control. A
            # delete that never reached the right Keychain also answers "no
            # such item", and taking that for "already gone" would report a
            # disconnect as done while the credential sat there — the exact
            # breach this method exists to prevent (Jeff 220726: ``_erase``
            # was reading 44 as success without probing).
            self._require_a_reachable_keychain()
            return
        # A disconnect that could not clear must not report success: the
        # promise is that the local credential goes first (Jeremy 217298 /
        # Jeff 217299), and a swallowed failure here breaks exactly that.
        raise KeychainUnavailable(
            f"could not remove the connection from the Keychain: {_explain(removed)}"
        )

    def _require_a_reachable_keychain(self) -> None:
        """Turn "not found" into "not connected" only when that is a fact.

        The same exit code covers "no such item" and "not looking in the right
        place", so absence on its own proves nothing. A default Keychain being
        resolvable is the cheapest evidence that the lookup above actually
        searched somewhere a connection could have been.
        """
        probe = self._run(["default-keychain"])
        if probe.returncode != 0 or not probe.stdout.strip():
            raise KeychainUnavailable(
                "no default Keychain is reachable, so 'no such item' cannot be "
                f"read as 'not connected': {_explain(probe)}"
            )

    def _run(self, arguments: list[str], *, stdin: bytes | None = None):
        try:
            return subprocess.run(
                [SECURITY, *arguments],
                input=stdin,
                capture_output=True,
                timeout=self.timeout,
                # A no-op off Windows, and this store is macOS-only — but the
                # repo's window-policy guard enumerates every spawn, and
                # earning an exemption entry for a call that costs nothing to
                # comply with would grow the list of things it stops watching.
                **no_window_kwargs(),
            )
        except FileNotFoundError as exc:
            raise KeychainUnavailable(f"{SECURITY} is not on this computer") from exc
        except subprocess.TimeoutExpired as exc:
            raise KeychainUnavailable(
                f"{SECURITY} did not answer within {self.timeout:g}s; "
                "an authorization prompt may be waiting for someone"
            ) from exc


def _explain(completed) -> str:
    """What ``security`` said, kept out of the exception's own wording.

    The stderr line is the only thing that distinguishes a locked Keychain
    from a missing one from a denied one, and none of those are worth their own
    branch — but losing the sentence would leave nothing to diagnose by.
    """
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    return f"exit {completed.returncode}" + (f": {stderr}" if stderr else "")
