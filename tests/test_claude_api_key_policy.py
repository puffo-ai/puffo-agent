from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from _portal_support import isolated_home, write_test_agent
from puffo_agent.agent.adapters.docker_cli import DockerCLIAdapter
from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter
from puffo_agent.agent.adapters.base import TurnResult
from puffo_agent.agent._invite_strings import format_anthropic_api_key_rejected
from puffo_agent.portal import cli, state
from puffo_agent.portal.daemon import Daemon
from puffo_agent.portal.state import AgentConfig, DaemonConfig, RuntimeState
from puffo_agent.portal.worker import (
    Worker,
    _handle_suppressed_reply,
    build_adapter,
)


def _local_adapter(tmp_path: Path, *, api_key: str = "") -> LocalCLIAdapter:
    return LocalCLIAdapter(
        agent_id="local-key-policy",
        model="claude-sonnet-5",
        workspace_dir=str(tmp_path / "workspace"),
        claude_dir=str(tmp_path / "workspace" / ".claude"),
        session_file=str(tmp_path / "session.json"),
        mcp_config_file=str(tmp_path / "mcp.json"),
        agent_home_dir=str(tmp_path / "agent-home"),
        claude_api_key=api_key,
    )


def _docker_adapter(tmp_path: Path, *, api_key: str = "") -> DockerCLIAdapter:
    return DockerCLIAdapter(
        agent_id="docker-key-policy",
        model="claude-sonnet-5",
        image="puffo/agent-runtime:test",
        workspace_dir=str(tmp_path / "workspace"),
        claude_dir=str(tmp_path / "workspace" / ".claude"),
        session_file=str(tmp_path / "session.json"),
        agent_home_dir=str(tmp_path / "agent-home"),
        shared_fs_dir=str(tmp_path / "shared"),
        claude_api_key=api_key,
    )


def test_daemon_anthropic_cli_api_key_opt_in_round_trips(tmp_path, monkeypatch):
    config_path = tmp_path / "daemon.yml"
    monkeypatch.setattr(state, "daemon_yml_path", lambda: config_path)
    cfg = DaemonConfig()
    assert cfg.anthropic.cli_use_api_key is False
    cfg.anthropic.api_key = "daemon-key"
    cfg.anthropic.cli_use_api_key = True

    cfg.save()

    loaded = DaemonConfig.load()
    assert loaded.anthropic.api_key == "daemon-key"
    assert loaded.anthropic.cli_use_api_key is True
    assert "cli_use_api_key" not in loaded.openai.__dict__


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, False), (False, False), ("true", False), (True, True)],
)
def test_daemon_cli_api_key_requires_yaml_boolean_true(
    tmp_path, monkeypatch, raw, expected,
):
    config_path = tmp_path / "daemon.yml"
    monkeypatch.setattr(state, "daemon_yml_path", lambda: config_path)
    anthropic = {"api_key": "daemon-key"}
    if raw is not None:
        anthropic["cli_use_api_key"] = raw
    config_path.write_text(
        "anthropic:\n" + "".join(f"  {key}: {json.dumps(value)}\n" for key, value in anthropic.items()),
        encoding="utf-8",
    )

    assert DaemonConfig.load().anthropic.cli_use_api_key is expected


def test_config_command_preserves_cli_api_key_opt_in(monkeypatch):
    isolated_home()
    cfg = DaemonConfig()
    cfg.anthropic.api_key = "daemon-key"
    cfg.anthropic.cli_use_api_key = True
    cfg.save()
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    assert cli.main(["config"]) == 0

    loaded = DaemonConfig.load()
    assert loaded.anthropic.api_key == "daemon-key"
    assert loaded.anthropic.cli_use_api_key is True


def test_config_command_ignores_ambient_anthropic_api_key(monkeypatch):
    isolated_home()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    assert cli.main(["config"]) == 0

    assert DaemonConfig.load().anthropic.api_key == ""


def test_config_command_sets_anthropic_cli_api_key_options(capsys):
    isolated_home()

    assert cli.main([
        "config",
        "--anthropic-api-key", "configured-key",
        "--anthropic-cli-use-api-key", "true",
    ]) == 0

    loaded = DaemonConfig.load()
    assert loaded.anthropic.api_key == "configured-key"
    assert loaded.anthropic.cli_use_api_key is True
    assert "configured-key" not in capsys.readouterr().out

    assert cli.main([
        "config",
        "--anthropic-api-key", "",
        "--anthropic-cli-use-api-key", "false",
    ]) == 0
    loaded = DaemonConfig.load()
    assert loaded.anthropic.api_key == ""
    assert loaded.anthropic.cli_use_api_key is False


