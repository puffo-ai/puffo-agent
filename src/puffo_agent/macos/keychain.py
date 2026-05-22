"""macOS Keychain primitives for Claude Code OAuth credentials.

Claude Code 2.x stores its OAuth token in the system Keychain
(``"Claude Code-credentials"``). Per-agent ``$HOME`` overrides don't
isolate Keychain access (Keychain ACL is keyed by user UID + signing
identity, not by HOME), so the host's claude binary running under a
puffo-agent worker can trigger an ACL re-prompt every spawn.

Also: GitHub issue anthropics/claude-code#37512 documents that setting
``CLAUDE_CODE_OAUTH_TOKEN`` causes the CLI to silently
``security delete-generic-password "Claude Code-credentials"`` on exit
via its fallback-combiner cleanup path — which kicks the user's main
CLI / VS Code extension off. Anthropic did not fix it; the issue auto-
closed in April 2026.

This module provides the storage primitives only:

  - **Keychain read / write** via the ``security`` CLI.
  - **PATH shim** that intercepts the buggy delete-generic-password
    call from issue #37512.
  - **CredentialCache** — atomic-write JSON blob to
    ``~/.puffo-agent/run/claude-credentials.json``, daemon-owned.
  - **Bootstrap** — populate the cache from Keychain on first call.
  - **Refresh oneshot** — run a sandboxed ``claude --print`` so claude
    rotates the token and writes the new blob back to the cache.

The async ``CredentialRefresher`` lifecycle (poll loop, agent fan-out,
401-wake) lives in ``portal/credential_refresh.py`` and delegates here
via the ``KeychainBackend`` adapter — this module stays
synchronous-primitive-only so it's straightforward to unit-test off
macOS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# The macOS Keychain "service" name Claude Code uses. Hard-coded by
# Claude Code itself; verified via reverse-engineered cli.js in issue
# #37512 and reproducible with ``security dump-keychain | grep``.
KEYCHAIN_SERVICE = "Claude Code-credentials"

# File names — picked deliberately short + obvious.
CACHE_FILENAME = "claude-credentials.json"
SHIM_FILENAME = "security"

# Refresh every 6h. Claude Code OAuth access tokens TTL is 8h, so the
# 2h margin means a slow / failed refresh has a retry before tokens
# actually expire.
REFRESH_INTERVAL_SECONDS = 6 * 3600

# How long we wait for a single ``security`` invocation.
SECURITY_TIMEOUT_SECONDS = 60

# Refresh oneshot timeout.
REFRESH_ONESHOT_TIMEOUT_SECONDS = 90

# How often the Keychain-poll loop wakes to detect tokens rotated by
# OTHER processes (operator's main CLI, an agent's own claude
# subprocess self-refreshing on 401).
KEYCHAIN_POLL_INTERVAL_SECONDS = 5 * 60


def is_macos() -> bool:
    return platform.system() == "Darwin"


# ─────────────────────────────────────────────────────────────────────────────
# Keychain read / write primitives
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class KeychainReadResult:
    ok: bool
    blob: Optional[str]
    error: Optional[str]
    stderr: Optional[str]


def read_keychain_blob(timeout: float = SECURITY_TIMEOUT_SECONDS) -> KeychainReadResult:
    """Read the ``"Claude Code-credentials"`` entry from the user's
    login Keychain. Returns the raw stdout (a JSON-shaped string written
    by Claude Code) or an error reason.
    """
    if not is_macos():
        return KeychainReadResult(False, None, "not_macos", None)
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return KeychainReadResult(False, None, "security_binary_missing", None)
    except subprocess.TimeoutExpired:
        return KeychainReadResult(
            False, None, "timeout (ACL prompt may be open)", None,
        )
    if result.returncode != 0:
        return KeychainReadResult(
            False, None, f"exit_code={result.returncode}", result.stderr or None,
        )
    blob = result.stdout.strip()
    if not blob:
        return KeychainReadResult(False, None, "empty_stdout", None)
    try:
        json.loads(blob)
    except json.JSONDecodeError as exc:
        return KeychainReadResult(False, None, f"invalid_json: {exc}", None)
    return KeychainReadResult(True, blob, None, None)


def writeback_to_keychain(
    blob: str, timeout: float = SECURITY_TIMEOUT_SECONDS,
) -> tuple[bool, Optional[str]]:
    """Upsert the JSON blob into the Keychain entry. Best-effort."""
    if not is_macos():
        return (False, "not_macos")
    try:
        result = subprocess.run(
            [
                "security", "add-generic-password",
                "-U",
                "-s", KEYCHAIN_SERVICE,
                "-a", os.environ.get("USER", "claude"),
                "-w", blob,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return (False, f"security_failed: {exc}")
    if result.returncode != 0:
        return (False, f"exit_code={result.returncode}; stderr={result.stderr.strip()!r}")
    return (True, None)


# ─────────────────────────────────────────────────────────────────────────────
# PATH shim
# ─────────────────────────────────────────────────────────────────────────────

_SHIM_BODY = r"""#!/bin/bash
# Auto-generated by puffo-agent. Intercepts
# `security delete-generic-password "Claude Code-credentials"` and
# silently no-ops it (Claude Code issue #37512), passing every other
# `security` invocation through to the real binary.
#
# DO NOT EDIT — overwritten on daemon start.
REAL=/usr/bin/security

