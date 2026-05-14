import tempfile
from pathlib import Path

from puffo_agent.agent.shared_content import (
    DEFAULT_SHARED_CLAUDE_MD,
    rebuild_agent_claude_md,
    reseed_shared_primer,
)
from puffo_agent.portal.cli import build_parser


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


# ── reseed_shared_primer ────────────────────────────────────────────


def test_reseed_creates_on_fresh_dir():
    shared = _tmp() / "shared"
    actions = reseed_shared_primer(shared)
    assert actions, "expected managed files to be reported"
    assert all(action == "created" for _, action in actions)
    # CLAUDE.md landed with this install's content.
    assert (
        (shared / "CLAUDE.md").read_text(encoding="utf-8")
        == DEFAULT_SHARED_CLAUDE_MD
    )
    # skill files are reported with a posix-style relative path.
    assert any(rel.startswith("skills/") for rel, _ in actions)


def test_reseed_is_noop_when_already_current():
    shared = _tmp() / "shared"
    reseed_shared_primer(shared)
    actions = reseed_shared_primer(shared)
    assert all(action == "unchanged" for _, action in actions)


def test_reseed_overwrites_edited_file_and_backs_it_up():
    shared = _tmp() / "shared"
    reseed_shared_primer(shared)
    primer = shared / "CLAUDE.md"
    primer.write_text("operator's local edit", encoding="utf-8")

    by_rel = dict(reseed_shared_primer(shared))
    assert by_rel["CLAUDE.md"] == "updated (backed up)"
    # Untouched files aren't churned.
    assert by_rel["README.md"] == "unchanged"
    # The edit is recoverable, and the file is back to the install version.
    assert (
        (shared / "CLAUDE.md.bak").read_text(encoding="utf-8")
        == "operator's local edit"
    )
    assert primer.read_text(encoding="utf-8") == DEFAULT_SHARED_CLAUDE_MD


# ── rebuild_agent_claude_md ─────────────────────────────────────────


def test_rebuild_agent_claude_md_assembles_primer_profile_memory():
    root = _tmp()
    shared = root / "shared"
    profile = root / "profile.md"
    profile.write_text("# Soul\nI am a test agent.", encoding="utf-8")
    memory = root / "memory"
    memory.mkdir()
    (memory / "notes.md").write_text("a remembered fact", encoding="utf-8")
    workspace = root / "workspace"
    workspace.mkdir()
    claude_user = root / ".claude"
    gemini_user = root / ".gemini"

    out = rebuild_agent_claude_md(
        shared_dir=shared,
        profile_path=profile,
        memory_dir=memory,
        workspace_dir=workspace,
        claude_user_dir=claude_user,
        gemini_user_dir=gemini_user,
    )
    # Shared primer is seeded on demand, then primer + profile + memory
    # all land in the assembled prompt.
    assert "Puffo.ai platform primer" in out
    assert "I am a test agent." in out
    assert "a remembered fact" in out
    # Written to both user-level dirs.
    assert (claude_user / "CLAUDE.md").read_text(encoding="utf-8") == out
    assert (gemini_user / "GEMINI.md").read_text(encoding="utf-8") == out


# ── agent reset-primer CLI ──────────────────────────────────────────


def _seed_agent(home: Path, agent_id: str) -> Path:
    adir = home / "agents" / agent_id
    (adir / "memory").mkdir(parents=True)
    (adir / "profile.md").write_text(
        "# Soul\nI test things.", encoding="utf-8",
    )
    (adir / "agent.yml").write_text(
        f"id: {agent_id}\nstate: running\nruntime:\n  kind: chat-local\n",
        encoding="utf-8",
    )
    return adir


def test_cli_reset_primer_rebuilds_listed_agent(monkeypatch):
    home = _tmp()
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(home))
    adir = _seed_agent(home, "tester-0001")

    args = build_parser().parse_args(
        ["agent", "reset-primer", "tester-0001"],
    )
    assert args.func(args) == 0

    claude_md = (adir / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Puffo.ai platform primer" in claude_md
    assert "I test things." in claude_md
    # The shared primer was seeded as a side effect.
    assert (home / "docker" / "shared" / "CLAUDE.md").exists()


def test_cli_reset_primer_unknown_agent_returns_error(monkeypatch):
    home = _tmp()
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(home))

    args = build_parser().parse_args(
        ["agent", "reset-primer", "does-not-exist"],
    )
    # Shared primer still re-seeds; the unknown agent yields a non-zero rc.
    assert args.func(args) == 2
    assert (home / "docker" / "shared" / "CLAUDE.md").exists()
