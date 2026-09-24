"""Where a claimed connection lives on this computer.

v0.4 §8 runs the first chain against a test store deliberately: the real store
brings OS keychain integration, which belongs to step two. This one is a plain
file so the whole chain can be exercised now, and it is the piece step two
replaces — nothing above it parses what it holds.

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


@dataclass(frozen=True)
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


class StaleConnection(Exception):
    """The connection an update was meant for is not the one on this computer.

    Either it was disconnected while the update was in flight, or a different
    one was claimed in the meantime. Both say the same thing to the caller —
    the credential in hand belongs to a connection this computer no longer
    holds — and both must leave the store exactly as it was.
    """


class SkeletonConnectionStore:
    """One connection per computer, in one file. Step two swaps this out."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Connection | None:
        """The saved connection, or None when this computer has none.

        An unreadable file is not "no connection": answering None would invite
        the caller to overwrite a credential it could not read.
        """
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        record = json.loads(raw)
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
        self._write(connection)
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
        self._write(refreshed)
        return refreshed

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

    def _write(self, connection: Connection) -> None:
        """Put the whole record on disk, or leave nothing new behind.

        Written whole, then renamed: a crash mid-write must not leave a
        half-parsed credential behind, because ``load`` treats an unreadable
        file as "cannot tell", not "absent".

        Shared by ``save`` and ``update`` so a refreshed credential gets the
        same guarantee as a first one — there is only one way a credential
        reaches this disk, and so only one place that has to be right.
        """
        body = json.dumps(
            {
                "reference": connection.reference,
                "request_ref": connection.request_ref,
                "provider": connection.provider,
                "credential": connection.credential,
            }
        ).encode()
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
