"""On-disk state layout for the multi-agent portal.

Home defaults to ``~/.puffo-agent/`` (override with ``PUFFO_AGENT_HOME``)::

    ~/.puffo-agent/
      daemon.yml          # model defaults, reserved provider settings, daemon settings
      daemon.pid          # daemon pid
      agents/
        <agent_id>/
          agent.yml       # puffo_core identity, runtime, triggers, state
          profile.md      # system-prompt profile
          memory/         # per-agent memory + token_usage.json
          keys/           # per-agent puffo-core keystore
          messages.db     # encrypted message store (sqlite)
      archived/
        <agent_id>-<ts>/

The CLI writes intent into these files; the daemon reconciler polls
the tree. No IPC port, no auth — the filesystem is the contract.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil
import yaml

from ..limits import DEFAULT_CATCHUP_STALE_HOURS


logger = logging.getLogger(__name__)


# Where daemon.yml, agents/, etc. live.
def home_dir() -> Path:
    override = os.environ.get("PUFFO_AGENT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".puffo-agent"


def agents_dir() -> Path:
    return home_dir() / "agents"


def archived_dir() -> Path:
    return home_dir() / "archived"


def docker_dir() -> Path:
    """Root for puffoagent-owned docker state (mcp scripts, shared
    primer, default-image build context)."""
    return home_dir() / "docker"


def docker_shared_dir() -> Path:
    """Shared primer + skill markdown folded into each agent's
    workspace/.claude/CLAUDE.md at worker startup."""
    return docker_dir() / "shared"


def agent_home_dir(agent_id: str) -> Path:
    """Per-agent virtual $HOME, used as ``HOME`` env for cli-local
    and bind-mounted to ``/home/agent`` for cli-docker.

    Claude Code reads user-level state from ``$HOME/.claude/``, so
    pointing HOME here isolates credentials, session transcripts, and
    history.jsonl per agent.
    """
    return agent_dir(agent_id)


def agent_claude_user_dir(agent_id: str) -> Path:
    """The agent's ``.claude/`` dir, seeded once from the operator's
    real ``~/.claude/`` so a one-time ``claude login`` carries over."""
    return agent_home_dir(agent_id) / ".claude"


def agent_codex_user_dir(agent_id: str) -> Path:
    """The agent's ``.codex/`` dir — used as ``CODEX_HOME`` so
    sessions, config.toml, and AGENTS.md are isolated per agent and
    don't collide with the operator's own ``~/.codex/``.
    """
    return agent_home_dir(agent_id) / ".codex"


def shared_fs_dir() -> Path:
    """Shared dir for cross-agent cooperation. Bind-mounted to
    ``/workspace/.shared`` for cli-docker; referenced by absolute path
    for cli-local agents."""
    return home_dir() / "shared"


# Host integration lives separately from persistent state models. Keep this
# module's public surface stable for existing daemon and third-party callers.
from . import host_assets as _host_assets

_CLAUDE_HOME_SEED_PATHS = _host_assets._CLAUDE_HOME_SEED_PATHS
HOST_SYNCED_MARKER = _host_assets.HOST_SYNCED_MARKER
AGENT_INSTALLED_MARKER = _host_assets.AGENT_INSTALLED_MARKER
seed_claude_home = _host_assets.seed_claude_home
sanitize_claude_code_auth_blob = _host_assets.sanitize_claude_code_auth_blob
sanitize_codex_auth_blob = _host_assets.sanitize_codex_auth_blob
read_host_codex_mcp_servers = _host_assets.read_host_codex_mcp_servers
_sync_host_skills_dir = _host_assets._sync_host_skills_dir
sync_host_skills = _host_assets.sync_host_skills
sync_host_gemini_skills = _host_assets.sync_host_gemini_skills
_looks_host_local_command = _host_assets._looks_host_local_command
sync_host_mcp_servers = _host_assets.sync_host_mcp_servers
sync_host_plugins = _host_assets.sync_host_plugins
sync_host_enabled_plugins = _host_assets.sync_host_enabled_plugins
sync_host_gemini_mcp_servers = _host_assets.sync_host_gemini_mcp_servers


def _sync_credentials_from_keychain(host_home: Path) -> bool:
    return _host_assets._sync_credentials_from_keychain(host_home)


def _is_current_user_home(path: Path) -> bool:
    return _host_assets._is_current_user_home(path)


def _write_credential_view(target: Path, blob: str) -> None:
    _host_assets._write_credential_view(target, blob)


def sync_host_claude_code_auth_view(host_home: Path, agent_home: Path) -> str:
    return _host_assets.sync_host_claude_code_auth_view(
        host_home,
        agent_home,
        write_view=_write_credential_view,
        sync_credentials=_sync_credentials_from_keychain,
        is_current_home=_is_current_user_home,
    )


def sync_host_codex_auth_view(host_home: Path, agent_codex_home: Path) -> str:
    return _host_assets.sync_host_codex_auth_view(
        host_home,
        agent_codex_home,
        write_view=_write_credential_view,
    )


def daemon_yml_path() -> Path:
    return home_dir() / "daemon.yml"


def daemon_pid_path() -> Path:
    return home_dir() / "daemon.pid"


def background_log_path() -> Path:
    """stdout/stderr sink for ``start --background`` — the detached
    tray+daemon child has no terminal to write to."""
    return home_dir() / "background.log"


def pairing_path() -> Path:
    """Single-pairing file holding (slug, device_id) + cached certs
    for the operator currently authorised to drive this daemon.
    Removed by ``puffo-agent pairing unpair``."""
    return home_dir() / "pairing.json"


def agent_dir(agent_id: str) -> Path:
    return agents_dir() / agent_id


def agent_yml_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "agent.yml"


def runtime_json_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "runtime.json"


def cli_session_json_path(agent_id: str) -> Path:
    """Persisted Claude Code session id for cli-local/cli-docker."""
    return agent_dir(agent_id) / "cli_session.json"


def archive_flag_path(agent_id: str) -> Path:
    """Sentinel dropped by the worker on server-side space deletion.
    Reconciler stops the worker and moves the dir to ``archived/``."""
    return agent_dir(agent_id) / ".puffo-agent" / "archive.flag"


def restart_flag_path(agent_id: str) -> Path:
    """Sentinel for operator-initiated Restart. Reconciler stops the
    worker (auto-respawned next tick) and removes the flag."""
    return agent_dir(agent_id) / ".puffo-agent" / "restart.flag"


def delete_flag_path(agent_id: str) -> Path:
    """Sentinel for operator-initiated Delete (destructive — no
    archived/ copy retained). Distinct from archive.flag."""
    return agent_dir(agent_id) / ".puffo-agent" / "delete.flag"


# Refresh flags — 5 axes touched by MCP refresh() / CLI / control-ws.
# All under ``<workspace>/.puffo-agent/`` so the location is reachable
# from both the worker and the MCP subprocess in cli-docker.


def refresh_agent_flag_path(workspace: Path) -> Path:
    return workspace / ".puffo-agent" / "refresh_agent.flag"


def refresh_host_sync_flag_path(workspace: Path) -> Path:
    return workspace / ".puffo-agent" / "refresh_host_sync.flag"


def refresh_session_flag_path(workspace: Path) -> Path:
    return workspace / ".puffo-agent" / "refresh_session.flag"


def refresh_model_flag_path(workspace: Path) -> Path:
    return workspace / ".puffo-agent" / "refresh_model.flag"


def refresh_runtime_flag_path(workspace: Path) -> Path:
    return workspace / ".puffo-agent" / "refresh_runtime.flag"


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ProviderConfig:
    api_key: str = ""
    model: str = ""


@dataclass
class DataServiceConfig:
    """Loopback HTTP service MCP subprocesses use to read each
    agent's ``messages.db``. See ``portal/data_service.py``."""

    enabled: bool = True
    bind_host: str = "127.0.0.1"
    port: int = 63386


