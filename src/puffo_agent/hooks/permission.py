"""Claude Code PreToolUse hook — puffo-agent permission proxy.

Runs per tool invocation that claude would normally prompt on. DMs
the operator "agent X wants to run Y", polls the thread for a y/n
reply, and returns the decision via the PreToolUse protocol:

  exit 2                  -> deny (reason on stderr)
  exit 0 + allow JSON     -> allow (skip native prompt)
  exit 0 + empty stdout   -> fall through to normal flow (fail-open)

A hook (rather than ``--permission-prompt-tool``) is required
because our cli-local adapter runs claude in interactive mode and
that CLI flag is non-interactive-mode only.

Env vars set by the spawning adapter:

  PUFFO_URL                base URL (required)
  PUFFO_BOT_TOKEN          bot's personal access token (required)
  PUFFO_OPERATOR_USERNAME  who to DM (required; empty -> fail-open)
  PUFFO_AGENT_ID           shown in the DM (default "unknown")
  PUFFO_PERMISSION_TIMEOUT poll timeout seconds (default 300)

stdlib-only (urllib + json + time + sys + os) so it can run from
a minimal interpreter without importing the rest of puffo-agent.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _fail_open(reason: str) -> None:
    """Exit 0 with stderr note: claude proceeds through its normal
    permission flow. We fail-open on proxy-side failures (missing
    config, transport errors before posting the request) because a
    silently-denied agent is harder to debug than one that surfaces
    claude's native error.
    """
    print(f"[puffo-permission-hook] fail-open: {reason}", file=sys.stderr)
    sys.exit(0)


def _deny(reason: str) -> None:
    """Exit 2 with reason on stderr. Claude blocks the tool call
    and surfaces the reason back to the model.
    """
    print(reason, file=sys.stderr)
    sys.exit(2)


def _allow(reason: str) -> None:
    """Exit 0 with explicit allow JSON. Required to short-circuit
    claude's post-hook permission checks (without it, claude would
    still prompt).
    """
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out))
    sys.exit(0)


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _http_get(url: str, headers: dict, timeout: float = 10.0):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post(url: str, headers: dict, payload, timeout: float = 10.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _lookup_operator_id(
    base_url: str, headers: dict, operator_username: str,
) -> str:
    """Return the operator's user id, used to filter replies in the
    request thread to only that operator's posts.
    """
    user = _http_get(
        f"{base_url}/api/v4/users/username/{operator_username}", headers,
    )
    return user["id"]


def read_current_turn(cwd: str) -> dict | None:
    """Read the per-turn context the daemon wrote before dispatching
    to claude. Returns ``{channel_id, root_id, triggering_post_id}``
    or ``None`` when the file is missing (proactive agent work, no
    user-triggered turn — hook should fail open).
    """
    path = Path(cwd) / ".puffo-agent" / "current_turn.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("channel_id"):
        return None
    return data


def summarise_tool_input(data, limit: int = 400) -> str:
    """Render ``tool_input`` for the permission DM. Per-value cap
    120 chars and overall cap ``limit`` so a pasted file doesn't
    turn the DM into a wall.
    """
    if isinstance(data, dict):
        parts = []
        for k, v in data.items():
            s = str(v)
            if len(s) > 120:
                s = s[:120] + "…"
            parts.append(f"- **{k}**: `{s}`")
        text = "\n".join(parts)
    elif data is None:
        text = ""
    else:
        text = f"`{str(data)[:limit]}`"
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "(no input)"


def poll_for_reply(
    base_url: str,
    headers: dict,
    thread_root_id: str,
    owner_id: str,
    request_ts: int,
    timeout_seconds: int,
    sleep_seconds: float = 2.0,
) -> bool | None:
    """Poll for the operator's reply in the request thread.

    Returns True on approval (first char y/a), False on denial,
    None on timeout. The thread root keeps concurrent permission
    requests correlated when claude fires parallel tool calls.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            data = _http_get(
                f"{base_url}/api/v4/posts/{thread_root_id}/thread",
                headers,
            )
        except Exception:
            time.sleep(sleep_seconds)
            continue
        posts = data.get("posts") or {}
        order = data.get("order") or []
        for pid in order:
            post = posts.get(pid) or {}
            if post.get("user_id") != owner_id:
                continue
            created_ms = int(post.get("create_at", 0))
            if created_ms // 1000 <= request_ts:
                continue
            msg = (post.get("message") or "").strip().lower()
            if not msg:
                continue
            return msg[0] in ("y", "a")
        time.sleep(sleep_seconds)
    return None


def main() -> None:
    base_url = (os.environ.get("PUFFO_URL") or "").rstrip("/")
    bot_token = os.environ.get("PUFFO_BOT_TOKEN") or ""
    operator = os.environ.get("PUFFO_OPERATOR_USERNAME") or ""
    agent_id = os.environ.get("PUFFO_AGENT_ID") or "unknown"
    try:
        timeout_s = int(os.environ.get("PUFFO_PERMISSION_TIMEOUT") or "300")
    except ValueError:
        timeout_s = 300

    if not (base_url and bot_token):
        _fail_open("PUFFO_URL / PUFFO_BOT_TOKEN not set")
    if not operator:
        _fail_open("PUFFO_OPERATOR_USERNAME empty — no operator to DM")

    try:
        raw = sys.stdin.read() or "{}"
        payload = json.loads(raw)
    except Exception as exc:
        _fail_open(f"could not parse hook payload: {exc}")
    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {})
    # claude passes its subprocess cwd (== agent's workspace_dir);
    # the daemon writes current_turn.json there so the hook knows
    # which channel + thread to reply in.
    cwd = payload.get("cwd", "")

    turn = read_current_turn(cwd)
    if turn is None:
        _fail_open(
            "no current_turn.json — proactive agent work, no user "
            "message to reply to"
        )
    channel_id = turn["channel_id"]
    # ``root_id`` falls back to the triggering post id when the user
    # message was itself the (new) thread root. Either way, this
    # lands the permission DM in the thread the user is reading.
    root_id = turn.get("root_id") or turn.get("triggering_post_id") or ""
    if not root_id:
        _fail_open("current_turn.json missing root_id")

    headers = _headers(bot_token)
    try:
        operator_id = _lookup_operator_id(base_url, headers, operator)
    except Exception as exc:
        _fail_open(f"cannot look up operator @{operator}: {exc}")

    summary = summarise_tool_input(tool_input)
    request_ts = int(time.time())
    try:
        _http_post(
            f"{base_url}/api/v4/posts",
            headers,
            {
                "channel_id": channel_id,
                "root_id": root_id,
                "message": (
                    f"@{operator} 🔐 **agent `{agent_id}` wants to run "
                    f"`{tool_name}`**\n\n"
                    f"{summary}\n\n"
                    f"Reply `y` to approve or `n` to deny (times out in "
                    f"{timeout_s}s)."
                ),
            },
        )
    except Exception as exc:
        _fail_open(f"could not post permission request: {exc}")

    # Poll the original thread for the operator's reply. Multiple
    # permission requests in the same turn share this thread; the
    # since_ts gate keeps each request's poll scoped to replies
    # posted AFTER it asked.
    decision = poll_for_reply(
        base_url, headers, root_id, operator_id, request_ts, timeout_s,
    )
    if decision is True:
        _allow(f"@{operator} approved via chat")
    if decision is False:
        _deny(f"@{operator} denied via chat")
    _deny(f"permission request timed out after {timeout_s}s (no reply from @{operator})")


if __name__ == "__main__":
    main()
