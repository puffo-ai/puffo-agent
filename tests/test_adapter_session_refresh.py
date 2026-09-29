"""adapter.reload(with_session=True) is the one place a native session is
dropped on purpose; it must say so in the log."""
import logging

import pytest

from puffo_agent.agent.harness.runtime.runtime_manager import RuntimeManagerAdapter
from tests.test_runtime_manager_failures import _autonomous_manager


async def _reload_with(monkeypatch, caplog, *, with_session: bool) -> str:
    manager = _autonomous_manager()
    manager.native_session_id = "native-old"
    seen = {}

    async def fake_reload_resources(*, preserve_session, spec=None):
        seen["preserve_session"] = preserve_session

    monkeypatch.setattr(manager, "reload_resources", fake_reload_resources)
    adapter = RuntimeManagerAdapter(manager)
    with caplog.at_level(
        logging.WARNING, logger="puffo_agent.agent.harness.runtime.runtime_manager",
    ):
        await adapter.reload("prompt", with_session=with_session)
    assert seen["preserve_session"] is (not with_session)
    return caplog.text


@pytest.mark.asyncio
async def test_session_refresh_logs_the_dropped_native_session(monkeypatch, caplog):
    text = await _reload_with(monkeypatch, caplog, with_session=True)
    assert "session refresh drops native session native-old" in text


@pytest.mark.asyncio
async def test_resource_reload_is_silent(monkeypatch, caplog):
    text = await _reload_with(monkeypatch, caplog, with_session=False)
    assert "session refresh" not in text
