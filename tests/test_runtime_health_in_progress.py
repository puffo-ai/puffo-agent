"""PUF-270: ``runtime.health = "in_progress"`` lifecycle.

Tests the two new static helpers on Worker:
- ``_flip_health_in_progress`` (called at the top of every
  ``on_message_batch``) overrides any sticky red into in_progress.
- ``_resolve_health_on_success`` (called in the finally on
  ``turn_succeeded``) transitions in_progress → ok, but leaves any
  other state alone.

Also covers the StatusReporter heartbeat shape extension (carries
both per-turn ``status`` AND persistent ``health``) and the
``cli.cmd_agent_list`` surfaced-health tuple includes
``in_progress``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from puffo_agent.portal.state import RuntimeState
from puffo_agent.portal.worker import Worker


_LOG = logging.getLogger("test-puf-270")


def _seed_runtime(tmp_path: Path, monkeypatch, *, health: str) -> str:
    """Materialize a per-agent runtime.json on disk so save() round-
    trips correctly. Returns the agent_id."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    agent_id = "tester-puf270"
    (tmp_path / "agents" / agent_id).mkdir(parents=True, exist_ok=True)
    rs = RuntimeState(
        status="running",
        started_at=1,
        msg_count=0,
        health=health,
        error="prior",
    )
    rs.save(agent_id)
    return agent_id


# ── _flip_health_in_progress ─────────────────────────────────────


@pytest.mark.parametrize(
    "starting_health",
    ["ok", "unknown", "auth_failed", "api_error_abandoned", "refresh_broken"],
)
def test_flip_in_progress_overrides_any_starting_state(
    tmp_path, monkeypatch, starting_health,
):
    agent_id = _seed_runtime(tmp_path, monkeypatch, health=starting_health)
    rs = RuntimeState.load(agent_id)
    assert rs is not None
    Worker._flip_health_in_progress(rs, agent_id, _LOG)
    assert rs.health == "in_progress"
    assert rs.error == ""
    # Persisted to disk.
    on_disk = RuntimeState.load(agent_id)
    assert on_disk is not None
    assert on_disk.health == "in_progress"
    assert on_disk.error == ""


def test_flip_in_progress_is_idempotent_when_already_in_progress(
    tmp_path, monkeypatch,
):
    agent_id = _seed_runtime(tmp_path, monkeypatch, health="in_progress")
    rs = RuntimeState.load(agent_id)
    assert rs is not None
    # Carry a stale error string + assert flip leaves it alone (no-op).
    rs.error = "stale"
    Worker._flip_health_in_progress(rs, agent_id, _LOG)
    assert rs.health == "in_progress"
    assert rs.error == "stale", "no-op path must not clear unrelated error"


# ── _resolve_health_on_success ───────────────────────────────────


def test_resolve_on_success_transitions_in_progress_to_ok(
    tmp_path, monkeypatch,
):
    agent_id = _seed_runtime(tmp_path, monkeypatch, health="in_progress")
    rs = RuntimeState.load(agent_id)
    assert rs is not None
    Worker._resolve_health_on_success(rs, agent_id, _LOG)
    assert rs.health == "ok"
    assert rs.error == ""
    on_disk = RuntimeState.load(agent_id)
    assert on_disk is not None
    assert on_disk.health == "ok"


@pytest.mark.parametrize(
    "give_up_health",
    ["api_error_abandoned", "auth_failed", "refresh_broken"],
)
def test_resolve_on_success_leaves_give_up_red_untouched(
    tmp_path, monkeypatch, give_up_health,
):
    """If a category-specific path mid-turn set a give-up red (e.g.,
    api_error_abandoned at L1006 in worker.py), the resolve must NOT
    overwrite it back to ``ok`` — that's the give-up signal operator's
    rule 3 preserves."""
    agent_id = _seed_runtime(tmp_path, monkeypatch, health=give_up_health)
    rs = RuntimeState.load(agent_id)
    assert rs is not None
    Worker._resolve_health_on_success(rs, agent_id, _LOG)
    assert rs.health == give_up_health, (
        f"resolve must not clobber {give_up_health!r}"
    )


def test_resolve_on_success_leaves_ok_alone(tmp_path, monkeypatch):
    agent_id = _seed_runtime(tmp_path, monkeypatch, health="ok")
    rs = RuntimeState.load(agent_id)
    assert rs is not None
    Worker._resolve_health_on_success(rs, agent_id, _LOG)
    assert rs.health == "ok"


