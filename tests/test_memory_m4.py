"""M4 memory acceptance tests: the read-only recall + observability MCP
tools layered on the M2 store and the M3 local git audit — status
(``get_memory_status`` / ``get_memory_file_status``), recall
(``list_memory_files``), and history (``get_memory_history_status`` /
``get_memory_history``, a bounded audit query that is NOT a ``git log``
passthrough). M4 adds no write, rollback, or generic git surface.

One test (or small group) per §M4 validation scenario in
docs/user-lead-designs/memory-implementation.md, named after it.
"""

import json
import subprocess

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from puffo_agent.agent import memory_git
from puffo_agent.agent.memory import (
    TOTAL_LIMIT,
    _byte_size,
    compile_briefing,
    ensure_memory_tree,
)
from puffo_agent.agent.memory_errors import MemoryHistoryError
from puffo_agent.agent.memory_git import (
    HISTORY_DIFF_BYTE_CAP,
    HISTORY_DIFF_MAX_LIMIT,
    HISTORY_MAX_LIMIT,
)
from puffo_agent.agent.memory_store import LIST_MAX_LIMIT
from puffo_agent.mcp.memory_tools import (
    MemoryToolsConfig,
    register_memory_tools,
)

# The five read-only tools M4 adds. All are agent-facing (unlike the
# maintenance-only append_recollection).
M4_TOOLS = {
    "get_memory_status",
    "get_memory_file_status",
    "list_memory_files",
    "get_memory_history_status",
    "get_memory_history",
}


@pytest.fixture
def root(tmp_path):
    # M4 read tools never ensure the tree/repo; tests that need one seed
    # it via the M3 write tools (which do).
    return tmp_path / "memory"


def _build(root, workspace="", maintenance=False):
    mcp = FastMCP("test")
    register_memory_tools(mcp, MemoryToolsConfig(
        memory_root=str(root),
        workspace=str(workspace) if workspace else "",
        maintenance=maintenance,
    ))
    return mcp


async def _call(mcp, name, args):
    result = await mcp.call_tool(name, args)
    text = "".join(getattr(item, "text", "") for item in result)
    return json.loads(text)


async def _call_err(mcp, name, args):
    """Call a tool expecting failure; return the structured M4 error
    envelope extracted from the raised tool error's JSON text."""
    with pytest.raises(ToolError) as ei:
        await mcp.call_tool(name, args)
    text = str(ei.value)
    payload = json.loads(text[text.index("{"):])
    assert payload["ok"] is False
    return payload["error"]


