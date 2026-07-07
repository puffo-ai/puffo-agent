"""M3 memory acceptance tests: the semantic memory MCP tools layered
on the M2 MemoryStore — name normalization onto safe logical paths,
per-scope confinement (notes/, briefing/, recollection/, read-only
imports/), the write result envelope with git/audit commit ids and
post-effects, structured tool errors with M3 codes, pure bounded batch
reads, and deterministic search.

One test (or small group) per M3 validation scenario in
docs/user-lead-designs/memory-implementation.md, named after it.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from puffo_agent.agent import memory_git
from puffo_agent.agent.memory import MemoryStoreError, ensure_memory_tree
from puffo_agent.agent.memory_store import NOTES_FILE_LIMIT, MemoryStore
from puffo_agent.mcp import memory_tools
from puffo_agent.mcp.memory_tools import (
    MemoryToolsConfig,
    register_memory_tools,
)

M3_TOOLS = {
    "create_note",
    "patch_note",
    "append_note",
    "create_briefing_topic",
    "patch_briefing_topic",
    "append_recollection",
    "read_memory_file",
    "read_memory_files",
    "search_memory",
    "search_imports",
}


@pytest.fixture
def root(tmp_path):
    # The tools ensure the tree + local git repo lazily on first call.
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
    """Call a tool expecting failure; return the structured M3 error
    envelope extracted from the raised tool error's JSON text."""
    with pytest.raises(ToolError) as ei:
        await mcp.call_tool(name, args)
    text = str(ei.value)
    payload = json.loads(text[text.index("{"):])
    assert payload["ok"] is False
    return payload["error"]


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


# ── scenario 1: semantic names normalize to safe logical paths ───────


@pytest.mark.asyncio
async def test_semantic_names_normalize_to_safe_logical_paths(root):
    mcp = _build(root)
    res = await _call(mcp, "create_note", {
        "name": "My Topic  Notes", "body": "body\n",
    })
    assert res["paths"] == ["notes/my-topic-notes.md"]
    assert (root / "notes" / "my-topic-notes.md").is_file()

    res = await _call(mcp, "create_briefing_topic", {
        "name": "Team_Prefs.md", "body": "briefing\n",
    })
    assert res["paths"] == ["briefing/team-prefs.md"]
    assert (root / "briefing" / "team-prefs.md").is_file()

    maint = _build(root, maintenance=True)
    res = await _call(maint, "append_recollection", {
        "date": "2026-07-06", "text": "daily entry",
    })
    assert res["paths"] == ["recollection/2026/07/2026-07-06.md"]
    assert (root / "recollection" / "2026" / "07" / "2026-07-06.md").is_file()

    # Omitted date maps to today's (UTC) dated path shape.
    res = await _call(maint, "append_recollection", {"text": "later entry"})
    assert re.fullmatch(
        r"recollection/\d{4}/\d{2}/\d{4}-\d{2}-\d{2}\.md", res["paths"][0],
    )


# ── scenario 2: invalid semantic names are rejected ──────────────────


@pytest.mark.parametrize("bad", [
    "",                # empty
    ".",               # dot only
    "../evil",         # traversal shape
    "a/b",             # slashes: semantic names are flat
    ".hidden",         # hidden (dot-led)
    "x" * 101,         # too long
])
@pytest.mark.asyncio
async def test_invalid_semantic_names_are_rejected(root, bad):
    mcp = _build(root)
    for tool, args in [
        ("create_note", {"name": bad, "body": "b"}),
        ("append_note", {"name": bad, "text": "b"}),
        ("patch_note", {"name": bad, "patches": [
            {"old_text": "a", "new_text": "b"},
        ]}),
        ("create_briefing_topic", {"name": bad, "body": "b"}),
        ("patch_briefing_topic", {"name": bad, "patches": [
            {"old_text": "a", "new_text": "b"},
        ]}),
    ]:
        err = await _call_err(mcp, tool, args)
        assert err["code"] == "memory_invalid_name"
        assert err["operation"] == tool
        assert err["suggestion"]


def test_non_string_semantic_name_is_rejected():
    # FastMCP's schema already blocks non-str args at the transport
    # boundary; the tools layer still validates (defense in depth).
    with pytest.raises(ToolError) as ei:
        memory_tools._normalize_name("create_note", 123)
    text = str(ei.value)
    err = json.loads(text[text.index("{"):])["error"]
    assert err["code"] == "memory_invalid_name"


