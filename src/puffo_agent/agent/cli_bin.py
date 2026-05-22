"""Resolve ``codex`` and ``claude`` binaries with broader-than-PATH
search.

LaunchAgent (macOS) and Windows service contexts run with a narrow
PATH that misses ``/opt/homebrew/bin`` (Apple Silicon Homebrew) and
ignores ``.app`` bundles entirely. Operators who installed Codex via
the desktop app (binary tucked inside
``/Applications/Codex.app/Contents/Resources/codex``) hit
``[Errno 2] No such file or directory: 'codex'`` even though the
binary exists.

The resolver layers three lookups so the operator doesn't need to
fiddle with ``launchd`` plists or symlink the binary into ``/usr/local/bin``:

1. ``$PUFFO_<NAME>_BIN`` env var — explicit operator override.
2. ``shutil.which(<name>)`` — npm / brew / scoop install.
3. OS-specific bundle paths — Codex.app on macOS, %LOCALAPPDATA%
   on Windows, /opt on Linux.

Returns absolute path on hit, ``None`` on full miss. Callers
distinguish "binary missing" (raise / report to status) from
"resolver hit" (use returned path).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resolve_codex_bin() -> str | None:
    """Return the absolute path of the ``codex`` binary, or ``None``."""
    return _resolve("codex", "PUFFO_CODEX_BIN", _codex_bundle_paths())


def resolve_claude_bin() -> str | None:
    """Return the absolute path of the ``claude`` binary, or ``None``."""
    return _resolve("claude", "PUFFO_CLAUDE_BIN", _claude_bundle_paths())


def _resolve(name: str, env_var: str, bundle_paths: list[Path]) -> str | None:
    env_override = os.environ.get(env_var)
    if env_override:
        p = Path(env_override).expanduser()
        if p.is_file():
            return str(p)
    on_path = shutil.which(name)
    if on_path:
        return on_path
    for cand in bundle_paths:
        if cand.is_file():
            return str(cand)
    return None


def _codex_bundle_paths() -> list[Path]:
    if sys.platform == "darwin":
        return _expand(
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


def _expand(*paths: str) -> list[Path]:
    return [Path(os.path.expandvars(p)).expanduser() for p in paths]