def _tree_snapshot(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _scope_stats(root, scope):
    """Independent (files, total_size_bytes) recomputation for a scope —
    mirrors the store's walk so status numbers are checked, not trusted."""
    base = root / scope
    n = 0
    total = 0
    if base.is_dir():
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if any(seg.startswith(".") for seg in rel.split("/")):
                continue
            if p.is_symlink() or not p.is_file():
                continue
            n += 1
            total += p.stat().st_size
    return n, total


# ── registration: the five M4 tools register on the real server ──────


@pytest.mark.asyncio
async def test_m4_tools_registered_on_built_server(root, tmp_path):
    from puffo_agent.mcp.puffo_core_server import build_server

    server = build_server(
        slug="agent-0001",
        device_id="dev_test",
        server_url="http://localhost:3000",
        space_id="",
        keystore_dir=str(tmp_path / "keys"),
        workspace=str(tmp_path / "workspace"),
        agent_id="agent-0001",
        data_service_url="http://127.0.0.1:1",
        memory_dir=str(root),
    )
    names = {t.name for t in await server.list_tools()}
    assert M4_TOOLS <= names
    # Building the server has no side effects: the memory tree and its
    # git repo are ensured lazily on first WRITE, never at build time
    # and never by an M4 read.
    assert not root.exists()


# ── M4 reads are side-effect-free (never create the tree/repo) ───────


@pytest.mark.asyncio
async def test_m4_reads_never_create_tree_or_repo(root):
    mcp = _build(root)
    status = await _call(mcp, "get_memory_status", {})
    assert status["ok"] is True
    assert status["root_exists"] is False
    assert status["git_enabled"] is False
    assert all(v["files"] == 0 for v in status["scopes"].values())

    fs = await _call(mcp, "get_memory_file_status", {"path": "notes/x.md"})
    assert fs["exists"] is False
    assert fs["git_tracked"] is None

    lst = await _call(mcp, "list_memory_files", {})
    assert lst["files"] == [] and lst["truncated"] is False

    # A history read on an uninitialised root reports the truth rather
    # than materialising a repo (this is what makes
    # memory_history_not_initialized reachable).
    err = await _call_err(mcp, "get_memory_history_status", {})
    assert err["code"] == "memory_history_not_initialized"

    assert not root.exists()


# ── scenario: get_memory_status dirty / rebuild / reload ─────────────


@pytest.mark.asyncio
async def test_get_memory_status_dirty_rebuild_reload(root, tmp_path):
    workspace = tmp_path / "workspace"
    mcp = _build(root, workspace=workspace)

    # Clean tree, no briefing change pending: dirty/reload both False,
    # and the budget fields are present.
    status = await _call(mcp, "get_memory_status", {})
    assert status["briefing"]["dirty"] is False
    assert status["briefing"]["provider_reload_required"] is False
    assert status["briefing"]["limit_bytes"] == TOTAL_LIMIT
    assert status["briefing"]["over_budget"] is False

    # A note write does not touch the briefing → still not dirty.
    await _call(mcp, "create_note", {"name": "n1", "body": "hello\n"})
    await _call(mcp, "create_note", {"name": "n2", "body": "world!\n"})
    status = await _call(mcp, "get_memory_status", {})
    assert status["briefing"]["dirty"] is False
    assert status["scopes"]["notes"]["files"] == 2

    # A briefing write flips dirty + provider_reload_required and drops
    # the refresh flag under the real workspace.
    await _call(mcp, "create_briefing_topic", {"name": "prefs", "body": "pb\n"})
    status = await _call(mcp, "get_memory_status", {})
    assert status["briefing"]["dirty"] is True
    assert status["briefing"]["provider_reload_required"] is True
    assert (workspace / ".puffo-agent" / "refresh_agent.flag").is_file()

    # git is available and the audit repo now exists.
    assert status["git_enabled"] is True

    # Compiled size + per-scope numbers are numerically correct for the
    # seeded tree.
    assert status["briefing"]["compiled_size_bytes"] == _byte_size(
        compile_briefing(root)
    )
    for scope in ("briefing", "notes", "recollection", "imports"):
        n, total = _scope_stats(root, scope)
        assert status["scopes"][scope]["files"] == n
        assert status["scopes"][scope]["total_size_bytes"] == total
    assert status["scopes"]["briefing"]["files"] == 1
    assert status["scopes"]["imports"]["files"] == 1
    assert status["scopes"]["recollection"]["files"] == 0


# ── scenario: get_memory_file_status has size/limit and NO body ──────


@pytest.mark.asyncio
async def test_get_memory_file_status_size_limit_no_body(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {
        "name": "topic", "body": "content\n", "reason": "seeded",
    })

    fs = await _call(mcp, "get_memory_file_status", {"path": "notes/topic.md"})
    assert "body" not in fs
    for key in ("size", "limit", "scope", "exists", "briefing_included"):
        assert key in fs
    assert fs["exists"] is True
    assert fs["scope"] == "notes"
    # A committed write reports non-null last-change git metadata.
    assert fs["git_tracked"] is True
    assert fs["last_changed_commit_id"]
    assert fs["last_changed_at"]

    # A never-written path exists=False with null last-change fields.
    fs2 = await _call(mcp, "get_memory_file_status", {"path": "notes/nope.md"})
    assert "body" not in fs2
    assert fs2["exists"] is False
    assert fs2["git_tracked"] is False
    assert fs2["last_changed_commit_id"] is None
    assert fs2["last_changed_at"] is None


# ── scenario: list_memory_files bounded metadata by scope, no bodies ─


@pytest.mark.asyncio
async def test_list_memory_files_bounded_metadata_by_scope(root):
    # A maintenance server can seed recollection/ too, so all four scopes
    # carry files.
    maint = _build(root, maintenance=True)
    await _call(maint, "create_note", {"name": "a", "body": "note a\n"})
    await _call(maint, "create_briefing_topic", {"name": "b", "body": "b\n"})
    await _call(maint, "append_recollection", {
        "date": "2026-07-06", "text": "an entry",
    })
    # imports/index.md exists from ensure_memory_tree.

    lst = await _call(maint, "list_memory_files", {})
    assert lst["ok"] is True
    paths = [f["path"] for f in lst["files"]]
    for f in lst["files"]:
        assert set(f) == {
            "path", "scope", "size", "writable", "briefing_included",
        }
        assert "body" not in f
    assert "notes/a.md" in paths
    assert "briefing/b.md" in paths
    assert any(p.startswith("recollection/") for p in paths)
    assert "imports/index.md" in paths

    # scope filter returns only that scope.
    notes_only = await _call(maint, "list_memory_files", {"scope": "notes"})
    assert [f["path"] for f in notes_only["files"]] == ["notes/a.md"]
    assert all(f["scope"] == "notes" for f in notes_only["files"])

    # limit=1 bounds to one entry and flags truncated.
    one = await _call(maint, "list_memory_files", {"limit": 1})
    assert len(one["files"]) == 1
    assert one["truncated"] is True

    # writable / briefing_included are set per-scope.
    by_path = {f["path"]: f for f in lst["files"]}
    assert by_path["briefing/b.md"]["briefing_included"] is True
    assert by_path["briefing/b.md"]["writable"] is True
    assert by_path["imports/index.md"]["writable"] is False
    assert by_path["imports/index.md"]["briefing_included"] is False


@pytest.mark.asyncio
async def test_list_memory_files_rejects_bad_scope_and_limit(root):
    mcp = _build(root)
    err = await _call_err(mcp, "list_memory_files", {"scope": "bogus"})
    assert err["code"] == "memory_path_out_of_scope"
    err = await _call_err(mcp, "list_memory_files", {"limit": 0})
    assert err["code"] == "memory_invalid_arguments"


# ── scenario: search_memory / search_imports unchanged and read-only ─


@pytest.mark.asyncio
async def test_search_tools_unchanged_and_read_only(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {"name": "n", "body": "needle here\n"})
    ensure_memory_tree(root)
    (root / "imports" / "doc.txt").write_text(
        "imported needle\n", encoding="utf-8",
    )
    before = _tree_snapshot(root)

    res = await _call(mcp, "search_memory", {"query": "needle"})
    assert res["results"]
    assert all("path" in m and "scope" in m for m in res["results"])
    assert all(len(m["snippet"]) <= 200 for m in res["results"])

    imp = await _call(mcp, "search_imports", {"query": "needle"})
    assert imp["results"]
    assert all(m["path"].startswith("imports/") for m in imp["results"])

    # Search never writes: tree bytes are unchanged.
    assert _tree_snapshot(root) == before


# ── scenario: get_memory_history_status health + tracked path ────────


@pytest.mark.asyncio
async def test_get_memory_history_status_health_and_tracked_path(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {"name": "a", "body": "x\n"})
    # Commit the seeded imports/index.md too so the work tree is clean.
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "seed imports")

    hs = await _call(mcp, "get_memory_history_status", {"path": "notes/a.md"})
    assert hs["ok"] is True
    assert hs["git_enabled"] is True
    assert hs["repo_initialized"] is True
    assert hs["clean"] is True
    assert hs["uncommitted_paths"] == []
    assert hs["head_commit_id"]
    assert hs["path_tracked"] is True
    assert hs["last_changed_commit_id"]
    assert hs["last_changed_at"]

    # An unknown path is not tracked, with null last-change fields.
    hs2 = await _call(mcp, "get_memory_history_status", {
        "path": "notes/unknown.md",
    })
    assert hs2["path_tracked"] is False
    assert hs2["last_changed_commit_id"] is None
    assert hs2["last_changed_at"] is None

    # A dirty work tree is reported, not raised.
    (root / "notes" / "dirty.md").write_text("uncommitted\n", encoding="utf-8")
    hs3 = await _call(mcp, "get_memory_history_status", {})
    assert hs3["clean"] is False
    assert hs3["uncommitted_paths"]


