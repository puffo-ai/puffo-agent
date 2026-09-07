"""``exa_search`` forwards to the server gateway and formats its result. The
tool holds no key and does no budgeting itself — the server holds the exa key,
enforces the cap, and settles the charge; the tool's checks are UX pre-guards
and its errors are the server's own mapped messages.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from puffo_agent.crypto.http_client import HttpError
from puffo_agent.mcp.core_exa_tools import register_exa_tools


class _FakeHttp:
    def __init__(self, *, response=None, post_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.keyless = False
        self._response = response if response is not None else {"ok": True}
        self._post_error = post_error

    async def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        if self._post_error is not None:
            raise self._post_error
        return self._response


def _tools(http):
    cfg = SimpleNamespace(http_client=http, keyless=http.keyless)
    mcp = FastMCP("test")
    register_exa_tools(mcp, cfg)
    return mcp


async def _call(mcp, name, args):
    result = await mcp.call_tool(name, args)
    if isinstance(result, tuple):
        result = result[0]
    return "".join(getattr(item, "text", str(item)) for item in result)


@pytest.mark.asyncio
async def test_search_forwards_and_formats_result():
    http = _FakeHttp(
        response={
            "ledger_id": "led_1",
            "cost_micro": 7000,
            "provider_http_status": 200,
            "results": [{"title": "t", "url": "https://x"}],
        }
    )
    mcp = _tools(http)
    text = await _call(
        mcp,
        "exa_search",
        {"query": "weather in tokyo", "num_results": 5, "max_cost_micro": 20000},
    )
    # Provenance stamp: a settled search is marked exa-sourced so the model can
    # attribute it and not conflate it with its own answers.
    assert "via exa · search" in text
    assert "cost 7000 micro-dollars" in text
    assert "provider status 200" in text
    assert "https://x" in text
    assert http.calls[-1][0] == "POST"
    assert http.calls[-1][1] == "/v2/exa/search"
    body = http.calls[-1][2]
    assert body["query"] == "weather in tokyo"
    assert body["num_results"] == 5
    assert body["type"] == "auto"
    assert body["max_cost_micro"] == 20000


@pytest.mark.asyncio
async def test_search_rejects_empty_query_without_calling_server():
    http = _FakeHttp()
    mcp = _tools(http)
    with pytest.raises(Exception):
        await _call(mcp, "exa_search", {"query": "   "})
    assert not [c for c in http.calls if c[1] == "/v2/exa/search"]


@pytest.mark.asyncio
async def test_search_rejects_non_positive_ceiling_without_calling_server():
    http = _FakeHttp()
    mcp = _tools(http)
    with pytest.raises(Exception):
        await _call(mcp, "exa_search", {"query": "x", "max_cost_micro": 0})
    assert not [c for c in http.calls if c[1] == "/v2/exa/search"]


@pytest.mark.asyncio
async def test_search_surfaces_server_error_and_labels_non_exa():
    http = _FakeHttp(
        post_error=HttpError(
            402,
            json.dumps(
                {"error": "BUDGET_EXCEEDED", "message": "agent spend cap reached"}
            ),
        )
    )
    mcp = _tools(http)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "exa_search", {"query": "x", "max_cost_micro": 20000})
    msg = str(excinfo.value)
    assert "agent spend cap reached" in msg
    # A search failure carries the label rule: if the model gives up and answers
    # from elsewhere, it must mark that non-exa.
    assert "NOT an exa search result" in msg
    # Only the mapped message + directive surface — never a raw upstream body.
    assert "BUDGET_EXCEEDED" not in msg


@pytest.mark.asyncio
async def test_already_settled_replay_reports_no_fresh_results():
    http = _FakeHttp(
        response={
            "ledger_id": "led_1",
            "cost_micro": 7000,
            "results": None,
            "already_settled": True,
        }
    )
    mcp = _tools(http)
    text = await _call(
        mcp,
        "exa_search",
        {"query": "x", "max_cost_micro": 20000, "idempotency_key": "k1"},
    )
    assert "already settled" in text
    assert "cannot be replayed" in text


@pytest.mark.asyncio
async def test_pending_reconcile_202_is_reported_not_treated_as_results():
    # A 202 (prior attempt under this key still resolving) comes back as a
    # success body, not an HttpError — the tool reports it as pending, not as
    # search results.
    http = _FakeHttp(
        response={
            "error": "PENDING_RECONCILE",
            "message": "still resolving",
            "ledger_id": "led_pending",
        }
    )
    mcp = _tools(http)
    text = await _call(
        mcp,
        "exa_search",
        {"query": "x", "max_cost_micro": 20000, "idempotency_key": "k1"},
    )
    assert "still resolving" in text
    assert "led_pending" in text
    assert "do not retry with the same idempotency key" in text


@pytest.mark.asyncio
async def test_tool_not_registered_for_keyless_agents():
    # A keyless bridge agent cannot reach the subkey-gated route, so the tool is
    # not exposed for it — not registered, no error path.
    http = _FakeHttp()
    http.keyless = True
    mcp = _tools(http)
    tool_names = {t.name for t in await mcp.list_tools()}
    assert "exa_search" not in tool_names

    # Native agents DO get it.
    native = _tools(_FakeHttp())
    native_names = {t.name for t in await native.list_tools()}
    assert "exa_search" in native_names