@pytest.mark.asyncio
async def test_invalid_recollection_date_is_rejected(root):
    mcp = _build(root, maintenance=True)
    for bad in ("07/06/2026", "20260706", "2026-7-6", "2026-13-40"):
        err = await _call_err(mcp, "append_recollection", {
            "date": bad, "text": "x",
        })
        assert err["code"] == "memory_invalid_arguments"
    assert not (root / "recollection").exists() or not any(
        (root / "recollection").rglob("*.md")
    )


@pytest.mark.asyncio
async def test_invalid_patches_are_rejected(root):
    mcp = _build(root)
    # (Non-dict entries like ["nope"] are already rejected by the MCP
    # schema for list[dict] before the tool body runs.)
    for bad_patches in ([], [{"old_text": 1, "new_text": "x"}], [{}]):
        err = await _call_err(mcp, "patch_note", {
            "name": "n", "patches": bad_patches,
        })
        assert err["code"] == "memory_invalid_arguments"


# ── scenario 3: note tools cannot write outside notes/ ───────────────


@pytest.mark.asyncio
async def test_note_tools_cannot_write_outside_notes(root):
    mcp = _build(root)
    ensure_memory_tree(root)
    before = _tree_snapshot(root)
    for bad in ("../evil", "notes/../../etc/passwd", "a/b", ".."):
        err = await _call_err(mcp, "create_note", {"name": bad, "body": "b"})
        assert err["code"] == "memory_invalid_name"
    assert _tree_snapshot(root) == before

    res = await _call(mcp, "create_note", {"name": "safe", "body": "b\n"})
    assert res["paths"] == ["notes/safe.md"]
    res = await _call(mcp, "append_note", {"name": "safe", "text": "more\n"})
    assert res["paths"] == ["notes/safe.md"]
    res = await _call(mcp, "patch_note", {"name": "safe", "patches": [
        {"old_text": "more", "new_text": "MORE"},
    ]})
    assert res["paths"] == ["notes/safe.md"]
    # Every new file the note tools produced lives under notes/.
    new_paths = set(_tree_snapshot(root)) - set(before)
    assert new_paths == {"notes/safe.md"}


# ── scenario 4: briefing tools cannot write outside briefing/ ────────


@pytest.mark.asyncio
async def test_briefing_topic_tools_cannot_write_outside_briefing(root):
    mcp = _build(root)
    ensure_memory_tree(root)
    before = _tree_snapshot(root)
    for bad in ("../evil", "briefing/../notes/x", "a/b", "sub/topic"):
        err = await _call_err(mcp, "create_briefing_topic", {
            "name": bad, "body": "b",
        })
        assert err["code"] == "memory_invalid_name"
    assert _tree_snapshot(root) == before

    res = await _call(mcp, "create_briefing_topic", {
        "name": "prefs", "body": "b\n",
    })
    assert res["paths"] == ["briefing/prefs.md"]
    res = await _call(mcp, "patch_briefing_topic", {"name": "prefs", "patches": [
        {"old_text": "b\n", "new_text": "B\n"},
    ]})
    assert res["paths"] == ["briefing/prefs.md"]
    # Flat and confined: the only new file sits directly in briefing/.
    new_paths = set(_tree_snapshot(root)) - set(before)
    assert new_paths == {"briefing/prefs.md"}


# ── scenario 5: recollection/ needs explicit scope ───────────────────


