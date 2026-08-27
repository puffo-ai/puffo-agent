from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _portal_support import isolated_home, write_test_agent  # noqa: E402

from puffo_agent.portal.control.context_telemetry import (  # noqa: E402
    build_context_runtime,
    claude_autocompact_tokens,
    compact_threshold_pct,
    configured_compact_pct,
    estimate_compact_threshold_tokens,
    parse_threshold_pct,
    resolve_context_window,
)
from puffo_agent.portal.state import (  # noqa: E402
    AgentConfig,
    RuntimeState,
    validate_env_overrides,
)


def test_missing_threshold_is_not_guessed():
    assert estimate_compact_threshold_tokens(200_000, None) is None


@pytest.mark.parametrize(
    ("window", "threshold", "expected"),
    [(200_000, 167_000, 83.5), (1_000_000, 967_000, 96.7)],
)
def test_default_threshold_is_derived_from_claude(window, threshold, expected):
    assert compact_threshold_pct(window, threshold) == expected


@pytest.mark.parametrize("threshold", [None, 0, -1, 200_001, True, 1.5])
def test_invalid_claude_threshold_is_ignored(threshold):
    assert compact_threshold_pct(200_000, threshold) is None


@pytest.mark.parametrize("pct,expected", [(50, 100_000), (30, 60_000), (75, 150_000)])
def test_pct_override_scales_the_window(pct, expected):
    assert estimate_compact_threshold_tokens(200_000, pct) == expected


@pytest.mark.parametrize(
    ("window", "pct", "expected"),
    [
        (200_000, 30, 100_000),
        (200_000, 50, 100_000),
        (200_000, 75, 150_000),
        (1_000_000, 30, 300_000),
    ],
)
def test_claude_autocompact_tokens_enforces_cli_minimum(window, pct, expected):
    assert claude_autocompact_tokens(
        max_context=window, pct=pct, env={}
    ) == expected


def test_claude_autocompact_tokens_requires_a_known_window():
    assert claude_autocompact_tokens(
        model="claude-future-model", pct=50, env={}
    ) is None


def test_claude_autocompact_tokens_omits_default_without_resolving_model(caplog):
    assert claude_autocompact_tokens(
        model="claude-future-model", pct=None, env={}
    ) is None
    assert "context window is unknown" not in caplog.text


def test_pct_100_estimate_does_not_claim_compaction_is_disabled():
    assert estimate_compact_threshold_tokens(200_000, 100) == 200_000


@pytest.mark.parametrize("raw", ["0", "-5", "101", "abc", "", None])
def test_out_of_range_pct_reads_as_no_override(raw):
    assert parse_threshold_pct(raw) is None


@pytest.mark.parametrize("raw,expected", [("50", 50.0), ("30", 30.0), ("100", 100.0)])
def test_in_range_pct_parses(raw, expected):
    assert parse_threshold_pct(raw) == expected


def test_haiku_window_resolves_to_200k():
    assert resolve_context_window("claude-haiku-4-5", env={}) == 200_000


def test_unknown_model_window_is_not_invented(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_context_window("claude-future-model", env={}) is None
    assert "context window is unknown" in caplog.text


def test_window_env_override_wins():
    assert resolve_context_window("claude-sonnet-5", env={
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000",
    }) == 500_000


@pytest.mark.parametrize("raw", ["inf", "nan", "99999", "1000001", "n/a"])
def test_invalid_window_env_falls_back_without_raising(raw):
    assert resolve_context_window(
        "claude-haiku-4-5", {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": raw}
    ) == 200_000


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-5[1m]",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-fable-5",
        "claude-mythos-5",
    ],
)
def test_extended_context_model_detected(model):
    assert resolve_context_window(model, env={}) == 1_000_000


def test_standard_context_model_stays_at_200k():
    assert resolve_context_window("claude-haiku-4-5", env={}) == 200_000


def test_runtime_reports_default_when_no_override():
    out = build_context_runtime(model="claude-sonnet-5", env={})
    assert out["max_context"] == 1_000_000
    assert out["auto_compact_threshold_pct"] is None
    assert set(out) == {"max_context", "auto_compact_threshold_pct"}


def test_runtime_derives_default_from_session_limits():
    out = build_context_runtime(
        max_context=200_000,
        auto_compact_threshold=167_000,
        env={},
    )
    assert out == {
        "max_context": 200_000,
        "auto_compact_threshold_pct": 83.5,
    }


def test_runtime_reports_active_override():
    out = build_context_runtime(
        model="claude-sonnet-5",
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
        env={},
    )
    assert out["auto_compact_threshold_pct"] == 50.0