# PR #59 round-1 review item #5: simulate the in-turn flow
# (flip → in-turn red set → resolve sees red and skips), not just
# the bare "seed direct red" path the parametrized test above covers.
@pytest.mark.parametrize(
    "in_turn_red",
    ["auth_failed", "api_error_abandoned", "refresh_broken"],
)
def test_resolve_skips_when_in_turn_path_wrote_red(
    tmp_path, monkeypatch, in_turn_red,
):
    agent_id = _seed_runtime(tmp_path, monkeypatch, health="ok")
    rs = RuntimeState.load(agent_id)
    assert rs is not None
    Worker._flip_health_in_progress(rs, agent_id, _LOG)
    assert rs.health == "in_progress"
    # In-turn category-specific path writes its give-up red directly,
    # bypassing the resolve lane (matches the live worker.py paths).
    rs.health = in_turn_red
    Worker._resolve_health_on_success(rs, agent_id, _LOG)
    assert rs.health == in_turn_red


# ── _fallback_unhandled_error_if_stuck_in_progress (PR #59 Blocker 2) ────


def test_fallback_unhandled_error_fires_when_swallowed_exception_left_in_progress(
    tmp_path, monkeypatch,
):
    """Mirrors the live shape: ``handle_message_batch`` raised a non-
    AgentAPIError that the worker swallows. Finally branch must
    backstop in_progress → unhandled_error."""
    agent_id = _seed_runtime(tmp_path, monkeypatch, health="in_progress")
    rs = RuntimeState.load(agent_id)
    assert rs is not None
    Worker._fallback_unhandled_error_if_stuck_in_progress(
        rs, agent_id, "KeyError: 'missing-key'", _LOG,
    )
    assert rs.health == "unhandled_error"
    assert "KeyError" in rs.error
    on_disk = RuntimeState.load(agent_id)
    assert on_disk is not None and on_disk.health == "unhandled_error"


def test_fallback_unhandled_error_leaves_category_red_untouched(tmp_path, monkeypatch):
    """If an in-turn give-up red was already written, the backstop
    must NOT overwrite it (category red carries better detail)."""
    agent_id = _seed_runtime(tmp_path, monkeypatch, health="api_error_abandoned")
    rs = RuntimeState.load(agent_id)
    assert rs is not None
    Worker._fallback_unhandled_error_if_stuck_in_progress(
        rs, agent_id, "anything", _LOG,
    )
    assert rs.health == "api_error_abandoned"


def test_fallback_unhandled_error_default_error_when_caller_passes_none(
    tmp_path, monkeypatch,
):
    agent_id = _seed_runtime(tmp_path, monkeypatch, health="in_progress")
    rs = RuntimeState.load(agent_id)
    assert rs is not None
    Worker._fallback_unhandled_error_if_stuck_in_progress(rs, agent_id, None, _LOG)
    assert rs.health == "unhandled_error"
    assert rs.error  # populated even when caller had no error text


# ── chain integration tests (PR #59 Blocker 1 + Blocker 2) ───────


def test_chain_agent_api_error_then_retry_success_resolves_to_ok(
    tmp_path, monkeypatch,
):
    """Blocker 1 chain: AgentAPIError raise leaves in_progress
    intact (turn_will_retry → backstop skipped), consumer kick-retry
    succeeds → on_turn_success fires → resolve clears to ok.

    Walks the helper sequence the live worker now invokes; without
    the round-1 fix the chain would terminate with in_progress on
    disk forever.
    """
    agent_id = _seed_runtime(tmp_path, monkeypatch, health="ok")
    rs = RuntimeState.load(agent_id)
    assert rs is not None

    # T0: on_message_batch flips to in_progress.
    Worker._flip_health_in_progress(rs, agent_id, _LOG)
    assert rs.health == "in_progress"

    # T1: handle_message_batch raises AgentAPIError → finally's
    # success branch is skipped. turn_will_retry=True, so the
    # backstop also skips. in_progress survives.
    # (Simulated by reading the on-disk value after a no-op.)
    on_disk = RuntimeState.load(agent_id)
    assert on_disk is not None and on_disk.health == "in_progress"

    # T2: consumer kicks the retry; it succeeds; on_turn_success
    # fires the same helper pair the worker installed in this PR.
    Worker._clear_api_error_abandoned_if_recoverable(
        rs, agent_id, "root_x", _LOG,
    )
    Worker._resolve_health_on_success(rs, agent_id, _LOG)

    assert rs.health == "ok"
    on_disk = RuntimeState.load(agent_id)
    assert on_disk is not None and on_disk.health == "ok"


