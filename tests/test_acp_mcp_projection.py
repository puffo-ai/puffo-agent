"""ACP MCP projection policy.

The ACP Driver forwards ``RuntimeSpec.mcp_servers`` into ``session/new``
verbatim (``test_spec_mcp_servers_are_forwarded_into_the_acp_launch_plan``),
so ``_project_protocol_mcp`` is the single policy point for which tools an
ACP agent can discover. These tests pin that policy:

  * LingTai's constrained ``puffo-v0`` profile rejects a non-empty
    ``mcpServers`` at ``session/new``, so its projection must stay empty —
    an existing imported v0 agent must keep starting after the Driver
    became a pass-through;
  * every other ACP launch (``puffo-v1``, generic agents) receives
    Puffo's core server, which is what makes the imported agent a full
    platform member.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.state import (
    AgentConfig,
    DaemonConfig,
    PuffoCoreConfig,
    RuntimeConfig,
)


@pytest.fixture(autouse=True)
def _tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))


def _preparer():
    from puffo_agent.agent.harness.runtime.local_runtime import (
        LocalRuntimePreparer,
    )

    cfg = AgentConfig(
        id="acp-projection",
        runtime=RuntimeConfig(
            kind="cli-local",
            harness="acp",
            harness_command=["lingtai-agent", "acp"],
        ),
        puffo_core=PuffoCoreConfig(
            server_url="http://localhost:3000",
            slug="bot-0001",
            device_id="dev_1",
            space_id="sp_test",
        ),
    )
    return LocalRuntimePreparer(DaemonConfig(), cfg)


def _project(launch_args):
    preparer = _preparer()
    assert preparer._puffo_core_env, "fixture must yield a configured core env"
    return preparer._project_protocol_mcp({}, {}, tuple(launch_args))


@pytest.mark.parametrize(
    "launch_args",
    [
        pytest.param(
            ("acp", "--runtime-id", "rt_x", "--profile", "puffo-v0"),
            id="flag-pair",
        ),
        pytest.param(
            ("acp", "--profile=puffo-v0"),
            id="flag-equals",
        ),
    ],
)
def test_puffo_v0_projection_keeps_mcp_servers_empty(launch_args):
    """puffo-v0 fails ``session/new`` on any server list; with the Driver
    now a pass-through, this projection is the only thing keeping an
    imported v0 agent bootable."""
    assert _project(launch_args) == ()


@pytest.mark.parametrize(
    "launch_args",
    [
        pytest.param(
            ("acp", "--runtime-id", "rt_x", "--profile", "puffo-v1"),
            id="puffo-v1",
        ),
        pytest.param(("acp", "--agent-dir", "/agent"), id="generic-acp"),
    ],
)
def test_non_v0_acp_projection_carries_the_puffo_server(launch_args):
    (server,) = _project(launch_args)
    assert server.name == "puffo"
    assert server.args == ("-m", "puffo_agent.mcp.puffo_core_server")
    assert server.environment.get("PUFFO_CORE_SLUG") == "bot-0001"


def test_duplicate_profile_flags_classify_by_the_last_occurrence():
    """LingTai's argparse takes the LAST ``--profile``; Puffo must
    classify by the same effective value or a duplicated flag launches
    the child under a different profile than the one prepared for it —
    both mismatch directions pinned."""
    v0_then_v1 = ("acp", "--profile", "puffo-v0", "--profile", "puffo-v1")
    v1_then_v0 = ("acp", "--profile", "puffo-v1", "--profile=puffo-v0")
    (server,) = _project(v0_then_v1)  # effective v1 → carries the server
    assert server.name == "puffo"
    assert _project(v1_then_v0) == ()  # effective v0 → stays empty
