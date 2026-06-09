"""Auth errors must be classified as auth (not rate-limit), so the
consumer skips the pointless kick-retries and the worker flips
auth_failed + DMs the operator.

Regression for: a ``401 Invalid authentication credentials`` reply was
seen as a generic ``API Error`` rate-limit, kick-retried 3×, abandoned,
and never DMed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from puffo_agent.agent._auth_markers import looks_like_auth_error
from puffo_agent.agent.core import AgentAPIError
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.portal.worker import Worker


# ── shared detector ────────────────────────────────────────────────


@pytest.mark.parametrize("reply", [
    "Failed to authenticate. API Error: 401 Invalid authentication credentials",
    "API Error: 401",
    "Invalid API key · Please run /login",
    "invalid_grant",
    "authentication failed",
    "credentials expired",
    '{"type":"authentication_error"}',
])
def test_detector_flags_auth(reply):
    assert looks_like_auth_error(reply) is True


@pytest.mark.parametrize("reply", [
    "API Error: Request rejected (429)",
    "API Error: Server is temporarily limiting requests",
    "I hit a 401 earlier but it cleared up",   # bare 401 in prose → not auth
    "Let me explain how unauthorized access works",
    "",
])
def test_detector_does_not_flag_non_auth(reply):
    assert looks_like_auth_error(reply) is False


def test_agent_api_error_carries_is_auth():
    assert AgentAPIError("x", is_auth=True).is_auth is True
    assert AgentAPIError("y").is_auth is False


# ── consumer: auth skips kick-retries ──────────────────────────────


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    real = asyncio.sleep

    async def fast(_s):
        await real(0)

    monkeypatch.setattr(asyncio, "sleep", fast)


def _make_client() -> PuffoCoreMessageClient:
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "tester-1234"
    client._log = logging.getLogger("test-auth-class")
    client.MAX_API_ERROR_RETRIES = 3  # type: ignore[attr-defined]

    class _StubStore:
        async def mark_thread_processed(self, *a, **k):
            return None

    client.store = _StubStore()  # type: ignore[assignment]
    return client


def _make_entry():
    class _Entry:
        def __init__(self):
            self.dispatching_ids: set[str] = set()

    return _Entry()


@pytest.mark.asyncio
async def test_auth_error_skips_kick_retries_and_abandons():
    client = _make_client()
    retry_calls: list[int] = []
    abandon: list[Any] = []

    async def on_retry(root_id, batch, channel_meta):
        retry_calls.append(1)

    async def on_abandon(root_id, batch, channel_meta, attempts):
        abandon.append(attempts)

    await client._do_api_error_retries(  # type: ignore[arg-type]
        root_id="r",
        entry=_make_entry(),  # type: ignore[arg-type]
        batch=[{"envelope_id": "e1"}],
        channel_meta={},
        on_api_error_retry=on_retry,
        on_api_error_abandon=on_abandon,
        last_envelope="e1",
        is_auth=True,
    )
    assert retry_calls == []   # no pointless kick-retries
    assert abandon == [0]      # abandoned immediately (0 attempts)


@pytest.mark.asyncio
async def test_rate_limit_still_kick_retries(monkeypatch):
    """Sanity: a non-auth API error keeps the existing retry path."""
    client = _make_client()
    retry_calls: list[int] = []

    async def on_retry(root_id, batch, channel_meta):
        retry_calls.append(1)
        raise AgentAPIError("still rate-limited")

    async def on_abandon(*a):
        return None

    await client._do_api_error_retries(  # type: ignore[arg-type]
        root_id="r",
        entry=_make_entry(),  # type: ignore[arg-type]
        batch=[{"envelope_id": "e1"}],
        channel_meta={},
        on_api_error_retry=on_retry,
        on_api_error_abandon=on_abandon,
        last_envelope="e1",
        is_auth=False,
    )
    assert len(retry_calls) == 3   # MAX_API_ERROR_RETRIES kicks


# ── worker: _enter_auth_failed edge ────────────────────────────────


class _RT:
    def __init__(self, health: str):
        self.health = health
        self.error = ""

    def save(self, agent_id: str) -> None:
        pass


def _stub_worker(health: str):
    class _W:
        pass

    w = _W()
    w.runtime = _RT(health)
    w.dm_fired: list[int] = []
    w.refresh_fired: list[int] = []
    # Stub the DM-enter + refresher-kick so the edge logic is exercised
    # without the async DM machinery.
    w._on_auth_failed_enter = lambda: w.dm_fired.append(1)
    w._notify_refresh_needed = lambda: w.refresh_fired.append(1)
    return w


def test_enter_auth_failed_fires_dm_on_edge():
    w = _stub_worker("ok")
    Worker._enter_auth_failed(w, "t-agent")
    assert w.runtime.health == "auth_failed"
    assert w.refresh_fired == [1]   # refresher kicked
    assert w.dm_fired == [1]        # DM fired on the ok→auth_failed edge


def test_enter_auth_failed_no_dm_on_reentry():
    w = _stub_worker("auth_failed")   # already failed
    Worker._enter_auth_failed(w, "t-agent")
    assert w.runtime.health == "auth_failed"
    assert w.refresh_fired == [1]     # still kicks the refresher
    assert w.dm_fired == []           # but no duplicate DM (was_ok=False)