def test_config_command_partial_updates_preserve_other_anthropic_field():
    isolated_home()
    cfg = DaemonConfig()
    cfg.anthropic.api_key = "original-key"
    cfg.anthropic.cli_use_api_key = True
    cfg.save()

    assert cli.main([
        "config", "--anthropic-cli-use-api-key", "false",
    ]) == 0
    loaded = DaemonConfig.load()
    assert loaded.anthropic.api_key == "original-key"
    assert loaded.anthropic.cli_use_api_key is False

    assert cli.main([
        "config", "--anthropic-api-key", "replacement-key",
    ]) == 0
    loaded = DaemonConfig.load()
    assert loaded.anthropic.api_key == "replacement-key"
    assert loaded.anthropic.cli_use_api_key is False


def test_interactive_config_updates_anthropic_cli_api_key(monkeypatch):
    isolated_home()
    answers = iter(["", "configured-key", "true", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli.main(["config"]) == 0

    loaded = DaemonConfig.load()
    assert loaded.anthropic.api_key == "configured-key"
    assert loaded.anthropic.cli_use_api_key is True


def test_interactive_config_rejects_invalid_cli_api_key_mode(monkeypatch):
    isolated_home()
    answers = iter(["", "", "yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli.main(["config"]) == 2

    assert not state.daemon_yml_path().exists()


def test_settings_scrubber_handles_non_object_and_empty_env(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[]", encoding="utf-8")
    assert state.strip_claude_api_key_from_settings(path) is False
    path.write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "stale-key"}}),
        encoding="utf-8",
    )

    assert state.strip_claude_api_key_from_settings(path) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_settings_scrubber_reports_write_failure(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "stale-key"}}),
        encoding="utf-8",
    )

    def fail_write(_path, _data):
        raise OSError("read-only")

    monkeypatch.setattr(state, "_atomic_write_json", fail_write)

    assert state.strip_claude_api_key_from_settings(path) is False


def test_local_ignores_ambient_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    adapter = _local_adapter(tmp_path)
    adapter._prepare_mcp_args = lambda: []

    session = adapter._ensure_session()

    assert "ANTHROPIC_API_KEY" not in session.env


def test_local_injects_only_configured_daemon_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    adapter = _local_adapter(tmp_path, api_key="daemon-key")
    adapter._prepare_mcp_args = lambda: []

    session = adapter._ensure_session()

    assert session.env["ANTHROPIC_API_KEY"] == "daemon-key"


def test_local_removes_persisted_api_key_settings(tmp_path):
    adapter = _local_adapter(tmp_path)
    adapter._prepare_mcp_args = lambda: []
    paths = (
        adapter.agent_home_dir / ".claude" / "settings.json",
        Path(adapter.claude_dir) / "settings.json",
        Path(adapter.claude_dir) / "settings.local.json",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "env": {"ANTHROPIC_API_KEY": "stale-key", "KEEP": "value"},
            }),
            encoding="utf-8",
        )

    adapter._ensure_session()

    for path in paths:
        assert json.loads(path.read_text(encoding="utf-8"))["env"] == {
            "KEEP": "value",
        }


def test_local_rescrubs_api_key_settings_before_each_spawn(tmp_path):
    adapter = _local_adapter(tmp_path)
    adapter._prepare_mcp_args = lambda: []
    session = adapter._ensure_session()
    paths = (
        adapter.agent_home_dir / ".claude" / "settings.json",
        adapter.agent_home_dir / ".claude" / "settings.local.json",
        Path(adapter.claude_dir) / "settings.json",
        Path(adapter.claude_dir) / "settings.local.json",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"env": {"ANTHROPIC_API_KEY": "late-key"}}),
            encoding="utf-8",
        )

    session.build_command([], session.env_overrides)

    assert all(json.loads(path.read_text()) == {} for path in paths)


def test_local_settings_scrub_tolerates_legacy_adapter_without_paths(tmp_path):
    adapter = _local_adapter(tmp_path)
    del adapter.agent_home_dir
    del adapter.claude_dir

    adapter._strip_claude_api_key_settings()


