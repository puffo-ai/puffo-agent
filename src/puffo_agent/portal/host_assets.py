"""Host credential, skill, plugin, and MCP projection into agent homes."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Files copied from the operator's $HOME into a per-agent virtual
# $HOME on first use. Lift OAuth-essential files only.
# Note: ``.claude.json`` is a sibling of the ``.claude/`` dir; Claude
# CLI reads it from ``$HOME/.claude.json`` so we mirror that layout.
# ``.credentials.json`` is intentionally excluded — set up separately
# via ``sync_host_claude_code_auth_view`` so every agent tracks live OAuth
# state (matches cli-docker's bind-mount model).
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


def _is_current_user_home(path: Path) -> bool:
    """Whether ``path`` names the real account home for this process.

    Keychain materialization is a host integration, not part of copying from
    an arbitrary source directory. Keeping that distinction prevents callers
    using an import/test directory from having it overwritten with live
    account credentials.
    """
    return path.expanduser().resolve() == Path.home().expanduser().resolve()


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


def sync_host_claude_code_auth_view(
    host_home: Path,
    agent_home: Path,
    *,
    write_view: Callable[[Path, str], None] = _write_credential_view,
    sync_credentials: Callable[[Path], bool] = _sync_credentials_from_keychain,
    is_current_home: Callable[[Path], bool] = _is_current_user_home,
) -> str:
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
    if is_current_home(host_home):
        sync_credentials(host_home)
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
        write_view(agent_creds, view_blob)
    except OSError:
        return "write-failed"
    return "view (migrated-from-symlink)" if migrated else "view"


def sync_host_codex_auth_view(
    host_home: Path,
    agent_codex_home: Path,
    *,
    write_view: Callable[[Path, str], None] = _write_credential_view,
) -> str:
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
        write_view(agent_auth, view_blob)
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
                entry["http_headers"] = {str(k): str(v) for k, v in headers.items()}
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
    src: Path,
    dst_root: Path,
    marker_body: str,
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
                    marker_body,
                    encoding="utf-8",
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
    host_home: Path,
    agent_home: Path,
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
    host_home: Path,
    project_dir: Path,
    *,
    extra_servers: dict | None = None,
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
                host_servers = json.loads(raw).get("mcpServers") or {}
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
