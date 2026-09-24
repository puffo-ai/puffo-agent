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

from pathlib import Path

import pytest

from puffo_agent.portal.connector import store as store_module
from puffo_agent.portal.connector.claim import replace_credential
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
async def test_the_refresh_entry_point_writes_through_to_the_store(tmp_path):
    """``replace_credential`` exists to take the claim lock; prove it still
    reaches the store rather than only holding it."""
    store = store_at(tmp_path)
    original = connected(store, "req-1", FIRST)

    await replace_credential(
        original.reference, {"refresh_token": SECOND}, store=store
    )

    assert store_at(tmp_path).load().credential == {"refresh_token": SECOND}


@pytest.mark.asyncio
async def test_the_refresh_entry_point_raises_rather_than_reporting_a_dict(tmp_path):
    """No page is waiting on a refresh, so a failure is an exception, not a
    result dict a UI would have to interpret."""
    store = store_at(tmp_path)

    with pytest.raises(StaleConnection):
        await replace_credential("nothing-here", {"refresh_token": SECOND}, store=store)
