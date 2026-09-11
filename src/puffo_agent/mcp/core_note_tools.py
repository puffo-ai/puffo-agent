"""Sticky-note MCP tools: add_note / get_channel_notes / get_thread_notes."""

from __future__ import annotations

import re
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from ..agent.send_models import SemanticSendRequest
from .data_client import DataNotFound
from .puffo_core_tools import (
    _dispatch_semantic_send,
    _resolve_channel_space,
    _ts_to_iso,
)
from .tool_result_projection import ToolResultSurface, project_send_result

# Preset -> (pill color hex, fixed label). Mirrors the web client's
# NOTE_PRESETS (parse-note-command.ts).
NOTE_PRESETS: dict[str, tuple[str, str]] = {
    "waiting": ("#db4cac", "Waiting"),
    "processing": ("#fde047", "Processing"),
    "complete": ("#c9f748", "Complete"),
}
NOTE_LABEL_MAX = 32


def _format_note(color: str, label: str, message: str, mentions: list[str]) -> str:
    """Build a canonical ``/note`` body — the same wire format the web
    client parses into a pill (parse-note-command.ts)."""
    # Marker line is "/note " — the trailing space is load-bearing.
    lines = ["/note ", f"color: {color}", f"label: {label}"]
    if message:
        # Continuation lines carry a two-space indent, matching
        # ``formatNoteCommand``: it stops a body line that happens to
        # read as ``label: …`` from terminating the message on parse.
        first, *rest = message.split("\n")
        lines.append(f"message: {first}")
        lines.extend(f"  {line}" for line in rest)
    if mentions:
        lines.append("mentions: " + " ".join(f"@{m.lstrip('@')}" for m in mentions))
    return "\n".join(lines)


_NOTE_FIELDS = ("color", "label", "message", "mentions")
_NOTE_FIELD_RE = re.compile(r"^([a-zA-Z]+)\s*:")


def _starts_note_field(line: str) -> bool:
    """PUF-417: does this line open a recognized note field? Only those
    end a multi-line ``message:`` body, so prose like ``TODO: ...`` or a
    URL with a port stays part of the message.

    Matched against the raw line, deliberately not a stripped one: the
    web composer escapes a continuation line that would otherwise look
    like a field by indenting it two spaces, and stripping first would
    defeat that escape and truncate the body.
    """
    m = _NOTE_FIELD_RE.match(line)
    return bool(m) and m.group(1).lower() in _NOTE_FIELDS


def _decode_message_line(line: str) -> str:
    """Undo the two-space continuation indent the web side adds
    (``decodeMessageLine`` in parse-note-command.ts)."""
    return line[2:] if line.startswith("  ") else line


def _parse_note(content: Any) -> Optional[dict[str, Any]]:
    """Extract label/message/mentions from a ``/note`` body, or None if
    it isn't a note. Only a leading ``"/note "`` (with the space) marks
    a note — bare ``/note`` or ``/notebook...`` don't."""
    text = str(content or "").replace("\r\n", "\n")
    if not text.startswith("/note "):
        return None
    body = text.split("\n")[1:]
    fields: dict[str, str] = {}
    mentions: list[str] = []
    i = 0
    while i < len(body):
        s = body[i].strip()
        if not s or ":" not in s:
            i += 1
            continue
        key, _, val = s.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "mentions":
            for tok in val.split():
                tok = tok.lstrip("@")
                if tok and tok not in mentions:
                    mentions.append(tok)
        elif key == "message":
            # PUF-417: the body runs to the next recognized field or to
            # the end; blank lines are content (paragraph breaks).
            # Mirrors the web parser in parse-note-command.ts.
            if "message" not in fields:
                chunk = [val]
                while i + 1 < len(body) and not _starts_note_field(body[i + 1]):
                    i += 1
                    chunk.append(_decode_message_line(body[i]))
                fields["message"] = "\n".join(chunk).strip()
        elif key in ("color", "label"):
            fields.setdefault(key, val)
        i += 1
    return {
        "label": fields.get("label") or "Note",
        "message": fields.get("message", ""),
        "mentions": mentions,
    }


def _fmt_note_line(m: Any) -> str:
    """One output line for a note message (both note read tools share it)."""
    note = _parse_note(m.content) or {"label": "Note", "message": "", "mentions": []}
    ts = _ts_to_iso(m.sent_at)
    root = m.thread_root_id or m.envelope_id
    body = str(note["message"]).replace("\n", " ")
    tail = (
        "  for " + " ".join(f"@{x}" for x in note["mentions"])
        if note["mentions"]
        else ""
    )
    return (
        f"{ts}  note:{m.envelope_id}  thread:{root}  "
        f"[{note['label']}] @{m.sender_slug}: {body}{tail}"
    )


def _resolve_note_style(
    preset: str,
    color: str,
    label: str,
    mentions: list[str],
    slug: str,
) -> tuple[str, str, list[str]]:
    """Validate preset/color/label/mentions and return the note's
    (color, label, mentions)."""
    preset_key = (preset or "").strip().lower()
    if color:
        if preset_key:
            raise RuntimeError("color and preset are mutually exclusive — pass one")
        if not label:
            raise RuntimeError("a custom color requires a label")
        return color, label[:NOTE_LABEL_MAX], mentions
    if label:
        raise RuntimeError(
            "label is only for a custom note — pass a color too, or use a preset"
        )
    preset_key = preset_key or "waiting"
    if preset_key not in NOTE_PRESETS:
        raise RuntimeError("preset must be one of: waiting, processing, complete")
    note_color, note_label = NOTE_PRESETS[preset_key]
    if preset_key in ("processing", "complete"):
        if mentions:
            raise RuntimeError(
                f"{preset_key} notes are self-reports; don't pass mentions"
            )
        return note_color, note_label, [slug]
    return note_color, note_label, mentions  # waiting


