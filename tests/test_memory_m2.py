"""M2 memory acceptance tests: the low-level MemoryStore file API —
logical-path validation (invalid path / traversal / symlink escape),
per-area scope rules, per-area size limits with structured errors,
atomic writes with changed status, bounded batch reads, body-less
status, and the briefing dirty/rebuild hook.

One test (or small group) per M2 validation scenario in
docs/user-lead-designs/memory-implementation.md, named after it.
"""

import json
from pathlib import Path

import pytest

from puffo_agent.agent.memory import (
    PER_FILE_LIMIT,
    TOTAL_LIMIT,
    BriefingCompileError,
    MemoryStoreError,
    ensure_memory_tree,
)
from puffo_agent.agent.memory_store import (
    NOTES_FILE_LIMIT,
    READ_BATCH_LIMIT,
    RECOLLECTION_FILE_LIMIT,
    MemoryStore,
)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    return root


@pytest.fixture
def store(root):
    return MemoryStore(root)


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _all_ops(store: MemoryStore, path: str):
    """Every primitive applied to one path, for reject-everywhere checks."""
    yield lambda: store.create_memory_file(path, "x")
    yield lambda: store.read_memory_file(path)
    yield lambda: store.patch_memory_file(path, [{"old_text": "a", "new_text": "b"}])
    yield lambda: store.append_memory_file(path, "x")
    yield lambda: store.delete_memory_file(path)
    yield lambda: store.get_memory_file_status(path)


# ── scenario 1: invalid logical path is rejected ─────────────────────


@pytest.mark.parametrize("bad", [
    "foo.md",                 # top-level, no memory area
    "briefing",               # a scope alone is not a file
    "",                       # empty
    "notes/.hidden.md",       # hidden segment
    "notes\\evil.md",         # backslash
    "notes//x.md",            # empty segment
    "notes/x.md/",            # trailing slash
    "briefing/sub/deep.md",   # briefing/ is flat (compile globs *.md)
    "notes/x\x00.md",         # NUL
])
def test_invalid_logical_path_is_rejected(store, bad):
    with pytest.raises(MemoryStoreError) as ei:
        store.create_memory_file(bad, "body")
    assert ei.value.code == "memory_invalid_path"
    assert ei.value.suggestion


def test_invalid_logical_path_rejected_for_reads_too(store):
    for op in _all_ops(store, "foo.md"):
        with pytest.raises(MemoryStoreError) as ei:
            op()
        assert ei.value.code == "memory_invalid_path"


def test_absolute_path_is_rejected(store):
    for bad in ("/etc/passwd", "C:\\evil.md"):
        with pytest.raises(MemoryStoreError) as ei:
            store.create_memory_file(bad, "body")
        assert ei.value.code == "memory_invalid_path"
    assert Path("/etc/passwd").exists()  # and it was never touched


def test_unknown_scope_is_out_of_scope(store):
    with pytest.raises(MemoryStoreError) as ei:
        store.create_memory_file("secrets/x.md", "body")
    assert ei.value.code == "memory_path_out_of_scope"
    assert ei.value.suggestion


def test_non_md_write_target_is_rejected(store, root):
    with pytest.raises(MemoryStoreError) as ei:
        store.create_memory_file("notes/plain.txt", "body")
    assert ei.value.code == "memory_invalid_path"
    # …but reads of non-.md files (e.g. under imports/) stay allowed.
    (root / "imports" / "blob.txt").write_text("raw", encoding="utf-8")
    assert store.read_memory_file("imports/blob.txt")["body"] == "raw"


# ── scenario 2: path traversal is rejected ───────────────────────────


@pytest.mark.parametrize("bad", [
    "notes/../briefing/profile.md",
    "../outside.md",
    "notes/../../etc/passwd",
    "./notes/x.md",
])
def test_path_traversal_is_rejected(store, bad):
    before = _tree_snapshot(store.memory_root)
    for op in _all_ops(store, bad):
        with pytest.raises(MemoryStoreError) as ei:
            op()
        assert ei.value.code == "memory_invalid_path"
    assert _tree_snapshot(store.memory_root) == before


def test_symlink_escape_is_rejected(tmp_path, root, store):
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (root / "notes" / "link.md").symlink_to(outside)
    for op in _all_ops(store, "notes/link.md"):
        with pytest.raises(MemoryStoreError) as ei:
            op()
        assert ei.value.code == "memory_invalid_path"
    assert outside.read_text(encoding="utf-8") == "secret"
    assert (root / "notes" / "link.md").is_symlink()  # left alone


def test_symlinked_directory_escape_is_rejected(tmp_path, root, store):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "notes" / "sub").symlink_to(elsewhere)
    with pytest.raises(MemoryStoreError) as ei:
        store.create_memory_file("notes/sub/x.md", "body")
    assert ei.value.code == "memory_invalid_path"
    assert list(elsewhere.iterdir()) == []  # nothing escaped the root


