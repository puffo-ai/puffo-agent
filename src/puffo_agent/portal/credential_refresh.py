"""Daemon-owned Claude OAuth credential refresh.

Replaces the per-agent ``refresh_ping`` from
``agent/adapters/base.py``. Every refresh of the on-disk Claude
credentials file goes through ONE process — the puffo-agent daemon —
so Anthropic's single-use refresh-token rotation can't be raced by N
agent workers all reading the same disk RT and burning each other's
in-memory copies.

Backend abstraction
-------------------

The platform-agnostic ``CredentialRefresher`` owns the cross-cutting
concerns: an ``asyncio.Lock`` (single-writer), an agent home registry,
the ``_refresh_request`` event (notify_refresh_needed wake), the poll
loop, and the per-tick fan-out to every registered agent. It delegates
all platform specifics to a pluggable ``CredentialBackend``:

  - ``FileBackend`` (Linux / Windows): host file
    ``~/.claude/.credentials.json`` is the canonical store; refresh
    spawns ``claude --print`` with ``HOME=host_home`` so the atomic
    tmp+rename lands at the host file; per-agent sync is a symlink (or
    copy fallback) via ``link_host_credentials``. External rotation
    (operator running ``claude /login``) propagates atomically through
    the symlink — no poll needed.
  - ``KeychainBackend`` (macOS): Keychain is the canonical store
    (Claude Code 2.x). The daemon maintains a cache at
    ``~/.puffo-agent/run/claude-credentials.json``; refresh runs a
    sandboxed ``claude --print`` so claude rotates the token against
    Anthropic and writes it back; writeback to Keychain is best-effort
    so the operator's main CLI / VS Code extension see the new token;
    per-agent sync is a copy (Keychain ACL is keyed on UID + signing
    identity, not HOME, so the per-agent HOME trick is moot). An extra
    5-minute Keychain poll picks up rotations done by OTHER processes
    (operator's main CLI, an agent's own claude self-refreshing on a
    401) and fans them to running agents.

Public API (unchanged from the file-backend-only version):

  - ``register_agent`` / ``unregister_agent`` — agent-home set
  - ``notify_refresh_needed`` — in-process 401 trigger
  - ``run_loop`` — the daemon's long-lived coroutine
  - ``expires_in_seconds`` — diagnostics surface

The refactor is invisible to the daemon's ``daemon.py`` wiring
beyond the choice of backend at construction time.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from .state import link_host_codex_auth, link_host_credentials


logger = logging.getLogger(__name__)


REFRESH_POLL_SECONDS = 120
REFRESH_SAFETY_MARGIN_SECONDS = 10 * 60
REFRESH_ONESHOT_TIMEOUT_SECONDS = 120

# 2 ticks ≈ 4 min @ 120s poll: surfaces fast, doesn't false-positive on
# a single transient subprocess hiccup. RATE_LIMITED also contributes.
REFRESH_BROKEN_THRESHOLD = 2

# PUF-265 v2: refresh probe model. The refresh subprocess just needs
# to make ANY successful API call so Anthropic's auth layer rotates
# the OAuth token; the model doesn't matter for the rotation itself.
# Defaulting to the cheapest current model (Haiku 4.5) instead of the
# operator's interactive default (typically Opus) makes the probe
# (a) cheaper per call, and (b) less likely to hit per-model rate
# limits during high-usage windows.
REFRESH_PROBE_MODEL = "claude-haiku-4-5"

# PUF-265 v2: when the refresh probe fails specifically because
# Anthropic rate-limited us, the normal 120s poll wait is wasteful —
# rate limits typically clear in seconds, not minutes. Wake the
# refresher loop after a short randomised delay so we re-try sooner
# without hammering. Window is small (5-15s) so we don't synchronise
# multiple post-rate-limit retries across the fleet.
RATE_LIMIT_FAST_RETRY_MIN_SECONDS = 5.0
RATE_LIMIT_FAST_RETRY_MAX_SECONDS = 15.0

# Patterns matched against the refresh subprocess's stderr+stdout
# tails. Verbatim canonical Anthropic rate-limit signals + the
# Claude Code wrapper's most-common surfaces. Anchored / unambiguous
# tokens only — avoid generic "429" / "limit" which would false-
# positive on legitimate model output (Haiku probe replies are short
# but not infallibly clean).
_RATE_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bAPI Error: Request rejected \(429\)", re.IGNORECASE),
    re.compile(r"\bServer is temporarily limiting requests\b", re.IGNORECASE),
    re.compile(r"\brate[_ -]limit[_ -]error\b", re.IGNORECASE),
    re.compile(r'"type"\s*:\s*"rate_limit_error"', re.IGNORECASE),
    re.compile(r"\bYou've hit your\b.*?\blimit\b", re.IGNORECASE),
    re.compile(r"\bRepeated 529 Overloaded errors\b", re.IGNORECASE),
)


def _looks_like_rate_limit(out_tail: str, err_tail: str) -> bool:
    """True iff the refresh subprocess output matches one of the
    canonical Anthropic rate-limit signatures. Drives the fast-retry
    path so a transient rate-limit doesn't park the refresher on a
    full 120s wait."""
    combined = f"{out_tail}\n{err_tail}"
    for pattern in _RATE_LIMIT_PATTERNS:
        if pattern.search(combined):
            return True
    return False

# Codex's OAuth access_token is a JWT — the only authoritative expiry
# is the ``exp`` claim inside the token. Codex's own ``last_refresh``
# field uses an ~8-day staleness heuristic that's too coarse for our
# refresh-before-expiry strategy (claude rotates hourly; codex's
# access_token similarly expires in tens of minutes).
def _jwt_exp_unix(token: str) -> int | None:
    """Decode a JWT's ``exp`` claim without signature verification.

    Signature verification is OpenAI's job at use time; we only need
    the expiry timestamp to schedule pre-emptive refresh. Returns the
    Unix-seconds expiry, or None if the token isn't a parseable JWT.
    """
    import base64
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload = base64.urlsafe_b64decode(padded).decode("utf-8")
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return int(exp)


class RefreshOutcome(enum.Enum):
    """Result of a single backend refresh attempt."""
    REFRESHED = "refreshed"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    # PUF-265 v2: ``FAILED`` whose stderr/stdout matches a canonical
    # Anthropic rate-limit signature. Counts toward the
    # ``refresh_broken`` streak the same as ``FAILED`` (the refresh
    # really didn't happen) but additionally schedules a fast retry
    # via ``RATE_LIMIT_FAST_RETRY_{MIN,MAX}_SECONDS`` rather than
    # parking on the full ``REFRESH_POLL_SECONDS`` wait — rate limits
    # typically clear in seconds, and the refresher waking back up
    # within 5-15s rather than 2 min materially shrinks the
    # operator-observable outage when a momentary throttle clears.
    RATE_LIMITED = "rate_limited"


class CredentialBackend(Protocol):
    """Platform adapter for credential storage + refresh.

    Implementations: ``FileBackend`` (Linux/Windows host file),
    ``KeychainBackend`` (macOS Keychain + per-agent cache).
    """

    def expires_in_seconds(self) -> int | None:
        """Seconds until the canonical token expires (negative if
        past). ``None`` if not readable / not OAuth."""
        ...

    async def refresh(self) -> RefreshOutcome:
        """Run one refresh attempt. Must not be called concurrently —
        the ``CredentialRefresher`` holds an ``asyncio.Lock`` around
        every invocation."""
        ...

    def sync_to_agent(self, agent_home: Path) -> None:
        """Mirror the canonical credentials to one agent's per-agent
        ``.credentials.json``. Called by the refresher's fan-out after
        every tick (refresh or not) so external rotation propagates."""
        ...

    async def bootstrap(self) -> tuple[bool, Optional[str]]:
        """One-time setup at daemon start. ``FileBackend`` is a no-op;
        ``KeychainBackend`` reads from Keychain to populate the cache
        and installs the PATH shim."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# FileBackend — Linux / Windows
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FileBackend:
    """Host-file-canonical backend used on Linux and Windows. The host
    ``~/.claude/.credentials.json`` is the single source of truth;
    every agent's ``<agent_home>/.claude/.credentials.json`` is a
    symlink (or copy fallback) onto it via ``link_host_credentials``.

    External rotation (operator running ``claude /login``) propagates
    atomically through the symlink, so no external-poll is needed
    beyond the refresher's 2-minute ``expires_in`` poll.
    """
    host_home: Path

    @property
    def host_credentials(self) -> Path:
        return self.host_home / ".claude" / ".credentials.json"

    def expires_in_seconds(self) -> int | None:
        try:
            data = json.loads(self.host_credentials.read_text(encoding="utf-8"))
            expires_ms = int(data["claudeAiOauth"]["expiresAt"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return int(expires_ms / 1000 - time.time())

    async def refresh(self) -> RefreshOutcome:
        before = self.expires_in_seconds()
        env = {**os.environ, "HOME": str(self.host_home)}
        # PUF-265 v2: ``--model`` pins the probe to the cheapest current
        # Claude (Haiku 4.5) so it doesn't fight per-model rate limits on
        # the operator's interactive default (Opus / Sonnet) during
        # high-usage windows. Any successful response refreshes the
        # OAuth token regardless of model — model choice is a probe-cost
        # decision, not a correctness one.
        cmd = [
            "claude", "--dangerously-skip-permissions",
            "--model", REFRESH_PROBE_MODEL,
            "--print", "--max-turns", "1",
            "--output-format", "stream-json", "--verbose",
            "ok",
        ]
        started = time.time()
        try:
            # cwd=host_home so claude's project-resolution doesn't
            # drift into the daemon's launch directory (which is
            # whatever the operator ran `puffo-agent start` from);
            # the host home is the operator's normal claude
            # working dir and matches single-process /login UX.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(self.host_home),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "credential refresh subprocess timed out after %ds",
                REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
            return RefreshOutcome.FAILED
        except FileNotFoundError:
            logger.warning(
                "credential refresh: claude binary missing on PATH"
            )
            return RefreshOutcome.FAILED
        elapsed = time.time() - started
        after = self.expires_in_seconds()
        if proc.returncode != 0:
            err_tail = stderr.decode("utf-8", errors="replace").strip()[-400:]
            out_tail = stdout.decode("utf-8", errors="replace").strip()[-400:]
            if _looks_like_rate_limit(out_tail, err_tail):
                logger.warning(
                    "credential refresh rate-limited rc=%d in %.1fs | "
                    "stdout: %s | stderr: %s",
                    proc.returncode, elapsed, out_tail, err_tail,
                )
                return RefreshOutcome.RATE_LIMITED
            logger.warning(
                "credential refresh rc=%d in %.1fs | stdout: %s | stderr: %s",
                proc.returncode, elapsed, out_tail, err_tail,
            )
            return RefreshOutcome.FAILED
        if before is not None and after is not None and after <= before:
            err_tail = stderr.decode("utf-8", errors="replace").strip()[-400:]
            out_tail = stdout.decode("utf-8", errors="replace").strip()[-400:]
            logger.error(
                "credential refresh exit=0 but expiresAt didn't advance "
                "(before=%ds, after=%ds) in %.1fs — claude may not be "
                "rewriting credentials.json on this build; operator may "
                "need `claude /login` to recover | stdout: %s | stderr: %s",
                before, after, elapsed, out_tail, err_tail,
            )
            return RefreshOutcome.UNCHANGED
        logger.info(
            "credential refresh ok in %.1fs (expires_in: %s -> %s)",
            elapsed, before, after,
        )
        return RefreshOutcome.REFRESHED

    def sync_to_agent(self, agent_home: Path) -> None:
        link_host_credentials(self.host_home, agent_home)

    async def bootstrap(self) -> tuple[bool, Optional[str]]:
        return (True, "host_file_authoritative")


# ─────────────────────────────────────────────────────────────────────────────
# CodexFileBackend — cli-local + cli-docker, all platforms
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CodexFileBackend:
    """Host-file-canonical backend for codex OAuth (``~/.codex/auth.json``).

    Codex stores OAuth credentials there when the operator runs
    ``codex login``. Each agent's ``$CODEX_HOME`` is forced into
    ``cli_auth_credentials_store = "file"`` by ``write_codex_config_toml``
    so the host file is the single source of truth across platforms —
    including macOS, where codex's default ``auto`` mode would otherwise
    pick Keychain and break the symlink-propagation model.

    Refresh runs ``codex exec --ephemeral --skip-git-repo-check`` with
    a trivial prompt; codex's auth pipeline kicks in the same way the
    long-running ``codex app-server`` would, rotates the OAuth bundle
    if stale, and writes back atomically to auth.json.

    Per-agent sync is a symlink (or copy fallback on Windows
    non-developer-mode) via ``link_host_codex_auth``.
    """
    host_home: Path

    @property
    def host_auth(self) -> Path:
        return self.host_home / ".codex" / "auth.json"

    def expires_in_seconds(self) -> int | None:
        try:
            data = json.loads(self.host_auth.read_text(encoding="utf-8"))
            access = data.get("tokens", {}).get("access_token")
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(access, str) or not access:
            return None
        exp_unix = _jwt_exp_unix(access)
        if exp_unix is None:
            return None
        return int(exp_unix - time.time())

    async def refresh(self) -> RefreshOutcome:
        before = self.expires_in_seconds()
        env = {**os.environ, "HOME": str(self.host_home)}
        devnull = "NUL" if os.name == "nt" else "/dev/null"
        codex_bin = _resolve_codex_bin()
        if codex_bin is None:
            logger.warning(
                "codex credential refresh: codex binary missing on PATH"
            )
            return RefreshOutcome.FAILED
        # ``--ephemeral`` skips session-file writes; ``-o NUL`` discards
        # codex's final message; ``--skip-git-repo-check`` so refresh
        # works outside a git repo (the daemon's cwd).
        cmd = [
            codex_bin, "exec",
            "--ephemeral", "--skip-git-repo-check",
            "-o", devnull,
            "ok",
        ]
        started = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(self.host_home),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "codex credential refresh timed out after %ds",
                REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
            return RefreshOutcome.FAILED
        except FileNotFoundError:
            logger.warning(
                "codex credential refresh: codex binary missing on PATH"
            )
            return RefreshOutcome.FAILED
        elapsed = time.time() - started
        after = self.expires_in_seconds()
        if proc.returncode != 0:
            err_tail = stderr.decode("utf-8", errors="replace").strip()[-400:]
            out_tail = stdout.decode("utf-8", errors="replace").strip()[-400:]
            logger.warning(
                "codex credential refresh rc=%d in %.1fs | "
                "stdout: %s | stderr: %s",
                proc.returncode, elapsed, out_tail, err_tail,
            )
            return RefreshOutcome.FAILED
        if before is not None and after is not None and after <= before:
            logger.info(
                "codex credential refresh exit=0 but exp didn't advance "
                "(before=%ds, after=%ds) — token still fresh or "
                "operator on keyring store (cli_auth_credentials_store)",
                before, after,
            )
            return RefreshOutcome.UNCHANGED
        logger.info(
            "codex credential refresh ok in %.1fs (expires_in: %s -> %s)",
            elapsed, before, after,
        )
        return RefreshOutcome.REFRESHED

    def sync_to_agent(self, agent_home: Path) -> None:
        agent_codex_home = agent_home / ".codex"
        # Only codex agents have a ``.codex`` subdir (created lazily by
        # ``LocalCLIAdapter._ensure_codex_session``). Skip claude-only
        # agents to avoid cluttering them with a stray auth.json.
        if not agent_codex_home.exists():
            return
        link_host_codex_auth(self.host_home, agent_codex_home)

    async def bootstrap(self) -> tuple[bool, Optional[str]]:
        if not self.host_auth.exists():
            return (False, "no-host-codex-auth")
        return (True, "host_codex_file_authoritative")


def _resolve_codex_bin() -> str | None:
    """Resolve the ``codex`` executable via the shared resolver
    (``agent.cli_bin``). Covers PATH + ``PUFFO_CODEX_BIN`` env
    override + macOS / Windows / Linux bundle paths so a LaunchAgent
    PATH that misses ``/opt/homebrew/bin`` or ``Codex.app`` still
    finds the binary the operator installed."""
    from ..agent.cli_bin import resolve_codex_bin as _resolver
    return _resolver()


# ─────────────────────────────────────────────────────────────────────────────
# KeychainBackend — macOS
# ─────────────────────────────────────────────────────────────────────────────

class KeychainBackend:
    """macOS backend. The Keychain is the canonical store; the daemon
    maintains a cache file and propagates rotations to every running
    agent via per-agent file copies (Keychain ACL is keyed on UID +
    signing identity, not HOME, so a symlink trick wouldn't help).

    Refresh strategy: identical to ``FileBackend`` — run
    ``claude --print "ok"`` with the *real* user HOME and let claude's
    own OAuth path read Keychain, rotate against Anthropic if expired,
    and write the rotated blob back to Keychain. This is exactly what
    the operator's interactive ``claude`` invocation does, so we share
    its battle-tested refresh code path instead of reinventing it via
    sandbox + cache-seeding (which proved fragile: see the keychain.py
    module docstring for the failure modes).

    External-rotation poll: every ``KEYCHAIN_POLL_INTERVAL_SECONDS``
    (5 min), re-read the Keychain (silent after the first
    "Always Allow" grant) and compare to the last-known blob. On
    change, write the cache and trigger a fan-out via the refresher's
    ``notify_refresh_needed`` — this catches rotations performed by
    the operator's main ``claude`` CLI or an agent's own subprocess
    self-refreshing on a 401.
    """

    def __init__(
        self,
        home: Path,
        cache,  # ..macos.keychain.CredentialCache
    ):
        self.home = home
        self.cache = cache
        # Last blob propagated to agents — cheap byte-equality key so
        # the poll loop only fans out on real changes.
        self._last_propagated_blob: Optional[str] = None

    def expires_in_seconds(self) -> int | None:
        """Pull expiry from the cache first (hot path; no subprocess).
        Cache miss → fall back to a Keychain read so the daemon's
        first-tick decision isn't blocked on bootstrap completion."""
        # Lazy import to keep the module-level import graph light.
        from ..macos.keychain import read_keychain_blob

        expires_at = self.cache.expires_at_seconds()
        if expires_at is not None:
            return int(expires_at - time.time())
        # Cache miss — try Keychain. Don't write the cache here; that's
        # ``bootstrap``'s job. We only want a TTL value.
        kr = read_keychain_blob()
        if not kr.ok or not kr.blob:
            return None
        try:
            data = json.loads(kr.blob)
            ms = int((data.get("claudeAiOauth") or {}).get("expiresAt"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        # Opportunistically warm the cache so subsequent ticks are fast.
        try:
            self.cache.write(kr.blob)
        except OSError:
            pass
        return int(ms / 1000 - time.time())

    async def refresh(self) -> RefreshOutcome:
        from ..macos.keychain import read_keychain_blob

        # Snapshot Keychain before claude runs so we can byte-compare
        # after. Cache.read() is not enough — agent processes may have
        # rotated Keychain externally between ticks and the cache is a
        # lagging mirror.
        kr_before = read_keychain_blob()
        before_blob = kr_before.blob if kr_before.ok else None

        # Real user HOME — claude reads Keychain, refreshes if expired,
        # writes back to Keychain. Identical pattern to FileBackend.
        # cwd=host_home so claude's project-resolution lands in the
        # operator's normal working dir (matches single-process /login
        # UX) instead of wherever the daemon was launched from.
        host_home = Path.home()
        env = {**os.environ, "HOME": str(host_home)}
        # PUF-265 v2: same model-pin rationale as ``FileBackend.refresh``
        # — Haiku to dodge per-model rate-limit windows on Opus/Sonnet.
        cmd = [
            "claude", "--dangerously-skip-permissions",
            "--model", REFRESH_PROBE_MODEL,
            "--print", "--max-turns", "1",
            "--output-format", "stream-json", "--verbose",
            "ok",
        ]
        started = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(host_home),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "claude credential refresh timed out after %ds",
                REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
            return RefreshOutcome.FAILED
        except FileNotFoundError:
            logger.warning(
                "claude credential refresh: claude binary missing on PATH"
            )
            return RefreshOutcome.FAILED
        elapsed = time.time() - started

        if proc.returncode != 0:
            err_tail = stderr.decode("utf-8", errors="replace").strip()[-400:]
            out_tail = stdout.decode("utf-8", errors="replace").strip()[-400:]
            if _looks_like_rate_limit(out_tail, err_tail):
                logger.warning(
                    "claude credential refresh rate-limited rc=%d in %.1fs "
                    "| stdout: %s | stderr: %s",
                    proc.returncode, elapsed, out_tail, err_tail,
                )
                return RefreshOutcome.RATE_LIMITED
            logger.warning(
                "claude credential refresh rc=%d in %.1fs | stdout: %s | stderr: %s",
                proc.returncode, elapsed, out_tail, err_tail,
            )
            return RefreshOutcome.FAILED

        # Pull the post-refresh blob straight from Keychain — that's
        # where claude wrote it. Then sync our cache so agent fan-out
        # has fresh bytes.
        kr_after = read_keychain_blob()
        if not kr_after.ok or not kr_after.blob:
            logger.warning(
                "claude credential refresh exit=0 but Keychain re-read "
                "failed (%s); cache untouched",
                kr_after.error,
            )
            return RefreshOutcome.FAILED
        try:
            self.cache.write(kr_after.blob)
        except OSError as exc:
            logger.warning(
                "claude credential refresh: cache write failed: %s", exc,
            )

        # Byte-compare the Keychain blob before and after. Anything
        # else (e.g. comparing int(expires_in_seconds)) loses
        # sub-second resolution to time.time()'s fractional part.
        if before_blob is not None and before_blob == kr_after.blob:
            logger.info(
                "claude credential refresh ok in %.1fs but Keychain "
                "unchanged — token was still fresh; claude skipped the "
                "OAuth round-trip",
                elapsed,
            )
            return RefreshOutcome.UNCHANGED

        self._last_propagated_blob = kr_after.blob
        logger.info(
            "claude credential refresh ok in %.1fs (Keychain rotated)",
            elapsed,
        )
        return RefreshOutcome.REFRESHED

    def sync_to_agent(self, agent_home: Path) -> None:
        """Atomic-write the cache blob to the agent's per-agent
        ``.credentials.json``. No symlinking — Keychain ACL is on UID
        + signing identity, not HOME, so a symlink to the host file
        gives no benefit and the per-agent file diverges anyway when
        claude self-refreshes inside the agent's process."""
        blob = self.cache.read()
        if not blob:
            return
        agent_claude = agent_home / ".claude"
        try:
            agent_claude.mkdir(parents=True, exist_ok=True)
            target = agent_claude / ".credentials.json"
            tmp = agent_claude / f".{target.name}.tmp.{os.getpid()}"
            tmp.write_text(blob, encoding="utf-8")
            try:
                import stat as _stat
                tmp.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
            except OSError:
                pass
            os.replace(tmp, target)
        except OSError as exc:
            logger.warning(
                "keychain backend: sync to %s failed: %s",
                agent_home, exc,
            )

    async def bootstrap(self) -> tuple[bool, Optional[str]]:
        from ..macos.keychain import bootstrap_from_keychain

        ok, reason = bootstrap_from_keychain(self.cache)
        if ok:
            self._last_propagated_blob = self.cache.read()
        return (ok, reason)

    async def poll_external_rotation(self) -> bool:
        """Read Keychain and compare to the last-propagated blob.
        Returns True when a rotation was detected and the cache was
        updated; the caller (``CredentialRefresher``) then fans the
        new blob to every registered agent via ``_sync_views``.
        """
        from ..macos.keychain import read_keychain_blob

        kr = read_keychain_blob()
        if not kr.ok or not kr.blob:
            logger.debug(
                "keychain poll: read failed (%s); will retry next tick",
                kr.error,
            )
            return False
        if kr.blob == self._last_propagated_blob:
            return False
        self._last_propagated_blob = kr.blob
        try:
            self.cache.write(kr.blob)
        except OSError as exc:
            logger.warning("keychain poll: cache write failed: %s", exc)
            return False
        logger.info("keychain poll: token rotation detected; fanning out")
        return True


# ─────────────────────────────────────────────────────────────────────────────
# CredentialRefresher — platform-agnostic owner of poll + fan-out
# ─────────────────────────────────────────────────────────────────────────────

class CredentialRefresher:
    """Daemon-level Claude credential refresh coordinator.

    Owns the cross-cutting concerns (lock, agent registry, poll loop,
    fan-out) and delegates platform specifics to a ``CredentialBackend``.
    """

    def __init__(
        self,
        backend: CredentialBackend | None = None,
        *,
        host_home: Path | None = None,
    ):
        # Backwards-compat shim: callers that pass ``host_home=...``
        # (the pre-backend constructor signature) get an implicit
        # ``FileBackend``. This keeps existing tests pinning the
        # old API working unchanged.
        if backend is None:
            if host_home is None:
                raise TypeError(
                    "CredentialRefresher requires either `backend` or `host_home`"
                )
            backend = FileBackend(host_home=host_home)
        self.backend = backend
        # FileBackend exposes the host_home; preserve the attribute so
        # the existing test ``test_credential_refresher.py`` can read
        # ``r.host_credentials`` if it wants to.
        if isinstance(backend, FileBackend):
            self.host_home = backend.host_home
            self.host_credentials = backend.host_credentials
        self._refresh_request = asyncio.Event()
        self._agent_homes: set[Path] = set()
        self._on_refresh_success: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()
        self._consecutive_non_success = 0
        # In-flight fast-retry task spawned on a RATE_LIMITED outcome.
        # Tracked so back-to-back hits coalesce (no pile-up).
        self._rate_limit_retry_task: asyncio.Task | None = None

    def register_agent(self, agent_home: Path) -> None:
        self._agent_homes.add(Path(agent_home))

    def unregister_agent(self, agent_home: Path) -> None:
        self._agent_homes.discard(Path(agent_home))

    def register_on_refresh_success(self, callback: Callable[[], None]) -> None:
        self._on_refresh_success.append(callback)

    def unregister_on_refresh_success(self, callback: Callable[[], None]) -> None:
        try:
            self._on_refresh_success.remove(callback)
        except ValueError:
            pass

    def _fire_refresh_success(self) -> None:
        # list(...) defensive copy: callback may (un)register during dispatch.
        for cb in list(self._on_refresh_success):
            try:
                cb()
            except Exception as exc:
                logger.warning(
                    "credential refresh-success callback raised: %s", exc,
                )

    def notify_refresh_needed(self) -> None:
        """In-process trigger from an agent that just saw a 401."""
        self._refresh_request.set()

    def expires_in_seconds(self) -> int | None:
        return self.backend.expires_in_seconds()

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        """Main daemon coroutine. Polls every REFRESH_POLL_SECONDS or
        wakes early when an agent reports a 401. macOS backend also
        runs an external-rotation poll on its own cadence as a sibling
        task."""
        logger.info(
            "credential refresher started (backend=%s, poll=%ds, margin=%ds)",
            type(self.backend).__name__,
            REFRESH_POLL_SECONDS,
            REFRESH_SAFETY_MARGIN_SECONDS,
        )
        # Optional bootstrap — primarily for ``KeychainBackend``.
        try:
            ok, reason = await self.backend.bootstrap()
            if not ok:
                logger.warning(
                    "credential backend bootstrap reported not-ok: %s", reason,
                )
        except Exception as exc:
            logger.warning("credential backend bootstrap errored: %s", exc)

        # macOS-only sibling task: 5-min external-rotation poll. The
        # ``CredentialRefresher`` itself stays platform-agnostic; we
        # detect the capability via duck-typing on the backend so we
        # don't import the macos module up here.
        external_poll_task: asyncio.Task | None = None
        if hasattr(self.backend, "poll_external_rotation"):
            external_poll_task = asyncio.ensure_future(
                self._external_rotation_loop(stop_event),
            )

        try:
            while not stop_event.is_set():
                triggered_by_agent = self._refresh_request.is_set()
                self._refresh_request.clear()
                try:
                    await self._tick(triggered_by_agent=triggered_by_agent)
                except Exception as exc:
                    logger.warning("credential refresher tick errored: %s", exc)
                if stop_event.is_set():
                    break
                await self._sleep_until_next_tick(stop_event)
        finally:
            if external_poll_task is not None:
                external_poll_task.cancel()
                try:
                    await external_poll_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _external_rotation_loop(self, stop_event: asyncio.Event) -> None:
        """KeychainBackend-only: poll Keychain every
        ``KEYCHAIN_POLL_INTERVAL_SECONDS``, fan out on detected
        rotation. Runs as a sibling task to the main run_loop so the
        two cadences don't entangle (file-expiry poll = 2 min;
        external-rotation poll = 5 min)."""
        # Lazy import — keeps the platform-agnostic module free of a
        # hard dependency on the macos package, matching the pattern
        # used inside ``KeychainBackend`` for every other macos
        # touchpoint.
        from ..macos.keychain import KEYCHAIN_POLL_INTERVAL_SECONDS

        interval = KEYCHAIN_POLL_INTERVAL_SECONDS
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            if stop_event.is_set():
                return
            try:
                rotated = await self.backend.poll_external_rotation()
            except Exception as exc:
                logger.warning("external-rotation poll errored: %s", exc)
                continue
            if rotated:
                self._sync_views()
                self._fire_refresh_success()

    async def _sleep_until_next_tick(self, stop_event: asyncio.Event) -> None:
        stop_task = asyncio.create_task(stop_event.wait())
        refresh_task = asyncio.create_task(self._refresh_request.wait())
        try:
            await asyncio.wait(
                {stop_task, refresh_task},
                timeout=REFRESH_POLL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
            refresh_task.cancel()

    async def _tick(self, *, triggered_by_agent: bool = False) -> None:
        """One refresh cycle: check expiry, refresh if needed, sync
        views regardless so external rotation propagates."""
        expires_in = self.expires_in_seconds()
        if expires_in is None and not triggered_by_agent:
            self._sync_views()
            return
        should_refresh = triggered_by_agent or (
            expires_in is not None
            and expires_in <= REFRESH_SAFETY_MARGIN_SECONDS
        )
        if should_refresh:
            await self._refresh_now(
                expires_in=expires_in, by_agent=triggered_by_agent,
            )
        self._sync_views()

    async def _refresh_now(
        self, *, expires_in: int | None, by_agent: bool,
    ) -> None:
        """Single-writer refresh through the backend; the lock makes
        the rotating-RT race unwinnable."""
        # Fire only on REFRESHED: UNCHANGED / FAILED leave the on-disk
        # token unchanged, so clearing auth_failed would oscillate.
        outcome: RefreshOutcome | None = None
        async with self._lock:
            before = self.expires_in_seconds()
            if (
                not by_agent
                and before is not None
                and before > REFRESH_SAFETY_MARGIN_SECONDS
            ):
                logger.debug(
                    "another caller already refreshed (now expires in %ds)",
                    before,
                )
                return
            logger.info(
                "refreshing credentials (expires_in=%s, by_agent=%s)",
                expires_in, by_agent,
            )
            try:
                outcome = await self.backend.refresh()
            except Exception as exc:
                logger.warning("backend refresh errored: %s", exc)
                outcome = RefreshOutcome.FAILED
            self._propagate_outcome(outcome)
        if outcome is RefreshOutcome.REFRESHED:
            self._fire_refresh_success()

    def _propagate_outcome(self, outcome: RefreshOutcome) -> None:
        # RATE_LIMITED counts toward the refresh_broken streak (same
        # as FAILED) AND schedules a fast retry so the loop doesn't
        # park on the full 120s wait when the throttle clears.
        if outcome is RefreshOutcome.REFRESHED:
            if self._consecutive_non_success > 0:
                logger.info(
                    "credential refresh recovered after %d non-success "
                    "tick(s) — clearing refresh_broken health on "
                    "registered agents",
                    self._consecutive_non_success,
                )
            # Always clear: a daemon restart resets the in-memory counter
            # to 0 while leaving on-disk ``refresh_broken`` from the
            # previous instance — without the unconditional call those
            # agents stay stuck. _clear_refresh_broken is idempotent.
            self._clear_refresh_broken()
            self._consecutive_non_success = 0
            return
        self._consecutive_non_success += 1
        if self._consecutive_non_success >= REFRESH_BROKEN_THRESHOLD:
            self._flip_refresh_broken(outcome)
        if outcome is RefreshOutcome.RATE_LIMITED:
            self._schedule_rate_limit_retry()

    def _schedule_rate_limit_retry(self) -> None:
        """Spawn a background task that pings ``_refresh_request`` after
        a randomised ``[5, 15]`` s delay so the next refresh attempt
        fires before the natural 120s poll deadline. Multiple
        concurrent rate-limit hits coalesce into one pending retry —
        we don't pile up tasks if every poll keeps hitting the limit."""
        existing = self._rate_limit_retry_task
        if existing is not None and not existing.done():
            return
        delay = random.uniform(
            RATE_LIMIT_FAST_RETRY_MIN_SECONDS,
            RATE_LIMIT_FAST_RETRY_MAX_SECONDS,
        )
        logger.info(
            "credential refresh rate-limited — scheduling fast retry "
            "in %.1fs (vs. %ds natural poll)",
            delay, REFRESH_POLL_SECONDS,
        )

        async def _retry() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            self._refresh_request.set()

        coro = _retry()
        try:
            self._rate_limit_retry_task = asyncio.create_task(coro)
        except RuntimeError:
            # No running loop yet — happens in some unit-test paths.
            # Close the coroutine cleanly (no "never awaited" warning)
            # and rely on the natural poll to pick the retry up.
            coro.close()
            self._rate_limit_retry_task = None

    def _flip_refresh_broken(self, outcome: RefreshOutcome) -> None:
        from .state import RuntimeState
        msg = (
            f"daemon CredentialRefresher saw {self._consecutive_non_success} "
            f"consecutive {outcome.value} outcome(s); on-disk token isn't "
            f"advancing. Run `claude /login` then "
            f"`puffo-agent agent resume <id>` to recover."
        )
        for agent_home in self._agent_homes:
            agent_id = Path(agent_home).name
            try:
                rs = RuntimeState.load(agent_id)
            except Exception as exc:
                logger.warning(
                    "refresh_broken flip: failed to load runtime for %s: %s",
                    agent_id, exc,
                )
                continue
            if rs is None:
                continue
            if rs.health in (
                "auth_failed", "api_error_abandoned", "refresh_broken",
            ):
                continue
            rs.health = "refresh_broken"
            rs.error = msg
            try:
                rs.save(agent_id)
            except Exception as exc:
                logger.warning(
                    "refresh_broken flip: failed to save runtime for %s: %s",
                    agent_id, exc,
                )

    def _clear_refresh_broken(self) -> None:
        from .state import RuntimeState
        for agent_home in self._agent_homes:
            agent_id = Path(agent_home).name
            try:
                rs = RuntimeState.load(agent_id)
            except Exception:
                continue
            if rs is None or rs.health != "refresh_broken":
                continue
            rs.health = "ok"
            rs.error = ""
            try:
                rs.save(agent_id)
            except Exception as exc:
                logger.warning(
                    "refresh_broken clear: failed to save runtime for %s: %s",
                    agent_id, exc,
                )

    def _sync_views(self) -> None:
        """Mirror canonical credentials to every registered agent."""
        for agent_home in self._agent_homes:
            try:
                self.backend.sync_to_agent(agent_home)
            except Exception as exc:
                logger.warning(
                    "credential view-sync failed for %s: %s",
                    agent_home, exc,
                )
