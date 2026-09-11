"""The publisher half of `tools/seed_cloud_agent.py`.

Ported from cloud-infra#174, which shipped this logic in a tree that repo's CI
does not run. Same cases, this PR's names (`agent_name`, `SeedError`), and a
suite that actually runs them.

The behaviours pinned here come from measuring a real 62-directory fleet: half
the directories are empty scratch dirs, 30 agents have no memory at all, and two
slug shapes are in use.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import seed_cloud_agent  # noqa: E402
from seed_cloud_agent import (  # noqa: E402
    SeedError,
    discover,
    resolve_one,
    agent_name,
    soul_body,
    stage,
)


def _agent(root: Path, slug: str, *, profile: str | None = None, notes=()) -> Path:
    d = root / slug
    d.mkdir(parents=True)
    if profile is not None:
        (d / "profile.md").write_text(profile)
        (d / "agent.yml").write_text(f"id: {slug}\n")
    for rel, body in notes:
        f = d / "memory" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return d


class TestSoulExtraction:
    """All 31 real profiles in the surveyed fleet carry a soul-like heading,
    so this is the importer's one hard dependency on profile shape."""

    def test_body_runs_to_the_next_same_level_heading(self):
        md = "# The Desk\nintro\n\n# Soul\n\nvoice\n\n## How you speak\nrules\n"
        # `##` is deeper than `#`, so the subsection stays inside the body
        assert soul_body(md) == "voice\n\n## How you speak\nrules"

    def test_a_deeper_heading_delimits_a_deeper_soul(self):
        md = "## Soul\nbody\n\n## Next\nother\n"
        assert soul_body(md) == "body"

    @pytest.mark.parametrize("h", ["# Soul", "## Description", "# About", "## Summary"])
    def test_every_accepted_spelling(self, h):
        assert soul_body(f"{h}\nbody\n") == "body"

    def test_no_heading_is_empty_not_an_error(self):
        assert soul_body("# Agent\njust prose\n") == ""


class TestDiscovery:
    """Half the surveyed fleet was empty scratch directories."""

    def test_requires_both_profile_and_agent_yml(self, tmp_path):
        _agent(tmp_path, "real", profile="# Soul\nx\n")
        (tmp_path / "scratch").mkdir()
        (tmp_path / "half").mkdir()
        (tmp_path / "half" / "profile.md").write_text("# Soul\nx\n")
        assert [d.name for d in discover(tmp_path)] == ["real"]

    def test_missing_fleet_dir_is_a_clear_error(self, tmp_path):
        with pytest.raises(SeedError, match="no fleet directory"):
            discover(tmp_path / "nope")


class TestResolution:
    def test_slug_prefix(self, tmp_path):
        _agent(tmp_path, "desk-6332-b73d5e96", profile="# Soul\nx\n")
        assert resolve_one("desk", tmp_path).name == "desk-6332-b73d5e96"

    def test_ambiguous_prefix_refuses(self, tmp_path):
        _agent(tmp_path, "desk-1-aaaaaaaa", profile="# Soul\nx\n")
        _agent(tmp_path, "desk-2-bbbbbbbb", profile="# Soul\nx\n")
        with pytest.raises(SeedError, match="ambiguous"):
            resolve_one("desk", tmp_path)

    def test_unknown_prefix_refuses(self, tmp_path):
        with pytest.raises(SeedError, match="no agent matching"):
            resolve_one("ghost", tmp_path)


@pytest.mark.parametrize(
    "slug, expected",
    [
        ("desk-6332-b73d5e96", "desk"),  # name-NNNN-hex
        ("planner-2d35d73e", "planner"),  # older name-hex
        ("qa-eab9b962", "qa"),
        ("fundamenta-4179-75907e80", "fundamenta"),
        ("desk", "desk"),  # already short
    ],
)
def test_agent_name_handles_both_slug_shapes(slug, expected):
    """A cloud agent gets a fresh slug on every create; its memory should follow
    the agent, not the identity it happens to be wearing."""
    assert agent_name(slug) == expected