# ── scenario: get_memory_history bounded entries + diff + truncation ─


@pytest.mark.asyncio
async def test_get_memory_history_entries_diff_and_filters(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {
        "name": "a", "body": "one\n", "reason": "first reason",
    })
    await _call(mcp, "append_note", {
        "name": "a", "text": "two\n", "reason": "second reason",
    })
    await _call(mcp, "create_note", {"name": "b", "body": "bee\n"})

    hist = await _call(mcp, "get_memory_history", {})
    assert hist["ok"] is True
    for e in hist["entries"]:
        assert set(e) >= {
            "commit_id", "time", "actor", "operation", "reason", "message",
            "changed_paths", "summary",
        }
        assert set(e["summary"]) == {
            "files_changed", "insertions", "deletions",
        }
        assert "diff" not in e  # no diff unless include_diff

    # Newest first; operation equals the recorded write tool.
    assert [e["operation"] for e in hist["entries"]] == [
        "create_note", "append_note", "create_note",
    ]
    # reason reflects the write's reason (or None when omitted).
    assert hist["entries"][0]["reason"] is None            # create b
    assert hist["entries"][1]["reason"] == "second reason"  # append a
    assert hist["entries"][2]["reason"] == "first reason"   # create a
    # summary numbers are populated for a real change.
    assert hist["entries"][2]["summary"]["files_changed"] == 1
    assert hist["entries"][2]["changed_paths"] == ["notes/a.md"]

    # limit=1 bounds to one entry and flags truncated.
    one = await _call(mcp, "get_memory_history", {"limit": 1})
    assert len(one["entries"]) == 1
    assert one["truncated"] is True

    # include_diff attaches a bounded diff excerpt + a truncation flag.
    diffed = await _call(mcp, "get_memory_history", {
        "path": "notes/b.md", "include_diff": True, "limit": 1,
    })
    e = diffed["entries"][0]
    assert isinstance(e["diff"], str) and e["diff"]
    assert e["diff_truncated"] is False
    assert len(e["diff"].encode("utf-8")) <= HISTORY_DIFF_BYTE_CAP

    # A large write's diff is length-capped and flagged truncated.
    await _call(mcp, "create_note", {"name": "big", "body": "x" * 5000 + "\n"})
    big = await _call(mcp, "get_memory_history", {
        "path": "notes/big.md", "include_diff": True, "limit": 1,
    })
    eb = big["entries"][0]
    assert eb["diff_truncated"] is True
    assert len(eb["diff"].encode("utf-8")) <= HISTORY_DIFF_BYTE_CAP

    # Each filter narrows the result set.
    op = await _call(mcp, "get_memory_history", {"operation": "append_note"})
    assert op["entries"] and all(
        e["operation"] == "append_note" for e in op["entries"]
    )
    by_path = await _call(mcp, "get_memory_history", {"path": "notes/b.md"})
    assert by_path["entries"] and all(
        "notes/b.md" in e["changed_paths"] for e in by_path["entries"]
    )
    by_scope = await _call(mcp, "get_memory_history", {"scopes": ["notes"]})
    assert by_scope["entries"]
    by_query = await _call(mcp, "get_memory_history", {"query": "first reason"})
    assert by_query["entries"] and all(
        "notes/a.md" in e["changed_paths"] for e in by_query["entries"]
    )
    # actor maps to the recorded commit author (puffo-agent).
    actor_hit = await _call(mcp, "get_memory_history", {"actor": "puffo-agent"})
    assert len(actor_hit["entries"]) == len(hist["entries"]) + 1  # +big
    actor_miss = await _call(mcp, "get_memory_history", {"actor": "nobody-xyz"})
    assert actor_miss["entries"] == []


