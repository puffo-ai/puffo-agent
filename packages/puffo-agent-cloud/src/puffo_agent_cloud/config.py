"""Thin agent config for the cloud runtime.

Reads the ``agent.yml`` that ``bundle.materialise_agent_dir`` writes and
exposes ONLY what ``runner.py`` consumes: the display name, the LLM
``runtime`` triple (provider / model / api_key), and the resolved
profile path. Deliberately NOT the fat agent's ``AgentConfig`` — that
carries the whole runtime_matrix / docker / sync surface and would drag
the fat package (and its heavy deps) into the sandbox. Promoting the
full config into ``puffo-agent-core`` is Stage B."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from puffo_agent_core.paths import agent_dir, agent_yml_path


@dataclass
class CloudRuntime:
    provider: str = ""
    model: str = ""
    api_key: str = ""


@dataclass
class CloudAgentConfig:
    """Subset of ``agent.yml`` the cloud runner needs."""

    id: str = ""
    display_name: str = ""
    # Path to profile.md relative to the agent dir, or absolute.
    profile: str = "profile.md"
    runtime: CloudRuntime = field(default_factory=CloudRuntime)

    @classmethod
    def load(cls, agent_id: str) -> "CloudAgentConfig":
        path = agent_yml_path(agent_id)
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        rt = raw.get("runtime") or {}
        return cls(
            id=raw.get("id", agent_id),
            display_name=raw.get("display_name", ""),
            profile=raw.get("profile", "profile.md"),
            runtime=CloudRuntime(
                provider=rt.get("provider", ""),
                model=rt.get("model", ""),
                api_key=rt.get("api_key", ""),
            ),
        )

    def resolve_profile_path(self) -> Path:
        p = Path(self.profile)
        if p.is_absolute():
            return p
        return agent_dir(self.id) / p
