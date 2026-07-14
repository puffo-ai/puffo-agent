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


def _wall(epoch: int):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(epoch, ZoneInfo("America/Los_Angeles"))


def test_parse_full_snapshot():
    out = us.parse_claude_usage(SAMPLE)
    assert out["session"]["used_pct"] == 41
    assert out["weekly"]["used_pct"] == 9
    assert out["weekly_by_model"][0] == {
        "model": "Fable",
        "used_pct": 3,
        "resets_at": out["weekly"]["resets_at"],
    }
    # resets_at is an epoch that round-trips to the prose wall-clock.
    s = _wall(out["session"]["resets_at"])
    assert (s.month, s.day, s.hour, s.minute) == (7, 14, 0, 40)
    w = _wall(out["weekly"]["resets_at"])
    assert (w.month, w.day, w.hour) == (7, 20, 17)


def test_parse_session_only_with_unparseable_resets_omits_the_field():
    # "tomorrow" isn't the dated-tz format → resets_at is dropped, pct kept.
    out = us.parse_claude_usage("Current session: 5% used · resets tomorrow")
    assert out == {"session": {"used_pct": 5}}


def test_claude_resets_to_epoch_variants():
    assert (_wall(us._claude_resets_to_epoch("Jul 20, 5pm (America/Los_Angeles)")).hour) == 17
    midnight = _wall(us._claude_resets_to_epoch("Jul 14, 12:40am (America/Los_Angeles)"))
    assert (midnight.hour, midnight.minute) == (0, 40)
    noon = _wall(us._claude_resets_to_epoch("Jul 14, 12pm (America/Los_Angeles)"))
    assert noon.hour == 12
    # Every valid parse resolves to a near-future reset (year inferred).
    import time

    assert us._claude_resets_to_epoch("Jan 1, 9am (America/Los_Angeles)") >= time.time() - 86400


def test_claude_resets_to_epoch_bad_input_is_none():
    assert us._claude_resets_to_epoch("whenever") is None
    assert us._claude_resets_to_epoch("Jul 20, 5pm (Not/AZone)") is None
    assert us._claude_resets_to_epoch("") is None


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
async def test_collect_snapshot_codex_from_active_probe(monkeypatch):
    monkeypatch.setattr(us, "machine_harnesses", lambda: {"codex"})
    monkeypatch.setattr(us, "resolve_codex_bin", lambda: "codex")

    async def _probe(_bin, _home):
        return {"primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 111}}

    monkeypatch.setattr(us, "_probe_codex_rate_limits", _probe)
    snap = await us.collect_usage_snapshot(Path("."))
    assert snap == {"codex": {"session": {"used_pct": 7, "resets_at": 111}}}


@pytest.mark.asyncio
async def test_collect_snapshot_codex_falls_back_to_reporter_when_probe_fails(monkeypatch):
    from puffo_agent.portal.control import reporter as reporter_mod

    monkeypatch.setattr(us, "machine_harnesses", lambda: {"codex"})
    monkeypatch.setattr(us, "resolve_codex_bin", lambda: "codex")

    async def _probe(_bin, _home):
        return None  # spawn/turn failed

    monkeypatch.setattr(us, "_probe_codex_rate_limits", _probe)
    rep = reporter_mod.AgentStatusReporter()
    rep.record_codex_rate_limits(
        {"primary": {"usedPercent": 9, "windowDurationMins": 10080, "resetsAt": 222}}
    )
    monkeypatch.setattr(reporter_mod, "get_reporter", lambda: rep)
    snap = await us.collect_usage_snapshot(Path("."))
    assert snap == {"codex": {"weekly": {"used_pct": 9, "resets_at": 222}}}


@pytest.mark.asyncio
async def test_collect_snapshot_codex_omitted_when_probe_and_reporter_empty(monkeypatch):
    from puffo_agent.portal.control import reporter as reporter_mod

    monkeypatch.setattr(us, "machine_harnesses", lambda: {"codex"})
    monkeypatch.setattr(us, "resolve_codex_bin", lambda: "codex")

    async def _probe(_bin, _home):
        return None

    monkeypatch.setattr(us, "_probe_codex_rate_limits", _probe)
    monkeypatch.setattr(reporter_mod, "get_reporter", lambda: reporter_mod.AgentStatusReporter())
    assert await us.collect_usage_snapshot(Path(".")) is None


# ── codex active probe: JSON-RPC drive over a fake app-server ───────

class _FakeStdin:
    def __init__(self):
        self.written: list[bytes] = []

    def write(self, b: bytes) -> None:
        self.written.append(b)

    async def drain(self) -> None:
        pass


class _FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, lines: list[bytes], terminate_raises: bool = False):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self._terminate_raises = terminate_raises

    def terminate(self) -> None:
        if self._terminate_raises:
            raise ProcessLookupError()


