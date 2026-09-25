"""The ``connector.*`` computer commands: wiring, not policy.

Machine-level, like ``refresh_usage`` — a connection is about this computer,
not about one agent. No command carries which computer it is about: that
travels in the machine signature the server verifies
(``machine_auth.signed_headers``), so the server reads the caller from a
verified header rather than from a field the caller filled in.

Three ops. The claim route is interface v1 (Bob 219652) and has run. The
refresh route is agreed in full (Bob 221549) and has not: a handler exists on
the other side now (#406) and these two ends have never spoken.
The disconnect is local only and reaches no server at all — the remote half of
a disconnect is missing rather than deferred.
"""

from __future__ import annotations

import json
import logging
import platform
from pathlib import Path
from urllib.parse import quote
from typing import Any

from ...crypto.http_session import create_remote_http_session
from ..control import machine_auth
from ..control.store import MachineControlIdentity, load_or_create_machine
from ..host_assets import _ensure_private_directory
from ..state import home_dir
from .claim import (
    STAGE_CLEAR,
    STAGE_EXCHANGE,
    STAGE_READ_LOCAL,
    STAGE_SAVE,
    STAGE_STALE,
    ClaimFailed,
    claim_connection,
    disconnect,
    refresh_connection,
)
from .keychain_store import KeychainConnectionStore
from .store import ConnectionStore, SkeletonConnectionStore, StaleConnection

logger = logging.getLogger(__name__)

# Server contract: claim interface v1 (Bob 219652) §2.
#
# The request carries NO body. `signed_headers` signs
# ``POST\n{path}\n{timestamp}\n{nonce}\n`` with zero body bytes appended, and
# the POST is sent without one — an empty body and a literal ``{}`` are
# different bytes and would verify differently, so this matches the eight
# existing machine-signed calls rather than inventing a third convention.
_CLAIM_ROOT = "/v2/machines/me/oauth-requests"


def connection_path() -> Path:
    directory = home_dir() / "connector"
    _ensure_private_directory(directory)
    return directory / "connection.json"


# macOS keeps the file store, and the reason is now one specific piece of
# unfinished work rather than doubt about the backend.
#
# The transport question is settled and measured. Writing with the value piped
# to a trailing ``-w`` stored an EMPTY password while exiting 0 on both the
# write and the read back (Jeff 220726, synthetic data). That phenomenon is the
# whole of what was measured; why it happens is an untraced reading of the help
# text, kept as such in ``keychain_store`` and deliberately not repeated here
# as a cause. (Written as a cause twice now — Jeff 220792, then again 220903.)
# The write now goes through ``security -i`` with the
# value as hex through ``-X``, which keeps argv clean; Jeff 220838
# independently ran save, load, update and clear against a real Keychain on a
# random service and account and got equality both ways, the reference kept
# across the update, and exit 44 from an outside lookup after the clear.
#
# Not ``SecItemAdd``. It would pin the item's trusted application to whichever
# Python called it, and that path moves with the venv. What happens next is no
# longer inferred, though the observation is a narrow one: on one machine, from
# an agent's session, four calls to it each raised an authorization dialog on
# the logged-in operator's screen and each returned -60006
# (errAuthorizationCanceled) to the caller. Four attempts in one environment do
# not establish what every daemon would see, and this is not a claim that the
# ``security`` path never prompts — nobody has watched that screen while it
# ran. What it does establish is that the prompt is not visible from the
# calling process at all: -60006 is the whole of what the caller learns, and it
# reads like an unreachable Keychain rather than like an interrupted person. A
# credential path whose side effects are invisible to the side making them is
# not one to hand a daemon.
#
# The older file is now handled: the Keychain store is given the file store's
# path, reads it when the Keychain is empty, sweeps it once a write is verified,
# and takes it on a disconnect. Without that, a computer that already held a
# ``connection.json`` would have been reported "not connected" while the
# credential stayed readable on disk — the breach 4bf86f72 closed for the
# temporary file, arriving by the other door.
#
# So what the flag below is still waiting on is not a missing piece but a
# measurement: none of the two-store behaviour has run against a real Keychain.
# Every cell covering it uses a stand-in, and the one real round trip on record
# (Jeff 220838) predates all of it and was a Keychain-only store. Flipping this
# on stand-in evidence would be doing exactly what the last three rounds of
# this file were spent undoing.
_KEYCHAIN_VERIFIED = False


