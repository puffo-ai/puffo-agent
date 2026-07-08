"""M1 memory acceptance tests: memory tree, bounded briefing compile
(fail closed), managed profile briefing, flat-memory migration, and
the MemoryManager compat surface (save → briefing topic + refresh
flag)."""

import json
import re
from pathlib import Path

import pytest

from puffo_agent.agent.memory import (
    PER_FILE_LIMIT,
    TOTAL_LIMIT,
    BriefingCompileError,
    MemoryManager,
    compile_briefing,
    ensure_memory_tree,
    migrate_flat_memory,
    render_profile_briefing,
    sync_profile_briefing,
)
from puffo_agent.agent.shared_content import rebuild_agent_claude_md


# ── tree creation ────────────────────────────────────────────────────


def test_ensure_memory_tree_creates_layout(tmp_path):
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    assert (root / "briefing").is_dir()
    assert (root / "notes").is_dir()
    assert (root / "recollection").is_dir()
    assert (root / "imports" / "index.md").is_file()
    # Exactly the M1 tree, and every created path is inside the root.
    created = {
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")
    }
    assert created == {
        "memory",
        "memory/briefing",
        "memory/notes",
        "memory/recollection",
        "memory/imports",
        "memory/imports/index.md",
    }


def test_memory_manager_init_seeds_tree(tmp_path):
    root = tmp_path / "memory"
    MemoryManager(str(root))
    assert (root / "briefing").is_dir()
    assert (root / "notes").is_dir()
    assert (root / "recollection").is_dir()
    assert (root / "imports" / "index.md").is_file()


def test_ensure_memory_tree_is_idempotent_and_preserves_content(tmp_path):
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    (root / "briefing" / "facts.md").write_text("kept", encoding="utf-8")
    (root / "imports" / "index.md").write_text("edited", encoding="utf-8")
    ensure_memory_tree(root)
    assert (root / "briefing" / "facts.md").read_text(encoding="utf-8") == "kept"
    assert (root / "imports" / "index.md").read_text(encoding="utf-8") == "edited"


# ── bounded compile, fail closed ─────────────────────────────────────


def test_compile_profile_first_then_sorted_topics(tmp_path):
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    (root / "briefing" / "profile.md").write_text(
        "# Ada\n\nidentity framing", encoding="utf-8",
    )
    (root / "briefing" / "zeta.md").write_text("zeta body", encoding="utf-8")
    (root / "briefing" / "alpha.md").write_text("alpha body", encoding="utf-8")
    (root / "notes" / "hidden.md").write_text("never injected", encoding="utf-8")
    out = compile_briefing(root)
    assert out.startswith("# Ada")
    assert out.index("### alpha") < out.index("### zeta")
    assert "never injected" not in out


def test_compile_empty_briefing_is_empty_string(tmp_path):
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    assert compile_briefing(root) == ""


def test_compile_rejects_oversized_file(tmp_path):
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    big = root / "briefing" / "big.md"
    big.write_text("x" * (PER_FILE_LIMIT + 1), encoding="utf-8")
    with pytest.raises(BriefingCompileError) as ei:
        compile_briefing(root)
    err = ei.value
    assert err.code == "memory_file_too_large"
    assert err.path == str(big)
    assert err.size == PER_FILE_LIMIT + 1
    assert err.limit == PER_FILE_LIMIT
    assert err.suggestion
    assert err.to_dict()["code"] == "memory_file_too_large"


def test_compile_rejects_oversized_total(tmp_path):
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    # 5 × 15KB topics: each under the per-file limit, 75KB total over
    # the 64KB budget.
    for i in range(5):
        (root / "briefing" / f"topic-{i}.md").write_text(
            "y" * (15 * 1024), encoding="utf-8",
        )
    with pytest.raises(BriefingCompileError) as ei:
        compile_briefing(root)
    err = ei.value
    assert err.code == "memory_briefing_too_large"
    assert err.path == str(root / "briefing")
    assert err.size > TOTAL_LIMIT
    assert err.limit == TOTAL_LIMIT


# ── briefing/profile.md content ──────────────────────────────────────


