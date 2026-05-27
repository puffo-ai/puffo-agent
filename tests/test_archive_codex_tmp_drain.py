"""PUF-archive-tmp: regression seal for ``_drain_codex_tmp``.

The codex CLI holds an exclusive lock on
``.codex/tmp/arg0/codex-<id>/.lock``. On Windows the file handle
can lag the subprocess exit by several hundred milliseconds, so a
shutil.move/rmtree firing immediately after worker shutdown sees
the .lock as locked and raises Permission denied. The helper
drains the throwaway tmp dir with brief retries before the outer
move/rmtree walks the tree."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from puffo_agent.portal.daemon import _drain_codex_tmp


def _seed_codex_tmp(root: Path) -> Path:
    """Lay down a representative ``.codex/tmp/arg0/codex-<id>/.lock``
    plus a sibling file so the helper has something non-trivial
    to walk."""
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
    """First rmtree raises (simulating a still-locked .lock); the
    second succeeds. Helper must not propagate the transient error."""
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
    """If every retry fails, the final call swallows errors so the
    caller's outer try/except can still attempt the surrounding
    shutil.move / shutil.rmtree (a permanently-stuck lock is rare
    and gets retried next reconciler tick anyway)."""
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

    # Should NOT raise.
    await _drain_codex_tmp(tmp_path)

    # 5 retries + 1 final ignore_errors call.
    assert calls["n"] == 6
    assert calls["ignore_errors_seen"] is True
