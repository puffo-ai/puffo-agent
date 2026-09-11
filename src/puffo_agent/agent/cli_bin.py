"""Resolve host CLI binaries with broader-than-PATH search.

A daemon started by a LaunchAgent (macOS) / Windows service / before a
shell-profile refresh inherits a narrow, stale PATH that misses
npm-global / scoop / nvm / fnm / volta / homebrew installs. The
resolver layers, in order:

1. ``$PUFFO_<NAME>_BIN`` env var — explicit operator override.
2. An in-memory cache for this daemon's lifetime.
3. ``shutil.which`` against the process PATH.
4. ``shutil.which`` against the user's *real* PATH, reconstructed from
   the persistent Machine+User registry env (Windows) or a login shell
   (POSIX) — catches installs the narrow process PATH missed.
5. OS-specific bundle paths (desktop-app installs).
6. The validated ``resolved_clis.json`` disk cache as a last resort.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Resolved-path caches: in-memory for this daemon's lifetime, plus a
# last-resort JSON fallback for installs that later disappear from PATH.
_resolve_memcache: dict[str, str] = {}
_real_path_cache: str | None = None


@dataclass(frozen=True)
class HarnessReadiness:
    """Explicit three-state readiness for one harness.

    ``state`` is one of:

    * ``ready`` — resolved and verified usable.
    * ``degraded`` — startable, but with a caveat the caller should surface
      (typically: this harness has no checkable local credential store, so
      usability is unknown until a turn runs).
    * ``unavailable`` — cannot run (no binary, or credentials known absent).

    ``reason`` is empty for ``ready`` and machine-readable otherwise.
    ``legacy`` reproduces exactly the string the portal's frozen
    ``cli_tools`` field carried before this model existed — including its
    lie of reporting an unknown credential state as ``ready``. That wire
    contract stays byte-stable for old consumers; new consumers read
    ``state``/``reason`` and must not re-derive them from ``legacy``.
    """

    state: str
    reason: str
    legacy: str


def harness_readiness(
    resolver: Callable[[], str | None],
    credential_check: Callable[[], bool] | None,
) -> HarnessReadiness:
    """Compute three-state readiness for a host CLI harness.

    Pass ``credential_check=None`` when the harness has no local
    credential store this daemon can inspect — the honest answer is
    ``degraded``/``credentials_unknown``, never ``ready``.
    """
    try:
        path = resolver()
    except Exception:
        path = None
    if not path:
        return HarnessReadiness("unavailable", "not_installed", "not_installed")
    if credential_check is None:
        return HarnessReadiness("degraded", "credentials_unknown", "ready")
    try:
        has_credentials = credential_check()
    except Exception:
        # The check itself failed; that is not evidence of missing
        # credentials. Legacy reported need_login here — preserved.
        return HarnessReadiness("degraded", "credential_check_error", "need_login")
    if has_credentials:
        return HarnessReadiness("ready", "", "ready")
    return HarnessReadiness("unavailable", "need_login", "need_login")


def cli_tool_status(
    resolver: Callable[[], str | None], credential_check: Callable[[], bool],
) -> str:
    """Return ``not_installed``, ``need_login``, or ``ready`` for a host CLI.

    Legacy string view of ``harness_readiness``; kept for callers bound to
    the frozen portal vocabulary.
    """
    return harness_readiness(resolver, credential_check).legacy


def resolve_codex_bin() -> str | None:
    """Return the absolute path of the ``codex`` binary, or ``None``."""
    return _resolve("codex", "PUFFO_CODEX_BIN", _codex_bundle_paths())


def resolve_claude_bin() -> str | None:
    """Return the absolute path of the ``claude`` binary, or ``None``."""
    return _resolve("claude", "PUFFO_CLAUDE_BIN", _claude_bundle_paths())


def resolve_docker_bin() -> str | None:
    """Return the absolute path of the ``docker`` binary, or ``None``."""
    return _resolve("docker", "PUFFO_DOCKER_BIN", _docker_bundle_paths())


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


def resolve_opencode_bin() -> str | None:
    """Return the absolute path of the ``opencode`` binary, or ``None``.

    ``PUFFO_OPENCODE_BIN`` is an expert deployment override for daemons whose
    inherited PATH predates the OpenCode installation.  The bundle fallback
    covers OpenCode's standard per-user installer location for launchd and
    other services with a narrow PATH.
    """
    return _resolve(
        "opencode", "PUFFO_OPENCODE_BIN", _opencode_bundle_paths(),
    )


def resolve_pi_bin() -> str | None:
    """Return the absolute path of the Pi coding-agent binary, or ``None``."""
    return _resolve("pi", "PUFFO_PI_BIN", [])


def pi_has_credentials() -> bool:
    """Ask the resolved Pi binary whether any host credential is ready."""
    executable = resolve_pi_bin()
    if not executable:
        return False
    from .pi_auth import pi_has_credentials as native_pi_has_credentials

    return native_pi_has_credentials(executable)


def opencode_has_accessible_models() -> bool:
    """Whether native OpenCode exposes at least one currently usable model."""
    executable = resolve_opencode_bin()
    if not executable:
        return False
    from .opencode_auth import list_opencode_models

    return bool(list_opencode_models(executable))


def _resolve(name: str, env_var: str, bundle_paths: list[Path]) -> str | None:
    # 1. Explicit operator override — always wins, read live.
    env_override = os.environ.get(env_var)
    if env_override:
        p = Path(env_override).expanduser()
        if _is_executable_file(p):
            return str(p)
    # 2. The daemon-lifetime cache avoids repeated path reconstruction.
    cached = _resolve_memcache.get(name)
    if cached and _is_executable_file(Path(cached)):
        return cached
    # 3-5. Prefer live and known-install lookups over the user-writable
    # disk cache, especially for privileged Docker invocations.
    resolved = shutil.which(name)
    if not resolved:
        resolved = shutil.which(name, path=_real_path())
    if not resolved:
        resolved = _first_executable(bundle_paths)
    # 6. Last-resort restart cache. Executability is revalidated so a
    # stale or non-executable entry cannot shadow a working live lookup.
    if not resolved:
        saved = _read_path_cache().get(name)
        if saved and _is_executable_file(Path(saved)):
            resolved = saved
    if resolved:
        _resolve_memcache[name] = resolved
        _write_path_cache(name, resolved)
    return resolved


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _first_executable(paths: list[Path]) -> str | None:
    for cand in paths:
        if _is_executable_file(cand):
            return str(cand)
    return None


# ── Windows shim launch normalization ────────────────────────────────


def normalize_launch_argv(executable: str) -> list[str]:
    """Return the argv prefix that launches ``executable`` on this host.

    POSIX passes the executable through unchanged. Windows maps an
    extensionless path to an existing ``.exe`` / ``.cmd`` / ``.bat`` /
    ``.ps1`` sibling and wraps the interpreter-backed shims so a direct
    ``asyncio.create_subprocess_exec`` can run them: ``.cmd`` / ``.bat``
    through ``cmd.exe /c``, ``.ps1`` through ``powershell.exe``. Every
    returned element is a single argv entry, so paths containing spaces
    survive intact.
    """
    if sys.platform != "win32":
        return [executable]
    resolved = _windows_launch_path(executable)
    lower = resolved.lower()
    if lower.endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", resolved]
    if lower.endswith(".ps1"):
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            resolved,
        ]
    return [resolved]


def _windows_launch_path(executable: str) -> str:
    """Prefer an existing sibling for an extensionless Windows executable.

    Order: ``.exe``, ``.cmd``, ``.bat``, ``.ps1``. A path that already
    carries an extension — or whose sibling set has no match — is used
    as-is (the resolver-miss fallback launches the bare name directly).
    """
    path = Path(executable).expanduser()
    if path.suffix:
        return str(path)
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            return str(candidate)
    return str(path)


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
    # The standard Windows install is the npm-global shim set under
    # ``%APPDATA%\npm``; Anthropic doesn't currently ship a desktop app
    # that bundles the ``claude`` CLI the way Codex.app does, so the
    # app-root paths are defensive coverage.
    if sys.platform == "darwin":
        return _expand(
            "/Applications/Claude.app/Contents/Resources/claude",
            "~/Applications/Claude.app/Contents/Resources/claude",
        )
    if sys.platform == "win32":
        return _expand(
            r"%APPDATA%\npm\claude.exe",
            r"%APPDATA%\npm\claude.cmd",
            r"%APPDATA%\npm\claude.ps1",
            r"%LOCALAPPDATA%\Programs\claude\claude.exe",
            r"%LOCALAPPDATA%\Programs\Claude\claude.exe",
            r"%PROGRAMFILES%\Claude\claude.exe",
        )
    return _expand(
        "/opt/Claude/claude",
        "/opt/claude/claude",
        "/usr/lib/claude/claude",
    )


def _docker_bundle_paths() -> list[Path]:
    if sys.platform == "darwin":
        return _expand(
            "/Applications/Docker.app/Contents/Resources/bin/docker",
            "~/Applications/Docker.app/Contents/Resources/bin/docker",
            "/usr/local/bin/docker",
            "/opt/homebrew/bin/docker",
        )
    if sys.platform == "win32":
        return _expand(
            r"%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe",
            r"%PROGRAMFILES%\Docker\Docker\resources\bin\docker.exe",
            r"%PROGRAMDATA%\DockerDesktop\version-bin\docker.exe",
        )
    return _expand(
        "/usr/bin/docker",
        "/usr/local/bin/docker",
        "/snap/bin/docker",
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


def _opencode_bundle_paths() -> list[Path]:
    """OpenCode's documented per-user installer location."""
    executable = "opencode.exe" if sys.platform == "win32" else "opencode"
    return [Path.home() / ".opencode" / "bin" / executable]


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
