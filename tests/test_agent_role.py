"""Agent profile ``role`` / ``role_short`` plumbing.

Covers the local pieces: ``AgentConfig`` load/save round-trip with
the new fields, ``_derive_role_short`` helper in the bridge handler
and the CLI helper. The bridge HTTP endpoints + server-side derive
have their own tests in puffo-server.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.api.handlers import _derive_role_short
from puffo_agent.portal.cli import _derive_role_short_cli
from puffo_agent.portal.state import AgentConfig


# ─── _derive_role_short (bridge handler + CLI mirror) ─────────────


@pytest.mark.parametrize("derive", [_derive_role_short, _derive_role_short_cli])
def test_derive_matches_recommended_shape(derive):
    """``<short>: <description>`` → returns the trimmed prefix."""
    assert derive("coder: main puffo-core coder") == "coder"
    assert derive("reviewer: code review specialist") == "reviewer"
    # Trailing whitespace before the colon is tolerated.
    assert derive("coder :  desc") == "coder"


@pytest.mark.parametrize("derive", [_derive_role_short, _derive_role_short_cli])
def test_derive_rejects_non_matching_shapes(derive):
    """Anything not matching the recommended shape returns the empty
    string so the caller can store an explicit-empty role_short
    rather than emit a malformed chip."""
    assert derive("") == ""
    assert derive("just a description") == ""
    assert derive(": missing prefix") == ""
    assert derive("only-prefix:") == ""
    assert derive("only-prefix:   ") == ""
    # Whitespace inside the prefix → not a clean short label.
    assert derive("two words: desc") == ""
    # Overlong prefix (>32 chars).
    too_long = "a" * 33
    assert derive(f"{too_long}: x") == ""


def test_bridge_and_cli_derive_agree():
    """Belt-and-suspenders: the two derives must stay in lockstep
    with each other (and with the server). They duplicate logic for
    different call sites — if one drifts, the agent.yml stored locally
    could disagree with what the server stores."""
    cases = [
        "coder: main coder",
        "plain text no colon",
        ": empty-prefix",
        "trailing:",
        "two words: nope",
        "a" * 33 + ": overlong",
        "",
    ]
    for c in cases:
        assert _derive_role_short(c) == _derive_role_short_cli(c), c


# ─── AgentConfig round-trip with role fields ──────────────────────


def test_agent_config_yaml_roundtrip_preserves_role(monkeypatch):
    """Load → save → load must preserve role + role_short. agent.yml
    is the local source of truth for what the daemon thinks the
    server's identity profile contains; round-trip drift would mean
    a daemon restart silently clears a configured role."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("PUFFO_AGENT_HOME", tmp)

        # Reproduce the on-disk shape ``bridge create_agent`` writes.
        agent_id = "smoke-bot"
        agents_dir = os.path.join(tmp, "agents", agent_id)
        os.makedirs(agents_dir)
        with open(os.path.join(agents_dir, "agent.yml"), "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "id": agent_id,
                "state": "running",
                "display_name": "Smoke Bot",
                "avatar_url": "",
                "role": "coder: smoke testing puffo",
                "role_short": "coder",
                "puffo_core": {
                    "server_url": "http://localhost:3000",
                    "slug": "smoke-bot-0001",
                    "device_id": "dev_smoke",
                    "space_id": "sp_smoke",
                    "operator_slug": "alice-0001",
                },
                "runtime": {
                    "kind": "chat-local",
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "api_key": "sk-ant-test",
                    "harness": "claude-code",
                    "permission_mode": "bypassPermissions",
                    "max_turns": 10,
                },
                "profile": "profile.md",
                "memory_dir": "memory",
                "workspace_dir": "workspace",
                "triggers": {"on_mention": True, "on_dm": True},
            }, f, sort_keys=False)

        cfg = AgentConfig.load(agent_id)
        assert cfg.role == "coder: smoke testing puffo"
        assert cfg.role_short == "coder"

        cfg.save()

        cfg2 = AgentConfig.load(agent_id)
        assert cfg2.role == "coder: smoke testing puffo"
        assert cfg2.role_short == "coder"


def test_agent_config_load_defaults_role_to_empty(monkeypatch):
    """Legacy agent.yml without role/role_short fields → defaults to
    empty string. Existing agents from before this change must keep
    loading without a fresh ``save`` step."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("PUFFO_AGENT_HOME", tmp)

        agent_id = "legacy-bot"
        agents_dir = os.path.join(tmp, "agents", agent_id)
        os.makedirs(agents_dir)
        with open(os.path.join(agents_dir, "agent.yml"), "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "id": agent_id,
                "state": "running",
                "display_name": "Legacy Bot",
                "puffo_core": {
                    "server_url": "http://localhost:3000",
                    "slug": "legacy-bot-0001",
                    "device_id": "dev_legacy",
                    "space_id": "sp_legacy",
                },
                "runtime": {
                    "kind": "chat-local",
                    "provider": "anthropic",
                },
                "profile": "profile.md",
                "memory_dir": "memory",
                "workspace_dir": "workspace",
                "triggers": {"on_mention": True, "on_dm": True},
            }, f, sort_keys=False)

        cfg = AgentConfig.load(agent_id)
        assert cfg.role == ""
        assert cfg.role_short == ""
