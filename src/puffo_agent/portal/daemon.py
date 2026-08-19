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
from .ws_local.hub import WsLocalHub
from .ws_local.server import start_ws_local_server, stop_ws_local_server
from .credential_refresh import (
    CodexFileBackend,
    CredentialRefresher,
    FileBackend,
    KeychainBackend,
)
from .data_service import (
    set_client_resolver,
    set_profile_setter,
    start_data_service,
    stop_data_service,
)
from .host_mcp_handler import HostMcpContext
from .rpc_service import set_rpc_resolver, start_rpc_service, stop_rpc_service
from .runtime_matrix import RUNTIME_CLI_DOCKER, RUNTIME_CLI_LOCAL
from .state import (
    AgentConfig,
    DaemonConfig,
    agent_dir,
    agent_home_dir,
    agent_yml_path,
    agents_dir,
    archive_flag_path,
    archived_dir,
    claude_cli_api_key,
    clear_daemon_pid,
    clear_daemon_ready,
    clear_refresh_token_request,
    clear_stop_request,
    delete_flag_path,
    discover_agents,
    home_dir,
    is_daemon_alive,
    is_daemon_ready,
    is_pid_alive,
    read_daemon_pid,
    refresh_model_flag_path,
    refresh_provider_auth_flag_path,
    refresh_runtime_flag_path,
    refresh_session_flag_path,
    refresh_token_request_path,
    restart_flag_path,
    shared_fs_dir,
    stop_request_path,
    stop_requested_for,
    write_daemon_pid,
    write_daemon_ready,
)
from .workspace_layout import (
    AVAILABLE_SHARED_WORKSPACE_STATES,
    prepare_workspace_shared_access,
)
from .worker import Worker

logger = logging.getLogger(__name__)


class _DaemonRuntime:
    """Resources acquired incrementally during one daemon generation."""

    def __init__(self) -> None:
        self.startup_tasks: list[asyncio.Task] = []
        self.runtime_tasks: list[asyncio.Task] = []
        self.ws_local_runner = None
        self.rpc_runner = None
        self.data_runner = None
        self.control_manager = None


