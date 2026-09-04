"""Loopback conformance against the pinned official ACP SDK transport."""

import asyncio
from typing import Any

import pytest

from acp import AgentSideConnection, PROTOCOL_VERSION
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    TextContentBlock,
)

from puffo_agent.agent.harness.drivers.acp import AcpDriver
from puffo_agent.agent.harness.driver import (
    HarnessEventType,
    RuntimeSpec,
    TurnInput,
)


class _EchoAgent:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, **kwargs: Any):
        assert protocol_version == PROTOCOL_VERSION
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(load_session=True),
        )

    async def new_session(self, cwd: str, **kwargs: Any):
        return NewSessionResponse(session_id="loopback_session")

    async def load_session(self, **kwargs: Any):
        return None

    async def prompt(self, session_id: str, prompt, **kwargs: Any):
        text = prompt[0].text
        await self.conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=f"echo:{text}"),
            ),
        )
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, **kwargs: Any) -> None:
        return None

    def on_connect(self, conn) -> None:
        return None


class _LoopbackProcess:
    def __init__(self, reader, writer) -> None:
        self.stdout = reader
        self.stdin = writer
        self.stderr = None
        self.returncode = None
        self._exit = asyncio.get_running_loop().create_future()

    async def wait(self) -> int:
        return await self._exit

    def terminate(self) -> None:
        if self.returncode is not None:
            return
        self.returncode = 0
        self.stdin.close()
        self._exit.set_result(0)

    def kill(self) -> None:
        self.terminate()


async def _collect_through(stream, type_):
    events = []
    async for event in stream:
        events.append(event)
        if event.type is type_:
            return events
    raise AssertionError(f"stream ended before {type_}")


@pytest.mark.asyncio
async def test_official_sdk_v1_round_trip_streams_update_then_terminal():
    server_connections = []
    server_writers = []

    async def serve(reader, writer):
        server_writers.append(writer)
        server_connections.append(
            AgentSideConnection(lambda conn: _EchoAgent(conn), writer, reader)
        )

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def process_factory(*_args):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return _LoopbackProcess(reader, writer)

    driver = AcpDriver(process_factory)
    await asyncio.wait_for(
        driver.open(RuntimeSpec("/workspace", executable="fixture")),
        timeout=2,
    )
    stream = driver.events()
    await asyncio.wait_for(driver.start_turn(TurnInput("hello")), timeout=2)
    events = await asyncio.wait_for(
        _collect_through(stream, HarnessEventType.TURN_COMPLETED), timeout=2
    )

    [delta] = [
        event for event in events
        if event.type is HarnessEventType.ASSISTANT_DELTA
    ]
    assert delta.data["delta"] == "echo:hello"
    assert events[-1].data["outcome"] == "succeeded"

    await asyncio.wait_for(driver.close(), timeout=2)
    for connection in server_connections:
        await asyncio.wait_for(connection.close(), timeout=2)
    for writer in server_writers:
        writer.close()
        await writer.wait_closed()
    server.close()
    await asyncio.wait_for(server.wait_closed(), timeout=2)
    assert server_connections
