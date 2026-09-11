"""Discovery must preserve CLI state, existing folders, and operator isolation."""

import json
import sys
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
    executable = tmp_path / "lingtai-agent"
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
    monkeypatch.setattr(discovery, "_known_paths", lambda operator: ([], [], []))
    monkeypatch.setattr(discovery, "_executable_paths", lambda known: [])
    result = await discovery.discover_lingtai(
        {"executable": str(executable), "root": str(root)}, operator="owner",
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
                "/mine/lingtai-agent", "acp", "--profile", "puffo-v1", "--runtime-id", "runtime-mine",
            ]), resolve_workspace_dir=lambda: owned,
        )
    monkeypatch.setattr(discovery.AgentConfig, "load", config)
    executables, roots, warnings = discovery._known_paths("operator")
    assert str(executables[0]) == "/mine/lingtai-agent"
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
    assert row["profile_name_source"] == "init.json"
    assert row["import_display_name"] == (None if error else expected or "Unnamed Agent")
    assert row["description"] is None
    assert row["profile_source"] == "lingtai"
    assert row["display_name"] == "CLI Label"


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
    assert row["profile_name_source"] == ".agent.json"
    assert row["import_display_name"] == (None if error else expected or "Unnamed Agent")


def test_agent_metadata_permission_error_is_not_absence(tmp_path, monkeypatch):
    """Unreadable current metadata cannot be replaced with a stale readable init name."""
    from puffo_agent.portal.control import lingtai_profile

    (tmp_path / "init.json").write_text('{"manifest":{"agent_name":"Stale Init"}}')
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


def test_agent_metadata_symlink_is_not_a_fallback(tmp_path):
    """Unsafe source indirection must not import a different identity or use init fallback."""
    (tmp_path / "init.json").write_text('{"manifest":{"agent_name":"Stale Init"}}')
    (tmp_path / ".agent.json").symlink_to(tmp_path / "missing")
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
