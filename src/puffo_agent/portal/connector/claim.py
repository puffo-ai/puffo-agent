"""Claim a prepared credential for this computer and report the result.

The notification carries only a request reference (v0.4 §3); the credential
travels server → daemon over the claim call, never through the UI. One round
trip does both jobs: whatever this returns becomes the computer command's
result, which is what the page reads to decide "已连接" — so "领取及保存结果"
and "命令执行结果" are the same dict, not two channels.

"Server says claimable" and "this computer is connected" stay separate
(v0.4 §3): only a completed local save produces a connection reference, and
only that reference means connected.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Stages a claim can end at. Named so a log line answers "which segment"
# (v0.4 §7) without a fixed reason vocabulary above it — the design explicitly
# declines to mandate the old enum, so these say where, not a closed what.
STAGE_READ_LOCAL = "read_local"
STAGE_ALREADY_CONNECTED = "already_connected"
STAGE_FETCH = "fetch"
STAGE_SAVE = "save"

# Reading the local store can fail in these ways: absent field, bad JSON, or
# the file system. TypeError is deliberately absent — that would be this
# module's own bug, and swallowing it would hide it as a claim failure.
_READ_ERRORS = (OSError, ValueError, KeyError)


class ClaimFailed(Exception):
    """The server could not hand over a credential for this request.

    ``reason`` is what reaches the caller. Nothing branches on which refusal it
    was: the claim is serialised, so by the time the server can say "already
    claimed", the local check above has already answered from disk.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


Fetch = Callable[[str], Awaitable[tuple[str, Any]]]

# One claim at a time in this process. `connector.claim` is a background op, so
# two commands really do run concurrently, and read-then-save is not atomic:
# without this, two different requests both read "nothing here", both fetch,
# and the second save overwrites the first — defeating the refusal below and
# handing the first caller a connection reference that no longer exists
# (Boris 219713, reproduced before fixing).
#
# Keyed by running loop rather than a single module-level Lock: an asyncio.Lock
# binds to the loop that first awaits it and refuses any other, which would
# break every test after the first. A daemon has one loop, so this is one lock
# there; the weak keys let finished test loops go.
_CLAIM_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _claim_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _CLAIM_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _CLAIM_LOCKS[loop] = lock
    return lock


async def claim_connection(request_ref: str, *, fetch: Fetch, store: Any) -> dict:
    """Fetch, save, and report. What this returns is the command result.

    Serialised against other claims on this computer. Not a caller's choice:
    the guarantee below — that an existing connection is never silently
    replaced — is only true if the read and the save cannot interleave.

    ``fetch`` is the whole of the server contract, kept to one call so the real
    HTTP shape can land without touching anything else here.

    Every path returns a dict. A claim that ends badly is still an answer: the
    page has to be able to stop waiting (v0.4 §7), so raising out of here would
    move the problem rather than report it.
    """
    async with _claim_lock():
        return await _claim_while_locked(request_ref, fetch=fetch, store=store)


async def _claim_while_locked(request_ref: str, *, fetch: Fetch, store: Any) -> dict:
    try:
        existing = store.load()
    except _READ_ERRORS as exc:
        # Not the same as "no connection": that answer would invite the save
        # below to overwrite a credential this computer could not read.
        return _failure(
            request_ref, STAGE_READ_LOCAL, f"local store unreadable: {type(exc).__name__}"
        )

    if existing is not None:
        # Re-notifying the same request is not a conflict: the connection being
        # asked for is already here, so report it and stay idempotent.
        if existing.request_ref == request_ref:
            logger.info(
                "connector: claim %s already saved as connection %s",
                request_ref, existing.reference,
            )
            return {"ok": True, "connected": True, "connection_ref": existing.reference}
        # Replacing it would decide a product question nobody has answered:
        # v0.4 fixes neither the number of accounts nor what re-authorizing
        # does to an existing connection, and overwriting would answer both by
        # accident (Jeff 218183). Refusing changes nothing on disk.
        return _failure(
            request_ref,
            STAGE_ALREADY_CONNECTED,
            "this computer already holds a connection for another request; "
            "whether re-authorizing replaces it is undecided, so nothing changed",
        )

    try:
        provider, credential = await fetch(request_ref)
    except ClaimFailed as exc:
        return _failure(request_ref, STAGE_FETCH, exc.reason)

    try:
        connection = store.save(
            request_ref=request_ref, provider=provider, credential=credential
        )
    except OSError as exc:
        # The credential reached this computer but did not survive to disk, so
        # the page must not show connected (v0.4 §7, local-save row).
        return _failure(request_ref, STAGE_SAVE, f"{type(exc).__name__}: {exc}")

    logger.info(
        "connector: claim %s saved as connection %s", request_ref, connection.reference
    )
    return {"ok": True, "connected": True, "connection_ref": connection.reference}


def _failure(request_ref: str, stage: str, reason: str) -> dict:
    """Log where and why, keyed by the reference tying the two ends together."""
    logger.error("connector: claim %s failed at %s: %s", request_ref, stage, reason)
    return {"ok": False, "connected": False, "stage": stage, "reason": reason}


async def replace_credential(reference: str, credential: Any, *, store: Any) -> Any:
    """Write a refreshed credential onto the connection already on this computer.

    Shares the claim lock rather than taking one of its own. ``store.update``
    reads, compares and writes, which is no more atomic than the claim's
    read-then-save above; two separate locks would serialise each entry point
    against itself and leave the pair free to interleave — the same hole
    Boris 219713 found between two claims, one door further along.

    Raises ``StaleConnection`` (from the store) when the named connection is
    not the one here. Nothing is written in that case and the caller's only
    sound move is to drop the credential it holds: it belongs to a connection
    this computer no longer has.

    Unlike ``claim_connection`` this raises rather than returning a result
    dict. There is no page waiting on it — a refresh is the daemon's own
    errand, not an answer to a notification — so the caller is code that can
    handle a failure, not a UI that must stop spinning.
    """
    async with _claim_lock():
        return store.update(reference=reference, credential=credential)
