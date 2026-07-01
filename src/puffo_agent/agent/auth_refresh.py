"""Remote auth refresh for Claude Code / Codex.

Two independent state machines — the CLIs' headless auth shapes
don't align:

- Claude (``claude auth login``) prints an OAuth URL whose
  ``redirect_uri`` is a hosted callback page (not localhost).
  The operator opens the URL on any device, gets a code, pastes
  it back through the daemon control channel; the CLI accepts
  the code on stdin.
- Codex (``codex login --device-auth``) prints URL + one-time
  device code; the CLI polls the token endpoint itself and
  self-completes on operator authorization. No paste-back.
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
    SPAWNING = "spawning"
    AWAITING_TOKEN = "awaiting_token"
    POLLING = "polling"
    APPLYING = "applying"
    DONE = "done"
    FAILED = "failed"


@dataclass
class LoginResult:
    ok: bool
    url: str | None = None
    device_code: str | None = None
    credentials_path: Path | None = None
    error: str | None = None


class ClaudeRunner(Protocol):
    async def spawn(self) -> LoginResult: ...
    async def submit_token(self, token: str) -> LoginResult: ...
    async def cancel(self) -> None: ...


class CodexRunner(Protocol):
    async def spawn(self) -> LoginResult: ...
    async def wait_until_complete(self) -> LoginResult: ...
    async def cancel(self) -> None: ...


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_URL = re.compile(r"https?://[^\s'\"<>]+")
_DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b")


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


@dataclass
class _SubprocessBase:
    command: list[str]
    credentials_path: Path | None = None
    timeout_seconds: float = 600.0

    _proc: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _stdout_buffer: list[str] = field(default_factory=list, init=False, repr=False)
    _spawn_at: float = field(default=0.0, init=False, repr=False)

    async def _launch(self) -> LoginResult | None:
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
        return None

    async def _readline_stripped(self) -> str | None:
        assert self._proc and self._proc.stdout
        try:
            line_bytes = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=1.0
            )
        except asyncio.TimeoutError:
            return ""
        if not line_bytes:
            return None
        line = _strip_ansi(line_bytes.decode("utf-8", errors="replace"))
        self._stdout_buffer.append(line)
        return line

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


@dataclass
class ClaudeSubprocessRunner(_SubprocessBase):
    async def spawn(self) -> LoginResult:
        failure = await self._launch()
        if failure is not None:
            return failure
        deadline = self._spawn_at + self.timeout_seconds
        while time.monotonic() <= deadline:
            line = await self._readline_stripped()
            if line is None:
                await self.cancel()
                return LoginResult(ok=False, error="login subprocess exited before URL")
            match = _URL.search(line)
            if match:
                return LoginResult(ok=True, url=match.group(0))
        await self.cancel()
        return LoginResult(ok=False, error="no URL printed within timeout")

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
            return LoginResult(ok=False, error=f"login subprocess exited with code {returncode}")
        return LoginResult(ok=True, credentials_path=self.credentials_path)


@dataclass
class CodexSubprocessRunner(_SubprocessBase):
    async def spawn(self) -> LoginResult:
        failure = await self._launch()
        if failure is not None:
            return failure
        url: str | None = None
        device_code: str | None = None
        deadline = self._spawn_at + self.timeout_seconds
        while time.monotonic() <= deadline and not (url and device_code):
            line = await self._readline_stripped()
            if line is None:
                await self.cancel()
                return LoginResult(ok=False, error="login subprocess exited before URL")
            if url is None:
                m = _URL.search(line)
                if m:
                    url = m.group(0)
                    continue
            if device_code is None:
                m = _DEVICE_CODE.search(line)
                if m:
                    device_code = m.group(0)
        if not url:
            await self.cancel()
            return LoginResult(ok=False, error="no URL printed within timeout")
        return LoginResult(ok=True, url=url, device_code=device_code)

    async def wait_until_complete(self) -> LoginResult:
        if not self._proc:
            return LoginResult(ok=False, error="subprocess not running")
        elapsed = time.monotonic() - self._spawn_at
        remaining = max(1.0, self.timeout_seconds - elapsed)
        try:
            returncode = await asyncio.wait_for(self._proc.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            await self.cancel()
            return LoginResult(ok=False, error="device-auth polling timed out")
        if returncode != 0:
            return LoginResult(ok=False, error=f"login subprocess exited with code {returncode}")
        return LoginResult(ok=True, credentials_path=self.credentials_path)


def claude_login_runner() -> ClaudeSubprocessRunner:
    cmd = os.environ.get("PUFFO_CLAUDE_LOGIN_CMD", "claude auth login --claudeai")
    return ClaudeSubprocessRunner(
        command=shlex.split(cmd),
        credentials_path=Path.home() / ".claude" / ".credentials.json",
    )


def codex_login_runner() -> CodexSubprocessRunner:
    # ``codex login`` (bare) opens a local-callback URL on the daemon
    # host; ``--device-auth`` prints URL + operator-enterable code so
    # no callback host is needed.
    cmd = os.environ.get("PUFFO_CODEX_LOGIN_CMD", "codex login --device-auth")
    return CodexSubprocessRunner(
        command=shlex.split(cmd),
        credentials_path=Path.home() / ".codex" / "auth.json",
    )


RestartAllOwned = Callable[[], Awaitable[int]]
EmitToOperator = Callable[[str, dict], Awaitable[None]]


@dataclass
class _FlowBase:
    emit: EmitToOperator
    restart_all_owned: RestartAllOwned
    provider_label: str = ""

    _state: FlowState = field(default=FlowState.IDLE, init=False)
    _operator_slug: str = field(default="", init=False)
    _started_at: float = field(default=0.0, init=False)

    @property
    def state(self) -> FlowState:
        return self._state

    async def _restart(self) -> int:
        try:
            return await self.restart_all_owned()
        except Exception as exc:  # noqa: BLE001
            logger.warning("auth-refresh: restart_all_owned failed: %s", exc)
            return -1

    async def _safe_emit(self, payload: dict) -> None:
        if not self._operator_slug:
            return
        try:
            await self.emit(self._operator_slug, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("auth-refresh: emit failed: %s", exc)


@dataclass
class ClaudeAuthFlow(_FlowBase):
    """spawn → paste-back token → apply."""

    runner_factory: Callable[[], ClaudeRunner] = field(default=claude_login_runner)

    _runner: ClaudeRunner | None = field(default=None, init=False)

    async def start(self, operator_slug: str) -> dict:
        if self._state in (FlowState.SPAWNING, FlowState.AWAITING_TOKEN, FlowState.APPLYING):
            return {"ok": False, "error": "claude login already in progress"}
        self._runner = self.runner_factory()
        self._operator_slug = operator_slug
        self._started_at = time.time()
        self._state = FlowState.SPAWNING

        result = await self._runner.spawn()
        if not result.ok:
            self._state = FlowState.FAILED
            await self._safe_emit({
                "type": "auth-refresh.error", "provider": "claude",
                "stage": "spawn", "error": result.error,
            })
            return {"ok": False, "error": result.error}

        self._state = FlowState.AWAITING_TOKEN
        await self._safe_emit({
            "type": "auth-refresh.url", "provider": "claude", "url": result.url,
        })
        return {"ok": True, "url": result.url}

    async def submit_token(self, token: str, operator_slug: str) -> dict:
        if self._state != FlowState.AWAITING_TOKEN:
            return {"ok": False, "error": "no claude login awaiting a token"}
        if operator_slug != self._operator_slug:
            return {"ok": False, "error": "different operator owns this flow"}
        assert self._runner is not None

        self._state = FlowState.APPLYING
        result = await self._runner.submit_token(token)
        if not result.ok:
            self._state = FlowState.FAILED
            await self._safe_emit({
                "type": "auth-refresh.error", "provider": "claude",
                "stage": "apply", "error": result.error,
            })
            return {"ok": False, "error": result.error}

        restarted = await self._restart()
        self._state = FlowState.DONE
        await self._safe_emit({
            "type": "auth-refresh.done", "provider": "claude",
            "agents_restarted": restarted,
        })
        return {"ok": True, "agents_restarted": restarted}

    async def cancel(self) -> dict:
        if self._runner is not None:
            await self._runner.cancel()
        self._state = FlowState.IDLE
        self._runner = None
        self._operator_slug = ""
        return {"ok": True, "state": "idle"}


@dataclass
class CodexAuthFlow(_FlowBase):
    """spawn → CLI polls the token endpoint → apply. No paste-back;
    a background watcher awaits CLI exit and triggers the apply."""

    runner_factory: Callable[[], CodexRunner] = field(default=codex_login_runner)

    _runner: CodexRunner | None = field(default=None, init=False)
    _watcher: asyncio.Task | None = field(default=None, init=False)

    async def start(self, operator_slug: str) -> dict:
        if self._state in (FlowState.SPAWNING, FlowState.POLLING, FlowState.APPLYING):
            return {"ok": False, "error": "codex login already in progress"}
        self._runner = self.runner_factory()
        self._operator_slug = operator_slug
        self._started_at = time.time()
        self._state = FlowState.SPAWNING

        result = await self._runner.spawn()
        if not result.ok:
            self._state = FlowState.FAILED
            await self._safe_emit({
                "type": "auth-refresh.error", "provider": "codex",
                "stage": "spawn", "error": result.error,
            })
            return {"ok": False, "error": result.error}

        self._state = FlowState.POLLING
        payload = {
            "type": "auth-refresh.url", "provider": "codex",
            "url": result.url, "device_code": result.device_code,
        }
        await self._safe_emit(payload)
        self._watcher = asyncio.create_task(self._watch())
        return {
            "ok": True, "url": result.url, "device_code": result.device_code,
        }

    async def _watch(self) -> None:
        assert self._runner is not None
        try:
            result = await self._runner.wait_until_complete()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._state = FlowState.FAILED
            await self._safe_emit({
                "type": "auth-refresh.error", "provider": "codex",
                "stage": "poll", "error": str(exc),
            })
            return
        if not result.ok:
            self._state = FlowState.FAILED
            await self._safe_emit({
                "type": "auth-refresh.error", "provider": "codex",
                "stage": "poll", "error": result.error,
            })
            return
        self._state = FlowState.APPLYING
        restarted = await self._restart()
        self._state = FlowState.DONE
        await self._safe_emit({
            "type": "auth-refresh.done", "provider": "codex",
            "agents_restarted": restarted,
        })

    async def cancel(self) -> dict:
        if self._watcher is not None and not self._watcher.done():
            self._watcher.cancel()
            try:
                await self._watcher
            except (asyncio.CancelledError, Exception):
                pass
        if self._runner is not None:
            await self._runner.cancel()
        self._state = FlowState.IDLE
        self._runner = None
        self._watcher = None
        self._operator_slug = ""
        return {"ok": True, "state": "idle"}


@dataclass
class AuthRefreshCoordinator:
    emit: EmitToOperator
    restart_all_owned: RestartAllOwned
    claude_factory: Callable[[], ClaudeRunner] = field(default=claude_login_runner)
    codex_factory: Callable[[], CodexRunner] = field(default=codex_login_runner)

    _claude: ClaudeAuthFlow = field(init=False)
    _codex: CodexAuthFlow = field(init=False)

    def __post_init__(self) -> None:
        self._claude = ClaudeAuthFlow(
            emit=self.emit,
            restart_all_owned=self.restart_all_owned,
            runner_factory=self.claude_factory,
        )
        self._codex = CodexAuthFlow(
            emit=self.emit,
            restart_all_owned=self.restart_all_owned,
            runner_factory=self.codex_factory,
        )

    async def start_claude(self, operator_slug: str) -> dict:
        return await self._claude.start(operator_slug)

    async def submit_claude_token(self, token: str, operator_slug: str) -> dict:
        return await self._claude.submit_token(token, operator_slug)

    async def start_codex(self, operator_slug: str) -> dict:
        return await self._codex.start(operator_slug)

    async def cancel(self, provider: Provider) -> dict:
        if provider == Provider.CLAUDE:
            return await self._claude.cancel()
        return await self._codex.cancel()

    def state(self, provider: Provider) -> FlowState:
        return self._claude.state if provider == Provider.CLAUDE else self._codex.state

    @property
    def claude(self) -> ClaudeAuthFlow:
        return self._claude

    @property
    def codex(self) -> CodexAuthFlow:
        return self._codex


_COORDINATOR: AuthRefreshCoordinator | None = None


def set_auth_refresh_coordinator(coord: AuthRefreshCoordinator | None) -> None:
    global _COORDINATOR
    _COORDINATOR = coord


def get_auth_refresh_coordinator() -> AuthRefreshCoordinator | None:
    return _COORDINATOR