# ── scenario 3: create existing file is rejected ─────────────────────


def test_create_existing_file_is_rejected(store, root):
    store.create_memory_file("notes/topic.md", "one")
    with pytest.raises(MemoryStoreError) as ei:
        store.create_memory_file("notes/topic.md", "two")
    assert ei.value.code == "memory_file_exists"
    assert ei.value.suggestion
    assert (root / "notes" / "topic.md").read_text(encoding="utf-8") == "one"


# ── scenario 4: oversized create/append/patch is rejected ────────────


def _assert_oversize_shape(err: MemoryStoreError, path: str, limit: int):
    d = err.to_dict()
    assert d["code"] == "memory_file_too_large"
    assert d["path"] == path
    assert d["size"] > limit
    assert d["limit"] == limit
    assert d["suggestion"]


def test_oversized_create_is_rejected_per_area(root):
    cases = [
        ("briefing/big.md", PER_FILE_LIMIT, MemoryStore(root)),
        ("notes/big.md", NOTES_FILE_LIMIT, MemoryStore(root)),
        (
            "recollection/2026/07/2026-07-06.md",
            RECOLLECTION_FILE_LIMIT,
            MemoryStore(root, maintenance=True),
        ),
    ]
    for path, limit, store in cases:
        with pytest.raises(MemoryStoreError) as ei:
            store.create_memory_file(path, "x" * (limit + 1))
        _assert_oversize_shape(ei.value, path, limit)
        assert not root.joinpath(*path.split("/")).exists()
    # Briefing-scope size violations surface as BriefingCompileError so
    # M1 callers keep their error handling.
    with pytest.raises(BriefingCompileError):
        MemoryStore(root).create_memory_file(
            "briefing/big.md", "x" * (PER_FILE_LIMIT + 1),
        )


def test_oversized_append_is_rejected_and_file_unchanged(store, root):
    store.create_memory_file("notes/n.md", "keep")
    with pytest.raises(MemoryStoreError) as ei:
        store.append_memory_file("notes/n.md", "x" * NOTES_FILE_LIMIT)
    _assert_oversize_shape(ei.value, "notes/n.md", NOTES_FILE_LIMIT)
    assert (root / "notes" / "n.md").read_text(encoding="utf-8") == "keep"


def test_oversized_patch_is_rejected_and_file_unchanged(store, root):
    store.create_memory_file("notes/n.md", "small MARK end")
    with pytest.raises(MemoryStoreError) as ei:
        store.patch_memory_file("notes/n.md", [
            {"old_text": "MARK", "new_text": "y" * NOTES_FILE_LIMIT},
        ])
    _assert_oversize_shape(ei.value, "notes/n.md", NOTES_FILE_LIMIT)
    assert (root / "notes" / "n.md").read_text(encoding="utf-8") == "small MARK end"


def test_oversized_compiled_briefing_total_is_rejected(store, root):
    # 4 × 15KB topics: each under the per-file limit, so an extra 8KB
    # write busts only the 64KB compiled total.
    for i in range(4):
        (root / "briefing" / f"seed-{i}.md").write_text(
            "s" * (15 * 1024), encoding="utf-8",
        )
    with pytest.raises(BriefingCompileError) as ei:
        store.create_memory_file("briefing/overflow.md", "o" * (8 * 1024))
    d = ei.value.to_dict()
    assert d["code"] == "memory_briefing_too_large"
    assert d["path"] == "briefing/overflow.md"
    assert d["size"] > TOTAL_LIMIT
    assert d["limit"] == TOTAL_LIMIT
    assert d["suggestion"]
    assert not (root / "briefing" / "overflow.md").exists()


# ── scenarios 5 & 6: exact patch match counts ────────────────────────


def test_patch_with_zero_matches_is_rejected(store, root):
    store.create_memory_file("notes/n.md", "alpha beta")
    with pytest.raises(MemoryStoreError) as ei:
        store.patch_memory_file("notes/n.md", [
            {"old_text": "gamma", "new_text": "delta"},
        ])
    assert ei.value.code == "memory_patch_no_match"
    assert ei.value.suggestion
    assert (root / "notes" / "n.md").read_text(encoding="utf-8") == "alpha beta"


def test_patch_with_multiple_matches_is_rejected(store, root):
    store.create_memory_file("notes/n.md", "dup thing dup")
    with pytest.raises(MemoryStoreError) as ei:
        store.patch_memory_file("notes/n.md", [
            {"old_text": "dup", "new_text": "one"},
        ])
    assert ei.value.code == "memory_patch_multiple_matches"
    assert (root / "notes" / "n.md").read_text(encoding="utf-8") == "dup thing dup"


