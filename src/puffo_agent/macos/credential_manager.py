"""Transparent Claude Code credential management on macOS.

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

This module makes the daemon the *single owner* of the credential and
treats the Keychain as a sync target rather than a live read path for
agent workers:

  1. **Bootstrap**: once per daemon lifetime, read the Keychain entry
     via ``security find-generic-password -w``. First call may trigger
     a one-time ACL prompt; "Always Allow" makes it silent forever.

  2. **Cache**: write the JSON blob to
     ``~/.puffo-agent/run/claude-credentials.json`` (chmod 600). This
     is the shared source of truth that all agent workers copy from.

  3. **Refresh**: every ``REFRESH_INTERVAL_SECONDS`` (default 6h, well
     under the 8h token TTL so we never let it lapse), run a
     dedicated ``claude --print --max-turns 1`` oneshot in a sandbox
     ``$HOME`` seeded from the cache; on exit the sandbox file holds a
     freshly-refreshed token; atomic-rename back into the cache.

  4. **Writeback**: after a successful refresh, push the new blob to
     the Keychain via ``security add-generic-password -U`` so the
     user's main CLI sees the same fresh token. Best-effort — if the
     write fails (ACL prompt, sandboxed binary, etc.) we keep going;
     the daemon-owned cache is still authoritative for agents.

  5. **PATH shim**: every agent worker gets a tiny shim earlier on
     ``$PATH`` that intercepts ``security delete-generic-password
     "Claude Code-credentials"`` (issue #37512). All other ``security``
     calls pass through to ``/usr/bin/security`` unchanged.

The diagnostic CLI surface (``puffo-agent test ...``) is in
``portal/diagnostic.py``; this module exposes the building blocks it
needs without binding to the CLI.
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
# actually expire. Lowering increases refresh churn; raising risks
# concurrent expiry across agent + host CLI.
REFRESH_INTERVAL_SECONDS = 6 * 3600

# How long we wait for a single ``security`` invocation. Reads are
# instantaneous on a granted ACL; long blocks mean the ACL prompt is
# up and the user is deciding — we deliberately don't time those out
# aggressively, otherwise we'd kill the prompt and never get the grant.
SECURITY_TIMEOUT_SECONDS = 60

# Refresh oneshot timeout — the OAuth refresh round-trip is sub-second;
# anything > 60s means something is wedged.
REFRESH_ONESHOT_TIMEOUT_SECONDS = 90


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
    # Stderr is interesting for diagnostics — e.g. "could not be found"
    # vs. "could not be authenticated" vs. "user canceled the operation".
    stderr: Optional[str]


def read_keychain_blob(timeout: float = SECURITY_TIMEOUT_SECONDS) -> KeychainReadResult:
    """Read the ``"Claude Code-credentials"`` entry from the user's
    login Keychain. Returns the raw stdout (a JSON-shaped string written
    by Claude Code) or an error reason.

    Note: ``-a $USER`` is omitted because Claude Code does not set an
    account name on the entry; lookups by service alone are how the CLI
    itself finds it.
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
    # Validate JSON shape early — we'd rather fail at bootstrap than
    # ship a corrupted blob to agent workers.
    try:
        json.loads(blob)
    except json.JSONDecodeError as exc:
        return KeychainReadResult(False, None, f"invalid_json: {exc}", None)
    return KeychainReadResult(True, blob, None, None)


