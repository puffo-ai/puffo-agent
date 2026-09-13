"""``no_window_kwargs`` — windowless child spawns so a detached
``start --background`` daemon doesn't pop a console per claude/codex."""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from puffo_agent import _proc
from puffo_agent.agent import memory_git
from puffo_agent.agent.harness.drivers import claude_code as claude_code_driver
from puffo_agent.agent.harness.drivers import codex as codex_driver
from puffo_agent.agent.harness.drivers.claude_code import ClaudeCodeCliDriver
from puffo_agent.agent.harness.drivers.codex import CodexAppServerDriver
from puffo_agent.agent.harness.driver import RuntimeSpec


_CREATE_NO_WINDOW = 0x08000000


def _finished_process():
    stdout = asyncio.StreamReader()
    stderr = asyncio.StreamReader()
    stdout.feed_eof()
    stderr.feed_eof()
    return SimpleNamespace(
        stdin=object(), stdout=stdout, stderr=stderr, returncode=0
    )


@pytest.mark.skipif(
    not hasattr(subprocess, "CREATE_NO_WINDOW"),
    reason="CREATE_NO_WINDOW is Windows-only",
)
def test_no_window_kwargs_on_windows(monkeypatch):
    monkeypatch.setattr(_proc.os, "name", "nt")
    assert _proc.no_window_kwargs() == {
        "creationflags": subprocess.CREATE_NO_WINDOW
    }


def test_no_window_kwargs_off_windows(monkeypatch):
    monkeypatch.setattr(_proc.os, "name", "posix")
    assert _proc.no_window_kwargs() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_cli_drivers_request_windowless_child_processes(monkeypatch, provider):
    """A detached Windows daemon must not create one console per Driver."""
    captured = {}

    async def fake_exec(*_args, **kwargs):
        captured.update(kwargs)
        return _finished_process()

    module = claude_code_driver if provider == "claude" else codex_driver
    monkeypatch.setattr(
        module,
        "no_window_kwargs",
        lambda: {"creationflags": _CREATE_NO_WINDOW},
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    if provider == "claude":
        driver = ClaudeCodeCliDriver()
        await driver.open(RuntimeSpec("/workspace"))
    else:
        driver = CodexAppServerDriver()
        await driver._start_process(RuntimeSpec("/workspace"))

    assert captured["creationflags"] == _CREATE_NO_WINDOW
    await driver.close()


def test_memory_git_requests_a_windowless_child_process(monkeypatch, tmp_path):
    """Memory maintenance in a detached daemon must not flash a git console."""
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(
        memory_git,
        "no_window_kwargs",
        lambda: {"creationflags": _CREATE_NO_WINDOW},
    )
    monkeypatch.setattr(memory_git.subprocess, "run", fake_run)

    result = memory_git._run_git(tmp_path, ["init"], pin_repo=False)

    assert result is not None
    assert captured["creationflags"] == _CREATE_NO_WINDOW


@pytest.mark.parametrize("probe", ["pi-auth", "pi-models", "opencode-models", "windows-path"])
def test_background_readiness_probes_request_windowless_processes(monkeypatch, tmp_path, probe):
    """The heartbeat's native readiness checks and PATH discovery must not
    allocate a console on each poll of a detached Windows daemon."""
    from puffo_agent.agent import cli_bin, pi_auth, opencode_auth

    observed = []

    def run(command, **kwargs):
        observed.append(kwargs)
        output = '{"status":"ready","provider":"test"}' if probe == "pi-auth" else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    module = {"pi-auth": pi_auth, "pi-models": pi_auth,
              "opencode-models": opencode_auth, "windows-path": cli_bin}[probe]
    monkeypatch.setattr(module, "no_window_kwargs", lambda: {"creationflags": _CREATE_NO_WINDOW}, raising=False)
    monkeypatch.setattr(subprocess, "run", run)
    if probe == "pi-auth":
        pi_auth.check_pi_auth("pi", provider="test", config_dir=tmp_path)
    elif probe == "pi-models":
        pi_auth.list_pi_models("pi", config_dir=tmp_path)
    elif probe == "opencode-models":
        opencode_auth.list_opencode_models("opencode")
    else:
        cli_bin._windows_persistent_path()
    assert len(observed) == 1
    assert observed[0].get("creationflags", 0) & _CREATE_NO_WINDOW
