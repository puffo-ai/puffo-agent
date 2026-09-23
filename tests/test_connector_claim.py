"""Claiming a prepared credential onto this computer.

The cases that matter are the ones where a wrong answer costs a credential:
refusing to overwrite a connection this computer already holds, and refusing to
read an unreadable store as "nothing here".
"""

from __future__ import annotations

import json

import pytest

from puffo_agent.portal.connector.claim import (
    STAGE_ALREADY_CONNECTED,
    STAGE_FETCH,
    STAGE_READ_LOCAL,
    STAGE_SAVE,
    ClaimFailed,
    claim_connection,
)
from puffo_agent.portal.connector.store import SkeletonConnectionStore


def store_at(tmp_path):
    return SkeletonConnectionStore(tmp_path / "connection.json")


async def hands_over(_request_ref):
    return "fake", {"token": "opaque-to-the-daemon"}


@pytest.mark.asyncio
async def test_a_claim_saves_the_credential_and_reports_the_connection(tmp_path):
    store = store_at(tmp_path)

    result = await claim_connection("req-1", fetch=hands_over, store=store)

    assert result["ok"] is True
    assert result["connected"] is True
    saved = store.load()
    # The reported reference is the saved one: the page's "connected" and the
    # stored connection have to be the same thing, not two ids that agree today.
    assert result["connection_ref"] == saved.reference
    assert saved.credential == {"token": "opaque-to-the-daemon"}


@pytest.mark.asyncio
async def test_a_failed_claim_reports_the_stage_and_saves_nothing(tmp_path):
    store = store_at(tmp_path)

    async def refuses(_request_ref):
        raise ClaimFailed("server has no such claim")

    result = await claim_connection("req-1", fetch=refuses, store=store)

    assert result["ok"] is False
    assert result["connected"] is False
    assert result["stage"] == STAGE_FETCH
    assert result["reason"] == "server has no such claim"
    assert store.load() is None


@pytest.mark.asyncio
async def test_a_second_request_does_not_replace_the_connection_already_here(tmp_path):
    """Whether re-authorizing replaces an existing connection is undecided
    (Jeff 218183), so the claim must refuse rather than answer it by writing."""
    store = store_at(tmp_path)
    first = store.save(request_ref="req-1", provider="fake", credential={"token": "first"})

    result = await claim_connection("req-2", fetch=hands_over, store=store)

    assert result["ok"] is False
    assert result["stage"] == STAGE_ALREADY_CONNECTED
    still_here = store.load()
    assert still_here.reference == first.reference
    assert still_here.credential == {"token": "first"}


@pytest.mark.asyncio
async def test_renotifying_the_same_request_reports_the_same_connection(tmp_path):
    """A repeated notification must not fail the page: the connection being
    asked for is already here, so the answer is the reference it already has."""
    store = store_at(tmp_path)
    first = await claim_connection("req-1", fetch=hands_over, store=store)

    again = await claim_connection("req-1", fetch=hands_over, store=store)

    assert again["ok"] is True
    assert again["connection_ref"] == first["connection_ref"]


@pytest.mark.asyncio
async def test_an_unreadable_store_is_not_reported_as_no_connection(tmp_path):
    """Answering "nothing here" would invite the save below to overwrite a
    credential this computer could not read."""
    store = store_at(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"{ this is not json")

    result = await claim_connection("req-1", fetch=hands_over, store=store)

    assert result["ok"] is False
    assert result["stage"] == STAGE_READ_LOCAL
    # Still exactly what was there: the claim did not write over it.
    assert store.path.read_bytes() == b"{ this is not json"


@pytest.mark.asyncio
async def test_a_credential_that_does_not_reach_disk_is_not_reported_connected(tmp_path):
    """v0.4 §7, local-save row: the page shows not-finished, not connected."""

    class RefusesToSave(SkeletonConnectionStore):
        def save(self, **_kwargs):
            raise OSError("disk full")

    store = RefusesToSave(tmp_path / "connection.json")

    result = await claim_connection("req-1", fetch=hands_over, store=store)

    assert result["ok"] is False
    assert result["connected"] is False
    assert result["stage"] == STAGE_SAVE


def test_the_stored_credential_is_never_parsed(tmp_path):
    """The daemon is not told the credential's shape (v0.4 §4), so a shape it
    has never seen must round-trip untouched."""
    store = store_at(tmp_path)
    alien = {"nested": [1, {"unexpected": None}], "kind": "not-a-google-token"}

    store.save(request_ref="req-1", provider="whatever", credential=alien)

    assert store.load().credential == alien
    assert json.loads(store.path.read_bytes())["credential"] == alien
