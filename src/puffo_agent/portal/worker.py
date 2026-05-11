"""Per-agent worker running one PuffoCoreMessageClient loop.

Owns the agent's adapter + WS listen loop + heartbeat task; written
into runtime.json so the CLI can read live stats without IPC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from ..agent.adapters import Adapter
from ..agent.core import AgentAPIError, PuffoAgent
from ..agent.status_reporter import StatusReporter
from ..agent.shared_content import (

    assemble_claude_md,
    ensure_shared_primer,
    looks_like_managed_claude_md,
    read_memory_snapshot,
    read_shared_primer,
    sync_shared_skills,
    write_claude_md,
    write_gemini_md,
)
from .state import (
    AgentConfig,
    DaemonConfig,
    PuffoCoreConfig,
    RuntimeConfig,
    RuntimeState,
    agent_claude_user_dir,
    agent_dir,
    agent_home_dir,
    cli_session_json_path,
    docker_shared_dir,
    shared_fs_dir,
)

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF_SECONDS = 5.0

# Poll rate for the adapter's OAuth refresh check. The adapter
# decides per-tick whether to actually probe based on credentials
# mtime; this is just the upper bound on staleness.
CREDENTIAL_REFRESH_TICK_SECONDS = 10 * 60


def build_adapter(daemon_cfg: DaemonConfig, agent_cfg: AgentConfig) -> Adapter:
    """Construct the adapter for ``runtime.kind``. Raises on unknown
    or misconfigured kinds."""
    kind = agent_cfg.runtime.kind or "chat-local"

    if kind == "chat-local":
        from ..agent.adapters.chat_only import ChatOnlyAdapter
        provider = _build_legacy_provider(daemon_cfg, agent_cfg.runtime)
        return ChatOnlyAdapter(provider)

    if kind == "sdk-local":
        from ..agent.adapters.sdk import SDKAdapter
        api_key = agent_cfg.runtime.api_key or daemon_cfg.anthropic.api_key
        model = agent_cfg.runtime.model or daemon_cfg.anthropic.model or "claude-sonnet-4-6"
        if not api_key:
            raise RuntimeError(
                f"agent {agent_cfg.id!r}: runtime kind 'sdk-local' requires an anthropic "
                "api_key in daemon.yml or agent.yml"
            )
        adapter = SDKAdapter(
            api_key=api_key,
            model=model,
            allowed_tools=agent_cfg.runtime.allowed_tools,
            agent_id=agent_cfg.id,
            workspace_dir=str(agent_cfg.resolve_workspace_dir()),
            max_turns=agent_cfg.runtime.max_turns,
        )
        if agent_cfg.puffo_core.is_configured():
            from ..mcp.config import puffo_core_stdio_sdk_config, default_python_executable
            pc = agent_cfg.puffo_core
            adapter.mcp_servers_override = puffo_core_stdio_sdk_config(
                python=default_python_executable(),
                slug=pc.slug,
                device_id=pc.device_id,
                server_url=pc.server_url,
                space_id=pc.space_id,
                keystore_dir=str(agent_dir(agent_cfg.id) / "keys"),
                workspace=str(agent_cfg.resolve_workspace_dir()),
                agent_id=agent_cfg.id,
            )
        return adapter

    # CLI adapters authenticate via the host's
    # ~/.claude/.credentials.json (set up by `claude login`); no
    # api_key is threaded through. Model overrides still flow.
    if kind == "cli-docker":
        from ..agent.adapters.docker_cli import DockerCLIAdapter
        from ..agent.harness import build_harness
        harness = build_harness(agent_cfg.runtime.harness)
        # gemini-cli needs GEMINI_API_KEY per docker exec; claude-code
        # and hermes use the bind-mounted credentials file.
        google_key = ""
        if harness.name() == "gemini-cli":
            google_key = daemon_cfg.google.api_key
            if not google_key:
                raise RuntimeError(
                    f"agent {agent_cfg.id!r}: harness=gemini-cli requires a "
                    "google.api_key — pass --api-key on `agent create`, set "
                    "GEMINI_API_KEY in the environment, or run "
                    "`puffo-agent config` to save a daemon-wide default."
                )
        # Per-agent overrides win; empty falls through to daemon
        # defaults, then to "no cap".
        memory_limit = (
            agent_cfg.runtime.docker_memory_limit
            or daemon_cfg.docker_memory_limit
        )
        memory_reservation = (
            agent_cfg.runtime.docker_memory_reservation
            or daemon_cfg.docker_memory_reservation
        )
        adapter = DockerCLIAdapter(
            agent_id=agent_cfg.id,
            model=agent_cfg.runtime.model or daemon_cfg.anthropic.model or "",
            image=agent_cfg.runtime.docker_image,
            workspace_dir=str(agent_cfg.resolve_workspace_dir()),
            claude_dir=str(agent_cfg.resolve_claude_dir()),
            session_file=str(cli_session_json_path(agent_cfg.id)),
            agent_home_dir=str(agent_home_dir(agent_cfg.id)),
            shared_fs_dir=str(shared_fs_dir()),
            harness=harness,
            google_api_key=google_key,
            memory_limit=memory_limit,
            memory_reservation=memory_reservation,
        )
        # When puffo_core is configured, give the adapter env to spawn
        # ``python -m puffo_agent.mcp.puffo_core_server``. The adapter
        # rewrites path-typed env values to container bind-mount paths
        # at config-write time.
        if agent_cfg.puffo_core.is_configured():
            from ..mcp.config import puffo_core_mcp_env
            pc = agent_cfg.puffo_core
            adapter.puffo_core_mcp_env = puffo_core_mcp_env(
                slug=pc.slug,
                device_id=pc.device_id,
                server_url=pc.server_url,
                space_id=pc.space_id,
                # Host paths; rewritten to container paths by
                # docker_cli's _write_cli_mcp_config.
                keystore_dir=str(agent_dir(agent_cfg.id) / "keys"),
                workspace=str(agent_cfg.resolve_workspace_dir()),
                agent_id=agent_cfg.id,
                # MCP runs inside the container; reach the host's
                # 127.0.0.1 data service via Docker's host alias.
                data_service_url="http://host.docker.internal:63386",
                runtime_kind="cli-docker",
                harness=agent_cfg.runtime.harness,
            )
        return adapter

    if kind == "cli-local":
        from ..agent.adapters.local_cli import LocalCLIAdapter
        # The legacy permission-proxy DM flow has not been ported to
        # puffo-core; the hook fail-opens when PUFFO_OPERATOR_USERNAME
        # is unset, so cli-local works without supervised approvals.
        operator = ""
        from ..agent.harness import build_harness
        adapter = LocalCLIAdapter(
            agent_id=agent_cfg.id,
            model=agent_cfg.runtime.model or daemon_cfg.anthropic.model or "",
            workspace_dir=str(agent_cfg.resolve_workspace_dir()),
            claude_dir=str(agent_cfg.resolve_claude_dir()),
            session_file=str(cli_session_json_path(agent_cfg.id)),
            mcp_config_file=str(agent_dir(agent_cfg.id) / "mcp-config.json"),
            agent_home_dir=str(agent_home_dir(agent_cfg.id)),
            owner_username=operator,
            permission_mode=agent_cfg.runtime.permission_mode,
            harness=build_harness(agent_cfg.runtime.harness),
        )
        if agent_cfg.puffo_core.is_configured():
            from ..mcp.config import puffo_core_mcp_env
            pc = agent_cfg.puffo_core
            adapter.puffo_core_mcp_env = puffo_core_mcp_env(
                slug=pc.slug,
                device_id=pc.device_id,
                server_url=pc.server_url,
                space_id=pc.space_id,
                keystore_dir=str(agent_dir(agent_cfg.id) / "keys"),
                workspace=str(agent_cfg.resolve_workspace_dir()),
                agent_id=agent_cfg.id,
                runtime_kind="cli-local",
                harness=agent_cfg.runtime.harness,
            )
        return adapter

    raise RuntimeError(
        f"agent {agent_cfg.id!r}: unknown runtime kind {kind!r} "
        "(valid: chat-local, sdk-local, cli-docker, cli-local)"
    )


def _build_legacy_provider(daemon_cfg: DaemonConfig, runtime: RuntimeConfig):
    """Anthropic/OpenAI message-completion provider for the
    chat-local adapter. Per-agent fields override daemon defaults."""
    provider_name = runtime.provider or daemon_cfg.default_provider

    if provider_name == "anthropic":
        from ..agent.providers.anthropic_provider import AnthropicProvider
        api_key = runtime.api_key or daemon_cfg.anthropic.api_key
        model = runtime.model or daemon_cfg.anthropic.model or "claude-sonnet-4-6"
        if not api_key:
            raise RuntimeError(
                "anthropic api_key is not set in daemon.yml or agent.yml"
            )
        return AnthropicProvider(api_key=api_key, model=model)

    if provider_name == "openai":
        from ..agent.providers.openai_provider import OpenAIProvider
        api_key = runtime.api_key or daemon_cfg.openai.api_key
        model = runtime.model or daemon_cfg.openai.model or "gpt-4o"
        if not api_key:
            raise RuntimeError(
                "openai api_key is not set in daemon.yml or agent.yml"
            )
        return OpenAIProvider(api_key=api_key, model=model)

    raise RuntimeError(f"unknown provider {provider_name!r}")


def _puffo_cli_keystore_dir() -> Path:
    """Where ``puffo-cli`` saves identities. Mirrors the Rust
    ``directories`` crate (``ProjectDirs::from("ai", "puffo",
    "puffo-cli").data_dir().join("keys")``).

    Windows: ``%APPDATA%\\puffo\\puffo-cli\\data\\keys``
    macOS:   ``~/Library/Application Support/ai.puffo.puffo-cli/keys``
    Linux:   ``$XDG_DATA_HOME/puffo/puffo-cli/keys``
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "puffo" / "puffo-cli" / "data" / "keys"
        return Path.home() / "AppData" / "Roaming" / "puffo" / "puffo-cli" / "data" / "keys"
    if sys.platform == "darwin":
        return (
            Path.home() / "Library" / "Application Support"
            / "ai.puffo.puffo-cli" / "keys"
        )
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "puffo" / "puffo-cli" / "keys"


