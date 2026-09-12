"""Prevent stale/private runtime snapshots from becoming reassuring public health."""
import json
import os

import pytest

from puffo_agent.portal.lingtai_health import reported_runtime_health
from puffo_agent.portal.state import RuntimeConfig


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    registry = tmp_path / "puffo/lingtai/runtime-registry.json"
    registry.parent.mkdir(parents=True)
    directory = tmp_path / "source"
    directory.mkdir()
    registry.write_text(json.dumps({"runtimes": {"bound": {
        "runtime_id": "bound", "agent_dir": str(directory), "status": "active",
    }}}))
    runtime = RuntimeConfig(kind="cli-local", harness="acp", harness_command=[
        "/bin/lingtai-agent", "acp", "--profile", "puffo-v1",
        "--runtime-id", "bound", "--registry", str(registry),
    ])

    def snapshot(state="active", *, changed=500, progress=500, turn=None, stamp=999):
        path = directory / ".status.json"
        path.write_text(json.dumps({"runtime": {
            "state": state, "state_changed_at": changed,
            "no_progress_seconds": progress, "running": True,
            "heartbeat_age_seconds": 0,
        }, "active_turn": turn, "private_prompt": "never publish this"}))
        heartbeat = directory / ".agent.heartbeat"
        heartbeat.write_text(str(stamp))
        os.utime(heartbeat, (stamp, stamp))
        os.utime(path, (stamp, stamp))

    def health(**kwargs):
        return reported_runtime_health(runtime=runtime, current_health=kwargs.pop("current_health", "ok"),
            worker_status=kwargs.pop("worker_status", "running"), worker_started_at=900,
            now=1000, **kwargs)

    return directory, registry, runtime, snapshot, health


def test_sustained_stuck_warns_and_recovery_clears_without_mutating_source(source):
    directory, _, _, snapshot, health = source
    snapshot("stuck", changed=800)
    before = (directory / ".status.json").read_bytes()
    assert health() == "runtime_stalled"
    assert (directory / ".status.json").read_bytes() == before
    snapshot("stuck", changed=995)
    assert health() == "ok"
    snapshot("asleep")
    assert health() == "ok"


def test_no_turn_wedge_differs_from_a_long_active_call(source):
    _, _, _, snapshot, health = source
    snapshot(progress=600)
    assert health() == "runtime_stalled"
    snapshot(progress=600, turn={"kind": "tool", "id": "private"})
    assert health() == "ok"
    snapshot(progress=10)
    assert health() == "ok"


def test_hard_kill_frozen_healthy_fields_do_not_prove_liveness(source):
    _, _, _, snapshot, health = source
    snapshot(stamp=980)
    assert health() == "runtime_unresponsive"
    # Old process residue must not diagnose a new worker during warm-up.
    snapshot(stamp=800)
    assert health() == "ok"
    assert health(worker_status="starting") == "ok"


def test_specific_worker_failures_win_and_revoked_bindings_are_not_read(source):
    _, registry, _, snapshot, health = source
    snapshot("stuck", changed=800)
    assert health(current_health="auth_failed") == "auth_failed"
    registry.write_text('{"runtimes":{}}')
    assert health() == "ok"


def test_invalid_or_stale_snapshots_are_not_a_stall_diagnosis(source):
    directory, _, _, snapshot, health = source
    snapshot("stuck", changed=float("nan"))
    assert health() == "ok"
    snapshot("stuck", changed=800)
    os.utime(directory / ".status.json", (800, 800))
    assert health() == "ok"
    (directory / ".status.json").write_text("{")
    assert health() == "ok"


@pytest.mark.asyncio
async def test_worker_projects_health_on_wire_and_clears_after_recovery(source, monkeypatch):
    """Wiring must not silently report only the old worker health or leak snapshots."""
    from types import SimpleNamespace
    from puffo_agent.portal.state import AgentConfig, DaemonConfig
    from puffo_agent.portal.worker import Worker
    from test_status_reporter import FakeHttp

    _, _, runtime, snapshot, _ = source
    monkeypatch.setattr("puffo_agent.portal.lingtai_health.time.time", lambda: 1000)
    worker = Worker(DaemonConfig(), AgentConfig(id="test", runtime=runtime))
    worker.runtime.status = "running"
    worker.runtime.started_at = 900
    worker.runtime.health = "ok"
    http = FakeHttp()
    reporter = worker._build_status_reporter(SimpleNamespace(http=http))
    snapshot("stuck", changed=800)
    await reporter.report_current_status()
    assert http.calls[-1][1]["health"] == "runtime_stalled"
    assert "private" not in json.dumps(http.calls)
    snapshot("asleep")
    await reporter.report_current_status()
    assert http.calls[-1][1]["health"] == "ok"
    assert worker.runtime.health == "ok"  # projection never overwrites the lifecycle owner


def test_graceful_exit_missing_heartbeat_does_not_reuse_a_healthy_snapshot(source):
    directory, _, _, snapshot, health = source
    snapshot("asleep")
    (directory / ".agent.heartbeat").unlink()
    assert health() == "runtime_unresponsive"
    (directory / ".status.json").unlink()
    assert health() == "ok"  # no evidence from an older unsupported runtime