is_delete=0
for arg in "$@"; do
  if [ "$arg" = "delete-generic-password" ]; then
    is_delete=1
    break
  fi
done

if [ "$is_delete" = "1" ]; then
  for arg in "$@"; do
    if [ "$arg" = "Claude Code-credentials" ]; then
      exit 0
    fi
  done
fi

exec "$REAL" "$@"
"""


def shim_dir(home: Path) -> Path:
    """Where the PATH shim binary lives. Inside daemon run dir so it
    auto-cleans on uninstall."""
    return home / "run" / "keychain-shim"


def install_path_shim(home: Path) -> Path:
    """Write the security-shim script and chmod it executable. Returns
    the directory to prepend to ``PATH``. Idempotent."""
    d = shim_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    binary = d / SHIM_FILENAME
    binary.write_text(_SHIM_BODY, encoding="utf-8")
    binary.chmod(
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        | stat.S_IRGRP | stat.S_IXGRP
        | stat.S_IROTH | stat.S_IXOTH,
    )
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Credential cache
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CredentialCache:
    """The on-disk cache of the OAuth blob, daemon-owned. Lives at
    ``~/.puffo-agent/run/claude-credentials.json``."""
    path: Path

    @classmethod
    def at(cls, home: Path) -> "CredentialCache":
        return cls(home / "run" / CACHE_FILENAME)

    def exists(self) -> bool:
        return self.path.exists()

    def write(self, blob: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / f".{self.path.name}.tmp.{os.getpid()}"
        tmp.write_text(blob, encoding="utf-8")
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, self.path)

    def read(self) -> Optional[str]:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def access_token(self) -> Optional[str]:
        raw = self.read()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return ((data.get("claudeAiOauth") or {}).get("accessToken")) or None

    def expires_at_seconds(self) -> Optional[float]:
        """Unix-seconds expiry for the cached token, or None if the
        blob is missing/malformed. ``expiresAt`` is stored in
        milliseconds."""
        raw = self.read()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            ms = int((data.get("claudeAiOauth") or {}).get("expiresAt"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return ms / 1000.0


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap + Refresh
# ─────────────────────────────────────────────────────────────────────────────

def bootstrap_from_keychain(cache: CredentialCache) -> tuple[bool, Optional[str]]:
    """Materialise the cache from the Keychain if the cache is missing
    or empty. Returns (ok, reason_when_not_ok)."""
    if cache.exists() and cache.access_token():
        return (True, "cache_already_warm")
    read = read_keychain_blob()
    if not read.ok:
        return (False, read.error)
    cache.write(read.blob)
    return (True, "bootstrapped")


async def _run_claude_oneshot(
    env: dict[str, str],
    cwd: str,
    *,
    timeout: float = REFRESH_ONESHOT_TIMEOUT_SECONDS,
) -> tuple[Optional[int], str]:
    """Spawn ``claude --print --max-turns 1 "ok"`` and wait. Returns
    ``(returncode, error_reason)`` — ``returncode`` is None on
    failure paths. Always cleans up the subprocess + pipe FDs even on
    early-return paths (timeout / spawn error)."""
    from ..agent.cli_bin import resolve_claude_bin
    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        return (None, "claude_binary_missing")
    proc: asyncio.subprocess.Process | None = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                claude_bin, "--dangerously-skip-permissions",
                "--print", "--max-turns", "1",
                "--output-format", "stream-json", "--verbose",
                "ok",
                env=env, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return (None, "claude_binary_missing")

        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.communicate()
            except Exception:
                pass
            return (None, "refresh_oneshot_timeout")
        return (proc.returncode, "")
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass


async def refresh_via_oneshot(
    cache: CredentialCache,
    shim_dir_path: Path,
    *,
    timeout: float = REFRESH_ONESHOT_TIMEOUT_SECONDS,
) -> tuple[bool, Optional[str]]:
    """macOS path: run a one-turn ``claude --print`` in a sandbox $HOME
    seeded from the cache. On exit, Claude has rewritten
    ``.credentials.json`` with a fresh token; copy it back to the cache.

    Returns (ok, reason_when_not_ok).

    ⚠️ **Keep in sync with** ``portal.diagnostic._run_sandboxed_claude_oneshot``:
    that probe is a sync mirror of this function so the diagnostic
    can run without an asyncio event loop. Any env / arg change here
    must be mirrored there or the probe stops validating prod.
    """
    if not is_macos():
        return (False, "not_macos")
    blob = cache.read()
    if not blob:
        return (False, "cache_empty")
    try:
        old_token = ((json.loads(blob).get("claudeAiOauth") or {}).get("accessToken")) or ""
    except json.JSONDecodeError:
        old_token = ""

    with tempfile.TemporaryDirectory(prefix="puffo-agent-refresh-") as sandbox:
        sandbox_path = Path(sandbox)
        sandbox_claude_dir = sandbox_path / ".claude"
        sandbox_claude_dir.mkdir(parents=True, exist_ok=True)
        sandbox_creds = sandbox_claude_dir / ".credentials.json"
        sandbox_creds.write_text(blob, encoding="utf-8")

        env = {
            **os.environ,
            "HOME": str(sandbox_path),
            "USERPROFILE": str(sandbox_path),
            "CLAUDE_CONFIG_DIR": str(sandbox_claude_dir),
            "PATH": f"{shim_dir_path}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        # Deliberately NO ``CLAUDE_CODE_OAUTH_TOKEN``: we want claude's
        # native refresh path to engage (read .credentials.json → if
        # expired refresh against Anthropic → write back). The env-var
        # mode would bypass storage entirely.
        rc, err = await _run_claude_oneshot(env, str(sandbox_path), timeout=timeout)
        if rc is None:
            return (False, err)
        if rc != 0:
            return (False, f"claude_exit_code={rc}")

        try:
            refreshed = sandbox_creds.read_text(encoding="utf-8")
        except FileNotFoundError:
            return (False, "refreshed_credentials_missing")
        try:
            new_token = (
                (json.loads(refreshed).get("claudeAiOauth") or {}).get("accessToken")
            ) or ""
        except json.JSONDecodeError:
            return (False, "refreshed_credentials_unparseable")

        cache.write(refreshed)
        if old_token and new_token and old_token == new_token:
            return (True, "token_unchanged")
        return (True, "token_refreshed")
