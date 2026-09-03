"""Standard runtime lifecycle orchestration for :mod:`portal.worker`."""

from __future__ import annotations

import asyncio
import functools
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from . import worker as worker_module
from .runtime_matrix import (
    RUNTIME_CLI_DOCKER,
    RUNTIME_CLI_LOCAL,
)
from .state import agent_dir, shared_fs_dir
from .workspace_layout import (
    AVAILABLE_SHARED_WORKSPACE_STATES,
    prepare_workspace_shared_access,
)
from ..agent.errors import ProviderFailureError
from ..agent.processing_receipts import processing_run_id
from ..agent._usage_markers import parse_reset_epoch
from ..tasks import spawn

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
    workspace_shared_status: str
    system_prompt: str

    @property
    def refresh_flags(self) -> tuple[Path, Path, Path, Path]:
        root = Path(self.workspace_path) / ".puffo-agent"
        return (
            root / "refresh_agent.flag",
            root / "refresh_host_sync.flag",
            root / "refresh_session.flag",
            root / "refresh_provider_auth.flag",
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
    async def begin_notice_turn(self, _mid):
        return None

    async def end_notice_turn(self, **_kwargs):
        return None

    async def begin_turn(self, _mid, *, run_id=None):
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


class GlobalInboxStatusLifecycle:
    """Mirror one durable Global Inbox turn into Server processing rows.

    Provider admission first emits a provisional busy status anchored to the
    notice's first message without creating a processing row. ``begin_turn``
    fires only when ``read_inbox`` admits the first real message. Run ids are
    derived from the durable turn and message identities, so crash recovery
    reopens and settles the same Server row. Terminal cleanup settles either
    the exact active union or the provisional status. State always resets so
    the next turn cannot inherit runs.
    """

    def __init__(self, reporter) -> None:
        self._reporter = reporter
        self._notice_began = False
        self._began = False
        self._turn_id: str | None = None

    async def on_notice_admitted(
        self, *, turn_id: str, message_ids: tuple[str, ...]
    ) -> None:
        if not message_ids:
            return
        self._claim_turn(turn_id)
        if self._notice_began or self._began:
            return
        self._notice_began = True
        await self._reporter.begin_notice_turn(message_ids[0])

    async def on_turn_active(
        self, *, turn_id: str, message_ids: tuple[str, ...]
    ) -> None:
        if not message_ids:
            return
        self._claim_turn(turn_id)
        if self._began:
            return
        self._began = True
        await self._reporter.begin_turn(
            message_ids[0],
            run_id=self._run_id(turn_id, message_ids[0]),
        )

    async def on_turn_terminal(
        self,
        *,
        turn_id: str,
        message_ids: tuple[str, ...],
        succeeded: bool,
        error_text: str | None,
    ) -> None:
        if (self._notice_began or self._began) and self._turn_id != turn_id:
            raise RuntimeError("status lifecycle terminal turn does not match")
        runs = self._build_runs(turn_id, message_ids, succeeded, error_text)
        try:
            if runs:
                await self._reporter.end_turn_batch(runs)
            elif self._notice_began:
                await self._reporter.end_notice_turn(
                    succeeded=succeeded,
                    error_text=error_text,
                )
        finally:
            self.reset()

    def _claim_turn(self, turn_id: str) -> None:
        if self._turn_id is not None and self._turn_id != turn_id:
            raise RuntimeError("status lifecycle already owns another turn")
        self._turn_id = turn_id

    def _build_runs(
        self,
        turn_id: str,
        message_ids: tuple[str, ...],
        succeeded: bool,
        error_text: str | None,
    ) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for index, message_id in enumerate(message_ids):
            entry: dict[str, Any] = {
                "run_id": self._run_id(turn_id, message_id),
                "message_id": message_id,
                "succeeded": succeeded,
            }
            if error_text is not None:
                entry["error_text"] = error_text
            runs.append(entry)
        return runs

    def reset(self) -> None:
        self._notice_began = False
        self._began = False
        self._turn_id = None

    @staticmethod
    def _run_id(turn_id: str, message_id: str) -> str:
        return processing_run_id(turn_id, message_id)


class StandardWorkerRun:
    """Own the non-ws-local phases of one ``Worker._run`` invocation."""

    def __init__(self, worker: Worker):
        self.worker = worker

    async def run(self) -> None:
        context = await self._initialize()
        if context is None or not await self._warm(context):
            return
        spawn(
            self._sync_profile_after_warm(context.paths.agent_id),
            name="sync_profile_after_warm",
        )
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
        workspace_shared_status = prepare_workspace_shared_access(
            Path(workspace_path),
            shared_fs_dir(),
            mounted=(agent_cfg.runtime.kind or RUNTIME_CLI_LOCAL)
            == RUNTIME_CLI_DOCKER,
        )
        if workspace_shared_status not in AVAILABLE_SHARED_WORKSPACE_STATES:
            logger.error(
                "agent %s: shared workspace is %s; cross-Agent file handoffs "
                "through workspace/shared are unavailable",
                agent_id,
                workspace_shared_status,
            )
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
            workspace_shared_status=workspace_shared_status,
        )
        paths = WorkerRunPaths(
            agent_id=agent_id,
            effective_harness=effective_harness,
            profile_path=profile_path,
            memory_path=memory_path,
            workspace_path=workspace_path,
            claude_path=claude_path,
            shared_path=shared_path,
            workspace_shared_status=workspace_shared_status,
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
        kind = worker.agent_cfg.runtime.kind or RUNTIME_CLI_LOCAL
        if kind == RUNTIME_CLI_DOCKER:
            return await self._prepare_driver_runtime(
                paths,
                outbox_ref,
                worker_module.build_docker_runtime(
                    worker.daemon_cfg, worker.agent_cfg
                ),
            )
        if kind != RUNTIME_CLI_LOCAL:
            raise RuntimeError(
                f"agent {worker.agent_cfg.id!r}: runtime kind {kind!r} "
                "does not use the built-in Driver runtime"
            )
        from ..agent.harness.runtime.local_runtime import LocalRuntimePreparer

        return await self._prepare_driver_runtime(
            paths,
            outbox_ref,
            LocalRuntimePreparer(worker.daemon_cfg, worker.agent_cfg),
        )

    async def _prepare_driver_runtime(
        self,
        paths: WorkerRunPaths,
        outbox_ref: list[Any],
        preparer: Any,
    ) -> tuple[Any, str, Any]:
        """Prepare a Driver-backed runtime and bind it to the durable outbox."""
        from ..agent.runtime_event_outbox import (
            RuntimeEventOutbox,
            runtime_event_outbox_path,
        )

        outbox = RuntimeEventOutbox(
            runtime_event_outbox_path(agent_dir(paths.agent_id))
        )
        outbox_ref[0] = outbox
        persisted = outbox.state()
        prepared = await preparer.prepare(
            system_prompt=paths.system_prompt,
            persisted_native_session_id=persisted.get("native_session_id", ""),
            persisted_native_session_harness=persisted.get(
                "native_session_harness", ""
            ),
        )
        try:
            return await self._bind_driver_runtime(
                outbox, prepared, persisted
            )
        except Exception:
            # The Docker owner starts the per-agent container inside
            # ``prepare``; if anything between that and adapter
            # construction fails, the not-yet-wired preparer must stop it
            # so partial initialization never orphans a running container.
            await self._abort_docker_preparation(preparer)
            raise

    async def _bind_driver_runtime(
        self,
        outbox: Any,
        prepared: Any,
        persisted: dict[str, Any],
    ) -> tuple[Any, str, Any]:
        """Bind a prepared runtime to the durable outbox and Runtime Manager."""
        worker = self.worker
        from ..agent.harness import build_driver
        from ..agent.harness.runtime.docker_runtime import DockerRuntimePreparer
        from ..agent.harness.runtime.local_runtime import build_local_runtime_adapter

        session_ref = (
            persisted.get("session_ref", "")
            or f"session_{uuid.uuid4().hex}"
        )
        active_turn_ref = persisted.get("active_turn_ref") or None
        outbox.set_active_turn(
            active_turn_ref,
            session_ref=session_ref,
            native_session_id=prepared.native_session_id,
            native_session_harness=prepared.harness_name,
        )
        driver = None
        cleanup = None
        if isinstance(preparer := prepared.preparer, DockerRuntimePreparer):
            driver = build_driver(
                prepared.harness_name,
                process_factory=preparer.process_factory,
            )
            cleanup = preparer.aclose
        worker._adapter = build_local_runtime_adapter(
            prepared,
            outbox=outbox,
            logical_session_ref=session_ref,
            driver=driver,
            cleanup=cleanup,
        )
        return outbox, session_ref, prepared

    async def _abort_docker_preparation(self, preparer: Any) -> None:
        """Stop a Docker container after a partial Driver assembly.

        Only the Docker owner starts a container inside ``prepare``; the
        host-local preparer has nothing to tear down.
        """
        from ..agent.harness.runtime.docker_runtime import DockerRuntimePreparer

        if not isinstance(preparer, DockerRuntimePreparer):
            return
        try:
            await preparer.aclose()
        except Exception:
            logger.exception(
                "agent %s: failed to stop Docker container after "
                "preparation failure",
                self.worker.agent_cfg.id,
            )

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
                native_session_harness=(
                    context.prepared_local_runtime.harness_name
                ),
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
            # drained: hold-no-retry, same as auth
            return not exc.is_auth and not exc.is_drained
        if isinstance(exc, ProviderFailureError):
            # plan quota arrives here, not as AgentAPIError
            return exc.error_code != "plan_drained"
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
        (
            refresh_agent,
            refresh_host,
            refresh_session,
            refresh_provider_auth,
        ) = context.paths.refresh_flags
        paths = context.paths
        worker = self.worker
        ok = await worker_module._process_refresh_flags(
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
            workspace_shared_status=paths.workspace_shared_status,
            puffo=context.puffo,
            adapter=worker._adapter,
            refresh_agent_flag=refresh_agent,
            refresh_host_sync_flag=refresh_host,
            refresh_session_flag=refresh_session,
            refresh_provider_auth_flag=refresh_provider_auth,
        )
        worker._note_refresh_reload(
            ok,
            (refresh_agent, refresh_host, refresh_session, refresh_provider_auth),
            paths.agent_id,
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
            worker_module.Worker._flip_health_in_progress(
                worker.runtime, context.paths.agent_id, logger
            )
            reply = await context.puffo.handle_global_inbox_retry(planned)
        finally:
            worker._turn_active = False
        if reply:
            logger.warning(
                "agent %s: suppressed global retry plain output; outbound "
                "chat requires send_message",
                context.paths.agent_id,
            )

    def _build_global_turn_handler(self, context: WorkerRunContext):
        worker = self.worker

        async def run_global_turn(planned):
            reply = await self._execute_global_turn(context, planned)
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

    def _build_global_runtime(
        self,
        context: WorkerRunContext,
        *,
        status_lifecycle=None,
        process_outcome=None,
    ):
        from ..agent.channel_audience import read_channel_audience
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
            identity_aliases=(paths.agent_id, client.slug),
            agent_id=paths.agent_id,
            runtime_event_outbox=context.runtime_event_outbox,
            status_lifecycle=status_lifecycle,
            process_outcome=process_outcome,
            channel_audience_loader=lambda space_id, channel_id: read_channel_audience(
                space_id,
                channel_id,
                http=client.http,
                log=logger,
            ),
            covers_renotice_enabled=(
                True if worker.daemon_cfg.covers_renotice else None
            ),
            # unpark = snapshot-cleared health + a wake
            drained_check=lambda: worker.runtime.health == "drained",
        )
        coordinator = SendCoordinator(
            slug=client.slug,
            keystore=client.keystore,
            http_client=client.http,
            data_client=InProcessDataClient(client.store, client),
            workspace=paths.workspace_path,
            shared_workspace=str(shared_fs_dir()),
            baseline_source=BaselineAdapter(client.store),
            active_turn_source=ActiveBoundaryAdapter(
                client.store, global_runtime.active
            ),
            held_recovery_source=global_runtime.held_recovery_source,
            channel_policy_source=client,
        )
        global_runtime.coordinator = coordinator
        global_runtime.send_delegate = TrackingSendDelegate(
            coordinator, global_runtime.attempts, global_runtime
        )
        client.global_runtime = global_runtime
        client.send_coordinator = coordinator
        client.send_delegate = global_runtime.send_delegate
        # Harness background tasks wake the model after the daemon turn is
        # finalized; adopting those runs keeps sends and Inbox reads on the
        # normal lifecycle instead of leaving them unbound.
        global_runtime.register_autonomous_adoption()
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
                await worker.probe_mcp_transport(agent_id)
            except Exception:  # noqa: BLE001
                # The probe must never take the heartbeat down with it.
                logger.warning(
                    "agent %s: MCP transport probe raised", agent_id,
                    exc_info=True,
                )
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

    @staticmethod
    def _settle_process_health(
        worker: Worker, agent_id: str, outcome: str, error_text: str | None
    ) -> None:
        try:
            if outcome == "succeeded":
                worker._resolve_health_after_success(agent_id)
            elif outcome == "cancelled":
                worker_module.Worker._resolve_health_on_success(
                    worker.runtime, agent_id, logger
                )
            elif outcome == "auth_failed":
                worker._enter_auth_failed(agent_id)
            elif outcome == "drained":
                worker._enter_drained(
                    agent_id, parse_reset_epoch(error_text or "")
                )
            elif outcome == "api_error_abandoned":
                worker_module.Worker._mark_api_error_abandoned_if_in_progress(
                    worker.runtime, agent_id, error_text, logger
                )
            elif outcome == "provider_failed":
                worker_module.Worker._mark_provider_failure_if_in_progress(
                    worker.runtime, agent_id, error_text, logger
                )
            else:
                worker_module.Worker._fallback_unhandled_error_if_stuck_in_progress(
                    worker.runtime, agent_id, error_text, logger
                )
        except Exception:
            logger.warning("process health settlement failed", exc_info=True)

    async def _start_services(self, context: WorkerRunContext) -> WorkerRunServices:
        worker = self.worker
        uploader = self._build_runtime_event_uploader(context)
        reporter = self._build_reporter(context.client)

        global_runtime = self._build_global_runtime(
            context,
            status_lifecycle=GlobalInboxStatusLifecycle(reporter),
            process_outcome=functools.partial(
                self._settle_process_health, worker, context.paths.agent_id
            ),
        )
        reminder_sync = await self._prepare_reminder_sync(context, global_runtime)
        global_task = spawn(
            global_runtime.run(),
            name="global_runtime.run",
        )
        reminder_task = None
        if reminder_sync is not None:
            reminder_task = spawn(
                reminder_sync.run(request_snapshot_on_start=False),
                name="reminder_sync.run",
            )
        heartbeat_task = spawn(self._heartbeat(context.paths.agent_id), name="heartbeat")
        status_task = spawn(reporter.run_heartbeat_loop(), name="reporter.run_heartbeat_loop")
        watch_task = spawn(
            worker._refresh_watcher_loop(
                context.paths.refresh_flags,
                lambda: self._apply_refresh(context),
            ),
            name="worker.refresh_watcher_loop",
        )
        upload_task = spawn(self._upload_runtime_events(uploader), name="upload_runtime_events")
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
