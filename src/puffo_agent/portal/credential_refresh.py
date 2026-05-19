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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from .state import link_host_credentials


logger = logging.getLogger(__name__)


REFRESH_POLL_SECONDS = 120
REFRESH_SAFETY_MARGIN_SECONDS = 10 * 60
REFRESH_ONESHOT_TIMEOUT_SECONDS = 120

# Re-export from macos.keychain for callers that want the constant
# without importing the macos package directly.
from ..macos.keychain import KEYCHAIN_POLL_INTERVAL_SECONDS  # noqa: E402


class RefreshOutcome(enum.Enum):
    """Result of a single backend refresh attempt."""
    REFRESHED = "refreshed"
    UNCHANGED = "unchanged"
    FAILED = "failed"


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
        cmd = [
            "claude", "--dangerously-skip-permissions",
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
            logger.warning(
                "credential refresh rc=%d in %.1fs | stdout: %s | stderr: %s",
                proc.returncode, elapsed, out_tail, err_tail,
            )
            return RefreshOutcome.FAILED
        if before is not None and after is not None and after <= before:
            logger.error(
                "credential refresh exit=0 but expiresAt didn't advance "
                "(before=%ds, after=%ds) — claude may not be rewriting "
                "credentials.json on this build; operator may need "
                "`claude /login` to recover",
                before, after,
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
# KeychainBackend — macOS
# ─────────────────────────────────────────────────────────────────────────────

class KeychainBackend:
    """macOS backend. The Keychain is the canonical store; the daemon
    maintains a cache file and propagates rotations to every running
    agent via per-agent file copies (Keychain ACL is keyed on UID +
    signing identity, not HOME, so a symlink trick wouldn't help).

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
        shim_dir: Path,
    ):
        self.home = home
        self.cache = cache
        self.shim_dir = shim_dir
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
        from ..macos.keychain import (
            refresh_via_oneshot,
            writeback_to_keychain,
        )

        before = self.cache.read()
        ok, reason = await refresh_via_oneshot(self.cache, self.shim_dir)
        if not ok:
            logger.warning(
                "claude credential refresh failed: %s", reason,
            )
            return RefreshOutcome.FAILED
        logger.info("claude credential refresh: %s", reason)
        after = self.cache.read()
        if after and after != before:
            # Push the rotated blob back to Keychain best-effort so the
            # operator's main CLI / VS Code extension see the new token.
            wb_ok, wb_reason = writeback_to_keychain(after)
            if wb_ok:
                logger.info("claude credential writeback to keychain: ok")
            else:
                logger.info(
                    "claude credential writeback to keychain skipped: %s",
                    wb_reason,
                )
            self._last_propagated_blob = after
            return RefreshOutcome.REFRESHED
        return RefreshOutcome.UNCHANGED

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
        from ..macos.keychain import (
            bootstrap_from_keychain,
            install_path_shim,
        )

        install_path_shim(self.home)
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
        self._lock = asyncio.Lock()

    def register_agent(self, agent_home: Path) -> None:
        self._agent_homes.add(Path(agent_home))

    def unregister_agent(self, agent_home: Path) -> None:
        self._agent_homes.discard(Path(agent_home))

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
        """Single-writer refresh through the backend. The
        ``asyncio.Lock`` is what makes the rotating-RT race
        unwinnable: a second caller can't see an in-flight rotation
        mid-write."""
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
                await self.backend.refresh()
            except Exception as exc:
                logger.warning("backend refresh errored: %s", exc)

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
