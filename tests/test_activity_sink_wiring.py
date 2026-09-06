"""Compaction-activity sink wiring: harness events → status refinement."""

from __future__ import annotations

import asyncio

import pytest

from puffo_agent.agent.adapters.base import TurnContext
from puffo_agent.agent.harness.driver import RuntimeSpec
from puffo_agent.agent.runtime_event_outbox import RuntimeEventOutbox
from test_harness_driver import (
    _ToolResultDriver,
    _feed_projection_events,
    _projection_event,
    _reporter_emits,
    _wait_until,
)


@pytest.mark.asyncio
async def test_compaction_events_drive_activity_sink(tmp_path, monkeypatch):
    """``compaction.started``/``completed``/``failed`` must reach the
    activity sink as the fixed "compacting"/None labels (status-refinement
    wiring); a failed compaction clears the overlay exactly like a
    completed one, or the operator reads "Compacting" forever. Removing
    the ``persist_event`` hook turns this red."""
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime
    from puffo_agent.agent.harness.runtime.local_runtime import (
        PreparedLocalRuntime,
        build_local_runtime_adapter,
    )

    class _StubPreparer:
        agent_id = "agent"

    driver = _ToolResultDriver()
    _reporter_emits(monkeypatch)
    prepared = PreparedLocalRuntime(
        harness_name="codex",
        spec=RuntimeSpec(str(tmp_path)),
        native_session_id="",
        migration_source="fresh",
        legacy_session_path=tmp_path / "legacy.json",
        preparer=_StubPreparer(),
    )
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db", max_rows=8)
    monkeypatch.setattr(local_runtime, "build_driver", lambda name: driver)
    seen: list[str | None] = []

    async def sink(activity):
        seen.append(activity)

    adapter = build_local_runtime_adapter(
        prepared,
        outbox=outbox,
        logical_session_ref="logical-session",
        activity_sink=sink,
    )
    manager = adapter.manager
    ctx = TurnContext(
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
        workspace_dir=str(tmp_path),
        claude_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
    )
    task = asyncio.create_task(adapter.run_turn(ctx))
    await _wait_until(lambda: bool(manager._turn_refs))
    await _feed_projection_events(driver, [
        _projection_event("turn.started", {}),
        _projection_event("compaction.started", {}),
        _projection_event("compaction.completed", {}),
        _projection_event("compaction.started", {}),
        _projection_event("compaction.failed", {"diagnostic": "boom"}),
        _projection_event("turn.completed", {"outcome": "succeeded"}),
    ])
    await asyncio.wait_for(task, timeout=5)

    assert seen == ["compacting", None, "compacting", None]
    await adapter.aclose()
    outbox.close()


@pytest.mark.asyncio
async def test_warm_phase_activity_seeds_reporter_on_attach():
    """Warm opens the driver before ``_start_services`` builds the
    reporter; a session-resume compaction in that window parks its
    label on ``worker._pending_activity``. ``_build_reporter`` must
    seed the overlay from it so the heartbeat loop's immediate first
    beat carries the label instead of dropping the whole window."""
    from types import SimpleNamespace

    from puffo_agent.agent.status_reporter import StatusReporter
    from puffo_agent.portal.worker_run import StandardWorkerRun

    class _Http:
        keyless = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def post(self, path, body=None):
            self.calls.append((path, body or {}))
            return {}

    http = _Http()
    reporter = StatusReporter(http, heartbeat_interval_s=999)
    worker = SimpleNamespace(
        _build_status_reporter=lambda client: reporter,
        _pending_activity="compacting",
    )
    runner = StandardWorkerRun(worker)

    built = runner._build_reporter(SimpleNamespace(http=http))

    assert built is reporter
    assert worker._pending_activity is None
    await built.report_current_status()
    assert http.calls[-1][1]["activity"] == "compacting"
