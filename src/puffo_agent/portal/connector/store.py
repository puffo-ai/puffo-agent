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
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
        """Write the connection and return it, reference included.

        Written whole, then renamed: a crash mid-write must not leave a
        half-parsed credential behind, because the reader above treats an
        unreadable file as "cannot tell", not "absent".
        """
        connection = Connection(
            reference=uuid.uuid4().hex,
            request_ref=request_ref,
            provider=provider,
            credential=credential,
        )
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
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.path)
        return connection

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
