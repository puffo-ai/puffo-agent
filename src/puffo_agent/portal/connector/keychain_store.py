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

  **That stdin form does not work.** Measured on a real Keychain with
  synthetic data (Jeff 220726): the value piped in, ``security`` exits 0 on
  both the write and the read back, and what comes back is an EMPTY password.
  That is the whole of the measurement. *Why* is Boris 220727's reading of the
  help text — ``-w`` last means "prompt", and a prompt reads a terminal rather
  than our pipe — which fits but which nobody has traced through the
  implementation, so it stays an explanation and not a located root cause
  (Jeff 220729, and again 220792 when this comment said otherwise).

  The read-back check below is what turned it into a known failure instead of
  a daemon reporting connected with nothing stored. Until the write moves to
  ``security -i`` with the value as hex through ``-X`` — measured working on a
  real Keychain, Jeff 220731/220737/220759 — ``_put`` cannot succeed and this
  backend stays unreachable behind ``_KEYCHAIN_VERIFIED``.
"""

from __future__ import annotations

import json
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

    def _write_command(self, body: bytes) -> bytes:
        """The one line handed to ``security -i``.

        The value travels as hex through ``-X``: nothing secret reaches argv,
        and hex sidesteps every quoting question a JSON blob would raise.
        Names are single-quoted, which Jeff 220737 measured working for a
        service name containing a space.

        Refusals rather than escaping. A name holding a quote would need the
        shell-quoting rules of a mode nobody has characterised — Jeff's probe
        covered a space and two wrapping styles and said so — and this store
        picks its own names, so refusing costs nothing and guessing could
        silently write somewhere else.
        """
        for label, name in (("service", self.service), ("account", self.account)):
            if not name or "'" in name or any(not 0x20 <= b <= 0x7E for b in name.encode()):
                raise KeychainUnavailable(
                    f"refusing to build a Keychain command: the {label} name is "
                    "empty, quoted, or not printable ASCII"
                )
        return (
            f"add-generic-password -U -s '{self.service}' -a '{self.account}' "
            f"-X {body.hex()}\n"
        ).encode()

    def _read(self) -> bytes | None:
        found = self._run(["find-generic-password", "-s", self.service, "-a", self.account, "-w"])
        if found.returncode == 0:
            # ``security`` prints the password with one newline appended;
            # ``_encode`` never ends in one, so removing exactly one is exact.
            # If that ever stops being true the read-back check in ``_put``
            # turns it into a loud failure rather than a corrupted record.
            raw = found.stdout.removesuffix(b"\n")
            # Parse it; do not sniff it. `security` hands a value back as hex
            # rather than raw once it holds bytes outside 0x20-0x7e (Jeff
            # 220731/220737), and our records never do — but "looks like hex"
            # cannot be the test, because a hex string is printable ASCII too
            # and so is every record we write. Whatever will not parse is not
            # ours, and that fails closed instead of being decoded on a guess.
            #
            # Deliberately parsed twice, here and in ``load``: the second one
            # costs nothing on a hundred bytes and this one buys an error that
            # says what happened instead of a bare JSONDecodeError on a
            # credential store.
            try:
                json.loads(raw)
            except ValueError as exc:
                # The type, not the exception's text. Same rule as in
                # ``_explain`` and for the same reason: a JSONDecodeError says
                # only where it gave up, but a UnicodeDecodeError names the
                # offending byte of the value, and this message travels out
                # through the claim's save-stage reason. The type is what there
                # is to diagnose by here anyway.
                raise KeychainUnavailable(
                    "the Keychain returned something this computer did not "
                    "write — it will not parse as a connection record, and it "
                    f"is not decoded on a guess ({type(exc).__name__})"
                ) from None
            return raw
        if found.returncode == _ITEM_NOT_FOUND:
            self._require_a_reachable_keychain()
            return None
        raise KeychainUnavailable(
            f"could not read the connection from the Keychain: {_explain(found)}"
        )

    def _put(self, body: bytes) -> None:
        stored = self._run(["-i"], stdin=self._write_command(body))
        if stored.returncode != 0:
            raise KeychainUnavailable(
                f"could not write the connection to the Keychain: {_explain(stored)}"
            )
        # `security -i` does not reliably carry an inner command's failure into
        # its own exit code (Boris 220754), so a zero here is not evidence. The
        # read-back below is the actual gate, and it is the only reason the
        # empty-password failure was caught rather than shipped.
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
    """Why the command failed: the exit code, and nothing read from stderr.

    Three versions of this got progressively narrower and the lesson is the
    reason it is now this blunt. It began by masking long hex runs; Jeff 220838
    put an ordinary plaintext sentinel through that and it came out whole. The
    repair was to forward only the OSStatus numbers — and Jeff 220856 put
    ``-123456`` inside a credential and got it back as ``security_status``.

    Each of those was an argument about which shapes of value the filter
    happens to catch, and the set of shapes a credential can take is not ours
    to enumerate. So stderr is not read at all. This is the shape Boris landed
    in the diagnostic at `e4b94e2d` — deliberately not the `f60c9c8e` one,
    which is the version with the bug.

    What that costs is real: a locked Keychain, a missing one and a denied one
    now look the same from here. The exit code still separates "no such item"
    (44) from the rest, which is the only distinction any caller branches on.
    Diagnosing the others means looking at the Keychain, not at our logs.
    """
    return f"exit_code={completed.returncode}"
