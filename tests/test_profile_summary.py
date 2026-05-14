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

from puffo_agent.portal.api.handlers import (
    _profile_summary,
    _update_profile_summary,
)
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
    """Multi-paragraph Soul with sub-sections: every line between
    ``# Soul`` and the next same-or-higher-level heading (here EOF)
    is returned, including ``## ...`` sub-headings, with internal
    blank lines preserved. Operators rely on sub-headings — the
    helper template in ~/Downloads/markdowns/helper-agent.md uses
    ``## How you act`` / ``## Tone`` / ``## What you don't do`` —
    so a multi-level capture is the round-trip-faithful behaviour."""
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
            "Sub-section content stays in the body.\n\n"
            "## Another subsection\n\n"
            "Also part of soul.\n"
        )
        cfg = _agent_with_profile(home, body)
        out = _profile_summary(cfg)
        # First-line + subsequent paragraphs.
        assert "First paragraph of soul." in out
        assert "Wrapped onto a second line." in out
        assert "Second paragraph." in out
        # Sub-headings (## ...) are deeper than the # Soul section
        # heading and stay part of the body so the operator's
        # markdown structure round-trips.
        assert "## Subsection" in out
        assert "Sub-section content stays in the body." in out
        assert "## Another subsection" in out
        assert "Also part of soul." in out
        # Leading + trailing whitespace trimmed.
        assert not out.startswith("\n")
        assert not out.endswith("\n")


def test_stops_on_same_level_heading_after_soul(monkeypatch):
    """A later H1 (same level as ``# Soul``) closes the section.
    Trailing top-level notes after Soul shouldn't leak in."""
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("PUFFO_AGENT_HOME", home)
        body = (
            "# Soul\n\n"
            "The actual soul.\n\n"
            "## A subsection\n\n"
            "Still soul.\n\n"
            "# Notes\n\n"
            "Operator's private notes — not soul.\n"
        )
        cfg = _agent_with_profile(home, body)
        out = _profile_summary(cfg)
        assert "The actual soul." in out
        assert "## A subsection" in out
        assert "Still soul." in out
        assert "# Notes" not in out
        assert "Operator's private notes" not in out


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


def test_soul_body_may_open_with_its_own_heading(monkeypatch):
    """A soul body that leads with its own H1 — ``# Soul`` immediately
    followed by ``# <agent-name>`` — is captured in full. The opening
    heading is part of the soul, not the end of the section.

    Regression: before the span fix this returned an empty string
    because the second H1 was read as a sibling heading closing
    ``# Soul`` instantly. The original templates (and the operator's
    ~/Downloads/markdowns souls) all open this way."""
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("PUFFO_AGENT_HOME", home)
        body = (
            "# Identity\nd2d2\n\n"
            "# Soul\n"
            "# d2d2-butler\n\n"
            "> Language rule: match the human.\n\n"
            "## Identity\n\n"
            "You are the household butler.\n"
        )
        cfg = _agent_with_profile(home, body)
        out = _profile_summary(cfg)
        assert "# d2d2-butler" in out
        assert "> Language rule: match the human." in out
        assert "## Identity" in out
        assert "You are the household butler." in out


def test_update_replaces_body_that_opens_with_a_heading(monkeypatch):
    """Updating the soul of a profile whose body opens with its own
    H1 REPLACES the whole body — it must not insert the new text
    above the old (the append-duplicate bug). A post-update read
    sees only the new content; the Identity section is untouched."""
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("PUFFO_AGENT_HOME", home)
        body = (
            "# Identity\nd2d2\n\n"
            "# Soul\n"
            "# old-title\n\n"
            "Old soul body.\n"
        )
        cfg = _agent_with_profile(home, body)
        _update_profile_summary(cfg, "# new-title\n\nBrand new soul body.")
        out = _profile_summary(cfg)
        assert "# new-title" in out
        assert "Brand new soul body." in out
        assert "old-title" not in out
        assert "Old soul body." not in out
        text = cfg.resolve_profile_path().read_text(encoding="utf-8")
        assert text.startswith("# Identity\nd2d2\n")


def test_update_preserves_trailing_section(monkeypatch):
    """A ``# Notes`` section after ``# Soul`` survives an update —
    only the soul body between the headings is replaced."""
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("PUFFO_AGENT_HOME", home)
        body = (
            "# Soul\n\nOld soul.\n\n"
            "# Notes\n\nOperator's private notes.\n"
        )
        cfg = _agent_with_profile(home, body)
        _update_profile_summary(cfg, "New soul.")
        text = cfg.resolve_profile_path().read_text(encoding="utf-8")
        assert "New soul." in text
        assert "Old soul." not in text
        assert "# Notes" in text
        assert "Operator's private notes." in text


def test_update_appends_soul_when_absent(monkeypatch):
    """A profile with no description-like heading gets a fresh
    ``# Soul`` section appended."""
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("PUFFO_AGENT_HOME", home)
        cfg = _agent_with_profile(home, "# Identity\nd2d2\n")
        _update_profile_summary(cfg, "Freshly added soul.")
        out = _profile_summary(cfg)
        assert out == "Freshly added soul."
