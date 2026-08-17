"""PUF-417: a wrapped ``/note`` body kept only its first line.

The parser walks the body matching ``key: value`` per line and skips
anything that doesn't match, so every continuation line of a ``message:``
was dropped. The web client (parse-note-command.ts) had the same defect;
both sides must agree on the wire format or notes stop round-tripping
between an agent and a browser.
"""
from __future__ import annotations

import pytest

from puffo_agent.mcp.puffo_core_tools import _format_note, _parse_note


def note(message: str, *, mentions: list[str] | None = None) -> str:
    return _format_note("#db4cac", "Waiting", message, mentions or [])


def test_multi_line_body_keeps_every_line():
    parsed = _parse_note("/note \ncolor: #db4cac\nlabel: Waiting\nmessage: first\nsecond\nthird")
    assert parsed is not None
    assert parsed["message"] == "first\nsecond\nthird"


def test_body_stops_at_the_next_recognized_field():
    parsed = _parse_note("/note \nmessage: first\nsecond\nmentions: @alice-0001")
    assert parsed is not None
    assert parsed["message"] == "first\nsecond"
    assert parsed["mentions"] == ["alice-0001"]


def test_body_does_not_stop_on_a_colon_that_is_not_a_field():
    """A note is prose; a colon in it is ordinary. Stopping on any colon
    would truncate half the notes the fleet writes."""
    parsed = _parse_note(
        "/note \nmessage: check this\nTODO: fix the thing\nsee https://x.dev/a:b\ndone"
    )
    assert parsed is not None
    assert parsed["message"] == "check this\nTODO: fix the thing\nsee https://x.dev/a:b\ndone"


def test_blank_lines_inside_the_body_are_kept():
    parsed = _parse_note("/note \nmessage: para one\n\npara two")
    assert parsed is not None
    assert parsed["message"] == "para one\n\npara two"


def test_single_line_note_parses_as_before():
    parsed = _parse_note(
        "/note \ncolor: #c9f748\nlabel: Complete\nmessage: all done\nmentions: @bob-0001"
    )
    assert parsed == {"label": "Complete", "message": "all done", "mentions": ["bob-0001"]}


def test_note_without_a_body():
    parsed = _parse_note("/note \ncolor: #eee\nlabel: Note")
    assert parsed is not None
    assert parsed["message"] == ""


def test_non_note_content_still_rejected():
    assert _parse_note("/notebook something") is None
    assert _parse_note("hello") is None


@pytest.mark.parametrize("n", [1, 2, 5, 10])
def test_round_trip_preserves_every_line(n: int):
    message = "\n".join(f"line {i + 1}" for i in range(n))
    parsed = _parse_note(note(message, mentions=["alice-0001"]))
    assert parsed is not None
    assert parsed["message"] == message
    assert parsed["mentions"] == ["alice-0001"]


def test_round_trip_with_blank_line_and_stray_colon():
    message = "intro\n\nNOTE: see below\ntail"
    parsed = _parse_note(note(message))
    assert parsed is not None
    assert parsed["message"] == message


def test_message_is_written_before_the_mentions_that_end_it():
    wire = note("a\nb", mentions=["x-0001"])
    assert wire.index("message:") < wire.index("mentions:")


# --- Wire-format symmetry with the web composer -------------------------
#
# parse-note-command.ts ``formatNoteCommand`` indents every continuation
# line by two spaces, and its ``decodeMessageLine`` strips them back off.
# That indent is also the escape hatch for a continuation line that would
# otherwise read as a field. Both halves have to exist on this side too,
# or notes composed in the browser arrive corrupted here.

WEB_WIRE = (
    "/note \n"
    "color: #c9f748\n"
    "label: Complete\n"
    "message: first line\n"
    "  second line\n"
    "  third line\n"
    "mentions: @alice-0001"
)


def test_decodes_the_web_continuation_indent():
    parsed = _parse_note(WEB_WIRE)
    assert parsed is not None
    assert parsed["message"] == "first line\nsecond line\nthird line"
    assert parsed["mentions"] == ["alice-0001"]


def test_indented_field_name_stays_prose_instead_of_truncating():
    # The web side escapes this exact case by indenting. Treating the
    # indented line as a terminator reintroduces the PUF-417 bug through
    # the escape channel: the body silently loses everything after it.
    parsed = _parse_note(
        "/note \nlabel: Complete\nmessage: intro\n  label: this is prose\n  tail line"
    )
    assert parsed is not None
    assert parsed["message"] == "intro\nlabel: this is prose\ntail line"
    assert parsed["label"] == "Complete"


# --- Gaps Solution surfaced at QA ---------------------------------------


def test_only_the_first_message_field_wins():
    parsed = _parse_note("/note \nmessage: first\nmessage: second")
    assert parsed is not None
    assert parsed["message"] == "first"


@pytest.mark.parametrize("terminator", ["color: #fff", "label: Done", "mentions: @bob-0002"])
def test_every_field_name_terminates_the_body(terminator: str):
    parsed = _parse_note(f"/note \nmessage: body line\n{terminator}")
    assert parsed is not None
    assert parsed["message"] == "body line"


def test_field_terminator_is_case_insensitive():
    # Decision, not an accident: ``_starts_note_field`` lowercases, which
    # mirrors ``VALID_KEYS.has(m[1].toLowerCase())`` on the web side. An
    # unindented ``MESSAGE:`` therefore ends the body; indent it to keep
    # it as prose.
    parsed = _parse_note("/note \nmessage: body\nMENTIONS: @bob-0002")
    assert parsed is not None
    assert parsed["message"] == "body"


def test_unrecognized_key_with_a_colon_stays_in_the_body():
    parsed = _parse_note("/note \nmessage: see\nTODO: not a field\nhttp://x.test:8080/p")
    assert parsed is not None
    assert parsed["message"] == "see\nTODO: not a field\nhttp://x.test:8080/p"


# --- The write side needs the same escape ------------------------------


def test_format_indents_continuation_lines_like_the_web_encoder():
    wire = _format_note("#c9f748", "Complete", "first\nsecond\nthird", [])
    assert wire.split("\n")[3:] == ["message: first", "  second", "  third"]


def test_round_trip_survives_a_body_line_that_reads_as_a_field():
    # Without the encoder's indent this truncates at "label:" the moment
    # the browser parses it — the same defect as the read side, mirrored.
    message = "intro\nlabel: not a field\ntail"
    assert _parse_note(_format_note("#c9f748", "Complete", message, []))["message"] == message


@pytest.mark.parametrize(
    "message",
    [
        "single",
        "two\nlines",
        "blank\n\nline in the middle",
        "trailing colon:\nnext",
        "first\n    deeply indented continuation",
    ],
)
def test_format_parse_round_trip(message: str):
    parsed = _parse_note(note(message, mentions=["alice-0001"]))
    assert parsed is not None
    assert parsed["message"] == message
    assert parsed["mentions"] == ["alice-0001"]


def test_leading_indent_on_the_first_line_is_not_preserved():
    # Known limit of the wire format, pinned rather than fixed: the value
    # starts after ``message:`` and both sides eat the whitespace there
    # (the web regex is ``:\s*(.*)``). Continuation lines keep any indent
    # beyond the two-space marker; only the first line can't. Fixing it
    # would need a format change on both sides, which this ticket isn't.
    parsed = _parse_note(note("  indented first\n    second"))
    assert parsed is not None
    assert parsed["message"] == "indented first\n    second"
