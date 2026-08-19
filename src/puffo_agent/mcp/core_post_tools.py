"""MCP registration for post tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent.message_projection import (
    CONTEXT_VERSION,
    format_message_group,
    iso_time,
    sender_type,
    target_ref,
)
from ..limits import MESSAGE_SEGMENT_CHARS
from .puffo_core_tools import (
    _history_text,
    _stage_model_visible_messages,
)


def _segment_source_text(content: Any) -> str:
    """Return the durable body rather than its bounded prompt projection."""
    if isinstance(content, dict) and "original_content" in content:
        return _history_text(content["original_content"])
    return _history_text(content)


async def _get_post_segment(
    cfg: Any,
    envelope_id: str,
    segment: int,
    segment_size: int,
) -> dict[str, Any]:
    envelope_id = (envelope_id or "").strip()
    if not envelope_id:
        raise RuntimeError("envelope_id is required")
    if segment < 0:
        raise RuntimeError("segment must be >= 0")
    if segment_size <= 0:
        raise RuntimeError("segment_size must be > 0")

    msg = await cfg.data_client.get_message_by_envelope(envelope_id)
    if msg is None:
        return {
            "context_version": CONTEXT_VERSION,
            "state": "not_found",
            "message_id": envelope_id,
        }

    # Eligible inbound rows carry a bounded ``text`` projection plus the
    # durable source. Legacy/outbound rows may still be a bare string or a
    # structured message without ``original_content``.
    text = _segment_source_text(msg.content)

    if not text:
        return {
            "context_version": CONTEXT_VERSION,
            "state": "empty",
            "message_id": envelope_id,
        }

    total = len(text)
    # ceil(total / segment_size); at least 1 when total > 0.
    seg_count = (total + segment_size - 1) // segment_size
    if segment >= seg_count:
        return {
            "context_version": CONTEXT_VERSION,
            "state": "out_of_range",
            "message_id": envelope_id,
            "requested_segment_index": segment,
            "segment_count": seg_count,
            "segment_size": segment_size,
        }
    start = segment * segment_size
    end = min(start + segment_size, total)
    chunk = text[start:end]
    tool_arguments = {
        "envelope_id": envelope_id,
        "segment": segment,
    }
    if segment_size != MESSAGE_SEGMENT_CHARS:
        tool_arguments["segment_size"] = segment_size
    receipt_marker = await _stage_model_visible_messages(
        cfg,
        [msg],
        tool_name="get_post_segment",
        tool_arguments=tool_arguments,
    )
    result: dict[str, Any] = {
        "context_version": CONTEXT_VERSION,
        "state": "ok",
        "source": {
            "target_ref": target_ref(
                msg,
                current_agent_aliases=(cfg.slug,),
            ),
            "message_id": msg.envelope_id,
            "sender_identity": f"@{str(msg.sender_slug).lstrip('@')}",
            "sender_type": sender_type(
                msg,
                current_agent_aliases=(cfg.slug,),
            ),
            "sent_at": iso_time(msg),
        },
        "segment": {
            "index": segment,
            "count": seg_count,
            "char_start": start,
            "char_end_exclusive": end,
            "total_chars": total,
            "text": chunk,
        },
    }
    if receipt_marker:
        result["admission_receipt"] = receipt_marker
    return result


def register_post_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def get_post(post_ref: str) -> str:
        """Fetch one local message by model-facing message id.

        Returns the semantic target/row projection. See the managed
        ``read-messages`` skill for supplementary-context guidance.
        """
        envelope_id = (post_ref or "").strip()
        if not envelope_id:
            raise RuntimeError("post_ref (envelope_id) is required")

        msg = await cfg.data_client.get_message_by_envelope(envelope_id)
        if msg is None:
            return f"message {envelope_id} not found in local storage"
        receipt_marker = await _stage_model_visible_messages(
            cfg,
            [msg],
            tool_name="get_post",
            tool_arguments={"post_ref": post_ref},
        )

        result = format_message_group([msg], current_agent_aliases=(cfg.slug,))
        return f"{result}\n{receipt_marker}" if receipt_marker else result

    @mcp.tool()
    async def get_post_segment(
        envelope_id: str,
        segment: int,
        segment_size: int = MESSAGE_SEGMENT_CHARS,
    ) -> dict[str, Any]:
        """Page a long message body back in chunks.

        When the daemon redacts an oversize inbound message it
        replaces the in-prompt body with a ``[puffo-agent system
        message]`` placeholder citing this tool's name plus the
        envelope_id and total segment count. Call this tool with
        ``segment=N`` (zero-indexed) and the same ``segment_size``
        the placeholder reported to retrieve chunk ``N`` of the
        full body. Only fetch the segments you actually need —
        the placeholder preview usually tells you whether the
        content is worth paging through.

        Returns a structured source and segment range. Missing, empty, and
        out-of-range results use an explicit ``state`` field.

        ``segment_size`` defaults to the daemon's default redaction
        page size; pass the value the placeholder cited if the operator
        has overridden it on their host.
        """
        return await _get_post_segment(
            cfg,
            envelope_id,
            segment,
            segment_size,
        )
