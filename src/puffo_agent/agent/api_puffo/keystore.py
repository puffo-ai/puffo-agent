"""Keystore loader for api-puffo agents.

The on-disk shape (``<agent_dir>/keys/<slug>.json``) is a strict
superset of the legacy puffo-core keystore: KEM secret + session
token + cloud server URL replace the Ed25519 signing material.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...portal.state import agent_dir


@dataclass
class ApiPuffoKeystore:
    slug: str
    device_id: str
    kem_secret_key: str  # base64url-encoded 32 bytes
    kem_cert: dict
    session_token: str
    puffo_cloud_server_url: str

    @classmethod
    def for_agent(cls, agent_id: str) -> "ApiPuffoKeystore":
        path = agent_dir(agent_id) / "keys" / f"{agent_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            slug=raw["slug"],
            device_id=raw["device_id"],
            kem_secret_key=raw["kem_secret_key"],
            kem_cert=json.loads(raw["kem_cert_json"]),
            session_token=raw["session_token"],
            puffo_cloud_server_url=raw["puffo_cloud_server_url"],
        )