def connection_store(machine: MachineControlIdentity) -> ConnectionStore:
    """Where this computer keeps its connection.

    Keyed on the machine identity rather than the home directory: one login
    Keychain serves every puffo home on a computer, while a connection belongs
    to the identity that claimed it, so two daemons must not land on one item.
    Not a caller's choice — which store holds a credential is a property of the
    computer, and a parameter here would make it a setting.
    """
    if _KEYCHAIN_VERIFIED and platform.system() == "Darwin":
        # Handed the file store's path as well: a computer that connected
        # before the switch still has its credential there, and the Keychain
        # store has to be able to find it and to take it away again.
        return KeychainConnectionStore(machine.machine_id, superseded=connection_path())
    return SkeletonConnectionStore(connection_path())


async def run_claim_command(params: dict, server_url: str) -> dict:
    """Entry point for the dispatcher. Always returns a command result."""
    request_ref = str(params.get("request_ref") or "").strip()
    if not request_ref:
        # Nothing to correlate a failure to, so this cannot be logged usefully
        # against a request; it is a malformed command, not a failed claim.
        logger.error("connector: claim command carried no request_ref")
        return {"ok": False, "connected": False, "stage": "command", "reason": "no request_ref"}

    machine = load_or_create_machine()
    base = server_url.rstrip("/")

    async def fetch(reference: str) -> tuple[str, Any]:
        return await _fetch_from_server(base, machine, reference)

    return await claim_connection(
        request_ref, fetch=fetch, store=connection_store(machine)
    )


async def _fetch_from_server(
    base: str, machine: MachineControlIdentity, request_ref: str
) -> tuple[str, Any]:
    """Ask the server to hand over the credential prepared for this request."""
    path = f"{_CLAIM_ROOT}/{quote(request_ref, safe='')}/claim"
    headers = machine_auth.signed_headers(machine, "POST", path)
    try:
        async with create_remote_http_session(base) as session:
            async with session.post(f"{base}{path}", headers=headers) as response:
                if response.status != 200:
                    raise _refusal(response.status, await _reason_of(response))
                return _read_claimed(await response.json())
    except ClaimFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - transport and decoding both end the claim
        # Reported, not raised: the page has to stop waiting either way, and
        # whether the server already handed the credential out is unknown here.
        raise ClaimFailed(f"claim call did not complete: {type(exc).__name__}") from exc


async def _reason_of(response: Any) -> str | None:
    """The server's structured ``reason``, when the body carries one."""
    try:
        body = await response.json()
    except Exception:  # noqa: BLE001 - an unparseable error body is just no reason
        return None
    return body.get("reason") if isinstance(body, dict) else None


# Interface v1 §2. The server's stable reason code becomes readable text here;
# nothing branches on it. 403 is deliberately the same answer for "no such
# request" and "not this machine" — the server refuses to say which, so neither
# does this.
_REFUSALS = {
    "not_claimable": "this computer cannot claim that request",
    "not_ready": "the credential is not ready yet",
    "already_claimed": "that credential was already claimed",
    "expired": "the prepared credential expired; start again",
}


