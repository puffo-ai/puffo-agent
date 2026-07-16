"""ContactCache: hydration, TTL / miss-refresh policy, in-memory notes."""

from __future__ import annotations

import logging
import time

import pytest

from puffo_agent.agent.contact_cache import ContactCache

log = logging.getLogger("contact-cache-test")


class _FakeHttp:
    def __init__(self):
        self.allow: list[str] = []
        self.block_users: list[str] = []
        self.extra_blocks: list[dict] = []  # non-user block rows
        self.get_calls: list[str] = []
        self.fail = False

    async def get(self, path):
        self.get_calls.append(path)
        if self.fail:
            raise RuntimeError("network down")
        if path == "/allowlists":
            return {"entries": [{"peer_slug": s, "added_at": 1} for s in self.allow]}
        if path == "/blocklists":
            blocks = [{"target": "user", "id": s, "blocked_at": 1} for s in self.block_users]
            return {"blocks": blocks + self.extra_blocks}
        return {}


@pytest.mark.asyncio
async def test_refresh_hydrates_both_sets():
    http = _FakeHttp()
    http.allow = ["alice-1"]
    http.block_users = ["bob-2"]
    c = ContactCache(http, log)
    await c.refresh()
    assert c._allow == {"alice-1"}
    assert c._block == {"bob-2"}


@pytest.mark.asyncio
async def test_blocklist_ignores_non_user_targets():
    http = _FakeHttp()
    http.block_users = ["u-1"]
    http.extra_blocks = [{"target": "space", "id": "sp_x", "blocked_at": 1}]
    c = ContactCache(http, log)
    await c.refresh()
    assert c._block == {"u-1"}


@pytest.mark.asyncio
async def test_is_allowed_hydrates_on_first_miss():
    http = _FakeHttp()
    http.allow = ["alice-1"]
    c = ContactCache(http, log)
    assert await c.is_allowed("alice-1") is True
    assert "/allowlists" in http.get_calls


@pytest.mark.asyncio
async def test_is_allowed_fresh_miss_does_not_refetch():
    http = _FakeHttp()
    c = ContactCache(http, log, miss_refresh_interval=15.0)
    await c.refresh()
    n = len(http.get_calls)
    assert await c.is_allowed("stranger-9") is False
    assert len(http.get_calls) == n  # fresh → no extra fetch


@pytest.mark.asyncio
async def test_is_allowed_stale_miss_refetches():
    http = _FakeHttp()
    c = ContactCache(http, log, ttl=300.0, miss_refresh_interval=15.0)
    await c.refresh()
    n = len(http.get_calls)
    c._fetched_at = time.monotonic() - 20  # older than miss interval, < ttl
    http.allow = ["late-add-3"]
    assert await c.is_allowed("late-add-3") is True
    assert len(http.get_calls) > n


@pytest.mark.asyncio
async def test_is_blocked_never_miss_refreshes():
    http = _FakeHttp()
    c = ContactCache(http, log, ttl=300.0, miss_refresh_interval=15.0)
    await c.refresh()
    n = len(http.get_calls)
    c._fetched_at = time.monotonic() - 20  # stale for a miss, but < ttl
    assert await c.is_blocked("bob-2") is False
    assert len(http.get_calls) == n  # channel hot-path must not fetch


@pytest.mark.asyncio
async def test_is_blocked_refreshes_after_ttl():
    http = _FakeHttp()
    c = ContactCache(http, log, ttl=300.0)
    await c.refresh()
    n = len(http.get_calls)
    c._fetched_at = time.monotonic() - 400  # past ttl
    http.block_users = ["newblock-5"]
    assert await c.is_blocked("newblock-5") is True
    assert len(http.get_calls) > n


@pytest.mark.asyncio
async def test_note_allowed_and_blocked_toggle():
    http = _FakeHttp()
    c = ContactCache(http, log)
    c.note_allowed("a-1")
    assert "a-1" in c._allow
    c.note_blocked("b-2", True)
    assert "b-2" in c._block
    c.note_blocked("b-2", False)
    assert "b-2" not in c._block


@pytest.mark.asyncio
async def test_refresh_failure_keeps_existing_sets():
    http = _FakeHttp()
    http.allow = ["keep-1"]
    c = ContactCache(http, log)
    await c.refresh()
    http.fail = True
    c._fetched_at = 0.0  # force the refresh path
    await c.refresh()  # fails, swallowed
    assert c._allow == {"keep-1"}


@pytest.mark.asyncio
async def test_empty_slug_is_neither_allowed_nor_blocked():
    http = _FakeHttp()
    c = ContactCache(http, log)
    assert await c.is_allowed("") is False
    assert await c.is_blocked("") is False
    assert http.get_calls == []  # short-circuits before any fetch
