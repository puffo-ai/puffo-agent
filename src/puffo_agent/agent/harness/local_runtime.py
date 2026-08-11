"""Prepare host-local Codex and Claude Code Driver runtimes.

This module owns configuration, credentials, host asset sync, and the
one-time import of session identifiers written by the pre-Driver Puffo Agent.
It deliberately does not execute turns or speak either provider protocol;
those responsibilities belong to :mod:`codex_driver`,
:mod:`claude_code_driver`, and :mod:`runtime_manager`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...macos.keychain import is_macos
from ...mcp.config import (
    INFERENCE_LEVELS,
    default_python_executable,
    puffo_core_mcp_env,
    write_cli_mcp_config,
    write_codex_mcp_config,
)
from ...portal.state import (
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
    sync_host_claude_code_auth_view,
    sync_host_codex_auth_view,
    sync_host_enabled_plugins,
    sync_host_mcp_servers,
    sync_host_plugins,
    sync_host_skills,
    strip_claude_api_key_from_settings,
)
from ...portal.runtime_matrix import (
    resolve_effective_harness,
    resolve_effective_provider,
)
from ..adapters.base import anthropic_base_url_env
from ..adapters.desired_install import run_spawn_install
from ..cli_bin import resolve_claude_bin, resolve_codex_bin
from ..runtime_event_outbox import (
    RuntimeEventOutbox,
    RuntimeEventProjectingSink,
)
from ..runtime_events import RuntimeEventProjector, TrustedScope
from ..shared_content import MEMORY_SECTION_HEADER
from . import UnsupportedDriver, build_driver
from .driver import HarnessEvent, RuntimeSpec, SessionRef, TurnRef
from .runtime_manager import RuntimeManager, RuntimeManagerAdapter

logger = logging.getLogger(__name__)

SUPPORTED_LOCAL_DRIVERS = frozenset({"codex", "claude-code"})
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


def _remove_legacy_permission_hook(claude_dir: Path) -> None:
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


@dataclass(slots=True)
class PreparedLocalRuntime:
    harness_name: str
    spec: RuntimeSpec
    native_session_id: str
    migration_source: str
    legacy_session_path: Path
    preparer: LocalRuntimePreparer
    session_fingerprint: str
    discarded_persisted_session: bool = False

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
        if self.harness_name not in SUPPORTED_LOCAL_DRIVERS:
            raise RuntimeError(
                f"agent {self.agent_id!r}: runtime.kind='cli-local' supports "
                "only harness='codex' or harness='claude-code' in the "
                "Driver runtime"
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
        persisted_session_fingerprint: str = "",
    ) -> PreparedLocalRuntime:
        spec = await self.refresh_spec(system_prompt)
        session_fingerprint = self.session_fingerprint(spec)
        legacy_path = self._legacy_session_path()
        legacy_id = self._load_legacy_session_id(legacy_path)
        discarded_persisted_session = bool(
            persisted_native_session_id
            and persisted_session_fingerprint != session_fingerprint
        )
        if persisted_native_session_id and not discarded_persisted_session:
            native_session_id = persisted_native_session_id
            source = "runtime_event_outbox"
        elif legacy_id:
            native_session_id = legacy_id
            source = "legacy_session_file"
        else:
            native_session_id = ""
            source = (
                "fresh_incompatible_persisted_session"
                if discarded_persisted_session
                else "fresh"
            )
        if discarded_persisted_session:
            logger.info(
                "agent %s: starting a fresh %s session because the durable "
                "session fingerprint is missing or incompatible",
                self.agent_id,
                self.harness_name,
            )
        return PreparedLocalRuntime(
            harness_name=self.harness_name,
            spec=spec,
            native_session_id=native_session_id,
            migration_source=source,
            legacy_session_path=legacy_path,
            preparer=self,
            session_fingerprint=session_fingerprint,
            discarded_persisted_session=discarded_persisted_session,
        )

    def session_fingerprint(self, spec: RuntimeSpec) -> str:
        """Fingerprint only inputs that make a native session incompatible."""
        prompt_core = spec.system_prompt.split(MEMORY_SECTION_HEADER, 1)[0]
        payload = {
            "version": 1,
            "harness": self.harness_name,
            "provider": resolve_effective_provider(
                self.agent_cfg.runtime.kind or "cli-local",
                self.agent_cfg.runtime.provider,
            ),
            "model": spec.model,
            "workspace": str(Path(spec.workspace_dir).resolve()),
            "sandbox": spec.sandbox,
            "permission_mode": spec.permission_mode,
            "inference_level": self.agent_cfg.runtime.inference_level,
            "llm_base_url": self.agent_cfg.runtime.llm_base_url.rstrip("/"),
            "prompt_core_sha256": hashlib.sha256(
                prompt_core.encode("utf-8")
            ).hexdigest(),
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return "v1:" + hashlib.sha256(encoded).hexdigest()

    async def refresh_spec(self, system_prompt: str) -> RuntimeSpec:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        if self.harness_name == "claude-code":
            self._sync_claude_host_state()
        await self._install_desired_once()
        if self.harness_name == "codex":
            return self._prepare_codex_spec(system_prompt)
        return self._prepare_claude_spec(system_prompt)

    def _resolve_model(self) -> str:
        runtime = self.agent_cfg.runtime
        if self.harness_name == "codex":
            return runtime.model or self.daemon_cfg.openai.model or ""
        return runtime.model or self.daemon_cfg.anthropic.model or ""

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
        from ...portal.control.context_telemetry import (
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
        _remove_legacy_permission_hook(self.claude_dir)
        runtime = self.agent_cfg.runtime
        llm_env = anthropic_base_url_env(runtime.llm_base_url)
        environment = dict(os.environ)
        environment.pop("ANTHROPIC_API_KEY", None)
        environment.update(self.agent_cfg.env_overrides)
        environment.pop("ANTHROPIC_API_KEY", None)
        if llm_env and runtime.api_key:
            llm_env["ANTHROPIC_API_KEY"] = runtime.api_key
        else:
            configured_key = claude_cli_api_key(self.daemon_cfg)
            if configured_key:
                llm_env["ANTHROPIC_API_KEY"] = configured_key
        environment.update({
            "HOME": str(self.agent_home),
            "USERPROFILE": str(self.agent_home),
            **llm_env,
        })
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

        environment = {
            **os.environ,
            **self.agent_cfg.env_overrides,
            "CODEX_HOME": str(codex_home),
        }
        if gateway:
            environment["OPENAI_API_KEY"] = self.agent_cfg.runtime.api_key
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
        environment["PATH"] = self._prepend_executable_path(
            environment.get("PATH", ""),
            executable,
        )
        self._log_host_access()
        from ...portal.control.context_telemetry import configured_compact_pct

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
        runtime = self.agent_cfg.runtime
        if not (runtime.llm_base_url and runtime.api_key):
            return None
        base = runtime.llm_base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        return {
            "name": "litellm",
            "display_name": "LiteLLM gateway",
            "base_url": base,
            "env_key": "OPENAI_API_KEY",
            "model": self.model or "codex",
            "wire_api": "responses",
        }

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


def build_local_runtime_adapter(
    prepared: PreparedLocalRuntime,
    *,
    outbox: RuntimeEventOutbox,
    logical_session_ref: str,
) -> RuntimeManagerAdapter:
    """Bind a prepared local Driver to the durable Runtime Manager."""
    driver = build_driver(prepared.harness_name)
    if isinstance(driver, UnsupportedDriver):
        raise RuntimeError(driver.diagnostic)
    projector = RuntimeEventProjector(
        agent_id=prepared.preparer.agent_id,
        session_ref=logical_session_ref,
        scope=TrustedScope(),
    )
    projecting_sink = RuntimeEventProjectingSink(outbox, projector)
    manager: RuntimeManager
    session_fingerprint = [prepared.session_fingerprint]

    async def require_initial_capacity() -> None:
        sizing_projector = RuntimeEventProjector(
            agent_id=prepared.preparer.agent_id,
            session_ref=projector.session_ref,
            scope=projector.scope,
        )
        sizing_start = HarnessEvent(
            type="turn.started",
            driver=prepared.harness_name,
            session_ref=SessionRef(projector.session_ref),
            turn_ref=TurnRef(f"turn_{'f' * 32}"),
            occurred_at="9999-12-31T23:59:59.999999+00:00",
        )
        estimated = sum(
            len(outbox.canonical_bytes(value))
            for value in sizing_projector.project_all(sizing_start)
        )
        # The durable usage read runs on the outbox worker thread: this hook is
        # awaited from the daemon loop, which must not block on SQLite.
        await outbox.arequire_turn_capacity(estimated_start_bytes=estimated)

    async def persist_event(event: HarnessEvent) -> None:
        logical_session = str(event.session_ref or manager.session_ref)
        projector.session_ref = logical_session
        await projecting_sink(event)
        event_type = getattr(event.type, "value", event.type)
        # Only the session and turn boundaries rewrite durable state; every
        # other event (a streamed delta above all) must reach the outbox no
        # more than once, so the state read stays inside the branch using it.
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
            session_fingerprint=session_fingerprint[0],
        )

    async def reload_spec(system_prompt: str) -> RuntimeSpec:
        spec = await prepared.preparer.refresh_spec(system_prompt)
        session_fingerprint[0] = prepared.preparer.session_fingerprint(spec)
        return spec

    manager = RuntimeManager(
        driver,
        prepared.spec,
        agent_id=prepared.preparer.agent_id,
        session_ref=SessionRef(logical_session_ref),
        native_session_id=prepared.native_session_id,
        driver_name=prepared.harness_name,
        event_sink=persist_event,
        before_start=require_initial_capacity,
    )
    return RuntimeManagerAdapter(
        manager,
        spec_reloader=reload_spec,
    )


__all__ = [
    "LocalRuntimePreparer",
    "PreparedLocalRuntime",
    "SUPPORTED_LOCAL_DRIVERS",
    "VALID_PERMISSION_MODES",
    "VALID_SANDBOX_MODES",
    "build_local_runtime_adapter",
]
