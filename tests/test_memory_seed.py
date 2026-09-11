"""First-boot memory seeding.

The load-bearing property is negative: seeding must never overwrite memory the
agent wrote. Everything else here is convenience; that one is a data-loss guard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from puffo_agent.agent.memory_seed import (
    memory_is_empty,
    memory_seed_name,
    seed_from_remote,
)


def _fleet_repo(root: Path, name: str = "desk") -> str:
    """A real local git repo shaped like the fleet memory remote."""
    repo = root / "fleet"
    (repo / name / "memory" / "notes").mkdir(parents=True)
    (repo / name / "memory" / "notes" / "lesson.md").write_text("CRDO forward-EPS")
    (repo / name / "profile.md").write_text("# Desk\n\n# Soul\n\nfront of house\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "seed"], check=True,
    )
    return str(repo)


class TestEmptiness:
    """What counts as "this agent has memory" decides whether we touch it."""

    def test_absent_or_bare_tree_is_empty(self, tmp_path):
        assert memory_is_empty(tmp_path / "nope")
        (tmp_path / "memory" / "notes").mkdir(parents=True)
        assert memory_is_empty(tmp_path / "memory")

    def test_one_note_is_not_empty(self, tmp_path):
        m = tmp_path / "memory" / "notes"
        m.mkdir(parents=True)
        (m / "a.md").write_text("x")
        assert not memory_is_empty(tmp_path / "memory")

    def test_the_agents_own_git_dir_does_not_count_as_content(self, tmp_path):
        """`~/memory` is itself a git repo. Its scaffolding is not a memory, and
        counting it would make every agent look seeded and skip the seed."""
        m = tmp_path / "memory"
        (m / ".git").mkdir(parents=True)
        (m / ".git" / "config").write_text("[core]")
        (m / ".gitkeep").touch()
        assert memory_is_empty(m)


class TestSeeding:
    def test_seeds_an_empty_agent(self, tmp_path):
        remote = _fleet_repo(tmp_path)
        mem, prof = tmp_path / "agent" / "memory", tmp_path / "agent" / "profile.md"
        mem.mkdir(parents=True)
        prof.write_text("# Desk-cloud\n\n**Role:** agent\n\n# Soul\n\n")  # the Hub stub

        assert seed_from_remote(memory_root=mem, remote=remote, name="desk") == "seeded"
        assert (mem / "notes" / "lesson.md").read_text() == "CRDO forward-EPS"

    def test_never_overwrites_an_agent_that_has_written(self, tmp_path):
        """The guard that makes this safe on every boot."""
        remote = _fleet_repo(tmp_path)
        mem = tmp_path / "agent" / "memory" / "notes"
        mem.mkdir(parents=True)
        mine = mem / "mine.md"
        mine.write_text("something the agent learned")
        prof = tmp_path / "agent" / "profile.md"
        prof.write_text("# Desk-cloud\n")

        out = seed_from_remote(
            memory_root=mem.parent, remote=remote, name="desk"
        )
        assert out == "skip:has-memory"
        assert mine.read_text() == "something the agent learned"
        assert not (mem.parent / "notes" / "lesson.md").exists()

    def test_profile_is_never_touched(self, tmp_path):
        """The agent store owns profile.md and restores its copy on resume, so
        writing one here is reverted the first time the agent idles. Found by
        --verify on a live agent whose hand-seeded 12,797-byte profile came back
        as the 71-byte stub."""
        remote = _fleet_repo(tmp_path)
        mem = tmp_path / "agent" / "memory"
        mem.mkdir(parents=True)
        prof = tmp_path / "agent" / "profile.md"
        prof.write_text("# Stub\n")

        seed_from_remote(memory_root=mem, remote=remote, name="desk")
        assert prof.read_text() == "# Stub\n"

    def test_unknown_agent_in_the_remote_is_a_skip_not_a_failure(self, tmp_path):
        remote = _fleet_repo(tmp_path)
        mem = tmp_path / "a" / "memory"
        mem.mkdir(parents=True)
        assert seed_from_remote(
            memory_root=mem,
            remote=remote, name="ghost",
        ) == "skip:no-such-agent"

    def test_no_remote_configured_is_inert(self, tmp_path):
        assert seed_from_remote(
            memory_root=tmp_path, remote="", name="x"
        ) == "skip:no-remote"

    def test_an_unreachable_remote_degrades_rather_than_raising(self, tmp_path):
        """A boot-time convenience must never stop an agent from starting."""
        mem = tmp_path / "memory"
        mem.mkdir()
        out = seed_from_remote(
            memory_root=mem,
            remote=str(tmp_path / "definitely-not-a-repo"), name="desk",
        )
        assert out.startswith("failed:")


@pytest.mark.parametrize(
    "slug, expected",
    [
        ("desk-6332-b73d5e96", "desk"),
        ("planner-2d35d73e", "planner"),
        ("qa-eab9b962", "qa"),
        ("desk", "desk"),
    ],
)
def test_name_follows_the_agent_not_the_slug(slug, expected):
    """A cloud agent gets a fresh slug on every create; its memory should not."""
    assert memory_seed_name(slug) == expected


class TestConfigRoundTrip:
    """`memory_remote` must survive a load/save cycle, or an agent loses its
    seed source the first time anything rewrites its config."""

    def test_absent_defaults_to_empty_and_stays_inert(self, tmp_path):
        from puffo_agent.portal.state import AgentConfig

        d = tmp_path / "a-1"
        d.mkdir()
        (d / "agent.yml").write_text("id: a-1\nstate: running\n")
        (d / "profile.md").write_text("# A\n")
        cfg = AgentConfig.load(d)
        assert cfg.memory_remote == ""

    def test_round_trips(self, tmp_path):
        from puffo_agent.portal.state import AgentConfig

        d = tmp_path / "a-1"
        d.mkdir()
        (d / "agent.yml").write_text(
            "id: a-1\nstate: running\nmemory_remote: git@example:fleet.git\n"
        )
        (d / "profile.md").write_text("# A\n")
        cfg = AgentConfig.load(d)
        assert cfg.memory_remote == "git@example:fleet.git"
        cfg.save()
        assert "memory_remote: git@example:fleet.git" in (d / "agent.yml").read_text()
