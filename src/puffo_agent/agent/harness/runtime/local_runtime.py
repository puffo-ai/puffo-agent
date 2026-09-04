"""Prepare host-local Codex and Claude Code Driver runtimes.

This module owns configuration, credentials, host asset sync, and the
one-time import of session identifiers written by the pre-Driver Puffo Agent.
It deliberately does not execute turns or speak either provider protocol;
those responsibilities belong to :mod:`codex_driver`,
:mod:`claude_code_driver`, and :mod:`runtime_manager`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ....macos.keychain import is_macos
from ....mcp.config import (
    INFERENCE_LEVELS,
    default_python_executable,
    puffo_core_mcp_env,
    write_cli_mcp_config,
    write_codex_mcp_config,
)
from ....portal.state import (
    AgentConfig,
    DaemonConfig,
    agent_claude_user_dir,
    agent_codex_user_dir,
    agent_dir,
    agent_home_dir,
    claude_cli_api_key,
    cli_session_json_path,
    read_host_codex_mcp_servers,
    seed_claude_home,
    shared_fs_dir,
    sync_host_claude_code_auth_view,
    sync_host_codex_auth_view,
    sync_host_enabled_plugins,
    sync_host_mcp_servers,
    sync_host_pi_auth_view,
    sync_host_plugins,
    sync_host_skills,
    strip_claude_api_key_from_settings,
)
from ....portal.workspace_layout import (
    AVAILABLE_SHARED_WORKSPACE_STATES,
    ensure_workspace_shared_link,
)
from ....portal.runtime_matrix import (
    resolve_effective_harness,
    resolve_effective_provider,
)
from ...adapters.base import (
    STATUS_PREVIEW_CHARS,
    anthropic_base_url_env,
    is_silent,
)
from ...adapters.desired_install import run_spawn_install
from ...cli_bin import (
    resolve_claude_bin,
    resolve_codex_bin,
    resolve_opencode_bin,
    resolve_pi_bin,
)
from ...runtime_event_outbox import (
    RuntimeEventOutbox,
    RuntimeEventProjectingSink,
)
from ...runtime_events import RuntimeEventProjector, TrustedScope
from .. import SUPPORTED_LOCAL_DRIVERS, UnsupportedDriver, build_driver
from ..support.child_env import build_child_environment
from ..drivers.pi_bridge import (
    build_bridge_environment,
    install_pi_tool_bridge,
    mint_bridge_nonce,
    ready_file_path,
)
from ..driver import (
    Driver,
    HarnessEvent,
    HarnessEventType,
    McpServerSpec,
    RuntimeSpec,
    SessionRef,
)
from .runtime_manager import RuntimeManager, RuntimeManagerAdapter
from ....tasks import spawn

logger = logging.getLogger(__name__)

VALID_PERMISSION_MODES = frozenset({"bypassPermissions"})
VALID_SANDBOX_MODES = frozenset({
    "read-only",
    "workspace-write",
    "danger-full-access",
})

_LEGACY_HOOK_COMMAND_MARKER = "puffo_agent.hooks.permission"


def _sanitise_permission_mode(mode: str, agent_id: str) -> str:
    if mode in VALID_PERMISSION_MODES:
        return mode
    if mode:
        logger.warning(
            "agent %s: permission_mode %r is not supported; using "
            "'bypassPermissions'",
            agent_id,
            mode,
        )
    return "bypassPermissions"


def _sanitise_sandbox(mode: str, agent_id: str) -> str:
    if mode in VALID_SANDBOX_MODES:
        return mode
    if mode:
        logger.warning(
            "agent %s: sandbox %r is invalid; using 'danger-full-access'",
            agent_id,
            mode,
        )
    return "danger-full-access"


def _is_legacy_permission_hook(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks") or []
    return any(
        isinstance(hook, dict)
        and _LEGACY_HOOK_COMMAND_MARKER in str(hook.get("command") or "")
        for hook in hooks
    )


def remove_legacy_permission_hook(claude_dir: Path) -> None:
    """Remove a hook written by an older local Adapter.

    Only ``bypassPermissions`` is supported by the Driver path, so retaining
    the old hook would unexpectedly reintroduce a second approval mechanism.
    User-authored hooks and every other settings key are preserved.
    """
    settings_path = claude_dir / "settings.json"
    try:
        document = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        logger.warning(
            "could not inspect %s while removing the legacy Puffo hook",
            settings_path,
        )
        return
    if not isinstance(document, dict):
        return
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return
    pre_tool = hooks.get("PreToolUse")
    if not isinstance(pre_tool, list):
        return
    retained = [
        entry for entry in pre_tool if not _is_legacy_permission_hook(entry)
    ]
    if len(retained) == len(pre_tool):
        return
    if retained:
        hooks["PreToolUse"] = retained
    else:
        hooks.pop("PreToolUse", None)
    if not hooks:
        document.pop("hooks", None)
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, indent=2), encoding="utf-8")
    os.replace(tmp, settings_path)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def build_codex_gateway_provider(
    *,
    model: str,
    llm_base_url: str,
    api_key: str,
) -> dict[str, str] | None:
    """Return the LiteLLM / OpenAI-compatible gateway provider block.

    ``None`` unless both a gateway URL and a virtual key are configured;
    when present codex talks to ``base_url`` via the Responses API using
    the bearer key in ``env_key`` instead of native ChatGPT OAuth.
    """
    if not (llm_base_url and api_key):
        return None
    base = llm_base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return {
        "name": "litellm",
        "display_name": "LiteLLM gateway",
        "base_url": base,
        "env_key": "OPENAI_API_KEY",
        "model": model or "codex",
        "wire_api": "responses",
    }


class RuntimePreparer(Protocol):
    """Minimal contract a runtime owner satisfies to be bound into the
    durable Runtime Manager by :func:`build_local_runtime_adapter`.

    Both host-local and Docker preparers implement it; the Docker owner
    additionally exposes ``process_factory`` and ``aclose`` that the
    composition boundary wires into the selected Driver.
    """

    agent_id: str

    async def refresh_spec(self, system_prompt: str) -> RuntimeSpec: ...


@dataclass(slots=True)
class PreparedLocalRuntime:
    harness_name: str
    spec: RuntimeSpec
    native_session_id: str
    migration_source: str
    legacy_session_path: Path
    preparer: RuntimePreparer

    def finalize_legacy_session_migration(self) -> None:
        """Retire the pre-Driver session sentinel after durable adoption."""
        try:
            self.legacy_session_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(
                "agent %s: could not retire legacy session file %s: %s",
                self.preparer.agent_id,
                self.legacy_session_path,
                exc,
            )
            return
        logger.info(
            "agent %s: migrated legacy %s session state into runtime_events.db",
            self.preparer.agent_id,
            self.harness_name,
        )


def select_native_session(
    *,
    harness_name: str,
    persisted_native_session_id: str,
    persisted_native_session_harness: str,
    legacy_native_session_id: str,
) -> tuple[str, str]:
    """Resume only native sessions created by the active harness."""
    if (
        persisted_native_session_id
        and persisted_native_session_harness == harness_name
    ):
        return persisted_native_session_id, "runtime_event_outbox"
    # Legacy session files predate generic Driver runtimes and belong only
    # to the two harnesses that originally wrote them.  In particular,
    # cli_session.json is a Claude Code file; feeding it to Pi/OpenCode/ACP
    # would reintroduce a stale cross-harness resume after the tagged outbox
    # correctly rejected the previous session.
    if legacy_native_session_id and harness_name in {"codex", "claude-code"}:
        return legacy_native_session_id, "legacy_session_file"
    if persisted_native_session_id:
        return "", "harness_changed"
    return "", "fresh"


class LocalRuntimePreparer:
    """Build refreshable RuntimeSpecs for one local Driver runtime."""

    def __init__(self, daemon_cfg: DaemonConfig, agent_cfg: AgentConfig):
        self.daemon_cfg = daemon_cfg
        self.agent_cfg = agent_cfg
        self.agent_id = agent_cfg.id
        provider = resolve_effective_provider(
            agent_cfg.runtime.kind or "cli-local",
            agent_cfg.runtime.provider,
        )
        self.harness_name = resolve_effective_harness(
            agent_cfg.runtime.kind or "cli-local",
            provider,
            agent_cfg.runtime.harness,
        ).strip()
        self.provider = provider
        if self.harness_name not in SUPPORTED_LOCAL_DRIVERS:
            supported = ", ".join(
                f"{name!r}" for name in sorted(SUPPORTED_LOCAL_DRIVERS)
            )
            raise RuntimeError(
                f"agent {self.agent_id!r}: runtime.kind='cli-local' supports "
                f"only harness in ({supported}) in the Driver runtime"
            )
        self.workspace_dir = agent_cfg.resolve_workspace_dir()
        self.claude_dir = agent_cfg.resolve_claude_dir()
        self.agent_home = agent_home_dir(self.agent_id)
        self.permission_mode = _sanitise_permission_mode(
            agent_cfg.runtime.permission_mode,
            self.agent_id,
        )
        self.sandbox = _sanitise_sandbox(
            agent_cfg.runtime.sandbox,
            self.agent_id,
        )
        self.model = self._resolve_model()
        self._desired_installed = False
        self._desired_codex_extras: dict[str, dict] = {}
        self._puffo_core_env = self._build_puffo_core_env()

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
        return PreparedLocalRuntime(
            harness_name=self.harness_name,
            spec=spec,
            native_session_id=native_session_id,
            migration_source=source,
            legacy_session_path=legacy_path,
            preparer=self,
        )

    async def refresh_spec(self, system_prompt: str) -> RuntimeSpec:
        shared_status = ensure_workspace_shared_link(
            self.workspace_dir,
            shared_fs_dir(),
        )
        if shared_status not in AVAILABLE_SHARED_WORKSPACE_STATES:
            logger.error(
                "agent %s: shared workspace is %s; cross-Agent file handoffs "
                "are unavailable",
                self.agent_id,
                shared_status,
            )
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        if self.harness_name == "claude-code":
            self._sync_claude_host_state()
        await self._install_desired_once()
        if self.harness_name == "codex":
            return self._prepare_codex_spec(system_prompt)
        if self.harness_name == "claude-code":
            return self._prepare_claude_spec(system_prompt)
        return self._prepare_generic_spec(system_prompt)

    def _resolve_model(self) -> str:
        runtime = self.agent_cfg.runtime
        if self.harness_name == "codex":
            return runtime.model or self.daemon_cfg.openai.model or ""
        if self.harness_name == "claude-code":
            return runtime.model or self.daemon_cfg.anthropic.model or ""
        provider_cfg = getattr(self.daemon_cfg, self.provider, None)
        return runtime.model or getattr(provider_cfg, "model", "") or ""

    def _prepare_generic_spec(self, system_prompt: str) -> RuntimeSpec:
        executable, launch_args = self._resolve_generic_command()
        controlled, opencode_config = self._prepare_executable_configuration(
            executable, system_prompt
        )
        mcp_servers = self._project_protocol_mcp(
            controlled, opencode_config
        )
        if opencode_config:
            controlled["OPENCODE_CONFIG_CONTENT"] = json.dumps(
                opencode_config
            )
        return RuntimeSpec(
            workspace_dir=str(self.workspace_dir),
            model=self.model,
            system_prompt=system_prompt,
            executable=executable,
            launch_args=tuple(launch_args),
            environment=build_child_environment(
                overrides=self.agent_cfg.env_overrides,
                controlled=controlled,
            ),
            mcp_servers=mcp_servers,
            permission_mode=self.permission_mode,
            sandbox=self.sandbox,
            task_timeout_seconds=self.agent_cfg.runtime.task_timeout_seconds,
        )

    def _resolve_generic_command(self) -> tuple[str, list[str]]:
        command = tuple(self.agent_cfg.runtime.harness_command)
        # Older web clients persisted the bare argv ["pi"]. Normalize that at
        # this compatibility boundary so both old configs and new commandless
        # presets use the daemon's broad PATH / PUFFO_PI_BIN resolver.
        if self.harness_name == "pi" and command == ("pi",):
            command = ()
        if command:
            executable, *launch_args = command
        elif self.harness_name in {"opencode", "pi"}:
            resolver = (
                resolve_opencode_bin
                if self.harness_name == "opencode"
                else resolve_pi_bin
            )
            executable = resolver() or ""
            launch_args = []
            if not executable:
                raise RuntimeError(
                    f"{self.harness_name} binary not found. Install "
                    f"{self.harness_name} or set "
                    f"PUFFO_{self.harness_name.upper()}_BIN=/absolute/path/"
                    f"to/{self.harness_name}."
                )
        else:
            raise RuntimeError(
                f"agent {self.agent_id!r}: harness='acp' requires "
                "runtime.harness_command, for example "
                "['opencode', 'acp']"
            )
        if (
            self.harness_name == "pi"
            and "/" not in self.model
            and "--provider" not in launch_args
        ):
            launch_args.extend(("--provider", self.provider))
        inference_level = self.agent_cfg.runtime.inference_level
        if (
            self.harness_name == "pi"
            and inference_level
            and "--thinking" not in launch_args
        ):
            launch_args.extend(("--thinking", inference_level))
        return executable, launch_args

    def _prepare_executable_configuration(
        self, executable: str, system_prompt: str
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """Project configuration selected by executable family.

        ACP-over-OpenCode intentionally receives OpenCode's instructions file;
        this axis is independent of which Driver protocol owns the process.
        """
        controlled: dict[str, str] = {}
        uses_opencode_executable = Path(executable).name.lower() in {
            "opencode",
            "opencode.exe",
        }
        opencode_config: dict[str, Any] = {}
        if uses_opencode_executable:
            instruction_path = (
                agent_dir(self.agent_id) / "opencode-instructions.md"
            )
            instruction_path.parent.mkdir(parents=True, exist_ok=True)
            instruction_path.write_text(system_prompt, encoding="utf-8")
            opencode_config["instructions"] = [str(instruction_path)]
        return controlled, opencode_config

    def _project_protocol_mcp(
        self,
        controlled: dict[str, str],
        opencode_config: dict[str, Any],
    ) -> tuple[McpServerSpec, ...]:
        """Project Puffo tools according to the selected Driver protocol.

        Native OpenCode receives its inline MCP configuration and must not
        also receive the generic projection.

        The ACP Driver does not honour ``RuntimeSpec.mcp_servers``: it seals
        an empty list into every launch plan, because ``puffo-v0`` rejects a
        non-empty ``mcpServers`` at ``session/new``. So an ACP runtime opens
        a session but gets no Puffo tools, and the server built here is
        dropped downstream. Carrying it to an ACP agent needs an audited
        profile and a fixed bridge, not a forward from this tuple. See
        ``test_spec_mcp_servers_never_reach_the_acp_launch_plan``.
        """
        mcp_servers: tuple[McpServerSpec, ...] = ()
        if self._puffo_core_env:
            puffo_command = default_python_executable()
            puffo_args = ("-m", "puffo_agent.mcp.puffo_core_server")
            puffo_server = McpServerSpec(
                name="puffo",
                command=puffo_command,
                args=puffo_args,
                environment=self._puffo_core_env,
            )
            if self.harness_name == "pi":
                # Pi has no MCP client. Its attested extension bridge carries
                # only Puffo's core server; keeping mcp_servers empty is part
                # of the Driver admission contract.
                controlled.update(self._prepare_pi_bridge(puffo_server))
            else:
                mcp_servers = (puffo_server,)
            if self.harness_name == "opencode":
                opencode_config["mcp"] = {
                    "puffo": {
                        "type": "local",
                        "command": [puffo_command, *puffo_args],
                        "environment": self._puffo_core_env,
                    }
                }
        else:
            logger.warning(
                "agent %s: Puffo MCP tools are unavailable because "
                "puffo_core is incomplete",
                self.agent_id,
            )
        return mcp_servers

    def _prepare_pi_bridge(self, mcp: McpServerSpec) -> dict[str, str]:
        """Install the bridge and mint fresh per-spec readiness evidence."""
        pi_home = agent_dir(self.agent_id) / ".pi" / "agent"
        install_pi_tool_bridge(pi_home)
        auth_mode = sync_host_pi_auth_view(Path.home(), pi_home)
        logger.info(
            "agent %s: Pi credential projection mode=%s",
            self.agent_id,
            auth_mode,
        )
        controlled = {"PI_CODING_AGENT_DIR": str(pi_home)}
        controlled.update(
            build_bridge_environment(
                mcp=mcp,
                ready_file=ready_file_path(pi_home),
                nonce=mint_bridge_nonce(),
            )
        )
        return controlled

    def _build_puffo_core_env(self) -> dict[str, str] | None:
        pc = self.agent_cfg.puffo_core
        if not pc.is_configured():
            return None
        return puffo_core_mcp_env(
            slug=pc.slug,
            device_id=pc.device_id,
            server_url=pc.server_url,
            space_id=pc.space_id,
            keystore_dir=str(agent_dir(self.agent_id) / "keys"),
            workspace=str(self.workspace_dir),
            shared_workspace=str(shared_fs_dir()),
            agent_id=self.agent_id,
            data_service_url=(
                f"http://127.0.0.1:{self.daemon_cfg.data_service.port}"
            ),
            rpc_url=f"http://127.0.0.1:{self.daemon_cfg.rpc_service.port}",
            runtime_kind="cli-local",
            harness=self.harness_name,
            memory_dir=str(self.agent_cfg.resolve_memory_dir()),
            transport=pc.transport,
        )

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
        )
        if extras:
            self._desired_codex_extras = extras

    def _sync_claude_host_state(self) -> None:
        executable = resolve_claude_bin()
        if executable is None:
            raise RuntimeError(
                "claude binary not found. Install Claude Code or set "
                "PUFFO_CLAUDE_BIN=/absolute/path/to/claude."
            )
        host_home = Path.home()
        self.agent_home.mkdir(parents=True, exist_ok=True)
        if seed_claude_home(host_home, self.agent_home):
            logger.info(
                "agent %s: seeded virtual HOME at %s",
                self.agent_id,
                self.agent_home,
            )
        auth_mode = sync_host_claude_code_auth_view(
            host_home,
            self.agent_home,
        )
        logger.info(
            "agent %s: refreshed Claude credential view (%s)",
            self.agent_id,
            auth_mode,
        )
        sync_host_skills(host_home, self.agent_home)
        sync_host_mcp_servers(host_home, self.agent_home)
        sync_host_plugins(host_home, self.agent_home)
        sync_host_enabled_plugins(host_home, self.agent_home)
        self._strip_claude_api_key_settings()

    def _strip_claude_api_key_settings(self) -> None:
        roots = [self.agent_home / ".claude"]
        claude_dir = getattr(self, "claude_dir", None)
        if claude_dir is not None and claude_dir not in roots:
            roots.append(claude_dir)
        for settings_path in (
            path
            for root in roots
            for path in (root / "settings.json", root / "settings.local.json")
        ):
            strip_claude_api_key_from_settings(settings_path)

    def _prepare_claude_spec(self, system_prompt: str) -> RuntimeSpec:
        executable = resolve_claude_bin()
        if executable is None:
            raise RuntimeError(
                "claude binary not found. Install Claude Code or set "
                "PUFFO_CLAUDE_BIN=/absolute/path/to/claude."
            )
        launch_args = ["--dangerously-skip-permissions"]
        from ....portal.control.context_telemetry import (
            claude_autocompact_tokens,
            configured_compact_pct,
        )

        compact_pct = configured_compact_pct(
            "claude-code", self.agent_cfg.env_overrides
        )
        compact_tokens = claude_autocompact_tokens(
            model=self.model,
            pct=compact_pct,
        )
        if compact_tokens is not None:
            launch_args.extend(["--autocompact", str(compact_tokens)])
        inference = self.agent_cfg.runtime.inference_level
        if inference:
            if inference in INFERENCE_LEVELS:
                launch_args.extend(["--effort", inference])
            else:
                logger.warning(
                    "agent %s: ignoring unsupported Claude inference_level %r",
                    self.agent_id,
                    inference,
                )
        mcp_path = agent_dir(self.agent_id) / "mcp-config.json"
        if self._puffo_core_env:
            write_cli_mcp_config(
                mcp_path,
                command=default_python_executable(),
                args=["-m", "puffo_agent.mcp.puffo_core_server"],
                env=self._puffo_core_env,
            )
            launch_args.extend(["--mcp-config", str(mcp_path)])
        else:
            logger.warning(
                "agent %s: Puffo MCP tools are unavailable because "
                "puffo_core is incomplete",
                self.agent_id,
            )
        remove_legacy_permission_hook(self.claude_dir)
        runtime = self.agent_cfg.runtime
        llm_env = anthropic_base_url_env(runtime.llm_base_url)
        if llm_env and runtime.api_key:
            llm_env["ANTHROPIC_API_KEY"] = runtime.api_key
        else:
            configured_key = claude_cli_api_key(self.daemon_cfg)
            if configured_key:
                llm_env["ANTHROPIC_API_KEY"] = configured_key
        # Same guarantee the old pop-before-and-after-overrides dance gave,
        # now from an allowlist: ambient provider keys never reach the child,
        # an override cannot reintroduce one, and only llm_env injects the
        # controlled key.
        environment = build_child_environment(
            overrides=self.agent_cfg.env_overrides,
            controlled={
                "HOME": str(self.agent_home),
                "USERPROFILE": str(self.agent_home),
                **llm_env,
            },
        )
        if is_macos():
            environment["CLAUDE_CONFIG_DIR"] = str(
                agent_claude_user_dir(self.agent_id)
            )
        self._log_host_access()
        return RuntimeSpec(
            workspace_dir=str(self.workspace_dir),
            model=self.model,
            system_prompt=system_prompt,
            executable=executable,
            launch_args=tuple(launch_args),
            environment=environment,
            permission_mode=self.permission_mode,
            sandbox=self.sandbox,
            task_timeout_seconds=self.agent_cfg.runtime.task_timeout_seconds,
            auto_compact_threshold_pct=compact_pct,
            auto_compact_threshold_tokens=compact_tokens,
        )

    def _prepare_codex_spec(self, system_prompt: str) -> RuntimeSpec:
        codex_home = agent_codex_user_dir(self.agent_id)
        codex_home.mkdir(parents=True, exist_ok=True)
        agents_md = codex_home / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text("", encoding="utf-8")
        host_home = Path.home()
        host_mcps = read_host_codex_mcp_servers(host_home)
        extras = dict(self._desired_codex_extras)
        extras.update(host_mcps)
        gateway = self._codex_gateway_provider()
        config_kwargs: dict[str, Any] = {
            "extra_servers": extras,
            "inference_level": self.agent_cfg.runtime.inference_level,
            "provider": gateway,
        }
        if self._puffo_core_env:
            config_kwargs.update({
                "command": default_python_executable(),
                "args": ["-m", "puffo_agent.mcp.puffo_core_server"],
                "env": self._puffo_core_env,
            })
        write_codex_mcp_config(codex_home / "config.toml", **config_kwargs)

        # Controlled injection only: an ambient OPENAI_API_KEY must not reach
        # the child. Native OAuth uses the CODEX_HOME auth view instead, and
        # the gateway branch supplies its own key explicitly.
        controlled: dict[str, str] = {"CODEX_HOME": str(codex_home)}
        if gateway:
            controlled["OPENAI_API_KEY"] = self.agent_cfg.runtime.api_key
        else:
            auth_mode = sync_host_codex_auth_view(host_home, codex_home)
            if auth_mode == "no-host-file":
                raise RuntimeError(
                    f"agent {self.agent_id!r}: codex needs auth; run "
                    "`codex login` or configure runtime.llm_base_url and "
                    "runtime.api_key"
                )
            logger.info(
                "agent %s: refreshed Codex credential view (%s)",
                self.agent_id,
                auth_mode,
            )
        executable = resolve_codex_bin()
        if executable is None:
            raise RuntimeError(
                "codex binary not found. Install @openai/codex or set "
                "PUFFO_CODEX_BIN=/absolute/path/to/codex."
            )
        self._ensure_codex_self_invoke(executable)
        environment = build_child_environment(
            overrides=self.agent_cfg.env_overrides,
            controlled=controlled,
            extra_allowed=("CODEX_HOME",),
        )
        environment["PATH"] = self._prepend_executable_path(
            environment.get("PATH", ""),
            executable,
        )
        self._log_host_access()
        from ....portal.control.context_telemetry import configured_compact_pct

        compact_pct = configured_compact_pct(
            "codex", self.agent_cfg.env_overrides
        )
        return RuntimeSpec(
            workspace_dir=str(self.workspace_dir),
            model=self.model,
            system_prompt=system_prompt,
            executable=executable,
            environment=environment,
            permission_mode=self.permission_mode,
            sandbox=self.sandbox,
            task_timeout_seconds=self.agent_cfg.runtime.task_timeout_seconds,
            auto_compact_threshold_pct=compact_pct,
        )

    def _codex_gateway_provider(self) -> dict[str, str] | None:
        return build_codex_gateway_provider(
            model=self.model,
            llm_base_url=self.agent_cfg.runtime.llm_base_url,
            api_key=self.agent_cfg.runtime.api_key,
        )

    def _ensure_codex_self_invoke(self, executable: str) -> None:
        if not is_macos():
            return
        hardcoded = Path.home() / ".local" / "bin" / "codex"
        if hardcoded.exists():
            return
        try:
            hardcoded.parent.mkdir(parents=True, exist_ok=True)
            if hardcoded.is_symlink():
                hardcoded.unlink()
            hardcoded.symlink_to(executable)
        except OSError as exc:
            logger.warning(
                "agent %s: could not create %s for Codex self-invoke: %s",
                self.agent_id,
                hardcoded,
                exc,
            )

    @staticmethod
    def _prepend_executable_path(existing: str, executable: str) -> str:
        directory = str(Path(executable).parent)
        normalized = {
            os.path.normcase(os.path.normpath(value))
            for value in existing.split(os.pathsep)
            if value
        }
        if os.path.normcase(os.path.normpath(directory)) in normalized:
            return existing
        return directory + (os.pathsep + existing if existing else "")

    def _legacy_session_path(self) -> Path:
        if self.harness_name == "codex":
            return agent_codex_user_dir(self.agent_id) / "codex_session.json"
        return cli_session_json_path(self.agent_id)

    def _load_legacy_session_id(self, path: Path) -> str:
        document = _read_json_object(path)
        if self.harness_name == "codex":
            persisted_sandbox = str(
                document.get("sandbox") or "danger-full-access"
            )
            if persisted_sandbox != self.sandbox:
                logger.info(
                    "agent %s: not importing legacy Codex session because "
                    "sandbox changed from %s to %s",
                    self.agent_id,
                    persisted_sandbox,
                    self.sandbox,
                )
                return ""
            return str(document.get("conversation_id") or "").strip()
        persisted_model = str(document.get("model") or "").strip()
        if self.model and persisted_model and persisted_model != self.model:
            logger.info(
                "agent %s: not importing legacy Claude session because "
                "model changed",
                self.agent_id,
            )
            return ""
        return str(document.get("session_id") or "").strip()

    def _log_host_access(self) -> None:
        logger.warning(
            "agent %s: cli-local runs with the operator's filesystem and "
            "network access (permission_mode=%s)",
            self.agent_id,
            self.permission_mode,
        )


def _event_kind(event: HarnessEvent) -> str:
    return (
        event.type.value
        if isinstance(event.type, HarnessEventType)
        else str(event.type)
    )


def _normalized_tool_label(label: str) -> str:
    """Normalize an MCP-scoped tool label to its bare name for status."""
    if label.startswith("mcp__") and "__" in label:
        return label.rsplit("__", 1)[-1]
    return label


def _emit_status(agent_id: str, event: str, payload: dict[str, Any]) -> None:
    from ....portal.control.reporter import get_reporter

    spawn(get_reporter().emit(agent_id, event, payload), name="reporter.emit")


class _LegacyStatusProjector:
    """Turn-scoped pre-2.0 status projection from normalized Driver events.

    Emits only the safe legacy surface: one bounded ``assistant_text`` per
    completed assistant block unless the accumulated text is silent, and one
    label-only ``tool_use`` per normalized tool start. It never reads the
    native diagnostic payload, tool arguments or results, reasoning events, or
    unknown provider frames; duplicate lifecycle events and post-terminal
    fragments are ignored, and buffers are discarded at terminal, abandonment,
    or runtime teardown.
    """

    def __init__(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._turn: str | None = None
        self._block_buffers: dict[str, str] = {}
        self._emitted_blocks: set[str] = set()
        self._emitted_tools: set[str] = set()

    def project(self, event: HarnessEvent) -> None:
        kind = _event_kind(event)
        turn = str(event.turn_ref.value) if event.turn_ref is not None else ""
        if kind == "turn.started":
            self._turn = turn
            self._reset()
            return
        if kind in {"turn.completed", "turn.abandoned", "runtime.exited"}:
            self._turn = None
            self._reset()
            return
        if not turn or self._turn != turn:
            return
        data = event.data
        if kind == "turn.assistant_delta":
            text = data.get("text")
            if isinstance(text, str):
                block_id = str(data.get("block_id") or "")
                buffer = self._block_buffers.get(block_id, "")
                room = STATUS_PREVIEW_CHARS - len(buffer)
                if room > 0:
                    self._block_buffers[block_id] = buffer + text[:room]
            return
        if kind == "turn.assistant_completed":
            block_id = str(data.get("block_id") or "")
            if block_id in self._emitted_blocks:
                return
            self._emitted_blocks.add(block_id)
            text = self._block_buffers.pop(block_id, "")
            if text and not is_silent(text):
                _emit_status(self._agent_id, "assistant_text", {"text": text})
            return
        if kind == "turn.tool_started":
            ref = str(data.get("tool_call_ref") or "")
            if ref in self._emitted_tools:
                return
            self._emitted_tools.add(ref)
            label = _normalized_tool_label(str(data.get("label") or ""))
            if label:
                _emit_status(self._agent_id, "tool_use", {"tool": label})

    def _reset(self) -> None:
        self._block_buffers.clear()
        self._emitted_blocks.clear()
        self._emitted_tools.clear()


def build_local_runtime_adapter(
    prepared: PreparedLocalRuntime,
    *,
    outbox: RuntimeEventOutbox,
    logical_session_ref: str,
    driver: Driver | None = None,
    cleanup: Callable[[], Awaitable[None]] | None = None,
) -> RuntimeManagerAdapter:
    """Bind a prepared Driver runtime to the durable Runtime Manager.

    ``driver`` defaults to the ratified Driver for ``prepared.harness_name``;
    Docker composition injects the selected Driver with its exec transport
    factory and passes ``cleanup`` (bounded container stop), which runs after
    the manager closes.
    """
    if driver is None:
        driver = build_driver(prepared.harness_name)
    if isinstance(driver, UnsupportedDriver):
        raise RuntimeError(driver.diagnostic)
    projector = RuntimeEventProjector(
        agent_id=prepared.preparer.agent_id,
        session_ref=logical_session_ref,
        scope=TrustedScope(),
    )
    projecting_sink = RuntimeEventProjectingSink(outbox, projector)
    legacy_projector = _LegacyStatusProjector(prepared.preparer.agent_id)
    manager: RuntimeManager

    async def persist_event(event: HarnessEvent) -> None:
        try:
            legacy_projector.project(event)
        except Exception:
            logger.exception(
                "agent %s: Profile Log projection failed; runtime continues",
                prepared.preparer.agent_id,
            )
        logical_session = str(event.session_ref or manager.session_ref)
        projector.session_ref = logical_session
        try:
            await projecting_sink(event)
        except Exception as exc:
            logger.warning(
                "agent %s: Runtime Events observation failed (%s); "
                "runtime continues",
                prepared.preparer.agent_id,
                type(exc).__name__,
            )
        event_type = getattr(event.type, "value", event.type)
        # Only the session and turn boundaries rewrite durable state; every
        # other event (a streamed delta above all) must reach the outbox no
        # more than once, so the state read stays inside the branch using it.
        try:
            if event_type == "turn.started":
                active_turn = str(event.turn_ref)
            elif event_type in {"turn.completed", "turn.abandoned"}:
                active_turn = None
            elif event_type in {"session.opened", "session.resumed"}:
                active_turn = (await outbox.astate()).get("active_turn_ref") or None
            else:
                return
            await outbox.aset_active_turn(
                active_turn,
                session_ref=logical_session,
                native_session_id=manager.native_session_id,
                native_session_harness=prepared.harness_name,
            )
        except Exception as exc:
            logger.warning(
                "agent %s: Runtime session snapshot failed (%s); "
                "runtime continues",
                prepared.preparer.agent_id,
                type(exc).__name__,
            )

    async def reload_spec(system_prompt: str) -> RuntimeSpec:
        return await prepared.preparer.refresh_spec(system_prompt)

    manager = RuntimeManager(
        driver,
        prepared.spec,
        agent_id=prepared.preparer.agent_id,
        session_ref=SessionRef(logical_session_ref),
        native_session_id=prepared.native_session_id,
        driver_name=prepared.harness_name,
        event_sink=persist_event,
    )
    return RuntimeManagerAdapter(
        manager,
        spec_reloader=reload_spec,
        post_close=cleanup,
    )


__all__ = [
    "LocalRuntimePreparer",
    "PreparedLocalRuntime",
    "RuntimePreparer",
    "SUPPORTED_LOCAL_DRIVERS",
    "VALID_PERMISSION_MODES",
    "VALID_SANDBOX_MODES",
    "build_local_runtime_adapter",
    "build_codex_gateway_provider",
    "remove_legacy_permission_hook",
]
