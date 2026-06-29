"""Home/agent path resolution + agent-id validation.

Stdlib-only (``os`` / ``re`` / ``pathlib``). These are the canonical
definitions; ``puffo_agent.portal.state`` re-exports them so every
existing ``from ..portal.state import home_dir / agent_dir / ...``
call site keeps resolving unchanged."""

from __future__ import annotations

import os
import re
from pathlib import Path


# Where daemon.yml, agents/, etc. live.
def home_dir() -> Path:
    override = os.environ.get("PUFFO_AGENT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".puffo-agent"


def agents_dir() -> Path:
    return home_dir() / "agents"


def agent_dir(agent_id: str) -> Path:
    return agents_dir() / agent_id


def agent_yml_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "agent.yml"


_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def is_valid_agent_id(agent_id: str) -> bool:
    return bool(_AGENT_ID_RE.match(agent_id)) and len(agent_id) <= 64
