"""Model-facing projection for context-bearing Puffo MCP results.

The daemon, RPC service, and ws-local bridge exchange structured Python
objects.  Only the stdio MCP boundary turns those objects into the semantic
text consumed by a harness.  Keeping that conversion here prevents transport
formatting from becoming part of the message lifecycle contract.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from mcp.types import TextContent

from ..agent.message_projection import CONTEXT_VERSION, target_label_from_ref


ToolResultSurface = Literal["stdio_mcp", "raw"]

# A held result must stay small enough that the harness never spills it
# to a file wholesale: the ``[puffo:model-visible-read:...]`` receipt in
# its tail completes the model-visible transition only when it actually
# enters the model's context, and ``send_anyway`` is refused without
# that transition — so a wholesale spill disables the escape hatch in
# exactly the busy-channel scenario it exists for. The budget is
# conservative against known harness tool-result caps.
_HELD_INLINE_BUDGET_CHARS = 40_000
_HELD_CONTENT_CAP_CHARS = 2_000
_HELD_CONTENT_FLOOR_CHARS = 300
_HELD_MAX_INLINE_MESSAGES = 40
_HELD_SPILL_SUBDIR = (".puffo", "held")


def _held_spill_dir() -> Path:
    root = os.environ.get("PUFFO_WORKSPACE") or os.getcwd()
    return Path(root).joinpath(*_HELD_SPILL_SUBDIR)


def _spill_held_text(text: str) -> str:
    """Persist the full held result; the compact inline form points here."""
    directory = _held_spill_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = directory / f"held-{stamp}-{os.urandom(4).hex()}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _truncate_content_line(line: str, cap: int) -> str:
    """Bound one ``content=<json>`` line, keeping it valid and marked."""
    prefix = "content="
    if not line.startswith(prefix) or len(line) <= cap + len(prefix):
        return line
    try:
        decoded = json.loads(line[len(prefix):])
    except Exception:
        return line
    if not isinstance(decoded, str) or len(decoded) <= cap:
        return line
    truncated = decoded[:cap] + " …[truncated; full text in the spill file]"
    return prefix + _json(truncated)


def _tail_message_blocks(body: str, keep: int) -> tuple[int, str, str]:
    """Split a message-group body into (omitted_count, preamble, kept tail).

    Newest messages are the reconsideration-relevant ones, so a count
    overflow drops from the oldest end; the preamble (the ``##`` target
    label ahead of the first message header) survives either way.
    """
    lines = body.split("\n")
    starts = [
        index for index, line in enumerate(lines)
        if line.startswith("[message ")
    ]
    if len(starts) <= keep:
        return 0, "", body
    preamble = "\n".join(lines[: starts[0]])
    if keep == 0:
        return len(starts), preamble, ""
    cut = starts[len(starts) - keep]
    return len(starts) - keep, preamble, "\n".join(lines[cut:])


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


def _held_result_lines_compact(
    reconsideration: Mapping[str, Any],
    *,
    target_ref: str,
    context_version: int,
    spilled_to: str,
    content_cap: int,
    max_messages: int | None = None,
) -> list[str]:
    """The must-stay-inline projection of an oversized held result.

    The agent's own draft echo and the already-seen basis carry no new
    information, so they go to the spill file; the new-context window
    (bounded per message, and per count when ``max_messages`` is set)
    and the participation/guidance stay inline.
    """
    lines: list[str] = []
    draft = reconsideration.get("draft")
    if isinstance(draft, str):
        lines.append(
            f"[draft context_version={context_version} chars={len(draft)} "
            f"spilled_to={_json(spilled_to)}]"
        )

    snapshot = reconsideration.get("participation_snapshot")
    if isinstance(snapshot, Mapping):
        lines.extend(_participation_lines(snapshot, context_version=context_version))

    lines.extend(
        _window_lines(
            "held_basis",
            f"[spilled context_version={context_version} "
            f"spilled_to={_json(spilled_to)}]",
            context_version=context_version,
        )
    )

    new_context = reconsideration.get("new_channel_context")
    body = new_context if isinstance(new_context, str) and new_context else ""
    if body:
        body = "\n".join(
            _truncate_content_line(line, content_cap) for line in body.split("\n")
        )
    if body and max_messages is not None:
        omitted, preamble, tail = _tail_message_blocks(body, max_messages)
        if omitted:
            notice = (
                f"[messages_omitted context_version={context_version} "
                f"omitted_count={omitted} spilled_to={_json(spilled_to)}]"
            )
            body = "\n".join(part for part in (preamble, notice, tail) if part)
    raw_count = reconsideration.get("new_channel_context_count")
    returned_count = (
        raw_count
        if isinstance(raw_count, int) and not isinstance(raw_count, bool)
        else 0
        if not body
        else None
    )
    lines.extend(
        _window_lines(
            "held_new_context",
            body or _empty_message_group(target_ref, context_version=context_version),
            context_version=context_version,
            returned_count=returned_count,
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


def _assemble_send_result(
    result: Mapping[str, Any],
    reconsideration: Mapping[str, Any],
    *,
    context_version: int,
    state: str,
    held_lines: list[str],
) -> str:
    lines = [
        _send_result_header(
            result,
            reconsideration,
            context_version=context_version,
        )
    ]
    _, target_lines = _target_lines(reconsideration)
    lines.extend(target_lines)
    lines.extend(
        _result_detail_lines(
            result,
            reconsideration,
            context_version=context_version,
        )
    )
    lines.extend(held_lines)
    admission_marker = result.get("tool_result_admission")
    if isinstance(admission_marker, str) and admission_marker:
        lines.append(admission_marker)
    lines.append(
        f"[end_send_result context_version={context_version} state={_json(state)}]"
    )
    return "\n".join(lines)


def format_send_result(result: Mapping[str, Any]) -> str:
    """Render one send result without exposing its nested transport object."""
    reconsideration = _reconsideration(result)
    context_version = _context_version(reconsideration)
    state = str(result.get("state") or "unknown")
    target_ref, _ = _target_lines(reconsideration)

    held_lines: list[str] = []
    if state == "held" and reconsideration:
        held_lines = _held_result_lines(
            reconsideration,
            target_ref=target_ref,
            context_version=context_version,
        )
    text = _assemble_send_result(
        result,
        reconsideration,
        context_version=context_version,
        state=state,
        held_lines=held_lines,
    )
    if len(text) <= _HELD_INLINE_BUDGET_CHARS or not held_lines:
        return text

    spilled_to = _spill_held_text(text)
    overflow_note = (
        f"[held_overflow context_version={context_version} "
        f"spilled_to={_json(spilled_to)} "
        f'note="full held result exceeded the inline budget; the draft '
        f'echo and prior basis are in the spill file"]'
    )
    # Tiers tighten until the result provably fits: body caps first, then
    # message-count caps, ending at a headers-only floor whose size is
    # bounded by construction — the receipt marker is inline in every tier.
    # That final bound assumes the non-message inline part (result header,
    # target/detail lines, participation, guidance, spill pointers, this
    # note, and the marker) stays well under the budget; those are fixed
    # platform strings today, so keep them small if they ever grow.
    for content_cap, max_messages in (
        (_HELD_CONTENT_CAP_CHARS, None),
        (_HELD_CONTENT_FLOOR_CHARS, None),
        (_HELD_CONTENT_FLOOR_CHARS, _HELD_MAX_INLINE_MESSAGES),
        (_HELD_CONTENT_FLOOR_CHARS, 0),
    ):
        compact = _assemble_send_result(
            result,
            reconsideration,
            context_version=context_version,
            state=state,
            held_lines=[overflow_note] + _held_result_lines_compact(
                reconsideration,
                target_ref=target_ref,
                context_version=context_version,
                spilled_to=spilled_to,
                content_cap=content_cap,
                max_messages=max_messages,
            ),
        )
        if len(compact) <= _HELD_INLINE_BUDGET_CHARS:
            return compact
    return compact


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