@pytest.mark.asyncio
async def test_conversation_scope_cannot_write_recollection(root):
    mcp = _build(root)
    err = await _call_err(mcp, "append_recollection", {
        "date": "2026-07-06", "text": "entry",
    })
    assert err["code"] == "memory_scope_readonly"
    assert err["suggestion"]
    assert not (root / "recollection" / "2026").exists()

    # The same write under the explicit maintenance scope succeeds —
    # the deny above is scope, not breakage.
    maint = _build(root, maintenance=True)
    res = await _call(maint, "append_recollection", {
        "date": "2026-07-06",
        "text": "learned a thing",
        "source_message_ids": ["m1", "m2"],
        "related_paths": ["notes/a.md"],
    })
    assert res["ok"] is True and res["changed"] is True
    body = (
        root / "recollection" / "2026" / "07" / "2026-07-06.md"
    ).read_text(encoding="utf-8")
    assert body.startswith("# 2026-07-06\n")
    assert "learned a thing" in body
    assert "sources: m1, m2" in body
    assert "related: notes/a.md" in body

    # A second entry appends to the same dated file.
    await _call(maint, "append_recollection", {
        "date": "2026-07-06", "text": "second entry",
    })
    body = (
        root / "recollection" / "2026" / "07" / "2026-07-06.md"
    ).read_text(encoding="utf-8")
    assert body.count("# 2026-07-06") == 1
    assert "second entry" in body


# ── scenario 6: imports are read-only ────────────────────────────────


@pytest.mark.asyncio
async def test_imports_are_read_only(root):
    mcp = _build(root)
    res = await _call(mcp, "read_memory_file", {"path": "imports/index.md"})
    assert res["scope"] == "imports"
    assert "Imports index" in res["body"]

    res = await _call(mcp, "search_imports", {"query": "provenance"})
    assert res["results"]
    assert all(m["path"].startswith("imports/") for m in res["results"])

    # No semantic write tool can produce an imports/ path: names are
    # flat, so "imports/…" never survives normalization.
    for tool, args in [
        ("create_note", {"name": "imports/evil", "body": "b"}),
        ("append_note", {"name": "imports/index", "text": "b"}),
        ("create_briefing_topic", {"name": "imports/evil", "body": "b"}),
    ]:
        err = await _call_err(mcp, tool, args)
        assert err["code"] == "memory_invalid_name"
    assert not (root / "imports" / "evil.md").exists()

    # Store-level defense in depth: a direct imports/ write is denied.
    with pytest.raises(MemoryStoreError) as ei:
        MemoryStore(root).create_memory_file("imports/new.md", "x")
    assert ei.value.code == "memory_scope_readonly"


# ── scenario 7: reason? appears in git/audit metadata ────────────────


@pytest.mark.asyncio
async def test_reason_appears_in_git_audit_metadata(root):
    mcp = _build(root)
    res = await _call(mcp, "create_note", {
        "name": "topic", "body": "body\n", "reason": "user asked",
    })
    assert res["commit_id"] == _git(root, "rev-parse", "--short", "HEAD")
    message = _git(root, "log", "-1", "--format=%B")
    assert "create_note" in message
    assert "notes/topic.md" in message
    assert "reason: user asked" in message

    # A write WITHOUT reason commits with no reason line.
    res2 = await _call(mcp, "append_note", {"name": "topic", "text": "more\n"})
    assert res2["commit_id"] == _git(root, "rev-parse", "--short", "HEAD")
    assert res2["commit_id"] != res["commit_id"]
    message = _git(root, "log", "-1", "--format=%B")
    assert "append_note" in message
    assert "reason:" not in message


@pytest.mark.asyncio
async def test_git_unavailable_degrades_gracefully(root, monkeypatch):
    monkeypatch.setattr(memory_git, "git_available", lambda: False)
    mcp = _build(root)
    res = await _call(mcp, "create_note", {"name": "n", "body": "b\n"})
    assert res["ok"] is True
    assert res["changed"] is True
    assert res["commit_id"] is None
    # Degrade is logged, not warned — warnings are for post-effect
    # failures.
    assert res["warnings"] == []
    assert (root / "notes" / "n.md").is_file()
    assert not (root / ".git").exists()


# ── scenario 8: write result envelope ────────────────────────────────


