"""Moving a pre-attach LingTai binding into the resident's registry.

Agents imported before attach name ``<puffo home>/lingtai/runtime-registry.json``
in their argv. A running LingTai refuses an attach through any registry but
its own (``runtime_registry_mismatch``), so these agents could never attach.
The fake LingTai below keeps a registry as plain JSON, answers
``puffo-v0 discover`` / ``provision`` and ``acp-socket-path``, and records
every call, so the tests can see what Puffo asked LingTai to do.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from puffo_agent.agent.harness.drivers.acp_attach import AcpAttachDriver
from puffo_agent.portal import state
from puffo_agent.portal.control.lingtai import lingtai_registry_path
from puffo_agent.portal.control.lingtai_registry_move import (
    _with_registry, legacy_registry_path, move_to_resident_registry,
)

RUNTIME_ID = "puffo-0123456789abcdef"

_FAKE_LINGTAI = r'''
import json, os, sys
from pathlib import Path

here = Path(sys.argv[0])
with open(str(here) + ".calls", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")

def opt(name):
    args = sys.argv[1:]
    return args[args.index(name) + 1]

def load(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"runtimes": {}, "revoked": []}

if sys.argv[1] == "acp-socket-path":
    print((here.parent / "socket-path").read_text().strip())
    sys.exit(0)
if sys.argv[1:3] == ["puffo-v0", "discover"]:
    root = str(Path(opt("--root")).resolve())
    registry = load(opt("--registry"))
    rows = []
    for runtime_id, entry in registry["runtimes"].items():
        if entry["agent_dir"] == root:
            status = "revoked" if runtime_id in registry.get("revoked", []) else "bound"
            rows.append({"agent_dir": root, "runtime_id": runtime_id,
                         "status": status, "workspace": entry["workspace"]})
    if not rows:
        rows.append({"agent_dir": root, "runtime_id": None,
                     "status": "available", "workspace": None})
    print(json.dumps({"runtimes": rows}))
    sys.exit(0)
if sys.argv[1:3] == ["puffo-v0", "provision"]:
    path = Path(opt("--registry"))
    registry = load(path)
    runtime_id = opt("--runtime-id")
    if runtime_id in registry["runtimes"]:
        sys.stderr.write("error: runtime_id is already provisioned\n")
        sys.exit(1)
    registry["runtimes"][runtime_id] = {
        "agent_dir": str(Path(opt("--agent-dir")).resolve()),
        "workspace": str(Path(opt("--workspace")).resolve()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry))
    sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def puffo_home(tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LINGTAI_PUFFO_V0_REGISTRY", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: host))
    return tmp_path / "home"


@pytest.fixture
def lingtai(tmp_path, puffo_home):
    """An agent bound in the legacy registry, as a pre-attach import left it."""
    agent_dir = (tmp_path / "agent").resolve()
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}")
    workspace = (tmp_path / "work").resolve()
    workspace.mkdir()
    legacy = legacy_registry_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"runtimes": {RUNTIME_ID: {
        "agent_dir": str(agent_dir), "workspace": str(workspace)}}, "revoked": []}))
    exe = tmp_path / "bin" / "lingtai-agent"
    exe.parent.mkdir()
    exe.write_text(f"#!{sys.executable}\n{_FAKE_LINGTAI}")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    (exe.parent / "socket-path").write_text("/tmp/lingtai-move-test.sock")

    def calls() -> list[list[str]]:
        log = Path(str(exe) + ".calls")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def argv(registry: Path) -> list[str]:
        return [str(exe), "acp", "--profile", "puffo-v1",
                "--runtime-id", RUNTIME_ID, "--registry", str(registry)]

    return SimpleNamespace(exe=exe, agent_dir=agent_dir, workspace=workspace,
                           legacy=legacy, resident=lingtai_registry_path(),
                           calls=calls, argv=argv)


def _write_agent(agent_id: str, harness_command: list[str], **extra) -> None:
    path = state.agent_yml_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"id": agent_id, "runtime": {
        "kind": "cli-local", "harness": "acp", "harness_command": harness_command, **extra,
    }}))


def _saved_argv(agent_id: str) -> list[str]:
    return state.AgentConfig.load(agent_id).runtime.harness_command


def _provisions(lingtai) -> list[list[str]]:
    return [call for call in lingtai.calls() if call[:2] == ["puffo-v0", "provision"]]


@pytest.mark.asyncio
async def test_a_legacy_binding_moves_under_the_same_runtime_id(lingtai):
    old = lingtai.argv(lingtai.legacy)
    _write_agent("a1", old)

    moved = await move_to_resident_registry("a1", old)

    assert moved == lingtai.argv(lingtai.resident)
    assert _saved_argv("a1") == moved
    resident = json.loads(lingtai.resident.read_text())["runtimes"]
    assert resident == {RUNTIME_ID: {"agent_dir": str(lingtai.agent_dir),
                                     "workspace": str(lingtai.workspace)}}
    # The old entry stays, so an argv that still names it keeps starting.
    assert RUNTIME_ID in json.loads(lingtai.legacy.read_text())["runtimes"]


@pytest.mark.asyncio
async def test_a_revoked_legacy_binding_is_not_brought_back(lingtai):
    registry = json.loads(lingtai.legacy.read_text())
    registry["revoked"] = [RUNTIME_ID]
    lingtai.legacy.write_text(json.dumps(registry))
    old = lingtai.argv(lingtai.legacy)
    _write_agent("a1", old)

    with pytest.raises(ValueError, match="not active"):
        await move_to_resident_registry("a1", old)

    assert _provisions(lingtai) == []
    assert not lingtai.resident.exists()
    assert _saved_argv("a1") == old


@pytest.mark.asyncio
async def test_a_move_interrupted_before_saving_finishes_without_provisioning_twice(lingtai):
    old = lingtai.argv(lingtai.legacy)
    _write_agent("a1", old)
    await move_to_resident_registry("a1", old)
    _write_agent("a1", old)                    # as if the save never happened

    moved = await move_to_resident_registry("a1", old)

    assert moved == lingtai.argv(lingtai.resident)
    assert _saved_argv("a1") == moved
    assert len(_provisions(lingtai)) == 1


@pytest.mark.asyncio
async def test_an_existing_resident_binding_with_another_workspace_is_rejected(lingtai):
    old = lingtai.argv(lingtai.legacy)
    _write_agent("a1", old)
    await move_to_resident_registry("a1", old)
    _write_agent("a1", old)
    registry = json.loads(lingtai.resident.read_text())
    registry["runtimes"][RUNTIME_ID]["workspace"] = str(lingtai.workspace / "other")
    lingtai.resident.write_text(json.dumps(registry))
    before = lingtai.resident.read_bytes()

    with pytest.raises(ValueError, match="workspace"):
        await move_to_resident_registry("a1", old)

    assert _saved_argv("a1") == old
    assert lingtai.resident.read_bytes() == before
    assert len(_provisions(lingtai)) == 1


@pytest.mark.asyncio
async def test_an_argv_already_on_the_resident_registry_is_left_alone(lingtai):
    current = lingtai.argv(lingtai.resident)
    _write_agent("a1", current)

    assert await move_to_resident_registry("a1", current) == current
    assert lingtai.calls() == []


@pytest.mark.asyncio
async def test_an_argv_changed_by_someone_else_meanwhile_is_not_overwritten(lingtai):
    old = lingtai.argv(lingtai.legacy)
    theirs = old + ["--verbose"]
    _write_agent("a1", theirs)

    assert await move_to_resident_registry("a1", old) == old
    assert _saved_argv("a1") == theirs


def test_every_registry_option_is_rewritten_so_the_last_one_wins():
    new = Path("/r/new.json")
    argv = ["x", "--registry", "/old", "--registry=/old2", "--runtime-id", "id"]
    assert _with_registry(argv, new) == [
        "x", "--registry", "/r/new.json", "--registry=/r/new.json", "--runtime-id", "id"]


# ── wiring: the move happens before the attach probe ─────────────────


class _Outbox:
    def set_active_turn(self, *args, **kwargs) -> None:
        pass


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
def resident_socket(lingtai):
    import os
    import socket
    import tempfile

    directory = tempfile.mkdtemp(prefix="lt-", dir="/tmp")
    path = Path(directory) / "acp.sock"
    (lingtai.exe.parent / "socket-path").write_text(str(path))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    yield path
    server.close()
    path.unlink()
    os.rmdir(directory)


async def _bind(agent_id: str):
    from puffo_agent.portal.worker_run import StandardWorkerRun

    worker = SimpleNamespace(_adapter=None)
    prepared = SimpleNamespace(
        native_session_id="", harness_name="acp",
        preparer=SimpleNamespace(agent_id=agent_id, agent_cfg=state.AgentConfig.load(agent_id)),
        spec=SimpleNamespace(mcp_generation=""),
    )
    await StandardWorkerRun(worker)._bind_driver_runtime(_Outbox(), prepared, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("imported_attach", [True, False], ids=["imported-attach", "imported-spawn"])
async def test_a_legacy_agent_attaches_through_the_resident_registry(
    lingtai, resident_socket, adapter_capture, imported_attach,
):
    _write_agent("a1", lingtai.argv(lingtai.legacy), lingtai_attach=imported_attach)

    await _bind("a1")

    driver = adapter_capture["driver"]
    assert isinstance(driver, AcpAttachDriver)
    assert driver.target.registry == lingtai.resident
    assert _saved_argv("a1") == lingtai.argv(lingtai.resident)


@pytest.mark.asyncio
async def test_a_failed_move_still_starts_with_the_old_argv(
    lingtai, adapter_capture,
):
    registry = json.loads(lingtai.legacy.read_text())
    registry["revoked"] = [RUNTIME_ID]
    lingtai.legacy.write_text(json.dumps(registry))
    old = lingtai.argv(lingtai.legacy)
    _write_agent("a1", old)

    await _bind("a1")

    assert adapter_capture["driver"] is None      # no resident: Puffo starts LingTai
    assert _saved_argv("a1") == old
