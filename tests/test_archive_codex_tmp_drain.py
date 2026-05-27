"""Regression seal for _drain_codex_tmp."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from puffo_agent.portal.daemon import _drain_codex_tmp


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