def test_profile_briefing_has_identity_and_no_instructions(tmp_path):
    root = tmp_path / "memory"
    path = sync_profile_briefing(
        root,
        agent_id="ada-0001",
        display_name="Ada",
        role="Research helper for the ops team",
        role_short="Research",
        soul="Curious and kind.",
    )
    assert path == root / "briefing" / "profile.md"
    text = path.read_text(encoding="utf-8")
    assert "Ada" in text
    assert "Research helper for the ops team" in text
    assert "Curious and kind." in text
    assert re.search(r"^#{1,6}\s*Instructions", text, re.MULTILINE) is None


def test_profile_briefing_minimal_default_with_empty_fields(tmp_path):
    root = tmp_path / "memory"
    path = sync_profile_briefing(root, agent_id="bot-42")
    text = path.read_text(encoding="utf-8")
    assert "You are bot-42 (agent `bot-42`)." in text
    # Fully empty still renders a usable identity line.
    assert "You are agent." in render_profile_briefing()


def test_profile_briefing_resync_preserves_user_text_outside_markers(tmp_path):
    root = tmp_path / "memory"
    path = sync_profile_briefing(
        root, agent_id="ada-0001", display_name="Ada", role="Old role",
    )
    path.write_text(
        path.read_text(encoding="utf-8") + "\nUser-authored addendum.\n",
        encoding="utf-8",
    )
    sync_profile_briefing(
        root, agent_id="ada-0001", display_name="Ada", role="New role",
    )
    text = path.read_text(encoding="utf-8")
    assert "New role" in text
    assert "Old role" not in text
    assert "User-authored addendum." in text


# ── legacy flat-memory migration ─────────────────────────────────────


