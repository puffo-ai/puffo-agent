"""Writing a refreshed credential onto the connection already on this computer.

Refresh goes through the server (Boris 220540 / Jeff 220550), so a fresher
credential comes back and has to land somewhere. It cannot land through the
claim: that refuses outright once a connection exists, which is right for a
second authorization and wrong for a fresher credential for this one.

Two things are checked here that a "did it write?" test would miss — that a
refresh which arrives for a connection this computer no longer holds changes
nothing, and that a refresh which fails halfway leaves neither a broken
connection nor a readable copy of either credential behind. Persistence is read
back off the store rather than taken from the return value (测试姬 220601).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from puffo_agent.portal.connector import store as store_module
from puffo_agent.portal.connector.claim import refresh_connection
from puffo_agent.portal.connector.store import SkeletonConnectionStore, StaleConnection

# Distinct per generation of the credential, so "the new one is here" and "the
# old one is gone" are two different questions the same scan can answer.
FIRST = "SENTINELb4e11d07firsttoken"
SECOND = "SENTINEL29ca6f83secondtoken"


def files_holding(directory: Path, needle: str) -> list[Path]:
    """Every readable file under ``directory`` whose bytes contain ``needle``.

    Reads bytes directly rather than shelling out: this machine's ``grep`` is a
    wrapper that injects ``-I`` and can drop a file while reporting "no match".
    """
    found = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            if needle.encode() in path.read_bytes():
                found.append(path)
    return found


def store_at(home: Path) -> SkeletonConnectionStore:
    return SkeletonConnectionStore(home / "connection.json")


def connected(store: SkeletonConnectionStore, request_ref: str, token: str):
    return store.save(
        request_ref=request_ref, provider="fake", credential={"refresh_token": token}
    )


def test_the_scan_can_see_a_credential_that_is_there(tmp_path):
    """Positive control: an empty scan below has to mean something."""
    (tmp_path / "left-behind").write_text(f'{{"refresh_token": "{FIRST}"}}')

    assert files_holding(tmp_path, FIRST)


def test_a_refresh_replaces_the_credential_and_keeps_the_reference(tmp_path):
    """The page is already holding this reference, so a refresh must not mint
    a new one — that would read as a different connection appearing."""
    store = store_at(tmp_path)
    original = connected(store, "req-1", FIRST)

    store.update(reference=original.reference, credential={"refresh_token": SECOND})

    # Read back off disk, not off the return value: what has to be true is that
    # the credential is persisted, not that the call reported it (测试姬 220601).
    reloaded = store_at(tmp_path).load()
    assert reloaded.reference == original.reference
    assert reloaded.request_ref == original.request_ref
    assert reloaded.credential == {"refresh_token": SECOND}


def test_the_credential_a_refresh_replaced_is_not_left_readable(tmp_path):
    """The old refresh token is as usable as the new one until it is gone."""
    store = store_at(tmp_path)
    original = connected(store, "req-1", FIRST)

    store.update(reference=original.reference, credential={"refresh_token": SECOND})

    assert files_holding(tmp_path, FIRST) == []
    assert files_holding(tmp_path, SECOND) == [tmp_path / "connection.json"]


def test_a_refresh_for_a_connection_since_replaced_changes_nothing(tmp_path):
    """Disconnect, re-authorize, then the old refresh lands.

    Without the reference check this writes one connection's credential over
    another's — the silent replacement the claim refuses to decide
    (Jeff 218183 / 219714), arriving by a different door.
    """
    store = store_at(tmp_path)
    superseded = connected(store, "req-1", FIRST)
    store.clear()
    current = connected(store, "req-2", SECOND)
    before = store.path.read_bytes()

    with pytest.raises(StaleConnection):
        store.update(reference=superseded.reference, credential={"refresh_token": FIRST})

    assert store.path.read_bytes() == before
    assert store.load().reference == current.reference
    assert files_holding(tmp_path, FIRST) == []


def test_a_refresh_after_a_disconnect_does_not_bring_the_connection_back(tmp_path):
    """The user disconnected; an in-flight refresh must not undo that."""
    store = store_at(tmp_path)
    original = connected(store, "req-1", FIRST)
    store.clear()

    with pytest.raises(StaleConnection):
        store.update(reference=original.reference, credential={"refresh_token": SECOND})

    assert store.load() is None
    assert files_holding(tmp_path, SECOND) == []


def test_a_refresh_that_fails_after_writing_keeps_the_connection_and_spills_nothing(
    tmp_path, monkeypatch
):
    """The rename fails with both credentials on disk at once.

    Injected at ``os.replace`` because that is the only point where the new
    credential is fully written and the old one is still in place — the case
    where getting the cleanup wrong costs two readable tokens instead of one.
    The closure reads the temporary file at the moment of failure so the test
    proves it reached that state rather than assuming it (Jeff 219890).
    """
    store = store_at(tmp_path)
    original = connected(store, "req-1", FIRST)
    intact = store.path.read_bytes()
    written_before_the_failure = []

    def rename_fails(source, target):
        written_before_the_failure.append(SECOND.encode() in Path(source).read_bytes())
        raise OSError("no space left on device")

    monkeypatch.setattr(store_module.os, "replace", rename_fails)

    with pytest.raises(OSError):
        store.update(reference=original.reference, credential={"refresh_token": SECOND})

    # The injection landed where it was aimed: the new credential really was on
    # disk when the rename failed, so the cleanup below had something to clean.
    assert written_before_the_failure == [True]
    # The failed refresh did not cost the connection this computer still has.
    assert store.path.read_bytes() == intact
    assert store.load().reference == original.reference
    # And the credential that did not make it is not sitting in the leftover.
    assert files_holding(tmp_path, SECOND) == []
    assert files_holding(tmp_path, FIRST) == [tmp_path / "connection.json"]


@pytest.mark.asyncio
async def test_a_refresh_hands_the_stored_credential_over_and_writes_what_comes_back(
    tmp_path,
):
    """Whole in, whole out: the daemon does not take the credential apart in
    either direction (v0.4 §4)."""
    store = store_at(tmp_path)
    original = connected(store, "req-1", FIRST)
    handed_over = []

    async def exchange(credential):
        handed_over.append(credential)
        return {"refresh_token": SECOND}

    await refresh_connection(original.reference, exchange=exchange, store=store)

    assert handed_over == [{"refresh_token": FIRST}]
    assert store_at(tmp_path).load().credential == {"refresh_token": SECOND}


@pytest.mark.asyncio
async def test_a_refresh_for_a_connection_that_is_gone_never_reaches_the_server(
    tmp_path,
):
    """Nothing to refresh, so nothing to ask for — and an exception, not a
    result dict: no page is waiting on a refresh."""
    store = store_at(tmp_path)
    asked = []

    async def exchange(credential):
        asked.append(credential)
        return {"refresh_token": SECOND}

    with pytest.raises(StaleConnection):
        await refresh_connection("nothing-here", exchange=exchange, store=store)

    assert asked == []


@pytest.mark.asyncio
async def test_a_slow_refresh_cannot_overwrite_a_newer_one_for_the_same_connection(
    tmp_path,
):
    """Two refreshes for one connection carry the *same* reference.

    So the reference check cannot tell a late response from a current one —
    the slow one just writes last and the newer credential is gone. Only
    holding the lock across the exchange closes it, which is why the lock went
    around the whole errand rather than the store write (Jeff 220726
    reproduced this against the version that locked only the write).
    """
    import asyncio

    store = store_at(tmp_path)
    original = connected(store, "req-1", FIRST)
    first_is_talking_to_the_server = asyncio.Event()
    let_the_first_finish = asyncio.Event()
    seen_by_the_second = []

    async def slow(credential):
        first_is_talking_to_the_server.set()
        await let_the_first_finish.wait()
        return {"refresh_token": "older-response"}

    async def quick(credential):
        seen_by_the_second.append(credential)
        return {"refresh_token": "newer-response"}

    slow_one = asyncio.create_task(
        refresh_connection(original.reference, exchange=slow, store=store)
    )
    await first_is_talking_to_the_server.wait()
    quick_one = asyncio.create_task(
        refresh_connection(original.reference, exchange=quick, store=store)
    )
    for _ in range(10):
        await asyncio.sleep(0)

    # The second one has not been let near the store while the first is still
    # out at the server — otherwise it would already have written.
    assert store.load().credential == {"refresh_token": FIRST}

    let_the_first_finish.set()
    await asyncio.gather(slow_one, quick_one)

    # The second refresh saw what the first one wrote, rather than the stale
    # credential it would have read had the two overlapped.
    assert seen_by_the_second == [{"refresh_token": "older-response"}]
    assert store.load().credential == {"refresh_token": "newer-response"}


@pytest.mark.asyncio
async def test_a_disconnect_is_not_undone_by_a_claim_that_was_already_running(tmp_path):
    """The user disconnects while a claim is waiting on the server.

    The claim read "nothing here" before the disconnect, so its save has
    nothing to compare against — the reference check cannot help. Only
    serialising the two keeps the connection from coming back after the user
    gave it up (Jeff 220610).
    """
    import asyncio

    from puffo_agent.portal.connector.claim import claim_connection, disconnect

    store = store_at(tmp_path)
    reached_the_server = asyncio.Event()
    let_it_finish = asyncio.Event()

    async def slow(request_ref):
        reached_the_server.set()
        await let_it_finish.wait()
        return "fake", {"refresh_token": SECOND}

    claiming = asyncio.create_task(claim_connection("req-1", fetch=slow, store=store))
    await reached_the_server.wait()

    disconnecting = asyncio.create_task(disconnect(store))
    # Give an unserialised disconnect every chance to run first and be wrong.
    for _ in range(10):
        await asyncio.sleep(0)
    let_it_finish.set()
    await asyncio.gather(claiming, disconnecting)

    assert store.load() is None
    assert files_holding(tmp_path, SECOND) == []


# ---------------------------------------------------------------------------
# What counts as a record at all.
#
# ``json.loads`` answers "is this JSON", which is strictly less than "is this a
# connection". Jeff 220838 drove a well-formed but meaningless record into
# ``_read`` and the claim answered ``connected: True`` with a null reference:
# a computer reporting a connection to nothing, and a page that would show it
# as 已连接. These cells fix what a record has to be, and check that the answer
# to a bad one is the fail-closed one the claim already knows how to give.


class InjectedStore(store_module.ConnectionStore):
    """A store handing back bytes nobody here wrote.

    Deliberately not the Keychain or the file store: the question is what
    ``ConnectionStore`` does with a record that got corrupted somehow, and
    routing it through a real backend would only test that backend's ability
    to hold the bytes.
    """

    def __init__(self, raw: bytes | None) -> None:
        self.raw = raw
        self.written: list[bytes] = []

    def _read(self):
        return self.raw

    def _put(self, body: bytes) -> None:
        self.written.append(body)

    def _erase(self) -> None:
        self.raw = None


MALFORMED = {
    "null-reference": b'{"reference":null,"request_ref":"r","provider":"p","credential":{"t":1}}',
    "empty-reference": b'{"reference":"","request_ref":"r","provider":"p","credential":{"t":1}}',
    "non-string-provider": b'{"reference":"a","request_ref":"r","provider":false,"credential":{"t":1}}',
    "missing-request_ref": b'{"reference":"a","provider":"p","credential":{"t":1}}',
    "null-credential": b'{"reference":"a","request_ref":"r","provider":"p","credential":null}',
    "missing-credential": b'{"reference":"a","request_ref":"r","provider":"p"}',
    # Valid JSON, but not an object. These used to raise TypeError out of the
    # subscript, which the claim deliberately does not catch because it means
    # "our own bug" — so they escaped its handling entirely.
    "a-JSON-array": b"[]",
    "a-JSON-number": b"123",
    "a-JSON-string": b'"hello"',
    "a-JSON-null": b"null",
}


@pytest.mark.parametrize("raw", MALFORMED.values(), ids=list(MALFORMED))
def test_a_record_that_is_not_a_connection_is_not_read_as_one(raw):
    with pytest.raises(ValueError):
        InjectedStore(raw).load()


@pytest.mark.parametrize("raw", MALFORMED.values(), ids=list(MALFORMED))
@pytest.mark.asyncio
async def test_a_malformed_record_never_reports_connected(raw):
    """The whole point of the exercise: fail closed, and never with a null
    reference in a success answer."""
    from puffo_agent.portal.connector.claim import STAGE_READ_LOCAL, claim_connection

    async def must_not_be_called(request_ref):
        raise AssertionError("a store that cannot be read must not be overwritten")

    answer = await claim_connection("r", fetch=must_not_be_called, store=InjectedStore(raw))

    assert answer["ok"] is False
    assert answer["connected"] is False
    assert answer["stage"] == STAGE_READ_LOCAL
    assert "connection_ref" not in answer


def test_a_well_formed_record_still_loads():
    """Positive control: the refusals above have to be refusing something."""
    good = b'{"reference":"a","request_ref":"r","provider":"p","credential":{"t":1}}'

    loaded = InjectedStore(good).load()

    assert loaded is not None
    assert (loaded.reference, loaded.request_ref, loaded.provider) == ("a", "r", "p")
    assert loaded.credential == {"t": 1}


def test_the_credential_is_still_opaque():
    """Checking the record's own fields is not licence to check inside it.

    The daemon is not told the credential's shape (v0.4 §4), so anything that
    is not None goes through — a string, a list, a number.
    """
    for credential in ("a string", ["a", "list"], 0, False, {}, ""):
        raw = json.dumps(
            {"reference": "a", "request_ref": "r", "provider": "p", "credential": credential}
        ).encode()

        assert InjectedStore(raw).load().credential == credential


def test_the_store_refuses_to_write_what_it_would_refuse_to_read():
    """Otherwise the read check above would be the bug.

    A record written through a gap in the write path could never be loaded or
    refreshed afterwards, only cleared — so the same rule guards both doors.
    """
    store = InjectedStore(None)

    with pytest.raises(ValueError):
        store.save(request_ref="r", provider="", credential={"t": 1})
    with pytest.raises(ValueError):
        store.save(request_ref="r", provider="p", credential=None)

    assert store.written == []


@pytest.mark.asyncio
async def test_a_save_the_store_refuses_is_reported_as_a_failed_save():
    """Not connected, and not an exception out of the command either: the page
    still has to be able to stop waiting (v0.4 §7)."""
    from puffo_agent.portal.connector.claim import STAGE_SAVE, claim_connection

    async def answers_with_no_provider(request_ref):
        return "", {"refresh_token": FIRST}

    answer = await claim_connection(
        "r", fetch=answers_with_no_provider, store=InjectedStore(None)
    )

    assert answer["ok"] is False
    assert answer["connected"] is False
    assert answer["stage"] == STAGE_SAVE
