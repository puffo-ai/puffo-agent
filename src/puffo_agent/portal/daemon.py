"""Multi-agent reconciler.

Walks ``~/.puffo-agent/agents/`` every ``reconcile_interval_seconds``
and diffs on-disk state against the in-memory task registry. New
agent directories become Workers; directories that disappear or
change their ``state`` field get stopped. The CLI controls the daemon
by mutating the filesystem — no IPC needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import time

from .api import start_api_server, stop_api_server
from .data_service import start_data_service, stop_data_service
from .state import (
    AgentConfig,
    DaemonConfig,
    agent_dir,
    agents_dir,
    archive_flag_path,
    archived_dir,
    clear_daemon_pid,
    clear_stop_request,
    delete_flag_path,
    discover_agents,
    home_dir,
    is_daemon_alive,
    read_daemon_pid,
    restart_flag_path,
    stop_request_path,
    write_daemon_pid,
)
from .worker import Worker

logger = logging.getLogger(__name__)


class Daemon:
    def __init__(self, daemon_cfg: DaemonConfig):
        self.daemon_cfg = daemon_cfg
        self.workers: dict[str, Worker] = {}
        self._stop = asyncio.Event()
        # Cap on per-worker warm wait so a wedged warm can't pin the
        # whole reconciler. The worker keeps retrying in the background.
        self._warm_serialise_timeout = 120.0

    async def run(self) -> None:
        logger.info("puffo-agent portal starting; home=%s", home_dir())
        interval = max(0.5, self.daemon_cfg.reconcile_interval_seconds)

        # One-shot version check at startup; non-blocking, best-effort.
        asyncio.ensure_future(_log_outdated_version_warning())

        # Start auxiliary HTTP services. Both are non-fatal on bind
        # failure — the daemon's primary job is still running agents.
        api_runner = await start_api_server(self.daemon_cfg.bridge)
        data_runner = await start_data_service(self.daemon_cfg.data_service)

        try:
            while not self._stop.is_set():
                try:
                    await self._reconcile_once()
                except Exception as exc:
                    logger.error("reconcile tick crashed: %s", exc, exc_info=True)
                # File-sentinel shutdown path. Required on Windows
                # where ``loop.add_signal_handler`` is unsupported, so
                # ``puffo-agent stop`` can't rely on SIGTERM.
                if stop_request_path().exists():
                    logger.info(
                        "stop sentinel detected at %s; shutting down",
                        stop_request_path(),
                    )
                    self._stop.set()
                    break
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop_all_workers()
            await stop_api_server(api_runner)
            await stop_data_service(data_runner)
            clear_daemon_pid()
            clear_stop_request()
            logger.info("puffo-agent portal stopped")

    def request_stop(self) -> None:
        self._stop.set()

    async def _reconcile_once(self) -> None:
        on_disk = set(discover_agents())
        running = set(self.workers.keys())

        # Agents that disappeared from disk → stop.
        for agent_id in running - on_disk:
            logger.info("agent %s: directory removed, stopping worker", agent_id)
            await self._stop_worker(agent_id)

        # archive.flag → stop worker + move dir to archived/.
        archived_this_tick: set[str] = set()
        for agent_id in sorted(on_disk):
            if archive_flag_path(agent_id).exists():
                await self._archive_on_flag(agent_id)
                archived_this_tick.add(agent_id)
        on_disk -= archived_this_tick

        # delete.flag → stop worker + remove dir (no archived/ copy).
        deleted_this_tick: set[str] = set()
        for agent_id in sorted(on_disk):
            if delete_flag_path(agent_id).exists():
                await self._delete_on_flag(agent_id)
                deleted_this_tick.add(agent_id)
        on_disk -= deleted_this_tick

        # restart.flag → stop worker; the same tick's start path
        # respawns it, producing a visible cycle.
        for agent_id in sorted(on_disk):
            if restart_flag_path(agent_id).exists():
                logger.info(
                    "agent %s: restart.flag detected, stopping worker for re-spawn",
                    agent_id,
                )
                await self._stop_worker(agent_id)
                try:
                    restart_flag_path(agent_id).unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning(
                        "agent %s: couldn't remove restart.flag: %s",
                        agent_id, exc,
                    )

        # Agents on disk → check state and (start | stop | leave alone).
        for agent_id in sorted(on_disk):
            try:
                agent_cfg = AgentConfig.load(agent_id)
            except Exception as exc:
                logger.warning("agent %s: failed to load agent.yml: %s", agent_id, exc)
                continue

            desired_state = agent_cfg.state
            worker = self.workers.get(agent_id)

            if desired_state == "running":
                if worker is None:
                    logger.info("agent %s: starting worker", agent_id)
                    worker = Worker(self.daemon_cfg, agent_cfg)
                    self.workers[agent_id] = worker
                    worker.start()
                    # Serialise heavy startup: ``adapter.warm()`` reads
                    # the persisted session into Node's heap, so N
                    # parallel warms can OOM the host. Awaiting one at
                    # a time keeps peak RSS bounded.
                    await worker.wait_warm(timeout=self._warm_serialise_timeout)
                elif _worker_needs_restart(worker.agent_cfg, agent_cfg):
                    logger.info("agent %s: config changed, restarting worker", agent_id)
                    await self._stop_worker(agent_id)
                    worker = Worker(self.daemon_cfg, agent_cfg)
                    self.workers[agent_id] = worker
                    worker.start()
                    await worker.wait_warm(timeout=self._warm_serialise_timeout)
                else:
                    worker.agent_cfg = agent_cfg
            elif desired_state == "paused":
                if worker is not None:
                    logger.info("agent %s: state=paused, stopping worker", agent_id)
                    await self._stop_worker(agent_id)
            else:
                logger.warning("agent %s: unknown state %r", agent_id, desired_state)

    async def _stop_worker(self, agent_id: str) -> None:
        worker = self.workers.pop(agent_id, None)
        if worker is not None:
            await worker.stop()

    async def _stop_all_workers(self) -> None:
        ids = list(self.workers.keys())
        await asyncio.gather(*(self._stop_worker(i) for i in ids), return_exceptions=True)

    async def _archive_on_flag(self, agent_id: str) -> None:
        """Stop the worker and move its dir to
        ``archived/<id>-ws-<stamp>/``. The ``-ws-`` suffix marks
        WS-cascade archives (operator-initiated has no suffix,
        sync-driven uses ``-sync-``)."""
        logger.warning(
            "agent %s: archive.flag detected, stopping worker + archiving",
            agent_id,
        )
        await self._stop_worker(agent_id)
        src = agent_dir(agent_id)
        if not src.exists():
            return
        archived_dir().mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = archived_dir() / f"{agent_id}-ws-{stamp}"
        try:
            shutil.move(str(src), str(dest))
            logger.info("agent %s: archived to %s", agent_id, dest)
        except OSError as exc:
            logger.error(
                "agent %s: archive failed: %s (flag still present — will retry next tick)",
                agent_id, exc,
            )

    async def _delete_on_flag(self, agent_id: str) -> None:
        """Stop the worker and remove the agent dir entirely
        (destructive — no archived/ copy). On removal failure the
        flag stays so the next tick retries; the worker remains
        stopped."""
        logger.warning(
            "agent %s: delete.flag detected, stopping worker + removing dir",
            agent_id,
        )
        await self._stop_worker(agent_id)
        src = agent_dir(agent_id)
        if not src.exists():
            return
        try:
            shutil.rmtree(src)
            logger.info("agent %s: deleted", agent_id)
        except OSError as exc:
            logger.error(
                "agent %s: delete failed: %s (flag still present — will retry next tick)",
                agent_id, exc,
            )


async def _log_outdated_version_warning() -> None:
    """Compare local version with latest GitHub release; WARN if
    behind. Best-effort — network/metadata errors silently skip."""
    # Lazy import: cli ↔ daemon module cycle at load time.
    from .cli import (
        fetch_latest_release_tag,
        get_local_version,
        is_outdated,
        is_source_install,
        upgrade_command_for_install_mode,
    )
    if is_source_install():
        # Editable installs are often ahead of the latest tag.
        return
    try:
        local = get_local_version()
        remote = await asyncio.to_thread(fetch_latest_release_tag)
    except Exception:
        return
    if not remote:
        return
    if is_outdated(local, remote):
        logger.warning(
            "puffo-agent %s is behind the latest release (%s). "
            "this daemon may be missing features or fixes documented "
            "on github. to upgrade: %s",
            local, remote, upgrade_command_for_install_mode(),
        )
    else:
        logger.info(
            "puffo-agent %s (latest release: %s)", local, remote,
        )


def _worker_needs_restart(old, new) -> bool:
    """True when identity/profile/runtime changed and the worker's
    WS session, keystore, or subprocess is now stale. Trigger-rule
    edits don't count — those re-read on every turn."""
    return (
        old.puffo_core != new.puffo_core
        or old.profile != new.profile
        or old.runtime != new.runtime
    )


