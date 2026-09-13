"""Native Windows regressions for archive integrity and CLI tree shutdown."""

import asyncio
import ctypes
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock

import psutil
import pytest

from puffo_agent.portal import daemon
from puffo_agent.agent.harness.drivers.codex import CodexAppServerDriver
from puffo_agent.agent.harness.support.subprocess_io import process_group_spawn_kwargs

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows filesystem/process semantics")


@pytest.mark.asyncio
@windows_only
async def test_archive_retries_locked_long_paths_without_copying(tmp_path):
    """A lock must fail promptly without partial copies; unlocking permits retry."""
    src, dest = tmp_path / "agent", tmp_path / "archived-agent"
    leaf = src / ("x" * 100) / ("y" * 100) / "history.json"
    extended = "\\\\?\\" + str(leaf)
    os.makedirs(os.path.dirname(extended))
    Path(extended).write_bytes(b"history")
    provider_tmp = src / ".codex" / "tmp"
    provider_tmp.mkdir(parents=True)
    (provider_tmp / "state").write_bytes(b"provider state")
    await daemon._drain_codex_tmp(src)
    assert (provider_tmp / "state").read_bytes() == b"provider state"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(extended, 0x80000000, 4, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        error = await asyncio.wait_for(daemon._retry_move(src, dest), 2)
        assert isinstance(error, PermissionError)
        assert src.exists()
        assert not dest.exists()
    finally:
        kernel.CloseHandle(handle)
    assert await daemon._retry_move(src, dest) is None
    assert not src.exists()
    assert Path("\\\\?\\" + str(dest / leaf.relative_to(src))).read_bytes() == b"history"


@pytest.mark.asyncio
@windows_only
async def test_archive_collision_preserves_both_directories(tmp_path):
    """A timestamp collision must never erase an existing archive."""
    src, dest = tmp_path / "agent", tmp_path / "archive"
    src.mkdir()
    dest.mkdir()
    (src / "history").write_text("new")
    (dest / "history").write_text("old")
    assert await daemon._retry_move(src, dest) is not None
    assert (src / "history").read_text() == "new"
    assert (dest / "history").read_text() == "old"


@pytest.mark.asyncio
@windows_only
async def test_codex_close_reaps_launcher_descendant(tmp_path):
    """Closing a CLI wrapper must release its native child's file handles."""
    script = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        "print(p.pid,flush=True); time.sleep(120)"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **process_group_spawn_kwargs(),
    )
    child = psutil.Process(int(await asyncio.wait_for(proc.stdout.readline(), 5)))
    driver = CodexAppServerDriver()
    driver._proc = proc
    try:
        await asyncio.wait_for(driver.close(), 8)
        assert not child.is_running()
    finally:
        if child.is_running():
            child.kill()
        if proc.returncode is None:
            proc.kill()
        await asyncio.wait_for(proc.wait(), 5)


@pytest.mark.asyncio
async def test_failed_archive_never_reports_completed(monkeypatch, tmp_path):
    """An accepted archive flag is not proof that the local move succeeded."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    cfg = daemon.AgentConfig(id="agent")
    cfg.save()
    instance = daemon.Daemon(daemon.DaemonConfig())
    instance._stop_worker = AsyncMock()
    report = AsyncMock()
    monkeypatch.setattr(daemon, "_report_lifecycle", report)
    monkeypatch.setattr(daemon, "_retry_move", AsyncMock(return_value=PermissionError("locked")))
    await instance._archive_on_flag("agent")
    report.assert_not_awaited()
    assert daemon.agent_dir("agent").exists()


@pytest.mark.asyncio
@windows_only
async def test_delete_removes_archived_long_paths(monkeypatch, tmp_path):
    """Moving to archived/ must not make subsequent deletion fail at MAX_PATH."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    cfg = daemon.AgentConfig(id="agent")
    cfg.save()
    leaf = daemon.agent_dir("agent") / ("x" * 100) / ("y" * 100) / "history"
    extended = "\\\\?\\" + str(leaf)
    os.makedirs(os.path.dirname(extended))
    Path(extended).write_bytes(b"history")
    instance = daemon.Daemon(daemon.DaemonConfig())
    instance._stop_worker = AsyncMock()
    await instance._delete_on_flag("agent")
    assert not daemon.agent_dir("agent").exists()
    assert list(daemon.archived_dir().iterdir()) == []