def _frame(obj: dict) -> bytes:
    return (__import__("json").dumps(obj) + "\n").encode()


@pytest.mark.asyncio
async def test_drive_codex_probe_sends_turn_and_returns_ratelimits():
    limits = {"primary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 111}}
    proc = _FakeProc([
        _frame({"id": 1, "result": {}}),
        _frame({"id": 2, "result": {"thread": {"id": "thr_1"}}}),
        _frame({"method": "account/rateLimits/updated", "params": {"rateLimits": limits}}),
    ])
    assert await us._drive_codex_probe(proc) == limits
    # The throwaway turn was fired against the started thread.
    sent = b"".join(proc.stdin.written).decode()
    assert '"turn/start"' in sent and '"thr_1"' in sent and "ignore this message" in sent


@pytest.mark.asyncio
async def test_drive_codex_probe_no_thread_id_is_none():
    proc = _FakeProc([_frame({"id": 2, "result": {"model": "x"}})])
    assert await us._drive_codex_probe(proc) is None
    assert proc.stdin.written and b"turn/start" not in b"".join(proc.stdin.written)


@pytest.mark.asyncio
async def test_drive_codex_probe_stream_ends_before_frame_is_none():
    proc = _FakeProc([_frame({"id": 2, "result": {"thread": {"id": "t"}}})])
    # Turn is sent, but the app-server closes before the budget frame arrives.
    assert await us._drive_codex_probe(proc) is None


@pytest.mark.asyncio
async def test_drive_codex_probe_skips_malformed_line():
    limits = {"primary": {"usedPercent": 3, "windowDurationMins": 300}}
    proc = _FakeProc([
        b"not json at all\n",
        _frame({"id": 2, "result": {"thread": {"id": "t"}}}),
        _frame({"method": "account/rateLimits/updated", "params": {"rateLimits": limits}}),
    ])
    assert await us._drive_codex_probe(proc) == limits


@pytest.mark.asyncio
async def test_probe_codex_rate_limits_spawn_failure_is_none(monkeypatch):
    async def _boom(*a, **k):
        raise FileNotFoundError("no codex")

    monkeypatch.setattr(us.asyncio, "create_subprocess_exec", _boom)
    assert await us._probe_codex_rate_limits("codex", Path(".")) is None


@pytest.mark.asyncio
async def test_probe_codex_rate_limits_happy_spawns_and_drives(monkeypatch):
    limits = {"primary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 111}}
    proc = _FakeProc([
        _frame({"id": 2, "result": {"thread": {"id": "t"}}}),
        _frame({"method": "account/rateLimits/updated", "params": {"rateLimits": limits}}),
    ])

    async def _spawn(*a, **k):
        return proc

    monkeypatch.setattr(us.asyncio, "create_subprocess_exec", _spawn)
    assert await us._probe_codex_rate_limits("codex", Path(".")) == limits


@pytest.mark.asyncio
async def test_probe_codex_rate_limits_timeout_terminates_and_is_none(monkeypatch):
    # terminate raises ProcessLookupError → the finally swallows it.
    proc = _FakeProc([], terminate_raises=True)

    async def _spawn(*a, **k):
        return proc

    async def _hang(_proc):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(us.asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(us, "_drive_codex_probe", _hang)
    assert await us._probe_codex_rate_limits("codex", Path(".")) is None


def test_extract_thread_id_variants():
    assert us._extract_thread_id({"thread": {"id": "a"}}) == "a"
    assert us._extract_thread_id({"threadId": "b"}) == "b"
    assert us._extract_thread_id({"thread": "c"}) == "c"
    assert us._extract_thread_id({"nope": 1}) is None
    assert us._extract_thread_id("x") is None


def test_reporter_records_and_returns_codex_rate_limits():
    from puffo_agent.portal.control.reporter import AgentStatusReporter

    rep = AgentStatusReporter()
    assert rep.latest_codex_rate_limits() is None
    rep.record_codex_rate_limits({"primary": {"usedPercent": 1}})
    assert rep.latest_codex_rate_limits() == {"primary": {"usedPercent": 1}}
    rep.record_codex_rate_limits(None)  # ignored, keeps last
    assert rep.latest_codex_rate_limits() == {"primary": {"usedPercent": 1}}


def test_parse_codex_rate_limits_omits_missing_resets_at():
    raw = {"primary": {"usedPercent": 6, "windowDurationMins": 300, "resetsAt": None}}
    assert us.parse_codex_rate_limits(raw) == {"session": {"used_pct": 6}}
