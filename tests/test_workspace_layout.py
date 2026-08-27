from puffo_agent.portal.workspace_layout import (
    ensure_workspace_shared_link,
    prepare_workspace_shared_access,
)


def test_shared_workspace_link_is_common_and_preserves_conflicts(tmp_path):
    shared = tmp_path / "shared"
    alice = tmp_path / "agents" / "alice" / "workspace"
    bob = tmp_path / "agents" / "bob" / "workspace"

    assert ensure_workspace_shared_link(alice, shared) == "created"
    assert ensure_workspace_shared_link(bob, shared) == "created"
    assert ensure_workspace_shared_link(alice, shared) == "existing"
    (alice / "shared" / "handoff.txt").write_text("ready", encoding="utf-8")
    assert (bob / "shared" / "handoff.txt").read_text(encoding="utf-8") == "ready"

    carol = tmp_path / "agents" / "carol" / "workspace"
    local_shared = carol / "shared"
    local_shared.mkdir(parents=True)
    (local_shared / "keep.txt").write_text("keep", encoding="utf-8")
    assert ensure_workspace_shared_link(carol, shared) == "conflict"
    assert (local_shared / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert prepare_workspace_shared_access(
        carol, shared, mounted=True
    ) == "conflict"

    dave = tmp_path / "agents" / "dave" / "workspace"
    (dave / "shared").mkdir(parents=True)
    assert ensure_workspace_shared_link(dave, shared) == "created"
    assert (dave / "shared").is_symlink()


def test_shared_workspace_repairs_stale_and_dangling_links(tmp_path):
    shared = tmp_path / "shared"
    stale_target = tmp_path / "old-shared"
    stale_target.mkdir()

    for name, target in (("stale", stale_target), ("dangling", tmp_path / "gone")):
        workspace = tmp_path / "agents" / name / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "shared").symlink_to(target, target_is_directory=True)

        assert ensure_workspace_shared_link(workspace, shared) == "created"
        assert (workspace / "shared").resolve() == shared.resolve()
