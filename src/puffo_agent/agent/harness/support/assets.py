"""Declarative projection of catalog assets into harness-owned surfaces.

This is deliberately smaller than a general harness plugin contract.  It
describes only the choices consumed by spawn-time desired installs: where a
harness reads skills, how their bodies are adapted, and how MCP specs cross
the runtime boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ...shared_content import (
    _rewrite_puffo_mcp_prefix_for_opencode,
    _strip_puffo_mcp_prefix_for_codex,
)


class SkillRoot(str, Enum):
    AGENT_CLAUDE = "agent_claude"
    AGENT_PI = "agent_pi"
    WORKSPACE_AGENTS = "workspace_agents"


class SkillBodyTransform(str, Enum):
    IDENTITY = "identity"
    STRIP_PUFFO_PREFIX = "strip_puffo_prefix"
    PUFFO_SERVER_PREFIX = "puffo_server_prefix"


class McpProjection(str, Enum):
    CLAUDE_JSON = "claude_json"
    RUNTIME_EXTRAS = "runtime_extras"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class HarnessAssetsProfile:
    harness_name: str
    skill_root: SkillRoot | None
    skill_body_transform: SkillBodyTransform
    mcp_projection: McpProjection
    unsupported_reason: str = ""

    @property
    def supported(self) -> bool:
        return self.skills_supported or self.mcp_supported

    @property
    def skills_supported(self) -> bool:
        return self.skill_root is not None

    @property
    def mcp_supported(self) -> bool:
        return self.mcp_projection is not McpProjection.UNSUPPORTED

    def skills_root(self, agent_home: Path, workspace_dir: Path) -> Path:
        if self.skill_root is SkillRoot.AGENT_CLAUDE:
            return agent_home / ".claude" / "skills"
        if self.skill_root is SkillRoot.AGENT_PI:
            return agent_home / ".pi" / "agent" / "skills"
        if self.skill_root is SkillRoot.WORKSPACE_AGENTS:
            return workspace_dir / ".agents" / "skills"
        raise ValueError(
            f"harness {self.harness_name!r} has no desired-skill projection"
        )

    def transform_skill_body(self, body: str) -> str:
        if self.skill_body_transform is SkillBodyTransform.STRIP_PUFFO_PREFIX:
            return _strip_puffo_mcp_prefix_for_codex(body)
        if self.skill_body_transform is SkillBodyTransform.PUFFO_SERVER_PREFIX:
            return _rewrite_puffo_mcp_prefix_for_opencode(body)
        return body


_PROFILES = {
    "claude-code": HarnessAssetsProfile(
        harness_name="claude-code",
        skill_root=SkillRoot.AGENT_CLAUDE,
        skill_body_transform=SkillBodyTransform.IDENTITY,
        mcp_projection=McpProjection.CLAUDE_JSON,
    ),
    "codex": HarnessAssetsProfile(
        harness_name="codex",
        skill_root=SkillRoot.WORKSPACE_AGENTS,
        skill_body_transform=SkillBodyTransform.STRIP_PUFFO_PREFIX,
        mcp_projection=McpProjection.RUNTIME_EXTRAS,
    ),
    "pi": HarnessAssetsProfile(
        harness_name="pi",
        skill_root=SkillRoot.AGENT_PI,
        skill_body_transform=SkillBodyTransform.IDENTITY,
        mcp_projection=McpProjection.UNSUPPORTED,
        unsupported_reason=(
            "Pi has no built-in MCP; a Puffo tool bridge is required"
        ),
    ),
    "opencode": HarnessAssetsProfile(
        harness_name="opencode",
        # Verified against opencode 1.18.16: `opencode debug skill`
        # discovers workspace-level `.agents/skills/*/SKILL.md`. The
        # workspace root (not home-level `~/.agents/skills`, which
        # opencode also reads) keeps installs per-agent — a home-level
        # root is one shared physical directory for every agent whose
        # harness runs without HOME redirection.
        skill_root=SkillRoot.WORKSPACE_AGENTS,
        skill_body_transform=SkillBodyTransform.PUFFO_SERVER_PREFIX,
        # Core Puffo MCP already reaches opencode inline via
        # opencode.json written at spawn (local_runtime); a catalog-MCP
        # projection into that config does not exist yet.
        mcp_projection=McpProjection.UNSUPPORTED,
        unsupported_reason=(
            "no catalog-MCP projection into opencode.json yet; core "
            "Puffo MCP is injected inline at spawn"
        ),
    ),
    "hermes": HarnessAssetsProfile(
        harness_name="hermes",
        skill_root=None,
        skill_body_transform=SkillBodyTransform.IDENTITY,
        mcp_projection=McpProjection.UNSUPPORTED,
        unsupported_reason="no skills/MCP surface in hermes v1",
    ),
}


def get_harness_assets_profile(harness_name: str) -> HarnessAssetsProfile:
    """Return one closed profile; unknown harnesses fail closed."""
    profile = _PROFILES.get(harness_name)
    if profile is not None:
        return profile
    return HarnessAssetsProfile(
        harness_name=harness_name,
        skill_root=None,
        skill_body_transform=SkillBodyTransform.IDENTITY,
        mcp_projection=McpProjection.UNSUPPORTED,
        unsupported_reason="no registered desired skill/MCP projection",
    )
