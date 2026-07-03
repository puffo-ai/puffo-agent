"""``CredentialRefresher.ensure_fresh()``: macOS pre-delivery gate.

The Worker calls this before handing a batch to its adapter so the
agent's own claude never has to discover an expired token via 401.
Same single-writer mutex + re-check-after-lock pattern as
``_refresh_now`` — N concurrent callers coalesce into one
backend.refresh() per real expiration."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from puffo_agent.portal.credential_refresh import (
    CredentialRefresher,
    REFRESH_SAFETY_MARGIN_SECONDS,
    RefreshOutcome,
)


def _write_creds(host_home: Path, *, expires_in_seconds: int) -> None:
    p = host_home / ".claude" / ".credentials.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-test",
            "refreshToken": "sk-ant-ort01-test",
            "expiresAt": int((time.time() + expires_in_seconds) * 1000),
            "scopes": ["user:inference"],
        }
    }))


@pytest.mark.asyncio
async def test_ensure_fresh_skips_refresh_when_token_is_fresh(tmp_path):
    # Token good for 2h — well above the 600s margin → no refresh fires.
    _write_creds(tmp_path, expires_in_seconds=7200)
    r = CredentialRefresher(host_home=tmp_path)

    refresh_calls = 0

    async def _fake_refresh() -> RefreshOutcome:
        nonlocal refresh_calls
        refresh_calls += 1
        return RefreshOutcome.UNCHANGED

    r.backend.refresh = _fake_refresh  # type: ignore[assignment]
    assert await r.ensure_fresh() is True
    assert refresh_calls == 0


@pytest.mark.asyncio
async def test_ensure_fresh_drives_refresh_when_near_expiry(tmp_path):
    # 60s remaining → under the 600s margin → refresh fires.
    _write_creds(tmp_path, expires_in_seconds=60)
    r = CredentialRefresher(host_home=tmp_path)

    async def _fake_refresh() -> RefreshOutcome:
        # Simulate a successful refresh: token now valid 1h.
        _write_creds(tmp_path, expires_in_seconds=3600)
        return RefreshOutcome.REFRESHED

    r.backend.refresh = _fake_refresh  # type: ignore[assignment]
    assert await r.ensure_fresh() is True


@pytest.mark.asyncio
async def test_ensure_fresh_returns_false_when_refresh_fails_and_token_is_expired(tmp_path):
    # Token already expired (-10s) — refresh attempt fails → return False.
    _write_creds(tmp_path, expires_in_seconds=-10)
    r = CredentialRefresher(host_home=tmp_path)

    async def _fake_refresh() -> RefreshOutcome:
        return RefreshOutcome.FAILED

    r.backend.refresh = _fake_refresh  # type: ignore[assignment]
    assert await r.ensure_fresh() is False


@pytest.mark.asyncio
async def test_ensure_fresh_concurrent_callers_coalesce_into_one_refresh(tmp_path):
    # 100 simultaneous callers; mutex + re-check-after-lock pattern means
    # exactly one backend.refresh() fires.
    _write_creds(tmp_path, expires_in_seconds=60)
    r = CredentialRefresher(host_home=tmp_path)

    refresh_calls = 0

    async def _fake_refresh() -> RefreshOutcome:
        nonlocal refresh_calls
        refresh_calls += 1
        # Yield once so other tasks have a chance to enter ensure_fresh
        # before this one writes the new token + releases the lock.
        await asyncio.sleep(0)
        _write_creds(tmp_path, expires_in_seconds=3600)
        return RefreshOutcome.REFRESHED

    r.backend.refresh = _fake_refresh  # type: ignore[assignment]

    results = await asyncio.gather(*(r.ensure_fresh() for _ in range(100)))
    assert all(results)
    # The mutex + re-check causes only the first acquirer to actually
    # call refresh; subsequent acquirers see the post-write fresh token
    # and bail out of _refresh_now early.
    assert refresh_calls == 1


@pytest.mark.asyncio
async def test_ensure_fresh_returns_false_on_missing_credentials(tmp_path):
    # No .credentials.json at all → expires_in is None → drives a
    # refresh that also fails → False.
    r = CredentialRefresher(host_home=tmp_path)

    async def _fake_refresh() -> RefreshOutcome:
        return RefreshOutcome.FAILED

    r.backend.refresh = _fake_refresh  # type: ignore[assignment]
    assert await r.ensure_fresh() is False


@pytest.mark.asyncio
async def test_ensure_fresh_fans_out_to_agents_in_fresh_path(tmp_path):
    """Daemon view says fresh → ensure_fresh still syncs canonical
    credentials to every registered agent. Closes the split-brain
    window where the agent's per-agent credentials file is stale
    while the daemon's view is fresh (macOS copy-mode drift, or a
    post-refresh fan-out the daemon hasn't done yet)."""
    _write_creds(tmp_path, expires_in_seconds=7200)
    r = CredentialRefresher(host_home=tmp_path)

    agent_a = tmp_path / "agent-a"
    agent_b = tmp_path / "agent-b"
    for agent in (agent_a, agent_b):
        agent.mkdir()
        r.register_agent(agent)

    assert await r.ensure_fresh() is True
    # FileBackend.sync_to_agent → sync_host_credentials_view → view copy
    # into <agent>/.claude/.credentials.json.
    for agent in (agent_a, agent_b):
        assert (agent / ".claude" / ".credentials.json").exists()


