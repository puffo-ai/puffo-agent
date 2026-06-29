"""Tool schemas + dispatch for the api-puffo runtime.

The LLM (Anthropic messages API shape) returns tool_use blocks
naming one of these tools; the runner dispatches each call into a
WebSocket bridge frame and waits for the matching ack / spaces
reply. Cloud does all crypto + persistence + delivery.

Day-1 surface (bounded by what the bridge protocol exposes):

  - ``send_message``   → bridge ``send`` (DM via recipient_slug;
                         channel via space_id + channel_id)
  - ``list_spaces``    → bridge ``list_spaces``

History / whoami tools from the legacy MCP surface are NOT here.
The bridge spec doesn't expose a history query (cloud-agents are
expected to ride ``fetch_pending`` backfill at connect + react to
live ``message`` frames; persistent per-thread storage will land
in a later spec revision). Adding them now would route through a
separate REST surface that doesn't exist yet.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .cloud_client import BridgeClosed, BridgeError, CloudBridgeClient

logger = logging.getLogger(__name__)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "send_message",
        "description": (
            "Post a message to a Puffo.ai channel or DM a user. "
            "Use 'recipient_slug' for a DM (e.g. 'alice-1234'); "
            "use 'space_id' + 'channel_id' (e.g. 'sp_<uuid>' + "
            "'ch_<uuid>') for a channel. Provide EXACTLY ONE of "
            "the two shapes — the bridge rejects mixed frames."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plaintext": {
                    "type": "string",
                    "description": "Message body. Markdown preserved verbatim.",
                },
                "recipient_slug": {
                    "type": "string",
                    "description": "DM target slug (no '@' prefix). Omit for channel send.",
                },
                "space_id": {
                    "type": "string",
                    "description": "Channel target space id. Provide with channel_id.",
                },
                "channel_id": {
                    "type": "string",
                    "description": "Channel target id. Provide with space_id.",
                },
            },
            "required": ["plaintext"],
        },
    },
    {
        "name": "list_spaces",
        "description": (
            "Enumerate the spaces (and their channels) the agent is "
            "a member of. Read-only, membership-scoped — never "
            "returns spaces the agent isn't in."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


async def dispatch_tool(
    bridge: CloudBridgeClient, name: str, args: dict[str, Any],
) -> str:
    """Translate a tool_use block into a bridge frame, await reply,
    return a string for the LLM tool_result block. All transport /
    server errors are normalised to a leading ``error:`` so the LLM
    can react inside its tool loop."""
    if name == "send_message":
        plaintext = args.get("plaintext", "")
        if not isinstance(plaintext, str) or not plaintext:
            return "error: send_message requires non-empty 'plaintext'"
        recipient_slug = args.get("recipient_slug") or None
        space_id = args.get("space_id") or None
        channel_id = args.get("channel_id") or None
        # Enforce the spec's one-shape-only rule client-side too so
        # the operator sees a clean message rather than BAD_FRAME.
        if recipient_slug and (space_id or channel_id):
            return (
                "error: send_message accepts EITHER recipient_slug "
                "(DM) OR space_id+channel_id (channel), not both"
            )
        if not recipient_slug and not (space_id and channel_id):
            return (
                "error: send_message requires recipient_slug "
                "(DM) OR space_id+channel_id (channel)"
            )
        try:
            ack = await bridge.send_send(
                plaintext=plaintext,
                recipient_slug=recipient_slug,
                space_id=space_id,
                channel_id=channel_id,
            )
        except BridgeError as exc:
            return f"error: {exc.code}: {exc.message}"
        except BridgeClosed:
            return "error: bridge is not connected; message not sent"
        except Exception as exc:  # noqa: BLE001
            return f"error: send_message failed: {exc}"
        envelope_id = ack.get("envelope_id", "?")
        queued = ack.get("devices_queued", 0)
        missing = ack.get("missing_devices", []) or []
        note = ""
        if missing:
            note = (
                f" (note: {len(missing)} recipient device(s) missed — "
                f"server will retry via supplementation)"
            )
        return f"posted {envelope_id} to {queued} device(s){note}"

    if name == "list_spaces":
        try:
            resp = await bridge.send_list_spaces()
        except BridgeError as exc:
            return f"error: {exc.code}: {exc.message}"
        except BridgeClosed:
            return "error: bridge is not connected"
        except Exception as exc:  # noqa: BLE001
            return f"error: list_spaces failed: {exc}"
        spaces = resp.get("spaces") or []
        if not spaces:
            return "(no spaces — agent is not a member of any)"
        lines: list[str] = []
        for sp in spaces:
            sname = sp.get("name", "") or sp.get("space_id", "?")
            sid = sp.get("space_id", "?")
            lines.append(f"# {sname} ({sid})")
            for ch in sp.get("channels") or []:
                cname = ch.get("name", "") or ch.get("channel_id", "?")
                cid = ch.get("channel_id", "?")
                lines.append(f"  - {cname} ({cid})")
        return "\n".join(lines)

    return f"error: unknown tool {name!r}"