async def run_daemon() -> int:
    # Single-daemon enforcement.
    if is_daemon_alive():
        pid = read_daemon_pid()
        logger.error("another daemon is already running (pid=%s)", pid)
        return 1

    home_dir().mkdir(parents=True, exist_ok=True)
    agents_dir().mkdir(parents=True, exist_ok=True)

    daemon_cfg = DaemonConfig.load()
    write_daemon_pid(os.getpid())

    daemon = Daemon(daemon_cfg)

    loop = asyncio.get_running_loop()

    def handle_signal() -> None:
        logger.info("received stop signal; shutting down")
        daemon.request_stop()

    # SIGINT/SIGTERM on posix.
    posix_handlers_installed = False
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, handle_signal)
            posix_handlers_installed = True
        except NotImplementedError:
            # Windows proactor loop doesn't support add_signal_handler.
            pass

    # Windows fallback: synchronous C-runtime Ctrl+C handler dispatched
    # back onto the loop via call_soon_threadsafe. Without this the
    # only graceful-stop path on Windows is the file sentinel.
    if not posix_handlers_installed:
        def _windows_sigint(*_args) -> None:
            loop.call_soon_threadsafe(daemon.request_stop)
        try:
            signal.signal(signal.SIGINT, _windows_sigint)
        except (ValueError, OSError):
            # Not on the main thread, or already trapped.
            pass

    await daemon.run()

    # Cancel surviving tasks; otherwise asyncio.run can hang on
    # Windows when a subprocess transport is still alive.
    survivors = [
        t for t in asyncio.all_tasks(loop)
        if t is not asyncio.current_task()
    ]
    for t in survivors:
        t.cancel()
    if survivors:
        try:
            await asyncio.wait_for(
                asyncio.gather(*survivors, return_exceptions=True),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "shutdown: %d tasks still running after 5s; exiting anyway",
                sum(1 for t in survivors if not t.done()),
            )

    # Hard exit avoids loop.close() hangs on leftover subprocess
    # transports; workers + adapters are already torn down.
    logger.info("shutdown complete; exiting")
    os._exit(0)
