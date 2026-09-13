"""Discovery must preserve CLI state, existing folders, and operator isolation."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from puffo_agent.portal.control import lingtai_discovery as discovery


@pytest.mark.asyncio
async def test_discovery_cli_contract_and_hidden_root(tmp_path, monkeypatch):
    """A hidden project root and null workspace must produce an importable row."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "daemon"))
    root = tmp_path / "project"
    agent = root / ".lingtai" / "writer"
    agent.mkdir(parents=True)
    (agent / "init.json").write_text("{}")
    executable = tmp_path / "lingtai-agent.py"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "assert sys.argv[1:3] == ['puffo-v0', 'discover']\n"
        "assert '--registry' in sys.argv and '--json' in sys.argv\n"
        f"rows = [dict(agent_dir={str(agent)!r}, display_name='writer', workspace=None, "
        "status='available', runtime_id=None)] "
        f"if sys.argv[sys.argv.index('--root') + 1] == {str(root / '.lingtai')!r} else []\n"
        "print(json.dumps({'runtimes': rows}))\n"
    )
    executable.chmod(0o700)
    # Run the Python CLI fixture explicitly; Windows does not execute shebangs.
    create_process = discovery.asyncio.create_subprocess_exec
    async def launch_fixture(program, *args, **kwargs):
        assert Path(program) == Path(sys.executable).resolve()
        return await create_process(program, str(executable), *args, **kwargs)
    monkeypatch.setattr(discovery.asyncio, "create_subprocess_exec", launch_fixture)
    monkeypatch.setattr(discovery, "_known_paths", lambda operator: ([], [], []))
    monkeypatch.setattr(discovery, "_executable_paths", lambda known: [])
    result = await discovery.discover_lingtai(
        {"executable": sys.executable, "root": str(root)}, operator="owner",
    )
    assert result["warnings"] == []
    assert result["agents"][0]["workspace"] == str(agent)
    assert result["agents"][0]["status"] == "available"
    assert not (tmp_path / "daemon").exists(), "discovery must not provision or create a registry"


def test_default_inventory_uses_only_owned_associations(tmp_path, monkeypatch):
    """Another operator's executable and location must not leak into defaults."""
    owned, other = tmp_path / "mine", tmp_path / "other"
    monkeypatch.setattr(discovery, "discover_agents", lambda: ["mine", "other"])
    monkeypatch.setattr(discovery, "is_owner", lambda agent_id, operator: agent_id == "mine")
    monkeypatch.setattr(discovery, "_registry_entries", lambda: {
        "runtime-mine": {"agent_dir": str(owned)},
        "runtime-other": {"agent_dir": str(other)},
    })
    def config(agent_id, *, allow_invalid_runtime):
        assert allow_invalid_runtime
        assert agent_id == "mine"
        return SimpleNamespace(
            runtime=SimpleNamespace(harness_command=[
                str(owned / "lingtai-agent"), "acp", "--profile", "puffo-v1", "--runtime-id", "runtime-mine",
            ]), resolve_workspace_dir=lambda: owned,
        )
    monkeypatch.setattr(discovery.AgentConfig, "load", config)
    executables, roots, warnings = discovery._known_paths("operator")
    assert executables == [owned / "lingtai-agent"]
    assert owned in roots and owned / ".lingtai" in roots
    assert other not in roots


@pytest.mark.asyncio
async def test_missing_pairing_does_not_scan(monkeypatch):
    """Machine inventory must not be callable without an authenticated operator."""
    def forbidden(*args):
        pytest.fail("filesystem scan ran without a pairing")
    monkeypatch.setattr(discovery, "_known_paths", forbidden)
    assert not (await discovery.discover_lingtai({}, operator=None))["ok"]


