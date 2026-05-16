"""On-disk state layout for the multi-agent portal.

Home defaults to ``~/.puffo-agent/`` (override with ``PUFFO_AGENT_HOME``)::

    ~/.puffo-agent/
      daemon.yml          # ai provider keys, defaults
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

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import psutil
import yaml


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


def shared_fs_dir() -> Path:
    """Shared dir for cross-agent cooperation. Bind-mounted to
    ``/workspace/.shared`` for cli-docker; referenced by absolute path
    for cli-local / sdk agents."""
    return home_dir() / "shared"


# Files copied from the operator's $HOME into a per-agent virtual
# $HOME on first use. Lift OAuth-essential files only.
# Note: ``.claude.json`` is a sibling of the ``.claude/`` dir; Claude
# CLI reads it from ``$HOME/.claude.json`` so we mirror that layout.
# ``.credentials.json`` is intentionally excluded — set up separately
# via ``link_host_credentials`` so every agent tracks live OAuth state
# (matches cli-docker's bind-mount model).
_CLAUDE_HOME_SEED_PATHS = (
    ".claude/settings.json",
    ".claude.json",
)


def seed_claude_home(host_home: Path, agent_home: Path) -> bool:
    """Seed a per-agent virtual ``$HOME`` from the operator's real
    ``$HOME``. Idempotent — never overwrites an existing file.

    ``.credentials.json`` is set up separately via
    ``link_host_credentials``. Returns True if any file was copied.
    """
    import shutil
    agent_home.mkdir(parents=True, exist_ok=True)
    copied = False
    for rel in _CLAUDE_HOME_SEED_PATHS:
        src = host_home / rel
        dst = agent_home / rel
        if dst.exists() or not src.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied = True
        except OSError:
            continue
    return copied


def _sync_credentials_from_keychain(host_home: Path) -> bool:
    """On macOS, materialise ``~/.claude/.credentials.json`` from the
    Keychain entry ``"Claude Code-credentials"`` when missing or stale.

    Claude Code 2.x stores OAuth in Keychain instead of the file; this
    bridges to the shared-file path used by every other agent. Called
    on every ``link_host_credentials`` invocation so refreshed tokens
    propagate. Returns True if the file was written.
    """
    import platform
    import subprocess
    if platform.system() != "Darwin":
        return False
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
        keychain_raw = result.stdout.strip()
        # Validate JSON before touching the file.
        keychain_data = json.loads(keychain_raw)
    except Exception:
        return False

    host_creds = host_home / ".claude" / ".credentials.json"

    # Skip write when the access token already matches; avoids mtime
    # churn that would trigger copy-mode re-syncs.
    if host_creds.exists():
        try:
            existing = json.loads(host_creds.read_text(encoding="utf-8"))
            kc_token = (keychain_data.get("claudeAiOauth") or {}).get("accessToken")
            ex_token = (existing.get("claudeAiOauth") or {}).get("accessToken")
            if kc_token and kc_token == ex_token:
                return False
        except Exception:
            pass  # Corrupted file — overwrite below.

    try:
        host_creds.parent.mkdir(parents=True, exist_ok=True)
        host_creds.write_text(keychain_raw, encoding="utf-8")
        return True
    except OSError:
        return False


def link_host_credentials(host_home: Path, agent_home: Path) -> str:
    """Share the operator's ``.credentials.json`` with the agent so
    OAuth refresh-token rotation propagates automatically.

    Anthropic OAuth uses rotating refresh tokens; per-agent copies go
    stale when the operator re-runs ``claude login``. Sharing one
    file means any refresh (host, any agent) updates the single file
    that everyone reads.

    Prefers symlink (free read-through, survives atomic rename
    writes); falls back to copy on Windows-without-Developer-Mode.
    Hardlinks are intentionally skipped — claude's atomic tmp+rename
    breaks the shared inode.

    On macOS, ``_sync_credentials_from_keychain`` materialises the
    file from the system Keychain first.

    Idempotent. Returns ``"symlink"``, ``"symlink (already)"``,
    ``"copy"``, ``"copy (fresh)"``, or ``"no-host-file"``.
    """
    import shutil
    host_creds = host_home / ".claude" / ".credentials.json"
    agent_creds = agent_home / ".claude" / ".credentials.json"
    # macOS Keychain → file bridge before we read host_creds.
    _sync_credentials_from_keychain(host_home)
    if not host_creds.exists():
        return "no-host-file"
    agent_creds.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: existing symlink already points at host_creds.
    if agent_creds.is_symlink():
        try:
            current = os.readlink(agent_creds)
            if Path(current) == host_creds or current == str(host_creds):
                return "symlink (already)"
        except OSError:
            pass

    # Fast path: copy-mode file already matches host.
    if (
        agent_creds.exists()
        and not agent_creds.is_symlink()
        and _file_is_up_to_date(agent_creds, host_creds)
    ):
        return "copy (fresh)"

    # Tear down whatever's there before a fresh create. Unlink can
    # fail on Windows races; the next call retries naturally.
    try:
        if agent_creds.is_symlink() or agent_creds.exists():
            agent_creds.unlink()
    except OSError:
        pass

    try:
        os.symlink(host_creds, agent_creds)
        return "symlink"
    except (OSError, NotImplementedError):
        pass

    try:
        shutil.copy2(host_creds, agent_creds)
        return "copy"
    except OSError:
        return "no-host-file"


def _file_is_up_to_date(dst: Path, src: Path) -> bool:
    """True when ``dst`` and ``src`` have matching mtime + size."""
    try:
        ds, ss = dst.stat(), src.stat()
    except OSError:
        return False
    return ds.st_mtime == ss.st_mtime and ds.st_size == ss.st_size


# Provenance markers dropped in skill dirs. Claude Code only loads
# SKILL.md as a skill's entrypoint, so these siblings are inert.
HOST_SYNCED_MARKER = "host-synced.md"
AGENT_INSTALLED_MARKER = "agent-installed.md"

_HOST_SYNCED_MARKER_BODY = (
    "This skill is synced from the operator's ~/.claude/skills/ on "
    "every worker start. Do not edit; changes will be overwritten.\n"
)
_AGENT_INSTALLED_MARKER_BODY = (
    "This skill was installed by the agent via the install_skill "
    "MCP tool. It lives at project scope and survives host syncs.\n"
)


def _sync_host_skills_dir(
    src: Path, dst_root: Path, marker_body: str,
) -> int:
    """Copy skill directories from ``src`` into ``dst_root``.

    Host is source of truth; agent-installed skills are preserved on
    name collision; stale host-synced skills are pruned. Returns the
    number of dirs copied.
    """
    import shutil
    host_names: set[str] = set()
    if src.is_dir():
        host_names = {p.name for p in src.iterdir() if p.is_dir()}

    copied = 0
    if host_names:
        dst_root.mkdir(parents=True, exist_ok=True)
        for name in sorted(host_names):
            src_dir = src / name
            dst_dir = dst_root / name
            if (dst_dir / AGENT_INSTALLED_MARKER).exists():
                continue
            try:
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir)
                (dst_dir / HOST_SYNCED_MARKER).write_text(
                    marker_body, encoding="utf-8",
                )
                copied += 1
            except OSError:
                continue

    if dst_root.is_dir():
        for entry in dst_root.iterdir():
            if not entry.is_dir() or entry.name in host_names:
                continue
            if (entry / HOST_SYNCED_MARKER).exists() and not (
                entry / AGENT_INSTALLED_MARKER
            ).exists():
                try:
                    shutil.rmtree(entry)
                except OSError:
                    pass

    return copied


def sync_host_skills(host_home: Path, agent_home: Path) -> int:
    """Sync host ``~/.claude/skills/`` into the agent's user-scope
    skills dir. Whole-tree copy; flat ``.md`` files are ignored
    because they aren't valid Claude Code skills."""
    return _sync_host_skills_dir(
        src=host_home / ".claude" / "skills",
        dst_root=agent_home / ".claude" / "skills",
        marker_body=_HOST_SYNCED_MARKER_BODY,
    )


