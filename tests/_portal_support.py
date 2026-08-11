"""Shared fixtures for portal, control, import, export, and UI tests."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from puffo_agent.crypto.encoding import base64url_encode


def isolated_home() -> str:
    home = tempfile.mkdtemp(prefix="puffo-agent-test-")
    os.environ["PUFFO_AGENT_HOME"] = home
    os.environ["PUFFO_HOME"] = home
    Path(home, "agents").mkdir(parents=True, exist_ok=True)
    return home


def write_test_agent(
    home: str,
    agent_id: str,
    *,
    owner_root_pubkey: str | None = None,
    workspace_files: dict[str, str] | None = None,
) -> Path:
    import yaml

    agent_root = Path(home) / "agents" / agent_id
    agent_root.mkdir(parents=True, exist_ok=True)
    workspace = agent_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    slug = f"{agent_id}-bot"
    config = {
        "id": agent_id,
        "state": "running",
        "display_name": agent_id,
        "puffo_core": {
            "server_url": "http://localhost:3000",
            "slug": slug,
            "device_id": "dev_agent",
            "space_id": "sp_test",
        },
        "runtime": {
            "kind": "chat-local",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_key": "sk-ant-test-secret",
            "harness": "claude-code",
            "permission_mode": "bypassPermissions",
        },
        "profile": "profile.md",
        "memory_dir": "memory",
        "workspace_dir": "workspace",
        "triggers": {"on_mention": True, "on_dm": True},
    }
    (agent_root / "agent.yml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8",
    )
    (agent_root / "profile.md").write_text("# test profile\n", encoding="utf-8")
    (agent_root / "memory").mkdir(exist_ok=True)
    if owner_root_pubkey is not None:
        keys_dir = agent_root / "keys"
        keys_dir.mkdir(exist_ok=True)
        identity_cert = {
            "type": "identity_cert",
            "version": 1,
            "root_public_key": "agent-root-pk-placeholder",
            "identity_type": "agent",
            "declared_operator_public_key": owner_root_pubkey,
        }
        (keys_dir / f"{slug}.json").write_text(
            json.dumps(
                {
                    "slug": slug,
                    "device_id": "dev_agent",
                    "root_secret_key": base64url_encode(b"\x01" * 32),
                    "device_signing_secret_key": base64url_encode(b"\x02" * 32),
                    "kem_secret_key": base64url_encode(b"\x03" * 32),
                    "server_url": "http://localhost:3000",
                    "identity_cert_json": json.dumps(identity_cert),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    for relative_path, content in (workspace_files or {}).items():
        target = workspace / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return workspace
