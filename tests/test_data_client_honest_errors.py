"""DataClient must not render a failure as a confidently empty result."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from puffo_agent.mcp.data_client import DataClient, DataNotFound, DataUnavailable


def _failing_app(status: int) -> web.Application:
    async def handler(_request):
        return web.json_response({"error": "boom"}, status=status)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


async def _client_for(app_or_none) -> tuple[DataClient, TestServer | None]:
    if app_or_none is None:
        # A port with nothing listening: transport-level failure.
        return DataClient(base_url="http://127.0.0.1:9", agent_id="agent_a"), None
    server = TestServer(app_or_none)
    await server.start_server()
    client = DataClient(
        base_url=str(server.make_url("")).rstrip("/"), agent_id="agent_a",
    )
    return client, server


READS = (
    lambda c: c.get_channel_history("ch-1"),
    lambda c: c.get_dm_history("peer"),
    lambda c: c.get_channel_roots("ch-1"),
    lambda c: c.get_thread_messages("root-1"),
    lambda c: c.get_message_by_envelope("m-1"),
)


@pytest.mark.asyncio
async def test_server_errors_raise_data_unavailable():
    client, server = await _client_for(_failing_app(500))
    try:
        for read in READS:
            with pytest.raises(DataUnavailable):
                await read(client)
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_transport_errors_raise_data_unavailable():
    client, _ = await _client_for(None)
    try:
        for read in READS:
            with pytest.raises(DataUnavailable):
                await read(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_404_semantics_are_preserved():
    """Absence stays absence: no DataUnavailable on 404."""
    client, server = await _client_for(_failing_app(404))
    try:
        assert await client.get_channel_history("ch-1") == []
        assert await client.get_dm_history("peer") == []
        assert await client.get_message_by_envelope("m-1") is None
        with pytest.raises(DataNotFound):
            await client.get_channel_roots("ch-1")
        with pytest.raises(DataNotFound):
            await client.get_thread_messages("root-1")
    finally:
        await client.close()
        await server.close()
