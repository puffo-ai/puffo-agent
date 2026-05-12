"""``handlers._profile_summary`` returns the full body of the ``#
Soul`` section in an agent's profile.md (or any description-like
heading: description / about / summary). Symmetric with the write
path in ``_update_profile_summary`` so the round-trip preserves the
operator's full text.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.api.handlers import _profile_summary
from puffo_agent.portal.state import AgentConfig


def _agent_with_profile(home: str, profile_text: str, agent_id: str = "smoke") -> AgentConfig:
    """Materialise an agent dir on disk with the given profile.md
    body, then load it through ``AgentConfig.load`` so the test runs
    against the real path-resolution logic."""
    adir = os.path.join(home, "agents", agent_id)
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "agent.yml"), "w", encoding="utf-8") as f:
        yaml.safe_dump({
            "id": agent_id,
            "state": "running",
            "display_name": agent_id,
            "puffo_core": {
                "server_url": "http://localhost:3000",
                "slug": f"{agent_id}-0001",
                "device_id": "dev_test",
                "space_id": "sp_test",
            },
            "runtime": {"kind": "chat-local", "provider": "anthropic"},
            "profile": "profile.md",
            "memory_dir": "memory",
            "workspace_dir": "workspace",
            "triggers": {"on_mention": True, "on_dm": True},
        }, f, sort_keys=False)
    with open(os.path.join(adir, "profile.md"), "w", encoding="utf-8") as f:
        f.write(profile_text)
    return AgentConfig.load(agent_id)


def test_returns_full_soul_body_multi_paragraph(monkeypatch):
    """Multi-paragraph Soul section: every line between ``# Soul``
    and the next heading (or EOF) is returned with internal blank
    lines preserved."""
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("PUFFO_AGENT_HOME", home)
        body = (
            "# Display\n\n"
            "**Role:** helper: ...\n\n"
            "**Operator:** @alice\n\n"
            "# Soul\n\n"
            "First paragraph of soul.\n"
            "Wrapped onto a second line.\n\n"
            "Second paragraph.\n\n"
            "## Subsection\n\n"
            "Still part of the soul because the subsection sits inside it.\n"
        )
        cfg = _agent_with_profile(home, body)
        out = _profile_summary(cfg)
        assert "First paragraph of soul." in out
        assert "Wrapped onto a second line." in out
        assert "Second paragraph." in out
        # Sub-section heading INSIDE the soul block stops collection,
        # matching the rule "any later heading closes the section".
        # This avoids accidentally swallowing trailing notes the
        # operator stashed below the soul.
        assert "## Subsection" not in out
        assert "Still part of the soul" not in out
        # Leading + trailing whitespace trimmed.
        assert not out.startswith("\n")
        assert not out.endswith("\n")


def test_returns_empty_when_no_soul_section(monkeypatch):
    """Profile with no Soul-like heading → empty string."""
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("PUFFO_AGENT_HOME", home)
        cfg = _agent_with_profile(home, "# Hello\n\nNot a soul section.\n")
        assert _profile_summary(cfg) == ""


def test_accepts_alternative_headings(monkeypatch):
    """``Description`` / ``About`` / ``Summary`` are all treated like
    ``Soul`` so legacy profiles created before the spec rename keep
    working."""
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("PUFFO_AGENT_HOME", home)
        cfg = _agent_with_profile(
            home,
            "# Description\n\nLegacy single-paragraph body.\n",
            agent_id="legacy",
        )
        out = _profile_summary(cfg)
        assert out == "Legacy single-paragraph body."


def test_unreadable_profile_returns_empty(monkeypatch):
    """Missing profile.md → empty string, no exception. The bridge
    handler uses this on every GET /v1/agents row so a broken
    profile can't fail the whole list."""
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("PUFFO_AGENT_HOME", home)
        # Build a dir without profile.md.
        adir = os.path.join(home, "agents", "no-profile")
        os.makedirs(adir, exist_ok=True)
        with open(os.path.join(adir, "agent.yml"), "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "id": "no-profile",
                "state": "running",
                "display_name": "no-profile",
                "puffo_core": {
                    "server_url": "http://localhost:3000",
                    "slug": "no-profile-0001",
                    "device_id": "dev_test",
                    "space_id": "sp_test",
                },
                "runtime": {"kind": "chat-local", "provider": "anthropic"},
                "profile": "profile.md",
                "memory_dir": "memory",
                "workspace_dir": "workspace",
                "triggers": {"on_mention": True, "on_dm": True},
            }, f, sort_keys=False)
        cfg = AgentConfig.load("no-profile")
        assert _profile_summary(cfg) == ""


def test_trims_blank_lines_around_body(monkeypatch):
    """Surrounding blank lines from layout whitespace are trimmed
    so the returned body starts and ends at content."""
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("PUFFO_AGENT_HOME", home)
        cfg = _agent_with_profile(
            home,
            "# Soul\n\n\n\nLine 1.\nLine 2.\n\n\n",
        )
        out = _profile_summary(cfg)
        assert out == "Line 1.\nLine 2."
