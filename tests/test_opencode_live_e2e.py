"""Live acceptance checks against an installed OpenCode binary.

The default check is network-free.  A separately gated check runs a real free
model turn, proving that OpenCode exposes the installed skill to the model and
that the model can load and follow it.  Both use isolated OpenCode state so a
passing test cannot be explained by unrelated host-level skills.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from puffo_agent.agent.adapters.desired_install import install_desired
from puffo_agent.agent.harness.driver import (
    CompactRequest,
    HarnessEventType,
    RuntimeSpec,
    TurnInput,
)
from puffo_agent.agent.harness.drivers.opencode import OpenCodeDriver


class _SkillTemplateHttp:
    async def get(self, path: str):
        if path == "/v2/skill-templates/puffo-e2e":
            return {
                "body": (
                    "---\n"
                    "name: puffo-e2e\n"
                    "description: Use only for the Puffo OpenCode E2E sentinel.\n"
                    "---\n\n"
                    "# Puffo E2E\n\n"
                    "After loading this skill, reply with exactly "
                    "`SENTINEL-OPENCODE-SKILL`.\n"
                )
            }
        raise AssertionError(path)


def _isolated_opencode_environment(tmp_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    return environment


@pytest.mark.skipif(shutil.which("opencode") is None, reason="OpenCode absent")
def test_desired_skill_is_discoverable_by_real_opencode_cli(tmp_path: Path):
    """User-selected skill survives the full Puffo install -> CLI discovery path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    asyncio.run(
        install_desired(
            http=_SkillTemplateHttp(),
            agent_home=tmp_path / "agent-home",
            workspace_dir=workspace,
            agent_id="opencode-live-e2e",
            harness_name="opencode",
            desired_skills=["puffo-e2e"],
            desired_mcps=[],
        )
    )

    environment = _isolated_opencode_environment(tmp_path)
    # OpenCode consults PWD while resolving project-local skills.  Keep it
    # consistent with cwd so the test cannot accidentally discover skills
    # from the repository that launched pytest.
    environment["PWD"] = str(workspace)
    result = subprocess.run(
        [shutil.which("opencode") or "opencode", "debug", "skill"],
        cwd=workspace,
        env=_isolated_opencode_environment(tmp_path),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    skills = json.loads(result.stdout)
    sentinel = next(item for item in skills if item["name"] == "puffo-e2e")

    assert sentinel["location"] == str(
        workspace / ".agents" / "skills" / "puffo-e2e" / "SKILL.md"
    )
    assert "SENTINEL-OPENCODE-SKILL" in sentinel["content"]

    disabled_environment = _isolated_opencode_environment(tmp_path)
    disabled_environment["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"
    disabled = subprocess.run(
        [shutil.which("opencode") or "opencode", "debug", "skill"],
        cwd=workspace,
        env=disabled_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    disabled_skills = json.loads(disabled.stdout)
    assert not any(item["name"] == "puffo-e2e" for item in disabled_skills)


@pytest.mark.skipif(
    os.environ.get("PUFFO_RUN_LIVE_OPENCODE_E2E") != "1",
    reason="set PUFFO_RUN_LIVE_OPENCODE_E2E=1 for a real model turn",
)
@pytest.mark.skipif(shutil.which("opencode") is None, reason="OpenCode absent")
@pytest.mark.asyncio
async def test_real_opencode_driver_loads_skill_and_reports_context(tmp_path: Path):
    """Opt-in user journey through the real driver, CLI, model, and skill."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await install_desired(
        http=_SkillTemplateHttp(),
        agent_home=tmp_path / "agent-home",
        workspace_dir=workspace,
        agent_id="opencode-live-model-e2e",
        harness_name="opencode",
        desired_skills=["puffo-e2e"],
        desired_mcps=[],
    )

    model = os.environ.get(
        "PUFFO_OPENCODE_E2E_MODEL", "opencode/mimo-v2.5-free"
    )
    environment = _isolated_opencode_environment(tmp_path)
    environment["PWD"] = str(workspace)
    driver = OpenCodeDriver(executable_version="live-e2e")
    await driver.open(
        RuntimeSpec(
            workspace_dir=str(workspace),
            executable=shutil.which("opencode") or "opencode",
            model=model,
            environment=environment,
            task_timeout_seconds=120,
        )
    )
    try:
        await driver.start_turn(
            TurnInput(
                "Load the puffo-e2e skill, follow it, and return only its "
                "required sentinel."
            )
        )
        events = []
        async for event in driver.events():
            events.append(event)
            if event.type is HarnessEventType.TURN_COMPLETED:
                break

        assert any(
            event.type is HarnessEventType.TOOL_COMPLETED
            and event.data.get("label") == "skill"
            and event.data.get("outcome") == "succeeded"
            for event in events
        ), events
        assert "".join(
            str(event.data.get("delta") or "")
            for event in events
            if event.type is HarnessEventType.ASSISTANT_DELTA
        ) == "SENTINEL-OPENCODE-SKILL"

        context = await driver.context_status()
        assert context.used_tokens and context.used_tokens > 0
        assert context.context_window and context.context_window > 0
        assert not context.stale

        native_session_id = driver._native_session_id
        receipt = await driver.compact(CompactRequest())
        assert receipt.accepted and receipt.operation_ref
        compact_events = []
        async for event in driver.events():
            compact_events.append(event)
            if event.type in {
                HarnessEventType.COMPACTION_COMPLETED,
                HarnessEventType.COMPACTION_FAILED,
            }:
                break
        assert compact_events[-1].type is HarnessEventType.COMPACTION_COMPLETED
        assert compact_events[-1].data["operation_ref"] == receipt.operation_ref
        assert driver._serve_proc is None

        await driver.start_turn(
            TurnInput(
                "After compaction, return only the exact sentinel you were "
                "required to emit earlier."
            )
        )
        continued = []
        async for event in driver.events():
            continued.append(event)
            if event.type is HarnessEventType.TURN_COMPLETED:
                break
        assert driver._native_session_id == native_session_id
        assert "SENTINEL-OPENCODE-SKILL" in "".join(
            str(event.data.get("delta") or "")
            for event in continued
            if event.type is HarnessEventType.ASSISTANT_DELTA
        )
    finally:
        await driver.close()
