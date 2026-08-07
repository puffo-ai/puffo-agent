from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _bridge_support import isolated_home, write_test_agent  # noqa: E402

from puffo_agent.portal.control.context_telemetry import (  # noqa: E402
    build_context_runtime,
    compact_threshold_pct,
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
        max_context=200_000,
        auto_compact_threshold=167_000,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "5"},
        env={},
    )
    assert out["auto_compact_threshold_pct"] == 5.0


def test_runtime_matching_session_threshold_preserves_override():
    out = build_context_runtime(
        max_context=200_000,
        auto_compact_threshold=60_000,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"},
        env={},
    )
    assert out["auto_compact_threshold_pct"] == 30.0


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


def test_codex_runtime_info_omits_claude_context_config():
    from types import SimpleNamespace

    from puffo_agent.portal.worker import Worker

    worker = Worker.__new__(Worker)
    worker.agent_cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            kind="cli-docker",
            provider="openai",
            harness="codex",
            model="gpt-5.4",
            inference_level="high",
        ),
        env_overrides={},
    )

    assert worker._runtime_info() == {
        "kind": "cli-docker",
        "provider": "openai",
        "harness": "codex",
        "model": "gpt-5.4",
        "inference_level": "high",
    }


def test_validate_accepts_whitelisted_key():
    assert validate_env_overrides(
        {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}
    ) == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}


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


def test_overrides_layer_over_os_environ_but_under_adapter_owned_vars():
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter

    home = isolated_home()
    write_test_agent(home, "ctx-bot")
    adapter = LocalCLIAdapter(
        agent_id="ctx-bot",
        model="claude-sonnet-5",
        workspace_dir=os.path.join(home, "ws"),
        claude_dir=os.path.join(home, "claude"),
        session_file=os.path.join(home, "s.json"),
        mcp_config_file=os.path.join(home, "mcp.json"),
        agent_home_dir=os.path.join(home, "agent-home"),
        env_overrides={
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50",
            "HOME": "/tmp/untrusted-home",
            "USERPROFILE": "/tmp/untrusted-profile",
        },
    )
    assert adapter.context_limits() == (None, None)
    session = adapter._ensure_session()
    session._context_usage = {
        "rawMaxTokens": 200_000,
        "autoCompactThreshold": 167_000,
    }
    assert adapter.context_limits() == (200_000, 167_000)

    env = session.env
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "50"
    assert env["HOME"] == str(adapter.agent_home_dir)
    assert env["USERPROFILE"] == str(adapter.agent_home_dir)


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
    from puffo_agent.agent.adapters.docker_cli import DockerCLIAdapter

    adapter = DockerCLIAdapter(
        agent_id="ctx-bot",
        model="sonnet",
        image="test",
        workspace_dir=str(tmp_path / "workspace"),
        claude_dir=str(tmp_path / "claude"),
        session_file=str(tmp_path / "session.json"),
        agent_home_dir=str(tmp_path / "home"),
        shared_fs_dir=str(tmp_path / "shared"),
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
    )
    adapter._prepare_mcp_args = lambda: []

    assert adapter.context_limits() == (None, None)
    session = adapter._ensure_session()
    session._context_usage = {
        "rawMaxTokens": 1_000_000,
        "autoCompactThreshold": 967_000,
    }
    assert adapter.context_limits() == (1_000_000, 967_000)
    assert session.env_overrides == {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"
    }
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "500000")
    runtime = build_context_runtime(
        model=adapter.model,
        env_overrides=adapter.env_overrides,
        env={},
    )
    assert runtime["max_context"] is None
    assert session.build_command([], session.env_overrides)[:6] == [
        "docker",
        "exec",
        "-i",
        "-e",
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50",
        "puffo-ctx-bot",
    ]


@pytest.mark.parametrize(
    ("context_tokens", "expected_context"),
    [(80_000, 80_000), (0, None), (-1, None), ("80000", None), (None, None)],
)
def test_turn_complete_carries_valid_context(
    tmp_path, monkeypatch, context_tokens, expected_context
):
    import asyncio

    from puffo_agent.agent.adapters.base import Adapter, TurnContext, TurnResult
    from puffo_agent.agent.core import PuffoAgent
    from puffo_agent.portal.control import reporter

    class _Adapter(Adapter):
        model = "sonnet"
        env_overrides = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}

        async def run_turn(self, ctx: TurnContext) -> TurnResult:
            return TurnResult(
                reply="[SILENT]", metadata={"context_tokens": context_tokens}
            )

    class _Reporter:
        def __init__(self):
            self.events = []

        async def emit(self, agent_slug, event, payload):
            self.events.append((agent_slug, event, payload))

    captured = _Reporter()
    monkeypatch.setattr(reporter, "get_reporter", lambda: captured)
    agent = PuffoAgent(
        adapter=_Adapter(),
        system_prompt="test",
        memory_dir=str(tmp_path),
        agent_id="ctx-bot",
    )
    assert agent.adapter.context_limits() == (None, None)

    async def drive():
        await agent.handle_message(
            channel_id="ch",
            channel_name="general",
            sender="alice",
            sender_email="",
            text="hello",
            post_id="msg",
        )
        await asyncio.sleep(0)

    asyncio.run(drive())
    event = next(item for item in captured.events if item[1] == "turn_complete")
    assert event[0] == "ctx-bot"
    assert event[2]["tokens"] == {"input": 0, "output": 0}
    if expected_context is None:
        assert "current_context" not in event[2]
    else:
        assert event[2]["current_context"] == expected_context
    assert all(item[1] != "context_telemetry" for item in captured.events)


def test_claude_session_passes_overrides_to_every_spawn(tmp_path, monkeypatch):
    import asyncio

    from puffo_agent.agent.adapters.cli_session import ClaudeSession

    captured = {}

    def build_command(args, env_overrides):
        captured["env_overrides"] = env_overrides
        return ["claude-test"]

    class _Process:
        pass

    async def create_subprocess_exec(*args, **kwargs):
        captured["command"] = args
        return _Process()

    async def read_init(_process):
        return "session-test"

    async def drain_stderr(_process):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    session = ClaudeSession(
        agent_id="ctx-bot",
        session_file=tmp_path / "session.json",
        build_command=build_command,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"},
    )
    monkeypatch.setattr(session, "_read_init", read_init)
    monkeypatch.setattr(session, "_drain_stderr", drain_stderr)

    asyncio.run(session._spawn("system"))

    assert captured["env_overrides"] == {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"
    }
    assert captured["command"] == ("claude-test",)


@pytest.mark.asyncio
async def test_remote_edit_reaches_docker_exec_command():
    from puffo_agent.portal.control.client import execute_command
    from puffo_agent.portal.state import DaemonConfig
    from puffo_agent.portal.worker import build_adapter

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

    adapter = build_adapter(DaemonConfig(), AgentConfig.load("ctx-bot"))
    adapter._prepare_mcp_args = lambda: []
    session = adapter._ensure_session()
    command = session.build_command([], session.env_overrides)
    assert command[3:5] == ["-e", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50"]