class TestStaging:
    def test_copies_profile_and_memory(self, tmp_path):
        src = _agent(
            tmp_path / "fleet",
            "desk-6332-b73d5e96",
            profile="# Desk\n\n# Soul\n\nfront of house\n",
            notes=[("notes/a.md", "one"), ("briefing/b.md", "two")],
        )
        out = stage(src, tmp_path / "repo" / "desk")
        assert out["memory_files"] == 2
        assert out["soul_chars"] == len("front of house")
        assert (tmp_path / "repo" / "desk" / "memory" / "notes" / "a.md").read_text() == "one"

    def test_agent_with_no_memory_still_imports(self, tmp_path):
        """30 of the surveyed fleet have no memory dir — that is not a failure."""
        src = _agent(tmp_path / "fleet", "grist-1-aaaaaaaa", profile="# Soul\nx\n")
        out = stage(src, tmp_path / "repo" / "grist")
        assert out["memory_files"] == 0
        assert (tmp_path / "repo" / "grist" / "memory").is_dir()

    def test_identity_and_bulk_are_left_behind(self, tmp_path):
        """Carrying keys/ would make this a migration; workspace/ is 100MB of
        reproducible checkout."""
        src = _agent(tmp_path / "fleet", "desk-1-aaaaaaaa", profile="# Soul\nx\n")
        (src / "keys").mkdir()
        (src / "keys" / "k.json").write_text("SECRET")
        (src / "workspace").mkdir()
        (src / "workspace" / "big.bin").write_text("x" * 1000)
        (src / "messages.db").write_text("db")
        dest = tmp_path / "repo" / "desk"
        stage(src, dest)
        assert not (dest / "keys").exists()
        assert not (dest / "workspace").exists()
        assert not (dest / "messages.db").exists()
        assert {p.name for p in dest.iterdir()} == {"profile.md", "memory"}

    def test_agent_local_git_dir_is_not_carried(self, tmp_path):
        """~/memory is itself a git repo; its .git is scaffolding, not content."""
        src = _agent(
            tmp_path / "fleet",
            "desk-1-aaaaaaaa",
            profile="# Soul\nx\n",
            notes=[("notes/a.md", "one"), (".git/config", "[core]")],
        )
        out = stage(src, tmp_path / "repo" / "desk")
        assert out["memory_files"] == 1
        assert not (tmp_path / "repo" / "desk" / "memory" / ".git").exists()

    def test_missing_soul_is_warned_not_fatal(self, tmp_path):
        src = _agent(tmp_path / "fleet", "x-1-aaaaaaaa", profile="# Agent\nprose\n")
        out = stage(src, tmp_path / "repo" / "x")
        assert out["warnings"] == ["profile has no soul-like heading"]


class TestTheProbeExtractsTheRelayHost:
    """The probe's relay-host extraction is shell, not Python, and the first
    version of it (`awk -F'[/:]' '{print $4}'`) silently produced an empty
    string for every real agent.yml — splitting `https://host/relay` on
    `[/:]` puts the host in $5, because `//` yields an empty field. A live
    `--verify` reported "relay host not resolvable" for a healthy agent with
    two sockets to the relay. So run the real line, under a real shell."""

    @staticmethod
    def _extract(tmp_path, yaml_text: str) -> str:
        """Run the probe's own RELAY= line against a fixture agent.yml."""
        line = next(
            ln for ln in seed_cloud_agent._PROBE.splitlines() if ln.startswith("RELAY=")
        )
        (tmp_path / "agent.yml").write_text(yaml_text)
        out = subprocess.run(
            ["bash", "-c", f'A="{tmp_path}"\n{line}\nprintf %s "$RELAY"'],
            capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout

    def test_the_real_staging_relay_url(self, tmp_path):
        assert self._extract(
            tmp_path, "server:\n  server_url: https://chat-staging.puffo.ai/relay\n"
        ) == "chat-staging.puffo.ai"

    def test_a_quoted_url(self, tmp_path):
        assert self._extract(
            tmp_path, '  server_url: "https://chat.puffo.ai/relay"\n'
        ) == "chat.puffo.ai"

    def test_a_url_with_no_path(self, tmp_path):
        assert self._extract(tmp_path, "  server_url: wss://relay.example.com\n") == "relay.example.com"

    def test_a_url_with_a_port(self, tmp_path):
        assert self._extract(tmp_path, "  server_url: http://localhost:8080/relay\n") == "localhost:8080"

    def test_no_server_url_yields_empty_not_garbage(self, tmp_path):
        """Empty is the signal `conns_relay=-1` rides on — it must stay empty."""
        assert self._extract(tmp_path, "harness: claude-code\n") == ""
