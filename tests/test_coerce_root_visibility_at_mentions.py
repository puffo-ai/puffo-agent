"""PUF-202: ``_coerce_root_visibility`` coerces ``false`` → ``true``
when the body @-mentions a slug the local known-agents cache hasn't
tagged as a peer agent. Default (empty cache) → all @-mentions
coerce — the safe floor for the FB-130 family of bugs where an
agent's ``@<human-slug> please do X`` got folded away from the human
it explicitly addressed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from puffo_agent.mcp.puffo_core_tools import (
    _AT_MENTION_RE,
    _KNOWN_AGENT_SLUGS,
    _coerce_root_visibility,
    _extract_at_mentioned_slugs,
    _set_known_agent_slugs,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Cache is module-level state; reset between tests so cases
    don't bleed into each other."""
    _set_known_agent_slugs(set())
    yield
    _set_known_agent_slugs(set())


# ── _extract_at_mentioned_slugs: regex behaviour ─────────────────


def test_extract_finds_bare_at_mention():
    assert _extract_at_mentioned_slugs("hi @bob-1234 please review") == ["bob-1234"]


def test_extract_finds_mention_after_open_paren():
    # Equation's edge case: the @ following punctuation should still match.
    assert _extract_at_mentioned_slugs("see (@bob-1234) for context") == ["bob-1234"]


def test_extract_finds_mention_after_quote_marker():
    # Markdown blockquote / reply prefix.
    assert _extract_at_mentioned_slugs("> @bob-1234 said earlier") == ["bob-1234"]


def test_extract_finds_mention_after_bullet():
    assert _extract_at_mentioned_slugs("- @bob-1234 owns this") == ["bob-1234"]


def test_extract_case_insensitive_but_lowercases_output():
    # Equation's edge case: case-mismatched at-mention should still
    # match, and we should canonicalize to lowercase for cache lookup.
    assert _extract_at_mentioned_slugs("hi @Equation-7256-87f7") == ["equation-7256-87f7"]
    assert _extract_at_mentioned_slugs("hi @EQUATION-7256-87f7") == ["equation-7256-87f7"]


def test_extract_finds_multiple_mentions_in_order():
    assert _extract_at_mentioned_slugs(
        "@alice-1 and @bob-2 — let's coordinate"
    ) == ["alice-1", "bob-2"]


def test_extract_ignores_email_address():
    # ``foo@bar.com`` shouldn't trigger a mention — the @ here is in
    # the middle of a word, not at a word boundary.
    assert _extract_at_mentioned_slugs("ping me at foo@bar.com") == []


def test_extract_ignores_lone_at_sign():
    assert _extract_at_mentioned_slugs("price @ market") == []


def test_extract_empty_body():
    assert _extract_at_mentioned_slugs("") == []
    assert _extract_at_mentioned_slugs(None) == []  # type: ignore[arg-type]


# ── _coerce_root_visibility: PUF-200's original root-level rule ──


def test_coerce_root_level_false_still_coerces():
    """Pre-PUF-202 behaviour: ``is_visible_to_human=False`` on a
    root-level send (empty ``root_id``) coerces to True with the
    original "messages can't fold" note. PUF-202 must not regress."""
    visible, note = _coerce_root_visibility(False, "")
    assert visible is True
    assert "can't fold" in note


def test_coerce_visible_true_is_passthrough():
    visible, note = _coerce_root_visibility(True, "msg_root", body="@bob hello")
    assert visible is True
    assert note == ""


# ── PUF-202: body @-mention coercion ─────────────────────────────


def test_coerce_threaded_false_no_mentions_passes_through():
    """Threaded reply, no @-mentions, empty cache — original
    semantics. False stays False, no coercion."""
    visible, note = _coerce_root_visibility(
        False, "msg_root", body="Got it, will look.",
    )
    assert visible is False
    assert note == ""


def test_coerce_threaded_false_human_mention_coerces():
    """The FB-130 medical-harm case: agent threads a reply, sets
    visible=false, and @-mentions a human. Pre-fix the human
    never saw the message. Post-PUF-202 we coerce visible."""
    visible, note = _coerce_root_visibility(
        False, "msg_root", body="@sam-1234 please give Maoyi Motrin 7.5ml",
    )
    assert visible is True
    assert "coerced to true" in note
    assert "sam-1234" in note
    assert "known-agents cache" in note


