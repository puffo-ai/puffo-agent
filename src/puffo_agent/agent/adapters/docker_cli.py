"""Docker CLI adapter.

Runs the Claude Code CLI inside a per-agent Docker container. The
container is the sandbox; Claude Code runs with
``--dangerously-skip-permissions`` inside.

Auth: each agent gets its own isolated claude identity at
``~/.puffo-agent/agents/<id>/.claude/`` (sessions, history, cache,
settings — seeded once from the operator's real ``~/.claude``). A private
credential view is refreshed from the host before the per-agent Claude home
is mounted into the container.

A second bind-mount exposes ``~/.puffo-agent/shared/`` at
``/workspace/.shared`` so all agents on this host can cooperate at
the filesystem level.

Lifecycle:
  - container: one per agent (``puffo-<id>``), started lazily,
    ``docker stop`` on ``aclose()``.
  - claude: one long-lived stream-json subprocess inside the
    container, kept alive across turns by ``ClaudeSession``.
  - session id: persisted to ``cli_session.json`` so daemon /
    container restarts re-spawn with ``--resume <id>``.

Image: bundled inline as a Dockerfile string, built on first use.
Users can override via ``runtime.docker_image`` to skip the build.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ..cli_bin import resolve_docker_bin
from ...mcp.config import (
    INFERENCE_LEVELS,
    write_cli_mcp_config,
)
from ...portal.state import (
    seed_claude_home,
    strip_claude_api_key_from_settings,
    sync_host_claude_code_auth_view,
    sync_host_enabled_plugins,
    sync_host_mcp_servers,
    sync_host_skills,
)
from .base import Adapter, TurnContext, TurnResult
from ..context_controller import (
    ContextCapabilities,
    ContextSnapshot,
)
from .cli_session import AuditLog, ClaudeSession


logger = logging.getLogger(__name__)


def _puffo_agent_pkg_dir() -> Path:
    """Host-side puffo_agent package import root, bind-mounted
    read-only into cli-docker containers at /opt/puffoagent-pkg so
    the in-container puffo-core MCP server can ``import puffo_agent.*``.
    """
    import puffo_agent

    return Path(puffo_agent.__file__).resolve().parent.parent


# Bump on Dockerfile changes so existing hosts rebuild without manual
# image-tag pruning. ``_ensure_image`` only builds when the tag is
# missing locally.
DEFAULT_IMAGE = "puffo/agent-runtime:v18"
CONTAINER_LAYOUT_VERSION = "18"

# Pinned Claude Code CLI version baked into the image. Floating would
# let an upstream release shift the stream-json protocol or
# ``--permission-mode`` semantics under us; bump deliberately after
# verification.
CLAUDE_CODE_NPM_VERSION = "2.1.224"

DOCKER_COMMAND_TIMEOUT_SECONDS = 60.0
DOCKER_BUILD_TIMEOUT_SECONDS = 900.0
_PROBE_FALSE_EXIT = 42

# Kept minimal. The claude CLI refuses --dangerously-skip-permissions
# as root, so we create a non-root ``agent`` user. UID doesn't need
# to match the host: Docker Desktop's VFS maps bind-mount perms.
#
# PID 1 tails the host-written audit log (via the workspace bind-
# mount) so ``docker logs <container>`` streams turn events. Without
# it the container would be a black box since the claude subprocess
# is spawned via docker-exec and its stdout returns to the host
# adapter, not container PID 1.
DOCKERFILE = """\
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git curl ca-certificates jq ripgrep \\
        python3 python3-pip \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@__CLAUDE_CODE_VERSION__

# Puffo MCP tools server deps. ``--break-system-packages`` is
# required on Debian bookworm (PEP 668); acceptable since the
# container is single-purpose and disposable. ``uv`` ships ``uvx``
# (Python counterpart of ``npx``) so agents can register stdio MCPs
# without per-server pip/npm install.
RUN pip3 install --break-system-packages --no-cache-dir \\
        "mcp>=1.0" "aiohttp>=3.9" "uv>=0.5" \\
        "cryptography>=43" "pyhpke>=0.6" "aiosqlite>=0.20" "pyyaml>=6.0"