def test_runtime_configured_override_wins_over_raw_session_default():
    out = build_context_runtime(
        model="claude-haiku-4-5",
        max_context=200_000,
        auto_compact_threshold=167_000,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "5"},
        env={},
    )
    assert out["auto_compact_threshold_pct"] == 50.0


def test_runtime_matching_session_threshold_preserves_override():
    out = build_context_runtime(
        model="claude-haiku-4-5",
        max_context=200_000,
        auto_compact_threshold=60_000,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"},
        env={},
    )
    assert out["auto_compact_threshold_pct"] == 50.0


def test_runtime_treats_cli_raw_max_as_compact_window_when_configured():
    out = build_context_runtime(
        model="claude-fable-5",
        max_context=300_000,
        auto_compact_threshold=267_000,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"},
        env={},
    )
    assert out == {
        "max_context": 1_000_000,
        "auto_compact_threshold_pct": 30.0,
    }


def test_runtime_prefers_session_reported_max_context():
    out = build_context_runtime(
        model="claude-haiku-4-5",
        max_context=1_000_000,
        env={},
    )
    assert out["max_context"] == 1_000_000


def test_runtime_unknown_model_reports_unknown_window():
    out = build_context_runtime(model="claude-future-model", env={})
    assert out == {
        "max_context": None,
        "auto_compact_threshold_pct": None,
    }


def test_runtime_preserves_valid_100_pct_config():
    out = build_context_runtime(
        model="claude-sonnet-5",
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "100"},
        env={},
    )
    assert out["auto_compact_threshold_pct"] == 100.0


def test_docker_runtime_info_persists_context_config_with_model(monkeypatch):
    from types import SimpleNamespace

    from puffo_agent.portal.worker import Worker

    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    worker = Worker.__new__(Worker)
    worker.agent_cfg = SimpleNamespace(
        id="ctx-bot",
        runtime=SimpleNamespace(
            kind="cli-docker",
            provider="anthropic",
            harness="claude-code",
            model="claude-opus-4-6",
            inference_level="",
        ),
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"},
    )
    worker.runtime = RuntimeState()

    assert worker._runtime_info() == {
        "kind": "cli-docker",
        "provider": "anthropic",
        "harness": "claude-code",
        "model": "claude-opus-4-6",
        "inference_level": "",
        "max_context": 1_000_000,
        "auto_compact_threshold_pct": 30.0,
    }


def test_runtime_info_persists_claude_cli_minimum_as_effective_pct(monkeypatch):
    from types import SimpleNamespace

    from puffo_agent.portal.worker import Worker

    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    worker = Worker.__new__(Worker)
    worker.agent_cfg = SimpleNamespace(
        id="ctx-bot",
        runtime=SimpleNamespace(
            kind="cli-local",
            provider="anthropic",
            harness="claude-code",
            model="claude-haiku-4-5",
            inference_level="",
        ),
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"},
    )
    worker.runtime = RuntimeState()

    assert worker._runtime_info()["auto_compact_threshold_pct"] == 50.0
    assert worker.runtime.auto_compact_threshold_pct == 50.0


def test_runtime_info_prefers_adapter_context_window(monkeypatch):
    from types import SimpleNamespace

    from puffo_agent.portal.worker import Worker

    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    worker = Worker.__new__(Worker)
    worker.agent_cfg = SimpleNamespace(
        id="ctx-bot",
        runtime=SimpleNamespace(
            kind="cli-docker",
            provider="anthropic",
            harness="claude-code",
            model="",
            inference_level="",
        ),
        env_overrides={},
    )
    worker.runtime = RuntimeState()
    worker._adapter = SimpleNamespace(
        model="sonnet",
        context_limits=lambda: (1_000_000, 967_000),
    )

    assert worker._runtime_info()["max_context"] == 1_000_000
    assert worker._runtime_info()["auto_compact_threshold_pct"] == 96.7


def test_runtime_info_preserves_unknown_model_window():
    from types import SimpleNamespace

    from puffo_agent.portal.worker import Worker

    worker = Worker.__new__(Worker)
    worker.agent_cfg = SimpleNamespace(
        id="ctx-bot",
        runtime=SimpleNamespace(
            kind="cli-local",
            provider="anthropic",
            harness="claude-code",
            model="claude-future-model",
            inference_level="",
        ),
        env_overrides={},
    )
    worker.runtime = RuntimeState()

    assert worker._runtime_info()["max_context"] is None
    assert worker.runtime.max_context == 0