_HOST_SYNCED_GEMINI_MARKER_BODY = (
    "This skill is synced from the operator's ~/.gemini/skills/ on "
    "every worker start. Do not edit; changes will be overwritten.\n"
)


def sync_host_gemini_skills(host_home: Path, project_dir: Path) -> int:
    """Sync host ``~/.gemini/skills/`` into project-scope
    ``<project_dir>/.gemini/skills/``.

    Project scope is required: gemini-cli's resolver defaults to
    project scope, so user-scope settings.json entries are silently
    ignored. Same provenance + pruning semantics as
    ``sync_host_skills``.
    """
    return _sync_host_skills_dir(
        src=host_home / ".gemini" / "skills",
        dst_root=project_dir / ".gemini" / "skills",
        marker_body=_HOST_SYNCED_GEMINI_MARKER_BODY,
    )


# Path prefixes that won't resolve inside the runtime container.
# ``/home/agent/`` is handled separately because it IS valid inside.
_HOST_LOCAL_COMMAND_PREFIXES = ("/Users/", "/tmp/", "/var/folders/")


def _looks_host_local_command(command: str) -> bool:
    """True when ``command`` points at a host-only path. Conservative:
    bare program names (``npx``, ``python3``) pass through."""
    if not command:
        return False
    # Windows drive-letter / backslash paths can't resolve in a Linux container.
    if re.match(r"^[A-Za-z]:[\\/]", command) or "\\" in command:
        return True
    # /home/* on the host (but the container's own /home/agent/ is fine).
    if command.startswith("/home/") and not command.startswith("/home/agent/"):
        return True
    return any(command.startswith(p) for p in _HOST_LOCAL_COMMAND_PREFIXES)