def _refusal(status: int, reason: str | None) -> ClaimFailed:
    """Turn the server's refusal into the text the caller and the log will see.

    An unrecognised code keeps its raw value. Dropping it would reproduce
    exactly the failure this whole contract exists to prevent: a code the table
    does not know — newly added server-side, or simply spelled differently —
    would surface as a bare status and leave no way to tell which one it was
    (Boris 219775). This assumes the contract holds and ``reason`` stays a short
    stable code; if free text ever arrives there, this is the line that puts it
    in the log.
    """
    described = _REFUSALS.get(reason or "")
    if described is None:
        return ClaimFailed(f"server refused the claim ({status}, reason={reason!r})")
    return ClaimFailed(described)


def _read_claimed(payload: Any) -> tuple[str, Any]:
    """Pull provider and credential out of the claim response.

    The credential is passed through untouched. The daemon is not told the
    credential's shape by the design (v0.4 §4), so parsing any field of it here
    would invent a coupling to Google that the generic layer does not have.
    """
    if not isinstance(payload, dict):
        raise ClaimFailed("claim response was not an object")
    provider = payload.get("provider")
    if not isinstance(provider, str) or not provider:
        raise ClaimFailed("claim response named no provider")
    if "credential" not in payload:
        raise ClaimFailed("claim response carried no credential")
    return provider, payload["credential"]


