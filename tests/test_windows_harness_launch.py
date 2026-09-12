"""Installed Windows shims must reach the interpreter at every launch boundary."""

from types import SimpleNamespace

import pytest

from puffo_agent.agent import cli_bin, opencode_auth, pi_auth
from puffo_agent.agent.harness.driver import RuntimeSpec
from puffo_agent.agent.harness.drivers import codex, pi


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex", "pi"])
async def test_windows_driver_launches_resolved_shim(harness, tmp_path, monkeypatch):
    """A resolved npm shim must not be passed directly to CreateProcess."""
    executable = tmp_path / "Program Files" / harness
    executable.parent.mkdir()
    executable.with_suffix(".cmd").touch()
    monkeypatch.setattr(cli_bin, "sys", SimpleNamespace(platform="win32"))
    seen = {}

    async def spawn(*argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return SimpleNamespace()

    module = codex if harness == "codex" else pi
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    driver = codex.CodexAppServerDriver() if harness == "codex" else pi.PiDriver()
    spec = RuntimeSpec(
        workspace_dir=str(tmp_path), executable=str(executable),
        launch_args=("--custom", "value with spaces"), environment={"SAFE": "1"},
    )
    await driver._start_process(spec)

    expected_tail = (
        ("--custom", "value with spaces", "app-server") if harness == "codex"
        else ("--mode", "rpc", "--custom", "value with spaces")
    )
    assert seen["argv"] == ("cmd.exe", "/c", str(executable.with_suffix(".cmd")), *expected_tail)
    assert seen["kwargs"]["env"] == {"SAFE": "1"}
    assert seen["kwargs"]["cwd"] == str(tmp_path)


@pytest.mark.parametrize("probe", ["pi_auth", "pi_models", "opencode_models"])
def test_windows_readiness_probe_launches_resolved_shim(probe, tmp_path, monkeypatch):
    """An installed shim must not turn readiness into credential_check_error."""
    executable = tmp_path / "Program Files" / ("opencode.ps1" if probe == "opencode_models" else "pi.cmd")
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(cli_bin, "sys", SimpleNamespace(platform="win32"))
    prefix = (
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(executable)]
        if probe == "opencode_models" else ["cmd.exe", "/c", str(executable)]
    )
    tail, output = {
        "pi_auth": (["auth", "check", "--provider", "anthropic", "--json", "--no-refresh"], '{"status":"ready","provider":"anthropic"}'),
        "pi_models": (["--list-models"], "provider model thinking\nanthropic example yes\n"),
        "opencode_models": (["models"], "opencode/example\n"),
    }[probe]

    def run(command, **kwargs):
        assert command == prefix + tail
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(pi_auth.subprocess, "run", run)
    if probe == "pi_auth":
        assert pi_auth.check_pi_auth(str(executable), provider="anthropic", config_dir=tmp_path).status == "ready"
    elif probe == "pi_models":
        assert pi_auth.list_pi_models(str(executable), config_dir=tmp_path)
    else:
        assert opencode_auth.list_opencode_models(str(executable)) == ("opencode/example",)