def _ensure_agent_identity_imported(agent_id: str, slug: str) -> None:
    """Copy ``<slug>.json`` from puffo-cli's keystore into the
    per-agent puffo-agent keystore if missing. Without this the
    worker crash-loops on ``identity not found``. No-op when the
    destination exists or the source is missing."""
    if not slug:
        return
    dest = agent_dir(agent_id) / "keys" / f"{slug}.json"
    if dest.exists():
        return
    src = _puffo_cli_keystore_dir() / f"{slug}.json"
    if not src.exists():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        logger.info(
            "agent %s: imported identity from puffo-cli keystore (%s → %s)",
            agent_id, src, dest,
        )
    except OSError as exc:
        logger.warning(
            "agent %s: failed to import identity from %s: %s",
            agent_id, src, exc,
        )


def _build_puffo_core_client(agent_cfg: AgentConfig, agent_id: str):
    """Construct a PuffoCoreMessageClient from the agent's config."""
    from ..agent.message_store import MessageStore
    from ..agent.puffo_core_client import PuffoCoreMessageClient
    from ..crypto.http_client import PuffoCoreHttpClient
    from ..crypto.keystore import KeyStore

    pc = agent_cfg.puffo_core
    _ensure_agent_identity_imported(agent_id, pc.slug)
    ks_dir = str(agent_dir(agent_id) / "keys")
    ks = KeyStore(ks_dir)
    http = PuffoCoreHttpClient(pc.server_url, ks, pc.slug)
    ms = MessageStore(str(agent_dir(agent_id) / "messages.db"))

    return PuffoCoreMessageClient(
        slug=pc.slug,
        device_id=pc.device_id,
        space_id=pc.space_id,
        operator_slug=pc.operator_slug,
        keystore=ks,
        http_client=http,
        message_store=ms,
        workspace=str(agent_cfg.resolve_workspace_dir()),
    )