class RefreshRefused(Exception):
    """The server did not hand back a replacement credential.

    Separate from ``ClaimFailed`` on purpose, and not a rename of it. The claim
    has a contract with a closed list of reason codes (``_REFUSALS``); the
    refresh has neither, because the endpoint does not exist yet. Sharing one
    type would put refresh failures through a table written for a different
    route and read like the two had the same contract behind them — which is
    the reading Jeff 221524 asked for in as many words: do not treat the claim
    path as a confirmed refresh API.

    Raised and caught in this module. ``refresh_connection`` does not catch it:
    unlike a claim it has no result dict to put a failure in, so the exception
    travels out through it to the entry point below.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Agreed, not yet met. The whole shape comes from Bob 221549, who owns the
# contract and is building the server to it: ``POST`` here, machine-signed the
# same way a claim is, ``{connection_ref, credential}`` in and ``{credential}``
# out, with the credential whole in both directions and no ``request_ref``
# (the body itself is Jeff 221524).
#
# Not the claim root with a different leaf, and not a ``/connections/{ref}/``
# path either. The server has no connection table, so a refresh is stateless —
# it takes the whole credential, exchanges it, and hands back the replacement
# (Bob 221549). A path that looked a connection up would describe a server that
# does not exist.
#
# This used to add "after a claim the server keeps no credential", and that
# half has stopped being true. To let a computer claim again after the network
# dropped mid-claim, the server now holds the encrypted credential on the
# request row until it expires — bounded by the TTL, same machine only
# (puffo-server item ①, agreed and pushed there, not deployed). None of this
# file depends on which it is: the refresh carries the whole credential either
# way. The sentence is corrected rather than deleted because it was describing
# a retention property, and a stale claim about retention is the kind a later
# reader acts on.
#
# What is NOT established: that any of this works. A handler exists on the
# other side — puffo-server #406, same shape both ways (Bob 221596) — and
# nothing here has sent it a byte.
#
# No SHA for it on purpose. An open PR's head is built to move, and citing one
# here pins a comment to a value that changes without anybody touching this
# file: #406 went 127ee329 -> d44a88e0 within the hour, over CI failures that
# had nothing to do with the contract. The fact that survives that churn is the
# one worth writing down — these two ends have never spoken — and it stops
# being true only when somebody measures it false.
_REFRESH_PATH = "/v2/machines/me/oauth-credentials/refresh"


async def run_refresh_command(params: dict, server_url: str) -> dict:
    """Entry point for the dispatcher. Always returns a command result.

    The result says whether the credential was replaced. It deliberately
    carries no ``connected`` key, unlike a claim: a refresh that fails at any
    stage but the last leaves this computer holding the connection it had, and
    answering ``connected: False`` would report an outage that did not happen.
    "Did the refresh work" and "is this computer connected" are two questions
    and only one of them was asked.

    "Always" excludes cancellation, and that is not a loophole being reserved.
    ``asyncio.CancelledError`` is a ``BaseException`` and is deliberately not
    caught here: it says the task is being torn down, and answering it with a
    tidy result dict would keep a dying errand alive. Nothing is waiting on
    that answer in that case, because whatever is cancelling this is taking the
    control connection with it.
    """
    reference = str(params.get("connection_ref") or "").strip()
    if not reference:
        # Nothing to correlate a failure to, same as a claim without a request
        # reference: a malformed command, not a failed refresh.
        logger.error("connector: refresh command carried no connection_ref")
        return {"ok": False, "stage": "command", "reason": "no connection_ref"}

    machine = load_or_create_machine()
    base = server_url.rstrip("/")
    # Which segment an OSError came from, and the only thing that tells them
    # apart. A store that would not read and a replacement that would not save
    # both raise OSError, and they are not the same news: one leaves the
    # credential untouched, the other means the server has already been asked
    # for a replacement that is now nowhere. The exception cannot say which —
    # having reached the server is a fact about this call, so this call keeps
    # it.
    #
    # "Answered", not "was asked", and the distinction only stays harmless
    # because a transport error cannot arrive here as an OSError:
    # ``_exchange_with_server`` turns every one of them into ``RefreshRefused``
    # first, which is pinned by a cell of its own rather than left as a habit.
    # Without that, a refused connection would be reported as an unreadable
    # local store — a network fault described as a disk one.
    reached_the_server = False

    async def exchange(credential: Any) -> Any:
        nonlocal reached_the_server
        try:
            replacement = await _exchange_with_server(base, machine, reference, credential)
        except RefreshRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - the server leg, whatever it was
            # Not redundant with the wrapping inside ``_exchange_with_server``.
            # That one exists to give the caller a typed reason; this one is
            # what makes the classification below structural. Without it, an
            # ``OSError`` escaping the server leg would fall into the handler
            # for the store and be reported as "local store unreadable" — a
            # network fault described as a disk one, measured rather than
            # imagined. It is unreachable today only because a wrapper two
            # functions away happens to be exhaustive, and "unreachable
            # because something distant is careful" is the kind of guarantee
            # that stops holding without anybody editing this file.
            raise RefreshRefused(
                f"refresh call did not complete: {type(exc).__name__}"
            ) from exc
        reached_the_server = True
        return replacement

    try:
        refreshed = await refresh_connection(
            reference, exchange=exchange, store=connection_store(machine)
        )
    except StaleConnection as exc:
        return _refresh_failure(reference, STAGE_STALE, str(exc))
    except RefreshRefused as exc:
        return _refresh_failure(reference, STAGE_EXCHANGE, exc.reason)
    except (OSError, ValueError, KeyError) as exc:
        # The type name is carried through for the same reason the claim
        # carries it (Jeff 221379): the two copies of a connection disagreeing
        # is not a flaky read, needs a different thing done about it, and the
        # class name is the only part of that which survives to the caller.
        if reached_the_server:
            return _refresh_failure(
                reference,
                STAGE_SAVE,
                f"the server answered and this computer could not keep it: "
                f"{type(exc).__name__}: {exc}",
            )
        return _refresh_failure(
            reference, STAGE_READ_LOCAL, f"local store unreadable: {type(exc).__name__}"
        )

    logger.info("connector: refreshed connection %s", refreshed.reference)
    return {"ok": True, "connection_ref": refreshed.reference}


def _refresh_failure(reference: str, stage: str, reason: str) -> dict:
    """Log where and why, keyed by the connection the refresh was for."""
    logger.error("connector: refresh %s failed at %s: %s", reference, stage, reason)
    return {"ok": False, "stage": stage, "reason": reason}


async def _exchange_with_server(
    base: str, machine: MachineControlIdentity, reference: str, credential: Any
) -> Any:
    """Hand the stored credential over and get the replacement back, whole.

    ``connection_ref`` is this computer's own reference, minted locally at save
    time. The server does not resolve it — it keeps no connection table and
    uses the field for log correlation only (Bob 221549) — so this is the
    locally minted one rather than the request reference, and nothing depends
    on the server having seen it before.

    The body is signed, not just sent. A claim signs zero body bytes because it
    has no body; this one has one, so the bytes that are signed and the bytes
    that are posted must be the same object — hence ``data=body`` rather than
    ``json=``, which would re-serialise and could sign one string and send
    another.
    """
    body = json.dumps(
        {"connection_ref": reference, "credential": credential}
    ).encode()
    headers = machine_auth.signed_headers(machine, "POST", _REFRESH_PATH, body)
    headers["content-type"] = "application/json"
    try:
        async with create_remote_http_session(base) as session:
            async with session.post(
                f"{base}{_REFRESH_PATH}", data=body, headers=headers
            ) as response:
                if response.status != 200:
                    # No reason table here, unlike the claim. The claim's table
                    # exists because interface v1 fixes the codes; nothing
                    # fixes these, so the raw code goes in the log rather than
                    # a sentence this file made up about it.
                    reason = await _reason_of(response)
                    raise RefreshRefused(
                        f"server refused the refresh ({response.status}, reason={reason!r})"
                    )
                return _read_refreshed(await response.json())
    except RefreshRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - transport and decoding both end it
        raise RefreshRefused(
            f"refresh call did not complete: {type(exc).__name__}"
        ) from exc


def _read_refreshed(payload: Any) -> Any:
    """Pull the replacement credential out of the refresh response.

    The wrapping is ``{"credential": ...}`` (Bob 221549), the same shape the
    claim answers with. Strictness here is not about doubting that: the two
    ways of being wrong do not cost the same, so this refuses anything it does
    not recognise even though it expects to recognise everything. Requiring the
    wrapper and getting a bare package fails here, loudly, and writes nothing.
    Accepting whatever arrives would store an *envelope* as the credential the
    first time the shape moved — a connection quietly holding the wrong bytes,
    discovered whenever it was next used.

    No provider is read. A refresh replaces a credential, not a connection; the
    store keeps the provider it already has, and taking one from this response
    would let the server change it by answering.
    """
    if not isinstance(payload, dict):
        raise RefreshRefused("refresh response was not an object")
    if "credential" not in payload:
        raise RefreshRefused("refresh response carried no credential")
    return payload["credential"]


async def run_disconnect_command(params: dict) -> dict:
    """Entry point for the dispatcher. Always returns a command result.

    Local only. This clears the credential on this computer and tells nobody:
    no server call, no Google revocation. That is the decided order — the local
    copy goes first and is not held hostage to a remote confirmation (Jeremy
    217298 / Jeff 217299) — but the remote half is genuinely *missing*, not
    deferred by design, and a server that still lists this computer as
    connected will say so in the UI until someone builds the other end.

    ``params`` is accepted and not read. A ``connection_ref`` to check against
    would be the obvious addition and is deliberately absent: a disconnect is
    also the documented way out of a computer whose two local copies disagree,
    where ``load`` raises and no reference can be read at all. A check that
    needed the store to be readable would take that exit away, which is the one
    case the exit exists for. The race it would close — a different connection
    claimed between the click and the command — needs a re-authorization to
    complete inside that window on a product that refuses to claim while a
    connection is present.
    """
    store = connection_store(load_or_create_machine())
    try:
        await disconnect(store)
    except OSError as exc:
        # Not reported as done. A disconnect that could not clear must not look
        # like one that did: the credential is still readable on this computer.
        logger.error("connector: disconnect did not clear this computer: %s", exc)
        return {
            "ok": False,
            "connected": True,
            "stage": STAGE_CLEAR,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    logger.info("connector: this computer's connection was cleared")
    return {"ok": True, "connected": False}