@dataclass
class RpcServiceConfig:
    """Loopback RPC the MCP calls for daemon-mediated ops (install/sync host MCP).
    See ``portal/rpc_service.py``."""

    enabled: bool = True
    bind_host: str = "127.0.0.1"
    port: int = 63385


@dataclass
class BridgeConfig:
    """Local HTTP API for the puffo web/desktop client. Loopback only;
    auth uses the same ed25519 request-signing scheme as puffo-server.

    ``allowed_origins`` is the CORS allowlist for PNA preflights.

    Off by default — agents are managed remotely via the portal. Opt in
    per-run with ``start --with-local-bridge`` (or ``bridge.enabled`` in
    daemon.yml). The MCP-facing data + rpc services stay on regardless.
    """

    enabled: bool = False
    bind_host: str = "127.0.0.1"
    port: int = 63387
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "https://chat.puffo.ai",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )


@dataclass
class DaemonConfig:
    """Contents of ~/.puffo-agent/daemon.yml.

    Per-agent identity lives in each agent's ``agent.yml``; the
    daemon holds model hints, reserved provider settings, and reconcile knobs.
    """

    default_provider: str = "anthropic"
    anthropic: ProviderConfig = field(default_factory=ProviderConfig)
    openai: ProviderConfig = field(default_factory=ProviderConfig)
    # Reserved for future Google/Gemini support; no supported runtime reads it.
    google: ProviderConfig = field(default_factory=ProviderConfig)
    skills_dir: str = ""  # absolute path; empty = no shared skills
    reconcile_interval_seconds: float = 2.0
    runtime_heartbeat_seconds: float = 5.0
    # cli-docker memory caps. Defaults bound each container so one
    # runaway claude can't poison the VM (vm.overcommit_memory=1 +
    # uncapped containers can drain swap and surface ENOMEM on
    # unrelated reads). Operators can opt out with empty strings;
    # per-agent overrides live on ``runtime``.
    docker_memory_limit: str = "1.5g"
    docker_memory_reservation: str = "500m"
    # Inbound message redaction. When an envelope's text exceeds
    # ``max_inline_message_chars`` the daemon replaces the body the
    # LLM sees with a system-message placeholder (carrying
    # envelope_id, total length, segment count, and a preview), and
    # the agent fetches the full content one chunk at a time via
    # the ``get_post_segment`` MCP tool. The original envelope is
    # stored unmodified in ``messages.db`` — only the prompt-budget
    # view is redacted. Tuned for Claude's 200k window minus a
    # generous system-prompt + history headroom; defaults pinned at
    # 4000/2000 so a single 8-segment paste fits comfortably even
    # with a verbose primer.
    max_inline_message_chars: int = 4000
    segment_chars: int = 2000
    # Catch-up older than this is stored but skips the LLM; <= 0 disables.
    catchup_stale_hours: float = DEFAULT_CATCHUP_STALE_HOURS
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    data_service: DataServiceConfig = field(
        default_factory=lambda: DataServiceConfig(),
    )
    rpc_service: RpcServiceConfig = field(
        default_factory=lambda: RpcServiceConfig(),
    )

    @classmethod
    def load(cls) -> DaemonConfig:
        path = daemon_yml_path()
        if not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        # Legacy ``server:`` blocks are silently ignored and dropped
        # on the next save().
        cfg = cls(
            default_provider=raw.get("default_provider", "anthropic"),
            skills_dir=raw.get("skills_dir", ""),
            reconcile_interval_seconds=float(
                raw.get("reconcile_interval_seconds", 2.0)
            ),
            runtime_heartbeat_seconds=float(raw.get("runtime_heartbeat_seconds", 5.0)),
            docker_memory_limit=raw.get("docker_memory_limit", "1.5g"),
            docker_memory_reservation=raw.get("docker_memory_reservation", "500m"),
            max_inline_message_chars=int(raw.get("max_inline_message_chars", 4000)),
            segment_chars=int(raw.get("segment_chars", 2000)),
            catchup_stale_hours=float(
                raw.get("catchup_stale_hours", DEFAULT_CATCHUP_STALE_HOURS)
            ),
        )
        for name in ("anthropic", "openai", "google"):
            p = raw.get(name) or {}
            setattr(
                cfg,
                name,
                ProviderConfig(
                    api_key=p.get("api_key", ""),
                    model=p.get("model", ""),
                ),
            )
        # Older daemon.yml files may omit ``bridge:``.
        b = raw.get("bridge") or {}
        defaults = BridgeConfig()
        cfg.bridge = BridgeConfig(
            enabled=bool(b.get("enabled", defaults.enabled)),
            bind_host=str(b.get("bind_host", defaults.bind_host)),
            port=int(b.get("port", defaults.port)),
            allowed_origins=list(b.get("allowed_origins") or defaults.allowed_origins),
        )
        d = raw.get("data_service") or {}
        ds_defaults = DataServiceConfig()
        cfg.data_service = DataServiceConfig(
            enabled=bool(d.get("enabled", ds_defaults.enabled)),
            bind_host=str(d.get("bind_host", ds_defaults.bind_host)),
            port=int(d.get("port", ds_defaults.port)),
        )
        r = raw.get("rpc_service") or {}
        rs_defaults = RpcServiceConfig()
        cfg.rpc_service = RpcServiceConfig(
            enabled=bool(r.get("enabled", rs_defaults.enabled)),
            bind_host=str(r.get("bind_host", rs_defaults.bind_host)),
            port=int(r.get("port", rs_defaults.port)),
        )
        return cfg

    def save(self) -> None:
        path = daemon_yml_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "default_provider": self.default_provider,
            "skills_dir": self.skills_dir,
            "reconcile_interval_seconds": self.reconcile_interval_seconds,
            "runtime_heartbeat_seconds": self.runtime_heartbeat_seconds,
            "docker_memory_limit": self.docker_memory_limit,
            "docker_memory_reservation": self.docker_memory_reservation,
            "max_inline_message_chars": self.max_inline_message_chars,
            "segment_chars": self.segment_chars,
            "catchup_stale_hours": self.catchup_stale_hours,
            "anthropic": asdict(self.anthropic),
            "openai": asdict(self.openai),
            "google": asdict(self.google),
            "bridge": asdict(self.bridge),
            "data_service": asdict(self.data_service),
            "rpc_service": asdict(self.rpc_service),
        }
        _atomic_write_yaml(path, data)


