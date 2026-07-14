"""Usage-snapshot parsing + collection."""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

from puffo_agent.portal.control import usage_snapshot as us

SAMPLE = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current session: 41% used · resets Jul 14, 12:40am (America/Los_Angeles)\n"
    "Current week (all models): 9% used · resets Jul 20, 5pm (America/Los_Angeles)\n"
    "Current week (Fable): 3% used · resets Jul 20, 5pm (America/Los_Angeles)\n\n"
    "What's contributing to your limits usage?\n"
)


def test_parse_full_snapshot():
    out = us.parse_claude_usage(SAMPLE)
    assert out["session"] == {
        "used_pct": 41,
        "resets_at": "Jul 14, 12:40am (America/Los_Angeles)",
    }
    assert out["weekly"] == {
        "used_pct": 9,
        "resets_at": "Jul 20, 5pm (America/Los_Angeles)",
    }
    assert out["weekly_by_model"] == [
        {"model": "Fable", "used_pct": 3, "resets_at": "Jul 20, 5pm (America/Los_Angeles)"}
    ]


def test_parse_session_only():
    out = us.parse_claude_usage("Current session: 5% used · resets tomorrow")
    assert out == {"session": {"used_pct": 5, "resets_at": "tomorrow"}}


def test_parse_no_budget_line_is_none():
    assert us.parse_claude_usage("I don't have access to usage metrics.") is None
    assert us.parse_claude_usage("") is None


def test_machine_harnesses_dedupes_and_skips_broken(monkeypatch):
    monkeypatch.setattr(us, "discover_agents", lambda: ["a", "b", "c", "broken"])

    def _load(aid):
        if aid == "broken":
            raise ValueError("bad agent.yml")
        harness = "codex" if aid == "b" else "claude-code"
        return types.SimpleNamespace(runtime=types.SimpleNamespace(harness=harness))

    monkeypatch.setattr(us.AgentConfig, "load", staticmethod(_load))
    assert us.machine_harnesses() == {"claude-code", "codex"}


@pytest.mark.asyncio
async def test_run_claude_usage_returns_result_text(monkeypatch):
    class _Proc:
        async def communicate(self):
            return (json.dumps({"result": "budget text"}).encode(), b"")

    async def _exec(*a, **k):
        return _Proc()

    monkeypatch.setattr(us.asyncio, "create_subprocess_exec", _exec)
    assert await us._run_claude_usage("claude", Path(".")) == "budget text"


@pytest.mark.asyncio
async def test_run_claude_usage_timeout_is_none(monkeypatch):
    class _Proc:
        async def communicate(self):
            raise asyncio.TimeoutError

    async def _exec(*a, **k):
        return _Proc()

    monkeypatch.setattr(us.asyncio, "create_subprocess_exec", _exec)
    assert await us._run_claude_usage("claude", Path(".")) is None


@pytest.mark.asyncio
async def test_run_claude_usage_missing_binary_is_none(monkeypatch):
    async def _exec(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(us.asyncio, "create_subprocess_exec", _exec)
    assert await us._run_claude_usage("claude", Path(".")) is None


@pytest.mark.asyncio
async def test_run_claude_usage_bad_json_is_none(monkeypatch):
    class _Proc:
        async def communicate(self):
            return (b"not json", b"")

    async def _exec(*a, **k):
        return _Proc()

    monkeypatch.setattr(us.asyncio, "create_subprocess_exec", _exec)
    assert await us._run_claude_usage("claude", Path(".")) is None


@pytest.mark.asyncio
async def test_collect_snapshot_for_claude_code(monkeypatch):
    monkeypatch.setattr(us, "machine_harnesses", lambda: {"claude-code"})
    monkeypatch.setattr(us, "resolve_claude_bin", lambda: "claude")

    async def _run(_bin, _home):
        return SAMPLE

    monkeypatch.setattr(us, "_run_claude_usage", _run)
    snap = await us.collect_usage_snapshot(Path("."))
    assert set(snap) == {"claude-code"}
    assert snap["claude-code"]["session"]["used_pct"] == 41


@pytest.mark.asyncio
async def test_collect_snapshot_no_claude_harness_is_none(monkeypatch):
    monkeypatch.setattr(us, "machine_harnesses", lambda: {"hermes"})
    assert await us.collect_usage_snapshot(Path(".")) is None


@pytest.mark.asyncio
async def test_collect_snapshot_missing_binary_is_none(monkeypatch):
    monkeypatch.setattr(us, "machine_harnesses", lambda: {"claude-code"})
    monkeypatch.setattr(us, "resolve_claude_bin", lambda: None)
    assert await us.collect_usage_snapshot(Path(".")) is None


# ── codex rate-limits (app-server account/rateLimits/updated) ──────

def test_parse_codex_rate_limits_session_and_weekly():
    raw = {
        "primary": {"usedPercent": 4, "windowDurationMins": 300, "resetsAt": 1783405466},
        "secondary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": 1783969993},
    }
    assert us.parse_codex_rate_limits(raw) == {
        "session": {"used_pct": 4, "resets_at": 1783405466},
        "weekly": {"used_pct": 3, "resets_at": 1783969993},
    }


def test_parse_codex_rate_limits_weekly_only():
    raw = {
        "primary": {"usedPercent": 5, "windowDurationMins": 10080, "resetsAt": 1784489223},
        "secondary": None,
    }
    assert us.parse_codex_rate_limits(raw) == {
        "weekly": {"used_pct": 5, "resets_at": 1784489223}
    }


def test_parse_codex_rate_limits_empty_or_bad():
    assert us.parse_codex_rate_limits(None) is None
    assert us.parse_codex_rate_limits({}) is None
    assert us.parse_codex_rate_limits({"primary": {"windowDurationMins": 300}}) is None


@pytest.mark.asyncio
async def test_collect_snapshot_includes_codex_from_reporter(monkeypatch):
    from puffo_agent.portal.control import reporter as reporter_mod

    monkeypatch.setattr(us, "machine_harnesses", lambda: {"codex"})
    rep = reporter_mod.AgentStatusReporter()
    rep.record_codex_rate_limits(
        {"primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 111}}
    )
    monkeypatch.setattr(us, "get_reporter", lambda: rep, raising=False)
    monkeypatch.setattr(reporter_mod, "get_reporter", lambda: rep)
    snap = await us.collect_usage_snapshot(Path("."))
    assert snap == {"codex": {"session": {"used_pct": 7, "resets_at": 111}}}


def test_reporter_records_and_returns_codex_rate_limits():
    from puffo_agent.portal.control.reporter import AgentStatusReporter

    rep = AgentStatusReporter()
    assert rep.latest_codex_rate_limits() is None
    rep.record_codex_rate_limits({"primary": {"usedPercent": 1}})
    assert rep.latest_codex_rate_limits() == {"primary": {"usedPercent": 1}}
    rep.record_codex_rate_limits(None)  # ignored, keeps last
    assert rep.latest_codex_rate_limits() == {"primary": {"usedPercent": 1}}
