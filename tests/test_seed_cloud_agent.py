"""The publisher half of `tools/seed_cloud_agent.py`.

Ported from cloud-infra#174, which shipped this logic in a tree that repo's CI
does not run. Same cases, this PR's names (`agent_name`, `SeedError`), and a
suite that actually runs them.

The behaviours pinned here come from measuring a real 62-directory fleet: half
the directories are empty scratch dirs, 30 agents have no memory at all, and two
slug shapes are in use.
"""

from __future__ import annotations

import base64
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
    collect_memory,
    deliver,
    upload,
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


class TestCollectingMemory:
    """What goes up is memory and only memory. `profile.md` is read for the
    report but never uploaded: the agent store owns the persona and restores its
    copy on resume, so a profile written anywhere else is reverted on first idle."""

    def test_memory_is_collected_with_relative_paths(self, tmp_path):
        src = _agent(
            tmp_path / "fleet",
            "desk-6332-b73d5e96",
            profile="# Desk\n\n# Soul\n\nfront of house\n",
            notes=[("notes/a.md", "one"), ("briefing/b.md", "two")],
        )
        files, out = collect_memory(src)
        assert set(files) == {"notes/a.md", "briefing/b.md"}
        assert files["notes/a.md"] == b"one"
        assert out["memory_files"] == 2
        assert out["soul_chars"] == len("front of house")

    def test_the_profile_is_reported_but_not_collected(self, tmp_path):
        src = _agent(tmp_path / "fleet", "desk-1-aaaaaaaa", profile="# Soul\nx\n")
        files, out = collect_memory(src)
        assert out["profile_bytes"] > 0
        assert not any("profile" in k for k in files)

    def test_agent_with_no_memory_is_not_a_failure(self, tmp_path):
        """30 of the surveyed fleet have no memory dir."""
        src = _agent(tmp_path / "fleet", "grist-1-aaaaaaaa", profile="# Soul\nx\n")
        files, out = collect_memory(src)
        assert files == {} and out["memory_files"] == 0

    def test_identity_and_bulk_are_left_behind(self, tmp_path):
        """Carrying keys/ would make this a migration; workspace/ is 100MB of
        reproducible checkout. Only the memory tree is ever read."""
        src = _agent(tmp_path / "fleet", "desk-1-aaaaaaaa", profile="# Soul\nx\n")
        (src / "keys").mkdir()
        (src / "keys" / "k.json").write_text("SECRET")
        (src / "workspace").mkdir()
        (src / "workspace" / "big.bin").write_text("x" * 1000)
        (src / "messages.db").write_text("db")
        files, _ = collect_memory(src)
        blob = b"".join(files.values())
        assert b"SECRET" not in blob
        assert not any(k.startswith(("keys", "workspace")) or "messages.db" in k for k in files)

    def test_agent_local_git_dir_is_not_carried(self, tmp_path):
        """~/memory is itself a git repo; its .git is scaffolding, not content."""
        src = _agent(
            tmp_path / "fleet",
            "desk-1-aaaaaaaa",
            profile="# Soul\nx\n",
            notes=[("notes/a.md", "one"), (".git/config", "[core]")],
        )
        files, out = collect_memory(src)
        assert set(files) == {"notes/a.md"} and out["memory_files"] == 1

    def test_missing_soul_is_warned_not_fatal(self, tmp_path):
        src = _agent(tmp_path / "fleet", "x-1-aaaaaaaa", profile="# Agent\nprose\n")
        _, out = collect_memory(src)
        assert out["warnings"] == ["profile has no soul-like heading"]


    def test_a_symlink_out_of_the_tree_is_not_followed(self, tmp_path):
        """`is_file()` is true for a link to a file and `read_bytes()` reads the
        TARGET — a link under memory/ at ~/.ssh/id_ed25519 would upload the key
        as a note. Refused, not followed."""
        src = _agent(tmp_path / "fleet", "desk-1-aaaaaaaa", profile="# Soul\nx\n",
                     notes=[("real.md", "fine")])
        secret = tmp_path / "outside" / "id_ed25519"
        secret.parent.mkdir(); secret.write_text("PRIVATE KEY")
        (src / "memory" / "leak.md").symlink_to(secret)
        files, out = collect_memory(src)
        assert set(files) == {"real.md"}
        assert b"PRIVATE KEY" not in b"".join(files.values())
        assert out["memory_files"] == 1

    def test_a_symlinked_directory_is_not_descended(self, tmp_path):
        src = _agent(tmp_path / "fleet", "desk-1-aaaaaaaa", profile="# Soul\nx\n",
                     notes=[("real.md", "fine")])
        outside = tmp_path / "outside"; outside.mkdir()
        (outside / "creds.md").write_text("SECRET")
        (src / "memory" / "linked").symlink_to(outside, target_is_directory=True)
        files, _ = collect_memory(src)
        assert b"SECRET" not in b"".join(files.values())


