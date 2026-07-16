"""T23 phase-2 keyless wiring.

The subprocess MCP server (``puffo_agent.mcp.puffo_core_server``) turns
on its keyless / bridge path only when ``PUFFO_CORE_TRANSPORT == "bridge"``
(``build_server`` does ``keyless=(transport == "bridge")``). That env var
is produced by ``puffo_core_mcp_env`` and must be threaded from
``agent.yml``'s ``puffo_core.transport`` at the two subprocess-MCP call
sites in ``worker.build_adapter`` (cli-local and cli-docker).

These tests pin both halves of the seam:

  * the env builder emits ``PUFFO_CORE_TRANSPORT`` for ``bridge`` only,
    and for no other transport value (so keyless stays False elsewhere);
  * ``build_adapter`` forwards ``pc.transport`` end-to-end, so a bridge
    agent's adapter env carries the var while a native agent's does not
    (native stays byte-for-byte on the signed keystore path).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.mcp.config import puffo_core_mcp_env
from puffo_agent.portal.state import (
    AgentConfig,
    DaemonConfig,
    PuffoCoreConfig,
    RuntimeConfig,
)
from puffo_agent.portal.worker import build_adapter


@pytest.fixture(autouse=True)
def _tmp_home(tmp_path, monkeypatch):
    """Keep every adapter/config path under a throwaway home so building
    adapters never touches the real ~/.puffo-agent."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))


_ENV_BASE = dict(
    slug="bot-0001",
    device_id="dev_1",
    server_url="http://localhost:3000",
    keystore_dir="/tmp/keys",
    workspace="/workspace",
)


# ── env builder: "bridge" is the only value that emits the var ──────


def test_env_builder_emits_transport_for_bridge():
    env = puffo_core_mcp_env(**_ENV_BASE, transport="bridge")
    assert env["PUFFO_CORE_TRANSPORT"] == "bridge"


@pytest.mark.parametrize("transport", ["", "native", "local"])
def test_env_builder_omits_transport_for_non_bridge(transport):
    # "native" (the real default) and any other non-bridge value are
    # truthy strings, but the subprocess only branches on "bridge", so
    # emitting them would be dead weight — and would flip the agent's
    # mcp-config.json off byte-for-byte parity. None must appear.
    env = puffo_core_mcp_env(**_ENV_BASE, transport=transport)
    assert "PUFFO_CORE_TRANSPORT" not in env


def test_env_builder_default_omits_transport():
    env = puffo_core_mcp_env(**_ENV_BASE)
    assert "PUFFO_CORE_TRANSPORT" not in env


# ── worker call sites forward pc.transport end-to-end ───────────────


def _bridge_puffo_core() -> PuffoCoreConfig:
    return PuffoCoreConfig(
        server_url="http://localhost:3000",
        slug="bot-0001",
        device_id="dev_1",
        space_id="sp_test",
        transport="bridge",
        sandbox_token="sbx-token",
    )


def _native_puffo_core() -> PuffoCoreConfig:
    # transport defaults to "native".
    return PuffoCoreConfig(
        server_url="http://localhost:3000",
        slug="bot-0001",
        device_id="dev_1",
        space_id="sp_test",
    )


@pytest.mark.parametrize("kind", ["cli-local", "cli-docker"])
def test_worker_forwards_bridge_transport(kind):
    cfg = AgentConfig(
        id=f"{kind}-bridge",
        runtime=RuntimeConfig(kind=kind),
        puffo_core=_bridge_puffo_core(),
    )
    adapter = build_adapter(DaemonConfig(), cfg)
    assert adapter.puffo_core_mcp_env is not None
    assert adapter.puffo_core_mcp_env["PUFFO_CORE_TRANSPORT"] == "bridge"


@pytest.mark.parametrize("kind", ["cli-local", "cli-docker"])
def test_worker_native_agent_omits_transport(kind):
    cfg = AgentConfig(
        id=f"{kind}-native",
        runtime=RuntimeConfig(kind=kind),
        puffo_core=_native_puffo_core(),
    )
    adapter = build_adapter(DaemonConfig(), cfg)
    assert adapter.puffo_core_mcp_env is not None
    assert "PUFFO_CORE_TRANSPORT" not in adapter.puffo_core_mcp_env
