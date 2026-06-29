"""Keystore loader for api-puffo agents.

Server-side ``puffo-server/cloud_agent`` holds all crypto; the
runtime needs only the sandbox_token (bearer for the WS upgrade)
and the cloud server URL. ``slug`` is included for symmetry with
the legacy keystore + as a sanity check when constructing WS URLs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ...portal.state import agent_dir


@dataclass
class ApiPuffoKeystore:
    slug: str
    sandbox_token: str
    puffo_cloud_server_url: str

    @classmethod
    def for_agent(cls, agent_id: str) -> "ApiPuffoKeystore":
        path = agent_dir(agent_id) / "keys" / f"{agent_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            slug=raw["slug"],
            sandbox_token=raw["sandbox_token"],
            puffo_cloud_server_url=raw["puffo_cloud_server_url"],
        )