RUN useradd -m -u 2000 -s /bin/bash agent
USER agent
WORKDIR /workspace

# GNU ``tail -F`` relies on inotify, and inotify doesn't propagate
# through Docker Desktop's host bind-mount on Windows / macOS.
# Instead we poll file size each second and emit newly-appended
# bytes to stdout for ``docker logs``. Start from EOF so we don't
# re-dump history on every restart.
CMD ["sh", "-c", "set -eu; mkdir -p /workspace/.puffo-agent; touch /workspace/.puffo-agent/audit.log; echo \\"[$(date -u +%FT%TZ)] puffo agent=${PUFFO_AGENT_ID:-unknown} container starting; polling /workspace/.puffo-agent/audit.log every 1s\\"; last=$(stat -c%s /workspace/.puffo-agent/audit.log 2>/dev/null || echo 0); while :; do size=$(stat -c%s /workspace/.puffo-agent/audit.log 2>/dev/null || echo 0); if [ \\"$size\\" -gt \\"$last\\" ]; then tail -c +$((last + 1)) /workspace/.puffo-agent/audit.log; last=$size; elif [ \\"$size\\" -lt \\"$last\\" ]; then last=0; fi; sleep 1; done"]
""".replace(
    "__CLAUDE_CODE_VERSION__",
    CLAUDE_CODE_NPM_VERSION,
)


class DockerCLIAdapter(Adapter):
    def __init__(
        self,
        agent_id: str,
        model: str,
        image: str,
        workspace_dir: str,
        claude_dir: str,
        session_file: str,
        agent_home_dir: str,
        shared_fs_dir: str,
        owner_username: str = "",
        inference_level: str = "",
        auto_compact_threshold_pct: float | None = None,
        harness=None,
        memory_limit: str = "",
        memory_reservation: str = "",
        desired_skills: list[str] | None = None,
        desired_mcps: list[str] | None = None,
        env_overrides: dict[str, str] | None = None,
        puffo_core_server_url: str = "",
        puffo_core_slug: str = "",
        puffo_core_keys_dir: str = "",
        claude_api_key: str = "",
    ):
        self.agent_id = agent_id
        self.model = model
        self.image = image or DEFAULT_IMAGE
        self.workspace_dir = workspace_dir
        self.claude_dir = claude_dir
        self.session_file = Path(session_file)
        self.container_name = f"puffo-{agent_id}"
        # Agent's virtual $HOME; only .claude and .claude.json
        # are bind-mounted in, not the whole home, so the container's
        # default home skeleton stays intact.
        self.agent_home_dir = Path(agent_home_dir)
        self.claude_home_src = self.agent_home_dir / ".claude"
        # Cross-agent cooperation dir; same mount in every container
        # on this host — intentional escape hatch from per-agent
        # isolation.
        self.shared_fs_dir = Path(shared_fs_dir)
        self.owner_username = owner_username
        self.inference_level = inference_level
        self.auto_compact_threshold_pct = auto_compact_threshold_pct
        # Optional cgroup caps. ``--memory`` is a hard ceiling that
        # OOM-kills processes in this container only; ``--memory-
        # reservation`` is a soft floor. Bound a runaway claude so it
        # doesn't drain the VM and trigger ENOMEM on neighbours' small
        # reads. Empty = no flag = Docker default unbounded.
        self.memory_limit = memory_limit
        self.memory_reservation = memory_reservation
        # Which agent engine runs inside the container.
        if harness is None:
            from ..harness import ClaudeCodeHarness

            harness = ClaudeCodeHarness()
        if harness.name() != "claude-code":
            raise ValueError(
                f"Docker harness {harness.name()!r} is not executable; "
                "the supported Docker harness is 'claude-code'"
            )
        self.harness = harness
        # Installed into the bind-mounted Claude home on first start.
        self.desired_skills = list(desired_skills or [])
        self.desired_mcps = list(desired_mcps or [])
        self.env_overrides = {
            str(key): str(value) for key, value in (env_overrides or {}).items()
        }
        self.puffo_core_server_url = puffo_core_server_url
        self.puffo_core_slug = puffo_core_slug
        self.puffo_core_keys_dir = puffo_core_keys_dir
        self.claude_api_key = claude_api_key
        self._desired_installed = False
        self._started_lock = asyncio.Lock()
        self._started = False
        self._docker_bin = "docker"
        self._session: ClaudeSession | None = None
        # Set post-construction by worker.py. When non-None, claude-
        # code is routed at ``puffo_core_server``. Values must be
        # CONTAINER-local paths since the MCP subprocess runs inside
        # the container.
        self.puffo_core_mcp_env: dict[str, str] | None = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        await self._ensure_started()
        user_message = ctx.messages[-1]["content"] if ctx.messages else ""
        session = self._ensure_session()
        return await session.run_turn(user_message, ctx.system_prompt)

    async def run_retry_turn(
        self,
        kick_text: str,
        fallback_user_message: str,
        ctx: TurnContext,
    ) -> TurnResult:
        await self._ensure_started()
        session = self._ensure_session()
        return await session.run_retry_turn(
            kick_text,
            fallback_user_message,
            ctx.system_prompt,
        )

    async def warm(self, system_prompt: str) -> None:
        """Start the container eagerly; spawn the claude subprocess
        only when this agent has a persisted session (fresh agents
        wait for their first message). Container always starts so
        ``docker logs`` tailing is useful even when idle.
        """
        await self._ensure_started()
        session = self._ensure_session()
        if not session.has_persisted_session():
            logger.info(
                "agent %s: no persisted session; deferring claude spawn until first message",
                self.agent_id,
            )
            return
        await session.warm(system_prompt)

    async def reload(
        self,
        new_system_prompt: str,
        *,
        with_session: bool = False,
    ) -> None:
        """Close the in-container claude subprocess so the next turn
        re-reads CLAUDE.md; container stays up.
        ``with_session=True`` also unlinks ``cli_session.json``."""
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if with_session:
            try:
                self.session_file.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "agent %s: couldn't unlink session file %s: %s",
                    self.agent_id,
                    self.session_file,
                    exc,
                )

    async def get_context_snapshot(self) -> ContextSnapshot:
        return await self._ensure_session().get_context_snapshot()

    def context_limits(self) -> tuple[int | None, int | None]:
        return self._ensure_session().context_limits()

    def get_context_capabilities(self) -> ContextCapabilities:
        return self._ensure_session().get_context_capabilities()

    async def compact_context(self):
        return await self._ensure_session().compact_context()

    async def rollover_context(self):
        return await self._ensure_session().rollover_context()

    def get_provider_session_id(self) -> str | None:
        return self._ensure_session().get_provider_session_id()

    def register_admission_callback(
        self,
        callback,
        planning_cycle_key: str = "",
    ) -> None:
        self._ensure_session().register_admission_callback(
            callback,
            planning_cycle_key,
        )

    register_provider_admission_callback = register_admission_callback

    def register_continuation_callback(
        self,
        callback,
        planning_cycle_key: str = "",
        *,
        channel_id: str = "",
        tool_names: tuple[str, ...] = (),
        tool_arguments: dict[str, object] | None = None,
        correlation_receipt: str = "",
    ) -> None:
        self._ensure_session().register_continuation_callback(
            callback,
            planning_cycle_key,
            channel_id=channel_id,
            tool_names=tool_names,
            tool_arguments=tool_arguments,
            correlation_receipt=correlation_receipt,
        )

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if not self._started:
            return
        # ``docker stop`` (not ``rm -f``) preserves the container's
        # fs — workspace, claude session files, MCP config — so the
        # next start resumes ``--resume <session_id>`` cleanly.
        # ``-t 5`` shortens docker's 10s SIGTERM grace; stays within
        # Worker.stop's 30s asyncio.wait_for even on slow Windows.
        await _run_cmd(
            [self._docker_bin, "stop", "-t", "5", self.container_name],
            check=False,
        )
        self._started = False

    def _ensure_session(self) -> ClaudeSession:
        if self._session is not None:
            return self._session
        extra = self._prepare_mcp_args()
        environment = dict(os.environ)
        environment.pop("ANTHROPIC_API_KEY", None)
        if self.claude_api_key:
            environment["ANTHROPIC_API_KEY"] = self.claude_api_key
        self._session = ClaudeSession(
            agent_id=self.agent_id,
            session_file=self.session_file,
            build_command=self._build_command,
            # cwd is WORKDIR /workspace inside the container.
            cwd=None,
            # Host-side write; the workspace bind-mount delivers it
            # to the container's tail loop and ``docker logs``.
            audit=AuditLog(
                Path(self.workspace_dir) / ".puffo-agent" / "audit.log",
                self.agent_id,
            ),
            extra_args=extra,
            model=self.model,
            env=environment,
        )
        return self._session

    def _build_command(
        self,
        extra_args: list[str],
        env_overrides: dict[str, str] | None = None,
    ) -> list[str]:
        self._strip_claude_api_key_settings()
        cmd: list[str] = [self._docker_bin, "exec", "-i"]
        if self.claude_api_key:
            cmd.extend(["-e", "ANTHROPIC_API_KEY"])
        else:
            cmd.extend(["-e", "ANTHROPIC_API_KEY="])
        # ``env_overrides`` flows in before the container name so
        # docker treats each ``-e KEY=VALUE`` as an exec flag.
        merged_overrides = {**self.env_overrides, **(env_overrides or {})}
        for key, value in merged_overrides.items():
            if key in {"ANTHROPIC_API_KEY", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"}:
                continue
            cmd.extend(["-e", f"{key}={value}"])
        cmd.extend(
            [
                self.container_name,
                "claude",
                "--dangerously-skip-permissions",
            ]
        )
        from ...portal.control.context_telemetry import claude_autocompact_tokens

        compact_tokens = claude_autocompact_tokens(
            model=self.model,
            pct=self.auto_compact_threshold_pct,
            env={},
        )
        if compact_tokens is not None:
            cmd.extend(["--autocompact", str(compact_tokens)])
        if self.model:
            cmd.extend(["--model", self.model])
        if self.inference_level:
            if self.inference_level in INFERENCE_LEVELS:
                cmd.extend(["--effort", self.inference_level])
            else:
                logger.warning(
                    "agent %s: ignoring inference_level %r for claude-code "
                    "(expected one of %s)",
                    self.agent_id,
                    self.inference_level,
                    ", ".join(INFERENCE_LEVELS),
                )
        cmd.extend(extra_args)
        return cmd

    def _strip_claude_api_key_settings(self) -> None:
        paths: list[Path] = []
        claude_home_src = getattr(self, "claude_home_src", None)
        if claude_home_src is not None:
            paths.extend(
                [
                    Path(claude_home_src) / "settings.json",
                    Path(claude_home_src) / "settings.local.json",
                ]
            )
        claude_dir = getattr(self, "claude_dir", None)
        if claude_dir is not None:
            paths.extend(
                [
                    Path(claude_dir) / "settings.json",
                    Path(claude_dir) / "settings.local.json",
                ]
            )
        for settings_path in paths:
            strip_claude_api_key_from_settings(settings_path)

    def _prepare_mcp_args(self) -> list[str]:
        """Write the per-agent MCP config into the workspace and
        return the corresponding claude CLI flags. No
        ``--permission-prompt-tool`` — the container is the sandbox.
        """
        config_host = Path(self.workspace_dir) / ".puffo-agent" / "mcp-config.json"

        # Path values must be CONTAINER-local — override whatever the
        # worker put in the env dict from the host side.
        if self.puffo_core_mcp_env is not None:
            env = dict(self.puffo_core_mcp_env)
            env["PUFFO_CORE_KEYSTORE_DIR"] = "/home/agent/.puffo-agent-state/keys"
            # No PUFFO_CORE_DB_PATH — SQLite reads route via the
            # daemon's data service.
            env["PUFFO_WORKSPACE"] = "/workspace"
            env["PUFFO_MEMORY_DIR"] = "/home/agent/.puffo-agent-state/memory"
            env["PUFFO_RUNTIME_KIND"] = "cli-docker"
            env["PUFFO_HARNESS"] = self.harness.name()
            env["PYTHONPATH"] = "/opt/puffoagent-pkg"
            write_cli_mcp_config(
                config_host,
                command="python3",
                args=["-m", "puffo_agent.mcp.puffo_core_server"],
                env=env,
            )
            return ["--mcp-config", "/workspace/.puffo-agent/mcp-config.json"]

        logger.warning(
            "agent %s: cli-docker MCP tools unavailable — puffo_core is "
            "not configured. populate `puffo_core:` in agent.yml so "
            "send_message / list_channels_in_all_spaces / etc. show up under "
            "claude-code's tool surface.",
            self.agent_id,
        )
        return []

    async def _puffo_pkg_mount_is_current(self) -> bool | None:
        """``True`` iff the existing container's
        ``/opt/puffoagent-pkg`` bind mount still resolves to a
        directory containing the ``puffo_agent`` package.

        Implemented as a ``docker exec test -f`` rather than
        comparing ``docker inspect``'s Mount.Source against
        ``_puffo_agent_pkg_dir()`` because Docker Desktop on Windows
        rewrites the source path (``/run/desktop/mnt/host/c/...``)
        and a literal string compare wouldn't survive that. The
        in-container probe is authoritative: if claude-code's MCP
        subprocess can ``import puffo_agent`` from the bind mount,
        ``__init__.py`` must be visible — and if it isn't, the
        subprocess will crash and every puffo MCP tool will surface
        as "No such tool available".
        """
        rc, _, _ = await _run_cmd(
            [
                self._docker_bin,
                "exec",
                self.container_name,
                "sh",
                "-c",
                "test -f /opt/puffoagent-pkg/puffo_agent/__init__.py "
                f"&& exit 0 || exit {_PROBE_FALSE_EXIT}",
            ],
            check=False,
        )
        return _probe_result(rc)

    async def _container_harness_is_current(self) -> bool | None:
        rc, _, _ = await _run_cmd(
            [
                self._docker_bin,
                "exec",
                self.container_name,
                "sh",
                "-c",
                "if command -v claude >/dev/null; then exit 0; "
                f"else exit {_PROBE_FALSE_EXIT}; fi",
            ],
            check=False,
        )
        return _probe_result(rc)

    async def _container_state(self) -> str | None:
        """Docker-reported container State.Status (``running``,
        ``exited``, ``paused``, ``created``, ``dead``), ``""`` when
        the container doesn't exist, or ``None`` when Docker could not
        answer the probe.
        """
        rc, out, _ = await _run_cmd(
            [
                self._docker_bin,
                "container",
                "ls",
                "--all",
                "--filter",
                f"name=^/{self.container_name}$",
                "--format",
                "{{.State}}",
            ],
            check=False,
        )
        if rc != 0:
            return None
        return out.decode("utf-8", errors="replace").strip()

    async def _install_desired(self) -> None:
        """Install container-compatible desired assets once per instance."""
        if self._desired_installed:
            return
        self._desired_installed = True
        if not self.desired_skills and not self.desired_mcps:
            return
        from .desired_install import run_spawn_install

        await run_spawn_install(
            agent_id=self.agent_id,
            agent_home=self.agent_home_dir,
            workspace_dir=Path(self.workspace_dir),
            harness_name=self.harness.name(),
            desired_skills=self.desired_skills,
            desired_mcps=self.desired_mcps,
            server_url=self.puffo_core_server_url,
            slug=self.puffo_core_slug,
            keys_dir=self.puffo_core_keys_dir,
            containerized=True,
        )

    async def _ensure_started(self) -> None:
        async with self._started_lock:
            if self._started:
                return
            self._require_docker()
            host_home = Path.home()
            await self._sync_claude_host_assets(host_home)
            self._warn_missing_claude_credentials(host_home)
            existed = await self._start_or_resume_container()
            await self._recreate_if_mount_stale(existed)
            self._started = True

    def _require_docker(self) -> None:
        docker_bin = resolve_docker_bin()
        if docker_bin is None:
            raise RuntimeError(
                "docker binary not found. Tried $PUFFO_DOCKER_BIN, $PATH, "
                "the persistent user PATH, and known Docker Desktop install "
                "locations. Install Docker Desktop (Windows/macOS) or "
                "docker-ce (Linux) to use runtime kind 'cli-docker'."
            )
        self._docker_bin = docker_bin

    async def _sync_claude_host_assets(self, host_home: Path) -> None:
        if seed_claude_home(host_home, self.agent_home_dir):
            logger.info(
                "agent %s: seeded per-agent virtual $HOME at %s from %s",
                self.agent_id,
                self.agent_home_dir,
                host_home,
            )
        self._strip_claude_api_key_settings()
        auth_mode = sync_host_claude_code_auth_view(
            host_home,
            self.agent_home_dir,
        )
        logger.info(
            "agent %s: wrote host Claude credential view (%s)",
            self.agent_id,
            auth_mode,
        )
        skill_count = sync_host_skills(host_home, self.agent_home_dir)
        if skill_count:
            logger.info(
                "agent %s: synced %d host skill(s) into %s",
                self.agent_id,
                skill_count,
                self.agent_home_dir / ".claude" / "skills",
            )
        await self._install_desired()
        merged_mcp, unreachable = sync_host_mcp_servers(
            host_home,
            self.agent_home_dir,
            containerized=True,
        )
        if merged_mcp:
            logger.info(
                "agent %s: merged %d host MCP server registration(s) "
                "into per-agent .claude.json",
                self.agent_id,
                merged_mcp,
            )
        for name, command in unreachable:
            logger.warning(
                "agent %s: host MCP %r has host-local path %r that won't "
                "resolve inside the container — SKIPPED (not injected). "
                "Install the binary in the image or bind-mount it, then "
                "re-sync, to make this MCP available.",
                self.agent_id,
                name,
                command,
            )
        enabled_count = sync_host_enabled_plugins(host_home, self.agent_home_dir)
        if enabled_count:
            logger.info(
                "agent %s: propagated %d enabledPlugins entry/entries "
                "from host settings.json",
                self.agent_id,
                enabled_count,
            )

    def _warn_missing_claude_credentials(self, host_home: Path) -> None:
        if self.claude_api_key:
            return
        credentials = self.agent_home_dir / ".claude" / ".credentials.json"
        if not credentials.exists():
            logger.warning(
                "agent %s: host has no %s — run `claude login` on the "
                "host, then restart the agent. First turn will fail "
                "with an auth error otherwise.",
                self.agent_id,
                credentials,
            )

    async def _start_or_resume_container(self) -> bool:
        state = await self._container_state()
        if state is None:
            raise RuntimeError(
                f"could not inspect Docker container {self.container_name!r}; "
                "refusing to create or replace it while Docker is unavailable"
            )
        if state == "running":
            logger.info(
                "agent %s: reusing running container %r",
                self.agent_id,
                self.container_name,
            )
        elif state in ("exited", "created", "dead"):
            logger.info(
                "agent %s: starting existing container %r (was %s)",
                self.agent_id,
                self.container_name,
                state,
            )
            await _run_cmd([self._docker_bin, "start", self.container_name])
        elif state == "paused":
            logger.info(
                "agent %s: unpausing container %r",
                self.agent_id,
                self.container_name,
            )
            await _run_cmd([self._docker_bin, "unpause", self.container_name])
        elif state == "":
            await self._ensure_image()
            await self._start_container()
        else:
            raise RuntimeError(
                f"Docker container {self.container_name!r} is in transient "
                f"state {state!r}; refusing to replace it"
            )
        return state != ""

    async def _recreate_if_mount_stale(self, existed: bool) -> None:
        layout_marker = self.agent_home_dir / ".docker-layout"
        try:
            layout_current = (
                layout_marker.read_text(encoding="utf-8").strip()
                == CONTAINER_LAYOUT_VERSION
            )
        except OSError:
            layout_current = False
        package_current = await self._puffo_pkg_mount_is_current() if existed else True
        harness_current = (
            await self._container_harness_is_current() if existed else True
        )
        if package_current is None or harness_current is None:
            raise RuntimeError(
                f"could not validate existing Docker container "
                f"{self.container_name!r}; refusing to remove it after a "
                "failed probe"
            )
        if existed and not (layout_current and package_current and harness_current):
            logger.warning(
                "agent %s: recreating stale container %r "
                "(layout=%s package=%s harness=%s)",
                self.agent_id,
                self.container_name,
                layout_current,
                package_current,
                harness_current,
            )
            await _run_cmd(
                [self._docker_bin, "rm", "-f", self.container_name],
                check=False,
            )
            await self._ensure_image()
            await self._start_container()
        final_harness_probe = await self._container_harness_is_current()
        if final_harness_probe is None:
            raise RuntimeError(
                f"could not verify harness {self.harness.name()!r} in "
                f"Docker container {self.container_name!r}"
            )
        if not final_harness_probe:
            raise RuntimeError(
                f"docker image {self.image!r} does not provide a working "
                f"{self.harness.name()} harness"
            )
        layout_marker.parent.mkdir(parents=True, exist_ok=True)
        layout_marker.write_text(CONTAINER_LAYOUT_VERSION + "\n", encoding="utf-8")

    async def _ensure_image(self) -> None:
        if await _image_exists_locally(self._docker_bin, self.image):
            return
        if self.image != DEFAULT_IMAGE:
            raise RuntimeError(
                f"docker image {self.image!r} not found locally. "
                f"pull it (`docker pull {self.image}`) or clear "
                "runtime.docker_image to use the bundled default."
            )
        # Daemon-wide lock — concurrent ``docker build -t <tag>``
        # races in BuildKit's exporter and the loser crashes with
        # "image already exists". First wins; others wait and re-check.
        async with _BUILD_LOCK:
            if await _image_exists_locally(self._docker_bin, self.image):
                logger.info(
                    "agent %s: image %s was built by another worker "
                    "during our wait — skipping rebuild",
                    self.agent_id,
                    self.image,
                )
                return
            logger.info(
                "agent %s: building docker image %s (first use — this may take a few minutes)",
                self.agent_id,
                self.image,
            )
            await self._build_image()

    async def _build_image(self) -> None:
        from ..._proc import no_window_kwargs

        proc = await asyncio.create_subprocess_exec(
            self._docker_bin,
            "build",
            "-t",
            self.image,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **no_window_kwargs(),
        )
        stdout, _ = await _communicate_with_timeout(
            proc,
            input_data=DOCKERFILE.encode(),
            timeout_seconds=DOCKER_BUILD_TIMEOUT_SECONDS,
            operation="docker build",
        )
        if proc.returncode != 0:
            tail = stdout.decode("utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"docker build failed:\n{tail}")
        logger.info("agent %s: docker image %s built", self.agent_id, self.image)

    async def _start_container(self) -> None:
        agent_claude_json = self._prepare_container_mounts()
        command = self._container_run_command(agent_claude_json)
        self._add_optional_container_args(command)
        command.append(self.image)
        rc, _, stderr = await _run_cmd(command, check=False)
        if rc != 0:
            raise RuntimeError(
                f"docker run failed for {self.container_name}: "
                f"{stderr.decode('utf-8', errors='replace').strip()[:500]}"
            )

    def _prepare_container_mounts(self) -> Path:
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
        self.agent_home_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_home_dir / ".claude").mkdir(parents=True, exist_ok=True)
        agent_claude_json = self.agent_home_dir / ".claude.json"
        agent_claude_json.touch(exist_ok=True)
        self.shared_fs_dir.mkdir(parents=True, exist_ok=True)
        return agent_claude_json

    def _container_run_command(
        self,
        agent_claude_json: Path,
    ) -> list[str]:
        return [
            self._docker_bin,
            "run",
            "-d",
            "--name",
            self.container_name,
            "-e",
            f"PUFFO_AGENT_ID={self.agent_id}",
            "-v",
            f"{self.workspace_dir}:/workspace",
            "-v",
            f"{self.claude_home_src}:/home/agent/.claude",
            # Sibling .claude.json — without this it lands on the
            # container's ephemeral fs and is lost on restart.
            "-v",
            f"{agent_claude_json}:/home/agent/.claude.json",
            "-v",
            f"{self.shared_fs_dir}:/workspace/.shared",
            "-v",
            f"{_puffo_agent_pkg_dir()}:/opt/puffoagent-pkg:ro",
            # RW because subkey rotation rewrites <slug>.session.json.
            # Mounting :ro surfaced as [Errno 30] from MCP tool calls
            # past the subkey TTL. Whole agent_home_dir is mounted
            # rather than individual files because SQLite WAL files
            # (-wal, -shm) sit alongside the .db.
            "-v",
            f"{self.agent_home_dir}:/home/agent/.puffo-agent-state",
            "--init",
        ]

    def _add_optional_container_args(self, command: list[str]) -> None:
        host_plugins = Path.home() / ".claude" / "plugins"
        if host_plugins.is_dir():
            command.extend(
                [
                    "-v",
                    f"{host_plugins}:/home/agent/.claude/plugins:ro",
                ]
            )
        if self.memory_limit:
            command.extend(["--memory", self.memory_limit])
        if self.memory_reservation:
            command.extend(["--memory-reservation", self.memory_reservation])


# Serialises concurrent ``docker build -t <tag>`` across workers
# (right after an image-tag bump every cli-docker worker would
# otherwise race BuildKit's exporter).
_BUILD_LOCK = asyncio.Lock()


def _probe_result(returncode: int) -> bool | None:
    if returncode == 0:
        return True
    if returncode == _PROBE_FALSE_EXIT:
        return False
    return None


async def _communicate_with_timeout(
    proc: asyncio.subprocess.Process,
    *,
    input_data: bytes | None = None,
    timeout_seconds: float,
    operation: str,
) -> tuple[bytes, bytes]:
    communicate_task = asyncio.create_task(proc.communicate(input_data))
    try:
        return await asyncio.wait_for(
            asyncio.shield(communicate_task),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        await _kill_and_reap(proc, communicate_task)
        raise RuntimeError(
            f"{operation} timed out after {timeout_seconds:g}s; "
            "the child process was terminated"
        ) from exc
    except asyncio.CancelledError:
        await _kill_and_reap(proc, communicate_task)
        raise


async def _kill_and_reap(
    proc: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await communicate_task
    except (BrokenPipeError, ConnectionResetError):
        await proc.wait()


async def _image_exists_locally(docker_bin: str, tag: str) -> bool:
    rc, _, _ = await _run_cmd(
        [docker_bin, "image", "inspect", tag],
        check=False,
    )
    return rc == 0


async def _run_cmd(
    cmd: list[str],
    check: bool = True,
    *,
    timeout_seconds: float = DOCKER_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, bytes, bytes]:
    from ..._proc import no_window_kwargs

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **no_window_kwargs(),
    )
    stdout, stderr = await _communicate_with_timeout(
        proc,
        timeout_seconds=timeout_seconds,
        operation=" ".join(cmd[:2]),
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {stderr.decode('utf-8', errors='replace').strip()[:500]}"
        )
    return proc.returncode, stdout, stderr
