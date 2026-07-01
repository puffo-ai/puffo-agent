"""Remote auth refresh for Claude Code / Codex.

State machine per provider: idle → headless_login_running →
awaiting_token → applying → done (or failed). Single-flight per
provider; Claude and Codex run independently.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Protocol


logger = logging.getLogger(__name__)


class Provider(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"


class FlowState(str, Enum):
    IDLE = "idle"
    HEADLESS_LOGIN_RUNNING = "headless_login_running"
    AWAITING_TOKEN = "awaiting_token"
    APPLYING = "applying"
    DONE = "done"
    FAILED = "failed"


@dataclass
class LoginAttempt:
    provider: Provider
    state: FlowState
    started_at: float
    url: str | None = None
    error: str | None = None
    runner: "LoginRunner | None" = None
    operator_slug: str = ""


@dataclass
class LoginResult:
    ok: bool
    url: str | None = None
    credentials_path: Path | None = None
    error: str | None = None


class LoginRunner(Protocol):
    async def spawn(self) -> LoginResult: ...
    async def submit_token(self, token: str) -> LoginResult: ...
    async def cancel(self) -> None: ...


URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")


@dataclass
class SubprocessLoginRunner:
    command: list[str]
    credentials_path: Path | None = None
    timeout_seconds: float = 600.0

    _proc: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _stdout_buffer: list[str] = field(default_factory=list, init=False, repr=False)
    _spawn_at: float = field(default=0.0, init=False, repr=False)

    async def spawn(self) -> LoginResult:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return LoginResult(ok=False, error=f"CLI not found: {self.command[0]!r}")
        except Exception as exc:  # noqa: BLE001
            return LoginResult(ok=False, error=f"subprocess spawn failed: {exc}")
        self._spawn_at = time.monotonic()
        url = await self._read_until_url()
        if url is None:
            await self.cancel()
            return LoginResult(ok=False, error="no URL printed by login subprocess")
        return LoginResult(ok=True, url=url)

    async def _read_until_url(self) -> str | None:
        assert self._proc and self._proc.stdout
        deadline = self._spawn_at + self.timeout_seconds
        while True:
            if time.monotonic() > deadline:
                return None
            try:
                line_bytes = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=1.0
                )
            except asyncio.TimeoutError:
                if self._proc.returncode is not None:
                    return None
                continue
            if not line_bytes:
                return None
            line = line_bytes.decode("utf-8", errors="replace")
            self._stdout_buffer.append(line)
            match = URL_PATTERN.search(line)
            if match:
                return match.group(0)

    async def submit_token(self, token: str) -> LoginResult:
        if not self._proc or self._proc.returncode is not None:
            return LoginResult(ok=False, error="subprocess not running")
        if self._proc.stdin is None or self._proc.stdin.is_closing():
            return LoginResult(ok=False, error="subprocess stdin closed")
        try:
            self._proc.stdin.write(f"{token}\n".encode("utf-8"))
            await self._proc.stdin.drain()
            self._proc.stdin.close()
        except Exception as exc:  # noqa: BLE001
            return LoginResult(ok=False, error=f"stdin write failed: {exc}")
        try:
            returncode = await asyncio.wait_for(
                self._proc.wait(),
                timeout=max(1.0, self.timeout_seconds - (time.monotonic() - self._spawn_at)),
            )
        except asyncio.TimeoutError:
            await self.cancel()
            return LoginResult(ok=False, error="login subprocess timed out after token submit")
        if returncode != 0:
            return LoginResult(
                ok=False,
                error=f"login subprocess exited with code {returncode}",
            )
        return LoginResult(ok=True, credentials_path=self.credentials_path)

    async def cancel(self) -> None:
        if not self._proc:
            return
        if self._proc.returncode is None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass


def claude_login_runner() -> SubprocessLoginRunner:
    cmd = os.environ.get("PUFFO_CLAUDE_LOGIN_CMD", "claude /login")
    return SubprocessLoginRunner(
        command=shlex.split(cmd),
        credentials_path=Path.home() / ".claude" / "auth.json",
    )


def codex_login_runner() -> SubprocessLoginRunner:
    cmd = os.environ.get("PUFFO_CODEX_LOGIN_CMD", "codex login")
    return SubprocessLoginRunner(
        command=shlex.split(cmd),
        credentials_path=Path.home() / ".codex" / "auth.json",
    )


RestartAllOwned = Callable[[], Awaitable[int]]
EmitToOperator = Callable[[str, dict], Awaitable[None]]


@dataclass
class AuthRefreshCoordinator:
    emit: EmitToOperator
    restart_all_owned: RestartAllOwned
    runner_factory_claude: Callable[[], LoginRunner] = field(default=claude_login_runner)
    runner_factory_codex: Callable[[], LoginRunner] = field(default=codex_login_runner)

    _flows: dict[Provider, LoginAttempt] = field(default_factory=dict, init=False, repr=False)

    def _runner_factory(self, provider: Provider) -> Callable[[], LoginRunner]:
        return (
            self.runner_factory_claude
            if provider == Provider.CLAUDE
            else self.runner_factory_codex
        )

    async def start(self, provider: Provider, operator_slug: str) -> dict:
        existing = self._flows.get(provider)
        if existing and existing.state in (
            FlowState.HEADLESS_LOGIN_RUNNING,
            FlowState.AWAITING_TOKEN,
            FlowState.APPLYING,
        ):
            return {"ok": False, "error": f"{provider.value} login already in progress"}

        runner = self._runner_factory(provider)()
        attempt = LoginAttempt(
            provider=provider,
            state=FlowState.HEADLESS_LOGIN_RUNNING,
            started_at=time.time(),
            runner=runner,
            operator_slug=operator_slug,
        )
        self._flows[provider] = attempt

        result = await runner.spawn()
        if not result.ok:
            attempt.state = FlowState.FAILED
            attempt.error = result.error
            await self._safe_emit(
                operator_slug,
                {
                    "type": "auth-refresh.error",
                    "provider": provider.value,
                    "stage": "spawn",
                    "error": result.error,
                },
            )
            return {"ok": False, "error": result.error}

        attempt.state = FlowState.AWAITING_TOKEN
        attempt.url = result.url
        await self._safe_emit(
            operator_slug,
            {
                "type": "auth-refresh.url",
                "provider": provider.value,
                "url": result.url,
            },
        )
        return {"ok": True, "url": result.url}

    async def submit_token(self, provider: Provider, token: str, operator_slug: str) -> dict:
        attempt = self._flows.get(provider)
        if not attempt or attempt.state != FlowState.AWAITING_TOKEN:
            return {
                "ok": False,
                "error": f"no {provider.value} login awaiting a token",
            }
        if operator_slug != attempt.operator_slug:
            return {
                "ok": False,
                "error": "different operator owns this flow",
            }

        attempt.state = FlowState.APPLYING
        assert attempt.runner is not None
        result = await attempt.runner.submit_token(token)
        if not result.ok:
            attempt.state = FlowState.FAILED
            attempt.error = result.error
            await self._safe_emit(
                attempt.operator_slug,
                {
                    "type": "auth-refresh.error",
                    "provider": provider.value,
                    "stage": "apply",
                    "error": result.error,
                },
            )
            return {"ok": False, "error": result.error}

        try:
            restarted = await self.restart_all_owned()
        except Exception as exc:  # noqa: BLE001
            logger.warning("auth-refresh: restart_all_owned failed: %s", exc)
            restarted = -1
        attempt.state = FlowState.DONE
        await self._safe_emit(
            attempt.operator_slug,
            {
                "type": "auth-refresh.done",
                "provider": provider.value,
                "agents_restarted": restarted,
            },
        )
        return {"ok": True, "agents_restarted": restarted}

    async def cancel(self, provider: Provider) -> dict:
        attempt = self._flows.get(provider)
        if not attempt:
            return {"ok": True, "state": "idle"}
        if attempt.runner is not None:
            await attempt.runner.cancel()
        self._flows.pop(provider, None)
        return {"ok": True, "state": "idle"}

    def state(self, provider: Provider) -> FlowState:
        attempt = self._flows.get(provider)
        return attempt.state if attempt else FlowState.IDLE

    async def _safe_emit(self, operator_slug: str, payload: dict) -> None:
        if not operator_slug:
            return
        try:
            await self.emit(operator_slug, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("auth-refresh: emit to %s failed: %s", operator_slug, exc)


def parse_provider_from_op(op: str) -> Provider | None:
    if "claude" in op:
        return Provider.CLAUDE
    if "codex" in op:
        return Provider.CODEX
    return None


_COORDINATOR: AuthRefreshCoordinator | None = None


def set_auth_refresh_coordinator(coord: AuthRefreshCoordinator | None) -> None:
    global _COORDINATOR
    _COORDINATOR = coord


def get_auth_refresh_coordinator() -> AuthRefreshCoordinator | None:
    return _COORDINATOR