def test_coerce_threaded_false_known_agent_does_not_coerce():
    """If every @-mention is a known peer agent, leave the
    visibility flag alone — preserves the operator's agent-to-agent
    invisibility rule."""
    _set_known_agent_slugs({"equation-7256-87f7"})
    visible, note = _coerce_root_visibility(
        False, "msg_root", body="@equation-7256-87f7 ack",
    )
    assert visible is False
    assert note == ""


def test_coerce_threaded_false_mixed_known_unknown_coerces():
    """If ANY @-mention is unknown, coerce. Better to err visible
    when even one of the mentions might be a human."""
    _set_known_agent_slugs({"equation-7256-87f7"})
    visible, note = _coerce_root_visibility(
        False, "msg_root",
        body="@equation-7256-87f7 and @sam-1234 — both should see this",
    )
    assert visible is True
    assert "coerced to true" in note
    # The note names the *first* unknown slug as the trigger.
    assert "sam-1234" in note


def test_coerce_cache_miss_on_known_agent_still_coerces():
    """Equation's specifically-asked test: in a fresh worker
    session, the cert cache hasn't warmed up yet, so a peer-agent
    @-mention coerces visible. That's the right safe-fail — the
    note tells the agent the cache might catch up later."""
    # Cache empty (no _set_known_agent_slugs call).
    visible, note = _coerce_root_visibility(
        False, "msg_root", body="@equation-7256-87f7 see above",
    )
    assert visible is True
    assert "coerced to true" in note
    assert "subsequent sends should fold normally" in note


def test_coerce_case_mismatched_at_mention_uses_canonical_lookup():
    """Equation's edge case: ``@Equation-7256-87f7`` lowercases to
    ``equation-7256-87f7`` for the cache lookup, so a properly-
    cached peer agent isn't accidentally coerced."""
    _set_known_agent_slugs({"equation-7256-87f7"})
    visible, _ = _coerce_root_visibility(
        False, "msg_root", body="hi @Equation-7256-87f7",
    )
    assert visible is False


def test_coerce_at_mention_after_punctuation_still_triggers():
    """Equation's edge case: ``(@slug)`` parens + leading-quote ``>``."""
    visible, note = _coerce_root_visibility(
        False, "msg_root", body="(@sam-1234) — see this",
    )
    assert visible is True
    assert "sam-1234" in note

    visible, note = _coerce_root_visibility(
        False, "msg_root", body="> @sam-1234 said earlier",
    )
    assert visible is True
    assert "sam-1234" in note


def test_coerce_root_level_takes_precedence_over_mention_coercion():
    """Empty root_id wins — gets the original 'can't fold' note,
    not the new @-mention note. Saves the caller a meaningless
    'this is a known agent so no coercion needed' path on a root
    send where the flag was meaningless anyway."""
    _set_known_agent_slugs({"equation-7256-87f7"})
    visible, note = _coerce_root_visibility(
        False, "", body="@equation-7256-87f7 hello",
    )
    assert visible is True
    assert "can't fold" in note
    # The PUF-202 note isn't included since the root-level rule
    # already coerced.
    assert "coerced to true" not in note


def test_coerce_empty_body_passes_through():
    """No body → no @-mentions → no PUF-202 coercion. False stays
    False (assuming a threaded send)."""
    visible, note = _coerce_root_visibility(False, "msg_root", body="")
    assert visible is False
    assert note == ""


def test_coerce_email_in_body_does_not_trigger():
    """An email address (e.g. ``foo@bar.com``) shouldn't trip the
    coercion — the @ here is mid-word, not at a mention boundary."""
    visible, note = _coerce_root_visibility(
        False, "msg_root", body="reach me at foo@bar.com",
    )
    assert visible is False
    assert note == ""


# ── _set_known_agent_slugs: cache contract ────────────────────────


def test_set_known_agent_slugs_replaces_state():
    _set_known_agent_slugs({"alice-1"})
    assert "alice-1" in _KNOWN_AGENT_SLUGS

    _set_known_agent_slugs({"bob-2"})
    assert "alice-1" not in _KNOWN_AGENT_SLUGS  # replaced, not appended
    assert "bob-2" in _KNOWN_AGENT_SLUGS


def test_set_known_agent_slugs_normalises_to_lowercase():
    _set_known_agent_slugs({"Alice-1234", "BOB-5678"})
    assert "alice-1234" in _KNOWN_AGENT_SLUGS
    assert "bob-5678" in _KNOWN_AGENT_SLUGS


def test_set_known_agent_slugs_ignores_empty_strings():
    _set_known_agent_slugs({"", "  ", "alice-1"})
    assert _KNOWN_AGENT_SLUGS == {"alice-1"}