def test_candidate_keeps_binding_state_and_rejects_outside_root(tmp_path):
    """Bound candidates cannot silently become available or escape the selected root."""
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "init.json").write_text("{}")
    row = dict(agent_dir=str(agent), display_name="agent", workspace=str(agent),
               status="bound", runtime_id="existing")
    assert discovery._normalize(row, tmp_path)["runtime_id"] == "existing"
    assert discovery._normalize(row, tmp_path)["status"] == "bound"
    row.update(status="stale_binding", workspace=str(tmp_path / "missing"))
    assert discovery._normalize(row, tmp_path)["status"] == "stale_binding"
    with pytest.raises(ValueError, match="outside"):
        discovery._normalize(row, tmp_path / "different")


def test_inventory_fits_server_result_budget():
    """Many candidates must not cause the server to replace the whole result."""
    result = discovery._bounded_result({
        "ok": True, "executable": "/bin/lingtai-agent", "executables": [],
        "roots": [], "warnings": [], "agents": [{"agent_dir": "x" * 2000} for _ in range(50)],
    })
    assert len(json.dumps(result).encode()) < 16 * 1024
    assert result["agents"]
    assert "results_truncated" in result["warnings"]
    assert result["partial"] and result["truncated"]


def test_invalid_owned_config_does_not_abort_inventory(monkeypatch):
    monkeypatch.setattr(discovery, "discover_agents", lambda: ["broken", "unfinished"])
    monkeypatch.setattr(discovery, "is_owner", lambda *args: True)
    monkeypatch.setattr(discovery, "_registry_entries", lambda: {})
    def config(agent_id, *, allow_invalid_runtime):
        assert allow_invalid_runtime
        if agent_id == "broken":
            raise RuntimeError("malformed harness_command")
        return SimpleNamespace(runtime=SimpleNamespace(harness_command=[]))
    monkeypatch.setattr(discovery.AgentConfig, "load", config)
    executables, roots, warnings = discovery._known_paths("owner")
    assert executables == []
    assert warnings == ["invalid_candidate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("root_count, executable_count", [(17, 1), (1, 9)])
async def test_search_scope_limits_are_explicit(tmp_path, monkeypatch, root_count, executable_count):
    roots = [tmp_path / str(i) for i in range(root_count)]
    for root in roots:
        root.mkdir()
    monkeypatch.setattr(discovery, "_known_paths", lambda operator: ([], roots, []))
    monkeypatch.setattr(discovery, "_executable_paths", lambda known: [f"/bin/cli{i}" for i in range(executable_count)])
    async def query(*args):
        return []
    monkeypatch.setattr(discovery, "_query", query)
    result = await discovery.discover_lingtai({}, operator="owner")
    assert result["ok"] and result["partial"] and result["truncated"]
    assert result["warnings"] == ["results_truncated"]
    assert len(result["roots"]) <= 16 and len(result["executables"]) <= 8


@pytest.mark.asyncio
@pytest.mark.parametrize("root", ["relative", "/missing-lingtai-discovery-test-folder"])
async def test_invalid_explicit_root_returns_actionable_error(monkeypatch, root):
    monkeypatch.setattr(discovery, "_known_paths", lambda operator: ([], [], []))
    monkeypatch.setattr(discovery, "_executable_paths", lambda known: [])
    result = await discovery.discover_lingtai({"root": root}, operator="owner")
    assert result["ok"] is False
    assert result["error"]


@pytest.mark.parametrize("document, expected, error", [
    ('{"manifest":{"agent_name":"Source Name"}}', "Source Name", None),
    ('{"manifest":{}}', None, None),
    ('not json', None, "source_unreadable"),
    ('{"manifest":{"agent_name":""}}', None, None),
    (' ' * (1024 * 1024 + 1), None, "source_unreadable"),
], ids=['valid', 'missing', 'malformed', 'empty', 'oversize'])
def test_candidate_metadata_never_substitutes_directory_label(tmp_path, document, expected, error):
    """Missing/invalid source identity stays visible but cannot become a basename import."""
    directory = tmp_path / "misleading-name"
    directory.mkdir()
    (directory / "init.json").write_text(document)
    row = discovery._normalize({"agent_dir": str(directory), "display_name": "CLI Label",
                                "status": "available"}, tmp_path)
    assert row["agent_name"] == expected
    assert row["profile_read_error"] == error
    assert row["name_source_file"] == "init.json"
    assert row["import_display_name"] == (None if error else expected or "")
    assert row["description"] is None
    assert row["profile_source"] == "lingtai"
    assert row["source_dir_name"] == "misleading-name"
    assert "display_name" not in row


@pytest.mark.parametrize("source, expected, error", [
    ('{"agent_name":"Current"}', "Current", None),
    ('{"agent_name":null}', None, None),
    ('{"agent_name":""}', None, None),
    ('{}', None, None),
    ('bad json', None, "source_unreadable"),
    ('{"agent_name":42}', None, "source_unreadable"),
    (' ' * (1024 * 1024 + 1), None, "source_unreadable"),
], ids=['named', 'null', 'empty', 'missing-name', 'malformed', 'invalid-type', 'oversize'])
def test_existing_agent_metadata_is_exclusive_even_when_unnamed(tmp_path, source, expected, error):
    """Existing .agent.json must never silently fall back to stale init identity."""
    (tmp_path / "init.json").write_text('{"manifest":{"agent_name":"Stale Init"}}')
    (tmp_path / ".agent.json").write_text(source)
    row = discovery._normalize({"agent_dir": str(tmp_path), "display_name": "Directory Label",
                                "status": "available"}, tmp_path)
    assert row["agent_name"] == expected
    assert row["profile_read_error"] == error
    assert row["name_source_file"] == ".agent.json"
    assert row["import_display_name"] == (None if error else expected or "")


def test_agent_metadata_permission_error_is_not_absence(tmp_path, monkeypatch):
    """Unreadable current metadata cannot be replaced with a stale readable init name."""
    from puffo_agent.portal.control import lingtai_profile

    (tmp_path / "init.json").write_text('{"manifest":{"agent_name":"Stale Init"}}')
    (tmp_path / ".agent.json").write_text('{"agent_name":"Current"}')
    original_open = lingtai_profile.os.open
    def deny_source(path, flags):
        if path.name == ".agent.json":
            raise PermissionError("blocked")
        return original_open(path, flags)
    monkeypatch.setattr(lingtai_profile.os, "open", deny_source)
    row = discovery._normalize({"agent_dir": str(tmp_path), "display_name": "Label",
                                "status": "available"}, tmp_path)
    assert row["agent_name"] is None
    assert row["profile_read_error"] == "source_unreadable"
    assert row["import_display_name"] is None


def test_source_read_without_posix_open_flags(tmp_path, monkeypatch):
    """Windows must read source metadata without Unix-only open constants."""
    import os
    from types import SimpleNamespace
    from puffo_agent.portal.control import lingtai_profile

    portable_os = {key: value for key, value in vars(os).items()
                   if key not in {"O_NONBLOCK", "O_NOFOLLOW"}}
    monkeypatch.setattr(lingtai_profile, "os", SimpleNamespace(**portable_os))
    path = tmp_path / "source.json"
    path.write_bytes(b'{"agent_name":"Current"}\r\n')
    assert lingtai_profile._read_source_object(path) == {"agent_name": "Current"}


def test_source_replacement_during_open_is_rejected(tmp_path, monkeypatch):
    """A changed file must not become authoritative after the pre-open check."""
    import os
    from types import SimpleNamespace
    from puffo_agent.portal.control import lingtai_profile

    path, replacement = tmp_path / "source.json", tmp_path / "replacement.json"
    path.write_text('{"agent_name":"Original"}')
    replacement.write_text('{"agent_name":"Replacement"}')
    def replace_then_open(path, flags):
        os.replace(replacement, path)
        return os.open(path, flags)
    monkeypatch.setattr(lingtai_profile, "os", SimpleNamespace(
        **{**vars(os), "open": replace_then_open},
    ))
    with pytest.raises(ValueError, match="changed"):
        lingtai_profile._read_source_object(path)


@pytest.mark.parametrize("after_open", [False, True])
def test_disappearing_primary_never_imports_stale_init(tmp_path, monkeypatch, after_open):
    """Once observed, a primary source disappearing is an error, not absence."""
    from types import SimpleNamespace
    from puffo_agent.portal.control import lingtai_profile

    primary = tmp_path / ".agent.json"
    primary.write_text('{"agent_name":"Current"}')
    (tmp_path / "init.json").write_text('{"manifest":{"agent_name":"Stale"}}')
    opened = False
    def disappearing_open(path, flags):
        nonlocal opened
        if path == primary and not after_open:
            primary.unlink()
        fd = os.open(path, flags)
        opened = True
        return fd
    def disappearing_lstat(path):
        if path == primary and after_open and opened:
            # Windows cannot unlink this open CRT handle; inject that race at
            # the path lookup boundary while retaining real descriptor I/O.
            raise FileNotFoundError(path)
        return os.lstat(path)
    monkeypatch.setattr(lingtai_profile, "os", SimpleNamespace(
        **{**vars(os), "open": disappearing_open, "lstat": disappearing_lstat},
    ))
    profile = lingtai_profile.read_source_profile(tmp_path)
    assert profile.profile_read_error == "source_unreadable"
    assert profile.import_display_name is None
    assert profile.name_source_file == ".agent.json"


def test_agent_metadata_symlink_is_not_a_fallback(tmp_path):
    """Unsafe source indirection must not import a different identity or use init fallback."""
    (tmp_path / "init.json").write_text('{"manifest":{"agent_name":"Stale Init"}}')
    try:
        (tmp_path / ".agent.json").symlink_to(tmp_path / "missing")
    except OSError as exc:
        if os.name == "nt" and exc.winerror == 1314:
            pytest.skip("Windows account lacks symlink creation privilege")
        raise
    row = discovery._normalize({"agent_dir": str(tmp_path), "display_name": "Label",
                                "status": "available"}, tmp_path)
    assert row["profile_read_error"] == "source_unreadable"
    assert row["import_display_name"] is None


@pytest.mark.parametrize("primary_present", [False, True])
def test_corrupt_marker_prevents_only_absent_primary_fallback(tmp_path, primary_present):
    """LingTai quarantine must not turn a corrupt source into a stale init import."""
    (tmp_path / "init.json").write_text('{"manifest":{"agent_name":"Stale Init"}}')
    (tmp_path / ".agent.json.corrupt").write_text("corrupt previous metadata")
    if primary_present:
        (tmp_path / ".agent.json").write_text('{"agent_name":"Recovered"}')
    row = discovery._normalize({"agent_dir": str(tmp_path), "display_name": "Label",
                                "status": "available", "workspace": None}, tmp_path)
    assert row["workspace"] == str(tmp_path)
    assert row["agent_name"] == ("Recovered" if primary_present else None)
    assert row["profile_read_error"] == (None if primary_present else "source_unreadable")
    assert row["import_display_name"] == ("Recovered" if primary_present else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_directory_link", [False, True])
async def test_executable_folder_search_is_scoped_and_does_not_run_candidates(tmp_path, monkeypatch, with_directory_link):
    """The executable field must search its folder, never run discoveries or follow directory links."""
    root = tmp_path / "selected"
    binary = root / ".venv" / "bin" / "lingtai-agent"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "lingtai-agent").write_text("#!/bin/sh\nexit 99\n")
    (outside / "lingtai-agent").chmod(0o700)
    if with_directory_link:
        try:
            (root / "escape").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                pytest.skip("Windows account lacks symlink creation privilege")
            raise
    monkeypatch.setattr(discovery, "_known_paths", lambda operator: ([], [], []))
    monkeypatch.setattr(discovery, "_executable_paths", lambda known: ["/unrelated/lingtai-agent"])
    async def forbidden(*args):
        pytest.fail("binary search ran the candidate")
    monkeypatch.setattr(discovery, "_query", forbidden)
    result = await discovery.discover_lingtai({"executable_root": str(root)}, operator="owner")
    assert result["ok"] and result["executables"] == [str(binary)]
    assert result["searched_executable_root"] == str(root)
    assert result["agents"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["provision", "discover"])
@pytest.mark.parametrize("parent_exits", [False, True])
async def test_lingtai_cancel_closes_inherited_child_pipes(
    tmp_path, monkeypatch, operation, parent_exits,
):
    """Cancellation must finish when a CLI descendant keeps output pipes open."""
    import asyncio
    import os
    import sys
    from puffo_agent.portal.control import lingtai
    import psutil
    from puffo_agent.agent.harness.support.cleanup_errors import cleanup_errors

    if os.name == "nt" and parent_exits:
        pytest.skip("Windows taskkill cannot target a tree after its parent exits")
    ready = tmp_path / "child.pid"
    child = (
        "import os, pathlib, signal, time; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if os.name != "nt" else "")
        + f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        + ("" if parent_exits else "time.sleep(60)")
    )
    spawn = asyncio.create_subprocess_exec
    processes = []
    async def fixture_spawn(*args, **kwargs):
        if args[0] == "taskkill":
            return await spawn(*args, **kwargs)
        process = await spawn(sys.executable, "-c", parent, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_spawn)
    launch = lingtai.LingtaiLaunch(tmp_path / "cli", tmp_path, tmp_path,
                                   tmp_path / "registry.json", "test")
    task = asyncio.create_task(
        lingtai._command(launch, []) if operation == "provision"
        else discovery._query(str(launch.executable), tmp_path, launch.registry)
    )
    try:
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(.01)
            if parent_exits:
                while processes[0].returncode is None:
                    await asyncio.sleep(.01)
        child_process = psutil.Process(int(ready.read_text()))
        task.cancel("test cancellation")
        done, _ = await asyncio.wait({task}, timeout=11)
        assert task in done, "CLI cleanup hangs on a descendant's inherited pipe"
        with pytest.raises(asyncio.CancelledError, match="test cancellation") as cancelled:
            task.result()
        assert cleanup_errors(cancelled.value) == ()
        try:
            assert not child_process.is_running() or child_process.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass  # The child exited between the two process-state reads.
        assert processes[0]._transport.is_closing()
    finally:
        if ready.exists():
            try:
                psutil.Process(int(ready.read_text())).kill()
            except psutil.NoSuchProcess:
                pass
        for process in processes:
            if process.returncode is None:
                process.kill()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("name, rejected", [("a" * 60, False), ("a" * 61, True), ("名" * 20, False), ("名" * 21, True)])
def test_source_name_matches_server_utf8_byte_limit(tmp_path, name, rejected):
    """Discovery must not offer names that the profile PATCH will reject by byte length."""
    import json
    from puffo_agent.portal.control.lingtai_profile import validate_import_profile
    (tmp_path / "init.json").write_text("{}")
    (tmp_path / ".agent.json").write_text(json.dumps({"agent_name": name}))
    row = discovery._normalize({"agent_dir": str(tmp_path), "status": "available"}, tmp_path)
    assert row["profile_read_error"] == ("name_too_long" if rejected else None)
    assert row["import_display_name"] == (None if rejected else name)
    payload = {"runtime": {"lingtai": {"agent_name": name}}, "display_name": name,
               "profile": f"# {name}\n"}
    if rejected:
        with pytest.raises(ValueError, match="60 UTF-8 bytes"):
            validate_import_profile(payload, tmp_path)
    else:
        validate_import_profile(payload, tmp_path)