def test_docker_ignores_ambient_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    adapter = _docker_adapter(tmp_path)
    adapter._prepare_mcp_args = lambda: []

    session = adapter._ensure_session()
    command = session.build_command([], session.env_overrides)

    assert "ANTHROPIC_API_KEY" not in session.env
    assert "ANTHROPIC_API_KEY=" in command


def test_docker_injects_configured_key_without_argv_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    adapter = _docker_adapter(tmp_path, api_key="daemon-key")
    adapter._prepare_mcp_args = lambda: []

    session = adapter._ensure_session()
    command = session.build_command([], session.env_overrides)

    assert session.env["ANTHROPIC_API_KEY"] == "daemon-key"
    key_index = command.index("ANTHROPIC_API_KEY")
    assert command[key_index - 1] == "-e"
    assert "daemon-key" not in command
    assert "ambient-key" not in command


@pytest.mark.parametrize("api_key", ["", "daemon-key"])
def test_docker_env_overrides_cannot_inject_anthropic_api_key(
    tmp_path, api_key,
):
    adapter = _docker_adapter(tmp_path, api_key=api_key)
    adapter.env_overrides["ANTHROPIC_API_KEY"] = "override-key"
    adapter._prepare_mcp_args = lambda: []

    session = adapter._ensure_session()
    command = session.build_command([], session.env_overrides)

    assert "ANTHROPIC_API_KEY=override-key" not in command
    assert "override-key" not in command


def test_docker_preserves_non_secret_env_overrides(tmp_path):
    adapter = _docker_adapter(tmp_path)
    adapter.env_overrides["SAFE_VALUE"] = "123"
    adapter._prepare_mcp_args = lambda: []

    session = adapter._ensure_session()
    command = session.build_command([], session.env_overrides)

    assert "SAFE_VALUE=123" in command


def test_docker_rescrubs_api_key_settings_before_each_spawn(tmp_path):
    adapter = _docker_adapter(tmp_path)
    adapter._prepare_mcp_args = lambda: []
    session = adapter._ensure_session()
    paths = (
        adapter.claude_home_src / "settings.json",
        adapter.claude_home_src / "settings.local.json",
        Path(adapter.claude_dir) / "settings.json",
        Path(adapter.claude_dir) / "settings.local.json",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"env": {"ANTHROPIC_API_KEY": "late-key"}}),
            encoding="utf-8",
        )

    session.build_command([], session.env_overrides)

    assert all(json.loads(path.read_text()) == {} for path in paths)


def test_docker_settings_scrub_tolerates_legacy_adapter_without_paths(tmp_path):
    adapter = _docker_adapter(tmp_path)
    del adapter.claude_home_src
    del adapter.claude_dir

    adapter._strip_claude_api_key_settings()


@pytest.mark.parametrize("kind", ["cli-local", "cli-docker"])
@pytest.mark.parametrize(
    ("api_key", "enabled", "expected"),
    [("daemon-key", False, ""), ("", True, ""), ("daemon-key", True, "daemon-key")],
)
def test_worker_applies_daemon_key_policy(kind, api_key, enabled, expected):
    home = isolated_home()
    write_test_agent(home, "key-policy")
    cfg = AgentConfig.load("key-policy")
    cfg.runtime.kind = kind
    cfg.runtime.provider = "anthropic"
    cfg.runtime.harness = "claude-code"
    daemon = DaemonConfig()
    daemon.anthropic.api_key = api_key
    daemon.anthropic.cli_use_api_key = enabled

    adapter = build_adapter(daemon, cfg)

    assert adapter.claude_api_key == expected


@pytest.mark.parametrize("kind", ["cli-local", "cli-docker"])
def test_codex_never_receives_anthropic_daemon_key(kind):
    home = isolated_home()
    write_test_agent(home, "codex-key-policy")
    cfg = AgentConfig.load("codex-key-policy")
    cfg.runtime.kind = kind
    cfg.runtime.provider = "openai"
    cfg.runtime.harness = "codex"
    daemon = DaemonConfig()
    daemon.anthropic.api_key = "daemon-key"
    daemon.anthropic.cli_use_api_key = True

    adapter = build_adapter(daemon, cfg)

    assert adapter.claude_api_key == ""