# ── scenario: raw git is NOT exposed ─────────────────────────────────


@pytest.mark.asyncio
async def test_raw_git_is_not_exposed(root):
    mcp = _build(root)
    tools = {t.name for t in await mcp.list_tools()}
    # No registered tool resembles a raw git operation.
    forbidden = (
        "git", "checkout", "reset", "revert", "rollback", "stash",
        "cherry", "rebase", "merge", "push", "log",
    )
    for name in tools:
        assert not any(tok in name for tok in forbidden), name

    for i in range(4):
        await _call(mcp, "create_note", {"name": f"n{i}", "body": f"b{i}\n"})
    plain = await _call(mcp, "get_memory_history", {"limit": 100})
    n_plain = len(plain["entries"])
    assert n_plain == 4

    # A filter value that looks like a flag or a ref range is a LITERAL,
    # never a git flag/ref: it can only narrow (never exceed) the plain
    # committed set, and never injects commits from outside it.
    for filt in (
        {"query": "--all"},
        {"query": "HEAD~5..HEAD"},
        {"since": "--all"},
        {"since": "HEAD~5..HEAD"},
        {"actor": "--all"},
    ):
        res = await _call(mcp, "get_memory_history", {**filt, "limit": 100})
        assert len(res["entries"]) <= n_plain
        for e in res["entries"]:
            assert e["commit_id"]
    # The substring post-filters treat "--all" as literal text (no match).
    assert await _call(mcp, "get_memory_history", {"query": "--all"}) == {
        "ok": True, "entries": [], "truncated": False,
    }


