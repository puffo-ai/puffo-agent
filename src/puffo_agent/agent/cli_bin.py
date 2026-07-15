"""Resolve ``codex`` and ``claude`` binaries with broader-than-PATH
search.

A daemon started by a LaunchAgent (macOS) / Windows service / before a
shell-profile refresh inherits a narrow, stale PATH that misses
npm-global / scoop / nvm / fnm / volta / homebrew installs. The
resolver layers, in order:

1. ``$PUFFO_<NAME>_BIN`` env var — explicit operator override.
2. Caches — an in-memory one for this daemon's lifetime and a
   ``resolved_clis.json`` file so a restart skips the (slow) PATH
   reconstruction; both validated against the filesystem.
3. ``shutil.which`` against the process PATH.
4. ``shutil.which`` against the user's *real* PATH, reconstructed from
   the persistent Machine+User registry env (Windows) or a login shell
   (POSIX) — catches installs the narrow process PATH missed.
5. OS-specific bundle paths (desktop-app installs).

Returns absolute path on hit, ``None`` on full miss. Callers
distinguish "binary missing" (raise / report to status) from
"resolver hit" (use returned path).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Resolved-path caches: in-memory for this daemon's lifetime, plus a
# JSON file so a restart skips the (slow) real-PATH reconstruction.
_resolve_memcache: dict[str, str] = {}
_real_path_cache: str | None = None


def resolve_codex_bin() -> str | None:
    """Return the absolute path of the ``codex`` binary, or ``None``."""
    return _resolve("codex", "PUFFO_CODEX_BIN", _codex_bundle_paths())


def resolve_claude_bin() -> str | None:
    """Return the absolute path of the ``claude`` binary, or ``None``."""
    return _resolve("claude", "PUFFO_CLAUDE_BIN", _claude_bundle_paths())


def resolve_hermes_bin() -> str | None:
    """Return the absolute path of the ``hermes`` binary, or ``None``.

    The upstream installer (``install.sh`` / ``install.ps1``) drops
    the launcher in ``~/.local/bin`` on POSIX and
    ``%LOCALAPPDATA%\\hermes\\bin`` on Windows; both are usually on
    ``$PATH`` after the post-install ``source ~/.bashrc`` step.
    Bundle-path fallback covers the LaunchAgent narrow-PATH case
    that bit Codex.app the same way.
    """
    return _resolve("hermes", "PUFFO_HERMES_BIN", _hermes_bundle_paths())


def _resolve(name: str, env_var: str, bundle_paths: list[Path]) -> str | None:
    # 1. Explicit operator override — always wins, read live.
    env_override = os.environ.get(env_var)
    if env_override:
        p = Path(env_override).expanduser()
        if p.is_file():
            return str(p)
    # 2. Caches (in-memory, then on-disk) — validated against the FS so
    #    an uninstalled / moved binary falls through to a fresh lookup.
    cached = _resolve_memcache.get(name)
    if cached and Path(cached).is_file():
        return cached
    saved = _read_path_cache().get(name)
    if saved and Path(saved).is_file():
        _resolve_memcache[name] = saved
        return saved
    # 3-5. Live lookup: process PATH → the user's real (reconstructed)
    #      PATH → OS bundle paths. Persist whatever resolves.
    resolved = shutil.which(name)
    if not resolved:
        resolved = shutil.which(name, path=_real_path())
    if not resolved:
        resolved = _first_existing(bundle_paths)
    if resolved:
        _resolve_memcache[name] = resolved
        _write_path_cache(name, resolved)
    return resolved


def _first_existing(paths: list[Path]) -> str | None:
    for cand in paths:
        if cand.is_file():
            return str(cand)
    return None


# ── Real-PATH reconstruction (broader than the daemon's process PATH) ──


def _real_path() -> str:
    """The user's actual PATH, reconstructed + cached once. Falls back
    to the process PATH if the reconstruction fails."""
    global _real_path_cache
    if _real_path_cache is None:
        base = os.environ.get("PATH", "")
        extra = (
            _windows_persistent_path() if sys.platform == "win32"
            else _login_shell_path()
        )
        _real_path_cache = _merge_path(base, extra)
    return _real_path_cache


def _windows_persistent_path() -> str:
    """Machine + User ``Path`` from the registry, env-expanded — what a
    fresh shell sees, not the service's stale process PATH."""
    script = (
        "[Environment]::ExpandEnvironmentVariables("
        "([Environment]::GetEnvironmentVariable('Path','Machine') + ';' + "
        "[Environment]::GetEnvironmentVariable('Path','User')))"
    )
    return _run_capture(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
    )