def test_codex_runtime_info_persists_app_server_context_window():
    from types import SimpleNamespace

    from puffo_agent.portal.worker import Worker

    worker = Worker.__new__(Worker)
    worker.agent_cfg = SimpleNamespace(
        id="ctx-bot",
        runtime=SimpleNamespace(
            kind="cli-docker",
            provider="openai",
            harness="codex",
            model="gpt-5.4",
            inference_level="high",
        ),
        env_overrides={},
    )
    worker._adapter = SimpleNamespace(context_limits=lambda: (258_400, 244_800))
    worker.runtime = RuntimeState()

    assert worker._runtime_info() == {
        "kind": "cli-docker",
        "provider": "openai",
        "harness": "codex",
        "model": "gpt-5.4",
        "inference_level": "high",
        "max_context": 258_400,
        "auto_compact_threshold_pct": 94.737,
    }
    assert worker.runtime.max_context == 258_400


def test_codex_runtime_reports_configured_compact_pct():
    from types import SimpleNamespace

    from puffo_agent.portal.worker import Worker

    worker = Worker.__new__(Worker)
    worker.agent_cfg = SimpleNamespace(
        id="ctx-bot",
        runtime=SimpleNamespace(
            kind="cli-local",
            provider="openai",
            harness="codex",
            model="gpt-5.4",
            inference_level="high",
        ),
        env_overrides={"CODEX_AUTOCOMPACT_PCT_OVERRIDE": "75"},
    )
    worker._adapter = SimpleNamespace(context_limits=lambda: (258_400, 193_800))
    worker.runtime = RuntimeState()

    out = worker._runtime_info()

    assert out["max_context"] == 258_400
    assert out["auto_compact_threshold_pct"] == 75


def test_validate_accepts_whitelisted_key():
    assert validate_env_overrides(
        {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}
    ) == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}


def test_validate_accepts_codex_threshold_key():
    assert validate_env_overrides(
        {"CODEX_AUTOCOMPACT_PCT_OVERRIDE": "75"}
    ) == {"CODEX_AUTOCOMPACT_PCT_OVERRIDE": "75"}


def test_configured_threshold_is_harness_specific():
    overrides = {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30",
        "CODEX_AUTOCOMPACT_PCT_OVERRIDE": "75",
    }
    assert configured_compact_pct("claude-code", overrides) == 30
    assert configured_compact_pct("codex", overrides) == 75


def test_validate_rejects_unknown_key():
    with pytest.raises(ValueError, match="not allowed"):
        validate_env_overrides({"PATH": "/tmp/evil"})


@pytest.mark.parametrize("bad", ["0", "-1", "101", "abc"])
def test_validate_rejects_values_claude_code_would_ignore(bad):
    with pytest.raises(ValueError):
        validate_env_overrides({"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": bad})


def test_validate_allows_empty_string_to_clear():
    assert validate_env_overrides(
        {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": ""}
    ) == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": ""}


def test_validate_normalises_numeric_input():
    assert validate_env_overrides(
        {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": 70}
    ) == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}


def test_validate_rejects_non_mapping():
    with pytest.raises(ValueError, match="must be an object"):
        validate_env_overrides(["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50"])


def test_validate_none_is_an_empty_update():
    assert validate_env_overrides(None) == {}


def test_env_overrides_round_trip_through_agent_yml():
    home = isolated_home()
    write_test_agent(home, "ctx-bot")
    cfg = AgentConfig.load("ctx-bot")
    assert cfg.env_overrides == {}

    cfg.env_overrides = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}
    cfg.save()

    assert AgentConfig.load("ctx-bot").env_overrides == {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50",
    }


def test_env_override_change_requires_worker_restart():
    from copy import deepcopy

    from puffo_agent.portal.daemon import _worker_needs_restart

    old = AgentConfig(id="ctx-bot")
    new = deepcopy(old)
    assert _worker_needs_restart(old, new) is False

    new.env_overrides = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"}
    assert _worker_needs_restart(old, new) is True


