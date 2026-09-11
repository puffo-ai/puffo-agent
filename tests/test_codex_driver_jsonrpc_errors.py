"""Focused recovery classification for Codex app-server JSON-RPC errors."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from puffo_agent.agent.core import AgentAPIError
from puffo_agent.agent.errors import ProviderFailureError
from puffo_agent.agent.harness.drivers.codex import (
    CodexAppServerDriver,
    _classify_jsonrpc_error,
    _selection_ack_warnings,
)
from puffo_agent.agent.harness.driver import (
    PermissionDecision,
    PermissionRef,
    RuntimeSpec,
)


def test_unlisted_model_and_level_are_explicitly_unvalidated_without_ack():
    """A successful thread open must not imply its requested tier took effect."""
    spec = RuntimeSpec(
        "/workspace", model="gpt-6-astra", inference_level="high",
    )

    assert _selection_ack_warnings(spec, {"id": "thread-1"}) == (
        "model_ack_unavailable",
        "inference_level_ack_unavailable",
    )


def test_matching_harness_ack_validates_model_and_level():
    """An exact app-server echo is positive evidence for the selected config."""
    spec = RuntimeSpec(
        "/workspace", model="gpt-6-astra", inference_level="high",
    )

    assert _selection_ack_warnings(spec, {
        "id": "thread-1",
        "model": "gpt-6-astra",
        "reasoningEffort": "high",
    }) == ()


@pytest.mark.parametrize(
    "error",
    [
        {"code": 401, "message": "access token has expired"},
        {"code": -32000, "message": "token_invalidated"},
        {
            "code": -32000,
            "message": "stream error: unexpected status 401 Unauthorized",
        },
        {"code": -32000, "message": "Unauthorized: please run codex login"},
    ],
)
def test_jsonrpc_auth_errors_request_operator_reauthentication(error):
    exc = _classify_jsonrpc_error(error)

    assert isinstance(exc, AgentAPIError)
    assert exc.is_auth is True
    assert exc.error_code == "authentication"


@pytest.mark.parametrize(
    "error",
    [
        {"code": 429, "message": "rate limit exceeded"},
        {"code": -32000, "data": {"status": 503}, "message": "unavailable"},
        {
            "code": -32603,
            "message": "stream error: unexpected status 503 Service Unavailable",
        },
        {"code": -32603, "message": "unexpected status 500 Internal Server Error"},
    ],
)
def test_jsonrpc_retryable_provider_errors_requeue(error):
    exc = _classify_jsonrpc_error(error)

    assert isinstance(exc, AgentAPIError)
    assert exc.is_auth is False
    assert exc.error_code in {"rate_limit", "provider_unavailable"}


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            {"code": -32000, "message": "model_not_found: gpt-future"},
            "model_not_found",
        ),
        (
            {
                "code": 403,
                "message": "Your account is not entitled to use model gpt-future",
            },
            "not_entitled",
        ),
    ],
)
def test_jsonrpc_model_selection_failures_keep_actionable_class(error, expected):
    """A real model rejection must not collapse into generic provider failure."""
    exc = _classify_jsonrpc_error(error)

    assert isinstance(exc, ProviderFailureError)
    assert exc.error_code == expected


@pytest.mark.parametrize(
    "message",
    [
        "no rollout found for thread id 01a0356d-c424-7d00-0000-000000000000",
        "Rollout not found for thread abc",
        "thread not found: 01a0356d",
    ],
)
def test_jsonrpc_missing_rollout_is_classified_as_invalid_resume(message):
    exc = _classify_jsonrpc_error({"code": -32600, "message": message})

    assert isinstance(exc, AgentAPIError)
    assert exc.is_auth is False
    assert exc.error_code == "invalid_resume"
    # detail preserved for text-marker matching
    assert message in str(exc)


def test_jsonrpc_permission_and_quota_errors_do_not_enter_tight_retry():
    for error in (
        {"code": 403, "message": "permission denied"},
        {"code": -32000, "message": "credit balance is too low"},
        {"code": -32000, "message": "You have exceeded your quota"},
    ):
        exc = _classify_jsonrpc_error(error)
        assert isinstance(exc, ProviderFailureError)
        assert exc.error_code in {"permission_denied", "quota_exhausted"}


def test_jsonrpc_protocol_error_keeps_safe_diagnostic_without_retry(caplog):
    with caplog.at_level(logging.WARNING):
        exc = _classify_jsonrpc_error(
            {
                "code": -32602,
                "message": "invalid params authorization=Bearer sk_secret_token_123456789",
            }
        )

    assert isinstance(exc, ProviderFailureError)
    assert exc.error_code == "provider_error"
    assert "sk_secret_token_123456789" not in caplog.text
    assert "[REDACTED]" in caplog.text


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
def test_jsonrpc_diagnostics_redact_structured_credentials(message, secret, caplog):
    with caplog.at_level(logging.WARNING):
        exc = _classify_jsonrpc_error({"code": -32602, "message": message})

    assert isinstance(exc, ProviderFailureError)
    assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text


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
