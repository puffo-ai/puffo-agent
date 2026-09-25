"""Where a claimed connection lives on this computer.

``ConnectionStore`` is the shape; there are two of them. On macOS the record
goes in the login Keychain (``keychain_store``), which is what v0.4 §8 means
by the real store. Everywhere else it goes in a private file, which is also
what the first chain was built and verified against.

Both inherit the two rules that would be expensive to get differently right
twice: a store that cannot be read is not an empty one, and a refresh may only
replace the connection it names.

The credential is stored verbatim. The daemon is not told which provider it is
holding (v0.4 §4: credential shape is agreed between the provider adapter and
the tool that uses it), so interpreting any field here would be inventing a
coupling the design does not have.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class Connection:
    """A saved connection, as this computer knows it.

    ``reference`` is the "本机连接引用" of v0.4 §4 — minted here, at save time,
    because that is the first moment a connection exists to refer to. Its form
    is left to the implementation by the design; a random id is enough, and
    carrying no meaning is the point: nothing downstream can parse an account
    or a provider out of it.
    """

    reference: str
    request_ref: str
    provider: str
    credential: Any

    def __repr__(self) -> str:
        """Everything except the credential.

        A dataclass prints every field it has, so one ``%s`` on a Connection
        anywhere — a log line, a pytest failure, a traceback that happens to
        carry the frame it lives in — writes a working Google refresh token
        into a file. Nothing does that today; every log line in this package
        passes ``.reference``. This is about the day somebody adds one, which
        is the same reason the server half was asked to stop deriving ``Debug``
        on its request type (Boris 221597).

        The other three fields stay readable. A repr that showed nothing would
        just get replaced at the call site by a print of the field somebody
        wanted, and none of those three is a secret.
        """
        return (
            f"Connection(reference={self.reference!r}, "
            f"request_ref={self.request_ref!r}, "
            f"provider={self.provider!r}, credential=<not shown>)"
        )


class StaleConnection(Exception):
    """The connection an update was meant for is not the one on this computer.

    Either it was disconnected while the update was in flight, or a different
    one was claimed in the meantime. Both say the same thing to the caller —
    the credential in hand belongs to a connection this computer no longer
    holds — and both must leave the store exactly as it was.
    """


class ConnectionStore:
    """What every connection store does, and the rules that do not depend on
    where the bytes end up.

    Subclasses supply three things — read the record, put the record there,
    remove it — and inherit the rest. The rules below are the ones it would be
    expensive to get differently right twice: that an unreadable store is not
    an empty one, and that a refresh may only land on the connection it names.
    """

    def load(self) -> Connection | None:
        """The saved connection, or None when this computer has none.

        A store that cannot be read is not "no connection": answering None
        would invite the caller to overwrite a credential it could not read.
        Subclasses say "absent" by returning None from ``_read`` and say
        "cannot tell" by raising — they must never confuse the two.
        """
        raw = self._read()
        if raw is None:
            return None
        record = json.loads(raw)
        _check(record)
        return Connection(
            reference=record["reference"],
            request_ref=record["request_ref"],
            provider=record["provider"],
            credential=record["credential"],
        )

    def save(self, *, request_ref: str, provider: str, credential: Any) -> Connection:
        """Write a new connection and return it, reference included."""
        connection = Connection(
            reference=uuid.uuid4().hex,
            request_ref=request_ref,
            provider=provider,
            credential=credential,
        )
        self._put(_encode(connection))
        return connection

    def update(self, *, reference: str, credential: Any) -> Connection:
        """Replace the credential of the connection this computer holds.

        This is the write path a refresh takes. It cannot go through ``save``:
        a claim refuses outright once a connection exists, which is the right
        answer for a second authorization and the wrong one for a fresher
        credential for the connection already here.

        Keyed on ``reference``, and refusing anything else. A refresh that
        finishes late must not land on a connection minted after it started —
        disconnect, re-authorize, then the old refresh arrives — because that
        would silently replace one connection's credential with another's, the
        very thing the claim refuses to decide (Jeff 218183 / 219714). It
        arrives by a different door, so it needs its own lock on the way in.

        The reference is kept rather than re-minted: this is the same
        connection with a fresher credential, and the page is already holding
        that reference.
        """
        current = self.load()
        if current is None:
            raise StaleConnection(f"this computer holds no connection {reference}")
        if current.reference != reference:
            raise StaleConnection(
                f"this computer holds connection {current.reference}, not {reference}"
            )
        refreshed = Connection(
            reference=current.reference,
            request_ref=current.request_ref,
            provider=current.provider,
            credential=credential,
        )
        self._put(_encode(refreshed))
        return refreshed

    def clear(self) -> None:
        """Remove this computer's connection."""
        self._erase()

    def _read(self) -> bytes | None:
        """The stored record, or None when there is none. Raises when the
        store cannot be read — which is not the same answer."""
        raise NotImplementedError

    def _put(self, body: bytes) -> None:
        """Make ``body`` the stored record.

        On failure, what this computer holds is whatever the next ``_read``
        says. This used to promise "or leave the store unchanged", which the
        file store below does keep — it writes a temporary file and renames —
        and which the Keychain store cannot: ``security -i`` does not reliably
        carry an inner failure into its own exit code (Boris 220754), so a
        reported failure is not evidence the item is untouched, and a read-back
        that disagrees proves only that the new value is not in effect. The
        promise was written once for both and was false for one of them
        (Jeff 221356).
        """
        raise NotImplementedError

    def _erase(self) -> None:
        """Remove the stored record, leaving no readable copy behind."""
        raise NotImplementedError


