"""Unit tests for the between-turns config-file mtime watcher
(`_process_config_mtime_reload` / `_reload_from_disk` in
`puffo_agent.portal.worker`).

The watcher polls ``profile.md`` / ``agent.yml`` mtimes at turn start and
funnels any forward movement into a single ``adapter.reload`` — reusing
the flag-path reload primitives without touching them. These tests pin
the six behaviours from the plan: changed→exactly-one-reload,
unchanged→none, mid-write→no-crash-keeps-prior, seeded-baseline→no
spurious first reload, backwards mtime→none, and agent.yml→one reload
plus the documented restart-map log.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from puffo_agent.portal import worker


class FakeAdapter:
    """Records every ``reload`` call so tests can assert the
    exactly-one-reload contract."""

    def __init__(self) -> None:
        self.reload_calls: list[str] = []

    async def reload(self, prompt, *, with_session: bool = False) -> None:
        self.reload_calls.append(prompt)


def _fake_rebuild(**kwargs) -> str:
    """Stand-in for ``_rebuild_managed_system_prompt``: derives the
    prompt from the profile file's contents so a rewrite produces a new
    prompt, and raises ``FileNotFoundError`` if the profile is missing
    (mimicking a mid-write read)."""
    return "REBUILT:" + Path(kwargs["profile_path"]).read_text(encoding="utf-8")


def _seed(profile_path: Path, agent_yml: Path) -> dict:
    """Start-time baseline snapshot, mirroring what ``_run`` seeds."""
    return {
        "profile": worker._stat_mtime_or_none(profile_path),
        "agent_yml": worker._stat_mtime_or_none(agent_yml),
    }


def _bump_mtime(path: Path, delta: float) -> None:
    """Force ``path``'s mtime to move by ``delta`` seconds (deterministic
    — no reliance on wall-clock resolution between writes)."""
    base = os.stat(path).st_mtime
    os.utime(path, (base + delta, base + delta))


async def _watch(
    *,
    profile_path: Path,
    agent_yml: Path,
    puffo,
    adapter,
    mtime_state: dict,
) -> None:
    await worker._process_config_mtime_reload(
        agent_id="agent-1",
        harness_name="claude-code",
        shared_path=Path("/nonexistent/shared"),
        profile_path=str(profile_path),
        memory_path="/nonexistent/mem",
        workspace_path="/nonexistent/ws",
        agent_yml_path=str(agent_yml),
        puffo=puffo,
        adapter=adapter,
        mtime_state=mtime_state,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A profile.md + agent.yml on disk, a fake adapter/puffo, a seeded
    baseline, and the rebuild function patched to the deterministic
    stand-in."""
    profile = tmp_path / "profile.md"
    profile.write_text("you are helpful\n", encoding="utf-8")
    agent_yml = tmp_path / "agent.yml"
    agent_yml.write_text("runtime:\n  harness: claude-code\n", encoding="utf-8")

    monkeypatch.setattr(worker, "_rebuild_managed_system_prompt", _fake_rebuild)

    adapter = FakeAdapter()
    puffo = SimpleNamespace(system_prompt="ORIGINAL")
    mtime_state = _seed(profile, agent_yml)
    return SimpleNamespace(
        profile=profile,
        agent_yml=agent_yml,
        adapter=adapter,
        puffo=puffo,
        mtime_state=mtime_state,
    )


@pytest.mark.asyncio
async def test_changed_profile_triggers_exactly_one_reload(env):
    """(a) profile.md mtime moves forward → exactly one reload, prompt
    rebuilt from disk, and a second unchanged check does not re-fire."""
    env.profile.write_text("you are VERY helpful\n", encoding="utf-8")
    _bump_mtime(env.profile, 10)

    await _watch(
        profile_path=env.profile,
        agent_yml=env.agent_yml,
        puffo=env.puffo,
        adapter=env.adapter,
        mtime_state=env.mtime_state,
    )

    assert len(env.adapter.reload_calls) == 1
    assert env.puffo.system_prompt == "REBUILT:you are VERY helpful\n"
    assert env.adapter.reload_calls[0] == env.puffo.system_prompt

    # Baseline advanced → a follow-up check with no change is a no-op.
    await _watch(
        profile_path=env.profile,
        agent_yml=env.agent_yml,
        puffo=env.puffo,
        adapter=env.adapter,
        mtime_state=env.mtime_state,
    )
    assert len(env.adapter.reload_calls) == 1


