"""AuthRefreshCoordinator state machine — stubbed LoginRunner so
the suite doesn't need real ``claude`` / ``codex`` binaries."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.auth_refresh import (
    AuthRefreshCoordinator,
    FlowState,
    LoginResult,
    LoginRunner,
    Provider,
    parse_provider_from_op,
)


@dataclass
class StubLoginRunner:
    spawn_result: LoginResult = field(
        default_factory=lambda: LoginResult(ok=True, url="https://login.example/abc"),
    )
    submit_result: LoginResult = field(
        default_factory=lambda: LoginResult(
            ok=True, credentials_path=Path("/tmp/test-creds")
        ),
    )
    spawn_calls: int = 0
    submit_calls: list[str] = field(default_factory=list)
    cancel_calls: int = 0

    async def spawn(self) -> LoginResult:
        self.spawn_calls += 1
        return self.spawn_result

    async def submit_token(self, token: str) -> LoginResult:
        self.submit_calls.append(token)
        return self.submit_result

    async def cancel(self) -> None:
        self.cancel_calls += 1


def _make_coord(
    runner_claude: Optional[LoginRunner] = None,
    runner_codex: Optional[LoginRunner] = None,
    *,
    restart_returns: int = 3,
) -> tuple[AuthRefreshCoordinator, list[tuple[str, dict]], list[int]]:
    emit_calls: list[tuple[str, dict]] = []
    restart_calls: list[int] = []

    async def fake_emit(operator_slug: str, payload: dict) -> None:
        emit_calls.append((operator_slug, payload))

    async def fake_restart() -> int:
        restart_calls.append(1)
        return restart_returns

    coord = AuthRefreshCoordinator(
        emit=fake_emit,
        restart_all_owned=fake_restart,
        runner_factory_claude=lambda: runner_claude or StubLoginRunner(),
        runner_factory_codex=lambda: runner_codex or StubLoginRunner(),
    )
    return coord, emit_calls, restart_calls


def test_parse_provider_claude_variants():
    assert parse_provider_from_op("auth-claude") == Provider.CLAUDE
    assert parse_provider_from_op("auth-claude-token") == Provider.CLAUDE
    assert parse_provider_from_op("cancel-auth-claude") == Provider.CLAUDE


def test_parse_provider_codex_variants():
    assert parse_provider_from_op("auth-codex") == Provider.CODEX
    assert parse_provider_from_op("auth-codex-token") == Provider.CODEX
    assert parse_provider_from_op("cancel-auth-codex") == Provider.CODEX


def test_parse_provider_unknown_returns_none():
    assert parse_provider_from_op("pause") is None
    assert parse_provider_from_op("auth-other") is None


@pytest.mark.asyncio
async def test_start_emits_url_and_transitions_to_awaiting_token():
    runner = StubLoginRunner()
    coord, emits, _ = _make_coord(runner_claude=runner)

    result = await coord.start(Provider.CLAUDE, operator_slug="op-1")

    assert result == {"ok": True, "url": "https://login.example/abc"}
    assert coord.state(Provider.CLAUDE) == FlowState.AWAITING_TOKEN
    assert runner.spawn_calls == 1
    assert len(emits) == 1
    slug, payload = emits[0]
    assert slug == "op-1"
    assert payload["type"] == "auth-refresh.url"
    assert payload["provider"] == "claude"
    assert payload["url"] == "https://login.example/abc"


@pytest.mark.asyncio
async def test_submit_token_applies_credentials_and_restarts_agents():
    runner = StubLoginRunner()
    coord, emits, restarts = _make_coord(runner_claude=runner, restart_returns=5)
    await coord.start(Provider.CLAUDE, operator_slug="op-1")
    emits.clear()

    result = await coord.submit_token(Provider.CLAUDE, "my-token", "op-1")

    assert result == {"ok": True, "agents_restarted": 5}
    assert coord.state(Provider.CLAUDE) == FlowState.DONE
    assert runner.submit_calls == ["my-token"]
    assert restarts == [1]
    assert len(emits) == 1
    slug, payload = emits[0]
    assert slug == "op-1"
    assert payload["type"] == "auth-refresh.done"
    assert payload["agents_restarted"] == 5


@pytest.mark.asyncio
async def test_second_start_while_in_flight_returns_already_in_progress():
    coord, _, _ = _make_coord(runner_claude=StubLoginRunner())
    await coord.start(Provider.CLAUDE, operator_slug="op-1")

    second = await coord.start(Provider.CLAUDE, operator_slug="op-1")

    assert second == {"ok": False, "error": "claude login already in progress"}


@pytest.mark.asyncio
async def test_codex_and_claude_run_independently():
    coord, _, _ = _make_coord(
        runner_claude=StubLoginRunner(),
        runner_codex=StubLoginRunner(),
    )
    a = await coord.start(Provider.CLAUDE, operator_slug="op-1")
    b = await coord.start(Provider.CODEX, operator_slug="op-1")

    assert a["ok"] is True
    assert b["ok"] is True
    assert coord.state(Provider.CLAUDE) == FlowState.AWAITING_TOKEN
    assert coord.state(Provider.CODEX) == FlowState.AWAITING_TOKEN


@pytest.mark.asyncio
async def test_spawn_failure_emits_error_and_marks_failed():
    runner = StubLoginRunner(
        spawn_result=LoginResult(ok=False, error="CLI not found: 'claude'"),
    )
    coord, emits, _ = _make_coord(runner_claude=runner)

    result = await coord.start(Provider.CLAUDE, operator_slug="op-1")

    assert result == {"ok": False, "error": "CLI not found: 'claude'"}
    assert coord.state(Provider.CLAUDE) == FlowState.FAILED
    assert len(emits) == 1
    _, payload = emits[0]
    assert payload["type"] == "auth-refresh.error"
    assert payload["stage"] == "spawn"


@pytest.mark.asyncio
async def test_submit_failure_emits_error_and_skips_restart():
    runner = StubLoginRunner(
        submit_result=LoginResult(ok=False, error="login subprocess exited with code 1"),
    )
    coord, emits, restarts = _make_coord(runner_claude=runner)
    await coord.start(Provider.CLAUDE, operator_slug="op-1")
    emits.clear()

    result = await coord.submit_token(Provider.CLAUDE, "bad-token", "op-1")

    assert result["ok"] is False
    assert coord.state(Provider.CLAUDE) == FlowState.FAILED
    assert restarts == []
    assert len(emits) == 1
    _, payload = emits[0]
    assert payload["stage"] == "apply"


@pytest.mark.asyncio
async def test_submit_without_in_flight_returns_no_flow_error():
    coord, _, _ = _make_coord()
    result = await coord.submit_token(Provider.CLAUDE, "tok", "op-1")
    assert result == {
        "ok": False,
        "error": "no claude login awaiting a token",
    }


@pytest.mark.asyncio
async def test_submit_from_wrong_operator_rejected():
    coord, _, _ = _make_coord(runner_claude=StubLoginRunner())
    await coord.start(Provider.CLAUDE, operator_slug="op-1")
    result = await coord.submit_token(Provider.CLAUDE, "tok", "op-OTHER")
    assert result == {
        "ok": False,
        "error": "different operator owns this flow",
    }


@pytest.mark.asyncio
async def test_cancel_collapses_in_flight_back_to_idle():
    runner = StubLoginRunner()
    coord, _, _ = _make_coord(runner_claude=runner)
    await coord.start(Provider.CLAUDE, operator_slug="op-1")

    result = await coord.cancel(Provider.CLAUDE)

    assert result == {"ok": True, "state": "idle"}
    assert coord.state(Provider.CLAUDE) == FlowState.IDLE
    assert runner.cancel_calls == 1


@pytest.mark.asyncio
async def test_cancel_on_idle_is_noop():
    coord, _, _ = _make_coord()
    result = await coord.cancel(Provider.CLAUDE)
    assert result == {"ok": True, "state": "idle"}


@pytest.mark.asyncio
async def test_retry_after_cancel_works():
    runner_a = StubLoginRunner()
    runner_b = StubLoginRunner(
        spawn_result=LoginResult(ok=True, url="https://login.example/second"),
    )
    factories = iter([runner_a, runner_b])
    coord, _, _ = _make_coord()
    coord.runner_factory_claude = lambda: next(factories)

    await coord.start(Provider.CLAUDE, operator_slug="op-1")
    await coord.cancel(Provider.CLAUDE)
    result = await coord.start(Provider.CLAUDE, operator_slug="op-1")

    assert result["url"] == "https://login.example/second"


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_state_transition():
    runner = StubLoginRunner()

    async def failing_emit(_slug: str, _payload: dict) -> None:
        raise RuntimeError("control WS dropped")

    async def fake_restart() -> int:
        return 0

    coord = AuthRefreshCoordinator(
        emit=failing_emit,
        restart_all_owned=fake_restart,
        runner_factory_claude=lambda: runner,
    )

    result = await coord.start(Provider.CLAUDE, operator_slug="op-1")
    assert result["ok"] is True
    assert coord.state(Provider.CLAUDE) == FlowState.AWAITING_TOKEN
