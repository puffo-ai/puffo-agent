"""OpenCode desired-skill projection.

OpenCode 1.18.16 discovers workspace-level ``.agents/skills/*/SKILL.md``
(verified with ``opencode debug skill``), so its assets profile registers
the WORKSPACE_AGENTS root — deliberately not the home-level
``~/.agents/skills`` opencode also reads: a home-level root is one shared
physical directory for every agent whose harness runs without HOME
redirection.

Tool references need a rewrite, not codex's strip: opencode registers MCP
tools as ``<server>_<tool>`` (its docs: "MCP server tools are registered
with server name as prefix"), so ``mcp__puffo__send_message`` must become
``puffo_send_message`` — the bare ``send_message`` codex wants matches
nothing in opencode's router.

The last test runs the real ``opencode debug skill`` against an installed
skill when the binary is available (``OPENCODE_BIN`` or PATH); it is the
discovery half of the user-facing E2E.
"""

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from puffo_agent.agent.adapters.desired_install import (
    DESIRED_INSTALLED_MARKER,
    install_desired,
)
from puffo_agent.agent.harness.support.assets import (
    SkillBodyTransform,
    SkillRoot,
    get_harness_assets_profile,
)

SKILL_BODY = """---
name: probe-desired
description: Probe skill that reports workspace status via puffo tools.
---

Call mcp__puffo__send_message to reply, and mcp__puffo__read_inbox first.
"""


class _FakeHttpOneSkill:
    async def get(self, path):
        assert path == "/v2/skill-templates/probe-desired"
        return {"id": "probe-desired", "body": SKILL_BODY}

    async def close(self):
        pass


def test_opencode_profile_registers_workspace_skills_and_declares_mcp_gap():
    profile = get_harness_assets_profile("opencode")
    assert profile.skills_supported
    assert profile.skill_root is SkillRoot.WORKSPACE_AGENTS
    assert profile.skill_body_transform is SkillBodyTransform.PUFFO_SERVER_PREFIX
    # Catalog MCP stays fail-closed until a projection into opencode.json
    # exists; the core Puffo server reaches opencode inline at spawn.
    assert not profile.mcp_supported
    assert "opencode.json" in profile.unsupported_reason


def test_opencode_transform_rewrites_to_server_underscore_names():
    profile = get_harness_assets_profile("opencode")
    body = profile.transform_skill_body(SKILL_BODY)
    assert "puffo_send_message" in body
    assert "puffo_read_inbox" in body
    assert "mcp__puffo__" not in body
    # codex's strip produces bare names — that is a different router.
    codex = get_harness_assets_profile("codex").transform_skill_body(SKILL_BODY)
    assert "Call send_message" in codex


def _install_probe_skill(tmp_path):
    agent_home = tmp_path / "agent_home"
    workspace = tmp_path / "ws"
    asyncio.new_event_loop().run_until_complete(
        install_desired(
            http=_FakeHttpOneSkill(),
            agent_home=agent_home,
            workspace_dir=workspace,
            agent_id="t-agent",
            harness_name="opencode",
            desired_skills=["probe-desired"],
            desired_mcps=[],
        ),
    )
    return workspace


def test_install_desired_lands_transformed_skill_in_workspace(tmp_path):
    workspace = _install_probe_skill(tmp_path)
    skill_dir = workspace / ".agents" / "skills" / "probe-desired"
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "puffo_send_message" in text
    assert "mcp__puffo__" not in text
    assert (skill_dir / DESIRED_INSTALLED_MARKER).exists()


def _opencode_bin():
    return os.environ.get("OPENCODE_BIN") or shutil.which("opencode")


@pytest.mark.skipif(_opencode_bin() is None, reason="opencode binary not available")
def test_real_opencode_discovers_the_installed_skill(tmp_path):
    workspace = _install_probe_skill(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    out = subprocess.run(
        [_opencode_bin(), "debug", "skill"],
        cwd=workspace,
        env={
            "PATH": os.environ.get("PATH", ""),
            # Hermetic via an empty HOME. Do NOT set
            # OPENCODE_DISABLE_EXTERNAL_SKILLS here: that flag turns off
            # the whole ``.agents/skills`` scanner — the workspace level
            # this projection depends on included, not just ``~``-level
            # (measured on 1.18.16; the skill vanishes from discovery
            # with the flag set). The spawn env must never set it either.
            "HOME": str(home),
            "OPENCODE_PURE": "1",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    listed = {s["name"]: s for s in json.loads(out.stdout)}
    assert "probe-desired" in listed, sorted(listed)
    assert "puffo_send_message" in listed["probe-desired"]["content"]
