"""Regression coverage for agent-facing host MCP tool forwarding."""

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from puffo_agent.mcp.core_host_mcp_tools import register_host_mcp_tools


class _StubRpc:
    """Match the real client's keyword-only ``sync_mcp`` contract."""

    def __init__(self) -> None:
        self.template_ids: list[str] = []

    async def sync_mcp(self, *, template_id: str) -> str:
        self.template_ids.append(template_id)
        return f"mirrored {template_id} into your config"


def _build_tools(rpc_client: object | None) -> FastMCP:
    mcp = FastMCP("test")
    register_host_mcp_tools(
        mcp,
        SimpleNamespace(rpc_client=rpc_client, workspace=None),
    )
    return mcp


@pytest.mark.asyncio
async def test_sync_host_mcp_forwards_keyword_only_template_id():
    """A tool parameter rename must not rename the RPC keyword again."""
    rpc = _StubRpc()
    tool = _build_tools(rpc)._tool_manager._tools["sync_host_mcp"]

    result = await tool.fn(template_id="linear")

    assert rpc.template_ids == ["linear"]
    assert "linear" in result


@pytest.mark.asyncio
async def test_sync_host_mcp_without_rpc_reports_configuration_error():
    """An unwired RPC client must give guidance, not an AttributeError."""
    tool = _build_tools(None)._tool_manager._tools["sync_host_mcp"]

    with pytest.raises(RuntimeError, match="PUFFO_RPC_URL"):
        await tool.fn(template_id="linear")