# ── scenario: expected failures are structured tool errors ───────────


@pytest.mark.asyncio
async def test_history_git_unavailable_is_structured(root, monkeypatch):
    ensure_memory_tree(root)
    memory_git.ensure_memory_git(root)
    monkeypatch.setattr(memory_git, "git_available", lambda: False)
    mcp = _build(root)
    for tool in ("get_memory_history", "get_memory_history_status"):
        err = await _call_err(mcp, tool, {})
        assert err["code"] == "memory_history_unavailable"
        assert err["causes"][0]["layer"] == "memory_git"
        assert err["suggestion"]


@pytest.mark.asyncio
async def test_history_not_initialized_is_structured(root):
    # A memory root with no local .git audit repo (M4 reads never
    # create one).
    ensure_memory_tree(root)
    assert not (root / ".git").exists()
    mcp = _build(root)
    err = await _call_err(mcp, "get_memory_history", {})
    assert err["code"] == "memory_history_not_initialized"
    err = await _call_err(mcp, "get_memory_history_status", {})
    assert err["code"] == "memory_history_not_initialized"


@pytest.mark.asyncio
async def test_history_bad_args_are_invalid_query(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {"name": "a", "body": "x\n"})
    for args in ({"limit": 0}, {"scopes": ["bogus"]}, {"limit": -5}):
        err = await _call_err(mcp, "get_memory_history", {**args})
        assert err["code"] == "memory_invalid_history_query"


def test_query_history_rejects_non_str_filter(root):
    # FastMCP's schema blocks a non-str query at the transport boundary;
    # the git layer still validates it (defense in depth).
    ensure_memory_tree(root)
    memory_git.ensure_memory_git(root)
    with pytest.raises(MemoryHistoryError) as ei:
        memory_git.query_history(root, query=123)
    assert ei.value.code == "memory_invalid_history_query"


@pytest.mark.asyncio
async def test_history_too_large_is_structured(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {"name": "a", "body": "x\n"})
    err = await _call_err(mcp, "get_memory_history", {
        "limit": HISTORY_MAX_LIMIT + 1,
    })
    assert err["code"] == "memory_history_query_too_large"
    err = await _call_err(mcp, "get_memory_history", {
        "include_diff": True, "limit": HISTORY_DIFF_MAX_LIMIT + 1,
    })
    assert err["code"] == "memory_history_query_too_large"


@pytest.mark.asyncio
async def test_history_bad_path_is_structured(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {"name": "a", "body": "x\n"})
    for tool in ("get_memory_history", "get_memory_history_status"):
        err = await _call_err(mcp, tool, {"path": "../evil"})
        assert err["code"] == "memory_invalid_path"
        err = await _call_err(mcp, tool, {"path": "/etc/passwd"})
        assert err["code"] == "memory_invalid_path"
        err = await _call_err(mcp, tool, {"path": "bogus/x.md"})
        assert err["code"] == "memory_path_out_of_scope"


# ── constant sanity: the M4 bounds are what the doc/tools advertise ──


def test_list_and_history_bounds_are_sane():
    assert LIST_MAX_LIMIT >= 1
    assert HISTORY_DIFF_MAX_LIMIT <= HISTORY_MAX_LIMIT
    assert HISTORY_DIFF_BYTE_CAP > 0