@pytest.mark.asyncio
async def test_write_tools_return_changed_paths_commit_id_post_effects(
    root, tmp_path,
):
    workspace = tmp_path / "workspace"
    mcp = _build(root, workspace=workspace)

    res = await _call(mcp, "create_note", {"name": "n", "body": "b\n"})
    assert set(res) == {
        "ok", "tool", "changed", "paths", "commit_id", "post_effects",
        "warnings",
    }
    assert set(res["post_effects"]) == {"briefing_rebuilt", "provider_reload"}
    assert res["tool"] == "create_note"
    assert res["changed"] is True
    assert isinstance(res["commit_id"], str) and res["commit_id"]
    assert res["post_effects"] == {
        "briefing_rebuilt": False, "provider_reload": "not_needed",
    }
    assert res["warnings"] == []

    # Briefing write with a real workspace: rebuilt + reload requested,
    # and the refresh flag actually exists.
    res = await _call(mcp, "create_briefing_topic", {
        "name": "prefs", "body": "x\n",
    })
    assert res["post_effects"] == {
        "briefing_rebuilt": True, "provider_reload": "requested",
    }
    flag = workspace / ".puffo-agent" / "refresh_agent.flag"
    assert flag.is_file()
    payload = json.loads(flag.read_text(encoding="utf-8"))
    assert payload["reason"] == (
        "memory_tools.create_briefing_topic:briefing/prefs.md"
    )

    # No-op patch: changed False, nothing committed, no post-effects.
    head = _git(root, "rev-parse", "HEAD")
    res = await _call(mcp, "patch_briefing_topic", {"name": "prefs", "patches": [
        {"old_text": "x", "new_text": "x"},
    ]})
    assert res["changed"] is False
    assert res["commit_id"] is None
    assert res["post_effects"] == {
        "briefing_rebuilt": False, "provider_reload": "not_needed",
    }
    assert _git(root, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_briefing_write_with_failed_reload_warns_but_stays_ok(
    root, monkeypatch,
):
    monkeypatch.setattr(
        memory_tools, "request_prompt_refresh", lambda ws, reason: False,
    )
    mcp = _build(root, workspace="")
    res = await _call(mcp, "create_briefing_topic", {
        "name": "t", "body": "b\n",
    })
    # Post-effect failure after a successful write never pretends the
    # write failed.
    assert res["ok"] is True
    assert res["changed"] is True
    assert res["post_effects"]["briefing_rebuilt"] is True
    assert res["post_effects"]["provider_reload"] == "failed"
    assert [w["code"] for w in res["warnings"]] == [
        "memory_provider_reload_failed",
    ]
    assert res["warnings"][0]["message"]


# ── scenario 9: expected failures are structured tool errors ─────────


@pytest.mark.asyncio
async def test_expected_failures_return_structured_tool_errors(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {"name": "n", "body": "alpha beta\n"})

    err = await _call_err(mcp, "patch_note", {"name": "n", "patches": [
        {"old_text": "gamma", "new_text": "delta"},
    ]})
    assert set(err) >= {
        "code", "message", "operation", "path", "suggestion", "causes",
    }
    assert err["code"] == "memory_patch_no_match"
    assert err["operation"] == "patch_note"
    assert err["path"] == "notes/n.md"
    assert err["suggestion"]
    assert err["causes"][0]["layer"] == "memory_store"
    assert err["causes"][0]["code"] == "memory_patch_no_match"

    err = await _call_err(mcp, "create_note", {"name": "n", "body": "again"})
    assert err["code"] == "memory_file_exists"

    err = await _call_err(mcp, "create_note", {
        "name": "big", "body": "x" * (NOTES_FILE_LIMIT + 1),
    })
    assert err["code"] == "memory_file_too_large"
    assert err["limit"] == NOTES_FILE_LIMIT
    assert err["size"] > err["limit"]


# ── scenario 10: read_memory_files is a pure bounded batch ───────────


@pytest.mark.asyncio
async def test_read_memory_files_reads_bounded_files_and_never_writes(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {"name": "a", "body": "note a"})
    await _call(mcp, "create_briefing_topic", {"name": "b", "body": "brief b"})
    # Over-limit file placed out-of-band proves reads are bounded.
    (root / "notes" / "huge.md").write_text(
        "h" * (NOTES_FILE_LIMIT + 512), encoding="utf-8",
    )
    head_before = _git(root, "rev-parse", "HEAD")
    before = _tree_snapshot(root)

    res = await _call(mcp, "read_memory_files", {"paths": [
        "notes/a.md", "briefing/b.md", "notes/huge.md",
        "notes/missing.md", "foo.md",
    ]})
    results = res["results"]
    assert [r["path"] for r in results] == [
        "notes/a.md", "briefing/b.md", "notes/huge.md",
        "notes/missing.md", "foo.md",
    ]
    assert results[0]["body"] == "note a"
    assert results[1]["body"] == "brief b"
    assert results[2]["truncated"] is True
    assert len(results[2]["body"].encode("utf-8")) == NOTES_FILE_LIMIT
    # One bad path yields a per-entry error, not a batch failure.
    assert results[3]["error"]["code"] == "memory_file_not_found"
    assert results[4]["error"]["code"] == "memory_invalid_path"

    # Pure read: tree bytes and git HEAD are both untouched.
    assert _tree_snapshot(root) == before
    assert _git(root, "rev-parse", "HEAD") == head_before


# ── registration ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_ten_m3_tools_are_registered_on_built_server(root, tmp_path):
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
    assert M3_TOOLS <= names
    # Building the server has no side effects: the memory tree and its
    # git repo are ensured lazily on first tool call, not at build time.
    assert not root.exists()


# ── search ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_memory_bounded_deterministic_results(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {
        "name": "alpha",
        "body": "needle one\nplain line\nNEEDLE two\nneedle three\nneedle four\n",
    })
    await _call(mcp, "create_note", {
        "name": "beta", "body": ("x" * 400) + " needle tail\n",
    })
    await _call(mcp, "create_briefing_topic", {
        "name": "gamma", "body": "needle brief\n",
    })

    res = await _call(mcp, "search_memory", {"query": "NeEdLe"})
    assert res["ok"] is True and res["query"] == "NeEdLe"
    paths = [m["path"] for m in res["results"]]
    # Fixed scope order (briefing before notes), sorted files within.
    assert paths[0] == "briefing/gamma.md"
    assert res["results"][0]["scope"] == "briefing"
    assert res["results"][0]["line"] == 1
    # ≤ 3 matches per file: alpha has 4 matching lines.
    assert paths.count("notes/alpha.md") == 3
    # Snippets are bounded even for very long lines.
    assert all(len(m["snippet"]) <= 200 for m in res["results"])
    assert res["truncated"] is False

    # Deterministic: an identical query returns identical results.
    assert await _call(mcp, "search_memory", {"query": "NeEdLe"}) == res

    # limit is enforced and reported via truncated.
    res2 = await _call(mcp, "search_memory", {"query": "needle", "limit": 2})
    assert len(res2["results"]) == 2
    assert res2["truncated"] is True

    # Scope filtering.
    res3 = await _call(mcp, "search_memory", {
        "query": "needle", "scopes": ["notes"],
    })
    assert res3["results"]
    assert all(m["scope"] == "notes" for m in res3["results"])


