"""Model-facing projection for context-bearing Puffo MCP results.

The daemon, RPC service, and ws-local bridge exchange structured Python
objects.  Only the stdio MCP boundary turns those objects into the semantic
text consumed by a harness.  Keeping that conversion here prevents transport
formatting from becoming part of the message lifecycle contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from mcp.types import TextContent

from ..agent.message_projection import CONTEXT_VERSION, target_label_from_ref


ToolResultSurface = Literal["stdio_mcp", "raw"]


def _json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _field(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return f"{name}={'true' if value else 'false'}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{name}={value}"
    if isinstance(value, (list, tuple)):
        return f"{name}={_json(list(value))}"
    return f"{name}={_json(str(value))}"


def _fields(values: Mapping[str, Any], names: Sequence[str]) -> list[str]:
    return [
        _field(name, values[name])
        for name in names
        if values.get(name) not in (None, "")
    ]


def _content_block(tag: str, text: str, *, context_version: int) -> list[str]:
    return [
        f"[{tag} context_version={context_version}]",
        f"content={_json(text)}",
    ]


def _participation_lines(
    snapshot: Mapping[str, Any], *, context_version: int
) -> list[str]:
    fields = _fields(
        snapshot,
        (
            "current_agent_identity",
            "current_agent_has_visible_message",
            "other_visible_agent_count",
        ),
    )
    lines = [
        f"[participation context_version={context_version} {' '.join(fields)}]".rstrip()
    ]

    current_message_ids = snapshot.get("current_agent_visible_message_ids")
    if isinstance(current_message_ids, list):
        lines.append(
            f"[current_agent_messages context_version={context_version} "
            f"message_ids={_json(current_message_ids)}]"
        )
    peer_identities = snapshot.get("other_visible_agent_identities")
    if isinstance(peer_identities, list):
        lines.append(
            f"[other_visible_agents context_version={context_version} "
            f"identities={_json(peer_identities)}]"
        )

    membership = snapshot.get("channel_membership")
    if isinstance(membership, Mapping):
        membership_fields = _fields(
            membership,
            (
                "context_ready",
                "member_count",
                "agent_member_count",
                "current_agent_is_member",
            ),
        )
        lines.append(
            f"[channel_membership context_version={context_version} "
            f"{' '.join(membership_fields)}]".rstrip()
        )
        agent_identities = membership.get("agent_member_identities")
        if isinstance(agent_identities, list):
            lines.append(
                f"[agent_members context_version={context_version} "
                f"identities={_json(agent_identities)}]"
            )
    return lines


def _window_lines(
    kind: str,
    body: str,
    *,
    context_version: int,
    returned_count: int | None = None,
) -> list[str]:
    fields = [
        f"context_version={context_version}",
        f"kind={_json(kind)}",
        'order="oldest_to_newest"',
    ]
    if returned_count is not None:
        fields.append(f"returned_count={returned_count}")
    lines = [f"[window {' '.join(fields)}]"]
    if body:
        lines.append(body)
    lines.append(f"[end_window context_version={context_version} kind={_json(kind)}]")
    return lines


def _empty_message_group(target_ref: str, *, context_version: int) -> str:
    lines: list[str] = []
    if target_ref:
        lines.append(f"## {target_label_from_ref(target_ref)}")
    lines.append(f"[messages context_version={context_version} message_count=0]")
    return "\n".join(lines)


def _reconsideration(result: Mapping[str, Any]) -> Mapping[str, Any]:
    value = result.get("reconsideration")
    return value if isinstance(value, Mapping) else {}


def _context_version(reconsideration: Mapping[str, Any]) -> int:
    raw_version = reconsideration.get("context_version", CONTEXT_VERSION)
    return (
        raw_version
        if isinstance(raw_version, int) and not isinstance(raw_version, bool)
        else CONTEXT_VERSION
    )


def _send_result_header(
    result: Mapping[str, Any],
    reconsideration: Mapping[str, Any],
    *,
    context_version: int,
) -> str:
    state = str(result.get("state") or "unknown")
    header_fields = [
        f"context_version={context_version}",
        f"state={_json(state)}",
    ]
    header_fields.extend(
        _fields(
            result,
            (
                "attempted",
                "seq",
                "replay",
                "devices_queued",
                "context_baseline_seq",
                "seen_seq",
                "latest_seq",
                "blocking_seq",
                "blocking_sender_slug",
                "latest_seq_before_send",
                "mode",
                "synchronized",
                "recovered_through_seq",
                "recovery_more_pending",
                "error_kind",
                "status",
                "covers_recorded",
                "covers_unknown",
                "covers_dropped",
            ),
        )
    )
    for model_name, transport_name in (
        ("message_id", "envelope_id"),
        ("latest_message_id", "latest_envelope_id"),
        ("blocking_message_id", "blocking_envelope_id"),
    ):
        value = result.get(transport_name)
        if value not in (None, ""):
            header_fields.append(_field(model_name, value))
    if reconsideration:
        header_fields.extend(
            _fields(
                reconsideration,
                ("context_ready", "based_on_through_seq"),
            )
        )
    return f"[send_result {' '.join(header_fields)}]"


def _target_lines(
    reconsideration: Mapping[str, Any],
) -> tuple[str, list[str]]:
    target = reconsideration.get("target")
    if not isinstance(target, Mapping):
        return "", []
    target_ref = str(target.get("target_ref") or "")
    if not target_ref:
        return "", []
    return target_ref, [f"## {target_label_from_ref(target_ref)}"]


def _result_detail_lines(
    result: Mapping[str, Any],
    reconsideration: Mapping[str, Any],
    *,
    context_version: int,
) -> list[str]:
    lines: list[str] = []
    for tag, value in (
        ("note", result.get("note")),
        ("error", result.get("error")),
        ("diagnostic", reconsideration.get("diagnostic")),
    ):
        if isinstance(value, str) and value:
            lines.extend(_content_block(tag, value, context_version=context_version))

    missing_devices = result.get("missing_devices")
    if isinstance(missing_devices, list) and missing_devices:
        lines.append(
            f"[missing_devices context_version={context_version} "
            f"device_ids={_json(missing_devices)}]"
        )
    return lines


def _held_window_lines(
    reconsideration: Mapping[str, Any],
    *,
    target_ref: str,
    context_version: int,
) -> list[str]:
    if reconsideration.get("context_ready") is not True:
        return []
    lines: list[str] = []
    for kind, value_key, count_key in (
        ("held_basis", "visible_draft_basis", ""),
        ("held_new_context", "new_channel_context", "new_channel_context_count"),
    ):
        value = reconsideration.get(value_key)
        body = value if isinstance(value, str) and value else ""
        raw_count = reconsideration.get(count_key) if count_key else None
        returned_count = (
            raw_count
            if isinstance(raw_count, int) and not isinstance(raw_count, bool)
            else 0
            if not body
            else None
        )
        lines.extend(
            _window_lines(
                kind,
                body
                or _empty_message_group(
                    target_ref,
                    context_version=context_version,
                ),
                context_version=context_version,
                returned_count=returned_count,
            )
        )
    return lines


def _held_result_lines(
    reconsideration: Mapping[str, Any],
    *,
    target_ref: str,
    context_version: int,
) -> list[str]:
    lines: list[str] = []
    draft = reconsideration.get("draft")
    if isinstance(draft, str):
        lines.extend(_content_block("draft", draft, context_version=context_version))

    snapshot = reconsideration.get("participation_snapshot")
    if isinstance(snapshot, Mapping):
        lines.extend(_participation_lines(snapshot, context_version=context_version))

    lines.extend(
        _held_window_lines(
            reconsideration,
            target_ref=target_ref,
            context_version=context_version,
        )
    )

    guidance = reconsideration.get("guidance")
    if isinstance(guidance, str) and guidance:
        lines.extend(
            (
                f"[guidance context_version={context_version} "
                'kind="held_reconsideration"]',
                guidance,
                f"[end_guidance context_version={context_version} "
                'kind="held_reconsideration"]',
            )
        )
    return lines


def format_send_result(result: Mapping[str, Any]) -> str:
    """Render one send result without exposing its nested transport object."""
    reconsideration = _reconsideration(result)
    context_version = _context_version(reconsideration)
    state = str(result.get("state") or "unknown")
    lines = [
        _send_result_header(
            result,
            reconsideration,
            context_version=context_version,
        )
    ]

    target_ref, target_lines = _target_lines(reconsideration)
    lines.extend(target_lines)
    lines.extend(
        _result_detail_lines(
            result,
            reconsideration,
            context_version=context_version,
        )
    )

    if state == "held" and reconsideration:
        lines.extend(
            _held_result_lines(
                reconsideration,
                target_ref=target_ref,
                context_version=context_version,
            )
        )

    admission_marker = result.get("tool_result_admission")
    if isinstance(admission_marker, str) and admission_marker:
        lines.append(admission_marker)
    lines.append(
        f"[end_send_result context_version={context_version} state={_json(state)}]"
    )
    return "\n".join(lines)


def project_text_result(text: str, *, surface: ToolResultSurface) -> str | TextContent:
    if surface == "raw":
        return text
    if surface != "stdio_mcp":
        raise ValueError(f"unknown tool result surface: {surface}")
    return TextContent(type="text", text=text)


def project_send_result(
    result: dict[str, Any], *, surface: ToolResultSurface
) -> dict[str, Any] | TextContent:
    if surface == "raw":
        return result
    if surface != "stdio_mcp":
        raise ValueError(f"unknown tool result surface: {surface}")
    return TextContent(type="text", text=format_send_result(result))