def _login_shell_path() -> str:
    """The PATH a login + interactive shell sets — picks up nvm / fnm /
    volta / homebrew sourced from the user's profile + rc files."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    out = _run_capture([shell, "-ilc", 'printf "P=%s" "$PATH"'])
    for line in out.splitlines():
        if line.startswith("P="):
            return line[2:]
    return ""


def _run_capture(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return r.stdout.strip()
    except Exception:
        return ""


def _merge_path(*values: str) -> str:
    """Concatenate PATH strings, dropping empties + duplicates
    (case-insensitive on Windows)."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        for seg in value.split(os.pathsep):
            seg = seg.strip()
            if not seg:
                continue
            key = seg.lower() if sys.platform == "win32" else seg
            if key in seen:
                continue
            seen.add(key)
            out.append(seg)
    return os.pathsep.join(out)


# ── Resolved-path cache file ──────────────────────────────────────────


def _cache_file() -> Path:
    from ..portal.state import home_dir  # lazy — avoid an import cycle

    return home_dir() / "resolved_clis.json"


def _read_path_cache() -> dict:
    try:
        data = json.loads(_cache_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_path_cache(name: str, path: str) -> None:
    cache = _read_path_cache()
    if cache.get(name) == path:
        return
    cache[name] = path
    target = _cache_file()
    tmp = target.with_suffix(".json.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass  # best-effort cache


def _codex_bundle_paths() -> list[Path]:
    if sys.platform == "darwin":
        # ChatGPT.app first — newer builds bundle codex there (moved out
        # of Codex.app), so a leftover Codex.app copy is the stale one.
        return _expand(
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            "~/Applications/ChatGPT.app/Contents/Resources/codex",
            "/Applications/Codex.app/Contents/Resources/codex",
            "~/Applications/Codex.app/Contents/Resources/codex",
        )
    if sys.platform == "win32":
        return _expand(
            r"%LOCALAPPDATA%\Programs\codex\codex.exe",
            r"%LOCALAPPDATA%\Programs\Codex\codex.exe",
            r"%PROGRAMFILES%\Codex\codex.exe",
        )
    # Linux — common bundled-app install roots.
    return _expand(
        "/opt/Codex/codex",
        "/opt/codex/codex",
        "/usr/lib/codex/codex",
    )


def _claude_bundle_paths() -> list[Path]:
    # Anthropic doesn't currently ship a desktop app that bundles the
    # ``claude`` CLI the way Codex.app does; defensive paths cover
    # the case where they start to.
    if sys.platform == "darwin":
        return _expand(
            "/Applications/Claude.app/Contents/Resources/claude",
            "~/Applications/Claude.app/Contents/Resources/claude",
        )
    if sys.platform == "win32":
        return _expand(
            r"%LOCALAPPDATA%\Programs\claude\claude.exe",
            r"%LOCALAPPDATA%\Programs\Claude\claude.exe",
            r"%PROGRAMFILES%\Claude\claude.exe",
        )
    return _expand(
        "/opt/Claude/claude",
        "/opt/claude/claude",
        "/usr/lib/claude/claude",
    )


def _hermes_bundle_paths() -> list[Path]:
    """Hermes' Windows installer puts the launcher inside its private
    venv (``%LOCALAPPDATA%\\hermes\\hermes-agent\\venv\\Scripts\\hermes.exe``)
    and prepends that ``Scripts`` dir to user PATH. POSIX flavours
    drop a ``hermes`` shim in ``~/.local/bin``. The bundle paths
    cover both layouts so a daemon started before the post-install
    PATH refresh (launchd / scheduled task / new shell) still finds
    the binary.
    """
    if sys.platform == "win32":
        return _expand(
            r"%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\hermes.exe",
            r"%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\hermes.cmd",
            r"%LOCALAPPDATA%\hermes\bin\hermes.exe",
            r"%USERPROFILE%\.local\bin\hermes.exe",
        )
    # macOS + Linux + WSL2 — installer default + a few common venv
    # locations operators sometimes pip-install into.
    return _expand(
        "~/.local/bin/hermes",
        "/usr/local/bin/hermes",
        "/opt/homebrew/bin/hermes",
    )


def _expand(*paths: str) -> list[Path]:
    return [Path(os.path.expandvars(p)).expanduser() for p in paths]


# ── Credential presence (UI status discrimination) ───────────────────


def claude_has_credentials(home: Path | None = None) -> bool:
    """True if Claude OAuth credentials exist on this host. On macOS
    the canonical store is a Claude Code Keychain entry; on
    Linux/Windows it's ``~/.claude/.credentials.json``."""
    h = home if home is not None else Path.home()
    if (h / ".claude" / ".credentials.json").exists():
        return True
    if sys.platform == "darwin":
        from ..macos.keychain import KEYCHAIN_SERVICES
        try:
            for service in KEYCHAIN_SERVICES:
                r = subprocess.run(
                    ["security", "find-generic-password", "-s", service],
                    capture_output=True, timeout=2,
                )
                if r.returncode == 0:
                    return True
            return False
        except Exception:
            return False
    return False


def codex_has_credentials(home: Path | None = None) -> bool:
    """True if Codex OAuth credentials exist at ``~/.codex/auth.json``."""
    h = home if home is not None else Path.home()
    return (h / ".codex" / "auth.json").exists()
