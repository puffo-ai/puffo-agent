"""Inference-level persistence across CLI and daemon refresh paths."""

from __future__ import annotations

import pytest

from puffo_agent.mcp.host_tools import _write_refresh_model_flag
from puffo_agent.portal import daemon as daemon_module
from puffo_agent.portal.cli import build_parser
from puffo_agent.portal.state import AgentConfig, RuntimeConfig


def _save_codex_agent(tmp_path, monkeypatch, *, level: str = "minimal") -> AgentConfig:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    cfg = AgentConfig(
        id="agent-inference",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="openai",
            harness="codex",
            model="gpt-5.5",
            inference_level=level,
        ),
    )
    cfg.save()
    return cfg


def test_daemon_refresh_persists_standalone_inference_level(tmp_path, monkeypatch):
    cfg = _save_codex_agent(tmp_path, monkeypatch, level="low")
    _write_refresh_model_flag(
        cfg.resolve_workspace_dir(),
        harness="",
        model="",
        inference_level="high",
    )

    daemon_module._process_daemon_refresh_flags(cfg.id)

    loaded = AgentConfig.load(cfg.id)
    assert loaded.runtime.harness == "codex"
    assert loaded.runtime.model == "gpt-5.5"
    assert loaded.runtime.inference_level == "high"


def test_daemon_harness_swap_clears_incompatible_inference_level(
    tmp_path, monkeypatch,
):
    cfg = _save_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(
        daemon_module, "_validate_daemon_refresh_model", lambda harness, model: None,
    )
    _write_refresh_model_flag(
        cfg.resolve_workspace_dir(),
        harness="claude-code",
        model="claude-sonnet-4-6",
    )

    daemon_module._process_daemon_refresh_flags(cfg.id)

    loaded = AgentConfig.load(cfg.id)
    assert loaded.runtime.provider == "anthropic"
    assert loaded.runtime.harness == "claude-code"
    assert loaded.runtime.model == "claude-sonnet-4-6"
    assert loaded.runtime.inference_level == ""


def test_cli_harness_swap_clears_incompatible_inference_level(
    tmp_path, monkeypatch, capsys,
):
    cfg = _save_codex_agent(tmp_path, monkeypatch)
    args = build_parser().parse_args([
        "agent", "runtime", cfg.id,
        "--provider", "anthropic",
        "--harness", "claude-code",
    ])

    assert args.func(args) == 0

    loaded = AgentConfig.load(cfg.id)
    assert loaded.runtime.harness == "claude-code"
    assert loaded.runtime.inference_level == ""
    assert "incompatible prior value cleared" in capsys.readouterr().out


def test_load_rejects_inference_level_incompatible_with_harness(
    tmp_path, monkeypatch,
):
    cfg = _save_codex_agent(tmp_path, monkeypatch)
    cfg.runtime.harness = "claude-code"
    cfg.runtime.provider = "anthropic"
    cfg.save()

    with pytest.raises(RuntimeError, match="inference_level='minimal'.*claude-code"):
        AgentConfig.load(cfg.id)


_CLAUDE = ("anthropic", "claude-code", "claude-sonnet-4-6")
_CODEX = ("openai", "codex", "gpt-5.5")


def _save_agent(tmp_path, monkeypatch, start, level: str) -> AgentConfig:
    provider, harness, model = start
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    cfg = AgentConfig(
        id="agent-inference",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider=provider,
            harness=harness,
            model=model,
            inference_level=level,
        ),
    )
    cfg.save()
    return cfg


def _write_control_edit(cfg, payload) -> None:
    from puffo_agent.portal.control.client import _apply_edit_runtime

    assert _apply_edit_runtime(cfg, {"runtime": payload}) == (True, None)