def writeback_to_keychain(
    blob: str, timeout: float = SECURITY_TIMEOUT_SECONDS,
) -> tuple[bool, Optional[str]]:
    """Upsert the JSON blob into the Keychain entry. Best-effort: when
    the write fails (ACL denied, sandboxed binary, etc.) we return
    ``(False, reason)`` and the caller logs at WARNING and proceeds.
    """
    if not is_macos():
        return (False, "not_macos")
    try:
        result = subprocess.run(
            [
                "security", "add-generic-password",
                "-U",  # update if exists
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
    auto-cleans on uninstall; not in ``/usr/local/bin`` (no sudo, no
    residue)."""
    return home / "run" / "keychain-shim"


def install_path_shim(home: Path) -> Path:
    """Write the security-shim script and chmod it executable. Returns
    the directory to prepend to ``PATH``.

    Idempotent — overwrites the script on every call so a daemon
    update that changed the shim body propagates without user action.
    """
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
    ``~/.puffo-agent/run/claude-credentials.json``. Workers read from
    here; refresh writes to here; ``writeback_to_keychain`` reads from
    here.
    """
    path: Path

    @classmethod
    def at(cls, home: Path) -> "CredentialCache":
        return cls(home / "run" / CACHE_FILENAME)

    def exists(self) -> bool:
        return self.path.exists()

    def write(self, blob: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp on same FS, fsync, then rename. Workers
        # could be mid-spawn reading this; the rename swap means they
        # always see a complete file.
        tmp = self.path.parent / f".{self.path.name}.tmp.{os.getpid()}"
        tmp.write_text(blob, encoding="utf-8")
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
        os.replace(tmp, self.path)

    def read(self) -> Optional[str]:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def access_token(self) -> Optional[str]:
        """Pull ``claudeAiOauth.accessToken`` out of the cached blob,
        if present. Used to populate ``CLAUDE_CODE_OAUTH_TOKEN`` env
        var on spawn (Keychain-read bypass)."""
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
        blob is missing/malformed. Claude Code stores ``expiresAt`` in
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
    or empty. Returns (ok, reason_when_not_ok).

    Called once on daemon startup. Subsequent reads use the cache; the
    refresh loop keeps it fresh without re-reading Keychain.
    """
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
    failure paths (timeout / binary missing / spawn error). Always
    cleans up the subprocess and pipe FDs even on early-return paths
    (this is the source of the FD-exhaustion reports under load —
    asyncio's create_subprocess_exec leaves pipes open until the
    proc is awaited).
    """
    if shutil.which("claude") is None:
        return (None, "claude_binary_missing")
    proc: asyncio.subprocess.Process | None = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--dangerously-skip-permissions",
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
            # CRITICAL — without the kill + drain below the proc
            # zombies and its stdout/stderr pipes leak. Under load
            # ([Errno 24] Too many open files) this is the FD leak
            # the user reported.
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
        # Defensive belt-and-braces: any exception path between spawn
        # and the timeout/communicate block above leaves proc dangling.
        # On the success path proc.returncode is already set + pipes
        # are drained by communicate(), so this branch is a no-op.
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

        # Read whatever Claude wrote.
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
            # Token wasn't actually rotated — Claude saw it as still
            # valid. That's fine; the next refresh tick will try again.
            return (True, "token_unchanged")
        return (True, "token_refreshed")


async def refresh_via_host_oneshot(
    host_home: Path,
    *,
    timeout: float = REFRESH_ONESHOT_TIMEOUT_SECONDS,
) -> tuple[bool, Optional[str]]:
    """Linux/Windows path: run ``claude --print`` against the operator's
    real ``$HOME``. Claude reads / writes ``~/.claude/.credentials.json``
    natively, so this triggers an in-place token rotation that every
    agent's symlink picks up on the next read.

    No sandbox, no Keychain — the host file IS the source of truth on
    these platforms. Daemon-level scheduling means there's exactly ONE
    refresh in flight at any time, removing the rotating-refresh-token
    race that was causing "refresh ran but expiry didn't advance"
    under multi-agent load.
    """
    env = {**os.environ, "HOME": str(host_home), "USERPROFILE": str(host_home)}
    rc, err = await _run_claude_oneshot(env, str(host_home), timeout=timeout)
    if rc is None:
        return (False, err)
    if rc != 0:
        return (False, f"claude_exit_code={rc}")
    return (True, "token_refreshed_or_unchanged")


# ─────────────────────────────────────────────────────────────────────────────
# Daemon-level credential manager
# ─────────────────────────────────────────────────────────────────────────────

class CredentialManager:
    """Long-running daemon companion. Single source of truth for
    Claude Code OAuth refresh — every cli-local agent reads from the
    file this manager keeps fresh; no per-agent refresh_ping path.

    Lifecycle::

        cm = CredentialManager(home)
        await cm.bootstrap()  # may trigger one-time macOS ACL prompt
        cm.start()
        # ... daemon runs ...
        await cm.stop()

    Why this lives at the daemon level and not on each adapter:

    The previous per-agent path raced rotating refresh tokens whenever
    N agents hit their refresh window inside the same 30-minute span.
    First agent's refresh succeeds, server invalidates the prior
    refresh token, every other in-flight refresh fails with
    ``invalid_grant`` — but the failure path still rewrote the file
    with stale content, so ``expiresAt`` would not advance and the
    next ``refresh_ping`` tick repeated the race. Compounded by a FD
    leak on the timeout path (proc not killed + pipes not drained),
    you got "refresh ran but expiry didn't advance" plus
    ``[Errno 24] Too many open files``. The daemon-level manager
    here has exactly one refresh in flight, so there's nothing to
    race; the FD leak is also fixed in ``_run_claude_oneshot``.
    """

    def __init__(
        self,
        home: Path,
        *,
        refresh_interval_seconds: float = REFRESH_INTERVAL_SECONDS,
    ):
        self.home = home
        # macOS-only — Keychain bridge cache. Non-macOS uses the host
        # file (~/.claude/.credentials.json) directly.
        self.cache = CredentialCache.at(home) if is_macos() else None
        self.shim = shim_dir(home) if is_macos() else None
        self.refresh_interval_seconds = refresh_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # Observability — exposed via runtime.json so operators can
        # see "is credential refresh healthy?" without tailing logs.
        self.last_refresh_at: Optional[float] = None
        self.last_refresh_status: Optional[str] = None
        # Backoff state: bounded exponential off the normal 6h cadence
        # when refresh keeps failing. Without this, a permanently-bad
        # refresh token (revoked / user logged out elsewhere) hammers
        # the server forever.
        self.consecutive_failures: int = 0

    async def bootstrap(self) -> tuple[bool, Optional[str]]:
        """Bootstrap cache + install PATH shim. macOS-only — on
        Linux/Windows the host's ~/.claude/.credentials.json is
        already the canonical store. Safe to call on every daemon
        start.
        """
        if not is_macos():
            return (True, "host_file_authoritative")
        install_path_shim(self.home)
        return bootstrap_from_keychain(self.cache)

    def start(self) -> None:
        """Kick off the background refresh loop. Runs on all
        platforms (claude-code OAuth applies regardless of platform);
        the per-platform refresh strategy is dispatched in
        ``_refresh_once``. No-op when already started.
        """
        if self._task is not None:
            return
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        """Signal the refresh loop to exit and wait for it. Safe to
        call when ``start`` was never called."""
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    def _next_interval_seconds(self) -> float:
        """Bounded exponential backoff after consecutive failures.
        Healthy state stays on the normal 6h schedule; failures
        retry faster than normal (10min, 20min, 40min, …) but
        capped at the normal interval so a permanent failure
        doesn't burn FDs + tokens.
        """
        if self.consecutive_failures == 0:
            return self.refresh_interval_seconds
        base = 600.0  # 10 minutes
        delay = base * (2 ** (self.consecutive_failures - 1))
        return min(delay, self.refresh_interval_seconds)

    async def _loop(self) -> None:
        """Refresh-then-sleep loop. First tick fires immediately so
        the on-disk credential gets a fresh token soon after daemon
        start (the bootstrap blob may be several hours old).
        Subsequent ticks honour ``_next_interval_seconds()``.
        """
        # Tiny initial jitter so concurrent daemons (test fixtures)
        # don't all hammer claude simultaneously.
        await asyncio.sleep(2.0)
        while not self._stop.is_set():
            try:
                await self._refresh_once()
            except Exception as exc:
                logger.warning(
                    "credential refresh tick crashed: %s", exc, exc_info=True,
                )
                self.consecutive_failures += 1
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._next_interval_seconds(),
                )
            except asyncio.TimeoutError:
                pass

    async def _refresh_once(self) -> None:
        if is_macos():
            ok, reason = await refresh_via_oneshot(self.cache, self.shim)
        else:
            ok, reason = await refresh_via_host_oneshot(Path.home())
        self.last_refresh_at = time.time()
        self.last_refresh_status = (
            f"ok ({reason})" if ok else f"failed ({reason})"
        )
        if ok:
            self.consecutive_failures = 0
            logger.info("claude credential refresh: %s", reason)
            # macOS only: writeback to Keychain so main CLI / VS Code
            # extension see the rotated token.
            if is_macos() and self.cache is not None:
                blob = self.cache.read()
                if blob:
                    wb_ok, wb_reason = writeback_to_keychain(blob)
                    if wb_ok:
                        logger.info(
                            "claude credential writeback to keychain: ok",
                        )
                    else:
                        logger.info(
                            "claude credential writeback to keychain "
                            "skipped: %s", wb_reason,
                        )
        else:
            self.consecutive_failures += 1
            logger.warning(
                "claude credential refresh failed (attempt #%d): %s — "
                "next try in %.0fs",
                self.consecutive_failures, reason,
                self._next_interval_seconds(),
            )
