"""Focused recovery classification for Codex app-server JSON-RPC errors."""

from __future__ import annotations

import asyncio
import json

import pytest

from puffo_agent.agent.core import AgentAPIError
from puffo_agent.agent.harness.codex_driver import (
    CodexAppServerDriver,
    _classify_jsonrpc_error,
)
from puffo_agent.agent.harness.driver import PermissionDecision, PermissionRef


@pytest.mark.parametrize(
    "error",
    [
        {"code": 401, "message": "access token has expired"},
        {"code": -32000, "message": "token_invalidated"},
    ],
)
def test_jsonrpc_auth_errors_request_operator_reauthentication(error):
    exc = _classify_jsonrpc_error(error)

    assert isinstance(exc, AgentAPIError)
    assert exc.is_auth is True
    assert "code=" in str(exc)


@pytest.mark.parametrize(
    "error",
    [
        {"code": 429, "message": "rate limit exceeded"},
        {"code": -32000, "data": {"status": 503}, "message": "unavailable"},
    ],
)
def test_jsonrpc_retryable_provider_errors_requeue(error):
    exc = _classify_jsonrpc_error(error)

    assert isinstance(exc, AgentAPIError)
    assert exc.is_auth is False
    assert "code=" in str(exc)


def test_jsonrpc_protocol_error_keeps_safe_diagnostic_without_retry():
    exc = _classify_jsonrpc_error(
        {
            "code": -32602,
            "message": "invalid params authorization=Bearer sk_secret_token_123456789",
        }
    )

    assert type(exc) is RuntimeError
    assert "code=-32602" in str(exc)
    assert "invalid params" in str(exc)
    assert "sk_secret_token_123456789" not in str(exc)
    assert "[REDACTED]" in str(exc)


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ('invalid params {"api_key":"unmasked-secret-value"}',
         "unmasked-secret-value"),
        ('invalid params {"apiKey":"camel-secret-value"}',
         "camel-secret-value"),
        ("invalid params accessToken='quoted access secret'",
         "quoted access secret"),
        ("invalid params Authorization: Bearer plain-bearer-secret",
         "plain-bearer-secret"),
    ],
)
def test_jsonrpc_diagnostics_redact_structured_credentials(message, secret):
    exc = _classify_jsonrpc_error({"code": -32602, "message": message})

    assert type(exc) is RuntimeError
    assert secret not in str(exc)
    assert "[REDACTED]" in str(exc)


@pytest.mark.asyncio
async def test_reader_loop_delivers_classified_jsonrpc_error_to_requester():
    driver = CodexAppServerDriver()
    driver._closed = True
    driver._proc = type("Process", (), {"stdout": asyncio.StreamReader()})()
    future = asyncio.get_running_loop().create_future()
    driver._pending[7] = future
    driver._proc.stdout.feed_data(
        json.dumps(
            {
                "id": 7,
                "error": {"code": 429, "message": "rate limit exceeded"},
            }
        ).encode()
        + b"\n"
    )
    driver._proc.stdout.feed_eof()

    await driver._read_loop()

    with pytest.raises(AgentAPIError) as raised:
        future.result()
    assert raised.value.is_auth is False


class _StdinCapture:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> None:
        self.writes.append(value)

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _ReaderProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdin = _StdinCapture()

    def feed_raw(self, raw: str) -> None:
        self.stdout.feed_data(raw.encode() + b"\n")


def _drain_events(driver: CodexAppServerDriver) -> list:
    events = []
    while not driver._events.empty():
        events.append(driver._events.get_nowait())
    return events


@pytest.mark.asyncio
async def test_reader_loop_survives_malformed_and_unroutable_frames():
    """One bad frame must never orphan every pending request."""
    driver = CodexAppServerDriver()
    driver._closed = True
    proc = _ReaderProcess()
    driver._proc = proc
    future = asyncio.get_running_loop().create_future()
    driver._pending[7] = future
    for raw in (
        "[1, 2]",
        '"text"',
        "null",
        json.dumps({"id": [1], "result": {}}),
        json.dumps({
            "id": "srv-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "ls"},
        }),
        json.dumps({
            "method": "thread/tokenUsage/updated",
            "params": {"tokenUsage": None},
        }),
        json.dumps({"id": 7, "result": {"ok": True}}),
    ):
        proc.feed_raw(raw)
    proc.stdout.feed_eof()

    await driver._read_loop()

    # The reader survived every malformed frame and still dispatched the
    # trailing valid response.
    assert future.result() == {"ok": True}
    events = _drain_events(driver)
    codes = [str(event.data.get("code") or "") for event in events]
    assert codes.count("protocol_frame") == 4
    assert "frame_dispatch_failed" not in codes
    assert driver._context.used_tokens is None

    # A string JSON-RPC id is stored and echoed back verbatim, not coerced.
    ((ref, pending),) = driver._permission_requests.items()
    request_id, method, params = pending
    assert request_id == "srv-1"
    assert method == "item/commandExecution/requestApproval"
    assert params == {"command": "ls"}
    await driver.resolve_permission(
        PermissionRef(str(ref)), PermissionDecision.APPROVE
    )
    response = json.loads(proc.stdin.writes[-1])
    assert response["id"] == "srv-1"
    assert response["result"] == {"decision": "accept"}