def test_patch_list_is_all_or_nothing(store, root):
    store.create_memory_file("notes/n.md", "alpha beta")
    with pytest.raises(MemoryStoreError) as ei:
        store.patch_memory_file("notes/n.md", [
            {"old_text": "alpha", "new_text": "ALPHA"},  # would apply…
            {"old_text": "missing", "new_text": "x"},    # …but this fails
        ])
    assert ei.value.code == "memory_patch_no_match"
    assert (root / "notes" / "n.md").read_text(encoding="utf-8") == "alpha beta"


# ── scenario 7: successful create/append/patch/delete are atomic ─────


def test_successful_create_append_patch_delete_return_changed_status(store, root):
    res = store.create_memory_file("notes/log.md", "one\n")
    assert res == {"path": "notes/log.md", "changed": True, "size": 4}

    res = store.append_memory_file("notes/log.md", "two\n")
    assert res == {"path": "notes/log.md", "changed": True, "size": 8}
    assert (root / "notes" / "log.md").read_text(encoding="utf-8") == "one\ntwo\n"

    res = store.patch_memory_file("notes/log.md", [
        {"old_text": "two", "new_text": "2"},
    ])
    assert res == {"path": "notes/log.md", "changed": True, "size": 6}
    assert (root / "notes" / "log.md").read_text(encoding="utf-8") == "one\n2\n"

    # A patch that reproduces the current content reports changed=False.
    res = store.patch_memory_file("notes/log.md", [
        {"old_text": "one", "new_text": "one"},
    ])
    assert res == {"path": "notes/log.md", "changed": False, "size": 6}

    res = store.delete_memory_file("notes/log.md")
    assert res == {"path": "notes/log.md", "changed": True}
    assert not (root / "notes" / "log.md").exists()

    # Atomic temp+rename leaves no litter anywhere in the tree.
    assert set(_tree_snapshot(root)) == {"imports/index.md"}


def test_delete_refuses_profile_and_imports(store, root):
    store.create_memory_file("briefing/profile.md", "# Me\n")
    with pytest.raises(MemoryStoreError) as ei:
        store.delete_memory_file("briefing/profile.md")
    assert ei.value.code == "memory_scope_readonly"
    assert (root / "briefing" / "profile.md").is_file()

    with pytest.raises(MemoryStoreError) as ei:
        store.delete_memory_file("imports/index.md")
    assert ei.value.code == "memory_scope_readonly"
    assert (root / "imports" / "index.md").is_file()


# ── scenario 8: read_memory_files is a pure bounded batch ────────────


def test_read_memory_files_returns_bounded_reads_without_modifying_memory(store, root):
    store.create_memory_file("notes/a.md", "note body")
    store.create_memory_file("briefing/b.md", "briefing body")
    # An over-limit file (placed out-of-band) proves reads are bounded.
    (root / "notes" / "huge.md").write_text(
        "h" * (NOTES_FILE_LIMIT + 512), encoding="utf-8",
    )
    before = _tree_snapshot(root)

    results = store.read_memory_files([
        "notes/a.md", "briefing/b.md", "notes/huge.md",
        "notes/missing.md", "foo.md",
    ])

    assert [r["path"] for r in results] == [
        "notes/a.md", "briefing/b.md", "notes/huge.md",
        "notes/missing.md", "foo.md",
    ]
    assert results[0]["body"] == "note body"
    assert results[0]["scope"] == "notes"
    assert results[0]["truncated"] is False
    assert results[1]["body"] == "briefing body"
    assert results[2]["truncated"] is True
    assert results[2]["size"] == NOTES_FILE_LIMIT + 512
    assert len(results[2]["body"].encode("utf-8")) == NOTES_FILE_LIMIT
    assert results[3]["error"]["code"] == "memory_file_not_found"
    assert results[4]["error"]["code"] == "memory_invalid_path"
    assert all("body" not in r for r in results[3:])

    # Pure read: the tree is byte-identical afterwards.
    assert _tree_snapshot(root) == before


def test_read_memory_files_batch_over_limit_is_rejected(store):
    with pytest.raises(MemoryStoreError) as ei:
        store.read_memory_files(
            [f"notes/{i}.md" for i in range(READ_BATCH_LIMIT + 1)]
        )
    assert ei.value.code == "memory_invalid_arguments"
    assert ei.value.suggestion


# ── scenario 9: get_memory_file_status has no body ───────────────────