def sync_host_mcp_servers(
    host_home: Path, agent_home: Path,
) -> tuple[int, list[tuple[str, str]]]:
    """Merge host ``~/.claude.json`` MCP registrations into the
    per-agent ``.claude.json``.

    Host wins on name collision; agent-only names are preserved;
    every other key is left untouched. Returns
    ``(merged_count, unreachable)`` — ``unreachable`` lists
    ``(name, command)`` pairs whose command looks host-local.
    """
    host_path = host_home / ".claude.json"
    if not host_path.exists():
        return 0, []
    try:
        host_data = json.loads(host_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return 0, []
    host_servers = host_data.get("mcpServers") or {}
    if not host_servers:
        return 0, []

    agent_path = agent_home / ".claude.json"
    agent_data: dict[str, Any] = {}
    if agent_path.exists():
        try:
            raw = agent_path.read_text(encoding="utf-8")
            if raw.strip():
                agent_data = json.loads(raw)
        except (OSError, ValueError):
            agent_data = {}

    agent_servers = dict(agent_data.get("mcpServers") or {})
    unreachable: list[tuple[str, str]] = []
    for name, cfg in host_servers.items():
        agent_servers[name] = cfg
        if isinstance(cfg, dict):
            cmd = cfg.get("command") or ""
            if isinstance(cmd, str) and _looks_host_local_command(cmd):
                unreachable.append((name, cmd))
    agent_data["mcpServers"] = agent_servers

    try:
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = agent_path.with_suffix(agent_path.suffix + ".tmp")
        tmp.write_text(json.dumps(agent_data, indent=2), encoding="utf-8")
        os.replace(tmp, agent_path)
    except OSError:
        return 0, []
    return len(host_servers), unreachable


def sync_host_claude_ai_state(host_home: Path, agent_home: Path) -> int:
    """Mirror Claude.ai connector / account state from host
    ``~/.claude.json`` into the per-agent ``.claude.json`` so the
    agent's Claude Code subprocess inherits the operator's remote
    connectors (Gmail, Google Drive, Google Calendar, Notion, etc.).

    The connector state lives under a family of ``claudeAi*`` keys
    (``claudeAiMcpEverConnected``, ``claudeAiOauth`` not included —
    that's the auth path, handled separately, etc.). ``seed_claude_home``
    used to copy ``.claude.json`` exactly once at agent creation,
    so a connector added on the host AFTER the agent existed never
    propagated. ``sync_host_mcp_servers`` (which runs every spawn)
    only touches the local ``mcpServers`` field — remote Claude.ai
    connectors aren't in that field.

    Merge strategy: every host key beginning with ``claudeAi`` wins;
    every other agent-side key is preserved (transcript history,
    project state, MCP servers we already synced separately). Returns
    the number of keys copied. Safe to call on every worker spawn.
    """
    host_path = host_home / ".claude.json"
    if not host_path.exists():
        return 0
    try:
        host_data = json.loads(host_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return 0
    if not isinstance(host_data, dict):
        return 0

    # Auth lives in ``claudeAiOauth`` — already handled by
    # link_host_credentials + Keychain bridge. Skip it here to avoid
    # racing those paths.
    SKIP_KEYS = frozenset({"claudeAiOauth"})
    claude_ai_keys = {
        k: v for k, v in host_data.items()
        if k.startswith("claudeAi") and k not in SKIP_KEYS
    }
    if not claude_ai_keys:
        return 0

    agent_path = agent_home / ".claude.json"
    agent_data: dict[str, Any] = {}
    if agent_path.exists():
        try:
            raw = agent_path.read_text(encoding="utf-8")
            if raw.strip():
                agent_data = json.loads(raw)
                if not isinstance(agent_data, dict):
                    agent_data = {}
        except (OSError, ValueError):
            agent_data = {}

    agent_data.update(claude_ai_keys)

    try:
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = agent_path.with_suffix(agent_path.suffix + ".tmp")
        tmp.write_text(json.dumps(agent_data, indent=2), encoding="utf-8")
        os.replace(tmp, agent_path)
    except OSError:
        return 0
    return len(claude_ai_keys)


def sync_host_plugins(host_home: Path, agent_home: Path) -> str:
    """Mirror host ``~/.claude/plugins/`` into per-agent
    ``.claude/plugins/`` so the agent's spawned Claude session can
    resolve plugin names listed in ``settings.json#enabledPlugins``.

    Without this, ``settings.json`` carries enabledPlugins via
    ``seed_claude_home`` but Claude can't find the plugin code under
    ``<agent_home>/.claude/plugins/`` and silently drops every plugin
    — including any MCP servers they would register. cli-local
    repro: operator runs ``claude /plugin install
    chrome-devtools-mcp@claude-plugins-official``, then spawns an
    agent → the agent sees ``(no MCP servers registered)`` for the
    plugin-provided MCPs.

    Prefers symlink (free read-through; new host plugin installs /
    marketplace pulls show up automatically on next worker start
    without re-copy). Falls back to ``copytree`` on Windows-without-
    Developer-Mode. The plugin tree can be GB-scale (each marketplace
    is a git clone with history); on copy fallback we don't refresh
    an existing copy — operators can ``rm -rf <agent>/.claude/plugins``
    to force a fresh re-sync.

    Idempotent. Returns ``"symlink"``, ``"symlink (already)"``,
    ``"copy"``, ``"copy (fresh)"``, or ``"no-host-dir"``.
    """
    import shutil
    host_plugins = host_home / ".claude" / "plugins"
    agent_plugins = agent_home / ".claude" / "plugins"
    if not host_plugins.is_dir():
        return "no-host-dir"
    agent_plugins.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: existing symlink already points at host_plugins.
    if agent_plugins.is_symlink():
        try:
            current = os.readlink(agent_plugins)
            if Path(current) == host_plugins or current == str(host_plugins):
                return "symlink (already)"
        except OSError:
            pass

    # Fast path: copy-mode dir already in place. We deliberately
    # don't recopy — see the docstring for the GB-scale rationale.
    if agent_plugins.is_dir() and not agent_plugins.is_symlink():
        return "copy (fresh)"

    # Tear down whatever's there (stale symlink, regular file) before
    # creating a fresh one. Unlink can fail on Windows races; the
    # next call retries naturally.
    try:
        if agent_plugins.is_symlink() or agent_plugins.exists():
            agent_plugins.unlink()
    except OSError:
        pass

    try:
        os.symlink(host_plugins, agent_plugins, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass

    try:
        shutil.copytree(host_plugins, agent_plugins)
        return "copy"
    except OSError:
        return "no-host-dir"


def sync_host_enabled_plugins(host_home: Path, agent_home: Path) -> int:
    """Mirror host ``~/.claude/settings.json#enabledPlugins`` into the
    per-agent ``settings.json``. ``enabledPlugins`` is the complete
    enumeration of which ``<plugin>@<marketplace>`` names the operator
    has flipped on; host wins and overwrites the agent's value.

    ``seed_claude_home`` already copies ``settings.json`` once on
    first start, but it's idempotent — when the operator enables a
    new plugin later, the agent's copy stays stale. This helper
    rewrites just ``enabledPlugins`` on every worker start while
    leaving other settings keys (theme, model preferences, etc.)
    untouched. The actual plugin code is wired up by the sibling
    ``sync_host_plugins``.

    Returns the count of enabledPlugins entries propagated. Returns
    0 when host has no settings.json, no enabledPlugins key, or the
    value isn't a dict/list.
    """
    host_settings = host_home / ".claude" / "settings.json"
    if not host_settings.is_file():
        return 0
    try:
        host_data = json.loads(host_settings.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return 0
    enabled = host_data.get("enabledPlugins")
    # Claude Code has used both shapes historically — dict
    # (``{name: true}``) on newer versions, list (``[name, ...]``)
    # on older. Pass either through unchanged so we don't reshape
    # something Claude is about to read.
    if not isinstance(enabled, (list, dict)) or not enabled:
        return 0

    agent_settings = agent_home / ".claude" / "settings.json"
    agent_data: dict[str, Any] = {}
    if agent_settings.exists():
        try:
            raw = agent_settings.read_text(encoding="utf-8")
            if raw.strip():
                agent_data = json.loads(raw)
        except (OSError, ValueError):
            agent_data = {}

    agent_data["enabledPlugins"] = enabled

    try:
        agent_settings.parent.mkdir(parents=True, exist_ok=True)
        tmp = agent_settings.with_suffix(agent_settings.suffix + ".tmp")
        tmp.write_text(json.dumps(agent_data, indent=2), encoding="utf-8")
        os.replace(tmp, agent_settings)
    except OSError:
        return 0
    return len(enabled)


def sync_host_gemini_mcp_servers(
    host_home: Path, project_dir: Path, *, extra_servers: dict | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Merge host ``~/.gemini/settings.json`` MCP registrations into
    project-scope ``<project_dir>/.gemini/settings.json``.

    Project scope is required: gemini-cli's resolver defaults to
    project scope and silently ignores user-scope mcpServers entries.
    Other keys on the per-agent settings.json are preserved; only
    ``mcpServers`` is overwritten.

    ``extra_servers`` lets the caller inject adapter-managed entries
    (e.g. the puffo MCP stdio server) in the same write; these
    override same-named host entries. Returns
    ``(merged_count, unreachable)``; merged_count counts host entries
    only.
    """
    host_path = host_home / ".gemini" / "settings.json"
    host_servers: dict = {}
    if host_path.exists():
        try:
            raw = host_path.read_text(encoding="utf-8")
            if raw.strip():
                host_servers = (json.loads(raw).get("mcpServers") or {})
        except (OSError, ValueError):
            host_servers = {}

    agent_path = project_dir / ".gemini" / "settings.json"
    agent_data: dict[str, Any] = {}
    if agent_path.exists():
        try:
            raw = agent_path.read_text(encoding="utf-8")
            if raw.strip():
                agent_data = json.loads(raw)
        except (OSError, ValueError):
            agent_data = {}

    merged_servers = dict(agent_data.get("mcpServers") or {})
    unreachable: list[tuple[str, str]] = []
    for name, cfg in host_servers.items():
        merged_servers[name] = cfg
        if isinstance(cfg, dict):
            cmd = cfg.get("command") or ""
            if isinstance(cmd, str) and _looks_host_local_command(cmd):
                unreachable.append((name, cmd))

    if extra_servers:
        for name, cfg in extra_servers.items():
            merged_servers[name] = cfg

    agent_data["mcpServers"] = merged_servers

    try:
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = agent_path.with_suffix(agent_path.suffix + ".tmp")
        tmp.write_text(json.dumps(agent_data, indent=2), encoding="utf-8")
        os.replace(tmp, agent_path)
    except OSError:
        return 0, []
    return len(host_servers), unreachable


def daemon_yml_path() -> Path:
    return home_dir() / "daemon.yml"


def daemon_pid_path() -> Path:
    return home_dir() / "daemon.pid"


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


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ProviderConfig:
    api_key: str = ""
    model: str = ""


@dataclass
class DataServiceConfig:
    """Loopback HTTP service used by MCP subprocesses to read the
    per-agent ``messages.db``. See ``portal/data_service.py``."""
    enabled: bool = True
    bind_host: str = "127.0.0.1"
    port: int = 63386


@dataclass
class BridgeConfig:
    """Local HTTP API for the puffo web/desktop client. Loopback only;
    auth uses the same ed25519 request-signing scheme as puffo-server.

    ``allowed_origins`` is the CORS allowlist for PNA preflights.
    """
    enabled: bool = True
    bind_host: str = "127.0.0.1"
    port: int = 63387
    allowed_origins: list[str] = field(default_factory=lambda: [
        "https://chat.puffo.ai",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ])


@dataclass
class DaemonConfig:
    """Contents of ~/.puffo-agent/daemon.yml.

    Per-agent identity lives in each agent's ``agent.yml``; the
    daemon holds only provider keys + reconcile knobs.
    """
    default_provider: str = "anthropic"
    anthropic: ProviderConfig = field(default_factory=ProviderConfig)
    openai: ProviderConfig = field(default_factory=ProviderConfig)
    # Required for cli-docker + harness=gemini-cli agents; passed
    # through as GEMINI_API_KEY to the containerised gemini CLI.
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
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    data_service: "DataServiceConfig" = field(
        default_factory=lambda: DataServiceConfig(),
    )

    @classmethod
    def load(cls) -> "DaemonConfig":
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
            reconcile_interval_seconds=float(raw.get("reconcile_interval_seconds", 2.0)),
            runtime_heartbeat_seconds=float(raw.get("runtime_heartbeat_seconds", 5.0)),
            docker_memory_limit=raw.get("docker_memory_limit", "1.5g"),
            docker_memory_reservation=raw.get("docker_memory_reservation", "500m"),
            max_inline_message_chars=int(raw.get("max_inline_message_chars", 4000)),
            segment_chars=int(raw.get("segment_chars", 2000)),
        )
        for name in ("anthropic", "openai", "google"):
            p = raw.get(name) or {}
            setattr(cfg, name, ProviderConfig(
                api_key=p.get("api_key", ""),
                model=p.get("model", ""),
            ))
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
            "anthropic": asdict(self.anthropic),
            "openai": asdict(self.openai),
            "google": asdict(self.google),
            "bridge": asdict(self.bridge),
            "data_service": asdict(self.data_service),
        }
        _atomic_write_yaml(path, data)


@dataclass
class TriggerRules:
    on_mention: bool = True
    on_dm: bool = True


## Default puffo-core server. Override per-agent via
## ``puffo_core.server_url`` for self-hosted relays or local dev.
DEFAULT_PUFFO_SERVER_URL = "https://api.puffo.ai"


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

    def is_configured(self) -> bool:
        return bool(self.server_url and self.slug and self.device_id and self.space_id)


@dataclass
class RuntimeConfig:
    """Contents of the ``runtime:`` block in agent.yml.

    Three orthogonal knobs (see ``portal/runtime_matrix.py``):
    ``kind`` (where), ``provider`` (who), ``harness`` (what engine,
    CLI kinds only). Empty strings on ``provider`` / ``model`` /
    ``api_key`` mean "inherit from daemon defaults".
    """
    kind: str = "chat-local"      # chat-local | sdk-local | cli-local | cli-docker
    provider: str = ""            # empty = default for kind
    model: str = ""
    api_key: str = ""
    # Tool allowlist patterns (sdk | cli-local | cli-docker). Each
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
    # is supported today; see LocalCLIAdapter._sanitise_permission_mode.
    permission_mode: str = "bypassPermissions"
    # Agent engine (CLI kinds only):
    #   - ``claude-code``: ``claude`` CLI with our stream-json session
    #     protocol, --resume, --model, and the puffo MCP tool suite.
    #   - ``hermes``: ``hermes chat -q`` one-shot per turn against
    #     Anthropic, using Claude Code's credential store.
    #   - ``gemini-cli``: declared, not yet implemented.
    # Hermes + anthropic billing note: third-party OAuth clients route
    # to Anthropic's ``extra_usage`` pool, NOT a Claude subscription.
    # Same token, different ledger.
    harness: str = "claude-code"  # claude-code | hermes
    # sdk only: cap on agentic-loop iterations per turn. 10 is fine
    # for short Q&A; multi-step work often needs 30-50. CLI kinds
    # delegate turn-bounding to the claude CLI itself.
    max_turns: int = 10


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
    profile: str = "profile.md"       # path relative to agent dir, or absolute
    memory_dir: str = "memory"        # path relative to agent dir, or absolute
    workspace_dir: str = "workspace"  # path relative to agent dir, or absolute
    # Per-agent .claude/ lives inside workspace_dir so Claude Code's
    # project-level convention (.claude/CLAUDE.md, .claude/skills/) is
    # found automatically. Not user-configurable; owned by the adapter.
    triggers: TriggerRules = field(default_factory=TriggerRules)
    created_at: int = 0

    @classmethod
    def load(cls, agent_id: str) -> "AgentConfig":
        from .runtime_matrix import migrate_legacy_kind, validate_triple

        path = agent_yml_path(agent_id)
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        pc = raw.get("puffo_core") or {}
        rt = raw.get("runtime") or {}
        triggers = raw.get("triggers") or {}

        # Legacy kind names (chat-only, sdk) auto-migrate with a
        # one-time WARNING; current spellings are written on next save.
        kind = migrate_legacy_kind(
            rt.get("kind", "chat-local"), agent_id=agent_id,
        )
        provider = rt.get("provider", "")
        harness = rt.get("harness", "claude-code")

        # Fail fast on invalid triples (e.g. gemini-cli + anthropic,
        # or reserved kind=cli-sandbox).
        result = validate_triple(kind, provider, harness)
        if not result.ok:
            raise RuntimeError(
                f"agent {agent_id!r}: invalid runtime config — {result.error}"
            )

        return cls(
            id=raw.get("id", agent_id),
            state=raw.get("state", "running"),
            display_name=raw.get("display_name", ""),
            avatar_url=raw.get("avatar_url", ""),
            role=raw.get("role", ""),
            role_short=raw.get("role_short", ""),
            puffo_core=PuffoCoreConfig(
                server_url=pc.get("server_url") or DEFAULT_PUFFO_SERVER_URL,
                slug=pc.get("slug", ""),
                device_id=pc.get("device_id", ""),
                space_id=pc.get("space_id", ""),
                operator_slug=pc.get("operator_slug", ""),
            ),
            runtime=RuntimeConfig(
                kind=kind,
                provider=provider,
                model=rt.get("model", ""),
                api_key=rt.get("api_key", ""),
                allowed_tools=list(rt.get("allowed_tools") or []),
                docker_image=rt.get("docker_image", ""),
                docker_memory_limit=rt.get("docker_memory_limit", ""),
                docker_memory_reservation=rt.get("docker_memory_reservation", ""),
                permission_mode=rt.get("permission_mode", "bypassPermissions"),
                harness=harness,
                max_turns=int(rt.get("max_turns", 10)),
            ),
            profile=raw.get("profile", "profile.md"),
            memory_dir=raw.get("memory_dir", "memory"),
            workspace_dir=raw.get("workspace_dir", "workspace"),
            triggers=TriggerRules(
                on_mention=bool(triggers.get("on_mention", True)),
                on_dm=bool(triggers.get("on_dm", True)),
            ),
            created_at=int(raw.get("created_at", 0)),
        )

    def save(self) -> None:
        path = agent_yml_path(self.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "id": self.id,
            "state": self.state,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "role": self.role,
            "role_short": self.role_short,
            "created_at": self.created_at,
            "puffo_core": asdict(self.puffo_core),
            "runtime": asdict(self.runtime),
            "profile": self.profile,
            "memory_dir": self.memory_dir,
            "workspace_dir": self.workspace_dir,
            "triggers": asdict(self.triggers),
        }
        _atomic_write_yaml(path, data)

    def resolve_profile_path(self) -> Path:
        return self._resolve(self.profile)

    def resolve_memory_dir(self) -> Path:
        return self._resolve(self.memory_dir)

    def resolve_workspace_dir(self) -> Path:
        return self._resolve(self.workspace_dir)

    def resolve_claude_dir(self) -> Path:
        """Always ``<workspace>/.claude`` — adapter-owned."""
        return self.resolve_workspace_dir() / ".claude"

    def _resolve(self, rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        if p.is_absolute():
            return p
        return agent_dir(self.id) / p


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
    # Claude-side auth health, independent of ``status``. "ok" = last
    # refresh-ping smoke test passed; "auth_failed" = adapter saw 401 /
    # authentication_error; "unknown" = no probe yet.
    health: str = "unknown"  # ok | auth_failed | unknown

    @classmethod
    def load(cls, agent_id: str) -> "RuntimeState | None":
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
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(tmp, path)


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
        entry.name for entry in root.iterdir()
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
    try:
        proc = psutil.Process(pid)
        tokens = [t or "" for t in proc.cmdline()]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    # Match by token name prefix to cover script-shim, .exe, and
    # ``python -m puffo_agent.portal.cli`` invocations.
    def _is_ours(token: str) -> bool:
        low = Path(token).name.lower()
        return (
            low.startswith("puffo-agent")
            or low.startswith("puffo_agent")
        )
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


# ─────────────────────────────────────────────────────────────────────────────
# Atomic YAML write
# ─────────────────────────────────────────────────────────────────────────────


def _atomic_write_yaml(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        # allow_unicode keeps CJK/emoji/accented display_names readable.
        yaml.safe_dump(
            data, f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
    os.replace(tmp, path)
