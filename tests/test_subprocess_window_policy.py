"""Guard every subprocess expression, including inline and aliased probes."""
from __future__ import annotations

import ast
from pathlib import Path


# These deliberately do not use the daemon-child policy. Keep scope explicit;
# new calls in these files are NOT automatically exempt.
_EXEMPT = {
    ("portal/background.py", "_spawn_detached"): "detached daemon has its own flags policy",
    ("portal/cli.py", "cmd_agent_edit"): "operator explicitly opens an interactive editor",
    ("agent/cli_bin.py", "claude_has_credentials"): "security command under darwin branch",
    ("portal/diagnostic.py", "probe_refresh_flush"): "returns before spawn unless macOS",
    ("macos/keychain.py", "_read_keychain_service"): "macOS-only security command",
    ("macos/keychain.py", "writeback_to_keychain"): "macOS-only security command",
}
_SPAWNS = {
    "subprocess.run", "subprocess.Popen", "subprocess.call",
    "subprocess.check_call", "subprocess.check_output",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
}


def _unprotected_calls(source: str):
    tree = ast.parse(source)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update((item.asname or item.name, item.name) for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module in {"subprocess", "asyncio"}:
            aliases.update((item.asname or item.name, f"{node.module}.{item.name}") for item in node.names)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        first, *rest = name.split(".")
        qualified = ".".join([aliases.get(first, first), *rest])
        if qualified not in _SPAWNS:
            continue
        guarded = any(
            kw.arg == "creationflags" or (
                kw.arg is None and isinstance(kw.value, ast.Call)
                and ast.unparse(kw.value.func).split(".")[-1]
                in {"no_window_kwargs", "process_group_spawn_kwargs"}
            ) for kw in node.keywords
        )
        if guarded:
            continue
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents.get(owner)
        yield (owner.name if owner is not None else "<module>"), node.lineno


def test_daemon_subprocess_calls_cannot_bypass_window_policy():
    """A new inline probe can reintroduce flashing despite all Driver tests
    passing; enumerate call expressions, not just assigned subprocess results."""
    root = Path(__file__).resolve().parents[1] / "src" / "puffo_agent"
    missing = []
    exemptions_seen = set()
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        for owner, line in _unprotected_calls(path.read_text(encoding="utf-8")):
            key = (relative, owner)
            if key in _EXEMPT:
                exemptions_seen.add(key)
            else:
                missing.append(f"{relative}:{line} ({owner})")
    assert not missing, "Missing subprocess window policy:\n" + "\n".join(missing)
    assert exemptions_seen == set(_EXEMPT), "Remove obsolete exemptions"


def test_window_policy_guard_finds_inline_and_aliased_calls():
    """Keep the inventory guard able to catch non-assignment spawn syntax."""
    source = '''
import subprocess as child
from asyncio import create_subprocess_exec as spawn_child
def probe():
    return child.run(["pi", "auth", "check"])
async def other():
    await spawn_child("opencode", "models")
'''
    assert [owner for owner, _ in _unprotected_calls(source)] == ["probe", "other"]