def test_yaml_int_value_loads_as_string():
    home = isolated_home()
    write_test_agent(home, "ctx-bot")
    path = AgentConfig.load("ctx-bot")
    path.env_overrides = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}
    path.save()
    yml = os.path.join(home, "agents", "ctx-bot", "agent.yml")
    with open(yml, encoding="utf-8") as fh:
        body = fh.read().replace(
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: '70'",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: 70",
        )
    with open(yml, "w", encoding="utf-8") as fh:
        fh.write(body)

    loaded = AgentConfig.load("ctx-bot").env_overrides
    assert loaded == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}
    assert isinstance(loaded["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], str)


def test_local_driver_applies_override_under_owned_environment(tmp_path, monkeypatch):
    import puffo_agent.agent.harness.local_runtime as local_runtime
    from puffo_agent.agent.harness.local_runtime import LocalRuntimePreparer
    from puffo_agent.portal.state import DaemonConfig, RuntimeConfig

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "75")
    monkeypatch.setattr(local_runtime, "resolve_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(local_runtime, "is_macos", lambda: False)
    cfg = AgentConfig(
        id="ctx-bot",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="anthropic",
            harness="claude-code",
            model="claude-sonnet-5",
        ),
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
    )

    spec = LocalRuntimePreparer(DaemonConfig(), cfg)._prepare_claude_spec(
        "prompt"
    )

    assert spec.environment["HOME"].endswith("/agents/ctx-bot")
    assert spec.launch_args[:3] == (
        "--dangerously-skip-permissions",
        "--autocompact",
        "500000",
    )


def test_default_band_clears_the_override():
    for window in (200_000, 1_000_000):
        cleared = estimate_compact_threshold_tokens(window, parse_threshold_pct(""))
        assert cleared is None


def test_junk_persisted_value_falls_back_to_default_band():
    assert parse_threshold_pct("999") is None
    out = build_context_runtime(
        model="claude-sonnet-5",
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "999"},
        env={},
    )
    assert out["auto_compact_threshold_pct"] is None


def test_docker_session_receives_overrides(tmp_path, monkeypatch):
    from puffo_agent.agent.harness.docker_runtime import DockerRuntimePreparer
    from puffo_agent.portal.state import DaemonConfig, RuntimeConfig

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    cfg = AgentConfig(
        id="ctx-bot",
        runtime=RuntimeConfig(
            kind="cli-docker",
            provider="anthropic",
            harness="claude-code",
            model="claude-sonnet-5",
        ),
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
    )
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "500000")
    runtime = build_context_runtime(
        model=cfg.runtime.model,
        env_overrides=cfg.env_overrides,
        env={},
    )
    assert runtime["max_context"] == 1_000_000
    args = list(
        DockerRuntimePreparer(DaemonConfig(), cfg)
        ._prepare_claude_spec("prompt")
        .launch_args
    )
    compact_index = args.index("--autocompact")
    assert args[compact_index : compact_index + 2] == [
        "--autocompact",
        "500000",
    ]
    assert all("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in arg for arg in args)


@pytest.mark.asyncio
async def test_remote_edit_reaches_docker_exec_command():
    from puffo_agent.portal.control.client import execute_command
    from puffo_agent.portal.state import DaemonConfig
    from puffo_agent.portal.worker import build_docker_runtime

    home = isolated_home()
    write_test_agent(home, "ctx-bot")
    cfg = AgentConfig.load("ctx-bot")
    cfg.runtime.kind = "cli-docker"
    cfg.runtime.provider = "anthropic"
    cfg.runtime.harness = "claude-code"
    cfg.save()

    result = await execute_command(
        "edit",
        "ctx-bot",
        {"env_overrides": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}},
    )
    assert result["ok"] is True

    preparer = build_docker_runtime(DaemonConfig(), AgentConfig.load("ctx-bot"))
    args = list(preparer._prepare_claude_spec("prompt").launch_args)
    compact_index = args.index("--autocompact")
    assert args[compact_index : compact_index + 2] == [
        "--autocompact",
        "500000",
    ]
    assert all("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in arg for arg in args)


@pytest.mark.asyncio
async def test_local_codex_driver_receives_compact_pct(tmp_path, monkeypatch):
    import puffo_agent.agent.harness.local_runtime as local_runtime
    from puffo_agent.agent.harness.local_runtime import LocalRuntimePreparer
    from puffo_agent.portal.state import DaemonConfig

    home = isolated_home()
    monkeypatch.setattr(local_runtime, "resolve_codex_bin", lambda: "/bin/codex")
    monkeypatch.setattr(local_runtime, "is_macos", lambda: False)
    monkeypatch.setattr(
        local_runtime, "sync_host_codex_auth_view", lambda *_args: "view"
    )
    write_test_agent(home, "ctx-bot")
    cfg = AgentConfig.load("ctx-bot")
    cfg.runtime.kind = "cli-local"
    cfg.runtime.provider = "openai"
    cfg.runtime.harness = "codex"
    cfg.env_overrides = {"CODEX_AUTOCOMPACT_PCT_OVERRIDE": "75"}

    prepared = await LocalRuntimePreparer(DaemonConfig(), cfg).prepare(
        system_prompt="prompt"
    )

    assert prepared.spec.auto_compact_threshold_pct == 75