@pytest.mark.asyncio
async def test_search_memory_rejects_imports_scope_and_bad_arguments(root):
    mcp = _build(root)
    err = await _call_err(mcp, "search_memory", {
        "query": "x", "scopes": ["imports"],
    })
    assert err["code"] == "memory_invalid_arguments"
    assert "search_imports" in err["suggestion"]

    err = await _call_err(mcp, "search_memory", {
        "query": "x", "scopes": ["bogus"],
    })
    assert err["code"] == "memory_invalid_arguments"

    err = await _call_err(mcp, "search_memory", {"query": "x", "limit": 0})
    assert err["code"] == "memory_invalid_arguments"

    err = await _call_err(mcp, "search_memory", {"query": "   "})
    assert err["code"] == "memory_invalid_arguments"

    err = await _call_err(mcp, "search_imports", {"query": "x", "limit": -3})
    assert err["code"] == "memory_invalid_arguments"


@pytest.mark.asyncio
async def test_search_imports_hits_only_imports(root):
    mcp = _build(root)
    await _call(mcp, "create_note", {
        "name": "n", "body": "provenance mentioned in a note\n",
    })
    (root / "imports" / "doc.txt").write_text(
        "imported provenance data\n", encoding="utf-8",
    )

    res = await _call(mcp, "search_imports", {"query": "provenance"})
    paths = [m["path"] for m in res["results"]]
    assert paths
    assert all(p.startswith("imports/") for p in paths)
    assert "imports/doc.txt" in paths
    assert all(m["scope"] == "imports" for m in res["results"])
    assert "notes/n.md" not in paths

    # …and search_memory never reaches into imports/.
    res = await _call(mcp, "search_memory", {"query": "provenance"})
    assert [m["path"] for m in res["results"]] == ["notes/n.md"]
