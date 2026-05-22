"""macOS Keychain primitives for Claude Code OAuth credentials.

Claude Code 2.x stores its OAuth token in the system Keychain
(``"Claude Code-credentials"``). The Keychain is the canonical store on
macOS; the daemon-side ``KeychainBackend`` (in
``portal/credential_refresh.py``) just reads it for expiry inspection,
triggers refresh by running ``claude --print ok`` exactly the way an
interactive user does, and maintains an on-disk cache so each agent can
read a per-agent ``.credentials.json`` without round-tripping through
``security`` every read.

This module provides the storage primitives only:

  - **Keychain read / write** via the ``security`` CLI.
  - **CredentialCache** — atomic-write JSON blob to
    ``~/.puffo-agent/run/claude-credentials.json``, daemon-owned.
  - **Bootstrap** — populate the cache from Keychain on first call.

Refresh itself is *not* in this module. The previous design ran claude
in a sandboxed ``$HOME`` with a forged ``.credentials.json`` seeded
from the cache, then copied the resulting blob back; in production that
turned out to be brittle: claude on macOS deleted the sandbox creds
file on exit before flushing the rotated blob, sandboxing HOME caused
``security`` to fire ``loginKC:queryCreate`` authorization prompts, and
the only outcome was burning Anthropic refresh-tokens that we then
couldn't capture. The current design runs claude with the user's real
``$HOME`` (identical to the FileBackend on Linux/Windows) so claude's
own refresh path writes straight to Keychain, exactly the way the user
running ``claude`` interactively expects. The daemon picks the
rotation up via cache expiry inspection and propagates it to agents.

GitHub issue anthropics/claude-code#37512 documented that setting
``CLAUDE_CODE_OAUTH_TOKEN`` caused the CLI to silently
``security delete-generic-password "Claude Code-credentials"`` on exit
via its fallback-combiner cleanup path. We never set that env var, and
the real-HOME refresh path doesn't either, so the bug doesn't apply.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# The macOS Keychain "service" name Claude Code uses. Hard-coded by
# Claude Code itself; verified via reverse-engineered cli.js in issue
# #37512 and reproducible with ``security dump-keychain | grep``.
KEYCHAIN_SERVICE = "Claude Code-credentials"

# File name for the cache JSON blob — picked deliberately short + obvious.
CACHE_FILENAME = "claude-credentials.json"

# How long we wait for a single ``security`` invocation.
SECURITY_TIMEOUT_SECONDS = 60

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
    """Upsert the JSON blob into the Keychain entry. Best-effort.

    Only used by external-rotation poll when we detect Keychain drifted
    from cache for some reason; the canonical refresh path lets claude
    itself update Keychain.
    """
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
# Bootstrap
# ─────────────────────────────────────────────────────────────────────────────

def bootstrap_from_keychain(cache: CredentialCache) -> tuple[bool, Optional[str]]:
    """Materialise the cache from the Keychain on daemon start.

    Always reads Keychain — Keychain is canonical, and while the daemon
    was stopped the user (or anything else with the user's UID +
    signing identity) may have rotated the token via interactive
    ``claude /login``, the main CLI's own refresh-on-expiry, a VS Code
    plugin write, etc. A warm-cache short-circuit here previously
    caused the daemon to start with a stale RT, sync that stale RT to
    every agent's per-agent ``.credentials.json``, and then the
    spawned claude subprocesses immediately 401'd on Anthropic until
    the auth-error wake-up eventually pulled the current token from
    Keychain. The one extra ``security`` call per daemon start is
    cheap insurance against that startup race.

    If Keychain read fails *and* the cache still has a token, fall
    back to the cache so the daemon at least limps along (the 5-min
    external-rotation poll will keep trying Keychain).

    Returns ``(ok, reason_when_not_ok)``.
    """
    read = read_keychain_blob()
    if read.ok and read.blob:
        cache.write(read.blob)
        return (True, "bootstrapped")
    if cache.exists() and cache.access_token():
        return (True, f"keychain_read_failed_fell_back_to_cache: {read.error}")
    return (False, read.error)