@pytest.mark.asyncio
async def test_ensure_fresh_fans_out_after_successful_refresh(tmp_path):
    """Refresh-path also fans out — verifies the post-refresh _sync_views
    call lands when ensure_fresh had to drive an actual refresh."""
    _write_creds(tmp_path, expires_in_seconds=60)  # below safety margin
    r = CredentialRefresher(host_home=tmp_path)

    async def _fake_refresh() -> RefreshOutcome:
        _write_creds(tmp_path, expires_in_seconds=3600)
        return RefreshOutcome.REFRESHED

    r.backend.refresh = _fake_refresh  # type: ignore[assignment]
    agent = tmp_path / "agent-x"
    agent.mkdir()
    r.register_agent(agent)

    assert await r.ensure_fresh() is True
    assert (agent / ".claude" / ".credentials.json").exists()


@pytest.mark.asyncio
async def test_ensure_fresh_does_not_fan_out_when_refresh_fails(tmp_path):
    """Refresh failure path returns False before any fan-out — agents
    don't get stamped with stale/empty creds."""
    _write_creds(tmp_path, expires_in_seconds=-10)
    r = CredentialRefresher(host_home=tmp_path)

    async def _fake_refresh() -> RefreshOutcome:
        return RefreshOutcome.FAILED

    r.backend.refresh = _fake_refresh  # type: ignore[assignment]
    agent = tmp_path / "agent-y"
    agent.mkdir()
    r.register_agent(agent)

    sync_calls = 0

    def _spy_sync_to_agent(home):
        nonlocal sync_calls
        sync_calls += 1

    r.backend.sync_to_agent = _spy_sync_to_agent  # type: ignore[assignment]
    assert await r.ensure_fresh() is False
    assert sync_calls == 0


# ── AUTH_FAILED classification + propagation ─────────────────────────


def test_classify_failed_refresh_detects_401_as_auth_failed():
    from puffo_agent.portal.credential_refresh import _classify_failed_refresh

    outcome = _classify_failed_refresh(
        out_tail="", err_tail='API Error: 401 {"type":"authentication_error"}',
        rc=1, elapsed=1.2, log_prefix="test",
    )
    assert outcome is RefreshOutcome.AUTH_FAILED


def test_classify_failed_refresh_detects_invalid_grant_as_auth_failed():
    from puffo_agent.portal.credential_refresh import _classify_failed_refresh

    outcome = _classify_failed_refresh(
        out_tail="", err_tail="OAuth refresh: invalid_grant",
        rc=1, elapsed=1.2, log_prefix="test",
    )
    assert outcome is RefreshOutcome.AUTH_FAILED


def test_classify_failed_refresh_prefers_auth_failed_over_rate_limit():
    """When both markers are present (Anthropic sometimes returns 401
    with rate-limit-adjacent wording on rotated-and-revoked tokens),
    AUTH_FAILED wins so the DM path fires instead of the fast-retry."""
    from puffo_agent.portal.credential_refresh import _classify_failed_refresh

    outcome = _classify_failed_refresh(
        out_tail="",
        err_tail=(
            "API Error: 401 authentication_error "
            'rate_limit_error "type":"rate_limit_error"'
        ),
        rc=1, elapsed=1.2, log_prefix="test",
    )
    assert outcome is RefreshOutcome.AUTH_FAILED


def test_propagate_auth_failed_does_not_bump_streak(tmp_path):
    """AUTH_FAILED skips the consecutive_non_success streak so a later
    RATE_LIMITED / FAILED outcome starts fresh at 1, not N+1."""
    _write_creds(tmp_path, expires_in_seconds=7200)
    r = CredentialRefresher(host_home=tmp_path)
    r._consecutive_non_success = 0

    r._propagate_outcome(RefreshOutcome.AUTH_FAILED)
    assert r._consecutive_non_success == 0

    r._propagate_outcome(RefreshOutcome.FAILED)
    assert r._consecutive_non_success == 1
