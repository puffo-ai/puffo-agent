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


# ── StatusReporter heartbeat shape ───────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_carries_both_status_and_health():
    from puffo_agent.agent.status_reporter import StatusReporter
    mock_http = AsyncMock()
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


# ── CLI surfaced-health includes in_progress ─────────────────────


def test_cli_surfaced_health_tuple_includes_in_progress():
    """Sanity-check that the tuple in cli.py adds in_progress to the
    list of values that get the ``[<health>]`` suffix in ``agent
    list``. Source-string check rather than running the CLI subcommand
    because the CLI does heavy daemon/runtime initialization."""
    cli_path = (
        Path(__file__).parent.parent
        / "src" / "puffo_agent" / "portal" / "cli.py"
    )
    text = cli_path.read_text(encoding="utf-8")
    # The surfaced-health tuple lives in cmd_agent_list. Locate the
    # tuple by anchor + assert in_progress is a member.
    assert '"in_progress"' in text, (
        "in_progress must be in cli.py surfaced-health tuple so the "
        "operator can see at a glance the agent is alive mid-turn"
    )
    # Defensive: the legacy reds must still surface — no regression.
    for legacy in ("auth_failed", "api_error_abandoned", "refresh_broken"):
        assert f'"{legacy}"' in text, (
            f"surfaced-health tuple lost {legacy!r}"
        )
