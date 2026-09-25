"""The RPC client's ≥400 handling must surface the daemon's whole error
body — route, status, the ``error`` headline, and every remaining field —
because a 4xx body is the diagnosis (re-auth needed vs cloud config vs an
operator denial), not noise to discard."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent.mcp._host_mcp import PuffoRpcClient


async def _serving_client(body: Any, status: int) -> tuple[PuffoRpcClient, TestClient]:
    """A real loopback server that answers every RPC with one canned body."""
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/{route}", handler)
    http = TestClient(TestServer(app))
    await http.start_server()
    rpc = PuffoRpcClient(str(http.make_url("")).rstrip("/"), "agent_a")
    return rpc, http


@pytest.mark.asyncio
async def test_rich_error_body_survives_into_the_exception():
    """Status, route, headline, and the non-error fields all arrive."""
    rpc, http = await _serving_client(
        {
            "error": "permission denied by operator",
            "code": "permission_denied",
            "hint": "re-run /permissions and approve host-mcp",
            "request_id": "req-123",
        },
        403,
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.sync_mcp(template_id="tpl-1")
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "rpc sync-mcp failed with status 403" in message
    assert "permission denied by operator" in message
    assert "permission_denied" in message
    assert "re-run /permissions and approve host-mcp" in message
    assert "req-123" in message


@pytest.mark.asyncio
async def test_body_without_error_headline_still_reaches_the_exception():
    """An empty ``error`` must not reduce the failure to a bare status."""
    rpc, http = await _serving_client(
        {"error": "", "reason": "token expired", "reauthorize": True},
        401,
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.read_inbox()
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "rpc read-inbox failed with status 401" in message
    assert "token expired" in message
    assert "reauthorize" in message


@pytest.mark.asyncio
async def test_oversized_detail_is_bounded_and_codes_survive_truncation():
    """A pathological body may not turn the exception into a log bomb,
    and short stable fields must outlive a page-long trace."""
    rpc, http = await _serving_client(
        {
            "error": "internal",
            "trace": "x" * 2000,
            "code": "quota_exhausted",
            "reason": "burst limit exceeded",
        },
        500,
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.send_message(channel="ch_a", text="hello")
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "rpc send-message failed with status 500: internal" in message
    assert "quota_exhausted" in message
    assert "burst limit exceeded" in message
    assert "xxx" in message
    assert len(message) < 700


@pytest.mark.asyncio
async def test_many_short_filler_fields_cannot_crowd_out_diagnostics():
    """Jeff's counterexample: length-sorting alone lets a crowd of short
    junk fields exhaust the budget; diagnostics must be prioritized."""
    body: dict[str, Any] = {
        "error": "internal",
        "code": "quota_exhausted",
        "reason": "burst limit exceeded",
    }
    body.update({f"filler_{i:02d}": "v" * 10 for i in range(60)})
    rpc, http = await _serving_client(body, 500)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.read_inbox()
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "quota_exhausted" in message
    assert "burst limit exceeded" in message
    assert len(message) < 700


@pytest.mark.asyncio
async def test_opaque_oauth_code_and_state_are_redacted():
    """Jeff's counterexample: an OAuth authorization code or CSRF state
    is an opaque string no shape regex catches — top-level, nested, and
    inside a URL query it may not ride along raw."""
    rpc, http = await _serving_client(
        {
            "error": "authorization failed",
            "code": "SplxlOBeZQQYbYS6WxSbIA",
            "state": "af0ifjsldkj-9XQ",
            "details": {"oauth": {"code": "SplxlOBeZQQYbYS6WxSbIA"}},
            "redirect": (
                "https://cloud.example/cb"
                "?code=SplxlOBeZQQYbYS6WxSbIA&state=af0ifjsldkj-9XQ"
            ),
        },
        401,
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.sync_mcp(template_id="tpl-1")
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "SplxlOBeZQQYbYS6WxSbIA" not in message
    assert "af0ifjsldkj-9XQ" not in message
    assert "[REDACTED]" in message
    assert "authorization failed" in message


@pytest.mark.asyncio
async def test_snake_shaped_secret_code_and_state_are_still_redacted():
    """Jeff's counterexample: a secret can be a perfectly snake-shaped
    word (``private_session_nonce``); only *known* diagnostic values
    pass, not values that merely look like enums."""
    rpc, http = await _serving_client(
        {
            "error": "authorization failed",
            "state": "private_session_nonce",
            "code": "snarkle_blorp",
        },
        401,
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.sync_mcp(template_id="tpl-1")
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "private_session_nonce" not in message
    assert "snarkle_blorp" not in message
    assert "[REDACTED]" in message
    assert "authorization failed" in message


@pytest.mark.asyncio
async def test_enum_shaped_diagnostic_code_and_state_survive():
    """Known error codes and state enums are the diagnosis — they pass."""
    rpc, http = await _serving_client(
        {"error": "send rejected", "code": "permission_denied", "state": "held"},
        400,
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.send_message(channel="ch_a", text="hello")
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "permission_denied" in message
    assert "held" in message


@pytest.mark.asyncio
async def test_request_id_containing_404_does_not_trip_the_upgrade_probe():
    """Jeff's finding: mark_covered used to scan the whole diagnostic
    text for "404", so a request_id like req-404-abc rewrote a real 403
    denial into "daemon doesn't support mark_covered"."""
    rpc, http = await _serving_client(
        {
            "error": "permission denied",
            "code": "permission_denied",
            "reason": "operator denied",
            "request_id": "req-404-abc",
        },
        403,
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.mark_covered(covers=["msg-1"])
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "mark_covered is not available" not in message
    assert "status 403" in message
    assert "permission denied" in message
    assert "req-404-abc" in message


@pytest.mark.asyncio
async def test_real_404_still_reads_as_rolling_upgrade():
    """Positive control: an actual HTTP 404 keeps the friendly rewrite."""
    rpc, http = await _serving_client({"error": "no such route"}, 404)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.mark_covered(covers=["msg-1"])
    finally:
        await rpc.close()
        await http.close()
    assert "mark_covered is not available" in str(excinfo.value)


@pytest.mark.asyncio
async def test_missing_route_default_404_still_reads_as_rolling_upgrade():
    """Chris's gate finding, upgraded: the *actual* rolling-upgrade case
    is an old daemon with no such route, which answers with aiohttp's
    default text/plain 404 — non-JSON. The friendly rewrite must fire."""
    app = web.Application()  # no routes registered at all
    http = TestClient(TestServer(app))
    await http.start_server()
    rpc = PuffoRpcClient(str(http.make_url("")).rstrip("/"), "agent_a")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.mark_covered(covers=["msg-1"])
    finally:
        await rpc.close()
        await http.close()
    assert "mark_covered is not available" in str(excinfo.value)


@pytest.mark.asyncio
async def test_non_json_error_body_is_redacted():
    """A non-JSON error page may still embed a secret-shaped value."""
    async def handler(request: web.Request) -> web.Response:
        return web.Response(
            text="boom bearer sk-abcdefgh12345678", status=500,
        )

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/{route}", handler)
    http = TestClient(TestServer(app))
    await http.start_server()
    rpc = PuffoRpcClient(str(http.make_url("")).rstrip("/"), "agent_a")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.sync_mcp(template_id="tpl-1")
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "sk-abcdefgh12345678" not in message
    assert "[REDACTED]" in message
    assert "status 500" in message


@pytest.mark.asyncio
async def test_non_string_code_and_state_are_redacted():
    """Jeff's finding: ints bypassed the known-value gate — the field
    contract is a string enum, so any other type is an unknown value."""
    rpc, http = await _serving_client(
        {"error": "rejected", "state": 834729104928, "code": 17},
        400,
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.read_inbox()
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "834729104928" not in message
    assert "[REDACTED]" in message


@pytest.mark.asyncio
async def test_camelcase_and_plural_credential_keys_are_redacted():
    """Jeff's finding: accessToken (camelCase) and credentials (plural)
    slipped past the separator-based secret-key pattern."""
    rpc, http = await _serving_client(
        {
            "error": "auth failed",
            "accessToken": "FAKE_OPAQUE_ACCESS_SECRET",
            "credentials": {"value": "FAKE_OPAQUE_CREDENTIAL"},
        },
        401,
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.sync_mcp(template_id="tpl-1")
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "FAKE_OPAQUE_ACCESS_SECRET" not in message
    assert "FAKE_OPAQUE_CREDENTIAL" not in message
    assert "[REDACTED]" in message
    assert "auth failed" in message


@pytest.mark.asyncio
async def test_credentials_are_redacted_from_the_detail_tail():
    """Secret-named keys and secret-shaped values never ride along raw."""
    rpc, http = await _serving_client(
        {
            "error": "cloud re-auth required: bearer sk-abcdefgh12345678 expired",
            "access_token": "sk-abcdefgh12345678",
            "client_secret": "hunter2hunter2",
            "hint": "run /login again",
        },
        401,
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.sync_mcp(template_id="tpl-1")
    finally:
        await rpc.close()
        await http.close()
    message = str(excinfo.value)
    assert "sk-abcdefgh12345678" not in message
    assert "hunter2hunter2" not in message
    assert "[REDACTED]" in message
    assert "cloud re-auth required" in message
    assert "run /login again" in message


@pytest.mark.asyncio
async def test_error_only_body_gets_no_detail_tail():
    """Today's ``{"error": str}`` daemons keep a clean one-line message."""
    rpc, http = await _serving_client({"error": "bad request"}, 400)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await rpc.sync_mcp(template_id="tpl-1")
    finally:
        await rpc.close()
        await http.close()
    assert str(excinfo.value) == "rpc sync-mcp failed with status 400: bad request"
