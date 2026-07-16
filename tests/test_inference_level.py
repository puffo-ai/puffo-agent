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
