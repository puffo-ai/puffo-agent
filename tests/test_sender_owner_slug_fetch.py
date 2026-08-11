"""Daemon reads ``owner_slug`` off the same ``/identities/profiles``
response it already fetches for display names, caches it under the
profile TTL, and serves it via ``_fetch_owner_slug`` (agents only;
empty for humans)."""

from __future__ import annotations

import time

import pytest

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


def _bare_client() -> PuffoCoreMessageClient:
    c = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    c.slug = "agent-1"
    c._profile_cache = {}
    c._owner_slug_cache = {}
    return c


class _StubHttp:
    def __init__(self, profiles: dict[str, dict]):
        self.profiles = profiles
        self.calls: list[str] = []

    async def get(self, path: str):
        self.calls.append(path)
        slug = path.split("slugs=", 1)[1]
        entry = self.profiles.get(slug)
        return {"profiles": [entry] if entry else []}


@pytest.mark.asyncio
async def test_fetch_user_profile_caches_owner_slug_for_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "puffo_agent.agent.disk_cache.persist_profile", lambda *a, **k: None,
    )
    c = _bare_client()
    c.http = _StubHttp({
        "nova-bot-1234": {
            "slug": "nova-bot-1234",
            "display_name": "Nova",
            "avatar_url": "",
            "owner_slug": "nova-op-9999",
        },
    })
    owner = await c._fetch_owner_slug("nova-bot-1234")
    assert owner == "nova-op-9999"
    assert c._owner_slug_cache["nova-bot-1234"][0] == "nova-op-9999"


@pytest.mark.asyncio
async def test_human_sender_has_empty_owner_slug(monkeypatch):
    monkeypatch.setattr(
        "puffo_agent.agent.disk_cache.persist_profile", lambda *a, **k: None,
    )
    c = _bare_client()
    c.http = _StubHttp({
        "alice-1234": {
            "slug": "alice-1234",
            "display_name": "Alice",
            "avatar_url": "",
            # humans have no owner_slug field
        },
    })
    assert await c._fetch_owner_slug("alice-1234") == ""


@pytest.mark.asyncio
async def test_fetch_owner_slug_serves_from_cache_without_refetch():
    c = _bare_client()

    calls: list[str] = []

    async def fake_profile(slug: str, *, force_refresh: bool = False):
        calls.append(slug)
        c._owner_slug_cache[slug] = ("nova-op", time.monotonic())
        return ("Nova", "")

    c._fetch_user_profile = fake_profile  # type: ignore[assignment]

    assert await c._fetch_owner_slug("nova-bot") == "nova-op"  # cold → fetch
    assert await c._fetch_owner_slug("nova-bot") == "nova-op"  # warm → cache
    assert calls == ["nova-bot"]
    assert await c._fetch_owner_slug("") == ""


@pytest.mark.asyncio
async def test_fetch_owner_slug_refetches_when_stale():
    c = _bare_client()
    # A stale entry (fetched "long ago") forces a refresh → re-ownership
    # propagates without a daemon restart.
    c._owner_slug_cache["nova-bot"] = ("old-op", time.monotonic() - 10_000)

    calls: list[str] = []

    async def fake_profile(slug: str, *, force_refresh: bool = False):
        calls.append(slug)
        c._owner_slug_cache[slug] = ("new-op", time.monotonic())
        return ("Nova", "")

    c._fetch_user_profile = fake_profile  # type: ignore[assignment]

    assert await c._fetch_owner_slug("nova-bot") == "new-op"
    assert calls == ["nova-bot"]