def _tree_snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_migrate_flat_memory_small_to_briefing_big_to_notes(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    (root / "small.md").write_text("a small fact", encoding="utf-8")
    (root / "big.md").write_text("x" * (PER_FILE_LIMIT + 1), encoding="utf-8")
    (root / "README.md").write_text("readme stays", encoding="utf-8")

    moved = migrate_flat_memory(root)

    assert (root / "briefing" / "small.md").read_text(encoding="utf-8") == "a small fact"
    assert (root / "notes" / "big.md").is_file()
    pointer = (root / "briefing" / "migrated-notes.md").read_text(encoding="utf-8")
    assert "- notes/big.md — migrated from flat memory" in pointer
    # No flat *.md remain except README.md.
    assert [p.name for p in root.glob("*.md")] == ["README.md"]
    assert (root / "README.md").read_text(encoding="utf-8") == "readme stays"
    assert set(moved) == {"briefing/small.md", "notes/big.md"}
    # The migrated tree still compiles within budget.
    assert "a small fact" in compile_briefing(root)

    # Idempotent: a second pass is a no-op.
    before = _tree_snapshot(root)
    assert migrate_flat_memory(root) == []
    assert _tree_snapshot(root) == before


def test_migrate_flat_memory_over_total_budget_goes_to_notes(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    ensure_memory_tree(root)
    # Pre-existing briefing takes 60KB of the 64KB budget…
    for i in range(4):
        (root / "briefing" / f"seed-{i}.md").write_text(
            "s" * (15 * 1024), encoding="utf-8",
        )
    # …so a 10KB flat file (fine per-file) no longer fits the total.
    (root / "overflow.md").write_text("o" * (10 * 1024), encoding="utf-8")

    moved = migrate_flat_memory(root)

    assert moved == ["notes/overflow.md"]
    assert (root / "notes" / "overflow.md").is_file()
    assert not (root / "briefing" / "overflow.md").exists()
    assert "- notes/overflow.md — migrated from flat memory" in (
        root / "briefing" / "migrated-notes.md"
    ).read_text(encoding="utf-8")
    compile_briefing(root)  # still within budget after migration


# ── symlink injection (host-side compile & migration) ────────────────


def test_compile_excludes_symlinked_briefing_topic_outside_root(tmp_path):
    """A symlinked briefing topic pointing at a file OUTSIDE the memory
    root must never fold that file's bytes into the compiled prompt."""
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    (root / "briefing" / "real.md").write_text("real fact", encoding="utf-8")
    secret = tmp_path / "outside-secret.md"
    secret.write_text("SECRET-OUTSIDE-CONTENT", encoding="utf-8")
    (root / "briefing" / "evil.md").symlink_to(secret)

    out = compile_briefing(root)  # succeeds, does not raise

    assert "real fact" in out
    assert "SECRET-OUTSIDE-CONTENT" not in out
    # The symlink is left on disk untouched, just never compiled.
    assert (root / "briefing" / "evil.md").is_symlink()
    assert secret.read_text(encoding="utf-8") == "SECRET-OUTSIDE-CONTENT"


def test_compile_excludes_symlinked_briefing_topic_inside_root(tmp_path):
    """Even a symlink whose target sits inside the root is excluded —
    briefing topics are never followed through symlinks."""
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    (root / "notes" / "target.md").write_text(
        "NOTE-TARGET-BYTES", encoding="utf-8",
    )
    (root / "briefing" / "link.md").symlink_to(root / "notes" / "target.md")

    out = compile_briefing(root)

    assert "NOTE-TARGET-BYTES" not in out


def test_compile_excludes_topic_reached_through_symlinked_briefing_dir(tmp_path):
    """A real topic file that resolves outside the root through a
    symlinked briefing/ dir is dropped by the resolve() containment
    check — exercising the guard beyond leaf symlinks."""
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    external = tmp_path / "external-briefing"
    external.mkdir()
    (external / "leak.md").write_text("EXTERNAL-LEAK-BYTES", encoding="utf-8")
    briefing = root / "briefing"
    briefing.rmdir()  # ensure_memory_tree left it empty
    briefing.symlink_to(external)

    out = compile_briefing(root)  # succeeds, does not raise

    assert "EXTERNAL-LEAK-BYTES" not in out


def test_migrate_flat_memory_skips_symlinked_flat_file(tmp_path):
    """A symlinked flat *.md is never read or migrated — its target's
    bytes must not enter the tree."""
    root = tmp_path / "memory"
    root.mkdir()
    secret = tmp_path / "host-secret.md"
    secret.write_text("HOST-SECRET-BYTES", encoding="utf-8")
    (root / "evil.md").symlink_to(secret)
    (root / "real.md").write_text("real flat fact", encoding="utf-8")

    moved = migrate_flat_memory(root)  # does not raise

    # The real flat file migrated; the symlink was skipped entirely.
    assert "briefing/real.md" in moved
    assert not any("evil" in m for m in moved)
    # The symlink is left in place; its target was never read or moved.
    assert (root / "evil.md").is_symlink()
    assert secret.read_text(encoding="utf-8") == "HOST-SECRET-BYTES"
    # No regular file in the tree carries the secret bytes.
    for p in root.rglob("*.md"):
        if p.is_symlink():
            continue
        assert "HOST-SECRET-BYTES" not in p.read_text(encoding="utf-8")


def test_migrate_pointer_skipped_when_briefing_budget_full(tmp_path):
    """When the briefing is so full that even the migrated-notes pointer
    line would bust the total, the pointer is skipped (fail closed) — the
    note still migrates and the result still compiles within budget."""
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    # Four ~16KB topics compile to ~10 bytes under the 64KB total,
    # leaving no room for either the flat note or a pointer topic.
    for i in range(4):
        (root / "briefing" / f"seed-{i}.md").write_text(
            "s" * 16368, encoding="utf-8",
        )
    assert len(compile_briefing(root).encode("utf-8")) <= TOTAL_LIMIT
    # A small flat note can't fit briefing (total is full) → notes/.
    (root / "overflow.md").write_text("overflow note body\n", encoding="utf-8")

    moved = migrate_flat_memory(root)  # does not raise

    assert moved == ["notes/overflow.md"]
    assert (root / "notes" / "overflow.md").is_file()
    # The pointer write was rejected by the store's budget check before
    # any file was written, so no migrated-notes topic exists…
    assert not (root / "briefing" / "migrated-notes.md").exists()
    # …and the migrated tree still compiles within the total budget.
    compiled = compile_briefing(root)  # must not raise
    assert len(compiled.encode("utf-8")) <= TOTAL_LIMIT


def test_migrate_flat_memory_name_collision_falls_back(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    ensure_memory_tree(root)
    (root / "briefing" / "facts.md").write_text("already here", encoding="utf-8")
    (root / "notes" / "facts.md").write_text("also here", encoding="utf-8")
    (root / "facts.md").write_text("flat version", encoding="utf-8")

    moved = migrate_flat_memory(root)

    assert moved == ["notes/facts-migrated.md"]
    assert (root / "notes" / "facts-migrated.md").read_text(encoding="utf-8") == "flat version"
    assert (root / "briefing" / "facts.md").read_text(encoding="utf-8") == "already here"
    assert (root / "notes" / "facts.md").read_text(encoding="utf-8") == "also here"


# ── briefing change → artifact rebuilt ───────────────────────────────


def _rebuild(root: Path) -> str:
    return rebuild_agent_claude_md(
        shared_dir=root / "shared",
        profile_path=root / "profile.md",
        memory_dir=root / "memory",
        workspace_dir=root / "workspace",
        claude_user_dir=root / ".claude",
        gemini_user_dir=root / ".gemini",
        agent_id="tester-0001",
        display_name="Tester",
        role="Tests the memory tree",
        role_short="Tester",
    )


def test_rebuild_picks_up_new_briefing_topic_and_ignores_notes(tmp_path):
    (tmp_path / "profile.md").write_text(
        "# Soul\nI verify briefings.", encoding="utf-8",
    )
    (tmp_path / "workspace").mkdir()
    memory = tmp_path / "memory"

    first = _rebuild(tmp_path)
    assert "I verify briefings." in first

    (memory / "briefing" / "deploys.md").write_text(
        "Deploy fact ZQX-77.", encoding="utf-8",
    )
    (memory / "notes" / "scratch.md").write_text(
        "NOTE-ONLY-DETAIL-42", encoding="utf-8",
    )
    second = _rebuild(tmp_path)
    assert "Deploy fact ZQX-77." in second
    assert "NOTE-ONLY-DETAIL-42" not in second
    assert (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == second
    assert (tmp_path / ".gemini" / "GEMINI.md").read_text(encoding="utf-8") == second


def test_rebuild_flag_dropped_by_memory_manager_save(tmp_path):
    workspace = tmp_path / "workspace"
    mm = MemoryManager(str(tmp_path / "memory"), workspace_dir=str(workspace))
    mm.save("deploy notes", "The staging box is flaky.")
    flag = workspace / ".puffo-agent" / "refresh_agent.flag"
    assert flag.is_file()
    payload = json.loads(flag.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["reason"].startswith("memory.save:")
    assert isinstance(payload["requested_at"], int)


# ── MemoryManager.save() compat ──────────────────────────────────────


def test_save_writes_briefing_topic_with_frontmatter(tmp_path):
    root = tmp_path / "memory"
    mm = MemoryManager(str(root))
    mm.save("deploy notes/prod", "The fact.")
    path = root / "briefing" / "deploy_notes-prod.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\ntopic: deploy notes/prod\nupdated: ")
    assert text.endswith("The fact.\n")
    assert "The fact." in mm.get_context()


def test_save_oversized_file_fails_closed_without_partial_state(tmp_path):
    root = tmp_path / "memory"
    mm = MemoryManager(str(root))
    with pytest.raises(BriefingCompileError) as ei:
        mm.save("big", "x" * (PER_FILE_LIMIT + 1))
    assert ei.value.code == "memory_file_too_large"
    assert not (root / "briefing" / "big.md").exists()


def test_save_over_total_budget_fails_closed_without_partial_state(tmp_path):
    root = tmp_path / "memory"
    mm = MemoryManager(str(root))
    for i in range(4):
        (root / "briefing" / f"seed-{i}.md").write_text(
            "s" * (15 * 1024), encoding="utf-8",
        )
    with pytest.raises(BriefingCompileError) as ei:
        mm.save("overflow", "o" * (8 * 1024))
    assert ei.value.code == "memory_briefing_too_large"
    assert not (root / "briefing" / "overflow.md").exists()


def test_save_without_workspace_dir_drops_no_flag(tmp_path):
    root = tmp_path / "memory"
    mm = MemoryManager(str(root))
    mm.save("topic", "content")
    assert not list(tmp_path.rglob("refresh_agent.flag"))