@pytest.mark.parametrize(
    ("api_key", "enabled", "uses_api_key"),
    [("", True, False), ("daemon-key", False, False), ("daemon-key", True, True)],
)
def test_daemon_oauth_gate_follows_api_key_policy(api_key, enabled, uses_api_key):
    home = isolated_home()
    write_test_agent(home, "refresh-policy")
    cfg = AgentConfig.load("refresh-policy")
    cfg.runtime.kind = "cli-local"
    cfg.runtime.harness = "claude-code"
    daemon_cfg = DaemonConfig()
    daemon_cfg.anthropic.api_key = api_key
    daemon_cfg.anthropic.cli_use_api_key = enabled
    daemon = Daemon(daemon_cfg)

    if uses_api_key:
        assert daemon._notify_refresh_for(cfg) is None
        assert daemon._ensure_fresh_for(cfg) is None
        worker = SimpleNamespace()
        daemon._register_with_refresher(cfg, worker)
        assert not hasattr(worker, "_refresh_success_callback")
    else:
        assert daemon._notify_refresh_for(cfg) is not None
        assert daemon._ensure_fresh_for(cfg) is not None


def test_api_key_auth_failure_uses_correct_recovery_and_success_rearms(tmp_path):
    home = isolated_home()
    write_test_agent(home, "api-key-recovery")
    agent_cfg = AgentConfig.load("api-key-recovery")
    agent_cfg.runtime.kind = "cli-local"
    agent_cfg.runtime.harness = "claude-code"
    daemon_cfg = DaemonConfig()
    daemon_cfg.anthropic.api_key = "configured-key"
    daemon_cfg.anthropic.cli_use_api_key = True
    worker = Worker(daemon_cfg, agent_cfg)
    worker.runtime = RuntimeState(status="running", health="ok")
    worker._on_auth_failed_enter = lambda: setattr(
        worker, "_api_key_auth_recovery_pending", True,
    )

    worker._enter_auth_failed(agent_cfg.id)

    assert worker.runtime.health == "auth_failed"
    assert "anthropic.api_key" in worker.runtime.error
    assert "claude auth login" not in worker.runtime.error
    worker._auth_failed_notification_sent = True
    worker._resolve_health_after_success(agent_cfg.id)
    assert worker.runtime.health == "ok"
    assert worker.runtime.error == ""
    assert worker._api_key_auth_recovery_pending is False
    assert worker._auth_failed_notification_sent is False


def test_subscription_success_does_not_clear_auth_failed(tmp_path):
    home = isolated_home()
    write_test_agent(home, "subscription-sticky")
    worker = Worker(
        DaemonConfig(), AgentConfig.load("subscription-sticky"),
    )
    worker.runtime = RuntimeState(status="running", health="auth_failed")
    worker.runtime.error = "sign-in expired"

    worker._resolve_health_after_success("subscription-sticky")

    assert worker.runtime.health == "auth_failed"
    assert worker.runtime.error == "sign-in expired"


def test_non_cli_worker_does_not_use_claude_cli_api_key_recovery():
    home = isolated_home()
    write_test_agent(home, "sdk-key-boundary")
    agent_cfg = AgentConfig.load("sdk-key-boundary")
    agent_cfg.runtime.kind = "chat-local"
    agent_cfg.runtime.harness = "claude-code"
    daemon_cfg = DaemonConfig()
    daemon_cfg.anthropic.api_key = "configured-key"
    daemon_cfg.anthropic.cli_use_api_key = True

    worker = Worker(daemon_cfg, agent_cfg)

    assert worker._claude_api_key_mode is False


def test_suppressed_api_key_error_uses_api_key_recovery_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = RuntimeState(status="running", health="ok")
    message = Worker._auth_failed_error(True)

    suppressed, _ = _handle_suppressed_reply(
        "Invalid API key · Please run /login",
        runtime,
        "api-key-agent",
        scope="fallback",
        auth_error_message=message,
    )

    assert suppressed is True
    assert runtime.health == "auth_failed"
    assert runtime.error == message
    assert "claude auth login" not in runtime.error


