"""Desktop compaction activity follows the worker snapshot independently per agent."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from puffo_agent.portal.state import RuntimeState
from puffo_agent.portal.ui.widgets.agent_list import AgentList, AgentSummary


@pytest.fixture
def local_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_worker_activity_reaches_local_snapshot_before_reporter(local_home, monkeypatch):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime
    from puffo_agent.portal.worker_run import StandardWorkerRun

    captured = {}
    def builder(prepared, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr(local_runtime, "build_local_runtime_adapter", builder)
    worker = SimpleNamespace(runtime=RuntimeState(status="running"))
    prepared = SimpleNamespace(
        native_session_id="", harness_name="codex",
        preparer=SimpleNamespace(agent_id="desktop-test"),
        spec=SimpleNamespace(mcp_generation=""),
    )
    await StandardWorkerRun(worker)._bind_driver_runtime(
        SimpleNamespace(set_active_turn=lambda *a, **kw: None), prepared, {},
    )
    sink = captured["activity_sink"]
    for activity in ("compacting", None, "compacting", None):
        await sink(activity)
        summary = AgentSummary.for_id("desktop-test")
        assert summary.status == "running"
        assert summary.activity == activity
        assert worker._pending_activity == activity


def test_multiple_agents_display_and_clear_activity(local_home):
    app = QApplication.instance() or QApplication([])
    view = AgentList()
    for agent_id in ("one", "two"):
        RuntimeState(status="running", activity="compacting").save(agent_id)
    loader = lambda: [AgentSummary.for_id(a) for a in ("one", "two")]
    view.refresh(loader)
    assert view._list.count() == 2
    assert all("Compacting" in row._sub_label.text() for row in view._rows.values())
    RuntimeState(status="running").save("one")
    view.refresh(loader)
    assert "Compacting" not in view._rows["one"]._sub_label.text()
    assert "Compacting" in view._rows["two"]._sub_label.text()
    view.close()
    app.processEvents()


def test_long_warm_compaction_remains_visible(local_home, monkeypatch):
    import time
    app = QApplication.instance() or QApplication([])
    RuntimeState(status="starting", activity="compacting").save("warm")
    updated = RuntimeState.load("warm").updated_at
    monkeypatch.setattr(time, "time", lambda: updated + 60)
    assert AgentSummary.for_id("warm").activity == "compacting"
    view = AgentList()
    view.refresh(lambda: [AgentSummary.for_id("warm")])
    assert view._list.count() == 1
    assert "Compacting" in view._rows["warm"]._sub_label.text()
    view.close()
    app.processEvents()


@pytest.mark.parametrize("status", ["stopped", "paused", "error"])
def test_inactive_agent_does_not_show_old_compaction(local_home, status):
    app = QApplication.instance() or QApplication([])
    view = AgentList()
    view._running_only = False
    RuntimeState(status=status, activity="compacting").save("inactive")
    assert RuntimeState.load("inactive").activity is None
    view.refresh(lambda: [AgentSummary.for_id("inactive")])
    assert "Compacting" not in view._rows["inactive"]._sub_label.text()
    view.close()
    app.processEvents()
