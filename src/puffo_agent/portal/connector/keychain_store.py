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
  a daemon reporting connected with nothing stored, and it is still the gate
  even now that the write goes through ``security -i`` with the value as hex
  through ``-X`` (Jeff 220731/220737/220759, and 220838 for a full round trip
  against a real Keychain).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from ..._proc import no_window_kwargs
from .store import ConnectionStore, _check

logger = logging.getLogger(__name__)

# Items this process has already tried to move out of the file store, whether
# or not it worked. A refused move must not be retried on every read: each
# retry is another ``security`` call, and any prompt it raises lands on the
# operator's screen where the caller cannot see it (Boris 221326).
_MOVE_ATTEMPTED: set[tuple[str, str]] = set()

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


class LocalCopiesDisagree(OSError):
    """The two places this computer keeps a connection do not agree.

    An ``OSError`` for the same reason ``KeychainUnavailable`` is one: every
    caller above already reads that as "cannot tell", which is exactly what
    this is. Nothing is deleted and nothing is answered with — both halves,
    because both rest on knowing which copy is current and neither subclass
    below can be told that from here.

    Two subclasses rather than one type with two messages: an operator has to
    be able to tell this apart from an ordinary flaky read, and the claim
    flattens everything into one ``read_local`` stage. A stable, distinguishable
    error type is what that needs (Jeff 221379); the references in the message
    help and are not the mechanism.
    """


class ConflictingConnections(LocalCopiesDisagree):
    """The two records name different connections.

    The references are random hex minted at save time — they carry no order, so
    nothing here can say which record is the current one, and guessing costs a
    live credential either way (Boris 221354, reproduced by Jeff 221356;
    測試姬 221362 for why answering with one of them is the same mistake as
    deleting the other).
    """


class ConflictingCredentials(LocalCopiesDisagree):
    """One connection, and the two places hold different credentials for it.

    Refusing both halves, which took three rounds to get to. Deleting needs
    "the file holds nothing the Keychain does not" — the neighbouring
    proposition to "same reference", and not one this daemon can evaluate,
    because it does not look inside a credential (v0.4 §4). Jeff 221374
    measured the version that deleted: the Keychain holding an older credential
    and the file a newer one, the newer one gone.

    Answering is refused too, and that is the part I had wrong after fixing the
    delete. "Worst case, one stale read" assumes the credential handed back can
    still be refreshed, which is exactly the thing that cannot be established
    from here — the same unsupported step, one paragraph further down
    (Jeff 221379; 測試姬 221381 withdrew the return-side exemption she had
    proposed in 221378).
    """


class SupersededCopyRemains(OSError):
    """The Keychain took the value; the older copy could not be removed.

    Raised only after the write has been read back, which makes it the one
    failure out of ``_put`` that means the new credential IS what this computer
    holds. Named so the migration can tell it apart from a write that never
    landed — those two need opposite things said about them, and saying the
    wrong one is how "will not be retried until this process restarts" ended up
    logged for a move that had in fact succeeded (Jeff 221356).
    """


