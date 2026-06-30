"""Keystore loader for api-puffo agents — sandbox_token + cloud
URL only; server-side holds the actual crypto."""

from __future__ import annotations

import json
from dataclasses import dataclass

from puffo_agent_core.paths import agent_dir


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