def register_note_tools(
    mcp: FastMCP,
    cfg: Any,
    *,
    result_surface: ToolResultSurface = "stdio_mcp",
) -> None:
    _register_note_read_tools(mcp, cfg)
    _register_add_note(mcp, cfg, result_surface=result_surface)


def _register_note_read_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def get_channel_notes(channel: str, limit: int = 20) -> str:
        """List the **active sticky-notes** in a channel — one per
        thread (the note currently in effect), newest-first.

        A note is a short status marker on a thread, shown as a colored
        pill in human clients: a label (Waiting / Processing / Complete),
        an optional message, and @mentions. Use this to scan what's
        pending across a channel without reading every thread; drill
        into one thread's note history with ``get_thread_notes``.

        ``channel`` is a raw ``ch_<uuid>`` (no ``#name`` shortcut).
        Output lines:
        ``<ts>  note:<id>  thread:<root>  [<label>] @<sender>: <msg>  for @a @b``."""
        limit = max(1, min(int(limit), 200))
        channel_ref = channel.strip()
        if channel_ref.startswith("#"):
            raise RuntimeError(
                "'#<name>' channel addressing isn't supported; pass the "
                "channel id directly."
            )
        if not channel_ref.startswith("ch_"):
            await _resolve_channel_space(cfg, channel_ref)
        try:
            notes = await cfg.data_client.get_channel_notes(channel_ref, limit=limit)
        except DataNotFound:
            return f"(no such channel: {channel_ref})"
        if not notes:
            return "(no active notes in this channel)"
        return "\n".join(_fmt_note_line(m) for m in notes)

    @mcp.tool()
    async def get_thread_notes(root_id: str, limit: int = 20) -> str:
        """List the ``/note`` status markers on one thread, newest-first.
        ``limit=1`` returns just the note currently in effect (the latest
        wins, like stacking sticky-notes).

        ``root_id`` is the thread root envelope_id (``msg_<uuid>``).
        Output lines:
        ``<ts>  note:<id>  thread:<root>  [<label>] @<sender>: <msg>  for @a @b``."""
        if not root_id.strip():
            raise RuntimeError("root_id required")
        limit = max(1, min(int(limit), 200))
        try:
            notes = await cfg.data_client.get_thread_notes(root_id.strip(), limit=limit)
        except DataNotFound:
            return f"(no such thread: {root_id.strip()})"
        if not notes:
            return "(no notes on this thread)"
        return "\n".join(_fmt_note_line(m) for m in notes)


def _register_add_note(
    mcp: FastMCP,
    cfg: Any,
    *,
    result_surface: ToolResultSurface,
) -> None:
    @mcp.tool(structured_output=False)
    async def add_note(
        root_id: str,
        preset: str = "",
        message: str = "",
        mentions: Optional[list[str]] = None,
        color: str = "",
        label: str = "",
    ) -> Any:
        """Post a **sticky-note** onto a thread — a status marker that
        shows as a colored pill in human clients. The newest note on a
        thread is the one in effect; it supersedes older ones.

        Pass EITHER a ``preset`` OR a custom ``color`` (+ ``label``);
        the two are mutually exclusive. With neither, defaults to
        ``waiting``.

        - ``root_id`` — the thread root envelope_id (``msg_<uuid>``) the
          note is about. Posted as a reply in that thread.
        - ``preset`` — ``waiting`` | ``processing`` | ``complete``:
          - ``waiting`` (pink) — blocked on someone: ``mentions`` = who
            should act, ``message`` = what to do.
          - ``processing`` (yellow) — you're working on it (self-report).
            **Passing ``mentions`` is rejected**; the mention is you.
          - ``complete`` (green) — done (self-report). **Passing
            ``mentions`` is rejected**; the mention is you. ``message``
            = the delivery summary.
        - ``color`` — a custom pill color (hex, e.g. ``#38bdf8``) for a
          status that doesn't fit a preset. Requires ``label`` (<=32
          chars) and must not be combined with a preset. Custom notes
          take ``mentions`` freely.
        - ``label`` — the pill text; **required with ``color``**, and
          only valid for a custom note (presets set their own label).
        - ``message`` — the note body.
        - ``mentions`` — slugs who should act (waiting + custom only).

        See the managed ``use-puffo-notes`` skill for the preset
        protocol and typical flow."""
        mention_list = [m.lstrip("@") for m in (mentions or []) if m and m.strip()]
        note_color, note_label, note_mentions = _resolve_note_style(
            preset, color.strip(), label.strip(), mention_list, cfg.slug,
        )
        note_text = _format_note(note_color, note_label, message.strip(), note_mentions)

        root_ref = root_id.strip()
        if not root_ref:
            raise RuntimeError("root_id required")
        root_msg = await cfg.data_client.get_message_by_envelope(root_ref)
        if root_msg is None:
            raise RuntimeError(f"unknown root message: {root_ref}")
        if root_msg.channel_id:
            channel_ref = root_msg.channel_id
        elif root_msg.envelope_kind == "dm":
            channel_ref = (
                f"@{root_msg.recipient_slug}"
                if root_msg.sender_slug == cfg.slug
                else f"@{root_msg.sender_slug}"
            )
        else:
            raise RuntimeError("cannot resolve a channel/DM for that root")
        # Notes are status markers meant for humans → always sent visible.
        return project_send_result(
            await _dispatch_semantic_send(
                cfg,
                SemanticSendRequest(
                    destination=channel_ref,
                    text=note_text,
                    root_id=root_ref,
                    visibility_level="human",
                ),
                tool_name="add_note",
            ),
            surface=result_surface,
        )