@dataclass
class TriggerRules:
    on_mention: bool = True
    on_dm: bool = True


## Default puffo-core server. Override per-agent via
## ``puffo_core.server_url`` for self-hosted relays or local dev.
## ``api.puffo.ai`` is platform-internal only; client traffic goes
## through the public ``chat.puffo.ai/relay`` edge.
DEFAULT_PUFFO_SERVER_URL = "https://chat.puffo.ai/relay"


## Transports for the puffo-core backend (T23). ``native`` is today's
## signed-crypto path; ``bridge`` is the experimental keyless WS bridge
## (server holds all crypto, auth via sandbox_token). agent.yml only —
## no UI knob while bridge is experimental.
VALID_TRANSPORTS = ("native", "bridge")


@dataclass
class PuffoCoreConfig:
    """puffo-core signed API config — the agent's only chat backend."""

    server_url: str = DEFAULT_PUFFO_SERVER_URL
    slug: str = ""
    device_id: str = ""
    space_id: str = ""
    # Operator's puffo-core slug; the agent DMs the operator here when
    # human attention is needed. Cryptographic ownership is still
    # carried by ``identity_cert.declared_operator_public_key``.
    operator_slug: str = ""
    # Hidden knob (no UI, agent.yml only): when true, space invites from
    # non-operators are auto-accepted, then the operator is DM'd a report.
    auto_accept_space_invitations: bool = False
    # One of VALID_TRANSPORTS. ``bridge`` (experimental) talks plaintext
    # WS to ``server_url``'s /v2/cloud-agents/subscribe endpoint using
    # ``sandbox_token`` instead of local key files. Absent from saved
    # agent.yml unless set to bridge.
    transport: str = "native"
    # Bridge only — the keyless auth credential minted at provision time.
    sandbox_token: str = ""

    def is_configured(self) -> bool:
        return bool(self.server_url and self.slug and self.device_id and self.space_id)


