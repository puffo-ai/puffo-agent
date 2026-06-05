"""One-shot diagnostic: spawn codex app-server with the same config
the puffo-agent codex_session uses, start a thread, then query
mcpServerStatus/list and print the result.

Tells us EXACTLY what tools codex's App Server publishes for the
thread — which is the missing piece for the `unsupported call:
mcp__puffo__send_message` puzzle.

Run from puffo-agent repo root:
    python .scratch/codex_mcp_diag.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


CODEX_BIN = r"C:\Program Files\nodejs\node_modules\@openai\.codex-l1yVItDd\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\codex\codex.exe"
# Borrow the health-543c736b agent's HOME so the config.toml that
# gets loaded mirrors a real agent's setup.
AGENT_HOME = Path.home() / ".puffo-agent" / "agents" / "health-543c736b"


async def _read_one(stream: asyncio.StreamReader) -> dict | None:
    """Read one line-delimited JSON-RPC message. Returns None on EOF."""
    while True:
        line = await stream.readline()
        if not line:
            return None
        s = line.decode("utf-8", errors="replace").strip()
        if not s:
            continue
        try:
            return json.loads(s)
        except ValueError:
            # codex sometimes emits non-JSON banner lines; skip.
            continue


async def _send(stream: asyncio.StreamWriter, body: dict) -> None:
    stream.write((json.dumps(body) + "\n").encode("utf-8"))
    await stream.drain()


async def main() -> int:
    env = os.environ.copy()
    env["HOME"] = str(AGENT_HOME)
    env["USERPROFILE"] = str(AGENT_HOME)
    env["CODEX_HOME"] = str(AGENT_HOME / ".codex")

    print(f"[diag] launching {CODEX_BIN} app-server")
    print(f"[diag] HOME / USERPROFILE / CODEX_HOME = {AGENT_HOME}")

    # readline default buffer is 64KiB — codex's mcpServerStatus/list
    # response with full tool descriptions easily exceeds that. Bump
    # to 4 MiB.
    loop = asyncio.get_event_loop()
    reader_so = asyncio.StreamReader(limit=4 * 1024 * 1024, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader_so, loop=loop)
    transport, _ = await loop.subprocess_exec(
        lambda: protocol, CODEX_BIN, "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(AGENT_HOME / "workspace"),
        env=env,
    )

    class _Proc:
        def __init__(self):
            self.stdin = asyncio.StreamWriter(
                transport.get_pipe_transport(0), protocol, None, loop,
            )
            self.stdout = reader_so
            # Wrap stderr separately for the drain task.
            self.stderr_reader = asyncio.StreamReader(limit=1024 * 1024, loop=loop)
            self.stderr_protocol = asyncio.StreamReaderProtocol(
                self.stderr_reader, loop=loop,
            )
            transport.get_pipe_transport(2).resume_reading()
        def terminate(self):
            transport.terminate()
        async def wait(self):
            return await transport._wait()
    # Fall back to the simple subprocess wrapper — the custom one
    # above is over-engineered. Use create_subprocess_exec with a
    # post-hoc buffer bump via stream.set_limit (only available
    # internally) — easier approach: read stderr as separate proc
    # and use raw stdout reads with no readline.
    transport.close()

    proc = await asyncio.create_subprocess_exec(
        CODEX_BIN, "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(AGENT_HOME / "workspace"),
        env=env,
        limit=8 * 1024 * 1024,
    )

    async def _drain_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            sys.stderr.write(f"[codex-stderr] {line.decode('utf-8', 'replace')}")

    stderr_task = asyncio.ensure_future(_drain_stderr())

    next_id = 1

    def nxt() -> int:
        nonlocal next_id
        i = next_id
        next_id += 1
        return i

    # 1. initialize
    init_id = nxt()
    await _send(proc.stdin, {
        "jsonrpc": "2.0", "id": init_id, "method": "initialize",
        "params": {
            "clientInfo": {"name": "diag", "version": "0.1"},
            "capabilities": {},
            "protocolVersion": "2025-06-18",
        },
    })

    # 2. thread/start (same params as puffo-agent's codex_session)
    thread_id = nxt()
    await _send(proc.stdin, {
        "jsonrpc": "2.0", "id": thread_id, "method": "thread/start",
        "params": {
            "cwd": str(AGENT_HOME / "workspace"),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "model": "gpt-5.4-mini",
        },
    })

    # Read messages until we have responses for init + thread/start +
    # a notification list we can inspect.
    started_thread_id: str | None = None
    pending_ids = {init_id, thread_id}
    while pending_ids:
        msg = await asyncio.wait_for(_read_one(proc.stdout), timeout=20.0)
        if msg is None:
            print("[diag] EOF before all responses received", file=sys.stderr)
            break
        kind = "response" if "id" in msg and ("result" in msg or "error" in msg) else "notification"
        print(f"[diag] <- {kind}: {json.dumps(msg)[:400]}")
        if kind == "response":
            pending_ids.discard(msg["id"])
            if msg["id"] == thread_id:
                result = msg.get("result") or {}
                started_thread_id = (
                    result.get("threadId")
                    or result.get("thread", {}).get("id")
                    or result.get("id")
                )

    if not started_thread_id:
        print("[diag] no threadId — abort")
        proc.terminate()
        return 1

    print(f"\n[diag] thread started: {started_thread_id}")

    # 3. mcpServerStatus/list (with threadId)
    list_id = nxt()
    await _send(proc.stdin, {
        "jsonrpc": "2.0", "id": list_id, "method": "mcpServerStatus/list",
        "params": {"threadId": started_thread_id, "detail": "full"},
    })

    # Drain until we get the list response
    deadline = 30.0
    while True:
        try:
            msg = await asyncio.wait_for(_read_one(proc.stdout), timeout=deadline)
        except asyncio.TimeoutError:
            print("[diag] timed out waiting for mcpServerStatus/list")
            break
        if msg is None:
            break
        kind = "response" if "id" in msg and ("result" in msg or "error" in msg) else "notification"
        if kind == "response" and msg.get("id") == list_id:
            print("\n[diag] ===== mcpServerStatus/list result =====")
            print(json.dumps(msg, indent=2))
            break
        else:
            print(f"[diag] <- (filler) {kind}: {json.dumps(msg)[:300]}")

    proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.terminate()
    stderr_task.cancel()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