# The record's own fields, as opposed to the credential inside it. The daemon
# does know these three — it mints ``reference`` itself and carries the other
# two in from the claim — which is exactly why it is entitled to insist on
# them. ``credential`` is the one field whose shape it is not told (v0.4 §4).
_NAMED_FIELDS = ("reference", "request_ref", "provider")


def _check(record: Any) -> None:
    """Refuse a record this daemon could not have written.

    ``json.loads`` answers "is this JSON", which is less than "is this a
    connection". Without this, a record whose ``reference`` is null loads as a
    Connection and the claim answers ``connected: True`` with a null
    reference — a computer reporting it is connected to nothing, and a page
    that would show 已连接 for it (Jeff 220838, reproduced before fixing).

    Every refusal is a ``ValueError``, which is what ``json.loads`` already
    raises for malformed JSON and what the claim already reads as "local store
    unreadable". That matters for the non-object cases — ``[]``, ``123``,
    ``null`` — which previously came out as ``TypeError`` from the subscript
    below and escaped the claim's handling entirely, because ``TypeError`` is
    deliberately excluded from it as this module's own bug. They are not our
    bug; they are an unreadable store, and they now say so.

    Nothing here looks inside ``credential``: that it is present is a fact
    about the record, while anything further would be inventing the coupling
    v0.4 §4 declines to create.
    """
    if not isinstance(record, dict):
        raise ValueError(
            f"the stored record is a JSON {type(record).__name__}, not an object"
        )
    for field in _NAMED_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"the stored record's {field} is not a non-empty string")
    if record.get("credential") is None:
        raise ValueError("the stored record carries no credential")


def _encode(connection: Connection) -> bytes:
    """The record as stored. One line of ASCII JSON, no trailing newline —
    ``json.dumps`` escapes non-ASCII by default, which keeps the record
    printable through stores that hand it back through a pipe.

    Checked on the way out under the same rule as on the way in. A store that
    can write a record it would then refuse to read is one bad argument away
    from a connection that cannot be loaded or replaced, only cleared — and
    adding the read check without this one is what would have created that.
    """
    record = {
        "reference": connection.reference,
        "request_ref": connection.request_ref,
        "provider": connection.provider,
        "credential": connection.credential,
    }
    _check(record)
    return json.dumps(record).encode()


class SkeletonConnectionStore(ConnectionStore):
    """One connection per computer, in one file.

    The non-macOS backend, and the one the first chain was built against. It
    is not what step two replaces after all: a computer without a Keychain
    still has to keep a connection somewhere, so this stays as that computer's
    store while ``KeychainConnectionStore`` takes over on macOS.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def clear(self) -> None:
        """Remove this computer's connection, temporary file included.

        The temporary file holds the same credential in the clear, so clearing
        only the finished one would answer "disconnected" while a usable
        credential stayed on disk — a silent breach of the promise that a
        disconnect clears locally first (Jeremy 217298 / Jeff 217299).

        The finished file goes first, so a failure removing the leftover still
        leaves the connection unusable rather than half-live.
        """
        self.path.unlink(missing_ok=True)
        self.path.with_suffix(".partial").unlink(missing_ok=True)

    def _read(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def _erase(self) -> None:
        self.clear()

    def _put(self, body: bytes) -> None:
        """Put the whole record on disk, or leave nothing new behind.

        Written whole, then renamed: a crash mid-write must not leave a
        half-parsed credential behind, because ``load`` treats an unreadable
        file as "cannot tell", not "absent".

        Shared by ``save`` and ``update`` so a refreshed credential gets the
        same guarantee as a first one — there is only one way a credential
        reaches this disk, and so only one place that has to be right.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".partial")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            try:
                os.write(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, self.path)
        except BaseException:
            # From here the temporary file holds the whole credential in the
            # clear, and nothing else ever removes it: the next successful save
            # renames over it, and a clear that missed it would leave a usable
            # credential on a computer whose user just disconnected. A failed
            # save must not leave that copy behind (Boris 219863, Jeff 219864).
            #
            # BaseException, not Exception: a cancellation or an interrupt
            # arriving between the write and the rename leaves exactly the same
            # file, and re-raises just the same.
            _discard(temporary)
            raise


def _discard(path: Path) -> None:
    """Remove ``path``, keeping whatever error brought us here.

    The save error says why the connection did not happen; a cleanup error says
    only that the cleanup also failed. Letting the second replace the first
    would report the wrong thing, so this swallows its own failure — and logs
    it, because a credential then really is left on disk and that has to be
    observable somewhere (Jeff 219877).
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.error(
            "connector: could not remove %s after a failed save; "
            "a credential may remain on disk: %s",
            path,
            exc,
        )
