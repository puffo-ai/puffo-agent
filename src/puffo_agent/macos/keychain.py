"""macOS Keychain primitives for Claude Code OAuth credentials.

Claude Code stores its OAuth token in the system Keychain. Most 2.x
installs use ``"Claude Code-credentials"``, while some hosts expose the
same OAuth blob under ``"Claude Code"``. The Keychain is the canonical store on
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


# The macOS Keychain "service" names Claude Code has used. Prefer the
# historical puffo-compatible name on ties, but probe both because some
# current Claude Code installs only expose the bare "Claude Code" item.
KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_SERVICES = (KEYCHAIN_SERVICE, "Claude Code")

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
    service: Optional[str] = None


@dataclass(frozen=True)
class _KeychainCandidate:
    service: str
    blob: str
    expires_at_ms: Optional[int]


def _parse_credential(blob: str) -> tuple[Optional[int], Optional[str]]:
    """Validate a Claude OAuth blob in a single parse. Returns
    ``(expires_at_ms, None)`` for a well-formed blob, else
    ``(None, reason)`` — ``invalid_json`` for bad JSON, otherwise
    ``invalid_oauth_blob``.
    """
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        return None, f"invalid_json: {exc}"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None, "invalid_oauth_blob"
    access_token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")
    if not isinstance(access_token, str) or not access_token:
        return None, "invalid_oauth_blob"
    if not isinstance(refresh_token, str) or not refresh_token:
        return None, "invalid_oauth_blob"
    try:
        return int(oauth.get("expiresAt")), None
    except (TypeError, ValueError):
        return None, "invalid_oauth_blob"


def _service_rank(service: str) -> int:
    try:
        return KEYCHAIN_SERVICES.index(service)
    except ValueError:
        return len(KEYCHAIN_SERVICES)


def _select_keychain_candidate(
    candidates: list[_KeychainCandidate],
) -> _KeychainCandidate:
    """Pick the freshest valid credential blob.

    If an operator previously copied a stale blob into the old
    ``Claude Code-credentials`` item but Claude Code now rotates the
    bare ``Claude Code`` item, blindly preferring the old service would
    keep syncing stale refresh tokens. ``expiresAt`` is the least
    invasive freshness signal available inside the blob.
    """
    return max(
        candidates,
        key=lambda c: (
            c.expires_at_ms is not None,
            c.expires_at_ms or -1,
            -_service_rank(c.service),
        ),
    )


def _read_keychain_service(
    service: str,
    timeout: float,
) -> tuple[KeychainReadResult, Optional[int]]:
    """Read one service. Returns the result plus the parsed
    ``expiresAt`` (only set when ``result.ok``) so the caller doesn't
    re-parse the blob to rank candidates."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return KeychainReadResult(
            False, None, "security_binary_missing", None, service,
        ), None
    except subprocess.TimeoutExpired:
        return KeychainReadResult(
            False, None, "timeout (ACL prompt may be open)", None, service,
        ), None
    if result.returncode != 0:
        return KeychainReadResult(
            False,
            None,
            f"exit_code={result.returncode}",
            result.stderr or None,
            service,
        ), None
    blob = result.stdout.strip()
    if not blob:
        return KeychainReadResult(False, None, "empty_stdout", None, service), None
    expires_at_ms, reason = _parse_credential(blob)
    if reason is not None:
        return KeychainReadResult(False, None, reason, None, service), None
    return KeychainReadResult(True, blob, None, None, service), expires_at_ms


def read_keychain_blob(timeout: float = SECURITY_TIMEOUT_SECONDS) -> KeychainReadResult:
    """Read a Claude Code credential entry from the user's login Keychain.

    Returns the raw stdout (a JSON-shaped string written by Claude
    Code) or an error reason. Probes all known Claude Code service names
    and chooses the freshest valid blob when more than one exists.
    """
    if not is_macos():
        return KeychainReadResult(False, None, "not_macos", None)
    candidates: list[_KeychainCandidate] = []
    errors: list[str] = []
    stderrs: list[str] = []
    for service in KEYCHAIN_SERVICES:
        result, expires_at_ms = _read_keychain_service(service, timeout)
        if not result.ok:
            if result.error == "security_binary_missing":
                return result
            errors.append(f"{service}: {result.error}")
            if result.stderr:
                stderrs.append(f"{service}: {result.stderr.strip()}")
            continue
        candidates.append(
            _KeychainCandidate(
                service=service,
                blob=result.blob or "",
                expires_at_ms=expires_at_ms,
            )
        )
    if not candidates:
        return KeychainReadResult(
            False,
            None,
            "; ".join(errors) if errors else "no_keychain_services",
            "\n".join(stderrs) if stderrs else None,
        )
    selected = _select_keychain_candidate(candidates)
    return KeychainReadResult(True, selected.blob, None, None, selected.service)


def writeback_to_keychain(
    blob: str,
    timeout: float = SECURITY_TIMEOUT_SECONDS,
    service: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Upsert the JSON blob into the Keychain entry. Best-effort.

    Only used by external-rotation poll when we detect Keychain drifted
    from cache for some reason; the canonical refresh path lets claude
    itself update Keychain.
    """
    if not is_macos():
        return (False, "not_macos")
    target_service = service or KEYCHAIN_SERVICE
    try:
        result = subprocess.run(
            [
                "security", "add-generic-password",
                "-U",
                "-s", target_service,
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
