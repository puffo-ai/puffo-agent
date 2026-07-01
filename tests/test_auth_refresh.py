"""ClaudeAuthFlow (paste-back) + CodexAuthFlow (device-poll) state
machines — stubbed runners so the suite doesn't need real CLI binaries."""

from __future__ import annotations

import asyncio
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
    Provider,
)


@dataclass
class StubClaudeRunner:
    spawn_result: LoginResult = field(
        default_factory=lambda: LoginResult(ok=True, url="https://login.example/abc"),
    )
    submit_result: LoginResult = field(
        default_factory=lambda: LoginResult(ok=True, credentials_path=Path("/tmp/x")),
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


@dataclass
class StubCodexRunner:
    spawn_result: LoginResult = field(
        default_factory=lambda: LoginResult(
            ok=True, url="https://auth.example/device", device_code="ABCD-1234",
        ),
    )
    complete_result: LoginResult = field(
        default_factory=lambda: LoginResult(ok=True, credentials_path=Path("/tmp/x")),
    )
    complete_delay: float = 0.0
    spawn_calls: int = 0
    wait_calls: int = 0
    cancel_calls: int = 0

    async def spawn(self) -> LoginResult:
        self.spawn_calls += 1
        return self.spawn_result

    async def wait_until_complete(self) -> LoginResult:
        self.wait_calls += 1
        if self.complete_delay:
            await asyncio.sleep(self.complete_delay)
        return self.complete_result

    async def cancel(self) -> None:
        self.cancel_calls += 1


def _make_coord(
    claude_runner: Optional[StubClaudeRunner] = None,
    codex_runner: Optional[StubCodexRunner] = None,
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
        claude_factory=lambda: claude_runner or StubClaudeRunner(),
        codex_factory=lambda: codex_runner or StubCodexRunner(),
    )
    return coord, emit_calls, restart_calls


# ── Claude flow ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claude_start_emits_url_and_awaits_token():
    runner = StubClaudeRunner()
    coord, emits, _ = _make_coord(claude_runner=runner)

    result = await coord.start_claude("op-1")

    assert result == {"ok": True, "url": "https://login.example/abc"}
    assert coord.state(Provider.CLAUDE) == FlowState.AWAITING_TOKEN
    assert runner.spawn_calls == 1
    assert len(emits) == 1
    slug, payload = emits[0]
    assert slug == "op-1"
    assert payload == {
        "type": "auth-refresh.url", "provider": "claude",
        "url": "https://login.example/abc",
    }


@pytest.mark.asyncio
async def test_claude_submit_applies_and_restarts():
    runner = StubClaudeRunner()
    coord, emits, restarts = _make_coord(claude_runner=runner, restart_returns=5)
    await coord.start_claude("op-1")
    emits.clear()

    result = await coord.submit_claude_token("my-token", "op-1")

    assert result == {"ok": True, "agents_restarted": 5}
    assert coord.state(Provider.CLAUDE) == FlowState.DONE
    assert runner.submit_calls == ["my-token"]
    assert restarts == [1]
    assert emits[-1][1]["type"] == "auth-refresh.done"


@pytest.mark.asyncio
async def test_claude_second_start_rejected_while_in_flight():
    coord, _, _ = _make_coord(claude_runner=StubClaudeRunner())
    await coord.start_claude("op-1")
    second = await coord.start_claude("op-1")
    assert second == {"ok": False, "error": "claude login already in progress"}


@pytest.mark.asyncio
async def test_claude_spawn_failure_marks_failed():
    runner = StubClaudeRunner(
        spawn_result=LoginResult(ok=False, error="CLI not found: 'claude'"),
    )
    coord, emits, _ = _make_coord(claude_runner=runner)
    result = await coord.start_claude("op-1")
    assert result["ok"] is False
    assert coord.state(Provider.CLAUDE) == FlowState.FAILED
    assert emits[-1][1]["stage"] == "spawn"


@pytest.mark.asyncio
async def test_claude_submit_failure_skips_restart():
    runner = StubClaudeRunner(
        submit_result=LoginResult(ok=False, error="bad token"),
    )
    coord, emits, restarts = _make_coord(claude_runner=runner)
    await coord.start_claude("op-1")
    emits.clear()
    result = await coord.submit_claude_token("bad", "op-1")
    assert result["ok"] is False
    assert coord.state(Provider.CLAUDE) == FlowState.FAILED
    assert restarts == []
    assert emits[-1][1]["stage"] == "apply"


@pytest.mark.asyncio
async def test_claude_submit_without_in_flight_rejected():
    coord, _, _ = _make_coord()
    result = await coord.submit_claude_token("tok", "op-1")
    assert result == {"ok": False, "error": "no claude login awaiting a token"}


@pytest.mark.asyncio
async def test_claude_submit_wrong_operator_rejected():
    coord, _, _ = _make_coord(claude_runner=StubClaudeRunner())
    await coord.start_claude("op-1")
    result = await coord.submit_claude_token("tok", "op-OTHER")
    assert result == {"ok": False, "error": "different operator owns this flow"}


@pytest.mark.asyncio
async def test_claude_cancel_resets_to_idle():
    runner = StubClaudeRunner()
    coord, _, _ = _make_coord(claude_runner=runner)
    await coord.start_claude("op-1")
    result = await coord.cancel(Provider.CLAUDE)
    assert result == {"ok": True, "state": "idle"}
    assert coord.state(Provider.CLAUDE) == FlowState.IDLE
    assert runner.cancel_calls == 1


@pytest.mark.asyncio
async def test_claude_cancel_idle_is_noop():
    coord, _, _ = _make_coord()
    result = await coord.cancel(Provider.CLAUDE)
    assert result == {"ok": True, "state": "idle"}


# ── Codex flow ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_codex_start_emits_url_and_device_code():
    runner = StubCodexRunner()
    coord, emits, _ = _make_coord(codex_runner=runner)

    result = await coord.start_codex("op-1")

    assert result == {
        "ok": True, "url": "https://auth.example/device",
        "device_code": "ABCD-1234",
    }
    assert coord.state(Provider.CODEX) == FlowState.POLLING
    assert runner.spawn_calls == 1
    assert emits[0][1] == {
        "type": "auth-refresh.url", "provider": "codex",
        "url": "https://auth.example/device", "device_code": "ABCD-1234",
    }


@pytest.mark.asyncio
async def test_codex_watcher_completes_and_restarts():
    runner = StubCodexRunner()
    coord, emits, restarts = _make_coord(codex_runner=runner, restart_returns=4)
    await coord.start_codex("op-1")

    await asyncio.wait_for(coord.codex._watcher, timeout=1.0)

    assert coord.state(Provider.CODEX) == FlowState.DONE
    assert runner.wait_calls == 1
    assert restarts == [1]
    assert emits[-1][1] == {
        "type": "auth-refresh.done", "provider": "codex", "agents_restarted": 4,
    }


@pytest.mark.asyncio
async def test_codex_polling_failure_marks_failed_and_skips_restart():
    runner = StubCodexRunner(
        complete_result=LoginResult(ok=False, error="device-auth polling timed out"),
    )
    coord, emits, restarts = _make_coord(codex_runner=runner)
    await coord.start_codex("op-1")

    await asyncio.wait_for(coord.codex._watcher, timeout=1.0)

    assert coord.state(Provider.CODEX) == FlowState.FAILED
    assert restarts == []
    assert emits[-1][1]["stage"] == "poll"


@pytest.mark.asyncio
async def test_codex_second_start_rejected_while_polling():
    runner = StubCodexRunner(complete_delay=0.5)
    coord, _, _ = _make_coord(codex_runner=runner)
    await coord.start_codex("op-1")

    second = await coord.start_codex("op-1")

    assert second == {"ok": False, "error": "codex login already in progress"}
    await coord.cancel(Provider.CODEX)


@pytest.mark.asyncio
async def test_codex_cancel_stops_watcher():
    runner = StubCodexRunner(complete_delay=1.0)
    coord, _, restarts = _make_coord(codex_runner=runner)
    await coord.start_codex("op-1")

    result = await coord.cancel(Provider.CODEX)

    assert result == {"ok": True, "state": "idle"}
    assert coord.state(Provider.CODEX) == FlowState.IDLE
    assert runner.cancel_calls == 1
    assert restarts == []


@pytest.mark.asyncio
async def test_codex_spawn_failure_no_watcher_started():
    runner = StubCodexRunner(
        spawn_result=LoginResult(ok=False, error="CLI not found: 'codex'"),
    )
    coord, emits, _ = _make_coord(codex_runner=runner)
    result = await coord.start_codex("op-1")
    assert result["ok"] is False
    assert coord.state(Provider.CODEX) == FlowState.FAILED
    assert coord.codex._watcher is None
    assert emits[-1][1]["stage"] == "spawn"


# ── Independent providers ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claude_and_codex_run_independently():
    coord, _, _ = _make_coord(
        claude_runner=StubClaudeRunner(),
        codex_runner=StubCodexRunner(complete_delay=0.5),
    )
    a = await coord.start_claude("op-1")
    b = await coord.start_codex("op-1")

    assert a["ok"] is True
    assert b["ok"] is True
    assert coord.state(Provider.CLAUDE) == FlowState.AWAITING_TOKEN
    assert coord.state(Provider.CODEX) == FlowState.POLLING
    await coord.cancel(Provider.CODEX)


# ── Emit best-effort ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_state_transition():
    runner = StubClaudeRunner()

    async def failing_emit(_slug: str, _payload: dict) -> None:
        raise RuntimeError("control WS dropped")

    async def fake_restart() -> int:
        return 0

    coord = AuthRefreshCoordinator(
        emit=failing_emit,
        restart_all_owned=fake_restart,
        claude_factory=lambda: runner,
    )
    result = await coord.start_claude("op-1")
    assert result["ok"] is True
    assert coord.state(Provider.CLAUDE) == FlowState.AWAITING_TOKEN
