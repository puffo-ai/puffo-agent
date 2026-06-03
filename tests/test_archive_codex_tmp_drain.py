"""Regression seal for _drain_codex_tmp + _retry_on_oserror."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from puffo_agent.portal.daemon import _drain_codex_tmp, _retry_on_oserror


def _seed_codex_tmp(root: Path) -> Path:
    nested = root / ".codex" / "tmp" / "arg0" / "codex-arg0AAA"
    nested.mkdir(parents=True)
    (nested / ".lock").write_text("", encoding="utf-8")
    (nested / "state.json").write_text("{}", encoding="utf-8")
    return root / ".codex" / "tmp"


@pytest.mark.asyncio
async def test_drain_removes_codex_tmp_on_clean_path(tmp_path: Path):
    codex_tmp = _seed_codex_tmp(tmp_path)
    assert codex_tmp.exists()
    await _drain_codex_tmp(tmp_path)
    assert not codex_tmp.exists()
    assert (tmp_path / ".codex").exists()


@pytest.mark.asyncio
async def test_drain_is_noop_when_codex_tmp_missing(tmp_path: Path):
    (tmp_path / ".codex").mkdir()
    await _drain_codex_tmp(tmp_path)
    assert (tmp_path / ".codex").exists()


@pytest.mark.asyncio
async def test_drain_is_noop_when_codex_dir_missing(tmp_path: Path):
    await _drain_codex_tmp(tmp_path)
    assert not (tmp_path / ".codex").exists()


@pytest.mark.asyncio
async def test_drain_retries_then_succeeds(monkeypatch, tmp_path: Path):
    codex_tmp = _seed_codex_tmp(tmp_path)

    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def flaky_rmtree(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "still locked", str(path))
        return real_rmtree(path, *args, **kwargs)

    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr("puffo_agent.portal.daemon.shutil.rmtree", flaky_rmtree)
    monkeypatch.setattr("puffo_agent.portal.daemon.asyncio.sleep", fast_sleep)

    await _drain_codex_tmp(tmp_path)

    assert calls["n"] == 2
    assert not codex_tmp.exists()


@pytest.mark.asyncio
async def test_drain_falls_back_to_ignore_errors_after_exhaustion(
    monkeypatch, tmp_path: Path,
):
    _seed_codex_tmp(tmp_path)

    calls = {"n": 0, "ignore_errors_seen": False}

    def always_fail(path, *args, **kwargs):
        calls["n"] += 1
        if kwargs.get("ignore_errors"):
            calls["ignore_errors_seen"] = True
            return
        raise PermissionError(13, "stuck", str(path))

    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr("puffo_agent.portal.daemon.shutil.rmtree", always_fail)
    monkeypatch.setattr("puffo_agent.portal.daemon.asyncio.sleep", fast_sleep)

    await _drain_codex_tmp(tmp_path)

    assert calls["n"] == 6
    assert calls["ignore_errors_seen"] is True


# ── _retry_on_oserror ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_on_oserror_returns_on_first_success():
    calls = {"n": 0}

    def succeed():
        calls["n"] += 1

    await _retry_on_oserror(succeed)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retry_on_oserror_succeeds_after_transient_lock(monkeypatch):
    """Models the codex App Server logs_*.sqlite lingering past
    subprocess termination: first call raises WinError 32 / EBUSY, the
    second succeeds."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "still locked")

    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        await real_sleep(0)

    monkeypatch.setattr("puffo_agent.portal.daemon.asyncio.sleep", fast_sleep)
    await _retry_on_oserror(flaky)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_on_oserror_reraises_after_attempts_exhausted(monkeypatch):
    """Caller's except block depends on getting the OSError back when
    the file is held longer than the retry window."""
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise PermissionError(13, "stuck")

    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        await real_sleep(0)

    monkeypatch.setattr("puffo_agent.portal.daemon.asyncio.sleep", fast_sleep)
    with pytest.raises(PermissionError):
        await _retry_on_oserror(always_fail)
    assert calls["n"] == 5


@pytest.mark.asyncio
async def test_retry_on_oserror_honors_custom_attempts(monkeypatch):
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise OSError("x")

    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        await real_sleep(0)

    monkeypatch.setattr("puffo_agent.portal.daemon.asyncio.sleep", fast_sleep)
    with pytest.raises(OSError):
        await _retry_on_oserror(always_fail, attempts=2, delay=0.1)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_on_oserror_does_not_swallow_non_oserror():
    """ValueError / TypeError etc. must NOT be retried — those signal a
    bug in the caller, not a Windows handle linger."""
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise ValueError("bug")

    with pytest.raises(ValueError):
        await _retry_on_oserror(bad)
    assert calls["n"] == 1


# ── _archive_on_flag retry wiring (integration) ───────────────────


@pytest.mark.asyncio
async def test_archive_on_flag_retries_locked_move(monkeypatch, tmp_path):
    """End-to-end: shutil.move fails once with the WinError 32
    signature, then succeeds. Archive completes; no spurious error log."""
    from puffo_agent.portal import daemon as daemon_mod

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    src = tmp_path / "agents" / "agent-x"
    src.mkdir(parents=True)
    (src / "agent.yml").write_text("id: agent-x\n", encoding="utf-8")

    real_move = shutil.move
    calls = {"n": 0}

    def flaky_move(s, d):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "still locked by codex App Server")
        return real_move(s, d)

    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        await real_sleep(0)

    monkeypatch.setattr("puffo_agent.portal.daemon.shutil.move", flaky_move)
    monkeypatch.setattr("puffo_agent.portal.daemon.asyncio.sleep", fast_sleep)

    class _StubDaemon:
        workers: dict = {}

        async def _stop_worker(self, _agent_id):
            return

    await daemon_mod.Daemon._archive_on_flag(_StubDaemon(), "agent-x")

    assert calls["n"] == 2
    archived_root = tmp_path / "archived"
    assert archived_root.exists()
    assert any(p.name.startswith("agent-x-ws-") for p in archived_root.iterdir())
    assert not src.exists()
