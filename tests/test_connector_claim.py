"""Claiming a prepared credential onto this computer.

The cases that matter are the ones where a wrong answer costs a credential:
refusing to overwrite a connection this computer already holds, and refusing to
read an unreadable store as "nothing here".
"""

from __future__ import annotations

import asyncio
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


def test_a_connection_does_not_print_its_credential(tmp_path):
    """One ``%s`` must not be able to put a live token in a log file.

    The default dataclass repr prints every field. Nothing in this package
    logs a Connection — every line passes ``.reference`` — so this is about
    the line somebody adds later, and about the tracebacks and pytest failures
    nobody writes on purpose. The same field was flagged on the server half
    for the same reason (Boris 221597, on a derived ``Debug``).
    """
    store = store_at(tmp_path)
    saved = store.save(
        request_ref="req-1", provider="google",
        credential={"refresh_token": "SENTINEL8d31f0c2token", "nested": ["SENTINEL8d31f0c2token"]},
    )

    for shown in (repr(saved), str(saved), f"{saved}", "%s" % (saved,)):
        assert "SENTINEL8d31f0c2token" not in shown
    # Not achieved by printing nothing: the three non-secret fields are what
    # a log line would have wanted, and hiding them just moves the print.
    assert saved.reference in repr(saved)
    assert "google" in repr(saved)
    assert "req-1" in repr(saved)


def test_the_stored_credential_is_never_parsed(tmp_path):
    """The daemon is not told the credential's shape (v0.4 §4), so a shape it
    has never seen must round-trip untouched."""
    store = store_at(tmp_path)
    alien = {"nested": [1, {"unexpected": None}], "kind": "not-a-google-token"}

    store.save(request_ref="req-1", provider="whatever", credential=alien)

    assert store.load().credential == alien
    assert json.loads(store.path.read_bytes())["credential"] == alien


@pytest.mark.asyncio
async def test_two_different_requests_at_once_do_not_overwrite_each_other(tmp_path):
    """`connector.claim` is a background op, so two commands really do overlap.

    Read-then-save is not atomic, so without serialisation both claims see an
    empty store, both fetch, and the second save silently replaces the first —
    while the first caller is told "connected" with a reference that is no
    longer on disk (Boris 219713).
    """
    store = store_at(tmp_path)

    async def slow(request_ref):
        await asyncio.sleep(0.05)
        return "fake", {"token": request_ref}

    first, second = await asyncio.gather(
        claim_connection("req_A", fetch=slow, store=store),
        claim_connection("req_B", fetch=slow, store=store),
    )

    saved = store.load()
    winners = [r for r in (first, second) if r["ok"]]
    losers = [r for r in (first, second) if not r["ok"]]
    assert len(winners) == 1, "exactly one claim may take the single connection"
    assert len(losers) == 1
    assert losers[0]["stage"] == STAGE_ALREADY_CONNECTED
    # The one told "connected" must be the one actually on disk — the original
    # symptom was a reference handed out for a credential already replaced.
    assert winners[0]["connection_ref"] == saved.reference
    assert saved.credential == {"token": saved.request_ref}


@pytest.mark.asyncio
async def test_the_machine_level_command_reaches_the_connector(tmp_path, monkeypatch):
    """`connector.claim` carries no agent_slug; prove the dispatcher still
    routes it instead of falling through to "unsupported op"."""
    from puffo_agent.portal.connector import command as connector_command
    from puffo_agent.portal.control.client import execute_command

    seen = {}

    async def fake_run(params, server_url):
        seen["params"] = params
        seen["server_url"] = server_url
        return {"ok": True, "connected": True, "connection_ref": "deadbeef"}

    monkeypatch.setattr(connector_command, "run_claim_command", fake_run)

    result = await execute_command(
        "connector.claim", None, {"request_ref": "req-1"}, server_url="https://example.test/"
    )

    assert result == {"ok": True, "connected": True, "connection_ref": "deadbeef"}
    assert seen["params"] == {"request_ref": "req-1"}


@pytest.mark.asyncio
async def test_the_claim_command_without_a_server_url_does_not_reach_the_connector(tmp_path):
    from puffo_agent.portal.control.client import execute_command

    result = await execute_command("connector.claim", None, {"request_ref": "req-1"})

    assert result["ok"] is False


def test_an_unrecognised_server_code_is_not_swallowed():
    """A code the table does not know must still reach the log verbatim.

    Otherwise a newly added or misspelled server code degrades to a bare
    status — the very reason-loss this contract exists to prevent, and with
    nothing left to diagnose it by (Boris 219775).
    """
    from puffo_agent.portal.connector.command import _refusal

    assert "brand_new_code" in _refusal(409, "brand_new_code").reason
    # A code the table does know still reads as its own sentence.
    assert _refusal(409, "not_ready").reason == "the credential is not ready yet"
