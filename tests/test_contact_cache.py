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


def test_note_blocked_empty_slug_is_noop():
    c = ContactCache(_FakeHttp(), log)
    c.note_blocked("", True)
    assert c._block == set()


class _KeylessHttp:
    keyless = True

    async def get(self, path):
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_path", [True, False])
async def test_keyless_local_allow_block_state(tmp_path, with_path):
    """Keyless allow/block decisions persist to the per-agent local set and
    stay mutually exclusive; a cache without a local path writes nothing.

    A keyless transport cannot hydrate from the signed server lists, so its
    decisions only survive a daemon restart through the local JSON set. The
    regression this guards: an in-memory-only keyless cache re-admitted a
    blocked sender after every restart, because ``refresh`` is a no-op for
    keyless. The no-path arm pins that callers without a local file keep
    today's in-memory behavior and never create one.
    """
    path = tmp_path / "contacts.json"
    cache = ContactCache(
        _KeylessHttp(), log, local_state_path=path if with_path else None
    )

    cache.note_allowed("alice-1")
    cache.note_blocked("bob-2", True)
    cache.note_blocked("mallory-9", True)
    # Sets stay mutually exclusive under keyless: blocking an allowed slug
    # moves it out of allow, and un-blocking reverses the move.
    cache.note_blocked("alice-1", True)
    assert "alice-1" not in cache._allow
    assert "alice-1" in cache._block
    cache.note_blocked("mallory-9", False)
    assert await cache.is_blocked("mallory-9") is False

    if not with_path:
        assert list(tmp_path.iterdir()) == []
        return

    assert path.exists()
    reloaded = ContactCache(_KeylessHttp(), log, local_state_path=path)
    assert await reloaded.is_allowed("alice-1") is False
    assert await reloaded.is_blocked("alice-1") is True
    assert await reloaded.is_blocked("bob-2") is True
    assert await reloaded.is_allowed("mallory-9") is False
    assert reloaded._allow & reloaded._block == set()

    # Allowing a blocked slug keeps the sets disjoint and persists too.
    reloaded.note_allowed("bob-2")
    assert await reloaded.is_blocked("bob-2") is False
    again = ContactCache(_KeylessHttp(), log, local_state_path=path)
    assert await again.is_allowed("bob-2") is True
    assert await again.is_blocked("bob-2") is False
    assert again._allow & again._block == set()

    # A failed durable write must roll the in-memory sets back, in both the
    # allow and block directions, so a later note retries the full mutation
    # and the decision actually lands on disk. Without the rollback the
    # memory stays ahead of disk and the retry skips persistence entirely.
    write = again._persist_local_state

    def _disk_full():
        raise OSError("disk full")

    again._persist_local_state = _disk_full
    with pytest.raises(OSError):
        again.note_allowed("dave-4")
    with pytest.raises(OSError):
        again.note_blocked("carol-3", True)
    again._persist_local_state = write
    assert "dave-4" not in again._allow
    assert "carol-3" not in again._allow
    assert "carol-3" not in again._block
    again.note_blocked("carol-3", True)
    assert "carol-3" in again._block
    assert "carol-3" not in again._allow
    disk = ContactCache(_KeylessHttp(), log, local_state_path=path)
    assert await disk.is_blocked("carol-3") is True
    assert disk._allow & disk._block == set()
