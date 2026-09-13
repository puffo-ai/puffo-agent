"""A blocked Windows provider must not pin the machine control loop."""

import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

from puffo_agent.portal import daemon as daemon_module
from puffo_agent.portal.daemon import Daemon
from puffo_agent.portal.state import AgentConfig, DaemonConfig, RuntimeConfig


@pytest.fixture
def machine(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    # Patch the platform boundary, not os.name (which breaks pathlib on CI).
    monkeypatch.setattr(daemon_module, "_windows_scheduling", lambda: True, raising=False)
    started = asyncio.Queue()
    stopped = asyncio.Queue()
    released = asyncio.Event()
    starts = Counter()

    class ProviderWorker:
        def __init__(self, _daemon_cfg, agent_cfg, **_kwargs):
            self.agent_cfg = agent_cfg
            self.restart_required = False
            self.runtime = SimpleNamespace(status="starting")

        def start(self):
            starts[self.agent_cfg.id] += 1
            started.put_nowait(self.agent_cfg.id)

        async def wait_warm(self, *, timeout):
            if self.agent_cfg.id.startswith("slow"):
                await released.wait()
            self.runtime.status = "running"
            return True

        async def stop(self):
            stopped.put_nowait(self.agent_cfg.id)

    monkeypatch.setattr(daemon_module, "Worker", ProviderWorker)
    daemon = Daemon(DaemonConfig())

    def add(agent_id, state="running"):
        cfg = AgentConfig(id=agent_id, state=state, runtime=RuntimeConfig(harness="pi"))
        cfg.save()
        return cfg

    return SimpleNamespace(
        daemon=daemon, add=add, started=started, stopped=stopped,
        starts=starts, released=released,
    )


async def _finish(machine, loop):
    machine.daemon._stop.set()
    machine.released.set()
    await asyncio.wait_for(loop, 3)
    await machine.daemon._settle_agent_operations()
    await machine.daemon._stop_all_workers()


@pytest.mark.asyncio
async def test_new_agent_starts_while_another_provider_is_still_warming(machine):
    """Previously the first 120-second warm prevented discovery of new agents."""
    machine.add("slow-first")
    loop = asyncio.create_task(machine.daemon._run_reconcile_loop(0, 0.01, None))
    try:
        assert await asyncio.wait_for(machine.started.get(), 3) == "slow-first"
        machine.add("fast-new")
        assert await asyncio.wait_for(machine.started.get(), 3) == "fast-new"
        assert machine.starts == {"slow-first": 1, "fast-new": 1}
    finally:
        await _finish(machine, loop)


@pytest.mark.asyncio
async def test_pause_interrupts_its_own_warm_without_waiting_for_the_provider(machine):
    """Persisted pause must reach a worker even while its warm hangs."""
    cfg = machine.add("slow-paused")
    loop = asyncio.create_task(machine.daemon._run_reconcile_loop(0, 0.01, None))
    try:
        assert await asyncio.wait_for(machine.started.get(), 3) == cfg.id
        cfg.state = "paused"
        cfg.save()
        assert await asyncio.wait_for(machine.stopped.get(), 3) == cfg.id
        assert cfg.id not in machine.daemon.workers
    finally:
        await _finish(machine, loop)


@pytest.mark.asyncio
async def test_start_limit_single_flight_and_shutdown_do_not_spawn_queued_agents(machine):
    """Repeated ticks cannot multiply heavy warms or start queued work after stop."""
    for agent_id in ("slow-a", "slow-b", "slow-c"):
        machine.add(agent_id)
    daemon = machine.daemon
    daemon._dispatch_windows_reconcile()
    assert await asyncio.wait_for(machine.started.get(), 3) == "slow-a"
    assert await asyncio.wait_for(machine.started.get(), 3) == "slow-b"
    for _ in range(5):
        daemon._dispatch_windows_reconcile()
    # All three tasks have run up to their first wait; the third waits for a slot.
    assert machine.starts == {"slow-a": 1, "slow-b": 1}
    daemon._stop.set()
    await asyncio.wait_for(daemon._settle_agent_operations(), 3)
    await daemon._stop_all_workers()
    assert machine.starts == {"slow-a": 1, "slow-b": 1}
    assert not daemon.workers
    assert not daemon._starting_agents


@pytest.mark.asyncio
async def test_archive_network_wait_does_not_block_create_or_lose_shutdown_cleanup(
    machine, monkeypatch,
):
    """A slow archived heartbeat must neither block discovery nor be cancelled halfway."""
    from puffo_agent.portal.state import agent_dir, archive_flag_path

    entered = asyncio.Event()
    released = asyncio.Event()

    async def report(_cfg, status, **_kwargs):
        assert status == "archived"
        entered.set()
        await released.wait()
        return True

    async def revoke(*_args, **_kwargs):
        return None

    monkeypatch.setattr(daemon_module, "_report_lifecycle", report)
    monkeypatch.setattr("puffo_agent.portal.import_agents.revoke_archived_device", revoke)
    cfg = machine.add("archive-me")
    cfg.puffo_core.server_url = "http://127.0.0.1:9"
    cfg.puffo_core.slug = "archive-me"
    cfg.puffo_core.device_id = "archive-device"
    cfg.puffo_core.space_id = "archive-space"
    cfg.save()
    flag = archive_flag_path("archive-me")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    daemon = machine.daemon
    loop = asyncio.create_task(daemon._run_reconcile_loop(0, 0.01, None))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not agent_dir("archive-me").exists()
        machine.add("fast-new")
        assert await asyncio.wait_for(machine.started.get(), 3) == "fast-new"
        daemon._stop.set()
        settlement = asyncio.create_task(daemon._settle_agent_operations())
        await asyncio.sleep(0)
        assert not settlement.done()
        released.set()
        await asyncio.wait_for(settlement, 3)
    finally:
        released.set()
        await _finish(machine, loop)
