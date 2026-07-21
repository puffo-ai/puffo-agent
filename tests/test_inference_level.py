"""PUF-373: the web's provider-agnostic ``inference_level`` selector wires
through to a Codex agent's ``model_reasoning_effort`` in config.toml.

Per Vase (2026-07-16) the enum is harness-specific: Codex offers
low/medium/high, Claude adds xhigh. The daemon consumes one shared field
and drops levels Codex can't use (xhigh) at config.toml-write time.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from puffo_agent.mcp.config import (
    INFERENCE_LEVELS,
    REASONING_EFFORTS,
    write_codex_mcp_config,
)


def _doc(tmp_path: Path, **kwargs) -> dict:
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(dest, **kwargs)
    return tomllib.loads(dest.read_text(encoding="utf-8"))


def test_inference_levels_match_web_enum():
    # Mirrors AgentCoreInferenceLevel in the web app.
    assert INFERENCE_LEVELS == ("low", "medium", "high", "xhigh")


def test_codex_valid_levels_are_emitted(tmp_path):
    for level in ("low", "medium", "high"):
        assert _doc(tmp_path, inference_level=level)["model_reasoning_effort"] == level


def test_minimal_reachable_via_direct_edit(tmp_path):
    # No web-selector entry, but codex accepts it and yaml can set it.
    assert "minimal" in REASONING_EFFORTS
    assert _doc(tmp_path, inference_level="minimal")["model_reasoning_effort"] == "minimal"


def test_xhigh_is_dropped_for_codex(tmp_path):
    # xhigh is a Claude-only level; Codex has no xhigh tier, so it must not
    # land in config.toml (codex would reject it at model-invocation).
    assert "model_reasoning_effort" not in _doc(tmp_path, inference_level="xhigh")


def test_invalid_level_is_dropped(tmp_path):
    assert "model_reasoning_effort" not in _doc(tmp_path, inference_level="turbo")


def test_empty_omits_the_key(tmp_path):
    assert "model_reasoning_effort" not in _doc(tmp_path, inference_level="")
    assert "model_reasoning_effort" not in _doc(tmp_path)


def test_key_precedes_mcp_tables_and_coexists(tmp_path):
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest, inference_level="high", extra_servers={"fs": {"command": "x"}},
    )
    text = dest.read_text(encoding="utf-8")
    # Top-level key must appear before the first [table] for TOML validity.
    assert text.index("model_reasoning_effort") < text.index("[")
    doc = tomllib.loads(text)
    assert doc["model_reasoning_effort"] == "high"
    assert "fs" in doc.get("mcp_servers", {})


def test_runtime_config_default_is_empty():
    from puffo_agent.portal.state import RuntimeConfig
    assert RuntimeConfig().inference_level == ""


def test_runtime_config_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    from puffo_agent.portal.state import AgentConfig, RuntimeConfig
    cfg = AgentConfig(
        id="codex-agent",
        display_name="codex-agent",
        runtime=RuntimeConfig(kind="cli-local", harness="codex", inference_level="high"),
    )
    cfg.save()
    assert AgentConfig.load("codex-agent").runtime.inference_level == "high"


def test_legacy_yml_without_field_defaults_empty(tmp_path, monkeypatch):
    # Agents written before PUF-373 don't carry the field; load must default
    # to "" rather than KeyError.
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    from puffo_agent.portal.state import AgentConfig, agent_yml_path
    aid = "legacy-codex"
    yml = agent_yml_path(aid)
    yml.parent.mkdir(parents=True, exist_ok=True)
    yml.write_text(
        "id: legacy-codex\n"
        "state: running\n"
        "display_name: legacy-codex\n"
        "created_at: 0\n"
        "puffo_core: {server_url: 'https://api.puffo.ai', slug: '', "
        "device_id: '', space_id: '', operator_slug: ''}\n"
        "runtime: {kind: cli-local, provider: '', model: '', harness: codex, "
        "sandbox: danger-full-access}\n"
        "profile: profile.md\n"
        "memory_dir: memory\n"
        "workspace_dir: workspace\n"
        "triggers: {on_mention: true, on_dm: true}\n",
        encoding="utf-8",
    )
    assert AgentConfig.load(aid).runtime.inference_level == ""


# ─── claude-code: --effort on the spawn argv ─────────────────────────


def _local_adapter(level: str):
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter

    a = LocalCLIAdapter.__new__(LocalCLIAdapter)
    a.agent_id = "t-1"
    a.permission_mode = "bypassPermissions"
    a.model = "claude-opus-4-8"
    a.inference_level = level
    return a


def _docker_adapter(level: str):
    from puffo_agent.agent.adapters.docker_cli import DockerCLIAdapter

    a = DockerCLIAdapter.__new__(DockerCLIAdapter)
    a.agent_id = "t-1"
    a.container_name = "puffo-t-1"
    a.model = "claude-opus-4-8"
    a.inference_level = level
    return a


def test_local_claude_argv_carries_effort():
    cmd = _local_adapter("xhigh")._build_command([])
    assert cmd[cmd.index("--effort") + 1] == "xhigh"


def test_local_claude_argv_omits_empty_level():
    assert "--effort" not in _local_adapter("")._build_command([])


def test_local_claude_argv_skips_yaml_only_codex_value():
    assert "--effort" not in _local_adapter("minimal")._build_command([])


def test_docker_claude_argv_carries_effort():
    cmd = _docker_adapter("high")._build_command([])
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_docker_claude_argv_skips_invalid():
    assert "--effort" not in _docker_adapter("turbo")._build_command([])


def test_create_bundle_parses_and_validates_level():
    """Source pin: the linked-machine create path reads
    runtime.inference_level and rejects out-of-set values."""
    import inspect

    from puffo_agent.portal.api import handlers

    src = inspect.getsource(handlers._verify_agent_bundle)
    assert "inference_level=str(rt.get(\"inference_level\", \"\"))" in src
    assert "INFERENCE_LEVELS" in src


# ─── PUF-392: inference_level via the self-serve refresh MCP ──────────


import json  # noqa: E402
import pytest  # noqa: E402

from puffo_agent.mcp.config import supported_inference_levels  # noqa: E402
from puffo_agent.mcp.puffo_core_server import (  # noqa: E402
    _validate_refresh_inference_level,
)
from puffo_agent.portal.daemon import (  # noqa: E402
    _process_daemon_refresh_flags,
    _validate_daemon_inference_level,
)
from puffo_agent.portal.state import refresh_model_flag_path  # noqa: E402


def test_supported_levels_are_per_harness():
    assert supported_inference_levels("codex") == REASONING_EFFORTS
    assert supported_inference_levels("claude-code") == INFERENCE_LEVELS
    # codex has minimal but not xhigh; claude-code the reverse.
    assert "xhigh" not in supported_inference_levels("codex")
    assert "minimal" not in supported_inference_levels("claude-code")


def test_supported_levels_unknown_harness_is_permissive_union():
    levels = supported_inference_levels("")
    assert set(levels) == set(INFERENCE_LEVELS) | set(REASONING_EFFORTS)


@pytest.mark.parametrize(
    "harness,level",
    [("codex", "medium"), ("codex", "minimal"), ("claude-code", "high"),
     ("claude-code", "xhigh"), ("", "medium")],
)
def test_refresh_validator_accepts_in_set(harness, level):
    _validate_refresh_inference_level(harness, level)  # no raise


@pytest.mark.parametrize(
    "harness,level",
    [("codex", "xhigh"), ("claude-code", "minimal"), ("codex", "turbo")],
)
def test_refresh_validator_rejects_out_of_set(harness, level):
    with pytest.raises(RuntimeError):
        _validate_refresh_inference_level(harness, level)


def test_daemon_validator_rejects_codex_xhigh():
    with pytest.raises(ValueError):
        _validate_daemon_inference_level("codex", "xhigh")
    _validate_daemon_inference_level("codex", "high")  # no raise


def _codex_agent(tmp_path, monkeypatch, aid="codex-refresh", level=""):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    from puffo_agent.portal.state import AgentConfig, RuntimeConfig
    cfg = AgentConfig(
        id=aid,
        display_name=aid,
        runtime=RuntimeConfig(
            kind="cli-local", harness="codex", model="gpt-5.6",
            inference_level=level,
        ),
    )
    cfg.save()
    return cfg


def _write_model_flag(cfg, **payload):
    flag = refresh_model_flag_path(cfg.resolve_workspace_dir())
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(json.dumps({"requested_at": 0, **payload}) + "\n", encoding="utf-8")
    return flag


def test_daemon_applies_standalone_inference_level(tmp_path, monkeypatch):
    # AC 1: refresh(inference_level=...) with no harness/model persists the
    # effort to agent.yml and consumes the flag (respawn is the config-changed
    # check's job, driven by runtime inequality).
    from puffo_agent.portal.state import AgentConfig
    cfg = _codex_agent(tmp_path, monkeypatch)
    flag = _write_model_flag(cfg, harness="", model="", inference_level="medium")

    _process_daemon_refresh_flags("codex-refresh")

    loaded = AgentConfig.load("codex-refresh")
    assert loaded.runtime.inference_level == "medium"
    assert loaded.runtime.harness == "codex"  # untouched
    assert not flag.exists()


def test_daemon_applies_harness_model_and_level_together(tmp_path, monkeypatch):
    # AC 2: all three persist from one flag.
    from puffo_agent.portal.state import AgentConfig
    cfg = _codex_agent(tmp_path, monkeypatch)
    _write_model_flag(
        cfg, harness="claude-code", model="claude-opus-4-8", inference_level="xhigh",
    )
    # claude-code CLI must resolve for the harness+model validation to pass;
    # skip if it's not installed in this env.
    from puffo_agent.agent.cli_bin import resolve_claude_bin
    if resolve_claude_bin() is None:
        pytest.skip("claude-code CLI not installed")

    _process_daemon_refresh_flags("codex-refresh")

    loaded = AgentConfig.load("codex-refresh")
    assert loaded.runtime.harness == "claude-code"
    assert loaded.runtime.inference_level == "xhigh"


def test_daemon_marks_flag_broken_on_bad_level(tmp_path, monkeypatch):
    # AC 3: xhigh on a codex agent → no persistence, flag goes .broken.
    from puffo_agent.portal.state import AgentConfig
    cfg = _codex_agent(tmp_path, monkeypatch, level="low")
    flag = _write_model_flag(cfg, harness="", model="", inference_level="xhigh")

    _process_daemon_refresh_flags("codex-refresh")

    loaded = AgentConfig.load("codex-refresh")
    assert loaded.runtime.inference_level == "low"  # unchanged
    assert not flag.exists()
    assert flag.with_suffix(".flag.broken").exists()


def test_refresh_docstring_documents_inference_level_axis():
    """AC 7 source-pin: the refresh MCP tool advertises inference_level as an
    orthogonal axis so agents discover it; guards a future refactor from
    silently dropping it from the docs."""
    import inspect

    from puffo_agent.mcp import puffo_core_server

    src = inspect.getsource(puffo_core_server._register_local_tools)
    assert "Five orthogonal axes" in src
    assert "inference_level" in src
