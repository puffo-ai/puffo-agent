"""puffo-agent-core — stdlib-only shared kernel.

The pure helpers that both the fat local agent (``puffo-agent``) and
the slim cloud runtime (``puffo-agent-cloud``) depend on: home/agent
path resolution, agent-id validation, and profile (``# Soul``) parsing.
NO third-party imports live here — that invariant is what lets the
cloud package reuse these without pulling psutil / pyside6 / crypto."""

from __future__ import annotations

from .paths import (
    agent_dir,
    agent_yml_path,
    agents_dir,
    home_dir,
    is_valid_agent_id,
)
from .profile import extract_soul_body

__all__ = [
    "agent_dir",
    "agent_yml_path",
    "agents_dir",
    "home_dir",
    "is_valid_agent_id",
    "extract_soul_body",
]