@pytest.mark.asyncio
async def test_unchanged_mtime_never_reloads(env):
    """(b) No file change across two checks → zero reloads."""
    await _watch(
        profile_path=env.profile,
        agent_yml=env.agent_yml,
        puffo=env.puffo,
        adapter=env.adapter,
        mtime_state=env.mtime_state,
    )
    assert env.adapter.reload_calls == []

    await _watch(
        profile_path=env.profile,
        agent_yml=env.agent_yml,
        puffo=env.puffo,
        adapter=env.adapter,
        mtime_state=env.mtime_state,
    )
    assert env.adapter.reload_calls == []
    assert env.puffo.system_prompt == "ORIGINAL"


@pytest.mark.asyncio
async def test_midwrite_failure_keeps_prior_and_reloads_after_recovery(env, monkeypatch):
    """(c) A rebuild that raises (partial read) must not crash, must not
    mutate the prompt, and must not advance the baseline — so once the
    rebuild recovers the same still-newer mtime reloads."""
    _bump_mtime(env.profile, 10)
    baseline_before = env.mtime_state["profile"]

    def _raise(**kwargs):
        raise FileNotFoundError("profile.md vanished mid-write")

    monkeypatch.setattr(worker, "_rebuild_managed_system_prompt", _raise)

    # Must not raise out of the watcher.
    await _watch(
        profile_path=env.profile,
        agent_yml=env.agent_yml,
        puffo=env.puffo,
        adapter=env.adapter,
        mtime_state=env.mtime_state,
    )
    assert env.adapter.reload_calls == []
    assert env.puffo.system_prompt == "ORIGINAL"  # prior config kept
    assert env.mtime_state["profile"] == baseline_before  # baseline NOT advanced

    # Rebuild recovers; the still-newer mtime now reloads.
    monkeypatch.setattr(worker, "_rebuild_managed_system_prompt", _fake_rebuild)
    await _watch(
        profile_path=env.profile,
        agent_yml=env.agent_yml,
        puffo=env.puffo,
        adapter=env.adapter,
        mtime_state=env.mtime_state,
    )
    assert len(env.adapter.reload_calls) == 1
    assert env.puffo.system_prompt.startswith("REBUILT:")


@pytest.mark.asyncio
async def test_seeded_baseline_no_spurious_first_reload(env):
    """(d) Baseline seeded at start from the same snapshot → the first
    between-turns check on untouched files does not reload."""
    # env.mtime_state was seeded exactly as _run does; files untouched.
    await _watch(
        profile_path=env.profile,
        agent_yml=env.agent_yml,
        puffo=env.puffo,
        adapter=env.adapter,
        mtime_state=env.mtime_state,
    )
    assert env.adapter.reload_calls == []


@pytest.mark.asyncio
async def test_backwards_mtime_no_reload(env):
    """(e) mtime moving backwards (truncate-then-rewrite landing an
    earlier stamp) → no reload; baseline re-anchors to the earlier
    value so it cannot loop."""
    _bump_mtime(env.profile, -10)

    await _watch(
        profile_path=env.profile,
        agent_yml=env.agent_yml,
        puffo=env.puffo,
        adapter=env.adapter,
        mtime_state=env.mtime_state,
    )
    assert env.adapter.reload_calls == []
    assert env.puffo.system_prompt == "ORIGINAL"
    # Re-anchored to the earlier mtime.
    assert env.mtime_state["profile"] == os.stat(env.profile).st_mtime


@pytest.mark.asyncio
async def test_agent_yml_change_reloads_and_logs_restart_map(env, caplog):
    """(f) agent.yml mtime moves forward → exactly one reload plus the
    documented restart-map log (runtime fields take effect on next
    restart; only profile/system-prompt is hot-applied)."""
    _bump_mtime(env.agent_yml, 10)

    with caplog.at_level(logging.INFO, logger="puffo_agent.portal.worker"):
        await _watch(
            profile_path=env.profile,
            agent_yml=env.agent_yml,
            puffo=env.puffo,
            adapter=env.adapter,
            mtime_state=env.mtime_state,
        )

    assert len(env.adapter.reload_calls) == 1
    restart_notes = [
        r.getMessage()
        for r in caplog.records
        if "restart" in r.getMessage().lower()
        and "runtime fields" in r.getMessage().lower()
    ]
    assert restart_notes, "expected a log noting runtime fields need a restart"
