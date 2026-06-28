"""Perf-flavour tests for the name resolvers.

- ``_resolve_space_name`` populates every entry in the ``/spaces``
  response so the next unknown-space resolve is a cache hit.
- ``_resolve_channel_name`` tries ``/spaces/<sp>/channels`` first
  (one round-trip, all names), falling back to the per-channel
  event-replay only on miss.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


class _FakeHttp:
    """Records GET paths + replies from a path→response dict."""

    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = dict(responses)
        self.gets: list[str] = []

    async def get(self, path: str) -> dict:
        self.gets.append(path)
        if path in self.responses:
            return self.responses[path]
        raise KeyError(f"unexpected GET: {path}")


def _bare_client(http: _FakeHttp) -> PuffoCoreMessageClient:
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.http = http  # type: ignore[assignment]
    client._space_name_cache = {}
    client._channel_name_cache = {}
    return client


@pytest.mark.asyncio
async def test_resolve_space_name_populates_all_entries_in_one_fetch(monkeypatch):
    # Disable disk persistence side-effect (tested elsewhere).
    import puffo_agent.agent.puffo_core_client as mod
    monkeypatch.setattr(mod.disk_cache, "persist_space", lambda *a, **k: None)

    http = _FakeHttp({
        "/spaces": {
            "spaces": [
                {"space_id": "sp_1", "name": "Engineering"},
                {"space_id": "sp_2", "name": "Marketing"},
                {"space_id": "sp_3", "name": "General"},
            ],
        },
    })
    client = _bare_client(http)

    name = await client._resolve_space_name("sp_1")
    assert name == "Engineering"
    assert len(http.gets) == 1

    # All three names now in cache; no further HTTP for sp_2/sp_3.
    assert await client._resolve_space_name("sp_2") == "Marketing"
    assert await client._resolve_space_name("sp_3") == "General"
    assert len(http.gets) == 1


@pytest.mark.asyncio
async def test_resolve_space_name_falls_back_to_id_when_unknown(monkeypatch):
    import puffo_agent.agent.puffo_core_client as mod
    monkeypatch.setattr(mod.disk_cache, "persist_space", lambda *a, **k: None)

    http = _FakeHttp({"/spaces": {"spaces": []}})
    client = _bare_client(http)

    name = await client._resolve_space_name("sp_missing")
    assert name == "sp_missing"
    # Second call must hit the cached negative result.
    assert await client._resolve_space_name("sp_missing") == "sp_missing"
    assert len(http.gets) == 1


@pytest.mark.asyncio
async def test_resolve_channel_name_uses_channels_endpoint_first(monkeypatch):
    import puffo_agent.agent.puffo_core_client as mod
    monkeypatch.setattr(mod.disk_cache, "persist_channel", lambda *a, **k: None)

    http = _FakeHttp({
        "/spaces/sp_1/channels": {
            "channels": [
                {"channel_id": "ch_a", "name": "general"},
                {"channel_id": "ch_b", "name": "random"},
            ],
        },
    })
    client = _bare_client(http)

    assert await client._resolve_channel_name("sp_1", "ch_a") == "general"
    # /events MUST NOT be hit when channels endpoint covers the ask.
    assert http.gets == ["/spaces/sp_1/channels"]

    # ch_b was also populated in cache — second resolve no extra GET.
    assert await client._resolve_channel_name("sp_1", "ch_b") == "random"
    assert http.gets == ["/spaces/sp_1/channels"]


@pytest.mark.asyncio
async def test_resolve_channel_name_falls_back_to_events_on_miss(monkeypatch):
    import puffo_agent.agent.puffo_core_client as mod
    monkeypatch.setattr(mod.disk_cache, "persist_channel", lambda *a, **k: None)

    http = _FakeHttp({
        "/spaces/sp_1/channels": {"channels": []},
        "/spaces/sp_1/events": {
            "events": [
                {"kind": "create_channel",
                 "payload": {"channel_id": "ch_a", "name": "general"}},
            ],
            "has_more": False,
        },
    })
    client = _bare_client(http)

    name = await client._resolve_channel_name("sp_1", "ch_a")
    assert name == "general"
    # Both endpoints hit (channels first empty, then events replay).
    assert http.gets == ["/spaces/sp_1/channels", "/spaces/sp_1/events"]
