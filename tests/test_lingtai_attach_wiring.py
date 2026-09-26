"""Selecting attach mode for a LingTai agent, and finding its socket.

The driver itself is covered by test_acp_attach_driver.py. These tests cover
the step before it: which agents attach, and how Puffo gets from the argv
provision wrote to the running Agent's socket.
"""

from __future__ import annotations

import json
import stat
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from puffo_agent.agent.harness.drivers.acp_attach import AcpAttachDriver
from puffo_agent.portal import state
from puffo_agent.portal.control.lingtai import (
    LingtaiLaunch, resident_lingtai_available, resolve_attach_target,
)

RUNTIME_ID = "puffo-0123456789abcdef"


def _argv(exe: Path, registry: Path, *, profile: str = "puffo-v1") -> list[str]:
    return [str(exe), "acp", "--profile", profile,
            "--runtime-id", RUNTIME_ID, "--registry", str(registry)]


@pytest.fixture
def lingtai_install(tmp_path):
    """A registry naming one runtime, and an executable that answers
    ``acp-socket-path <dir>`` the way LingTai does: one line, derived from
    the directory it was given, so the test can see which directory Puffo
    asked about."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    registry = tmp_path / "registry" / "runtime-registry.json"
    registry.parent.mkdir()
    registry.write_text(json.dumps(
        {"runtimes": {RUNTIME_ID: {"agent_dir": str(agent_dir)}}}
    ))
    exe = tmp_path / "lingtai-agent"
    exe.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "mode = open(sys.argv[0] + '.mode').read().strip()\n"
        "if sys.argv[1] != 'acp-socket-path':\n"
        "    sys.exit(2)\n"
        "if mode == 'fail':\n"
        "    sys.stderr.write('no such agent\\n'); sys.exit(1)\n"
        "if mode == 'old':\n"
        "    sys.stderr.write(\"lingtai-agent: error: argument command: invalid choice: 'acp-socket-path'\\n\"); sys.exit(2)\n"
        "if mode == 'two':\n"
        "    print('/tmp/a.sock'); print('/tmp/b.sock'); sys.exit(0)\n"
        "if mode == 'relative':\n"
        "    print('a.sock'); sys.exit(0)\n"
        "if mode.startswith('path:'):\n"
        "    print(mode[5:]); sys.exit(0)\n"
        "print('/tmp/lingtai-acp-test/' + sys.argv[2].replace('/', '_') + '.sock')\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    Path(str(exe) + ".mode").write_text("ok")
    return SimpleNamespace(exe=exe, registry=registry, agent_dir=agent_dir,
                           mode=lambda m: Path(str(exe) + ".mode").write_text(m))


def _expected_socket(agent_dir: Path) -> Path:
    return Path("/tmp/lingtai-acp-test/" + str(agent_dir).replace("/", "_") + ".sock")


@pytest.mark.asyncio
async def test_resolves_the_socket_of_the_registered_directory(lingtai_install):
    target = await resolve_attach_target(_argv(lingtai_install.exe, lingtai_install.registry))

    assert target.socket_path == _expected_socket(lingtai_install.agent_dir)
    assert target.runtime_id == RUNTIME_ID
    assert target.registry == lingtai_install.registry


@pytest.mark.asyncio
async def test_repeated_options_resolve_like_argparse(lingtai_install, tmp_path):
    # LingTai's argparse keeps the last value, so Puffo must claim that one.
    argv = [str(lingtai_install.exe), "acp", "--profile", "puffo-v1",
            "--runtime-id", "puffo-stale", f"--runtime-id={RUNTIME_ID}",
            "--registry", str(tmp_path / "stale.json"),
            "--registry", str(lingtai_install.registry)]

    target = await resolve_attach_target(argv)

    assert target.runtime_id == RUNTIME_ID
    assert target.registry == lingtai_install.registry


@pytest.mark.asyncio
@pytest.mark.parametrize("argv_of", [
    lambda i: _argv(i.exe, i.registry, profile="puffo-v0"),
    lambda i: [str(i.exe), "acp", "--runtime-id", RUNTIME_ID, "--registry", str(i.registry)],
    lambda i: [str(i.exe), "acp", "--profile", "puffo-v1", "--registry", str(i.registry)],
    lambda i: [str(i.exe), "acp", "--profile", "puffo-v1", "--runtime-id", RUNTIME_ID],
    lambda i: [str(i.exe), "acp", "--profile", "puffo-v1", "--runtime-id", RUNTIME_ID,
               "--registry", "relative/registry.json"],
], ids=["puffo-v0", "no-profile", "no-runtime-id", "no-registry", "relative-registry"])
async def test_refuses_an_argv_that_does_not_name_one_v1_runtime(lingtai_install, argv_of):
    with pytest.raises(ValueError):
        await resolve_attach_target(argv_of(lingtai_install))


@pytest.mark.asyncio
@pytest.mark.parametrize("registry_text", [
    json.dumps({"runtimes": {}}),
    json.dumps({"runtimes": {RUNTIME_ID: {}}}),
    json.dumps({"runtimes": {RUNTIME_ID: {"agent_dir": "relative/dir"}}}),
    json.dumps({"runtimes": []}),
    "not json",
], ids=["unregistered", "no-dir", "relative-dir", "wrong-shape", "garbage"])
async def test_refuses_a_runtime_the_registry_does_not_place(lingtai_install, registry_text):
    lingtai_install.registry.write_text(registry_text)

    with pytest.raises(ValueError, match="registry"):
        await resolve_attach_target(_argv(lingtai_install.exe, lingtai_install.registry))


@pytest.mark.asyncio
async def test_missing_registry_is_refused(lingtai_install):
    lingtai_install.registry.unlink()

    with pytest.raises(ValueError, match="registry is unreadable"):
        await resolve_attach_target(_argv(lingtai_install.exe, lingtai_install.registry))


@pytest.mark.asyncio
async def test_lingtai_error_output_is_reported(lingtai_install):
    lingtai_install.mode("fail")

    with pytest.raises(ValueError, match="no such agent"):
        await resolve_attach_target(_argv(lingtai_install.exe, lingtai_install.registry))


@pytest.mark.asyncio
async def test_old_kernel_gets_upgrade_prompt_on_import_and_attach(lingtai_install, tmp_path):
    lingtai_install.mode("old")
    launch = LingtaiLaunch(
        executable=lingtai_install.exe, agent_dir=lingtai_install.agent_dir,
        workspace=tmp_path, registry=lingtai_install.registry, runtime_id=RUNTIME_ID,
    )

    with pytest.raises(ValueError, match="kernel 1.0.9 or newer is required"):
        await resident_lingtai_available(launch)
    with pytest.raises(ValueError, match="kernel 1.0.9 or newer is required"):
        await resolve_attach_target(_argv(lingtai_install.exe, lingtai_install.registry))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["two", "relative"])
async def test_refuses_output_that_is_not_one_absolute_path(lingtai_install, mode):
    lingtai_install.mode(mode)

    with pytest.raises(ValueError, match="one absolute path"):
        await resolve_attach_target(_argv(lingtai_install.exe, lingtai_install.registry))


# --- selection -------------------------------------------------------------


def _write_agent(agent_id: str, runtime: dict) -> None:
    path = state.agent_yml_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"id": agent_id, "runtime": runtime}))


def _lingtai_runtime(lingtai_install, **extra) -> dict:
    return {"kind": "cli-local", "harness": "acp",
            "harness_command": _argv(lingtai_install.exe, lingtai_install.registry),
            **extra}


@pytest.fixture
def puffo_home(tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: host))
    return tmp_path / "home"


def test_attach_is_off_unless_asked_for(puffo_home, lingtai_install):
    _write_agent("a1", _lingtai_runtime(lingtai_install))

    assert state.AgentConfig.load("a1").runtime.lingtai_attach is False


def test_attach_setting_survives_a_save(puffo_home, lingtai_install):
    _write_agent("a1", _lingtai_runtime(lingtai_install, lingtai_attach=True))
    cfg = state.AgentConfig.load("a1")
    assert cfg.runtime.lingtai_attach is True

    cfg.save()

    assert state.AgentConfig.load("a1").runtime.lingtai_attach is True
    assert asdict(cfg.runtime)["lingtai_attach"] is True


@pytest.mark.parametrize("value", ["true", 1, "yes"])
def test_attach_setting_must_be_a_boolean(puffo_home, lingtai_install, value):
    _write_agent("a1", _lingtai_runtime(lingtai_install, lingtai_attach=value))

    with pytest.raises(RuntimeError, match="lingtai_attach must be true or false"):
        state.AgentConfig.load("a1")


@pytest.mark.parametrize("runtime", [
    {"kind": "cli-local", "harness": "claude-code"},
    {"kind": "cli-local", "harness": "acp", "harness_command": ["/usr/bin/some-acp-agent"]},
    {"kind": "cli-local", "harness": "acp",
     "harness_command": ["/x/lingtai-agent", "acp", "--profile", "puffo-v0"]},
], ids=["claude-code", "generic-acp", "puffo-v0"])
def test_attach_is_refused_for_anything_but_a_lingtai_v1_runtime(puffo_home, runtime):
    _write_agent("a1", {**runtime, "lingtai_attach": True})

    with pytest.raises(RuntimeError, match="lingtai_attach requires"):
        state.AgentConfig.load("a1")


# --- binding ---------------------------------------------------------------


class _Outbox:
    def set_active_turn(self, *args, **kwargs) -> None:
        pass


def _bind(agent_cfg, harness_name="acp", preparer=None):
    from puffo_agent.portal.worker_run import StandardWorkerRun

    worker = SimpleNamespace(_adapter=None)
    prepared = SimpleNamespace(
        native_session_id="", harness_name=harness_name,
        preparer=preparer or SimpleNamespace(agent_id="a1", agent_cfg=agent_cfg),
        spec=SimpleNamespace(mcp_generation=""),
    )
    return worker, StandardWorkerRun(worker)._bind_driver_runtime(_Outbox(), prepared, {})


@pytest.fixture
def adapter_capture(monkeypatch):
    from puffo_agent.agent.harness.runtime import local_runtime

    seen: dict = {}

    def fake_adapter(prepared, **kwargs):
        seen.update(kwargs)
        return "adapter"

    monkeypatch.setattr(local_runtime, "build_local_runtime_adapter", fake_adapter)
    return seen


@pytest.fixture
def resident_socket(lingtai_install):
    """Point the fake LingTai at a short socket path (AF_UNIX caps its length)
    and hand the test a way to make a running Agent listen there."""
    import os
    import socket
    import tempfile

    directory = tempfile.mkdtemp(prefix="lt-", dir="/tmp")
    path = Path(directory) / "acp.sock"
    lingtai_install.mode("path:" + str(path))
    servers: list = []

    def listen() -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        servers.append(server)

    def stale() -> None:
        # A socket file left behind with nobody accepting on it.
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.close()

    yield SimpleNamespace(path=path, listen=listen, stale=stale)
    for server in servers:
        server.close()
    if path.exists():
        path.unlink()
    os.rmdir(directory)


@pytest.mark.asyncio
@pytest.mark.parametrize("imported_attach", [True, False], ids=["imported-attach", "imported-spawn"])
async def test_a_running_lingtai_is_attached_whatever_the_import_chose(
    puffo_home, lingtai_install, resident_socket, adapter_capture, imported_attach,
):
    # The mode is decided at each start: a LingTai the user opened after an
    # import that chose spawn is attached to all the same.
    _write_agent("a1", _lingtai_runtime(lingtai_install, lingtai_attach=imported_attach))
    resident_socket.listen()
    worker, bind = _bind(state.AgentConfig.load("a1"))

    await bind

    driver = adapter_capture["driver"]
    assert isinstance(driver, AcpAttachDriver)
    assert driver.target.socket_path == resident_socket.path
    assert driver.target.runtime_id == RUNTIME_ID
    assert worker._adapter == "adapter"


@pytest.mark.asyncio
@pytest.mark.parametrize("imported_attach", [True, False], ids=["imported-attach", "imported-spawn"])
async def test_no_running_lingtai_starts_one_whatever_the_import_chose(
    puffo_home, lingtai_install, resident_socket, adapter_capture, imported_attach,
):
    # Negative control for the test above: the same agent with no socket
    # leaves driver construction to the adapter, which spawns.
    _write_agent("a1", _lingtai_runtime(lingtai_install, lingtai_attach=imported_attach))
    _, bind = _bind(state.AgentConfig.load("a1"))

    await bind

    assert adapter_capture["driver"] is None


@pytest.mark.asyncio
async def test_attach_import_refuses_a_socket_nobody_answers(
    puffo_home, lingtai_install, resident_socket, adapter_capture,
):
    # A socket file with no listener may still belong to an Agent holding the
    # directory; starting a second LingTai there is what attach prevents.
    _write_agent("a1", _lingtai_runtime(lingtai_install, lingtai_attach=True))
    resident_socket.stale()
    _, bind = _bind(state.AgentConfig.load("a1"))

    with pytest.raises(ValueError, match="socket is unavailable"):
        await bind

    assert "driver" not in adapter_capture


@pytest.mark.asyncio
async def test_attach_import_refuses_an_unplaceable_runtime(puffo_home, lingtai_install, adapter_capture):
    _write_agent("a1", _lingtai_runtime(lingtai_install, lingtai_attach=True))
    cfg = state.AgentConfig.load("a1")
    lingtai_install.registry.unlink()
    _, bind = _bind(cfg)

    with pytest.raises(ValueError, match="registry"):
        await bind

    assert "driver" not in adapter_capture


@pytest.mark.asyncio
@pytest.mark.parametrize("break_probe", ["old-kernel", "no-registry"])
async def test_spawn_import_keeps_spawning_when_the_probe_cannot_run(
    puffo_home, lingtai_install, adapter_capture, break_probe,
):
    # Agents imported before attach existed, or on an older kernel, start
    # exactly as they did.
    _write_agent("a1", _lingtai_runtime(lingtai_install))
    if break_probe == "old-kernel":
        lingtai_install.mode("old")
    else:
        lingtai_install.registry.unlink()
    _, bind = _bind(state.AgentConfig.load("a1"))

    await bind

    assert adapter_capture["driver"] is None


@pytest.mark.asyncio
async def test_a_non_lingtai_acp_agent_is_never_probed(puffo_home, adapter_capture, monkeypatch):
    from puffo_agent.portal.control import lingtai as lingtai_control

    async def probe(_command):
        raise AssertionError("probed a non-LingTai agent")

    monkeypatch.setattr(lingtai_control, "running_lingtai_target", probe)
    _write_agent("a1", {"kind": "cli-local", "harness": "acp",
                        "harness_command": ["/usr/bin/some-acp-agent"]})
    _, bind = _bind(state.AgentConfig.load("a1"))

    await bind

    assert adapter_capture["driver"] is None


@pytest.mark.asyncio
async def test_attach_refuses_a_docker_runtime(puffo_home, lingtai_install, adapter_capture, monkeypatch):
    from puffo_agent.agent.harness.runtime import docker_runtime

    class _Docker:
        agent_id = "a1"

    monkeypatch.setattr(docker_runtime, "DockerRuntimePreparer", _Docker)
    _write_agent("a1", _lingtai_runtime(lingtai_install, lingtai_attach=True))
    docker = _Docker()
    docker.agent_cfg = state.AgentConfig.load("a1")
    _, bind = _bind(docker.agent_cfg, preparer=docker)

    with pytest.raises(RuntimeError, match="only on the cli-local acp harness"):
        await bind