def test_chain_non_api_error_swallow_falls_back_to_unhandled_error(
    tmp_path, monkeypatch,
):
    """Blocker 2 chain: non-AgentAPIError swallow (e.g., KeyError)
    means turn_succeeded=False AND turn_will_retry=False. Without the
    backstop, in_progress would persist until daemon restart. With
    the backstop, the finally lands at unhandled_error + populated error.
    """
    agent_id = _seed_runtime(tmp_path, monkeypatch, health="ok")
    rs = RuntimeState.load(agent_id)
    assert rs is not None

    Worker._flip_health_in_progress(rs, agent_id, _LOG)
    assert rs.health == "in_progress"

    Worker._fallback_unhandled_error_if_stuck_in_progress(
        rs, agent_id, "KeyError: 'thread_root_id'", _LOG,
    )

    assert rs.health == "unhandled_error"
    assert "KeyError" in rs.error
    on_disk = RuntimeState.load(agent_id)
    assert on_disk is not None and on_disk.health == "unhandled_error"


# ── StatusReporter heartbeat shape ───────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_carries_both_status_and_health():
    from puffo_agent.agent.status_reporter import StatusReporter
    mock_http = AsyncMock()
    mock_http.keyless = False  # AsyncMock auto-truthies attrs; pin native so the heartbeat isn't skipped
    health_value = {"v": "in_progress"}
    reporter = StatusReporter(
        mock_http,
        runtime_health_provider=lambda: health_value["v"],
    )
    reporter._current_status = "busy"
    reporter._current_message_id = "env_abc"
    await reporter._send_heartbeat()
    mock_http.post.assert_called_once()
    args, _kwargs = mock_http.post.call_args
    path, body = args
    assert path == "/agents/me/heartbeat"
    assert body == {
        "status": "busy",
        "current_message_id": "env_abc",
        "health": "in_progress",
    }


@pytest.mark.asyncio
async def test_heartbeat_without_provider_omits_health_field():
    """Back-compat: when constructed without a provider (no-op /
    tests), heartbeat carries the legacy single-stream shape."""
    from puffo_agent.agent.status_reporter import StatusReporter
    mock_http = AsyncMock()
    mock_http.keyless = False  # AsyncMock auto-truthies attrs; pin native so the heartbeat isn't skipped
    reporter = StatusReporter(mock_http)
    reporter._current_status = "idle"
    await reporter._send_heartbeat()
    args, _kwargs = mock_http.post.call_args
    _path, body = args
    assert body == {"status": "idle"}
    assert "health" not in body


@pytest.mark.asyncio
async def test_heartbeat_provider_exception_does_not_break_heartbeat():
    from puffo_agent.agent.status_reporter import StatusReporter
    mock_http = AsyncMock()
    mock_http.keyless = False  # AsyncMock auto-truthies attrs; pin native so the heartbeat isn't skipped

    def broken_provider() -> str:
        raise RuntimeError("runtime not loaded")

    reporter = StatusReporter(
        mock_http,
        runtime_health_provider=broken_provider,
    )
    reporter._current_status = "idle"
    # Should not raise.
    await reporter._send_heartbeat()
    mock_http.post.assert_called_once()
    _args, _kwargs = mock_http.post.call_args
    body = _args[1]
    # health field is OMITTED on provider failure; status still ships.
    assert body == {"status": "idle"}


# ── CLI surfaced-health includes new values ──────────────────────


def test_cli_surfaced_health_tuple_includes_new_values():
    """Source-string check on cli.py's cmd_agent_list tuple (running
    the CLI does heavy daemon init). Pins the new PUF-270 values."""
    cli_path = (
        Path(__file__).parent.parent
        / "src" / "puffo_agent" / "portal" / "cli.py"
    )
    text = cli_path.read_text(encoding="utf-8")
    for required in (
        "in_progress",
        "unhandled_error",
        "auth_failed",
        "api_error_abandoned",
        "refresh_broken",
    ):
        assert f'"{required}"' in text, (
            f"surfaced-health tuple missing {required!r}"
        )
