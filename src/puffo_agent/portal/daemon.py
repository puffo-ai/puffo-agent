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
import threading
import time

from pathlib import Path

from ..macos.keychain import CredentialCache, is_macos
from .api import start_api_server, stop_api_server
from .ws_local.hub import WsLocalHub
from .credential_refresh import (
    CodexFileBackend,
    CredentialRefresher,
    FileBackend,
    KeychainBackend,
)
from .data_service import set_profile_setter, start_data_service, stop_data_service
from .host_mcp_handler import HostMcpContext
from .rpc_service import set_rpc_resolver, start_rpc_service, stop_rpc_service
from .state import (
    AgentConfig,
    DaemonConfig,
    agent_dir,
    agent_home_dir,
    agent_yml_path,
    agents_dir,
    archive_flag_path,
    archived_dir,
    clear_daemon_pid,
    clear_refresh_token_request,
    clear_stop_request,
    delete_flag_path,
    discover_agents,
    home_dir,
    is_daemon_alive,
    read_daemon_pid,
    refresh_token_request_path,
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
        self._paused_reported: set[str] = set()
        # Shared attach registry: ws-local Workers register here; the
        # bridge's /v1/ws-local route serves tools against it.
        self.ws_local_hub = WsLocalHub()
        self._stop = asyncio.Event()
        # Cap on per-worker warm wait so a wedged warm can't pin the
        # whole reconciler. The worker keeps retrying in the background.
        self._warm_serialise_timeout = 120.0
        # agent.yml mtime cache; reconcile tick skips yaml.safe_load
        # when (mtime_ns, size) is unchanged.
        self._agent_cfg_cache: dict[str, tuple[int, int, "AgentConfig"]] = {}
        # PUF-221: daemon owns Claude OAuth refresh — single writer to
        # the canonical credential store so Anthropic's single-use
        # refresh-token rotation can't be raced by N agent workers.
        # Backend choice is platform-dependent:
        #   - macOS: Keychain is canonical (Claude Code 2.x); cache +
        #     per-agent file copies via KeychainBackend.
        #   - Linux/Windows: host file ``~/.claude/.credentials.json``
        #     is canonical; agent files are symlinks via FileBackend.
        if is_macos():
            home = home_dir()
            backend = KeychainBackend(
                home=home,
                cache=CredentialCache.at(home),
            )
        else:
            backend = FileBackend(host_home=Path.home())
        self.refresher = CredentialRefresher(backend=backend)
        # Sibling refresher for codex OAuth (~/.codex/auth.json). Always
        # FileBackend — the per-agent config.toml pins codex into file
        # mode (see ``write_codex_mcp_config``) so we don't need a
        # macOS-Keychain variant here. Both refreshers share the
        # daemon's event loop but have independent locks + poll loops;
        # they touch different files so there's no contention.
        self.codex_refresher = CredentialRefresher(
            backend=CodexFileBackend(host_home=Path.home()),
        )

    async def run(self) -> None:
        logger.info("puffo-agent portal starting; home=%s", home_dir())
        interval = max(0.5, self.daemon_cfg.reconcile_interval_seconds)

        # api-puffo: ingest any pending install bundles into agent_dir
        # layout BEFORE reconcile picks them up, so the first tick
        # already sees the new agents.
        try:
            from ..agent.api_puffo.bundle import sweep_install_dir
            n_new = sweep_install_dir()
            if n_new:
                logger.info("api-puffo: ingested %d new bundle(s)", n_new)
        except Exception as exc:  # noqa: BLE001
            logger.warning("api-puffo: install sweep failed: %s", exc)

        # One-shot version check at startup; non-blocking, best-effort.
        asyncio.ensure_future(_log_outdated_version_warning())
        # Re-assert machine_id for already-linked operators' agents so agents
        # created/paused before linking show as remote without a re-link.
        asyncio.ensure_future(_migrate_linked_agents_at_startup())
        # Re-push every owned agent's profile to the server in case
        # the operator hand-edited agent.yml / profile.md offline.
        asyncio.ensure_future(_full_sync_all_owned_agents_at_startup())

        # Start auxiliary HTTP services. Both are non-fatal on bind
        # failure — the daemon's primary job is still running agents.
        api_runner = await start_api_server(
            self.daemon_cfg.bridge, ws_local_hub=self.ws_local_hub,
        )
        set_profile_setter(self._set_worker_profile_cache)
        set_rpc_resolver(self._resolve_host_mcp_context)
        # Bridge pins 63387 (browser clients hard-code it). Both
        # fallbacks scan from 63388 onward so neither collides with
        # bridge; data starts after rpc so its fallback can route
        # past rpc's resolved port.
        rpc_runner = await start_rpc_service(
            self.daemon_cfg.rpc_service, fallback_start=63388,
        )
        data_runner = await start_data_service(
            self.daemon_cfg.data_service,
            fallback_start=max(63388, self.daemon_cfg.rpc_service.port + 1),
        )
        refresher_task = asyncio.ensure_future(
            self.refresher.run_loop(self._stop)
        )
        codex_refresher_task = asyncio.ensure_future(
            self.codex_refresher.run_loop(self._stop)
        )
        from .control.client import ControlManager

        control_manager = ControlManager()
        control_task = asyncio.ensure_future(control_manager.run())

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
                # PUF-221: ``puffo-agent agent refresh-token`` flag —
                # wake the credential refresher so the operator can
                # force a refresh + fan-out without waiting for the
                # 2-min poll.
                if refresh_token_request_path().exists():
                    logger.info("refresh-token sentinel detected; notifying refreshers")
                    self.refresher.notify_refresh_needed()
                    self.codex_refresher.notify_refresh_needed()
                    clear_refresh_token_request()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop_all_workers()
            control_manager.stop()
            for t in (refresher_task, codex_refresher_task, control_task):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            await stop_api_server(api_runner)
            set_profile_setter(None)
            set_rpc_resolver(None)
            await stop_data_service(data_runner)
            await stop_rpc_service(rpc_runner)
            clear_daemon_pid()
            clear_stop_request()
            logger.info("puffo-agent portal stopped")

    def request_stop(self) -> None:
        self._stop.set()

    def _load_agent_cfg_cached(self, agent_id: str) -> "AgentConfig":
        """Reuses a cached parse when (mtime_ns, size) is unchanged.
        Same exceptions as ``AgentConfig.load`` on parse failure."""
        path = agent_yml_path(agent_id)
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        cached = self._agent_cfg_cache.get(agent_id)
        if cached is not None and (cached[0], cached[1]) == key:
            return cached[2]
        cfg = AgentConfig.load(agent_id)
        self._agent_cfg_cache[agent_id] = (st.st_mtime_ns, st.st_size, cfg)
        return cfg

    async def _reconcile_once(self) -> None:
        on_disk = set(discover_agents())
        running = set(self.workers.keys())

        # Drop cached parses for ids that vanished — guards against a
        # re-created id serving a stale config.
        for stale_id in list(self._agent_cfg_cache.keys() - on_disk):
            self._agent_cfg_cache.pop(stale_id, None)

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
                agent_cfg = self._load_agent_cfg_cached(agent_id)
            except Exception as exc:
                logger.warning("agent %s: failed to load agent.yml: %s", agent_id, exc)
                continue

            desired_state = agent_cfg.state
            worker = self.workers.get(agent_id)

            if desired_state == "running":
                self._paused_reported.discard(agent_id)
                if worker is None:
                    logger.info("agent %s: starting worker", agent_id)
                    worker = Worker(
                        self.daemon_cfg,
                        agent_cfg,
                        notify_refresh_needed=self._notify_refresh_for(agent_cfg),
                        ws_local_hub=self.ws_local_hub,
                    )
                    self.workers[agent_id] = worker
                    self._register_with_refresher(agent_cfg, worker)
                    worker.start()
                    # Serialise heavy startup: ``adapter.warm()`` reads
                    # the persisted session into Node's heap, so N
                    # parallel warms can OOM the host. Awaiting one at
                    # a time keeps peak RSS bounded.
                    await worker.wait_warm(timeout=self._warm_serialise_timeout)
                elif _worker_needs_restart(worker.agent_cfg, agent_cfg):
                    logger.info("agent %s: config changed, restarting worker", agent_id)
                    await self._stop_worker(agent_id)
                    worker = Worker(
                        self.daemon_cfg,
                        agent_cfg,
                        notify_refresh_needed=self._notify_refresh_for(agent_cfg),
                        ws_local_hub=self.ws_local_hub,
                    )
                    self.workers[agent_id] = worker
                    self._register_with_refresher(agent_cfg, worker)
                    worker.start()
                    await worker.wait_warm(timeout=self._warm_serialise_timeout)
                else:
                    worker.agent_cfg = agent_cfg
            elif desired_state == "paused":
                if worker is not None:
                    logger.info("agent %s: state=paused, stopping worker", agent_id)
                    await self._stop_worker(agent_id)
                    # Worker's gone → it can't heartbeat "paused"; the daemon
                    # reports it so the operator's portal reflects the pause.
                    if await _report_lifecycle(agent_cfg, "paused"):
                        self._paused_reported.add(agent_id)
                elif agent_id not in self._paused_reported:
                    # Paused with no worker (e.g. after a daemon restart) —
                    # assert the state once so the portal isn't stuck stale.
                    if await _report_lifecycle(agent_cfg, "paused"):
                        self._paused_reported.add(agent_id)
            else:
                logger.warning("agent %s: unknown state %r", agent_id, desired_state)

    def _refresher_for(self, agent_cfg: AgentConfig) -> CredentialRefresher:
        """Pick the right refresher for an agent's harness. Codex
        agents only need their own auth.json refresh; claude-code +
        every other harness routes through the Claude refresher."""
        if (agent_cfg.runtime.harness or "claude-code") == "codex":
            return self.codex_refresher
        return self.refresher

    def _register_with_refresher(
        self, agent_cfg: AgentConfig, worker: Worker,
    ) -> None:
        refresher = self._refresher_for(agent_cfg)
        refresher.register_agent(agent_home_dir(agent_cfg.id))
        agent_id = agent_cfg.id

        def on_refresh_success() -> None:
            was_auth_failed = worker.runtime.health == "auth_failed"
            Worker._clear_auth_failed_if_recoverable(
                worker.runtime, agent_id, logger,
            )
            # Re-arm the auth_failed DM dedup so a re-expiry this
            # session re-notifies the operator.
            worker._auth_failed_notification_sent = False
            if was_auth_failed:
                # The running adapter session still holds the stale
                # credential; a restart re-links the fresh cred and
                # redelivers the stalled batch (cursor wasn't advanced).
                try:
                    flag = restart_flag_path(agent_id)
                    flag.parent.mkdir(parents=True, exist_ok=True)
                    flag.write_text("")
                    logger.info(
                        "agent %s: credential recovered — requesting restart "
                        "to pick up the new credential", agent_id,
                    )
                except OSError as exc:
                    logger.warning(
                        "agent %s: could not write restart flag: %s",
                        agent_id, exc,
                    )

        refresher.register_on_refresh_success(on_refresh_success)
        # Stash callback identity for _stop_worker's unregister.
        worker._refresh_success_callback = on_refresh_success

    def _notify_refresh_for(self, agent_cfg: AgentConfig):
        return self._refresher_for(agent_cfg).notify_refresh_needed

    def _set_worker_profile_cache(
        self, agent_id: str, slug: str, display_name: str, avatar_url: str,
    ) -> None:
        """Data-service shim — find the worker for ``agent_id`` and
        inject fresh profile values into its in-memory cache. Called
        from the data service's POST profile-cache route, which the
        MCP ``get_user_info`` tool hits right after fetching from
        puffo-server. Silently no-ops when the worker is gone (agent
        stopped between the tool's fetch and the POST)."""
        worker = self.workers.get(agent_id)
        if worker is None:
            return
        worker.set_profile_cache(slug, display_name, avatar_url)

    def _resolve_host_mcp_context(self, agent_id: str) -> "HostMcpContext | None":
        """Rpc-service shim. Returns None when the worker isn't warm yet."""
        worker = self.workers.get(agent_id)
        if worker is None:
            return None
        return worker.host_mcp_context()

    async def _stop_worker(self, agent_id: str) -> None:
        worker = self.workers.pop(agent_id, None)
        if worker is not None:
            # Unregister from both refreshers — set ops are idempotent
            # and we don't keep harness info after the worker dies.
            home = agent_home_dir(agent_id)
            self.refresher.unregister_agent(home)
            self.codex_refresher.unregister_agent(home)
            cb = getattr(worker, "_refresh_success_callback", None)
            if cb is not None:
                self.refresher.unregister_on_refresh_success(cb)
                self.codex_refresher.unregister_on_refresh_success(cb)
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
        # Report archived before the dir (+ keystore) moves out from under us.
        try:
            await _report_lifecycle(AgentConfig.load(agent_id), "archived")
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent %s: archived report failed: %s", agent_id, exc)
        src = agent_dir(agent_id)
        if not src.exists():
            return
        await _drain_codex_tmp(src)
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
        await _drain_codex_tmp(src)
        try:
            shutil.rmtree(src)
            logger.info("agent %s: deleted", agent_id)
        except OSError as exc:
            logger.error(
                "agent %s: delete failed: %s (flag still present — will retry next tick)",
                agent_id, exc,
            )


async def _report_lifecycle(agent_cfg: AgentConfig, status: str) -> bool:
    """Report an operator lifecycle state (paused/archived) to the server as the
    agent. The worker is stopped at this point, so it can't heartbeat the state
    itself; the daemon does it out-of-band. Returns True when the report is
    *settled* — delivered, or rejected with a permanent 4xx that retrying can't
    fix — so the caller stops re-reporting; False only on a transient failure
    (5xx / network) worth retrying next tick."""
    from ..crypto.http_client import HttpError, PuffoCoreHttpClient
    from ..crypto.keystore import KeyStore
    from .control.store import current_machine_id

    pc = agent_cfg.puffo_core
    if not pc.is_configured():
        return True  # can never report without config — settled, don't retry
    http = PuffoCoreHttpClient(pc.server_url, KeyStore.for_agent(agent_cfg.id), pc.slug)
    try:
        body: dict = {"status": status}
        machine_id = current_machine_id()
        if machine_id:
            body["machine_id"] = machine_id
        await http.post("/agents/me/heartbeat", body)
        return True
    except HttpError as exc:
        # 4xx is deterministic for this (agent, server) pair (bad certs / unknown
        # status) — settle and stop retrying; only 5xx is worth a retry.
        if 400 <= exc.status < 500:
            logger.warning(
                "agent %s: lifecycle report %r rejected (HTTP %s); giving up: %s",
                agent_cfg.id, status, exc.status, exc.body,
            )
            return True
        logger.warning(
            "agent %s: lifecycle report %r failed (HTTP %s); will retry",
            agent_cfg.id, status, exc.status,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — transient (network); retry next tick
        logger.warning(
            "agent %s: lifecycle report %r failed; will retry: %s", agent_cfg.id, status, exc
        )
        return False
    finally:
        await http.close()


async def _drain_codex_tmp(src: Path) -> None:
    """Windows: codex's .lock in .codex/tmp/ can outlive the subprocess
    by a few hundred ms; pre-clean so the outer move/rmtree doesn't trip."""
    codex_tmp = src / ".codex" / "tmp"
    if not codex_tmp.exists():
        return
    for _ in range(5):
        try:
            shutil.rmtree(codex_tmp)
            return
        except OSError:
            await asyncio.sleep(0.5)
    shutil.rmtree(codex_tmp, ignore_errors=True)


async def _migrate_linked_agents_at_startup() -> None:
    """For each already-linked operator, stamp machine_id on its owned agents
    so locals created/paused before the link become remote. Best-effort."""
    from .control.link import migrate_owned_agents
    from .control.store import load_pairings

    for pairing in load_pairings().values():
        try:
            n = await migrate_owned_agents(pairing.operator_root_pubkey)
            if n:
                logger.info(
                    "startup: stamped machine_id on %d agent(s) for operator %s",
                    n, pairing.operator_slug,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort, per-operator
            logger.warning(
                "startup machine_id migration failed for %s: %s",
                pairing.operator_slug, exc,
            )


async def _full_sync_all_owned_agents_at_startup() -> None:
    """Push every owned agent's profile to puffo-server on boot —
    defends against offline hand-edits. Independent of link state."""
    from ..crypto.keystore import KeyStore
    from .profile_sync import sync_full_profile

    async def _sync_one(agent_id: str) -> str | None:
        try:
            cfg = AgentConfig.load(agent_id)
        except Exception as exc:  # noqa: BLE001
            return f"{agent_id}: load failed: {exc}"
        if not cfg.puffo_core.is_configured():
            return None
        try:
            KeyStore.for_agent(agent_id).load_session(cfg.puffo_core.slug)
        except Exception:
            return None
        try:
            await sync_full_profile(cfg)
        except Exception as exc:  # noqa: BLE001
            return f"{agent_id}: {exc}"
        return None

    ids = discover_agents()
    if not ids:
        return
    results = await asyncio.gather(
        *(_sync_one(aid) for aid in ids), return_exceptions=False,
    )
    failures = [r for r in results if r]
    ok = len(ids) - len(failures)
    logger.info("startup: full-profile sync — ok=%d failed=%d", ok, len(failures))
    for line in failures:
        logger.warning("startup full-sync: %s", line)


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


def _install_posix_stop_handlers(loop, handle_signal) -> bool:
    """Install SIGINT/SIGTERM via the asyncio loop; return whether it
    did. No-op off the main thread, where ``add_signal_handler`` →
    ``set_wakeup_fd`` raises (the ``--ui`` / ``--background`` DaemonThread
    case — those stop via the file sentinel instead).
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    installed = False
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, handle_signal)
            installed = True
        except NotImplementedError:
            # Windows proactor loop doesn't support add_signal_handler.
            pass
    return installed


async def run_daemon(with_local_bridge: bool = False) -> int:
    # Single-daemon enforcement. ``start`` against an already-running
    # daemon exits 0 (the user wanted a running daemon; one exists) —
    # exit 1 read as an error in upgrade flows. Enforcement is unchanged:
    # we never spawn a second daemon. A different running version isn't
    # discriminated here; ``stop && start`` is the version-swap path.
    if is_daemon_alive():
        pid = read_daemon_pid()
        # print + log: background / tray runners may not surface INFO.
        msg = f"puffo-agent daemon already running (pid={pid})"
        logger.info(msg)
        print(msg)
        return 0

    home_dir().mkdir(parents=True, exist_ok=True)
    agents_dir().mkdir(parents=True, exist_ok=True)

    from .import_agents import cleanup_staging_dir
    cleanup_staging_dir()

    daemon_cfg = DaemonConfig.load()
    if with_local_bridge:
        daemon_cfg.bridge.enabled = True
    write_daemon_pid(os.getpid())

    daemon = Daemon(daemon_cfg)

    loop = asyncio.get_running_loop()

    def handle_signal() -> None:
        logger.info("received stop signal; shutting down")
        daemon.request_stop()

    posix_handlers_installed = _install_posix_stop_handlers(loop, handle_signal)

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