class KeychainConnectionStore(ConnectionStore):
    """One connection per machine identity, in the login Keychain.

    Two places to hold it, so every write and every clear has two legs, and a
    failure in either reaches the caller as one exception. What that exception
    means differs per cell, and guessing wrong is how a caller turns a
    committed write into a retry, or a connection that still works into
    "disconnected". Stated per cell rather than in prose, because prose is what
    was wrong here before (測試姬 221299's oracle, Jeff 221284's injections):

    ``save`` / ``update`` — Keychain first, the older file second:

    - the Keychain write fails      → not confirmed, nothing erased. Note the
                                      wording: ``security -i`` does not
                                      reliably carry an inner failure into its
                                      exit code (Boris 220754), so a reported
                                      failure is not evidence the item is
                                      unchanged. What the item holds is
                                      whatever the next ``load`` says.
    - the read-back disagrees       → the new value is **not** in effect; that
                                      much the read-back proves. It does not
                                      prove nothing was written, so this is
                                      "not confirmed", not "not committed"
                                      (Jeff 221335 — the cell this matrix had
                                      wrong). Nothing is erased, and the item
                                      is left alone on purpose: it may still
                                      hold the credential in use.
    - sweeping the older file fails → **committed**. ``load`` returns the new
                                      credential. What failed is the removal of
                                      the older copy, which is still readable
                                      on disk — that is what the exception is
                                      about, and a retry is not free.

    ``clear`` — both legs attempted, the first failure raised afterwards:

    - either leg fails              → the other leg still ran. Whatever
                                      survived is still loadable, so the
                                      connection may still work, and a
                                      surviving file gets migrated back into
                                      the Keychain by the next read. Neither
                                      "unusable" nor "nothing changed": the
                                      disconnect is unfinished, and the honest
                                      thing to tell the user is to retry it.

    ``load`` never raises for a cleanup problem: a migration that could not
    finish still answers with the connection it read.

    ``load`` — when the Keychain is empty and the older file is not, reading
    also moves it, which makes a read a write:

    - the move is refused         → answered with the file's connection, and
                                    not attempted again until this process
                                    restarts.
    - the write landed, the sweep → answered with the connection. It is in the
      did not                       Keychain; the next read sweeps the file.
    - something arrived first     → the re-read finds it and it is returned
                                    instead of being written over. That is a
                                    narrower window, not a closed one: a writer
                                    landing between the re-read and the write
                                    still overwrites (Jeff 221356 injected
                                    exactly that). What closes it is the
                                    caller's lock, below.
    - the record is not one       → not moved; ``load`` fails closed on it.

    ``load`` — when the Keychain holds a value, the older file is superseded
    and swept. "Holds a value" is not "holds the connection", and every cell
    below was one branch before Jeff 221356 and 測試姬 221362 took it apart:

    - the value is not a record   → nothing is swept. ``load`` fails closed on
                                    it a moment later, and the older copy has
                                    to still be there when it does — ``null``,
                                    ``{}`` and ``[]`` all parse, and all three
                                    used to take the file with them before
                                    anything checked them.
    - byte-identical both places  → one copy twice, and the file goes.
    - same reference, other bytes → ``ConflictingCredentials``. Neither copy
                                    deleted, neither answered with. Deleting
                                    needs "the file holds nothing newer" and
                                    answering needs "this one still works",
                                    and this daemon can establish neither.
    - different references        → refused, and nothing is deleted. Two
                                    records naming two connections, with no
                                    order between them.
    - the file cannot be read     → refused, and nothing is deleted. Same rule
                                    as everywhere else here: cannot tell is not
                                    absent. The cost is real and deliberate —
                                    an unreadable file takes down a read the
                                    Keychain could have answered.

    **What serialises any of this is the caller, not this class.** There is no
    lock here. ``connector.claim`` holds one asyncio lock across load, save,
    update and clear, which is what makes "the Keychain is empty" still true a
    moment later when the move writes; the re-check above narrows that window
    but two adjacent lookups are not a compare-and-set. A caller that reads
    outside that lock while another writes inside it gets no guarantee from
    here. Today every caller of ``load`` is inside it (``claim.py``), and
    ``update`` has no production caller at all.
    """

    def __init__(
        self,
        account: str,
        *,
        service: str = SERVICE,
        timeout: float = TIMEOUT_SECONDS,
        superseded: Path | None = None,
    ) -> None:
        self.account = account
        self.service = service
        self.timeout = timeout
        # The file store this one takes over from. A computer that connected
        # before the switch has a credential in that file, and a Keychain store
        # that only ever looked in the Keychain would answer "not connected"
        # for it and clear nothing on a disconnect — reporting the credential
        # gone while it stayed readable on disk. That is the breach 4bf86f72
        # closed for the temporary file, arriving by the other door.
        self.superseded = superseded

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
        """The record this computer holds, in either place it could be."""
        from_keychain = self._read_from_keychain()
        if from_keychain is not None:
            # An older copy alongside a Keychain entry is usually superseded,
            # and it is swept here rather than only when a write happens.
            # Without this the cleanup never converges: a move whose write
            # landed but whose unlink failed leaves the entry in place, so every
            # later read — including after a restart, because the entry outlives
            # the process — short-circuits right here and never reaches the
            # migration again. The plaintext would then sit there until some
            # explicit write or clear happened to come along (Jeff 221335, and
            # 221343 for the restart half; 測試姬 221338 withdrew "bounded
            # window" on the strength of it).
            #
            # "Usually" is the whole of the gate below, and returning the value
            # is behind the same gate as deleting the file: both assume the
            # Keychain holds *the* connection, and both are wrong in the same
            # cases (測試姬 221362).
            self._sweep_what_the_keychain_supersedes(from_keychain)
            return from_keychain
        # Nothing in the Keychain is not yet "nothing on this computer".
        older = self._read_superseded()
        if older is None:
            return None
        return self._migrate(older)

    def _sweep_what_the_keychain_supersedes(self, from_keychain: bytes) -> None:
        """Remove the older copy, once it is provably a copy of the same thing.

        One gate in front of two decisions — delete the file, and hand
        ``from_keychain`` back as the connection — because they rest on the
        same assumption and it fails in the same places. Checking only the
        delete side leaves a stale value being returned silently, and an
        invalid one at least gets refused downstream while a *valid* record for
        a different connection does not (測試姬 221362).

        What "provably" rules out, each one measured on this code:

        - **The Keychain value is not a connection.** ``null``, ``{}`` and
          ``[]`` are all valid JSON, so the Keychain leg's parse says yes to
          every one of them; ``load`` refuses them a moment later, but the
          sweep used to have already run. Delete-then-validate is backwards,
          and it cost the only readable copy of a live credential in all three
          cases (Jeff 221356, reproduced).
        - **The two records name different connections.** The rollback path is
          real: turn this store off, the file store reports not connected, the
          user reconnects into the file, turn it back on — now the Keychain's
          record is the one the provider may already have revoked, and it was
          deleting the live one on the first read (Boris 221354, reproduced by
          Jeff 221356 by injection).
        - **The file cannot be read.** Then there is no reference to compare,
          and "cannot tell" has never meant "absent" anywhere else in this
          file.

        **The two decisions need different propositions, and one of them is
        not available.** This started as one gate for both, which was an
        improvement on two ungated branches and still not right (測試姬
        221378, after Jeff 221374 measured the case):

        - *Deleting* the file needs "the file holds nothing the Keychain does
          not". Matching references do **not** give that — it is the
          neighbouring proposition, and the daemon cannot establish the real
          one, because it does not look inside a credential (v0.4 §4). Jeff
          221374 measured the version that deleted anyway: the Keychain
          holding an older credential and the file a newer one under one
          reference, the newer one gone.
        - *Answering* with the Keychain copy looks weaker, and is not. "Worst
          case, one stale read of a connection whose identity is not in doubt"
          assumes the credential handed back can still be refreshed — the same
          step that cannot be taken from here, one paragraph further down. I
          wrote it anyway while fixing the delete (Jeff 221379; 測試姬 221381
          withdrew the return-side exemption she had proposed).

        So only byte-identical copies are swept: one copy twice, and the file
        goes. Anything else refuses both halves and leaves both copies
        (``LocalCopiesDisagree``).

        What refusing costs, since it is not free either. A ``save`` or
        ``update`` whose Keychain write landed and whose sweep did **not**
        leaves two copies of one connection with different credentials — and
        from the next read on, this computer refuses to load at all until
        somebody disconnects. A cleanup failure becomes an outage. That path is
        narrow (the Keychain write verified, the ``unlink`` failed) and it is
        already reported at the time it happens, by ``SupersededCopyRemains``;
        it is called out here because nobody else has, and it belongs in
        real-machine acceptance rather than in a docstring's confidence.

        The trade taken: never delete a credential that may be the only usable
        one, and never answer with one that may not be. A file left behind is
        visible to the acceptance scan (測試姬 219875) and recoverable; a
        deleted credential is neither, and a silently dead one is worse than a
        refusal. The migration path still converges across restarts, which is
        what Jeff 221335/221343 asked for, because there the two copies are
        byte-identical by construction.

        Rolling this store back with a live connection remains unsupported:
        disconnect first. That is an instruction to whoever does it, not a
        precondition this code reads as permission to delete (Boris 221354
        offered exactly such a precondition and it was declined).
        """
        held = _reference_of(from_keychain)
        if held is None:
            # Nothing is superseded by a value that is not a connection. The
            # caller gets it anyway and ``load`` fails closed on it there.
            return
        older = self._read_superseded()
        if older is None:
            # No record to compare, and the ``.partial`` is never one: nothing
            # reads it, so it cannot be the connection this computer holds. It
            # is residue holding a credential in the clear, and it goes — the
            # sweep has to converge here too, or a clear that removed the
            # record but not the leftover leaves the leftover for good.
            self._erase_partial_quietly()
            return
        if _reference_of(older) != held:
            # Logged as well as raised: the claim maps this to the same
            # ``read_local`` stage as a transient read failure, so on its own
            # it reads as "try again" rather than "this machine is wedged and
            # needs a disconnect" (測試姬 221378). The references are random
            # hex minted here and are not credentials, so naming them is safe
            # and is the only thing that makes the state diagnosable.
            logger.error(
                "connector: this computer holds two records naming different "
                "connections — the Keychain has %s and the file store has %s. "
                "Nothing was deleted and neither is answered with. A "
                "disconnect clears both and is the way out",
                held,
                _reference_of(older),
            )
            raise ConflictingConnections(
                "this computer holds two records naming different connections: "
                f"the Keychain has {held}, the file store has "
                f"{_reference_of(older)}. Nothing was deleted and neither is "
                "answered with, because their references carry no order"
            )
        if older != from_keychain:
            logger.error(
                "connector: the Keychain and the file store hold different "
                "credentials for connection %s, and nothing here can tell "
                "which one is current. Neither was deleted and neither is "
                "answered with. A disconnect clears both and is the way out",
                held,
            )
            raise ConflictingCredentials(
                f"this computer holds two credentials for connection {held}, "
                "one in the Keychain and one in the file store. Neither was "
                "deleted and neither is answered with, because which of them "
                "the provider still honours cannot be known from here"
            )
        self._erase_quietly()

    def _read_from_keychain(self) -> bytes | None:
        """The Keychain leg on its own, which is what ``_put`` verifies against.

        Kept separate so the read-back gate cannot be satisfied by the file the
        write is about to delete: the question there is whether the *Keychain*
        took the value, and a fallback would answer a different one.
        """
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

    def _read_superseded(self) -> bytes | None:
        """The record left in the file store, for a computer that predates this.

        Returned rather than refused. Raising here would take a working
        connection away from every computer that had one the moment the switch
        landed, and the connection is genuinely still there — it is only in the
        older place. Returning it keeps the claim's refusal to replace an
        existing connection working, which is the check that would otherwise be
        bypassed: answering None lets a second authorization save into the
        Keychain while the first credential stays on disk, still live at the
        provider.

        Whatever the file system raises comes out, because an unreadable file
        here is "cannot tell", not "not connected" — the same rule as the
        Keychain leg above.
        """
        if self.superseded is None:
            return None
        try:
            return self.superseded.read_bytes()
        except FileNotFoundError:
            return None

    def _migrate(self, body: bytes) -> bytes:
        """Move the older copy into the Keychain, and answer with what to use.

        A write inside a read is not free, and it is here on purpose. Sweeping
        only on write or clear leaves an already-connected computer holding its
        credential in plaintext on disk for as long as nothing happens to write
        — and nothing has to: a refresh is reactive, so a credential that does
        not expire on a computer nobody sends mail from is never rewritten
        (Boris 221279, who found the gap by reading the shape of the code).
        "Cleaned up whenever a write next occurs" is not a property; the point
        of moving to the Keychain is that the plaintext stops existing.

        Best effort, and bounded so it can only improve matters:

        - A record that is not one is left alone. ``load`` fails closed on it,
          which is the right answer and not something to copy over first.
        - **It re-reads the Keychain immediately before writing, and whatever
          is already there wins.** Without that, a refresh committing between
          "the Keychain is empty" and this write would be overwritten by the
          older file value, the read-back would pass because it reads back what
          it just wrote, the sweep would delete the file, and a rotated
          credential would be silently dead (Boris 221326).

          Not "never overwrites", which is what this bullet used to lead with:
          Jeff 221356 put a writer between the re-read and the write and the
          older value landed on top of it just the same. Two adjacent lookups
          are not a compare-and-set. The re-read narrows the window; the
          caller's lock is what closes it, and an uncoordinated writer gets no
          guarantee from here — see the class docstring.
        - One attempt per process per item. A Keychain that refuses the write
          leaves the file, and retrying on *every* read would mean a fresh
          ``security`` call — and any prompt it raises on the operator's
          screen — each time anything asks whether this computer is connected
          (Boris 221326, and 220858 for why a prompt is invisible from here).
        - A failure still answers with the connection. A failed move must not
          cost a computer a connection it had.
        """
        try:
            _check(json.loads(body))
        except ValueError:
            return body

        newer = self._read_from_keychain()
        if newer is not None:
            # Something committed while the file was being read. Through the
            # same gate as the short-circuit in ``_read``: this is the second
            # of the two places that decided "the Keychain has a value" meant
            # "the file is superseded", and it was wrong here for exactly the
            # reasons it was wrong there (測試姬 221362).
            self._sweep_what_the_keychain_supersedes(newer)
            return newer

        key = (self.service, self.account)
        if key in _MOVE_ATTEMPTED:
            return body
        _MOVE_ATTEMPTED.add(key)

        try:
            self._put(body)
        except SupersededCopyRemains:
            # The write landed and was read back; only the unlink failed. The
            # move is done, and saying "will not be retried until this process
            # restarts" here — which this branch used to, because it was one
            # branch — describes a retry that is neither needed nor what
            # happens: the next read finds the value in the Keychain and sweeps
            # the file from there (Jeff 221356).
            logger.warning(
                "connector: the connection is now in the Keychain, but the "
                "superseded copy could not be removed from the file store; "
                "a later read sweeps it"
            )
            return body
        except (KeychainUnavailable, OSError):
            # Deliberately not re-raised and deliberately without the record:
            # the caller asked to read, and it is about to get a usable answer.
            #
            # OSError as well as KeychainUnavailable because the sweep is part
            # of ``_put``, and a file that will not unlink must not turn a
            # working read into a read failure. Note the asymmetry, which is on
            # purpose: the same sweep failing under an explicit ``save`` or
            # ``update`` does raise, because there the caller asked to write and
            # is owed the news.
            logger.warning(
                "connector: could not move the connection into the Keychain; "
                "it stays in the file store and will not be retried until "
                "this process restarts"
            )
            return body
        logger.info("connector: moved the connection from the file store into the Keychain")
        return body

    def _erase_quietly(self) -> None:
        """Sweep the older copy without letting a read fail over it."""
        try:
            self._erase_superseded()
        except OSError:
            logger.warning(
                "connector: a superseded copy of the connection could not be "
                "removed from the file store"
            )

    def _erase_partial_quietly(self) -> None:
        """Sweep only the leftover, for when there is no record to compare to."""
        if self.superseded is None:
            return
        try:
            self.superseded.with_suffix(".partial").unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "connector: a leftover from an interrupted save could not be "
                "removed from the file store"
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
        if self._read_from_keychain() != body:
            # Not erased. If the write simply did not take, the item still
            # holds the credential this computer was using, and deleting it
            # would turn a failed refresh into a lost connection. If something
            # unreadable did land, the next load fails to parse and the claim
            # refuses — closed either way, and visible.
            raise KeychainUnavailable(
                "the Keychain did not store what was written; the new value is "
                "not in effect, and what the item holds instead is not known "
                "from here"
            )
        # The Keychain now holds this record, verified, so the older copy is a
        # second readable copy of a credential and goes. Only in this order:
        # the file is the fallback ``_read`` relies on until the Keychain
        # actually has the value.
        #
        # This can only remove a file the caller already accounted for. A save
        # runs only after ``load`` answered None, and ``load`` reads the file
        # when the Keychain is empty — so a file holding a connection would
        # have made the claim refuse instead of arriving here, and a file that
        # could not be read would have made it fail closed.
        #
        # If this raises, the write above has already happened and been read
        # back: the new credential IS the one this computer holds, and the next
        # ``load`` returns it. The exception means the older copy could not be
        # removed, not that the write did not land, so a caller must not treat
        # it as "not committed" and must not assume a retry is free (Jeff
        # 221284). Raising is still right — the alternative is a stale
        # credential left in the clear with nobody told.
        try:
            self._erase_superseded()
        except OSError as exc:
            raise SupersededCopyRemains(
                "the Keychain holds the new connection, but the superseded "
                f"copy could not be removed from the file store: {exc}"
            ) from exc

    def _erase(self) -> None:
        """Remove the connection from both places it could be.

        Both legs are attempted even when the first one fails, and the failure
        is raised afterwards. Stopping at the first error would leave a copy
        this computer could have deleted, which is the wrong trade for a
        credential: a disconnect should take everything it can reach and then
        say it did not finish.

        What a failed disconnect leaves behind, stated plainly because this
        docstring used to claim the opposite: the connection may still be
        usable. If a copy survives, ``load`` finds it and answers with it —
        and, since reading the older file also migrates it, a clear that
        removed the Keychain item but not the file will put it back in the
        Keychain on the next read. There is no rollback here and no
        transaction; the caller learns only that the disconnect did not
        finish, and must not read that as "nothing changed" (Jeff 221284,
        by fault injection).
        """
        failure: Exception | None = None
        try:
            self._erase_from_keychain()
        except Exception as exc:  # noqa: BLE001 - re-raised below, after the rest
            failure = exc
        try:
            self._erase_superseded()
        except Exception as exc:  # noqa: BLE001 - same
            failure = failure or exc
        if failure is not None:
            raise failure

    def _erase_superseded(self) -> None:
        """Take the file store's copy too, temporary file included.

        Clearing only the Keychain would answer "disconnected" while a usable
        credential stayed readable on disk. The ``.partial`` goes for the same
        reason it does in the file store: a crash mid-save leaves one holding
        the same credential in the clear.
        """
        if self.superseded is None:
            return
        self.superseded.unlink(missing_ok=True)
        self.superseded.with_suffix(".partial").unlink(missing_ok=True)

    def _erase_from_keychain(self) -> None:
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


def _reference_of(body: bytes) -> str | None:
    """The connection reference in ``body``, or None when it is not a record.

    Parsed and then run past the same ``_check`` ``load`` uses, rather than
    subscripted: the question these comparisons ask is "is this a connection
    this daemon wrote", and it has to get the same answer ``load`` will give a
    moment later. A looser test here would sweep against a record ``load`` is
    about to refuse, which is the shape of the defect this exists to close.
    """
    try:
        record = json.loads(body)
        _check(record)
    except ValueError:
        return None
    return record["reference"]


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
