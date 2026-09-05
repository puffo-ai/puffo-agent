"""Prepare and own per-Agent Docker runtimes for ratified Drivers.

Docker owns process placement, mounts, credentials, and bounded container
lifecycle. Claude Code and Codex protocol behavior remains in their normal
Drivers; this module only supplies a ``docker exec -i`` process factory.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...._proc import no_window_kwargs
from ....mcp.config import (
    INFERENCE_LEVELS,
    puffo_core_mcp_env,
    write_cli_mcp_config,
    write_codex_mcp_config,
)
from ....portal.host_assets import filter_container_mcp_servers
from ....portal.runtime_matrix import (
    HARNESS_CLAUDE_CODE,
    HARNESS_CODEX,
    resolve_effective_harness,
    resolve_effective_provider,
)
from ....portal.state import (
    AgentConfig,
    DaemonConfig,
    agent_codex_user_dir,
    agent_dir,
    agent_home_dir,
    claude_cli_api_key,
    cli_session_json_path,
    read_host_codex_mcp_servers,
    seed_claude_home,
    shared_fs_dir,
    strip_claude_api_key_from_settings,
    sync_host_claude_code_auth_view,
    sync_host_codex_auth_view,
    sync_host_codex_skills,
    sync_host_enabled_plugins,
    sync_host_mcp_servers,
    sync_host_skills,
)
from ....portal.workspace_layout import prepare_workspace_shared_access
from ...adapters.base import anthropic_base_url_env
from ...adapters.desired_install import run_spawn_install
from ...cli_bin import resolve_docker_bin
from .docker_support import (
    CONTAINER_LAYOUT_VERSION,
    DEFAULT_IMAGE,
    container_state,
    ensure_docker_image,
    probe_result,
    puffo_agent_pkg_dir,
    run_cmd,
)
from ..driver import RuntimeSpec
from .local_runtime import (
    PreparedLocalRuntime,
    select_native_session,
    _read_json_object,
    build_codex_gateway_provider,
    remove_legacy_permission_hook,
)

logger = logging.getLogger(__name__)

SUPPORTED_DOCKER_DRIVERS = frozenset({HARNESS_CLAUDE_CODE, HARNESS_CODEX})

CLAUDE_CONTAINER_HOME = "/home/agent/.claude"
CODEX_CONTAINER_CODEX_HOME = "/home/agent/.codex"
CONTAINER_STATE_DIR = "/home/agent/.puffo-agent-state"
CONTAINER_SANDBOX = "danger-full-access"
_DOCKER_LAYOUT_MARKER = ".docker-layout"


def _sanitise_permission_mode(mode: str, agent_id: str) -> str:
    if mode in {"bypassPermissions"}:
        return mode
    if mode:
        logger.warning(
            "agent %s: permission_mode %r is not supported; using 'bypassPermissions'",
            agent_id,
            mode,
        )
    return "bypassPermissions"


class DockerRuntimePreparer:
    """Prepare one Claude Code or Codex Driver inside an Agent container."""

    def __init__(self, daemon_cfg: DaemonConfig, agent_cfg: AgentConfig):
        self.daemon_cfg = daemon_cfg
        self.agent_cfg = agent_cfg
        self.agent_id = agent_cfg.id
        provider = resolve_effective_provider("cli-docker", agent_cfg.runtime.provider)
        self.harness_name = resolve_effective_harness(
            "cli-docker", provider, agent_cfg.runtime.harness
        ).strip()
        if self.harness_name not in SUPPORTED_DOCKER_DRIVERS:
            raise RuntimeError(
                f"agent {self.agent_id!r}: runtime.kind='cli-docker' supports "
                "only harness='claude-code' or harness='codex'"
            )
        self.workspace_dir = agent_cfg.resolve_workspace_dir()
        self.claude_dir = agent_cfg.resolve_claude_dir()
        self.agent_home = agent_home_dir(self.agent_id)
        self.memory_dir = agent_cfg.resolve_memory_dir()
        self.codex_home = agent_codex_user_dir(self.agent_id)
        self.shared_fs_dir = shared_fs_dir()
        self.image = agent_cfg.runtime.docker_image or DEFAULT_IMAGE
        self.container_name = f"puffo-{self.agent_id}"
        self.permission_mode = _sanitise_permission_mode(
            agent_cfg.runtime.permission_mode, self.agent_id
        )
        self.model = self._resolve_model()
        self.memory_limit = (
            agent_cfg.runtime.docker_memory_limit or daemon_cfg.docker_memory_limit
        )
        self.memory_reservation = (
            agent_cfg.runtime.docker_memory_reservation
            or daemon_cfg.docker_memory_reservation
        )
        self._docker_bin = "docker"
        self._desired_extras: dict[str, dict] = {}
        self._desired_installed = False
        self._container_stopped = False

    async def prepare(
        self,
        *,
        system_prompt: str,
        persisted_native_session_id: str = "",
        persisted_native_session_harness: str = "",
    ) -> PreparedLocalRuntime:
        spec = await self.refresh_spec(system_prompt)
        legacy_path = self._legacy_session_path()
        legacy_id = self._load_legacy_session_id(legacy_path)
        native_session_id, source = select_native_session(
            harness_name=self.harness_name,
            persisted_native_session_id=persisted_native_session_id,
            persisted_native_session_harness=(
                persisted_native_session_harness
            ),
            legacy_native_session_id=legacy_id,
        )
        await self.ensure_container()
        return PreparedLocalRuntime(
            harness_name=self.harness_name,
            spec=spec,
            native_session_id=native_session_id,
            migration_source=source,
            legacy_session_path=legacy_path,
            preparer=self,
        )

    async def refresh_spec(self, system_prompt: str) -> RuntimeSpec:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.agent_home.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        if self.harness_name == HARNESS_CLAUDE_CODE:
            await self._sync_claude_host_assets()
            return self._prepare_claude_spec(system_prompt)
        self.codex_home.mkdir(parents=True, exist_ok=True)
        agents_md = self.codex_home / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text("", encoding="utf-8")
        await self._install_desired_once()
        return self._prepare_codex_spec(system_prompt)

    def _resolve_model(self) -> str:
        runtime = self.agent_cfg.runtime
        if self.harness_name == HARNESS_CODEX:
            return runtime.model or self.daemon_cfg.openai.model or ""
        return runtime.model or self.daemon_cfg.anthropic.model or ""

    async def _install_desired_once(self) -> None:
        if self._desired_installed:
            return
        self._desired_installed = True
        extras = await run_spawn_install(
            agent_id=self.agent_id,
            agent_home=self.agent_home,
            workspace_dir=self.workspace_dir,
            harness_name=self.harness_name,
            desired_skills=self.agent_cfg.desired_skills,
            desired_mcps=self.agent_cfg.desired_mcps,
            server_url=self.agent_cfg.puffo_core.server_url,
            slug=self.agent_cfg.puffo_core.slug,
            keys_dir=str(agent_dir(self.agent_id) / "keys"),
            containerized=True,
        )
        if extras:
            self._desired_extras = extras

    async def _sync_claude_host_assets(self) -> None:
        host_home = Path.home()
        if seed_claude_home(host_home, self.agent_home):
            logger.info(
                "agent %s: seeded Docker Claude home from %s",
                self.agent_id,
                host_home,
            )
        self._strip_claude_api_key_settings()
        auth_mode = sync_host_claude_code_auth_view(host_home, self.agent_home)
        logger.info(
            "agent %s: refreshed Docker Claude credential view (%s)",
            self.agent_id,
            auth_mode,
        )
        sync_host_skills(host_home, self.agent_home)
        await self._install_desired_once()
        merged, unreachable = sync_host_mcp_servers(
            host_home,
            self.agent_home,
            containerized=True,
        )
        if merged:
            logger.info(
                "agent %s: merged %d container-reachable host MCP server(s)",
                self.agent_id,
                merged,
            )
        for name, command in unreachable:
            logger.warning(
                "agent %s: host MCP %r uses host-local path %r and was "
                "skipped for Docker",
                self.agent_id,
                name,
                command,
            )
        sync_host_enabled_plugins(host_home, self.agent_home)
        credentials = self.agent_home / ".claude" / ".credentials.json"
        if not claude_cli_api_key(self.daemon_cfg) and not credentials.exists():
            logger.warning(
                "agent %s: Claude Code has no credential view; run `claude "
                "login` on the host and restart the Agent",
                self.agent_id,
            )

    def _strip_claude_api_key_settings(self) -> None:
        roots = (self.agent_home / ".claude", self.claude_dir)
        for root in roots:
            for name in ("settings.json", "settings.local.json"):
                strip_claude_api_key_from_settings(root / name)

    def _prepare_claude_spec(self, system_prompt: str) -> RuntimeSpec:
        from ....portal.control.context_telemetry import (
            claude_autocompact_tokens,
            configured_compact_pct,
        )

        launch_args = ["--dangerously-skip-permissions"]
        compact_pct = configured_compact_pct(
            HARNESS_CLAUDE_CODE, self.agent_cfg.env_overrides
        )
        compact_tokens = claude_autocompact_tokens(
            model=self.model,
            pct=compact_pct,
            env=self.agent_cfg.env_overrides,
        )
        if compact_tokens is not None:
            launch_args.extend(["--autocompact", str(compact_tokens)])
        inference = self.agent_cfg.runtime.inference_level
        if inference in INFERENCE_LEVELS:
            launch_args.extend(["--effort", inference])
        elif inference:
            logger.warning(
                "agent %s: ignoring unsupported Claude inference_level %r",
                self.agent_id,
                inference,
            )
        mcp_env = self._container_puffo_mcp_env()
        if mcp_env:
            config_host = self.workspace_dir / ".puffo-agent" / "mcp-config.json"
            write_cli_mcp_config(
                config_host,
                command="python3",
                args=["-m", "puffo_agent.mcp.puffo_core_server"],
                env=mcp_env,
            )
            launch_args.extend(
                ["--mcp-config", "/workspace/.puffo-agent/mcp-config.json"]
            )
        else:
            logger.warning(
                "agent %s: Docker Claude Puffo MCP tools are unavailable "
                "because puffo_core is incomplete",
                self.agent_id,
            )
        remove_legacy_permission_hook(self.agent_home / ".claude")
        environment = {
            key: value
            for key, value in self.agent_cfg.env_overrides.items()
            if key
            not in {
                "ANTHROPIC_API_KEY",
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
            }
        }
        runtime = self.agent_cfg.runtime
        llm_env = anthropic_base_url_env(runtime.llm_base_url)
        if llm_env and runtime.api_key:
            llm_env["ANTHROPIC_API_KEY"] = runtime.api_key
        else:
            configured_key = claude_cli_api_key(self.daemon_cfg)
            if configured_key:
                llm_env["ANTHROPIC_API_KEY"] = configured_key
        environment.update(
            {
                "HOME": "/home/agent",
                "USERPROFILE": "/home/agent",
                **llm_env,
            }
        )
        return RuntimeSpec(
            workspace_dir="/workspace",
            model=self.model,
            system_prompt=system_prompt,
            executable="claude",
            launch_args=tuple(launch_args),
            environment=environment,
            permission_mode=self.permission_mode,
            sandbox=CONTAINER_SANDBOX,
            task_timeout_seconds=self.agent_cfg.runtime.task_timeout_seconds,
            auto_compact_threshold_pct=compact_pct,
            auto_compact_threshold_tokens=compact_tokens,
        )

    def _codex_gateway_provider(self) -> dict[str, str] | None:
        return build_codex_gateway_provider(
            model=self.model,
            llm_base_url=self.agent_cfg.runtime.llm_base_url,
            api_key=self.agent_cfg.runtime.api_key,
        )

    def _container_puffo_mcp_env(self) -> dict[str, str] | None:
        pc = self.agent_cfg.puffo_core
        if not pc.is_configured():
            return None
        env = puffo_core_mcp_env(
            slug=pc.slug,
            device_id=pc.device_id,
            server_url=pc.server_url,
            space_id=pc.space_id,
            keystore_dir=f"{CONTAINER_STATE_DIR}/keys",
            workspace="/workspace",
            shared_workspace="/workspace/shared",
            agent_id=self.agent_id,
            data_service_url=(
                f"http://host.docker.internal:{self.daemon_cfg.data_service.port}"
            ),
            rpc_url=f"http://host.docker.internal:{self.daemon_cfg.rpc_service.port}",
            runtime_kind="cli-docker",
            harness=self.harness_name,
            memory_dir=f"{CONTAINER_STATE_DIR}/memory",
            transport=pc.transport,
        )
        if self.harness_name == HARNESS_CODEX:
            env["CODEX_HOME"] = CODEX_CONTAINER_CODEX_HOME
        env["PYTHONPATH"] = "/opt/puffoagent-pkg"
        return env

    def _prepare_codex_spec(self, system_prompt: str) -> RuntimeSpec:
        host_home = Path.home()
        host_mcps = read_host_codex_mcp_servers(host_home)
        reachable_mcps, unreachable = filter_container_mcp_servers(host_mcps)
        for name, command in unreachable:
            logger.warning(
                "agent %s: host codex MCP %r has host-local path %r that won't "
                "resolve inside the container — SKIPPED (not injected). "
                "Install the binary in the image or bind-mount it, then "
                "re-sync, to make this MCP available.",
                self.agent_id,
                name,
                command,
            )
        extras = dict(self._desired_extras)
        extras.update(reachable_mcps)
        gateway = self._codex_gateway_provider()
        config_kwargs: dict[str, Any] = {
            "extra_servers": extras,
            "inference_level": self.agent_cfg.runtime.inference_level,
            "provider": gateway,
        }
        puffo_env = self._container_puffo_mcp_env()
        if puffo_env:
            config_kwargs.update(
                {
                    "command": "python3",
                    "args": ["-m", "puffo_agent.mcp.puffo_core_server"],
                    "env": puffo_env,
                }
            )
        else:
            logger.warning(
                "agent %s: cli-docker codex MCP tools unavailable — "
                "puffo_core is not configured. populate `puffo_core:` in "
                "agent.yml so send_message / list_channels_in_all_spaces / "
                "etc. show up under codex's tool surface.",
                self.agent_id,
            )
        write_codex_mcp_config(self.codex_home / "config.toml", **config_kwargs)

        # Only codex-relevant values; never the operator's env wholesale.
        environment: dict[str, str] = {
            "HOME": "/home/agent",
            "CODEX_HOME": CODEX_CONTAINER_CODEX_HOME,
        }
        if gateway:
            environment["OPENAI_API_KEY"] = self.agent_cfg.runtime.api_key
        else:
            auth_mode = sync_host_codex_auth_view(host_home, self.codex_home)
            if auth_mode == "no-host-file":
                raise RuntimeError(
                    f"agent {self.agent_id!r}: codex needs auth; run "
                    "`codex login` on the host or configure "
                    "runtime.llm_base_url and runtime.api_key"
                )
            logger.info(
                "agent %s: refreshed Codex credential view (%s)",
                self.agent_id,
                auth_mode,
            )
        skill_count = sync_host_codex_skills(host_home, self.codex_home)
        if skill_count:
            logger.info(
                "agent %s: synced %d host codex skill(s) into %s",
                self.agent_id,
                skill_count,
                self.codex_home,
            )

        from ....portal.control.context_telemetry import configured_compact_pct

        compact_pct = configured_compact_pct("codex", self.agent_cfg.env_overrides)
        return RuntimeSpec(
            workspace_dir="/workspace",
            model=self.model,
            inference_level=self.agent_cfg.runtime.inference_level,
            system_prompt=system_prompt,
            environment=environment,
            permission_mode=self.permission_mode,
            sandbox=CONTAINER_SANDBOX,
            task_timeout_seconds=self.agent_cfg.runtime.task_timeout_seconds,
            auto_compact_threshold_pct=compact_pct,
        )

    def _legacy_session_path(self) -> Path:
        if self.harness_name == HARNESS_CLAUDE_CODE:
            return cli_session_json_path(self.agent_id)
        return self.codex_home / "codex_session.json"

    def _load_legacy_session_id(self, path: Path) -> str:
        document = _read_json_object(path)
        if self.harness_name == HARNESS_CLAUDE_CODE:
            persisted_model = str(document.get("model") or "").strip()
            if self.model and persisted_model and persisted_model != self.model:
                logger.info(
                    "agent %s: not importing legacy Docker Claude session "
                    "because the model changed",
                    self.agent_id,
                )
                return ""
            return str(document.get("session_id") or "").strip()
        persisted_sandbox = str(document.get("sandbox") or "danger-full-access")
        if persisted_sandbox != CONTAINER_SANDBOX:
            logger.info(
                "agent %s: not importing legacy Docker Codex session because "
                "sandbox changed from %s to %s",
                self.agent_id,
                persisted_sandbox,
                CONTAINER_SANDBOX,
            )
            return ""
        return str(document.get("conversation_id") or "").strip()

    # ── Container lifecycle ────────────────────────────────────────────

    async def ensure_container(self) -> None:
        """Start and validate the selected harness container."""
        self._require_docker()
        state = await container_state(self._docker_bin, self.container_name)
        if state is None:
            raise RuntimeError(
                f"could not inspect Docker container {self.container_name!r}; "
                "refusing to create or replace it while Docker is unavailable"
            )
        if state != "" and not self._layout_is_current():
            logger.warning(
                "agent %s: recreating Docker container %r for layout %s",
                self.agent_id,
                self.container_name,
                self._layout_marker_value(),
            )
            await self._remove_existing_container(state)
            state = ""
        await self._activate_container(state)
        package_ok, harness_ok = await self._probe_container()
        if package_ok is None or harness_ok is None:
            raise RuntimeError(
                f"could not validate Docker container {self.container_name!r}; "
                "refusing to replace it after a failed probe"
            )
        if not package_ok or not harness_ok:
            logger.warning(
                "agent %s: recreating stale Docker container %r "
                "(package=%s harness=%s)",
                self.agent_id,
                self.container_name,
                package_ok,
                harness_ok,
            )
            await self._remove_existing_container("running")
            await self._activate_container("")
            package_ok, harness_ok = await self._probe_container()
        if package_ok is not True or harness_ok is not True:
            raise RuntimeError(
                f"docker image {self.image!r} does not provide the Puffo MCP "
                f"package and {self.harness_name!r} harness"
            )
        self._write_layout_marker()

    async def _activate_container(self, state: str) -> None:
        if state == "running":
            logger.info(
                "agent %s: reusing running Docker container %r",
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
            await run_cmd([self._docker_bin, "start", self.container_name])
        elif state == "paused":
            logger.info(
                "agent %s: unpausing container %r",
                self.agent_id,
                self.container_name,
            )
            await run_cmd([self._docker_bin, "unpause", self.container_name])
        elif state == "":
            await ensure_docker_image(
                self._docker_bin, self.image, agent_id=self.agent_id
            )
            await self._start_container()
        else:
            raise RuntimeError(
                f"Docker container {self.container_name!r} is in transient "
                f"state {state!r}; refusing to replace it"
            )
        self._container_stopped = False

    async def _remove_existing_container(self, state: str) -> None:
        if state in {"running", "paused"}:
            if state == "paused":
                await run_cmd([self._docker_bin, "unpause", self.container_name])
            await run_cmd([self._docker_bin, "stop", "-t", "5", self.container_name])
        await run_cmd([self._docker_bin, "rm", self.container_name])

    async def _probe_container(self) -> tuple[bool | None, bool | None]:
        package = await self._probe_command(
            "test -f /opt/puffoagent-pkg/puffo_agent/__init__.py"
        )
        harness = await self._probe_command(
            f"command -v {self._harness_executable()} >/dev/null"
        )
        return package, harness

    async def _probe_command(self, shell_command: str) -> bool | None:
        rc, _, _ = await run_cmd(
            [
                self._docker_bin,
                "exec",
                self.container_name,
                "sh",
                "-c",
                f"{shell_command} && exit 0 || exit 42",
            ],
            check=False,
        )
        return probe_result(rc)

    def _harness_executable(self) -> str:
        return "codex" if self.harness_name == HARNESS_CODEX else "claude"

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

    def _layout_marker_value(self) -> str:
        return f"{CONTAINER_LAYOUT_VERSION}:{self.harness_name}"

    def _layout_is_current(self) -> bool:
        marker = self.agent_home / _DOCKER_LAYOUT_MARKER
        try:
            return (
                marker.read_text(encoding="utf-8").strip()
                == self._layout_marker_value()
            )
        except OSError:
            return False

    def _write_layout_marker(self) -> None:
        marker = self.agent_home / _DOCKER_LAYOUT_MARKER
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(self._layout_marker_value() + "\n", encoding="utf-8")
        except OSError:
            pass

    async def _start_container(self) -> None:
        shared_status = prepare_workspace_shared_access(
            self.workspace_dir,
            self.shared_fs_dir,
            mounted=True,
        )
        self.agent_home.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        harness_mounts = self._prepare_harness_mounts()
        command = [
            self._docker_bin,
            "run",
            "-d",
            "--name",
            self.container_name,
            "-e",
            f"PUFFO_AGENT_ID={self.agent_id}",
            "-v",
            f"{self.workspace_dir}:/workspace",
            *harness_mounts,
            # Per-agent keystore + memory for the Puffo MCP.
            "-v",
            f"{self.agent_home}:{CONTAINER_STATE_DIR}",
            *(
                ["-v", f"{self.shared_fs_dir}:/workspace/shared"]
                if shared_status == "mounted"
                else []
            ),
            # Compatibility for existing sessions and user-authored paths.
            "-v",
            f"{self.shared_fs_dir}:/workspace/.shared",
            "-v",
            f"{puffo_agent_pkg_dir()}:/opt/puffoagent-pkg:ro",
        ]
        default_memory = self.agent_home / "memory"
        if self.memory_dir.resolve() != default_memory.resolve():
            command.extend(
                [
                    "-v",
                    f"{self.memory_dir}:{CONTAINER_STATE_DIR}/memory",
                ]
            )
        command.append("--init")
        if self.memory_limit:
            command.extend(["--memory", self.memory_limit])
        if self.memory_reservation:
            command.extend(["--memory-reservation", self.memory_reservation])
        command.append(self.image)
        rc, _, stderr = await run_cmd(command, check=False)
        if rc != 0:
            raise RuntimeError(
                f"docker run failed for {self.container_name}: "
                f"{stderr.decode('utf-8', errors='replace').strip()[:500]}"
            )

    def _prepare_harness_mounts(self) -> list[str]:
        if self.harness_name == HARNESS_CODEX:
            self.codex_home.mkdir(parents=True, exist_ok=True)
            return [
                "-v",
                f"{self.codex_home}:{CODEX_CONTAINER_CODEX_HOME}",
            ]
        claude_home = self.agent_home / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        claude_json = self.agent_home / ".claude.json"
        claude_json.touch(exist_ok=True)
        mounts = [
            "-v",
            f"{claude_home}:{CLAUDE_CONTAINER_HOME}",
            "-v",
            f"{claude_json}:/home/agent/.claude.json",
        ]
        host_plugins = Path.home() / ".claude" / "plugins"
        if host_plugins.is_dir():
            mounts.extend(["-v", f"{host_plugins}:{CLAUDE_CONTAINER_HOME}/plugins:ro"])
        return mounts

    # ── Driver transport ───────────────────────────────────────────────

    def _docker_exec_prefix(
        self, spec: RuntimeSpec
    ) -> tuple[list[str], dict[str, str]]:
        """Build an explicit container environment without argv secrets."""
        command = [self._docker_bin, "exec", "-i"]
        child_env = dict(os.environ)
        for key, value in spec.environment.items():
            if key in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}:
                command.extend(["-e", key])
                if value:
                    child_env[key] = value
                else:
                    child_env.pop(key, None)
            else:
                command.extend(["-e", f"{key}={value}"])
        command.extend(["-w", spec.workspace_dir or "/workspace"])
        return command, child_env

    async def _spawn_exec(
        self,
        command: list[str],
        child_env: dict[str, str],
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            # One provider frame can carry a large tool result; the default
            # 64 KiB stream limit would terminate the reader mid-session.
            limit=16 * 1024 * 1024,
            **no_window_kwargs(),
        )

    async def _exec_codex_process(
        self, spec: RuntimeSpec
    ) -> asyncio.subprocess.Process:
        command, child_env = self._docker_exec_prefix(spec)
        command.extend([self.container_name, "codex", "app-server"])
        return await self._spawn_exec(command, child_env)

    async def _exec_claude_process(
        self,
        args: list[str],
        spec: RuntimeSpec,
    ) -> asyncio.subprocess.Process:
        command, child_env = self._docker_exec_prefix(spec)
        command.extend([self.container_name, *args])
        return await self._spawn_exec(command, child_env)

    @property
    def process_factory(self) -> Callable[..., Any]:
        """Return the selected Driver's ``docker exec -i`` transport."""
        if self.harness_name == HARNESS_CLAUDE_CODE:
            return self._exec_claude_process
        return self._exec_codex_process

    # ── Bounded shutdown ───────────────────────────────────────────────

    async def aclose(self) -> None:
        """Stop the container once the Driver transport has terminated."""
        if self._container_stopped:
            return
        self._container_stopped = True
        # ``docker stop`` (not ``rm -f``) preserves the container's fs —
        # codex home, sessions, config — so the next start resumes cleanly.
        # ``-t 5`` bounds the SIGTERM grace inside Worker.stop's 30s budget.
        await run_cmd(
            [self._docker_bin, "stop", "-t", "5", self.container_name],
            check=False,
        )


__all__ = [
    "DockerRuntimePreparer",
    "CLAUDE_CONTAINER_HOME",
    "CODEX_CONTAINER_CODEX_HOME",
    "CONTAINER_STATE_DIR",
    "CONTAINER_SANDBOX",
]
