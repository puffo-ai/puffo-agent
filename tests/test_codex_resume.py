from unittest.mock import AsyncMock

import pytest

from puffo_agent.agent.harness.drivers.codex import CodexAppServerDriver


@pytest.mark.asyncio
async def test_initialize_enables_api_required_by_exclude_turns():
    driver = CodexAppServerDriver()
    driver._request = AsyncMock(return_value={})
    driver._write = AsyncMock()

    await driver._initialize_app_server()

    driver._request.assert_awaited_once_with(
        "initialize",
        {
            "clientInfo": {"name": "puffo-agent", "version": "1"},
            "capabilities": {"experimentalApi": True},
        },
    )
    driver._write.assert_awaited_once_with(
        {"method": "initialized", "params": {}}
    )
