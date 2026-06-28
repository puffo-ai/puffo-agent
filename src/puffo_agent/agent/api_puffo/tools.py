"""Tool schemas + handlers for the api-puffo runtime.

The LLM (Anthropic messages API shape) returns tool_use blocks
naming one of these tools; the runner dispatches the call to the
matching cloud RPC. Cloud handles the heavy crypto / signing on
behalf of the agent — daemon just translates plaintext args into
a session-token-authenticated POST.
"""

from __future__ import annotations

import logging
from typing import Any

from .cloud_client import CloudHttpClient, CloudHttpError

logger = logging.getLogger(__name__)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "send_message",
        "description": (
            "Post a message to a Puffo.ai channel or DM a user. "
            "Use '@<slug>' for DMs (e.g. '@alice-1234'); use the raw "
            "channel id (e.g. 'ch_<uuid>') for channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "'@<slug>' for DM, or 'ch_<uuid>' for a channel.",
                },
                "text": {
                    "type": "string",
                    "description": "Message body. Markdown preserved verbatim.",
                },
                "is_visible_to_human": {
                    "type": "boolean",
                    "description": (
                        "REQUIRED. true for anything a person should read; "
                        "false for agent-to-agent coordination chatter (only "
                        "takes effect on threaded replies)."
                    ),
                },
                "root_id": {
                    "type": "string",
                    "description": (
                        "Optional. Reply inside a thread: pass the envelope_id "
                        "of the root post."
                    ),
                },
            },
            "required": ["channel", "text", "is_visible_to_human"],
        },
    },
    {
        "name": "get_channel_history",
        "description": (
            "List recent root posts in a channel from local storage, "
            "with reply count per thread. Replies are NOT inlined."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "ch_<uuid>",
                },
                "limit": {
                    "type": "integer",
                    "description": "Default 20, max 200.",
                },
            },
            "required": ["channel"],
        },
    },
    {
        "name": "get_thread_history",
        "description": (
            "Messages in a thread (root + every reply), filtered "
            "oldest-first up to limit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "root_id": {
                    "type": "string",
                    "description": "envelope_id of the root post.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Default 50, max 200.",
                },
            },
            "required": ["root_id"],
        },
    },
    {
        "name": "whoami",
        "description": "Return your own identity: display name, slug, device_id.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# Map tool name → cloud endpoint path (mocked until cloud is real).
_TOOL_CLOUD_PATHS: dict[str, str] = {
    "send_message": "/v1/send_message",
    "get_channel_history": "/v1/get_channel_history",
    "get_thread_history": "/v1/get_thread_history",
    "whoami": "/v1/whoami",
}


async def dispatch_tool(
    http: CloudHttpClient, name: str, args: dict[str, Any],
) -> str:
    """POST the tool args to its cloud endpoint, return the response
    body as a string (Anthropic tool_result accepts strings)."""
    path = _TOOL_CLOUD_PATHS.get(name)
    if path is None:
        return f"error: unknown tool {name!r}"
    try:
        resp = await http.post(path, args)
    except CloudHttpError as exc:
        logger.warning(
            "api-puffo tool %s: cloud error HTTP %d: %s",
            name, exc.status, exc.body,
        )
        return f"error: HTTP {exc.status}: {exc.body[:200]}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("api-puffo tool %s: transport error: %s", name, exc)
        return f"error: transport: {exc}"
    # Normalise to string for tool_result.
    text = resp.get("text") if isinstance(resp, dict) else None
    if isinstance(text, str):
        return text
    import json
    return json.dumps(resp)
