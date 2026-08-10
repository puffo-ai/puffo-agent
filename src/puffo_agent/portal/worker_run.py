"""Standard runtime lifecycle orchestration for :mod:`portal.worker`."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from . import worker as worker_module
from .runtime_matrix import RUNTIME_CLI_DOCKER, RUNTIME_CLI_LOCAL
from .state import agent_dir

if TYPE_CHECKING:
    from .worker import Worker


logger = worker_module.logger
RUNTIME_EVENT_DEGRADED_RETRY_SECONDS = 30.0
LOCAL_WARM_RETRY_DELAYS_SECONDS = (1.0, 2.0)


@dataclass(frozen=True)
class WorkerRunPaths:
    agent_id: str
    effective_harness: str
    profile_path: str
    memory_path: str
    workspace_path: str
    claude_path: str
    shared_path: Path
    system_prompt: str

    @property
    def refresh_flags(self) -> tuple[Path, Path, Path]:
        root = Path(self.workspace_path) / ".puffo-agent"
        return (
            root / "refresh_agent.flag",
            root / "refresh_host_sync.flag",
            root / "refresh_session.flag",
        )


@dataclass(frozen=True)
class WorkerRunContext:
    paths: WorkerRunPaths
    puffo: Any
    client: Any
    runtime_event_outbox: Any = None
    runtime_session_ref: str = ""
    prepared_local_runtime: Any = None


@dataclass(frozen=True)
class WorkerRunServices:
    global_runtime: Any
    global_runtime_task: asyncio.Task
    reminder_sync: Any
    reminder_sync_task: asyncio.Task | None
    reporter: Any
    heartbeat_task: asyncio.Task
    status_task: asyncio.Task
    watch_task: asyncio.Task
    runtime_upload_task: asyncio.Task


class _NoopStatusReporter:
    async def begin_turn(self, _mid):
        return None

    async def end_turn(self, *_args, **_kwargs):
        return None

    async def end_turn_batch(self, *_args, **_kwargs):
        return None

    async def report_error(self, _text):
        return None

    async def run_heartbeat_loop(self):
        return None

    def stop(self):
        return None


class StandardWorkerRun:
    """Own the non-ws-local phases of one ``Worker._run`` invocation."""

    def __init__(self, worker: Worker):
        self.worker = worker

    async def run(self) -> None:
        context = await self._initialize()
        if context is None or not await self._warm(context):
            return
        asyncio.ensure_future(self._sync_profile_after_warm(context.paths.agent_id))
        services = await self._start_services(context)
        try:
            await self._listen(context, services)
        finally:
            await self._cleanup(context, services)

    def _prepare_paths(self) -> WorkerRunPaths:
        worker = self.worker
        agent_cfg = worker.agent_cfg
        agent_id = agent_cfg.id
        effective_harness = worker._runtime_info()["harness"]
        profile_path = str(agent_cfg.resolve_profile_path())
        memory_path = str(agent_cfg.resolve_memory_dir())
        workspace_path = str(agent_cfg.resolve_workspace_dir())
        claude_path = str(agent_cfg.resolve_claude_dir())
        shared_path = worker_module.docker_shared_dir()
        Path(workspace_path).mkdir(parents=True, exist_ok=True)
        worker_module._seed_claude_dir(Path(claude_path))
        system_prompt = worker_module._rebuild_managed_system_prompt(
            harness_name=effective_harness,
            agent_id=agent_id,
            shared_path=shared_path,
            profile_path=profile_path,
            memory_path=memory_path,
            workspace_path=workspace_path,
            display_name=agent_cfg.display_name,
            role=agent_cfg.role,
            role_short=agent_cfg.role_short,
            puffo_handle=agent_cfg.puffo_core.slug,
        )
        paths = WorkerRunPaths(
            agent_id=agent_id,
            effective_harness=effective_harness,
            profile_path=profile_path,
            memory_path=memory_path,
            workspace_path=workspace_path,
            claude_path=claude_path,
            shared_path=shared_path,
            system_prompt=system_prompt,
        )
        self._remove_stale_managed_prompt(paths)
        self._validate_core_config(paths.agent_id)
        return paths

    def _remove_stale_managed_prompt(self, paths: WorkerRunPaths) -> None:
        old_managed = Path(paths.claude_path) / "CLAUDE.md"
        if not worker_module.looks_like_managed_claude_md(old_managed):
            return
        try:
            old_managed.unlink()
            logger.info(
                "agent %s: migrated stale managed CLAUDE.md out of %s",
                paths.agent_id,
                old_managed,
            )
        except OSError as exc:
            logger.warning(
                "agent %s: could not remove stale %s: %s",
                paths.agent_id,
                old_managed,
                exc,
            )

    def _validate_core_config(self, agent_id: str) -> None:
        if self.worker.agent_cfg.puffo_core.is_configured():
            return
        raise RuntimeError(
            f"agent {agent_id!r}: puffo_core block in agent.yml "
            "is incomplete. Required fields: server_url, slug, "
            "device_id, space_id."
        )

    async def _prepare_adapter(
        self, paths: WorkerRunPaths, outbox_ref: list[Any]
    ) -> tuple[Any, str, Any]:
        worker = self.worker
        if worker.agent_cfg.runtime.kind != RUNTIME_CLI_LOCAL:
            worker._adapter = worker_module.build_docker_adapter(
                worker.daemon_cfg, worker.agent_cfg
            )
            return None, "", None
        from ..agent.harness.local_runtime import (
            LocalRuntimePreparer,
            build_local_runtime_adapter,
        )
        from ..agent.runtime_event_outbox import (
            RuntimeEventOutbox,
            runtime_event_outbox_path,
        )

        outbox = RuntimeEventOutbox(
            runtime_event_outbox_path(agent_dir(paths.agent_id))
        )
        outbox_ref[0] = outbox
        persisted = outbox.state()
        session_ref = persisted.get("session_ref", "") or f"session_{uuid.uuid4().hex}"
        preparer = LocalRuntimePreparer(worker.daemon_cfg, worker.agent_cfg)
        prepared = await preparer.prepare(
            system_prompt=paths.system_prompt,
            persisted_native_session_id=persisted.get("native_session_id", ""),
        )
        outbox.set_active_turn(
            persisted.get("active_turn_ref") or None,
            session_ref=session_ref,
            native_session_id=prepared.native_session_id,
        )
        worker._adapter = build_local_runtime_adapter(
            prepared, outbox=outbox, logical_session_ref=session_ref
        )
        return outbox, session_ref, prepared

    async def _initialize(self) -> WorkerRunContext | None:
        worker = self.worker
        agent_id = worker.agent_cfg.id
        outbox_ref: list[Any] = [None]
        try:
            paths = self._prepare_paths()
            outbox, session_ref, prepared = await self._prepare_adapter(
                paths, outbox_ref
            )
            puffo = worker_module.PuffoAgent(
                adapter=worker._adapter,
                system_prompt=paths.system_prompt,
                memory_dir=paths.memory_path,
                workspace_dir=paths.workspace_path,
                claude_dir=paths.claude_path,
                agent_id=agent_id,
            )
            client = worker_module._build_puffo_core_client(
                worker.agent_cfg, agent_id, daemon_cfg=worker.daemon_cfg
            )
            worker._client = client
            return WorkerRunContext(
                paths=paths,
                puffo=puffo,
                client=client,
                runtime_event_outbox=outbox,
                runtime_session_ref=session_ref,
                prepared_local_runtime=prepared,
            )
        except Exception as exc:
            await self._fail_initialization(agent_id, outbox_ref[0], exc)
            return None

    async def _fail_initialization(
        self, agent_id: str, outbox: Any, exc: Exception
    ) -> None:
        worker = self.worker
        logger.error("agent %s: failed to initialise: %s", agent_id, exc, exc_info=True)
        worker.runtime.status = "error"
        worker.runtime.error = str(exc)
        worker.runtime.save(agent_id)
        worker._warm_done.set()
        if worker._adapter is not None:
            try:
                await worker._adapter.aclose()
            except Exception:
                logger.exception(
                    "agent %s: failed to close adapter after init error", agent_id
                )
            worker._adapter = None
        await worker._close_client()
        if outbox is not None:
            outbox.close()

    async def _warm(self, context: WorkerRunContext) -> bool:
        worker = self.worker
        if context.prepared_local_runtime is not None:
            warm_ok = await self._warm_prepared_local_runtime(context)
            if not warm_ok:
                return False
        else:
            warm_ok = False
            try:
                await worker._adapter.warm(context.paths.system_prompt)
                warm_ok = True
            except Exception as exc:
                logger.warning(
                    "agent %s: warm() failed (will retry on first turn): %s",
                    context.paths.agent_id,
                    exc,
                )
        if context.prepared_local_runtime is not None:
            assert context.runtime_event_outbox is not None
            persisted = context.runtime_event_outbox.state()
            context.runtime_event_outbox.set_active_turn(
                persisted.get("active_turn_ref") or None,
                session_ref=context.runtime_session_ref,
                native_session_id=worker._adapter.get_provider_session_id() or "",
            )
            context.prepared_local_runtime.finalize_legacy_session_migration()
        if warm_ok:
            await worker._run_post_warm_gate(context.paths.agent_id)
        else:
            worker._warm_done.set()
        return True

    @staticmethod
    def _retryable_local_warm_error(exc: Exception) -> bool:
        if isinstance(exc, worker_module.AgentAPIError):
            return not exc.is_auth
        return not isinstance(
            exc,
            (
                AssertionError,
                FileNotFoundError,
                ImportError,
                NotADirectoryError,
                PermissionError,
                TypeError,
                ValueError,
            ),
        )

    async def _warm_prepared_local_runtime(
        self,
        context: WorkerRunContext,
    ) -> bool:
        attempts = len(LOCAL_WARM_RETRY_DELAYS_SECONDS) + 1
        for index in range(attempts):
            try:
                await self.worker._adapter.warm(context.paths.system_prompt)
                return True
            except Exception as exc:
                exhausted = index >= attempts - 1
                if exhausted or not self._retryable_local_warm_error(exc):
                    await self._fail_local_warm(context, exc)
                    return False
                delay = LOCAL_WARM_RETRY_DELAYS_SECONDS[index]
                logger.warning(
                    "agent %s: local Driver warm attempt %d/%d failed: %s; "
                    "retrying in %.1fs",
                    context.paths.agent_id,
                    index + 1,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        return False

    async def _fail_local_warm(self, context: WorkerRunContext, exc: Exception) -> None:
        worker = self.worker
        agent_id = context.paths.agent_id
        logger.error(
            "agent %s: local Driver failed to open: %s",
            agent_id,
            exc,
            exc_info=True,
        )
        worker.runtime.status = "error"
        worker.runtime.error = str(exc)
        worker.runtime.save(agent_id)
        worker._warm_done.set()
        try:
            await worker._adapter.aclose()
        except Exception:
            logger.exception(
                "agent %s: failed to close Driver after warm error", agent_id
            )
        worker._adapter = None
        await worker._close_client()
        context.runtime_event_outbox.close()

    async def _sync_profile_after_warm(self, agent_id: str) -> None:
        from .profile_sync import sync_full_profile

        try:
            await sync_full_profile(self.worker.agent_cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent %s: post-warm profile sync failed: %s", agent_id, exc)

    def _build_runtime_event_uploader(self, context: WorkerRunContext):
        if context.runtime_event_outbox is None or not hasattr(context.client, "http"):
            return None
        from ..agent.runtime_event_outbox import RuntimeEventUploader

        async def append_transport(path: str, body: bytes):
            from ..crypto.http_client import HttpError

            try:
                decoded = json.loads(body)
                if getattr(context.client.http, "keyless", False):
                    response = await context.client.http.post_unsigned(path, decoded)
                else:
                    response = await context.client.http.post(path, decoded)
                return 200, response
            except HttpError as exc:
                return exc.status, {}

        return RuntimeEventUploader(context.runtime_event_outbox, append_transport)

    async def _apply_refresh(self, context: WorkerRunContext) -> None:
        refresh_agent, refresh_host, refresh_session = context.paths.refresh_flags
        paths = context.paths
        worker = self.worker
        await worker_module._process_refresh_flags(
            agent_id=paths.agent_id,
            harness_name=paths.effective_harness,
            shared_path=paths.shared_path,
            profile_path=paths.profile_path,
            memory_path=paths.memory_path,
            workspace_path=paths.workspace_path,
            display_name=worker.agent_cfg.display_name,
            role=worker.agent_cfg.role,
            role_short=worker.agent_cfg.role_short,
            puffo_handle=worker.agent_cfg.puffo_core.slug,
            puffo=context.puffo,
            adapter=worker._adapter,
            refresh_agent_flag=refresh_agent,
            refresh_host_sync_flag=refresh_host,
            refresh_session_flag=refresh_session,
        )

    async def _execute_global_turn(self, context: WorkerRunContext, planned):
        worker = self.worker
        worker._turn_active = True
        try:
            async with worker._reload_lock:
                await self._apply_refresh(context)
            worker._maybe_wake_refresher_if_auth_failed(context.paths.agent_id)
            if (
                worker._ensure_fresh_token is not None
                and worker.agent_cfg.runtime.kind
                in (RUNTIME_CLI_LOCAL, RUNTIME_CLI_DOCKER)
                and not await worker._ensure_fresh_token()
            ):
                worker._enter_auth_failed(context.paths.agent_id)
                raise worker_module.AgentAPIError(
                    "credential refresh failed before delivery", is_auth=True
                )
            worker_module.Worker._flip_health_in_progress(
                worker.runtime, context.paths.agent_id, logger
            )
            return await context.puffo.handle_global_inbox_turn(planned)
        finally:
            worker._turn_active = False

    async def _execute_global_retry(self, context: WorkerRunContext, planned) -> None:
        worker = self.worker
        worker._turn_active = True
        try:
            try:
                reply = await context.puffo.handle_global_inbox_retry(planned)
            finally:
                worker._turn_active = False
        except worker_module.AgentAPIError as exc:
            if exc.is_auth:
                worker._enter_auth_failed(context.paths.agent_id)
            raise
        else:
            worker_module.Worker._resolve_health_on_success(
                worker.runtime, context.paths.agent_id, logger
            )
        if reply:
            logger.warning(
                "agent %s: suppressed global retry plain output; outbound "
                "chat requires send_message",
                context.paths.agent_id,
            )

    def _build_global_turn_handler(self, context: WorkerRunContext):
        worker = self.worker

        async def run_global_turn(planned):
            try:
                reply = await self._execute_global_turn(context, planned)
            except worker_module.AgentAPIError as exc:
                if exc.is_auth:
                    worker._enter_auth_failed(context.paths.agent_id)
                raise
            else:
                worker_module.Worker._resolve_health_on_success(
                    worker.runtime, context.paths.agent_id, logger
                )
            worker.runtime.msg_count += 1
            worker.runtime.last_event_at = int(time.time())
            if reply:
                logger.warning(
                    "agent %s: suppressed global plain output; outbound chat "
                    "requires send_message",
                    context.paths.agent_id,
                )

        async def retry_global_turn(planned):
            await self._execute_global_retry(context, planned)

        run_global_turn.handle_global_inbox_retry = retry_global_turn
        return run_global_turn

    def _build_global_runtime(self, context: WorkerRunContext):
        from ..agent.global_inbox_runtime import (
            ActiveBoundaryAdapter,
            BaselineAdapter,
            GlobalInboxRuntime,
            TrackingSendDelegate,
        )
        from ..agent.send_coordinator import SendCoordinator
        from .ws_local.in_process_data_client import InProcessDataClient

        worker = self.worker
        client = context.client
        paths = context.paths
        global_runtime = GlobalInboxRuntime(
            store=client.store,
            adapter=worker._adapter,
            run_turn=self._build_global_turn_handler(context),
            workspace=paths.workspace_path,
            held_catchup=client.recover_pending_delivery,
            send_mode_keys=(paths.agent_id, client.slug),
            agent_id=paths.agent_id,
            runtime_event_outbox=context.runtime_event_outbox,
        )
        coordinator = SendCoordinator(
            slug=client.slug,
            keystore=client.keystore,
            http_client=client.http,
            data_client=InProcessDataClient(client.store, client),
            workspace=paths.workspace_path,
            baseline_source=BaselineAdapter(client.store),
            active_turn_source=ActiveBoundaryAdapter(
                client.store, global_runtime.active
            ),
            held_recovery_source=global_runtime.held_recovery_source,
        )
        global_runtime.coordinator = coordinator
        global_runtime.send_delegate = TrackingSendDelegate(
            coordinator, global_runtime.attempts, global_runtime
        )
        client.global_runtime = global_runtime
        client.send_coordinator = coordinator
        client.send_delegate = global_runtime.send_delegate
        return global_runtime

    async def _prepare_reminder_sync(self, context: WorkerRunContext, runtime):
        from ..agent.reminder_sync import prepare_reminder_sync

        return await prepare_reminder_sync(
            context.client, runtime, agent_id=context.paths.agent_id,
        )

    async def _heartbeat(self, agent_id: str) -> None:
        worker = self.worker
        interval = max(1.0, worker.daemon_cfg.runtime_heartbeat_seconds)
        while not worker._stop.is_set():
            worker.runtime.save(agent_id)
            try:
                await asyncio.wait_for(worker._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _upload_runtime_events(self, uploader) -> None:
        if uploader is None:
            return
        while not self.worker._stop.is_set():
            try:
                result = await uploader.upload_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A local store failure must not silently retire the loop and
                # strand every later event; surface it and keep draining.
                logger.warning(
                    "runtime event upload failed; retrying", exc_info=True
                )
                delay = RUNTIME_EVENT_DEGRADED_RETRY_SECONDS
            else:
                if result.state == "degraded":
                    delay = RUNTIME_EVENT_DEGRADED_RETRY_SECONDS
                else:
                    delay = 0.1 if result.state == "uploaded" else 1.0
            try:
                await asyncio.wait_for(self.worker._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _build_reporter(self, client):
        if not hasattr(client, "http"):
            return _NoopStatusReporter()
        reporter = self.worker._build_status_reporter(client)
        return reporter if reporter is not None else _NoopStatusReporter()

    async def _start_services(self, context: WorkerRunContext) -> WorkerRunServices:
        worker = self.worker
        uploader = self._build_runtime_event_uploader(context)
        global_runtime = self._build_global_runtime(context)
        reminder_sync = await self._prepare_reminder_sync(context, global_runtime)
        global_task = asyncio.ensure_future(global_runtime.run())
        reminder_task = None
        if reminder_sync is not None:
            reminder_task = asyncio.ensure_future(
                reminder_sync.run(request_snapshot_on_start=False)
            )
        reporter = self._build_reporter(context.client)
        heartbeat_task = asyncio.ensure_future(self._heartbeat(context.paths.agent_id))
        status_task = asyncio.ensure_future(reporter.run_heartbeat_loop())
        watch_task = asyncio.ensure_future(
            worker._refresh_watcher_loop(
                context.paths.refresh_flags,
                lambda: self._apply_refresh(context),
            )
        )
        upload_task = asyncio.ensure_future(self._upload_runtime_events(uploader))
        return WorkerRunServices(
            global_runtime=global_runtime,
            global_runtime_task=global_task,
            reminder_sync=reminder_sync,
            reminder_sync_task=reminder_task,
            reporter=reporter,
            heartbeat_task=heartbeat_task,
            status_task=status_task,
            watch_task=watch_task,
            runtime_upload_task=upload_task,
        )

    async def _report_listener_error(
        self, context: WorkerRunContext, services: WorkerRunServices, exc: Exception
    ) -> bool:
        worker = self.worker
        agent_id = context.paths.agent_id
        fatal = services.global_runtime_task.done()
        if fatal:
            worker._restart_required = True
            logger.error(
                "agent %s: stopping after global inbox runtime failure: %s",
                agent_id,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            logger.warning(
                "agent %s: listen() crashed: %s: %s — reconnecting in %.1fs",
                agent_id,
                type(exc).__name__,
                exc,
                worker_module.RECONNECT_BACKOFF_SECONDS,
            )
        worker.runtime.error = f"{type(exc).__name__}: {exc}"
        worker.runtime.save(agent_id)
        try:
            await services.reporter.report_error(
                worker.runtime.error or "listen crashed"
            )
        except Exception:
            pass
        return fatal

    async def _listen(
        self, context: WorkerRunContext, services: WorkerRunServices
    ) -> None:
        from ..agent.global_inbox_runtime import await_listener_with_runtime

        worker = self.worker
        while not worker._stop.is_set():
            try:
                await await_listener_with_runtime(
                    context.client.listen(),
                    services.global_runtime_task,
                    label=(f"agent {context.paths.agent_id} global inbox runtime"),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if await self._report_listener_error(context, services, exc):
                    raise
            if worker._stop.is_set():
                break
            try:
                await asyncio.wait_for(
                    worker._stop.wait(),
                    timeout=worker_module.RECONNECT_BACKOFF_SECONDS,
                )
            except asyncio.TimeoutError:
                pass

    async def _cleanup(
        self, context: WorkerRunContext, services: WorkerRunServices
    ) -> None:
        worker = self.worker
        if (
            services.reminder_sync is not None
            and services.reminder_sync_task is not None
        ):
            services.reminder_sync.stop()
            services.reminder_sync_task.cancel()
            try:
                await services.reminder_sync_task
            except (asyncio.CancelledError, Exception):
                pass
        services.global_runtime.stop()
        services.global_runtime_task.cancel()
        try:
            await services.global_runtime_task
        except (asyncio.CancelledError, Exception):
            pass
        services.reporter.stop()
        background_tasks = (
            services.heartbeat_task,
            services.status_task,
            services.watch_task,
            services.runtime_upload_task,
        )
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        worker.runtime.status = "stopped"
        worker.runtime.save(context.paths.agent_id)
        if context.runtime_event_outbox is not None:
            # The Runtime Manager's reader is what feeds this outbox, so it has
            # to stop first. ``Worker.stop`` closes the adapter only after
            # awaiting this task, which would leave a window where an in-flight
            # driver event is persisted onto an already shut-down executor.
            await self._close_local_adapter(context.paths.agent_id)
            context.runtime_event_outbox.close()

    async def _close_local_adapter(self, agent_id: str) -> None:
        """Stop the local Driver's event reader before its durable sink goes."""
        worker = self.worker
        if worker._adapter is None:
            return
        try:
            await asyncio.wait_for(worker._adapter.aclose(), timeout=5.0)
        except asyncio.TimeoutError:
            # Bounded so a wedged Driver cannot outlive the cancel budget
            # ``Worker.stop`` allows; the adapter is left for stop() to retry.
            logger.warning(
                "agent %s: local Driver close timed out; closing the runtime "
                "event outbox anyway", agent_id,
            )
            return
        except Exception:
            logger.exception(
                "agent %s: failed to close local Driver during cleanup", agent_id
            )
        worker._adapter = None