def test_get_memory_file_status_reports_size_and_scope_without_body(store, root):
    store.create_memory_file("briefing/facts.md", "hello")
    st = store.get_memory_file_status("briefing/facts.md")
    assert st == {
        "path": "briefing/facts.md",
        "exists": True,
        "scope": "briefing",
        "size": 5,
        "limit": PER_FILE_LIMIT,
        "writable": True,
        "briefing_included": True,
    }
    assert "body" not in st

    st = store.get_memory_file_status("notes/absent.md")
    assert st["exists"] is False
    assert st["size"] is None
    assert st["limit"] == NOTES_FILE_LIMIT
    assert st["writable"] is True
    assert st["briefing_included"] is False

    st = store.get_memory_file_status("imports/index.md")
    assert st["exists"] is True
    assert st["scope"] == "imports"
    assert st["writable"] is False
    assert st["briefing_included"] is False

    path = "recollection/2026/07/2026-07-06.md"
    assert store.get_memory_file_status(path)["writable"] is False
    maint = MemoryStore(root, maintenance=True)
    assert maint.get_memory_file_status(path)["writable"] is True
    assert maint.get_memory_file_status(path)["limit"] == RECOLLECTION_FILE_LIMIT


# ── scenario 10: briefing/ writes trigger dirty/rebuild ──────────────


def _read_flag(workspace: Path) -> dict:
    flag = workspace / ".puffo-agent" / "refresh_agent.flag"
    assert flag.is_file()
    payload = json.loads(flag.read_text(encoding="utf-8"))
    flag.unlink()
    return payload


def test_briefing_writes_trigger_dirty_rebuild_flag(tmp_path):
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    workspace = tmp_path / "workspace"
    store = MemoryStore(root, workspace_dir=str(workspace))

    store.create_memory_file("briefing/topic.md", "line one\n")
    payload = _read_flag(workspace)
    assert payload["version"] == 1
    assert isinstance(payload["requested_at"], int)
    assert payload["reason"] == "memory_store.create:briefing/topic.md"

    store.patch_memory_file("briefing/topic.md", [
        {"old_text": "one", "new_text": "1"},
    ])
    assert _read_flag(workspace)["reason"] == "memory_store.patch:briefing/topic.md"

    store.append_memory_file("briefing/topic.md", "line two\n")
    assert _read_flag(workspace)["reason"] == "memory_store.append:briefing/topic.md"

    store.delete_memory_file("briefing/topic.md")
    assert _read_flag(workspace)["reason"] == "memory_store.delete:briefing/topic.md"

    # Non-briefing writes do not mark the briefing dirty…
    store.create_memory_file("notes/n.md", "note\n")
    assert not (workspace / ".puffo-agent" / "refresh_agent.flag").exists()

    # …and neither does a briefing no-op (changed=False).
    store.create_memory_file("briefing/topic.md", "body\n")
    _read_flag(workspace)
    res = store.patch_memory_file("briefing/topic.md", [
        {"old_text": "body", "new_text": "body"},
    ])
    assert res["changed"] is False
    assert not (workspace / ".puffo-agent" / "refresh_agent.flag").exists()


def test_store_without_workspace_dir_drops_no_flag(tmp_path):
    root = tmp_path / "memory"
    ensure_memory_tree(root)
    MemoryStore(root).create_memory_file("briefing/topic.md", "body\n")
    assert not list(tmp_path.rglob("refresh_agent.flag"))


# ── scope rules ──────────────────────────────────────────────────────


def test_recollection_write_requires_maintenance_scope(root):
    path = "recollection/2026/07/2026-07-07.md"
    with pytest.raises(MemoryStoreError) as ei:
        MemoryStore(root).create_memory_file(path, "daily digest\n")
    assert ei.value.code == "memory_scope_readonly"
    assert ei.value.suggestion
    assert not (root / "recollection" / "2026").exists()

    maint = MemoryStore(root, maintenance=True)
    res = maint.create_memory_file(path, "daily digest\n")
    assert res == {"path": path, "changed": True, "size": 13}
    physical = root / "recollection" / "2026" / "07" / "2026-07-07.md"
    assert physical.read_text(encoding="utf-8") == "daily digest\n"
    # Reads never need the maintenance scope.
    assert MemoryStore(root).read_memory_file(path)["body"] == "daily digest\n"


def test_imports_scope_is_read_only(store, root):
    index_before = (root / "imports" / "index.md").read_bytes()
    write_ops = [
        lambda: store.create_memory_file("imports/new.md", "x"),
        lambda: store.patch_memory_file(
            "imports/index.md", [{"old_text": "a", "new_text": "b"}],
        ),
        lambda: store.append_memory_file("imports/index.md", "x"),
        lambda: store.delete_memory_file("imports/index.md"),
    ]
    for op in write_ops:
        with pytest.raises(MemoryStoreError) as ei:
            op()
        assert ei.value.code == "memory_scope_readonly"
        assert ei.value.suggestion
    assert (root / "imports" / "index.md").read_bytes() == index_before
    assert not (root / "imports" / "new.md").exists()
    # Maintenance scope does not unlock imports either.
    with pytest.raises(MemoryStoreError) as ei:
        MemoryStore(root, maintenance=True).create_memory_file(
            "imports/new.md", "x",
        )
    assert ei.value.code == "memory_scope_readonly"
