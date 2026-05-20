"""PUF-221: daemon-owned Claude OAuth credential refresh.

Replaces the per-agent ``refresh_ping`` from
``agent/adapters/base.py``. Every refresh of the on-disk Claude
credentials file goes through ONE process — the puffo-agent
daemon — so Anthropic's single-use refresh-token rotation can't
be raced by N agent workers all reading the same disk RT and
burning each other's in-memory copies.

Implementation: the daemon owns one ``CredentialRefresher``
instance. Its ``run_loop`` coroutine polls
``~/.claude/.credentials.json`` every ``REFRESH_POLL_SECONDS``
and triggers a refresh when ``expiresAt - now <
REFRESH_SAFETY_MARGIN_SECONDS``, OR when something calls
``notify_refresh_needed()``. Today's two callers: (a) the
``puffo-agent agent refresh-token`` CLI subcommand, which drops
a sentinel file the daemon's reconcile loop forwards into the
in-process ``asyncio.Event``; (b) the per-worker
``on_auth_failure`` hook fired from
``worker._handle_suppressed_reply`` when an auth-class leak is
detected in a turn reply. The event short-circuits the 2-minute
poll so an operator- or agent-initiated refresh runs within ~1s.

The refresh itself shells out to ``claude --print "ok"`` with
``HOME=<host_home>`` — mechanically identical to the cli-docker
``_run_refresh_oneshot`` pattern that's been proven to work on
Windows + macOS. Single-writer semantics: the daemon's mutex
ensures only one ``claude --print`` is in flight at a time, so
Anthropic's RT rotation can't be observed mid-write by another
caller.

After every successful refresh (or on every poll, as a safety
net for the operator running ``claude /login`` externally on the
device), the refresher fans out
``state.link_host_credentials(host_home, agent_home)`` to each
registered agent so per-agent symlinks / copy-mode copies stay
in sync with the host file.

Future: a follow-up PUF can replace the subprocess shell-out
with a direct ``aiohttp.post`` to Anthropic's ``/oauth/token``
endpoint once the endpoint + request shape are operator-
verified. The subprocess path is safer to ship first because it
re-uses the exact mechanism cli-docker already proves works.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from .state import link_host_credentials


logger = logging.getLogger(__name__)


REFRESH_POLL_SECONDS = 120
REFRESH_SAFETY_MARGIN_SECONDS = 10 * 60
REFRESH_ONESHOT_TIMEOUT_SECONDS = 120


class CredentialRefresher:
    """Daemon-level Claude credential refresh coordinator."""

    def __init__(self, host_home: Path):
        self.host_home = host_home
        self.host_credentials = host_home / ".claude" / ".credentials.json"
        self._refresh_request = asyncio.Event()
        self._agent_homes: set[Path] = set()
        self._lock = asyncio.Lock()

    def register_agent(self, agent_home: Path) -> None:
        """Add an agent home to the view-sync fan-out list. Called
        when each Worker starts so the daemon knows where to mirror
        credentials after a refresh."""
        self._agent_homes.add(Path(agent_home))

    def unregister_agent(self, agent_home: Path) -> None:
        self._agent_homes.discard(Path(agent_home))

    def notify_refresh_needed(self) -> None:
        """In-process trigger from an agent that just saw a 401. Wakes
        the run_loop immediately rather than waiting for the next
        2-minute poll. Safe to call from any coroutine; idempotent
        within a single refresh cycle."""
        self._refresh_request.set()

    def expires_in_seconds(self) -> int | None:
        """Seconds until the disk access token expires (negative if
        past). ``None`` if file unreadable / not OAuth."""
        try:
            data = json.loads(self.host_credentials.read_text(encoding="utf-8"))
            expires_ms = int(data["claudeAiOauth"]["expiresAt"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return int(expires_ms / 1000 - time.time())

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        """Main daemon coroutine. Polls every REFRESH_POLL_SECONDS or
        wakes early when an agent reports a 401."""
        logger.info(
            "credential refresher started (host=%s, poll=%ds, margin=%ds)",
            self.host_credentials,
            REFRESH_POLL_SECONDS,
            REFRESH_SAFETY_MARGIN_SECONDS,
        )
        while not stop_event.is_set():
            # Capture and clear the agent-trigger atomically so a
            # notification arriving mid-tick survives to the NEXT
            # iteration rather than being consumed twice.
            triggered_by_agent = self._refresh_request.is_set()
            self._refresh_request.clear()
            try:
                await self._tick(triggered_by_agent=triggered_by_agent)
            except Exception as exc:
                logger.warning("credential refresher tick errored: %s", exc)
            if stop_event.is_set():
                break
            await self._sleep_until_next_tick(stop_event)

    async def _sleep_until_next_tick(self, stop_event: asyncio.Event) -> None:
        """Wait for poll interval, OR an agent's 401 notification,
        OR daemon shutdown — whichever fires first. The event itself
        stays set; the next loop iteration reads + clears it before
        ``_tick`` so the notification isn't lost between sleep-wake
        and tick-entry."""
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
        views regardless so external ``claude /login`` propagates."""
        expires_in = self.expires_in_seconds()
        if expires_in is None and not triggered_by_agent:
            self._sync_views()
            return
        should_refresh = triggered_by_agent or (
            expires_in is not None
            and expires_in <= REFRESH_SAFETY_MARGIN_SECONDS
        )
        if should_refresh:
            await self._refresh_now(expires_in=expires_in, by_agent=triggered_by_agent)
        self._sync_views()

    async def _refresh_now(
        self, *, expires_in: int | None, by_agent: bool,
    ) -> None:
        """Spawn ``claude --print "ok"`` with HOST_HOME so Claude
        Code's internal OAuth refresh fires + writes back to disk
        on exit. Serialized by ``self._lock`` so concurrent triggers
        don't race."""
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
            env = {**os.environ, "HOME": str(self.host_home)}
            cmd = [
                "claude", "--dangerously-skip-permissions",
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
                return
            except FileNotFoundError:
                logger.warning(
                    "credential refresh: claude binary missing on PATH"
                )
                return
            elapsed = time.time() - started
            after = self.expires_in_seconds()
            if proc.returncode != 0:
                err_tail = stderr.decode("utf-8", errors="replace").strip()[-400:]
                out_tail = stdout.decode("utf-8", errors="replace").strip()[-400:]
                logger.warning(
                    "credential refresh rc=%d in %.1fs | stdout: %s | stderr: %s",
                    proc.returncode, elapsed, out_tail, err_tail,
                )
                return
            if before is not None and after is not None and after <= before:
                logger.error(
                    "credential refresh exit=0 but expiresAt didn't advance "
                    "(before=%ds, after=%ds) — claude may not be rewriting "
                    "credentials.json on this build; operator may need "
                    "`claude /login` to recover",
                    before, after,
                )
                return
            logger.info(
                "credential refresh ok in %.1fs (expires_in: %s -> %s)",
                elapsed, before, after,
            )

    def _sync_views(self) -> None:
        """Mirror the host credentials to every registered agent's
        per-agent home. Handles the case where the operator ran
        ``claude /login`` externally on the device — host file
        changed, agent views need to catch up."""
        for agent_home in self._agent_homes:
            try:
                link_host_credentials(self.host_home, agent_home)
            except Exception as exc:
                logger.warning(
                    "credential view-sync failed for %s: %s",
                    agent_home, exc,
                )