@dataclass
class RuntimeConfig:
    """Contents of the ``runtime:`` block in agent.yml.

    Four orthogonal knobs (see ``portal/runtime_matrix.py``):
    ``kind`` (where), ``provider`` (who), ``harness`` (what engine,
    CLI kinds only), and ``inference_level`` (provider reasoning effort).
    Empty strings on ``provider`` / ``model`` / ``inference_level`` /
    ``api_key`` mean "inherit from the harness or daemon defaults".
    """

    kind: str = "cli-local"  # cli-local | cli-docker | ws-local
    provider: str = ""  # empty = default for kind
    model: str = ""
    # Per-agent reasoning effort. Empty means harness default; when set,
    # adapters pin the generated provider config instead of inheriting an
    # operator-wide value that may be invalid for this agent's model.
    inference_level: str = ""
    api_key: str = ""
    # OpenAI/Anthropic-compatible base URL for the LLM plane. Set this
    # to route model calls through a proxy — e.g. Shan's LiteLLM virtual
    # key endpoint for cloud agents — instead of the vendor default.
    # Consumed by cli-local Drivers. Claude receives ANTHROPIC_BASE_URL;
    # Codex receives a generated model-provider entry. Empty keeps the
    # harness's normal OAuth endpoint. The matching gateway secret rides on
    # ``api_key`` (the VK), so no separate field is needed.
    llm_base_url: str = ""
    # Tool allowlist patterns (cli-local | cli-docker). Each
    # entry is a bare tool name ("Read") or tool-name-plus-arg glob
    # ("Bash(git *)", "Read(**/*.py)"). Empty = no tools allowed.
    allowed_tools: list[str] = field(default_factory=list)
    # cli-docker: override default image tag.
    docker_image: str = ""
    # cli-docker per-agent caps. Empty inherits daemon defaults;
    # docker memory string format: "768m", "1.5g", or raw bytes.
    docker_memory_limit: str = ""
    docker_memory_reservation: str = ""
    # cli-local Claude Code permission mode. Only ``bypassPermissions``
    # is supported by the local Driver runtime today.
    permission_mode: str = "bypassPermissions"
    # codex (cli-local) sandbox policy: read-only | workspace-write |
    # danger-full-access. Default leaves codex's sandbox fully open.
    sandbox: str = "danger-full-access"
    # Agent engine (CLI kinds only). cli-local supports the long-lived
    # ``claude-code`` and ``codex`` Drivers; cli-docker additionally keeps
    # the declarative ``hermes`` and ``gemini-cli`` harnesses.
    harness: str = "claude-code"
    # Retained only so older agent.yml files round-trip without losing data.
    # Driver runtimes use the wall-time limit below instead.
    max_turns: int = 10
    # cli-local: maximum wall time for one provider turn. The Runtime
    # Manager interrupts and retires a driver that does not terminate.
    task_timeout_seconds: float = 600.0