class TestUpload:
    """Upload goes through the control API, never to S3 directly — handing an
    agent's owner S3 access would hand them a key to the bucket every other
    agent's config lives in."""

    def test_it_puts_base64_to_the_agents_memory_route(self, tmp_path, monkeypatch):
        seen = {}

        def fake_control(path, payload):
            seen["path"] = path
            seen["payload"] = payload
            return {"files": len(payload["files"]), "delivered": False}

        monkeypatch.setattr(seed_cloud_agent, "_control", fake_control)
        n = upload("desk-cloud-2779", {"notes/a.md": b"one"})
        assert n == 1
        assert seen["path"] == "/agents/desk-cloud-2779/memory"
        assert base64.b64decode(seen["payload"]["files"]["notes/a.md"]) == b"one"

    def test_non_utf8_notes_survive_the_round_trip(self, tmp_path, monkeypatch):
        """Base64 is why: JSON cannot carry raw bytes, and a note is not
        guaranteed to be text."""
        raw = b"\xff\xfe\x00binary"
        captured = {}
        monkeypatch.setattr(
            seed_cloud_agent,
            "_control",
            lambda p, payload: captured.update(payload) or {"files": 1},
        )
        upload("a1", {"odd.bin": raw})
        assert base64.b64decode(captured["files"]["odd.bin"]) == raw

    def test_a_short_ack_is_an_error_not_a_success(self, monkeypatch):
        """The server reports what it actually wrote; the client used to report
        that number as success without comparing it to what it sent."""
        monkeypatch.setattr(seed_cloud_agent, "_control", lambda p, b: {"files": 2})
        with pytest.raises(SeedError, match="stored 2 of 3"):
            upload("a1", {"a.md": b"1", "b.md": b"2", "c.md": b"3"})

    def test_a_full_ack_returns_the_count(self, monkeypatch):
        monkeypatch.setattr(seed_cloud_agent, "_control", lambda p, b: {"files": 3})
        assert upload("a1", {"a.md": b"1", "b.md": b"2", "c.md": b"3"}) == 3

    def test_nothing_to_upload_makes_no_call(self, monkeypatch):
        monkeypatch.setattr(
            seed_cloud_agent,
            "_control",
            lambda p, b: pytest.fail("should not call the API with no files"),
        )
        assert upload("a1", {}) == 0

    def test_missing_credentials_is_a_clear_error(self, monkeypatch):
        monkeypatch.delenv("AIM_CONTROL_URL", raising=False)
        monkeypatch.delenv("AIM_CONTROL_TOKEN", raising=False)
        with pytest.raises(SeedError, match="AIM_CONTROL_URL"):
            seed_cloud_agent._control("/agents/a1/memory", {"files": {}})


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



class TestDeliver:
    """Closes "stored, not delivered" without a reboot — a reboot mints a new
    identity (keys/ lives only in the sandbox) and breaks the Hub agent."""

    def test_it_posts_to_the_deliver_route(self, monkeypatch):
        seen = {}

        def fake(path, payload, method="PUT"):
            seen.update(path=path, method=method)
            return {"delivered": 2, "reason": "seeded into the running sandbox"}

        monkeypatch.setattr(seed_cloud_agent, "_control", fake)
        out = deliver("desk-cloud-2779")
        assert seen == {"path": "/agents/desk-cloud-2779/memory/deliver", "method": "POST"}
        assert out["delivered"] == 2

    def test_a_non_empty_sandbox_is_reported_not_raised(self, monkeypatch):
        """seed-once: the server leaves an agent with memory alone and says why."""
        monkeypatch.setattr(
            seed_cloud_agent, "_control",
            lambda p, b, method="PUT": {"delivered": 0, "reason": "sandbox memory is not empty"},
        )
        out = deliver("a1")
        assert out["delivered"] == 0 and "not empty" in out["reason"]

    def test_deliver_only_mode_needs_to(self, capsys):
        with pytest.raises(SystemExit):
            seed_cloud_agent.main(["--deliver"])
        assert "--to" in capsys.readouterr().err

    def test_deliver_only_mode_runs_without_from(self, monkeypatch, capsys):
        monkeypatch.setattr(
            seed_cloud_agent, "_control",
            lambda p, b, method="PUT": {"delivered": 3, "reason": "seeded"},
        )
        assert seed_cloud_agent.main(["--deliver", "--to", "a1"]) == 0
        assert "delivered 3" in capsys.readouterr().out
