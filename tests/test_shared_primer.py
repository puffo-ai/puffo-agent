import tempfile
from pathlib import Path

from puffo_agent.agent.shared_content import (
    DEFAULT_SHARED_CLAUDE_MD,
    ensure_shared_primer,
    rebuild_agent_claude_md,
    rebuild_agent_codex_md,
    sync_shared_skills_codex,
)
from puffo_agent.portal.cli import build_parser


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


# ── ensure_shared_primer ────────────────────────────────────────────


def test_ensure_creates_on_fresh_dir():
    shared = _tmp() / "shared"
    actions = ensure_shared_primer(shared)
    assert actions, "expected managed files to be reported"
    assert all(action == "created" for _, action in actions)
    assert (
        (shared / "CLAUDE.md").read_text(encoding="utf-8")
        == DEFAULT_SHARED_CLAUDE_MD
    )
    assert any(rel.startswith("skills/") for rel, _ in actions)


def test_ensure_is_noop_when_already_current():
    shared = _tmp() / "shared"
    ensure_shared_primer(shared)
    actions = ensure_shared_primer(shared)
    assert all(action == "unchanged" for _, action in actions)


def test_ensure_overwrites_stale_content_without_backup():
    """No operator-edit protection — stale content is replaced in
    place. No ``.bak`` is written."""
    shared = _tmp() / "shared"
    ensure_shared_primer(shared)
    primer = shared / "CLAUDE.md"
    primer.write_text("stale content", encoding="utf-8")

    by_rel = dict(ensure_shared_primer(shared))
    assert by_rel["CLAUDE.md"] == "updated"
    assert by_rel["README.md"] == "unchanged"
    assert primer.read_text(encoding="utf-8") == DEFAULT_SHARED_CLAUDE_MD
    assert not (shared / "CLAUDE.md.bak").exists()


def test_ensure_prunes_stale_managed_skills():
    """A managed skill dir whose id is no longer in ``DEFAULT_SKILLS``
    is deleted on the next sync. Operator-authored dirs (no
    ``.puffo-managed`` marker) are preserved."""
    from puffo_agent.agent.shared_content import _MANAGED_MARKER

    shared = _tmp() / "shared"
    ensure_shared_primer(shared)

    stale = shared / "skills" / "removed-in-v2"
    stale.mkdir()
    (stale / "SKILL.md").write_text("old body", encoding="utf-8")
    (stale / _MANAGED_MARKER).write_text("m", encoding="utf-8")

    custom = shared / "skills" / "operator-authored"
    custom.mkdir()
    (custom / "SKILL.md").write_text("keep me", encoding="utf-8")

    by_rel = dict(ensure_shared_primer(shared))
    assert by_rel.get("skills/removed-in-v2") == "pruned"
    assert not stale.exists()
    assert custom.exists()
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "keep me"


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


# ── codex variants strip mcp__puffo__ prefix ───────────────────────


def test_rebuild_agent_codex_md_strips_mcp_puffo_prefix():
    """Codex's tool router dispatches MCP tools under bare names —
    the LLM must NOT see ``mcp__puffo__`` in its instructions or it
    will generate calls the router rejects with ``unsupported``."""
    root = _tmp()
    shared = root / "shared"
    profile = root / "profile.md"
    profile.write_text("# Soul\nI am codex.", encoding="utf-8")
    memory = root / "memory"
    memory.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    codex_user = root / ".codex"

    out = rebuild_agent_codex_md(
        shared_dir=shared,
        profile_path=profile,
        memory_dir=memory,
        workspace_dir=workspace,
        codex_user_dir=codex_user,
    )

    # Primer originally references the prefix; codex variant must
    # not.
    assert "mcp__puffo__" not in out
    # Bare tool names still present so the LLM sees what to call.
    assert "send_message" in out
    # File written.
    assert (codex_user / "AGENTS.md").read_text(encoding="utf-8") == out


def test_sync_shared_skills_codex_strips_prefix_in_skill_bodies():
    """Skill bodies mirror the same convention as the primer —
    codex needs them prefix-free."""
    root = _tmp()
    shared = root / "shared"
    workspace = root / "workspace"
    workspace.mkdir()

    # ensure_shared_primer populates shared/skills/<id>/SKILL.md with
    # the DEFAULT_SKILLS bodies (which include mcp__puffo__ refs).
    ensure_shared_primer(shared)

    sync_shared_skills_codex(shared, workspace)

    skills_root = workspace / ".agents" / "skills"
    assert skills_root.is_dir(), "skills dir should be created"
    found_skill_md = False
    for skill_md in skills_root.glob("*/SKILL.md"):
        found_skill_md = True
        body = skill_md.read_text(encoding="utf-8")
        assert "mcp__puffo__" not in body, (
            f"{skill_md} still carries mcp__puffo__ prefix — codex "
            f"router would reject calls generated against it"
        )
    assert found_skill_md, "no SKILL.md files materialised"


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