@dataclass
class AgentConfig:
    """Contents of ~/.puffo-agent/agents/<id>/agent.yml.

    The ``state`` field is the pause/resume knob; the daemon picks up
    changes on the next reconcile tick.
    """

    id: str = ""
    state: str = "running"  # running | paused
    display_name: str = ""
    # Cached chat avatar URL; server is source of truth.
    avatar_url: str = ""
    # ``role`` is the long-form (<=140 chars) "what does this agent do"
    # string; ``role_short`` is the chip label rendered by clients in
    # member lists. Mirror of the server-side identity profile fields
    # added in puffo-server's identity_role migration. On every edit
    # the daemon syncs both up to ``PATCH /identities/self``; the
    # server derives ``role_short`` from ``role`` when the client
    # omits it.
    role: str = ""
    role_short: str = ""
    puffo_core: PuffoCoreConfig = field(default_factory=PuffoCoreConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    profile: str = "profile.md"  # path relative to agent dir, or absolute
    memory_dir: str = "memory"  # path relative to agent dir, or absolute
    workspace_dir: str = "workspace"  # path relative to agent dir, or absolute
    # Per-agent .claude/ lives inside workspace_dir so Claude Code's
    # project-level convention (.claude/CLAUDE.md, .claude/skills/) is
    # found automatically. Not user-configurable; owned by the adapter.
    triggers: TriggerRules = field(default_factory=TriggerRules)
    # Operator-picked template ids installed at spawn AFTER host-sync,
    # de-duped against whatever host already provides.
    desired_skills: list[str] = field(default_factory=list)
    desired_mcps: list[str] = field(default_factory=list)
    created_at: int = 0

    def save(self) -> None:
        _agent_config_save(self)

    def resolve_profile_path(self) -> Path:
        return _agent_config_resolve(self, self.profile)

    def resolve_memory_dir(self) -> Path:
        return _agent_config_resolve(self, self.memory_dir)

    def resolve_workspace_dir(self) -> Path:
        return _agent_config_resolve(self, self.workspace_dir)

    def resolve_claude_dir(self) -> Path:
        """Always ``<workspace>/.claude`` — adapter-owned."""
        return self.resolve_workspace_dir() / ".claude"

    def _resolve(self, rel_or_abs: str) -> Path:
        return _agent_config_resolve(self, rel_or_abs)

    @classmethod
    def load(cls, agent_id: str) -> AgentConfig:
        path = agent_yml_path(agent_id)
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        pc = raw.get("puffo_core") or {}
        rt = raw.get("runtime") or {}
        triggers = raw.get("triggers") or {}
        runtime = _load_runtime_config(agent_id, rt)
        core = _load_puffo_core_config(agent_id, pc)

        return cls(
            id=raw.get("id", agent_id),
            state=raw.get("state", "running"),
            display_name=raw.get("display_name", ""),
            avatar_url=raw.get("avatar_url", ""),
            role=raw.get("role", ""),
            role_short=raw.get("role_short", ""),
            puffo_core=core,
            runtime=runtime,
            profile=raw.get("profile", "profile.md"),
            memory_dir=raw.get("memory_dir", "memory"),
            workspace_dir=raw.get("workspace_dir", "workspace"),
            triggers=TriggerRules(
                on_mention=bool(triggers.get("on_mention", True)),
                on_dm=bool(triggers.get("on_dm", True)),
            ),
            desired_skills=list(raw.get("desired_skills") or []),
            desired_mcps=list(raw.get("desired_mcps") or []),
            created_at=int(raw.get("created_at", 0)),
        )


def _load_runtime_config(agent_id: str, raw: dict) -> RuntimeConfig:
    from .runtime_matrix import (
        RUNTIME_CLI_LOCAL,
        migrate_legacy_kind,
        normalize_inference_level,
        resolve_effective_harness,
        resolve_effective_provider,
        validate_triple,
    )

    raw_kind, provider = raw.get("kind", RUNTIME_CLI_LOCAL), raw.get("provider", "")
    kind = migrate_legacy_kind(raw_kind, agent_id=agent_id)
    harness = raw.get("harness", "claude-code")
    inference = raw.get("inference_level", "") or ""
    if not isinstance(inference, str):
        raise RuntimeError(
            f"agent {agent_id!r}: runtime.inference_level must be a string"
        )
    if kind == RUNTIME_CLI_LOCAL:
        migrated = _migrate_local_harness(
            agent_id, provider, harness, kind_migrated=raw_kind != kind
        )
        if migrated != harness:
            harness = migrated
            inference = normalize_inference_level(kind, provider, harness, inference)
    result = validate_triple(kind, provider, harness)
    if not result.ok:
        raise RuntimeError(
            f"agent {agent_id!r}: invalid runtime config — {result.error}"
        )
    effective = resolve_effective_harness(
        kind, resolve_effective_provider(kind, provider), harness
    )
    _validate_inference_level(agent_id, inference, effective)
    return RuntimeConfig(
        kind=kind,
        provider=provider,
        model=raw.get("model", ""),
        inference_level=inference,
        api_key=raw.get("api_key", ""),
        llm_base_url=raw.get("llm_base_url", ""),
        allowed_tools=list(raw.get("allowed_tools") or []),
        docker_image=raw.get("docker_image", ""),
        docker_memory_limit=raw.get("docker_memory_limit", ""),
        docker_memory_reservation=raw.get("docker_memory_reservation", ""),
        permission_mode=raw.get("permission_mode", "bypassPermissions"),
        sandbox=raw.get("sandbox", "danger-full-access"),
        harness=harness,
        max_turns=int(raw.get("max_turns", 10)),
        task_timeout_seconds=float(raw.get("task_timeout_seconds", 600.0)),
    )


def _migrate_local_harness(
    agent_id: str, provider: str, harness: str, *, kind_migrated: bool
) -> str:
    """Return the harness a stale ``cli-local`` config should load as.

    Configs written before the host-local Driver runtime landed name a
    harness it never implements (``hermes``), and a legacy ``kind`` can leave
    a harness that contradicts the stored provider. Rather than making those
    agents unloadable, adopt the harness the provider resolves to — but only
    when that yields a valid triple, so genuinely broken hand-edits still hit
    the explicit error in ``_load_runtime_config``.
    """
    from .runtime_matrix import (
        HARNESS_CLAUDE_CODE,
        HARNESS_CODEX,
        RUNTIME_CLI_LOCAL,
        resolve_effective_harness,
        validate_triple,
    )

    if validate_triple(RUNTIME_CLI_LOCAL, provider, harness).ok:
        return harness
    if not kind_migrated and harness in {HARNESS_CLAUDE_CODE, HARNESS_CODEX}:
        # A Driver harness that fails for some other reason (e.g. an
        # explicitly mismatched provider) is an operator error, not legacy.
        return harness
    resolved = resolve_effective_harness(RUNTIME_CLI_LOCAL, provider, "")
    if resolved == harness or not validate_triple(
        RUNTIME_CLI_LOCAL, provider, resolved
    ).ok:
        return harness
    logger.warning(
        "agent %s: runtime.harness %r is not implemented by the %r runtime, "
        "auto-migrated to %r for this run; authenticate with `claude login` "
        "or `codex login`, then update agent.yml.",
        agent_id or "(?)", harness, RUNTIME_CLI_LOCAL, resolved,
    )
    return resolved


def _validate_inference_level(agent_id: str, value: str, harness: str) -> None:
    if not value:
        return
    from ..mcp.config import supported_inference_levels

    levels = supported_inference_levels(harness)
    if value not in levels:
        raise RuntimeError(
            f"agent {agent_id!r}: inference_level={value!r} is not supported by harness={harness!r}; expected one of {list(levels)}"
        )


def _load_puffo_core_config(agent_id: str, raw: dict) -> PuffoCoreConfig:
    transport, token = raw.get("transport", "native"), raw.get("sandbox_token", "")
    server_url = raw.get("server_url") or DEFAULT_PUFFO_SERVER_URL
    if transport not in VALID_TRANSPORTS:
        raise RuntimeError(
            f"agent {agent_id!r}: invalid puffo_core.transport {transport!r} — valid transports: {list(VALID_TRANSPORTS)}"
        )
    if transport == "bridge" and not (token and server_url):
        raise RuntimeError(
            f"agent {agent_id!r}: puffo_core.transport 'bridge' requires both server_url and sandbox_token"
        )
    return PuffoCoreConfig(
        server_url=server_url,
        slug=raw.get("slug", ""),
        device_id=raw.get("device_id", ""),
        space_id=raw.get("space_id", ""),
        operator_slug=raw.get("operator_slug", ""),
        auto_accept_space_invitations=bool(
            raw.get("auto_accept_space_invitations", False)
        ),
        transport=transport,
        sandbox_token=token,
    )


def _agent_config_save(self: AgentConfig) -> None:
    path = agent_yml_path(self.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pc_dict = asdict(self.puffo_core)
    if self.puffo_core.transport == "native":
        pc_dict.pop("transport", None)
        pc_dict.pop("sandbox_token", None)
    _atomic_write_yaml(
        path,
        {
            "id": self.id,
            "state": self.state,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "role": self.role,
            "role_short": self.role_short,
            "created_at": self.created_at,
            "puffo_core": pc_dict,
            "runtime": asdict(self.runtime),
            "profile": self.profile,
            "memory_dir": self.memory_dir,
            "workspace_dir": self.workspace_dir,
            "triggers": asdict(self.triggers),
            "desired_skills": list(self.desired_skills),
            "desired_mcps": list(self.desired_mcps),
        },
    )


def _agent_config_resolve(self: AgentConfig, rel_or_abs: str) -> Path:
    path = Path(rel_or_abs)
    return path if path.is_absolute() else agent_dir(self.id) / path


@dataclass
class RuntimeState:
    """Worker heartbeat snapshot, read by the CLI for list/show.

    ``updated_at`` lets readers detect stale entries (daemon down or
    worker deadlocked).
    """

    status: str = "stopped"  # running | paused | error | stopped
    started_at: int = 0
    updated_at: int = 0
    msg_count: int = 0
    last_event_at: int = 0
    error: str = ""
    # Worker-side health, independent of ``status``. Values:
    #   "ok"                  — refresh-ping passed, a turn cleared a
    #                           prior abandon, or a credential refresh
    #                           cleared a prior auth_failed
    #   "in_progress"         — turn mid-flight; overrides any sticky
    #                           red so the UI reads alive
    #   "auth_failed"         — adapter saw 401 / authentication_error
    #                           (set in worker._handle_suppressed_reply);
    #                           cleared by the CredentialRefresher's
    #                           refresh-success callback (PUF-258 wired
    #                           the clear; PUF-221 owns the set lane)
    #   "api_error_abandoned" — kick-retry exhausted, batch silently
    #                           abandoned; cleared on next successful turn
    #                           (PUF-255's on_turn_success lane)
    #   "refresh_broken"      — daemon saw N consecutive non-success
    #                           refresh outcomes; cleared by next
    #                           REFRESHED. Does not overwrite the two
    #                           stronger downstream signals above.
    #   "unhandled_error"     — non-AgentAPIError raised in the turn and
    #                           no category red was set; cleared by
    #                           next successful turn
    #   "codex_thread_wedged" — codex thread rotated after N consecutive
    #                           turn timeouts/failures OR the verbatim
    #                           "agent thread limit reached" error.
    #                           Auto-recovers on next inbound message;
    #                           cleared on next successful turn. Does
    #                           NOT overwrite the stronger downstream
    #                           signals above.
    #   "unknown"             — no probe yet
    health: str = "unknown"  # ok | in_progress | auth_failed | api_error_abandoned | refresh_broken | unhandled_error | codex_thread_wedged | unknown

    @classmethod
    def load(cls, agent_id: str) -> RuntimeState | None:
        path = runtime_json_path(agent_id)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                import json

                raw = json.load(f)
        except (OSError, ValueError):
            return None
        return cls(
            status=raw.get("status", "stopped"),
            started_at=int(raw.get("started_at", 0)),
            updated_at=int(raw.get("updated_at", 0)),
            msg_count=int(raw.get("msg_count", 0)),
            last_event_at=int(raw.get("last_event_at", 0)),
            error=raw.get("error", ""),
            health=raw.get("health", "unknown"),
        )

    def save(self, agent_id: str) -> None:
        import json

        self.updated_at = int(time.time())
        path = runtime_json_path(agent_id)
        # CLI staleness gate is 30s; throttle pure-updated_at writes
        # to <25s gives the reader 5s slack and kills the heartbeat
        # write-storm. Force-write on missing file.
        d = asdict(self)
        d.pop("updated_at", None)
        sig = json.dumps(d, sort_keys=True)
        key = str(path)
        prev = _RUNTIME_LAST_SAVE.get(key)
        if (
            prev is not None
            and sig == prev[0]
            and (self.updated_at - prev[1]) < 25
            and path.exists()
        ):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(tmp, path)
        _RUNTIME_LAST_SAVE[key] = (sig, self.updated_at)


# Keyed by resolved path so test tmp_path reuse doesn't collide.
_RUNTIME_LAST_SAVE: dict[str, tuple[str, int]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Discovery + helpers
# ─────────────────────────────────────────────────────────────────────────────


_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def is_valid_agent_id(agent_id: str) -> bool:
    return bool(_AGENT_ID_RE.match(agent_id)) and len(agent_id) <= 64


def discover_agents() -> list[str]:
    """Return agent ids in lexicographic order. Does not load their config."""
    root = agents_dir()
    if not root.exists():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and (entry / "agent.yml").exists()
    )


def read_daemon_pid() -> int | None:
    path = daemon_pid_path()
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def is_daemon_alive() -> bool:
    """True iff ``daemon.pid``'s pid is live AND its cmdline matches
    ``puffo-agent start``. The cmdline check guards against PID
    reuse and excludes concurrent CLI invocations."""
    pid = read_daemon_pid()
    if pid is None:
        return False
    return _is_puffo_agent_process(pid)


def is_pid_alive(pid: int) -> bool:
    """True iff ``pid`` is a live puffo-agent daemon process.

    Unlike ``is_daemon_alive()``, this checks a SPECIFIC pid the caller
    holds — so a ``cmd_stop`` poll tracks the daemon it asked to stop
    instead of whatever's in the pid file, which can swap mid-upgrade.
    """
    return _is_puffo_agent_process(pid)


def _is_puffo_agent_process(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
        tokens = [t or "" for t in proc.cmdline()]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False

    # Match by token name prefix to cover script-shim, .exe, and
    # ``python -m puffo_agent.portal.cli`` invocations.
    def _is_ours(token: str) -> bool:
        low = Path(token).name.lower()
        return low.startswith("puffo-agent") or low.startswith("puffo_agent")

    has_exe = any(_is_ours(t) for t in tokens)
    has_start = any(t.lower() == "start" for t in tokens)
    return has_exe and has_start


def write_daemon_pid(pid: int) -> None:
    path = daemon_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")


def clear_daemon_pid() -> None:
    path = daemon_pid_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def stop_request_path() -> Path:
    """File sentinel ``puffo-agent stop`` writes for graceful
    shutdown. Required on Windows where SIGTERM can't reach a
    proactor-loop daemon."""
    return home_dir() / ".stop_requested"


def write_stop_request() -> None:
    path = stop_request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(time.time())), encoding="utf-8")


def clear_stop_request() -> None:
    path = stop_request_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def refresh_token_request_path() -> Path:
    """PUF-221: file sentinel ``puffo-agent agent refresh-token``
    writes to ask the daemon to run an OAuth refresh + fan view-sync
    to every agent. Daemon's reconcile loop picks it up and clears."""
    return home_dir() / ".refresh_token_requested"


def write_refresh_token_request() -> None:
    path = refresh_token_request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(time.time())), encoding="utf-8")


def clear_refresh_token_request() -> None:
    path = refresh_token_request_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Atomic YAML write
# ─────────────────────────────────────────────────────────────────────────────


def _atomic_write_yaml(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        # allow_unicode keeps CJK/emoji/accented display_names readable.
        yaml.safe_dump(
            data,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
    os.replace(tmp, path)
