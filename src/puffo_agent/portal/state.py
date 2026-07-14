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

from ..limits import MAX_INLINE_MESSAGE_CHARS, MESSAGE_SEGMENT_CHARS


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
    for cli-local / sdk agents."""
    return home_dir() / "shared"


# OAuth-essential files seeded into the per-agent virtual $HOME.
# ``.claude.json`` is a sibling of ``.claude/``. ``.credentials.json``
# excluded; sync_host_claude_code_auth_view owns live OAuth state.
_CLAUDE_HOME_SEED_PATHS = (
    ".claude/settings.json",
    ".claude.json",
)


def seed_claude_home(host_home: Path, agent_home: Path) -> bool:
    """Seed a per-agent virtual ``$HOME`` from the operator's real
    ``$HOME``. Idempotent — never overwrites an existing file.

    ``.credentials.json`` is set up separately via
    ``sync_host_claude_code_auth_view``. Returns True if any file was copied.
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
    Claude Code Keychain entry when missing or stale.

    Claude Code stores OAuth in Keychain instead of the file on macOS;
    this bridges to the shared-file path used by every other agent.
    Called on every ``sync_host_claude_code_auth_view`` invocation so
    refreshed tokens propagate. Returns True if the file was written.
    """
    import platform
    if platform.system() != "Darwin":
        return False
    try:
        from ..macos.keychain import read_keychain_blob
        keychain = read_keychain_blob(timeout=5)
        if not keychain.ok or not keychain.blob:
            return False
        keychain_raw = keychain.blob
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


def sanitize_claude_code_auth_blob(blob: str) -> str | None:
    """Strip ``claudeAiOauth.refreshToken`` from the host blob for the
    agent view. ``None`` on unparseable JSON — never ship a blob we
    can't vet. Claude Code tolerates the missing field: uses the
    access token, 401s cleanly rather than attempting a refresh."""
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    oauth = data.get("claudeAiOauth")
    if isinstance(oauth, dict):
        oauth.pop("refreshToken", None)
    return json.dumps(data)


def sanitize_codex_auth_blob(blob: str) -> str | None:
    """Blank (not remove) ``tokens.refresh_token`` for the agent view.
    ``None`` on unparseable JSON. Codex serde is non-optional on this
    field — dropping it crashes; empty string parses, ``codex login
    status`` reports logged-in, and a refresh attempt fails server-side
    without consuming the real (single-use) token."""
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        tokens["refresh_token"] = ""
    return json.dumps(data)


def _write_credential_view(target: Path, blob: str) -> None:
    """Atomic tmp+rename write at ``target``, mode 0600. ``os.replace``
    swaps the path entry, so a legacy symlink at ``target`` is replaced,
    not followed — the host file it pointed at stays untouched."""
    import stat
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.tmp.{os.getpid()}"
    tmp.write_text(blob, encoding="utf-8")
    try:
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(tmp, target)


def sync_host_claude_code_auth_view(host_home: Path, agent_home: Path) -> str:
    """Write a refresh-token-free view of the host's
    ``.credentials.json`` into the agent's virtual ``$HOME`` — only
    the daemon holds the rotating RT, so agents can't race a refresh
    into a token-family revocation. Idempotent + self-healing;
    legacy symlinks migrated in place. Returns ``"view"``,
    ``"view (fresh)"``, ``"view (migrated-from-symlink)"``,
    ``"unparseable-host-file"``, ``"write-failed"``, or ``"no-host-file"``.
    """
    host_creds = host_home / ".claude" / ".credentials.json"
    agent_creds = agent_home / ".claude" / ".credentials.json"
    _sync_credentials_from_keychain(host_home)
    try:
        host_blob = host_creds.read_text(encoding="utf-8")
    except OSError:
        return "no-host-file"
    view_blob = sanitize_claude_code_auth_blob(host_blob)
    if view_blob is None:
        return "unparseable-host-file"

    migrated = agent_creds.is_symlink()
    if not migrated:
        try:
            if agent_creds.read_text(encoding="utf-8") == view_blob:
                return "view (fresh)"
        except OSError:
            pass
    try:
        _write_credential_view(agent_creds, view_blob)
    except OSError:
        return "write-failed"
    return "view (migrated-from-symlink)" if migrated else "view"


def sync_host_codex_auth_view(host_home: Path, agent_codex_home: Path) -> str:
    """Codex counterpart of ``sync_host_claude_code_auth_view``; RT
    blanked, not removed (see ``sanitize_codex_auth_blob``). Same
    return taxonomy."""
    host_auth = host_home / ".codex" / "auth.json"
    agent_auth = agent_codex_home / "auth.json"
    try:
        host_blob = host_auth.read_text(encoding="utf-8")
    except OSError:
        return "no-host-file"
    view_blob = sanitize_codex_auth_blob(host_blob)
    if view_blob is None:
        return "unparseable-host-file"

    migrated = agent_auth.is_symlink()
    if not migrated:
        try:
            if agent_auth.read_text(encoding="utf-8") == view_blob:
                return "view (fresh)"
        except OSError:
            pass
    try:
        _write_credential_view(agent_auth, view_blob)
    except OSError:
        return "write-failed"
    return "view (migrated-from-symlink)" if migrated else "view"


def read_host_codex_mcp_servers(host_home: Path) -> dict[str, dict]:
    """Return host codex ``[mcp_servers.*]`` as a per-name spec dict.
    Honours ``$CODEX_HOME``; ``{}`` on missing / unreadable / malformed.
    Drops entries that match neither stdio nor http/sse shape."""
    import tomllib
    codex_home_env = os.environ.get("CODEX_HOME")
    codex_home = Path(codex_home_env) if codex_home_env else host_home / ".codex"
    host_config = codex_home / "config.toml"
    if not host_config.exists():
        return {}
    try:
        with host_config.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return {}
    raw = data.get("mcp_servers")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        raw_env = spec.get("env")
        env = dict(raw_env) if isinstance(raw_env, dict) else {}
        url = spec.get("url")
        if isinstance(url, str) and url:
            entry: dict = {"url": url, "env": env}
            bearer = spec.get("bearer_token_env_var")
            if isinstance(bearer, str) and bearer:
                entry["bearer_token_env_var"] = bearer
            headers = spec.get("http_headers")
            if isinstance(headers, dict) and headers:
                entry["http_headers"] = {
                    str(k): str(v) for k, v in headers.items()
                }
            out[name] = entry
            continue
        cmd = spec.get("command")
        if not isinstance(cmd, str) or not cmd:
            continue
        raw_args = spec.get("args")
        args = list(raw_args) if isinstance(raw_args, list) else []
        out[name] = {"command": cmd, "args": args, "env": env}
    return out


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
# ``/home/agent/`` is handled separately because it IS valid inside;
# ``/opt/puffoagent-pkg`` stays resolvable (prefixes are more specific).
_HOST_LOCAL_COMMAND_PREFIXES = (
    "/Users/",
    "/tmp/",
    "/var/folders/",
    "/opt/homebrew/",
    "/opt/local/",
    "/Volumes/",
    "/private/",
)


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


def _host_local_token(cfg: dict) -> str | None:
    """First token in an MCP server cfg that points at a host-only path,
    or ``None`` when everything resolves inside the container. Scans
    ``args`` too — a bare ``npx`` / ``uvx`` command often hides the host
    path in an argument."""
    if not isinstance(cfg, dict):
        return None
    cmd = cfg.get("command") or ""
    if isinstance(cmd, str) and _looks_host_local_command(cmd):
        return cmd
    for arg in cfg.get("args") or []:
        # /tmp exists in the container: a /tmp arg is a valid output path.
        if (
            isinstance(arg, str)
            and not arg.startswith("/tmp/")
            and _looks_host_local_command(arg)
        ):
            return arg
    return None


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
    merged = 0
    for name, cfg in host_servers.items():
        token = _host_local_token(cfg)
        if token is not None:
            unreachable.append((name, token))
            continue
        agent_servers[name] = cfg
        merged += 1
    agent_data["mcpServers"] = agent_servers

    try:
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = agent_path.with_suffix(agent_path.suffix + ".tmp")
        tmp.write_text(json.dumps(agent_data, indent=2), encoding="utf-8")
        os.replace(tmp, agent_path)
    except OSError:
        return 0, []
    return merged, unreachable


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
    # Claude Code has shipped both shapes (dict + list); pass through unchanged.
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
    merged = 0
    for name, cfg in host_servers.items():
        token = _host_local_token(cfg)
        if token is not None:
            unreachable.append((name, token))
            continue
        merged_servers[name] = cfg
        merged += 1

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
    return merged, unreachable


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
    # cli-docker memory caps: one runaway claude must not drain the VM's
    # swap. Empty string = opt out; per-agent overrides on ``runtime``.
    docker_memory_limit: str = "1.5g"
    docker_memory_reservation: str = "500m"
    # Inbound redaction: over-limit envelope bodies become a placeholder
    # (id, length, segments, preview); the agent pages via get_post_segment.
    # Only the prompt view is redacted, messages.db keeps the original.
    # Guards session-lifetime growth; 16000 inlines typical code/log pastes.
    max_inline_message_chars: int = MAX_INLINE_MESSAGE_CHARS
    segment_chars: int = MESSAGE_SEGMENT_CHARS
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    data_service: "DataServiceConfig" = field(
        default_factory=lambda: DataServiceConfig(),
    )
    rpc_service: "RpcServiceConfig" = field(
        default_factory=lambda: RpcServiceConfig(),
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
            max_inline_message_chars=int(
                raw.get("max_inline_message_chars", MAX_INLINE_MESSAGE_CHARS)
            ),
            segment_chars=int(raw.get("segment_chars", MESSAGE_SEGMENT_CHARS)),
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


## Default puffo-core server; per-agent override via puffo_core.server_url.
## api.puffo.ai is platform-internal; clients use chat.puffo.ai/relay.
DEFAULT_PUFFO_SERVER_URL = "https://chat.puffo.ai/relay"


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
    # codex (cli-local) sandbox policy: read-only | workspace-write |
    # danger-full-access. Default leaves codex's sandbox fully open.
    sandbox: str = "danger-full-access"
    # codex (cli-local) per-turn wall-clock budget in seconds; raise for
    # agents running long reasoning/complex tasks.
    task_timeout_seconds: float = 600.0
    # Agent engine (CLI kinds only): ``claude-code`` (stream-json + resume +
    # puffo MCP), ``hermes`` (one-shot ``hermes chat -q``), ``gemini-cli``
    # (declared, unimplemented). Hermes OAuth bills to Anthropic
    # extra_usage, not a Claude subscription.
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
    # role = long-form (<=140 chars); role_short = client chip label.
    # Synced to PATCH /identities/self on edit; server derives role_short
    # when omitted.
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
    # Operator-picked template ids installed at spawn AFTER host-sync,
    # de-duped against whatever host already provides.
    desired_skills: list[str] = field(default_factory=list)
    desired_mcps: list[str] = field(default_factory=list)
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
                auto_accept_space_invitations=bool(
                    pc.get("auto_accept_space_invitations", False)
                ),
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
                sandbox=rt.get("sandbox", "danger-full-access"),
                task_timeout_seconds=float(rt.get("task_timeout_seconds", 600.0)),
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
            desired_skills=list(raw.get("desired_skills") or []),
            desired_mcps=list(raw.get("desired_mcps") or []),
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
            "desired_skills": list(self.desired_skills),
            "desired_mcps": list(self.desired_mcps),
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
    # Worker-side health, independent of ``status``:
    #   "ok"                  - clean turn / cleared red
    #   "in_progress"         - turn mid-flight; overrides sticky reds
    #   "auth_failed"         - adapter saw 401; cleared by refresh success
    #   "api_error_abandoned" - kick-retry exhausted; cleared on next good turn
    #   "refresh_broken"      - N consecutive refresh failures; cleared by next
    #                           REFRESHED; never overwrites the reds above
    #   "unhandled_error"     - uncategorised turn raise; cleared on next good turn
    #   "codex_thread_wedged" - thread rotated (timeouts/failures/thread-limit);
    #                           auto-recovers; never overwrites stronger reds
    #   "unknown"             - no probe yet
    health: str = "unknown"  # ok | in_progress | auth_failed | api_error_abandoned | refresh_broken | unhandled_error | codex_thread_wedged | unknown

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
            data, f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
    os.replace(tmp, path)
