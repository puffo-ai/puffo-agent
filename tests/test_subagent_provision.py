"""PUF-395: create-time sub-agent provisioning — inline .md validation +
write, plus the write path through ``write_agent_from_context``.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from puffo_agent.portal.subagent_provision import (
    sha256_hex,
    validate_desired_subagents,
    write_subagents,
    _frontmatter_name,
)


def _md(name: str, *, body: str = "You review code.") -> str:
    return (
        f"---\nname: {name}\ndescription: A {name}.\ntools: Read, Grep\n"
        f"model: sonnet\n---\n\n{body}\n"
    )


# ── _frontmatter_name ────────────────────────────────────────────────

def test_frontmatter_name_extracts():
    assert _frontmatter_name(_md("code-reviewer")) == "code-reviewer"


def test_frontmatter_name_tolerates_leading_whitespace():
    assert _frontmatter_name("\n\n" + _md("tdd-guide")) == "tdd-guide"


def test_frontmatter_name_none_without_fence():
    # Body-only content (no frontmatter) — claude-code wouldn't load it.
    assert _frontmatter_name("You are a reviewer, no frontmatter here.") is None


def test_frontmatter_name_none_on_unterminated_block():
    assert _frontmatter_name("---\nname: x\nno closing fence") is None


def test_frontmatter_name_none_on_bad_yaml():
    assert _frontmatter_name("---\nname: : : broken\n---\nbody") is None


def test_frontmatter_name_none_when_name_missing():
    assert _frontmatter_name("---\ndescription: no name\n---\nbody") is None


# ── validate_desired_subagents ───────────────────────────────────────

def test_validate_none_and_empty_are_empty():
    assert validate_desired_subagents(None, harness="claude-code") == []
    assert validate_desired_subagents([], harness="claude-code") == []


def test_validate_happy_single():
    got = validate_desired_subagents(
        [{"name": "code-reviewer", "content": _md("code-reviewer")}],
        harness="claude-code",
    )
    assert got == [{"name": "code-reviewer", "content": _md("code-reviewer")}]


def test_validate_happy_multiple():
    items = [
        {"name": "code-reviewer", "content": _md("code-reviewer")},
        {"name": "tdd-guide", "content": _md("tdd-guide")},
    ]
    assert validate_desired_subagents(items, harness="claude-code") == items


def test_validate_codex_nonempty_rejected():
    # AC #6 — hard error, not silent-drop.
    with pytest.raises(ValueError, match="codex"):
        validate_desired_subagents(
            [{"name": "code-reviewer", "content": _md("code-reviewer")}],
            harness="codex",
        )


def test_validate_codex_empty_ok():
    # AC #7 — empty/omitted on codex flows normally.
    assert validate_desired_subagents([], harness="codex") == []
    assert validate_desired_subagents(None, harness="codex") == []


def test_validate_non_list_rejected():
    with pytest.raises(ValueError, match="list"):
        validate_desired_subagents({"name": "x"}, harness="claude-code")


def test_validate_name_mismatch_rejected():
    # AC #10 — frontmatter name must match the request name.
    with pytest.raises(ValueError, match="does not match"):
        validate_desired_subagents(
            [{"name": "code-reviewer", "content": _md("security-reviewer")}],
            harness="claude-code",
        )


def test_validate_missing_frontmatter_rejected():
    # AC #10 — parse failure.
    with pytest.raises(ValueError, match="frontmatter"):
        validate_desired_subagents(
            [{"name": "code-reviewer", "content": "body only, no frontmatter"}],
            harness="claude-code",
        )


def test_validate_duplicate_names_rejected():
    # AC #11.
    with pytest.raises(ValueError, match="duplicate"):
        validate_desired_subagents(
            [
                {"name": "code-reviewer", "content": _md("code-reviewer")},
                {"name": "code-reviewer", "content": _md("code-reviewer", body="dup")},
            ],
            harness="claude-code",
        )


def test_validate_bad_name_charset_rejected():
    with pytest.raises(ValueError, match="kebab-case"):
        validate_desired_subagents(
            [{"name": "../evil", "content": _md("../evil")}],
            harness="claude-code",
        )


def test_validate_lax_frontmatter_with_valid_name_passes():
    # Intake: "daemon does NOT interpret semantics — harness owns them." A
    # valid name is the only structural guard; garbage tools/model pass through
    # and are caught (if at all) at claude-code load time. Source-pin the laxity.
    content = (
        "---\nname: loose-agent\ntools: not-a-list-lol\nmodel: 42\n"
        "description:\n---\n\nbody\n"
    )
    got = validate_desired_subagents(
        [{"name": "loose-agent", "content": content}], harness="claude-code",
    )
    assert got == [{"name": "loose-agent", "content": content}]


def test_validate_missing_content_rejected():
    with pytest.raises(ValueError, match="content"):
        validate_desired_subagents(
            [{"name": "code-reviewer"}], harness="claude-code",
        )


def test_validate_collects_all_errors():
    # AC #2 — every failure surfaced together, not just the first.
    with pytest.raises(ValueError) as exc:
        validate_desired_subagents(
            [
                {"name": "ok-one", "content": _md("ok-one")},
                {"name": "bad-name-mismatch", "content": _md("other")},
                {"name": "no-fm", "content": "nope"},
            ],
            harness="claude-code",
        )
    msg = str(exc.value)
    assert "bad-name-mismatch" in msg and "no-fm" in msg


# ── write_subagents ──────────────────────────────────────────────────

def test_write_subagents_creates_dir_on_demand(tmp_path):
    # AC #12 — .claude/agents created if absent.
    claude_dir = tmp_path / ".claude"
    assert not (claude_dir / "agents").exists()
    content = _md("code-reviewer")
    written = write_subagents(claude_dir, [{"name": "code-reviewer", "content": content}])
    md = claude_dir / "agents" / "code-reviewer.md"
    assert md.read_text(encoding="utf-8") == content
    assert written == [
        {"name": "code-reviewer", "sha256": hashlib.sha256(content.encode()).hexdigest()},
    ]


def test_write_subagents_empty_is_noop(tmp_path):
    claude_dir = tmp_path / ".claude"
    assert write_subagents(claude_dir, []) == []
    assert not (claude_dir / "agents").exists()


def test_write_subagents_byte_faithful_across_newlines(tmp_path):
    # sha256 contract: the on-disk file must be byte-identical to the reported
    # sha256 regardless of newline style (write_bytes, no translation).
    content = "---\r\nname: crlf-agent\r\n---\r\n\r\nbody\r\nline2\n"
    written = write_subagents(tmp_path / ".claude", [{"name": "crlf-agent", "content": content}])
    on_disk = (tmp_path / ".claude" / "agents" / "crlf-agent.md").read_bytes()
    assert on_disk == content.encode("utf-8")  # CRLF preserved verbatim
    assert written[0]["sha256"] == hashlib.sha256(on_disk).hexdigest()


def test_write_subagents_refuses_symlink(tmp_path):
    # Safe-by-construction: a pre-existing symlink at the target is refused,
    # never followed (would let a planted link escape the workspace).
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    (agents_dir / "code-reviewer.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        write_subagents(tmp_path / ".claude", [{"name": "code-reviewer", "content": _md("code-reviewer")}])
    assert not outside.exists()  # nothing written through the link


def test_sha256_hex_matches_stdlib():
    assert sha256_hex("hello") == hashlib.sha256(b"hello").hexdigest()


# ── write path through write_agent_from_context ──────────────────────

def _ctx(agent_id: str, subagents: list[dict]) -> dict:
    from puffo_agent.portal.state import RuntimeConfig
    return {
        "agent_id": agent_id,
        "display_name": "Reviewer Bot",
        "avatar_url": "",
        "role": "reviewer",
        "role_short": "rev",
        "profile_text": "# Role\n\nYou review.\n",
        "server_url": "https://api.puffo.ai",
        "slug": f"{agent_id}-slug",
        "device_id": "dev-1",
        "space_id": "sp_1",
        "operator_slug": "op-1",
        "runtime": RuntimeConfig(kind="cli-local", provider="anthropic", harness="claude-code"),
        "desired_skills": [],
        "desired_mcps": [],
        "desired_subagents": subagents,
        "bundle": {
            "root_secret_key": "rsk",
            "device_signing_secret_key": "dssk",
            "kem_secret_key": "ksk",
            "identity_cert": {"cert": "id"},
            "slug_binding": {"binding": "sb"},
        },
    }


def test_write_agent_from_context_lands_subagents(tmp_path, monkeypatch):
    # AC #3 + #5 — .md written under <workspace>/.claude/agents, response
    # carries {name, sha256}.
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    from puffo_agent.portal.control.provision import write_agent_from_context
    from puffo_agent.portal.state import agent_dir

    content = _md("code-reviewer")
    result = write_agent_from_context(
        _ctx("rev-bot", [{"name": "code-reviewer", "content": content}]),
    )
    md = agent_dir("rev-bot") / "workspace" / ".claude" / "agents" / "code-reviewer.md"
    assert md.read_text(encoding="utf-8") == content
    assert result["subagents"] == [
        {"name": "code-reviewer", "sha256": hashlib.sha256(content.encode()).hexdigest()},
    ]


def test_write_agent_from_context_prunes_on_subagent_write_failure(tmp_path, monkeypatch):
    # AC #4 partial-failure path: a disk-write failure AFTER validation prunes
    # the half-built agent dir (shutil.rmtree) rather than leaving it for the
    # reconcile loop. The new subagent write is a fresh IO surface; pin it.
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    from puffo_agent.portal.control import provision as provision_mod
    from puffo_agent.portal.control.provision import write_agent_from_context
    from puffo_agent.portal.state import agent_dir

    def _boom(claude_dir, subagents):
        raise OSError("disk full")

    # Patch where it's used, not where it's defined: provision.py binds the
    # symbol at import (``from ..subagent_provision import write_subagents``),
    # so patching the source module leaves the bound reference untouched.
    monkeypatch.setattr(provision_mod, "write_subagents", _boom)
    with pytest.raises(OSError):
        write_agent_from_context(
            _ctx("doomed-bot", [{"name": "code-reviewer", "content": _md("code-reviewer")}]),
        )
    assert not agent_dir("doomed-bot").exists()  # half-built dir pruned


def test_write_agent_from_context_no_subagents(tmp_path, monkeypatch):
    # AC #8 — no desired_subagents → no .claude/agents dir, empty response list.
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    from puffo_agent.portal.control.provision import write_agent_from_context
    from puffo_agent.portal.state import agent_dir

    result = write_agent_from_context(_ctx("plain-bot", []))
    assert result["subagents"] == []
    assert not (agent_dir("plain-bot") / "workspace" / ".claude" / "agents").exists()
