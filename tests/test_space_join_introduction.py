"""PUF-402 — a cloud agent introduces itself when it is added to a space.

A **local** agent introduces itself because *it* accepts its own invite, and
the accept routine carries the intro nudge. A **cloud** agent never runs that
routine: the server auto-accepts on its behalf the moment an owner invites it
(``machine_id = "puffo-cloud"``), so the agent is a member before its sandbox
is consulted. The only other route to an introduction wants an
``ACCEPT_CHANNEL_INVITE`` carrying ``original_invite``, and the server's space
auto-accept deliberately omits it — so a space join, the normal path, produced
no introduction at all.

The one join signal a cloud agent does receive is the ``added_to_space`` push,
which the runtime already handles by refreshing its spaces cache. The
introduction is enqueued there.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent import puffo_core_client as pcc
from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.keystore import KeyStore


class _FakeBridge:
    def __init__(self, spaces):
        self._spaces = spaces

    async def send_list_spaces(self, *, timeout: float = 30.0) -> dict:
        return {"spaces": self._spaces}


def _client(tmp_path, bridge, slug="cloud-bot-0001") -> PuffoCoreMessageClient:
    ks = KeyStore(str(tmp_path / "keys"))
    http = PuffoCoreHttpClient("http://127.0.0.1:1", ks, slug)
    store = MessageStore(str(tmp_path / "messages.db"))
    return PuffoCoreMessageClient(
        slug=slug,
        device_id="dev_test",
        space_id="sp_home",
        keystore=ks,
        http_client=http,
        message_store=store,
        workspace="",
        bridge_client=bridge,
    )


def _spaces():
    # The server orders each space's channels `is_public DESC, lower(name)`,
    # so the first entry is a public channel whenever the space has one.
    return [
        {
            "space_id": "sp_new",
            "name": "LasVegas",
            "channels": [
                {"channel_id": "ch_general", "name": "General"},
                {"channel_id": "ch_side", "name": "Side"},
            ],
        },
        {
            "space_id": "sp_old",
            "name": "Elsewhere",
            "channels": [{"channel_id": "ch_other", "name": "Other"}],
        },
    ]


def _record_nudges(client) -> list[tuple[str, str]]:
    """Replace the bound nudge with a recorder, returning its log."""
    seen: list[tuple[str, str]] = []

    async def _fake(*, space_id: str, channel_id: str) -> None:
        seen.append((space_id, channel_id))

    client._enqueue_channel_intro_nudge = _fake
    return seen


def test_being_added_to_a_space_enqueues_one_introduction(tmp_path, monkeypatch):
    """The bug: this produced nothing at all."""
    monkeypatch.setattr(pcc.disk_cache, "persist_space", lambda *a, **k: None)
    client = _client(tmp_path, _FakeBridge(_spaces()))
    seen = _record_nudges(client)

    async def go():
        await client.store.open()
        await client._refresh_bridge_spaces("sp_new")

    asyncio.run(go())
    assert seen == [("sp_new", "ch_general")], (
        "a space join must introduce into that space's first public channel, "
        "and only that one"
    )


def test_startup_refresh_introduces_nowhere(tmp_path, monkeypatch):
    """The same refresh runs on every boot and reconnect. Introducing into
    every known space each time an agent restarts is precisely the failure
    this gate exists to prevent."""
    monkeypatch.setattr(pcc.disk_cache, "persist_space", lambda *a, **k: None)
    client = _client(tmp_path, _FakeBridge(_spaces()))
    seen = _record_nudges(client)

    async def go():
        await client.store.open()
        await client._refresh_bridge_spaces()  # no trigger = startup

    asyncio.run(go())
    assert seen == []


def test_a_space_with_no_visible_channel_is_not_an_error(tmp_path, monkeypatch):
    """A join can land before any channel is visible to the agent. Say so and
    move on; the refresh must still seed what it did see."""
    monkeypatch.setattr(pcc.disk_cache, "persist_space", lambda *a, **k: None)
    spaces = [{"space_id": "sp_new", "name": "Empty"}]
    client = _client(tmp_path, _FakeBridge(spaces))
    seen = _record_nudges(client)

    async def go():
        await client.store.open()
        await client._refresh_bridge_spaces("sp_new")
        assert client._space_name_cache.get("sp_new") == "Empty"

    asyncio.run(go())
    assert seen == []


def test_a_failing_nudge_never_breaks_the_refresh(tmp_path, monkeypatch):
    """The cache seed is what lets the agent post at all. An introduction is a
    nicety; it must not take the seeding down with it."""
    monkeypatch.setattr(pcc.disk_cache, "persist_space", lambda *a, **k: None)
    client = _client(tmp_path, _FakeBridge(_spaces()))

    async def _boom(*, space_id: str, channel_id: str) -> None:
        raise RuntimeError("intro backend down")

    client._enqueue_channel_intro_nudge = _boom

    async def go():
        await client.store.open()
        await client._refresh_bridge_spaces("sp_new")
        # Seeding completed despite the failure.
        assert await client.store.lookup_channel_space("ch_general") == "sp_new"
        assert await client.store.lookup_channel_space("ch_other") == "sp_old"

    asyncio.run(go())


def test_the_real_nudge_is_idempotent_across_repeated_pushes(tmp_path, monkeypatch):
    """Uses the real nudge, not a recorder: a duplicate ``added_to_space`` push
    (a reconnect, a retried delivery) must not introduce twice. The once-only
    marker lives in the message store."""
    monkeypatch.setattr(pcc.disk_cache, "persist_space", lambda *a, **k: None)
    client = _client(tmp_path, _FakeBridge(_spaces()))

    async def _name(*args, **kwargs) -> str:
        return "LasVegas"

    client._resolve_space_name = _name
    client._resolve_channel_name = _name

    async def go():
        await client.store.open()
        await client._refresh_bridge_spaces("sp_new")
        await client._refresh_bridge_spaces("sp_new")
        assert await client.store.has_channel_intro_been_prompted("ch_general")

    asyncio.run(go())