class Worker:
    """Runs a single AI agent inside the daemon event loop."""

    def __init__(self, daemon_cfg: DaemonConfig, agent_cfg: AgentConfig):
        self.daemon_cfg = daemon_cfg
        self.agent_cfg = agent_cfg
        self.runtime = RuntimeState(
            status="running",
            started_at=int(time.time()),
            msg_count=0,
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._adapter: Adapter | None = None
        # Held here so ``stop()`` can close the SQLite + WS handles
        # the client owns. Required on Windows so ``messages.db*``
        # files release before ``puffo-agent agent archive`` renames.
        self._client = None
        # Signalled when warm() finishes (success, failure, or skipped).
        # Daemon awaits this to serialise heavy startup across workers.
        self._warm_done = asyncio.Event()

    def start(self) -> asyncio.Task:
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.ensure_future(self._run())
        return self._task

    async def wait_warm(self, timeout: float | None = None) -> bool:
        """Block until warm() finishes or the worker exits early.
        Returns True on completion, False on timeout."""
        try:
            await asyncio.wait_for(self._warm_done.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                # Bounded wait so a wedged WS recv / subprocess pipe
                # can't hang shutdown. Cancellation usually completes
                # in milliseconds.
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "agent %s: worker task didn't exit within 10s of cancel — "
                    "moving on to adapter cleanup anyway",
                    self.agent_cfg.id,
                )
            except (asyncio.CancelledError, Exception):
                pass
        if self._adapter is not None:
            try:
                # Bounded wait so a wedged ``docker stop`` can't
                # deadlock shutdown. 30s covers docker's 10s SIGTERM
                # grace plus our own ``docker stop -t 5`` and drains.
                await asyncio.wait_for(self._adapter.aclose(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "agent %s: adapter aclose timed out after 30s — "
                    "container may still be running, run `docker ps` to check",
                    self.agent_cfg.id,
                )
            except Exception as exc:
                logger.warning(
                    "agent %s: adapter aclose failed: %s", self.agent_cfg.id, exc,
                )
        if self._client is not None:
            # Release WS + SQLite handles. Required on Windows so
            # ``messages.db*`` is renamable by ``agent archive``.
            try:
                await asyncio.wait_for(self._client.stop(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "agent %s: client.stop timed out after 10s",
                    self.agent_cfg.id,
                )
            except Exception as exc:
                logger.warning(
                    "agent %s: client.stop failed: %s", self.agent_cfg.id, exc,
                )
            self._client = None
        self.runtime.status = "stopped"
        self.runtime.save(self.agent_cfg.id)

    async def _run(self) -> None:
        agent_id = self.agent_cfg.id
        try:
            self._adapter = build_adapter(self.daemon_cfg, self.agent_cfg)
            profile_path = str(self.agent_cfg.resolve_profile_path())
            memory_path = str(self.agent_cfg.resolve_memory_dir())
            workspace_path = str(self.agent_cfg.resolve_workspace_dir())
            claude_path = str(self.agent_cfg.resolve_claude_dir())
            Path(memory_path).mkdir(parents=True, exist_ok=True)
            Path(workspace_path).mkdir(parents=True, exist_ok=True)
            _seed_claude_dir(Path(claude_path))

            # Assemble managed CLAUDE.md from shared primer + profile
            # + memory snapshot. Written to user-level (.claude/
            # CLAUDE.md) so Claude Code auto-discovers it. The
            # project-level CLAUDE.md is left for the agent to edit.
            # chat-local / sdk-local don't auto-discover, so the same
            # string is passed as PuffoAgent's system_prompt.
            shared_path = docker_shared_dir()
            ensure_shared_primer(shared_path)
            sync_shared_skills(shared_path, Path(workspace_path))
            primer = read_shared_primer(shared_path)
            try:
                profile_text = Path(profile_path).read_text(encoding="utf-8")
            except OSError:
                profile_text = ""
            claude_md = assemble_claude_md(
                shared_primer=primer,
                profile=profile_text,
                memory_snapshot=read_memory_snapshot(Path(memory_path)),
            )
            write_claude_md(agent_claude_user_dir(agent_id), claude_md)

            # Mirror the same content to user-level GEMINI.md so a
            # harness swap doesn't need another sync cycle.
            write_gemini_md(
                agent_home_dir(agent_id) / ".gemini", claude_md,
            )

            # One-time migration: remove an older project-level
            # managed CLAUDE.md, but only if it still carries our
            # managed-content marker — never clobber user content.
            old_managed = Path(claude_path) / "CLAUDE.md"
            if looks_like_managed_claude_md(old_managed):
                try:
                    old_managed.unlink()
                    logger.info(
                        "agent %s: migrated stale managed CLAUDE.md out of %s",
                        agent_id, old_managed,
                    )
                except OSError as exc:
                    logger.warning(
                        "agent %s: could not remove stale %s: %s",
                        agent_id, old_managed, exc,
                    )

            puffo = PuffoAgent(
                adapter=self._adapter,
                system_prompt=claude_md,
                memory_dir=memory_path,
                workspace_dir=workspace_path,
                claude_dir=claude_path,
                agent_id=agent_id,
            )

            if not self.agent_cfg.puffo_core.is_configured():
                raise RuntimeError(
                    f"agent {agent_id!r}: puffo_core block in agent.yml "
                    "is incomplete. Required fields: server_url, slug, "
                    "device_id, space_id."
                )
            client = _build_puffo_core_client(self.agent_cfg, agent_id)
            self._client = client
        except Exception as e:
            logger.error("agent %s: failed to initialise: %s", agent_id, e, exc_info=True)
            self.runtime.status = "error"
            self.runtime.error = str(e)
            self.runtime.save(agent_id)
            # Init crashed before warm() — release the startup gate.
            self._warm_done.set()
            return

        # Warm the adapter so persisted-session agents re-spawn their
        # subprocess now rather than on the first DM. Non-fatal.
        try:
            await self._adapter.warm(claude_md)
        except Exception as exc:
            logger.warning(
                "agent %s: warm() failed (will retry on first turn): %s",
                agent_id, exc,
            )
        finally:
            self._warm_done.set()

        reload_flag_path = Path(workspace_path) / ".puffo-agent" / "reload.flag"
        refresh_flag_path = Path(workspace_path) / ".puffo-agent" / "refresh.flag"
        # Per-turn context for the cli-local permission hook. The hook
        # is a separate subprocess and reads this file to learn which
        # channel + root to reply to.
        current_turn_path = Path(workspace_path) / ".puffo-agent" / "current_turn.json"

        async def on_message_batch(
            root_id: str,
            batch: list[dict],
            channel_meta: dict,
        ):
            """One agent turn per thread batch. The puffo-core client
            collapses every arrival on the same ``root_id`` into a
            single list and hands it here in arrival order. The agent
            sees every message in one turn and decides whom (and how
            many times) to reply on its own.

            Server-side processing-run telemetry is keyed on the
            triggering post id; we use the LAST envelope in the batch
            as that anchor since it's the most recent thing the agent
            is reasoning about. The reply, if any, posts back to
            ``root_id`` as a thread reply (or to the channel root for
            a top-level batch).
            """
            if not batch:
                return
            # Diagnostic: log every dispatched batch so we can trace
            # cross-batch duplicates (same envelope_id surfacing in
            # consecutive turns). If the SAME envelope_id appears
            # across two log lines for one agent within a few
            # seconds, the cursor or dispatching_ids check missed it.
            batch_ids = [m.get("envelope_id", "") for m in batch]
            logger.info(
                "agent %s: on_message_batch root=%s size=%d envelopes=%s",
                agent_id, root_id, len(batch), batch_ids,
            )
            # Status telemetry is now per-thread-batch. The first
            # message in arrival order gets the /processing/start
            # call (yellow dot lands there) — that's what the human
            # who triggered the agent will see go yellow first. The
            # rest of the batch flips straight from white to green
            # via /processing/end:batch at the end of the turn.
            first_post_id = batch[0].get("envelope_id", "")
            channel_id = channel_meta.get("channel_id", "")

            # Honour reload before the turn so the first batch after
            # a flag-drop picks up fresh CLAUDE.md / profile / memory.
            if reload_flag_path.exists():
                await _reload_from_disk(
                    agent_id=agent_id,
                    shared_path=shared_path,
                    profile_path=profile_path,
                    memory_path=memory_path,
                    workspace_path=workspace_path,
                    puffo=puffo,
                    adapter=self._adapter,
                    flag_path=reload_flag_path,
                )
                # Reload subsumes refresh; drop any sibling flag so
                # we don't double-restart.
                try:
                    refresh_flag_path.unlink()
                except OSError:
                    pass
            elif refresh_flag_path.exists():
                await _refresh_from_disk(
                    agent_id=agent_id,
                    adapter=self._adapter,
                    flag_path=refresh_flag_path,
                )
            try:
                current_turn_path.parent.mkdir(parents=True, exist_ok=True)
                current_turn_path.write_text(
                    json.dumps({
                        "channel_id": channel_id,
                        "root_id": root_id,
                        "triggering_post_id": first_post_id,
                    }),
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning(
                    "agent %s: could not write current_turn.json: %s "
                    "(permission hook will fail-open)", agent_id, exc,
                )
            # Server-side processing-run + status transitions.
            # Reporter swallows network errors so a flaky status push
            # never blocks the actual reply.
            run_id = (
                await reporter.begin_turn(first_post_id)
                if first_post_id
                else None
            )
            turn_succeeded = True
            turn_error: str | None = None
            try:
                reply = await puffo.handle_message_batch(
                    root_id=root_id,
                    batch=batch,
                    channel_meta=channel_meta,
                )
            except AgentAPIError as exc:
                # Adapter surfaced an "API Error" string. Mark turn
                # errored and re-raise; the consumer loop re-enqueues
                # the batch with cursor preserved and backs off.
                logger.warning("agent %s: api-error retry: %s", agent_id, exc)
                reply = None
                turn_succeeded = False
                turn_error = "API Error"
                raise
            except Exception as exc:
                logger.error(
                    "agent %s: handle_message_batch error: %s",
                    agent_id, exc, exc_info=True,
                )
                reply = None
                turn_succeeded = False
                turn_error = f"{type(exc).__name__}: {exc}"
            finally:
                if run_id is not None and first_post_id:
                    # Build the batch payload: first row reuses the
                    # /start run_id (server UPDATEs its row); the
                    # rest get fresh run_ids and are UPSERTed by the
                    # server with started_at = ended_at = now.
                    runs: list[dict] = [{
                        "run_id": run_id,
                        "message_id": first_post_id,
                        "succeeded": turn_succeeded,
                        "error_text": turn_error,
                    }]
                    for msg in batch[1:]:
                        mid = msg.get("envelope_id", "")
                        if not mid:
                            continue
                        runs.append({
                            "run_id": f"run_{uuid.uuid4().hex}",
                            "message_id": mid,
                            "succeeded": turn_succeeded,
                            "error_text": turn_error,
                        })
                    await reporter.end_turn_batch(runs)
                # Clear turn context so post-turn background work
                # doesn't inherit a stale channel/root. Hook
                # fails-open when the file is absent.
                try:
                    current_turn_path.unlink()
                except OSError:
                    pass
            # One batch = one "message" for the runtime counter; the
            # display still reads as "N messages processed."
            self.runtime.msg_count += 1
            self.runtime.last_event_at = int(time.time())
            if reply:
                # Fallback reply when the agent skipped both
                # send_message and [SILENT]. Post to root so the
                # reply lands in the thread the agent was reading.
                await client.post_message(channel_id, reply, root_id=root_id)

        async def heartbeat():
            interval = max(1.0, self.daemon_cfg.runtime_heartbeat_seconds)
            while not self._stop.is_set():
                self.runtime.save(agent_id)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass

        async def credential_refresh():
            """Periodically refresh OAuth credentials before they
            expire. The adapter's mtime check skips the work when
            another consumer just refreshed the shared file."""
            # Skip the first tick to avoid piling onto warm().
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=CREDENTIAL_REFRESH_TICK_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                pass
            while not self._stop.is_set():
                try:
                    await self._adapter.refresh_ping()
                except Exception as exc:
                    logger.warning(
                        "agent %s: credential refresh tick failed: %s",
                        agent_id, exc,
                    )
                # Reflect the probe result into runtime.health so
                # ``puffoagent status`` shows auth_failed without log
                # tailing. None pre-first-probe stays "unknown".
                probed = getattr(self._adapter, "auth_healthy", None)
                if probed is True:
                    self.runtime.health = "ok"
                elif probed is False:
                    self.runtime.health = "auth_failed"
                self.runtime.save(agent_id)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=CREDENTIAL_REFRESH_TICK_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass

        # Server-side status reporter: own heartbeat task; begin_turn /
        # end_turn fire inline from on_message via this closure. Falls
        # back to a no-op when the client has no http client (tests).
        reporter = StatusReporter(client.http) if hasattr(client, "http") else None
        if reporter is None:  # pragma: no cover — defensive
            class _NoopReporter:
                async def begin_turn(self, _mid):
                    return None
                async def end_turn(self, *_a, **_kw):
                    return None
                async def end_turn_batch(self, *_a, **_kw):
                    return None
                async def report_error(self, _t):
                    return None
                async def run_heartbeat_loop(self):
                    return None
                def stop(self):
                    return None
            reporter = _NoopReporter()  # type: ignore[assignment]

        hb_task = asyncio.ensure_future(heartbeat())
        refresh_task = asyncio.ensure_future(credential_refresh())
        status_task = asyncio.ensure_future(reporter.run_heartbeat_loop())
        try:
            while not self._stop.is_set():
                try:
                    await client.listen(on_message=on_message_batch)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "agent %s: listen() crashed: %s: %s — reconnecting in %.1fs",
                        agent_id, type(exc).__name__, exc, RECONNECT_BACKOFF_SECONDS,
                    )
                    self.runtime.error = f"{type(exc).__name__}: {exc}"
                    self.runtime.save(agent_id)
                    # Surface the failure on the agent's row so the
                    # operator sees it without tailing logs.
                    try:
                        await reporter.report_error(self.runtime.error or "listen crashed")
                    except Exception:
                        pass
                if self._stop.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=RECONNECT_BACKOFF_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            reporter.stop()
            hb_task.cancel()
            refresh_task.cancel()
            status_task.cancel()
            for task in (hb_task, refresh_task, status_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            self.runtime.status = "stopped"
            self.runtime.save(agent_id)


async def _reload_from_disk(
    *,
    agent_id: str,
    shared_path: Path,
    profile_path: str,
    memory_path: str,
    workspace_path: str,
    puffo,
    adapter,
    flag_path: Path,
) -> None:
    """Rebuild managed CLAUDE.md from disk and ask the adapter to drop
    any cached subprocess. Failures are logged but don't drop the
    turn — a stale prompt beats a dropped message."""
    try:
        ensure_shared_primer(shared_path)
        sync_shared_skills(shared_path, Path(workspace_path))
        primer = read_shared_primer(shared_path)
        try:
            profile_text = Path(profile_path).read_text(encoding="utf-8")
        except OSError:
            profile_text = ""
        new_md = assemble_claude_md(
            shared_primer=primer,
            profile=profile_text,
            memory_snapshot=read_memory_snapshot(Path(memory_path)),
        )
        write_claude_md(agent_claude_user_dir(agent_id), new_md)
        puffo.system_prompt = new_md
        await adapter.reload(new_md)
        logger.info("agent %s: reloaded system prompt from disk", agent_id)
    except Exception as exc:
        logger.warning("agent %s: reload failed: %s", agent_id, exc)
    finally:
        try:
            flag_path.unlink()
        except OSError:
            pass


async def _refresh_from_disk(
    *,
    agent_id: str,
    adapter,
    flag_path: Path,
) -> None:
    """Drop the cached subprocess so the next turn re-reads skills,
    .mcp.json, .claude.json — without rebuilding CLAUDE.md. The flag
    payload may include a ``model`` override.

    Cheaper than ``_reload_from_disk``; use this for config churn
    (new skills, model switch) and reload for CLAUDE.md edits.
    """
    try:
        try:
            raw = flag_path.read_text(encoding="utf-8")
            payload = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError):
            payload = {}
        new_model = payload.get("model") if isinstance(payload, dict) else None
        if new_model is not None and hasattr(adapter, "model"):
            old_model = getattr(adapter, "model", "")
            adapter.model = str(new_model)
            logger.info(
                "agent %s: model override via refresh: %r -> %r",
                agent_id, old_model, adapter.model,
            )
        # reload() ignores its argument — both adapters just tear
        # down the subprocess; next turn re-reads all on-disk config.
        await adapter.reload("")
        logger.info(
            "agent %s: refreshed (subprocess will respawn next turn)",
            agent_id,
        )
    except Exception as exc:
        logger.warning("agent %s: refresh failed: %s", agent_id, exc)
    finally:
        try:
            flag_path.unlink()
        except OSError:
            pass


_CLAUDE_DIR_SUBDIRS = ("agents", "commands", "skills", "hooks")


def _seed_claude_dir(claude_dir: Path) -> None:
    """Create the Claude Code project-level skeleton (agents/,
    commands/, skills/, hooks/). Idempotent — never overwrites."""
    claude_dir.mkdir(parents=True, exist_ok=True)
    for sub in _CLAUDE_DIR_SUBDIRS:
        (claude_dir / sub).mkdir(exist_ok=True)