def test_api_key_auth_enter_marks_recovery_pending(monkeypatch):
    from puffo_agent.portal import worker as worker_module

    class StubWorker:
        agent_cfg = SimpleNamespace(id="api-key-agent")
        _auth_failed_notification_sent = False
        _claude_api_key_mode = True
        _api_key_auth_recovery_pending = False
        _on_auth_failed_enter = Worker._on_auth_failed_enter

        async def _notify_operator_of_auth_failed_oauth(self):
            return None

    def close_task(coro):
        coro.close()

    monkeypatch.setattr(worker_module.asyncio, "create_task", close_task)
    worker = StubWorker()

    worker._on_auth_failed_enter()

    assert worker._api_key_auth_recovery_pending is True
    assert worker._auth_failed_notification_sent is True


def test_api_key_auth_failure_copy_is_bilingual():
    text = format_anthropic_api_key_rejected("agent-1", "Linus")
    assert "Anthropic API key was rejected" in text
    assert "Anthropic API key 被拒绝" in text
    assert "anthropic.api_key" in text
    assert "anthropic.cli_use_api_key: true" in text
    assert "claude auth login" not in text


@pytest.mark.asyncio
async def test_api_key_auth_failure_dm_uses_api_key_recovery_copy():
    home = isolated_home()
    write_test_agent(home, "api-key-dm")
    agent_cfg = AgentConfig.load("api-key-dm")
    agent_cfg.runtime.kind = "cli-local"
    agent_cfg.runtime.harness = "claude-code"
    daemon_cfg = DaemonConfig()
    daemon_cfg.anthropic.api_key = "configured-key"
    daemon_cfg.anthropic.cli_use_api_key = True
    worker = Worker(daemon_cfg, agent_cfg)

    class Client:
        operator_slug = "operator"

        def __init__(self):
            self.text = ""

        async def _send_dm(self, _slug, text, *, root_id):
            assert root_id == ""
            self.text = text

    client = Client()
    worker._client = client

    await worker._notify_operator_of_auth_failed_oauth()

    assert "Anthropic API key was rejected" in client.text
    assert "claude auth login" not in client.text


@pytest.mark.asyncio
async def test_worker_success_callbacks_recover_api_key_auth(
    monkeypatch,
):
    from puffo_agent.portal import profile_sync, worker as worker_module

    home = isolated_home()
    write_test_agent(home, "api-key-callbacks")
    agent_cfg = AgentConfig.load("api-key-callbacks")
    agent_cfg.runtime.kind = "cli-local"
    agent_cfg.runtime.harness = "claude-code"
    daemon_cfg = DaemonConfig()
    daemon_cfg.anthropic.api_key = "configured-key"
    daemon_cfg.anthropic.cli_use_api_key = True

    class Adapter:
        model = "claude-sonnet-5"

        async def warm(self, _prompt):
            return None

        async def health_probe(self):
            return True

        async def run_turn(self, _ctx):
            return TurnResult(reply="[SILENT]", metadata={})

        def context_limits(self):
            return None, None

    class Client:
        async def listen(self, **callbacks):
            await callbacks["on_message"](
                "root-1",
                [{
                    "envelope_id": "message-1",
                    "sender_slug": "operator",
                    "sender_email": "",
                    "sender_is_agent": False,
                    "sender_display_name": "Operator",
                    "text": "hello",
                    "attachments": [],
                    "mentions": [],
                    "sent_at": 1,
                    "is_dm": True,
                    "is_visible_to_human": True,
                }],
                {"channel_id": "channel-1"},
            )
            worker.runtime.health = "auth_failed"
            worker.runtime.error = "rejected"
            worker._api_key_auth_recovery_pending = True
            worker._auth_failed_notification_sent = True
            await callbacks["on_turn_success"]("root-1", [], {})
            worker._stop.set()

        async def send_fallback_message(self, *_args, **_kwargs):
            return None

    async def no_profile_sync(_cfg):
        return None

    monkeypatch.setattr(worker_module, "build_adapter", lambda *_args: Adapter())
    monkeypatch.setattr(
        worker_module, "_build_puffo_core_client", lambda *_args, **_kwargs: Client(),
    )
    monkeypatch.setattr(profile_sync, "sync_full_profile", no_profile_sync)
    worker = Worker(daemon_cfg, agent_cfg)

    await worker._run()

    assert worker.runtime.health == "ok"
    assert worker._api_key_auth_recovery_pending is False
    assert worker._auth_failed_notification_sent is False