@pytest.mark.parametrize(("writer", "start", "level", "target", "expected"), [
    # The active control writer clears a level the incoming harness cannot serve...
    (_write_control_edit, _CLAUDE, "xhigh", _CODEX, ""),
    (_write_control_edit, _CODEX, "minimal", _CLAUDE, ""),
    # ...and keeps one that the incoming harness still supports.
    (_write_control_edit, _CLAUDE, "high", _CODEX, "high"),
])
def test_runtime_writers_save_a_loadable_config_on_harness_swap(
    tmp_path, monkeypatch, writer, start, level, target, expected,
):
    """No writer may persist an ``agent.yml`` that ``AgentConfig.load`` rejects."""
    cfg = _save_agent(tmp_path, monkeypatch, start, level)
    provider, harness, _model = target

    writer(cfg, {"harness": harness, "provider": provider})
    cfg.save()

    loaded = AgentConfig.load(cfg.id)
    assert loaded.runtime.harness == harness
    assert loaded.runtime.inference_level == expected



def test_desktop_save_harness_swap_clears_incompatible_inference_level(
    tmp_path, monkeypatch,
):
    """The Agent Detail save path is a runtime writer too: swapping a
    Codex agent to claude-code must not persist Codex-only ``minimal``,
    or the saved yaml is rejected by ``AgentConfig.load`` and by daemon
    reconciliation (which loads through the same gate)."""
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    cfg = _save_codex_agent(tmp_path, monkeypatch)

    from PySide6.QtWidgets import QApplication, QMessageBox

    from puffo_agent.agent.model_catalog import ModelOption
    from puffo_agent.portal.ui.widgets import agent_detail as detail_module

    # The real prefetch/provider_models reach the live /v1/models list and
    # the real warning() opens a modal dialog that would hang headless.
    warnings: list[str] = []
    monkeypatch.setattr(detail_module, "prefetch", lambda: None)
    monkeypatch.setattr(
        detail_module, "provider_models",
        lambda harness: [ModelOption("claude-sonnet-4-6", "claude-sonnet-4-6")],
    )
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda *a, **kw: warnings.append(a[2] if len(a) > 2 else ""),
    )

    QApplication.instance() or QApplication([])
    widget = detail_module.AgentDetail()
    widget.bind(cfg.id)
    widget._harness.setCurrentText("claude-code")
    widget._model.setCurrentIndex(0)
    widget._on_save()

    assert warnings == []
    loaded = AgentConfig.load(cfg.id)
    assert loaded.runtime.provider == "anthropic"
    assert loaded.runtime.harness == "claude-code"
    assert loaded.runtime.inference_level == ""


def test_cli_switch_to_harness_without_inference_support_clears_level(
    tmp_path, monkeypatch,
):
    """Codex-only ``minimal`` cannot survive a one-command switch to the
    Docker Claude Code runtime, which does not implement that level."""
    cfg = _save_codex_agent(tmp_path, monkeypatch, level="minimal")
    args = build_parser().parse_args([
        "agent", "runtime", cfg.id,
        "--kind", "cli-docker",
        "--provider", "anthropic",
        "--harness", "claude-code",
    ])

    assert args.func(args) == 0

    loaded = AgentConfig.load(cfg.id)
    assert loaded.runtime.provider == "anthropic"
    assert loaded.runtime.harness == "claude-code"
    assert loaded.runtime.inference_level == ""


def test_cli_runtime_edit_can_repair_a_retired_runtime_combination(
    tmp_path, monkeypatch,
):
    """An upgrade diagnostic must not recommend a command that cannot run."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    cfg = AgentConfig(
        id="retired-docker-codex",
        runtime=RuntimeConfig(
            kind="cli-docker",
            provider="openai",
            harness="codex",
            model="gpt-5.5",
        ),
    )
    cfg.save()

    args = build_parser().parse_args([
        "agent", "runtime", cfg.id, "--kind", "cli-local",
    ])

    assert args.func(args) == 0
    loaded = AgentConfig.load(cfg.id)
    assert loaded.runtime.kind == "cli-local"
    assert loaded.runtime.provider == "openai"
    assert loaded.runtime.harness == "codex"