class Daemon:
    def __init__(self, daemon_cfg: DaemonConfig):
        self.daemon_cfg = daemon_cfg
        self.workers: dict[str, Worker] = {}
        self._paused_reported: set[str] = set()
        # Shared attach registry for the ws-local loopback endpoint.
        self.ws_local_hub = WsLocalHub()
        self._stop = asyncio.Event()
        # Cap on per-worker warm wait so a wedged warm can't pin the
        # whole reconciler. The worker keeps retrying in the background.
        self._warm_serialise_timeout = 120.0
        # agent.yml mtime cache; reconcile tick skips yaml.safe_load
        # when (mtime_ns, size) is unchanged.
        self._agent_cfg_cache: dict[str, tuple[int, int, AgentConfig]] = {}
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

    async def run(
        self,
        external_stop_requested: threading.Event | None = None,
    ) -> None:
        logger.info("puffo-agent portal starting; home=%s", home_dir())
        interval = max(0.5, self.daemon_cfg.reconcile_interval_seconds)
        pid = os.getpid()
        runtime = _DaemonRuntime()
        try:
            await self._start_runtime(runtime)
            if self._stop_was_requested(pid, external_stop_requested):
                logger.info("stop requested during startup; shutting down")
                self._stop.set()
            else:
                write_daemon_ready(pid)
            await self._run_reconcile_loop(
                pid,
                interval,
                external_stop_requested,
            )
        finally:
            await self._shutdown_runtime(runtime, pid)

    async def _start_runtime(self, runtime: _DaemonRuntime) -> None:
        runtime.startup_tasks.append(
            asyncio.ensure_future(_log_outdated_version_warning())
        )
        from ..agent.model_catalog import prefetch as _prefetch_model_catalog

        _prefetch_model_catalog()
        runtime.startup_tasks.extend(
            (
                asyncio.ensure_future(_sweep_archived_pending_revokes_at_startup()),
                asyncio.ensure_future(_migrate_linked_agents_at_startup()),
                asyncio.ensure_future(_full_sync_all_owned_agents_at_startup()),
            )
        )
        runtime.ws_local_runner = await start_ws_local_server(
            self.daemon_cfg.ws_local_service,
            ws_local_hub=self.ws_local_hub,
        )
        set_profile_setter(self._set_worker_profile_cache)
        set_client_resolver(self._resolve_message_client)
        set_rpc_resolver(self._resolve_host_mcp_context)
        runtime.rpc_runner = await start_rpc_service(
            self.daemon_cfg.rpc_service,
            fallback_start=63388,
        )
        runtime.data_runner = await start_data_service(
            self.daemon_cfg.data_service,
            fallback_start=max(63388, self.daemon_cfg.rpc_service.port + 1),
        )
        runtime.runtime_tasks.extend(
            (
                asyncio.ensure_future(self.refresher.run_loop(self._stop)),
                asyncio.ensure_future(self.codex_refresher.run_loop(self._stop)),
            )
        )
        from .control.client import ControlManager

        runtime.control_manager = ControlManager()
        runtime.runtime_tasks.append(
            asyncio.ensure_future(runtime.control_manager.run())
        )
        _respawn_codex_on_mcp_change_at_startup()

    def _stop_was_requested(
        self,
        pid: int,
        external_stop_requested: threading.Event | None,
    ) -> bool:
        return (
            self._stop.is_set()
            or (
                external_stop_requested is not None
                and external_stop_requested.is_set()
            )
            or stop_requested_for(pid)
        )

    async def _run_reconcile_loop(
        self,
        pid: int,
        interval: float,
        external_stop_requested: threading.Event | None,
    ) -> None:
        while not self._stop.is_set():
            if self._stop_was_requested(pid, external_stop_requested):
                logger.info(
                    "stop request detected at %s; shutting down",
                    stop_request_path(),
                )
                self._stop.set()
                break
            try:
                await self._reconcile_once()
            except Exception as exc:
                logger.error("reconcile tick crashed: %s", exc, exc_info=True)
            if refresh_token_request_path().exists():
                logger.info("refresh-token sentinel detected; notifying refreshers")
                self.refresher.notify_refresh_needed()
                self.codex_refresher.notify_refresh_needed()
                clear_refresh_token_request()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _shutdown_runtime(self, runtime: _DaemonRuntime, pid: int) -> None:
        self._stop.set()
        try:
            await self._stop_all_workers()
        except Exception:
            logger.exception("shutdown: failed to stop all workers")
        if runtime.control_manager is not None:
            try:
                runtime.control_manager.stop()
            except Exception:
                logger.exception("shutdown: failed to stop control manager")
        owned_tasks = runtime.startup_tasks + runtime.runtime_tasks
        for task in owned_tasks:
            task.cancel()
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)

        for clear_resolver in (
            lambda: set_profile_setter(None),
            lambda: set_client_resolver(None),
            lambda: set_rpc_resolver(None),
        ):
            try:
                clear_resolver()
            except Exception:
                logger.exception("shutdown: failed to clear service resolver")
        for name, stop_service, runner in (
            ("ws-local", stop_ws_local_server, runtime.ws_local_runner),
            ("data", stop_data_service, runtime.data_runner),
            ("rpc", stop_rpc_service, runtime.rpc_runner),
        ):
            if runner is None:
                continue
            try:
                await stop_service(runner)
            except Exception:
                logger.exception("shutdown: failed to stop %s service", name)
        clear_daemon_ready(expected_pid=pid)
        logger.info("puffo-agent portal stopped")

    def request_stop(self) -> None:
        self._stop.set()

    def _load_agent_cfg_cached(self, agent_id: str) -> AgentConfig:
        """Reuses a cached parse when (mtime_ns, size) is unchanged.
        Same exceptions as ``AgentConfig.load`` on parse failure."""
        path = agent_yml_path(agent_id)
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        cached = self._agent_cfg_cache.get(agent_id)
        if cached is not None and (cached[0], cached[1]) == key:
            return cached[2]
        cfg = AgentConfig.load(agent_id)
        shared_status = prepare_workspace_shared_access(
            cfg.resolve_workspace_dir(),
            shared_fs_dir(),
            mounted=(cfg.runtime.kind or RUNTIME_CLI_LOCAL) == RUNTIME_CLI_DOCKER,
        )
        if shared_status not in AVAILABLE_SHARED_WORKSPACE_STATES:
            logger.error(
                "agent %s: shared workspace is %s; cross-Agent file handoffs "
                "are unavailable",
                agent_id,
                shared_status,
            )
        self._agent_cfg_cache[agent_id] = (st.st_mtime_ns, st.st_size, cfg)
        return cfg

    async def _reconcile_once(self) -> None:
        on_disk = set(discover_agents())
        await self._stop_removed_agents(on_disk)
        on_disk = await self._consume_agent_lifecycle_flags(on_disk)

        # refresh_model / refresh_runtime mutate agent.yml; the
        # config-changed check below picks up the respawn.
        for agent_id in sorted(on_disk):
            _process_daemon_refresh_flags(agent_id)

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
                        ensure_fresh_token=self._ensure_fresh_for(agent_cfg),
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
                elif (
                    worker.restart_required
                    or _worker_needs_restart(worker.agent_cfg, agent_cfg)
                ):
                    reason = (
                        "fatal runtime exit"
                        if worker.restart_required
                        else "config changed"
                    )
                    logger.info(
                        "agent %s: %s, restarting worker", agent_id, reason
                    )
                    await self._stop_worker(agent_id)
                    worker = Worker(
                        self.daemon_cfg,
                        agent_cfg,
                        notify_refresh_needed=self._notify_refresh_for(agent_cfg),
                        ensure_fresh_token=self._ensure_fresh_for(agent_cfg),
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

    async def _stop_removed_agents(self, on_disk: set[str]) -> None:
        for stale_id in list(self._agent_cfg_cache.keys() - on_disk):
            self._agent_cfg_cache.pop(stale_id, None)
        for agent_id in set(self.workers) - on_disk:
            logger.info("agent %s: directory removed, stopping worker", agent_id)
            await self._stop_worker(agent_id)

    async def _consume_agent_lifecycle_flags(self, on_disk: set[str]) -> set[str]:
        archived: set[str] = set()
        for agent_id in sorted(on_disk):
            if archive_flag_path(agent_id).exists():
                await self._archive_on_flag(agent_id)
                archived.add(agent_id)
        on_disk -= archived
        deleted: set[str] = set()
        for agent_id in sorted(on_disk):
            if delete_flag_path(agent_id).exists():
                await self._delete_on_flag(agent_id)
                deleted.add(agent_id)
        on_disk -= deleted
        for agent_id in sorted(on_disk):
            if restart_flag_path(agent_id).exists():
                await self._consume_restart_flag(agent_id)
        return on_disk

    async def _consume_restart_flag(self, agent_id: str) -> None:
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
            logger.warning("agent %s: couldn't remove restart.flag: %s", agent_id, exc)

    def _refresher_for(self, agent_cfg: AgentConfig) -> CredentialRefresher:
        """Pick the right refresher for an agent's harness. Codex
        agents only need their own auth.json refresh; claude-code +
        every other harness routes through the Claude refresher."""
        if (agent_cfg.runtime.harness or "claude-code") == "codex":
            return self.codex_refresher
        return self.refresher

    def _register_with_refresher(
        self,
        agent_cfg: AgentConfig,
        worker: Worker,
    ) -> None:
        # Gateway/VK mode: nothing to refresh (static VK via LiteLLM). Skip
        # registration so the periodic refresh loop never probes the provider
        # (api.openai.com for codex) and flips refresh_broken.
        # getattr: a runtime without the field is simply not in gateway mode —
        # never let a missing optional field crash worker startup.
        if (
            (getattr(agent_cfg.runtime, "llm_base_url", "") or "").strip()
            or Daemon._uses_claude_api_key(self, agent_cfg)
        ):
            return
        refresher = self._refresher_for(agent_cfg)
        refresher.register_agent(agent_home_dir(agent_cfg.id))
        agent_id = agent_cfg.id

        def on_refresh_success() -> None:
            Worker._clear_auth_failed_if_recoverable(
                worker.runtime,
                agent_id,
                logger,
            )
            # Re-arm the auth_failed DM dedup so a re-expiry this
            # session re-notifies the operator.
            worker._auth_failed_notification_sent = False
            # Long-lived Claude/Codex subprocesses may retain the credential
            # they opened with. Reload only the provider runtime at the
            # worker's next idle boundary; keep the bridge connection and
            # Puffo logical session intact.
            try:
                flag = refresh_provider_auth_flag_path(
                    agent_cfg.resolve_workspace_dir()
                )
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text(
                    '{"source":"credential_replaced"}', encoding="utf-8"
                )
                worker.notify_refresh()
                logger.info(
                    "agent %s: credential replaced — provider reload requested",
                    agent_id,
                )
            except OSError as exc:
                logger.warning(
                    "agent %s: could not request provider credential reload: %s",
                    agent_id,
                    exc,
                )
                raise

        refresher.register_on_refresh_success(on_refresh_success)
        # Stash callback identity for _stop_worker's unregister.
        worker._refresh_success_callback = on_refresh_success

    def _notify_refresh_for(self, agent_cfg: AgentConfig):
        if Daemon._uses_claude_api_key(self, agent_cfg):
            return None
        return self._refresher_for(agent_cfg).notify_refresh_needed

    def _ensure_fresh_for(self, agent_cfg: AgentConfig):
        # Gateway/VK mode (runtime.llm_base_url set): the LLM key is a static
        # per-agent virtual key routed through LiteLLM — there is NO OAuth token
        # to refresh. Returning None makes the worker skip the pre-delivery
        # refresh gate, so a turn isn't blocked (and deferred ~40s) by a spurious
        # api.openai.com probe that 401s. Native-auth harnesses keep the refresh.
        # getattr: see _register_with_refresher — a missing field means OAuth mode.
        if (
            (getattr(agent_cfg.runtime, "llm_base_url", "") or "").strip()
            or Daemon._uses_claude_api_key(self, agent_cfg)
        ):
            return None
        return self._refresher_for(agent_cfg).ensure_fresh

    def _uses_claude_api_key(self, agent_cfg: AgentConfig) -> bool:
        runtime = agent_cfg.runtime
        return (
            getattr(runtime, "kind", "cli-local") in {"cli-local", "cli-docker"}
            and (getattr(runtime, "harness", "") or "claude-code")
            == "claude-code"
            and bool(claude_cli_api_key(getattr(self, "daemon_cfg", None)))
        )

    def _set_worker_profile_cache(
        self,
        agent_id: str,
        slug: str,
        display_name: str,
        avatar_url: str,
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

    def _resolve_host_mcp_context(self, agent_id: str) -> HostMcpContext | None:
        """Rpc-service shim. Returns None when the worker isn't warm yet."""
        worker = self.workers.get(agent_id)
        if worker is None:
            return None
        return worker.host_mcp_context()

    def _resolve_message_client(self, agent_id: str):
        """Data-service shim for on-miss channel-cache recovery."""
        context = self._resolve_host_mcp_context(agent_id)
        return context.message_client if context is not None else None

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
        await asyncio.gather(
            *(self._stop_worker(i) for i in ids), return_exceptions=True
        )

    async def _archive_on_flag(self, agent_id: str) -> None:
        logger.warning(
            "agent %s: archive.flag detected, stopping worker + archiving",
            agent_id,
        )
        await self._stop_worker(agent_id)
        from .import_agents import (
            revoke_archived_device,
            write_archived_pending_revoke,
        )

        cfg_for_revoke = None
        try:
            cfg_for_revoke = AgentConfig.load(agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent %s: cfg load for revoke failed: %s", agent_id, exc)
        # Heartbeat must precede revoke — afterwards the device is 401'd.
        if cfg_for_revoke is not None:
            try:
                await _report_lifecycle(cfg_for_revoke, "archived")
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent %s: archived report failed: %s", agent_id, exc)
        src = agent_dir(agent_id)
        if not src.exists():
            return
        await _drain_codex_tmp(src)
        archived_dir().mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = archived_dir() / f"{agent_id}-ws-{stamp}"
        move_err = await _retry_move(src, dest)
        if move_err is not None:
            logger.error(
                "agent %s: archive move failed after retries: %s "
                "(flag still present — will retry next tick)",
                agent_id,
                move_err,
            )
            return
        logger.info("agent %s: archived to %s", agent_id, dest)
        if cfg_for_revoke is None or not cfg_for_revoke.puffo_core.is_configured():
            return
        pc = cfg_for_revoke.puffo_core
        try:
            await revoke_archived_device(dest, slug=pc.slug)
            logger.info("agent %s: device revoked server-side", agent_id)
            return
        except Exception as exc:  # noqa: BLE001
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "agent %s: revoke failed (%s); pending marker will be "
                "left in %s for the next startup sweep",
                agent_id,
                reason,
                dest,
            )
            try:
                from ..crypto.keystore import KeyStore

                identity = KeyStore(dest / "keys").load_identity(pc.slug)
                write_archived_pending_revoke(
                    dest,
                    server_url=identity.server_url,
                    slug=identity.slug,
                    device_id=identity.device_id,
                    last_error=reason,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent %s: failed to write pending_revoke marker: %s",
                    agent_id,
                    exc,
                )

    async def _delete_on_flag(self, agent_id: str) -> None:
        logger.warning(
            "agent %s: delete.flag detected, stopping worker + removing dir",
            agent_id,
        )
        await self._stop_worker(agent_id)
        from .import_agents import (
            revoke_archived_device,
            write_archived_pending_revoke,
        )

        cfg_for_revoke = None
        try:
            cfg_for_revoke = AgentConfig.load(agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent %s: cfg load for revoke failed: %s", agent_id, exc)
        src = agent_dir(agent_id)
        if not src.exists():
            return
        await _drain_codex_tmp(src)
        archived_dir().mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = archived_dir() / f"{agent_id}-del-{stamp}"
        move_err = await _retry_move(src, dest)
        if move_err is not None:
            logger.error(
                "agent %s: delete move failed after retries: %s "
                "(flag still present — will retry next tick)",
                agent_id,
                move_err,
            )
            return
        if cfg_for_revoke is None or not cfg_for_revoke.puffo_core.is_configured():
            try:
                shutil.rmtree(dest)
                logger.info("agent %s: deleted", agent_id)
            except OSError as exc:
                logger.warning(
                    "agent %s: delete rmtree failed: %s (leftover at %s)",
                    agent_id,
                    exc,
                    dest,
                )
            return
        pc = cfg_for_revoke.puffo_core
        try:
            await revoke_archived_device(dest, slug=pc.slug)
            logger.info("agent %s: device revoked server-side", agent_id)
        except Exception as exc:  # noqa: BLE001
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "agent %s: revoke failed (%s); keeping as archive + "
                "pending marker for the next sweep",
                agent_id,
                reason,
            )
            try:
                from ..crypto.keystore import KeyStore

                identity = KeyStore(dest / "keys").load_identity(pc.slug)
                write_archived_pending_revoke(
                    dest,
                    server_url=identity.server_url,
                    slug=identity.slug,
                    device_id=identity.device_id,
                    last_error=reason,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent %s: failed to write pending_revoke marker: %s",
                    agent_id,
                    exc,
                )
            return
        try:
            shutil.rmtree(dest)
            logger.info("agent %s: deleted", agent_id)
        except OSError as exc:
            logger.warning(
                "agent %s: delete rmtree failed after revoke: %s (leftover at %s)",
                agent_id,
                exc,
                dest,
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
                agent_cfg.id,
                status,
                exc.status,
                exc.body,
            )
            return True
        logger.warning(
            "agent %s: lifecycle report %r failed (HTTP %s); will retry",
            agent_cfg.id,
            status,
            exc.status,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — transient (network); retry next tick
        logger.warning(
            "agent %s: lifecycle report %r failed; will retry: %s",
            agent_cfg.id,
            status,
            exc,
        )
        return False
    finally:
        await http.close()


_ARCHIVE_RETRY_BACKOFF_SECONDS = (3.0, 6.0, 12.0, 12.0)


async def _retry_move(src: Path, dest: Path) -> OSError | None:
    # shutil.move's copytree+rmtree fallback hollows out src when a
    # child file is locked (Windows aiosqlite WAL/SHM after stop_worker).
    # Split: copy (read-only on src) then best-effort rmtree.
    copy_err: OSError | None = None
    for delay in _ARCHIVE_RETRY_BACKOFF_SECONDS:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        try:
            shutil.copytree(str(src), str(dest))
            copy_err = None
            break
        except (OSError, PermissionError) as exc:
            copy_err = exc
            await asyncio.sleep(delay)
    if copy_err is not None:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        return copy_err
    for delay in _ARCHIVE_RETRY_BACKOFF_SECONDS:
        try:
            shutil.rmtree(str(src))
            return None
        except (OSError, PermissionError):
            await asyncio.sleep(delay)
    shutil.rmtree(str(src), ignore_errors=True)
    return None


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


async def _sweep_archived_pending_revokes_at_startup() -> None:
    from .import_agents import sweep_archived_pending_revokes

    try:
        n = await sweep_archived_pending_revokes()
        if n:
            logger.info(
                "archived pending revokes: retried %d marker(s)",
                n,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("archived pending revoke sweep errored: %s", exc)


def _mcp_fingerprint_path() -> Path:
    return home_dir() / "mcp_tool_fingerprint"


def _respawn_codex_on_mcp_change_at_startup() -> None:
    """Rotate Codex sessions when their cached MCP surface changed."""
    import json

    from ..mcp.puffo_core_server import mcp_tool_fingerprint

    try:
        current = mcp_tool_fingerprint()
    except Exception as exc:  # noqa: BLE001 - startup remains best-effort
        logger.warning("startup: mcp fingerprint failed: %s", exc)
        return
    path = _mcp_fingerprint_path()
    try:
        previous = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        previous = ""
    except OSError as exc:
        logger.warning("startup: couldn't read mcp fingerprint: %s", exc)
        previous = ""
    if previous and previous != current:
        for agent_id in discover_agents():
            try:
                cfg = AgentConfig.load(agent_id)
            except Exception:  # noqa: BLE001 - one broken config is isolated
                continue
            if cfg.runtime.kind != "cli-local" or cfg.runtime.harness != "codex":
                continue
            try:
                flag = refresh_session_flag_path(cfg.resolve_workspace_dir())
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text(
                    json.dumps({"requested_at": int(time.time())}) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning(
                    "startup: couldn't rotate codex session for %s: %s",
                    agent_id,
                    exc,
                )
    try:
        path.write_text(current + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("startup: couldn't persist mcp fingerprint: %s", exc)


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
                    n,
                    pairing.operator_slug,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort, per-operator
            logger.warning(
                "startup machine_id migration failed for %s: %s",
                pairing.operator_slug,
                exc,
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
        *(_sync_one(aid) for aid in ids),
        return_exceptions=False,
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
            local,
            remote,
            upgrade_command_for_install_mode(),
        )
    else:
        logger.info(
            "puffo-agent %s (latest release: %s)",
            local,
            remote,
        )


def _worker_needs_restart(old, new) -> bool:
    """True when identity/profile/runtime changed and the worker's
    WS session, keystore, or subprocess is now stale. Trigger-rule
    edits don't count — those re-read on every turn."""
    return (
        old.puffo_core != new.puffo_core
        or old.profile != new.profile
        or old.runtime != new.runtime
        or old.env_overrides != new.env_overrides
    )


_DAEMON_REFRESH_HARNESSES: tuple[str, ...] = ("claude-code", "codex")


def _validate_daemon_refresh_model(harness: str, model: str) -> None:
    if harness not in _DAEMON_REFRESH_HARNESSES:
        raise ValueError(
            f"harness={harness!r} not supported (choose one of "
            f"{list(_DAEMON_REFRESH_HARNESSES)})"
        )
    from ..agent.cli_bin import resolve_claude_bin, resolve_codex_bin

    resolver = {
        "claude-code": resolve_claude_bin,
        "codex": resolve_codex_bin,
    }[harness]
    if resolver() is None:
        raise ValueError(f"harness={harness!r} CLI not installed on host")
    from ..agent.model_catalog import provider_models

    supported = [m.id for m in provider_models(harness) if m.id]
    if model not in supported:
        raise ValueError(
            f"model={model!r} not supported by harness={harness!r}; "
            f"supported: {supported}"
        )


def _validate_daemon_inference_level(harness: str, level: str) -> None:
    """Validate the public daemon refresh contract."""
    _validate_refresh_inference_level(level, harness)


def _mark_flag_broken(flag_path: Path, reason: str) -> None:
    """Rename a refresh_*.flag to ``<name>.broken`` for operator
    inspection; agent.yml is untouched."""
    import json

    broken = flag_path.with_suffix(flag_path.suffix + ".broken")
    try:
        original = flag_path.read_text(encoding="utf-8")
    except OSError:
        original = ""
    body = json.dumps({"error": reason, "original": original}, indent=2)
    try:
        broken.write_text(body, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "couldn't write %s: %s",
            broken,
            exc,
        )
    try:
        flag_path.unlink()
    except OSError:
        pass


def _process_daemon_refresh_flags(agent_id: str) -> None:
    """Consume ``refresh_model.flag`` + ``refresh_runtime.flag``:
    validate model, harness, provider, and inference-level agreement;
    mutate agent.yml; ``.broken``-rename invalid requests. Respawn is
    left to the config-changed check."""
    try:
        agent_cfg = AgentConfig.load(agent_id)
    except Exception as exc:
        logger.warning(
            "agent %s: couldn't load agent.yml for refresh flags: %s",
            agent_id,
            exc,
        )
        return
    workspace = agent_cfg.resolve_workspace_dir()
    _process_model_refresh_flag(agent_id, agent_cfg, refresh_model_flag_path(workspace))
    _process_runtime_refresh_flag(
        agent_id, agent_cfg, refresh_runtime_flag_path(workspace)
    )


def _process_model_refresh_flag(
    agent_id: str, agent_cfg: AgentConfig, model_flag: Path
) -> None:
    import json

    if not model_flag.exists():
        return
    try:
        payload = json.loads(model_flag.read_text(encoding="utf-8") or "{}")
        harness = str(payload.get("harness") or "")
        model = str(payload.get("model") or "")
        has_model_swap = bool(harness or model)
        if bool(harness) != bool(model):
            raise ValueError("harness and model must be provided together")
        has_inference_level = "inference_level" in payload
        inference_level = payload.get("inference_level")
        if not has_model_swap and not has_inference_level:
            raise ValueError("refresh_model.flag contains no requested change")
        if has_model_swap:
            _validate_daemon_refresh_model(harness, model)
        effective_harness = harness if has_model_swap else agent_cfg.runtime.harness
        if has_inference_level:
            _validate_refresh_inference_level(inference_level, effective_harness)
    except Exception as exc:
        logger.warning(
            "agent %s: refresh_model.flag invalid (%s); marking broken",
            agent_id,
            exc,
        )
        _mark_flag_broken(model_flag, str(exc))
        return
    _apply_model_refresh(
        agent_id=agent_id,
        agent_cfg=agent_cfg,
        model_flag=model_flag,
        harness=harness,
        model=model,
        inference_level=inference_level,
        has_model_swap=has_model_swap,
        has_inference_level=has_inference_level,
    )


def _validate_refresh_inference_level(value: object, harness: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("inference_level must be a non-empty string")
    from ..mcp.config import supported_inference_levels

    levels = supported_inference_levels(harness)
    if value not in levels:
        raise ValueError(
            f"inference_level={value!r} not supported by harness={harness!r}; "
            f"expected one of {list(levels)}"
        )


def _apply_model_refresh(
    *,
    agent_id: str,
    agent_cfg: AgentConfig,
    model_flag: Path,
    harness: str,
    model: str,
    inference_level: object,
    has_model_swap: bool,
    has_inference_level: bool,
) -> None:
    if has_model_swap:
        agent_cfg.runtime.harness = harness
        agent_cfg.runtime.model = model
        from .runtime_matrix import HARNESS_PROVIDERS

        providers = HARNESS_PROVIDERS.get(harness, frozenset())
        if len(providers) != 1:
            raise RuntimeError(
                f"refresh cannot infer a unique provider for harness={harness!r}"
            )
        agent_cfg.runtime.provider = next(iter(providers))
    if has_inference_level:
        # Explicit input; already validated against the effective harness.
        agent_cfg.runtime.inference_level = inference_level
    elif has_model_swap:
        from .runtime_matrix import normalize_inference_level

        agent_cfg.runtime.inference_level = normalize_inference_level(
            agent_cfg.runtime.kind,
            agent_cfg.runtime.provider,
            agent_cfg.runtime.harness,
            agent_cfg.runtime.inference_level,
        )
    try:
        agent_cfg.save()
        logger.info(
            "agent %s: refresh_model applied (provider=%r harness=%r "
            "model=%r inference_level=%r)",
            agent_id,
            agent_cfg.runtime.provider,
            agent_cfg.runtime.harness,
            agent_cfg.runtime.model,
            agent_cfg.runtime.inference_level,
        )
    except Exception as exc:
        logger.warning(
            "agent %s: couldn't save agent.yml on refresh_model: %s", agent_id, exc
        )
    try:
        model_flag.unlink()
    except OSError:
        pass


def _process_runtime_refresh_flag(
    agent_id: str, agent_cfg: AgentConfig, runtime_flag: Path
) -> None:
    import json

    if not runtime_flag.exists():
        return
    try:
        payload = json.loads(runtime_flag.read_text(encoding="utf-8") or "{}")
        kind = str(payload.get("kind") or "")
        new_harness = payload.get("harness")
        new_model = payload.get("model")
        new_provider = payload.get("provider")
        from .runtime_matrix import validate_triple

        candidate_harness = (
            str(new_harness) if new_harness is not None else agent_cfg.runtime.harness
        )
        candidate_provider = (
            str(new_provider)
            if new_provider is not None
            else agent_cfg.runtime.provider
        )
        result = validate_triple(kind, candidate_provider, candidate_harness)
        if not result.ok:
            raise ValueError(result.error)
        if new_model is not None and candidate_harness in _DAEMON_REFRESH_HARNESSES:
            _validate_daemon_refresh_model(candidate_harness, str(new_model))
    except Exception as exc:
        logger.warning(
            "agent %s: refresh_runtime.flag invalid (%s); marking broken",
            agent_id,
            exc,
        )
        _mark_flag_broken(runtime_flag, str(exc))
        return
    _apply_runtime_refresh(
        agent_id,
        agent_cfg,
        runtime_flag,
        kind,
        new_harness,
        new_provider,
        new_model,
    )


def _apply_runtime_refresh(
    agent_id: str,
    agent_cfg: AgentConfig,
    runtime_flag: Path,
    kind: str,
    new_harness: object,
    new_provider: object,
    new_model: object,
) -> None:
    agent_cfg.runtime.kind = kind
    if new_harness is not None:
        agent_cfg.runtime.harness = str(new_harness)
    if new_provider is not None:
        agent_cfg.runtime.provider = str(new_provider)
    if new_model is not None:
        agent_cfg.runtime.model = str(new_model)
    from .runtime_matrix import normalize_inference_level

    agent_cfg.runtime.inference_level = normalize_inference_level(
        agent_cfg.runtime.kind,
        agent_cfg.runtime.provider,
        agent_cfg.runtime.harness,
        agent_cfg.runtime.inference_level,
    )
    try:
        agent_cfg.save()
        logger.info("agent %s: refresh_runtime applied (kind=%r)", agent_id, kind)
    except Exception as exc:
        logger.warning(
            "agent %s: couldn't save agent.yml on refresh_runtime: %s", agent_id, exc
        )
    try:
        runtime_flag.unlink()
    except OSError:
        pass


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


async def _wait_for_existing_daemon_ready(pid: int, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if is_daemon_ready(pid):
            return True
        if not is_pid_alive(pid):
            return False
        await asyncio.sleep(0.1)
    return is_daemon_ready(pid)


async def run_daemon(
    external_stop_requested: threading.Event | None = None,
) -> int:
    # Single-daemon enforcement. ``start`` against an already-running
    # daemon exits 0 (the user wanted a running daemon; one exists) —
    # exit 1 read as an error in upgrade flows. Enforcement is unchanged:
    # we never spawn a second daemon. A different running version isn't
    # discriminated here; ``stop && start`` is the version-swap path.
    if is_daemon_alive():
        pid = read_daemon_pid()
        if pid is not None and await _wait_for_existing_daemon_ready(pid):
            # print + log: background / tray runners may not surface INFO.
            msg = f"puffo-agent daemon already running (pid={pid})"
            logger.info(msg)
            print(msg)
            return 0
        msg = f"puffo-agent daemon failed to become ready (pid={pid})"
        logger.error(msg)
        print(msg)
        return 1

    home_dir().mkdir(parents=True, exist_ok=True)
    agents_dir().mkdir(parents=True, exist_ok=True)
    clear_daemon_ready()

    from .import_agents import cleanup_staging_dir

    cleanup_staging_dir()

    daemon_cfg = DaemonConfig.load()
    pid = os.getpid()
    try:
        # With no live daemon proven above, a leftover stop sentinel
        # (old-CLI timestamp-only or stale JSON) must not kill the new
        # process — clear it before publishing our own pid.
        clear_stop_request()
        write_daemon_pid(pid)
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

        await daemon.run(external_stop_requested)

        # Cancel surviving tasks; otherwise asyncio.run can hang on
        # Windows when a subprocess transport is still alive.
        survivors = [
            task
            for task in asyncio.all_tasks(loop)
            if task is not asyncio.current_task()
        ]
        for task in survivors:
            task.cancel()
        if survivors:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*survivors, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "shutdown: %d tasks still running after 5s; exiting anyway",
                    sum(1 for task in survivors if not task.done()),
                )
    finally:
        clear_daemon_ready(expected_pid=pid)
        clear_stop_request(expected_pid=pid)
        clear_daemon_pid(expected_pid=pid)

    # Hard exit avoids loop.close() hangs on leftover subprocess
    # transports; workers + adapters are already torn down.
    logger.info("shutdown complete; exiting")
    os._exit(0)
