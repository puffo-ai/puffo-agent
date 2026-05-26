"""PUF-247: regression coverage for ``format_invite_error``.

Sam's tier-1 symptom (raw JSON in operator-DM confirm) is captured
by the first test -- pre-fix the daemon emitted
``"Couldn't accept invite to ...: HTTP 400: {\\"error\\": \\"INVALID_PAYLOAD\\", \\"message\\": \\"channel not found: ch_...\\"}"``
which then became the agent's reply in chat. Post-fix the typed
``HttpError(400, '{"error":"INVALID_PAYLOAD","message":"channel not found: ..."}')``
maps to a softer "isn't reachable right now" sentence (deliberately
ambiguous until bug-1's root-cause discrimination lands -- see the
helper's docstring).
"""

import asyncio

from puffo_agent.agent._invite_strings import format_invite_error
from puffo_agent.crypto.http_client import HttpError


def test_channel_not_found_maps_to_friendly_text() -> None:
    exc = HttpError(
        400,
        '{"error": "INVALID_PAYLOAD", "message": "channel not found: ch_475684b6"}',
    )
    out = format_invite_error(exc, "accept")
    assert "isn't reachable right now" in out
    assert "Try again later" in out
    assert "ch_475684b6" not in out  # raw id must not leak
    assert "INVALID_PAYLOAD" not in out  # raw error code must not leak
    assert out.startswith("Couldn't accept invite:")


def test_channel_not_found_works_for_reject_verb_too() -> None:
    exc = HttpError(
        400, '{"error": "INVALID_PAYLOAD", "message": "channel not found: x"}',
    )
    out = format_invite_error(exc, "reject")
    assert out.startswith("Couldn't reject invite:")
    assert "isn't reachable right now" in out


def test_space_not_found_maps_to_friendly_text() -> None:
    exc = HttpError(
        400, '{"error": "INVALID_PAYLOAD", "message": "space not found: sp_x"}',
    )
    out = format_invite_error(exc, "accept")
    assert "space isn't reachable right now" in out
    assert "sp_x" not in out


def test_403_forbidden_maps_to_permission_text() -> None:
    exc = HttpError(403, '{"error": "FORBIDDEN", "message": "not a member"}')
    assert "permission" in format_invite_error(exc, "accept")


def test_409_conflict_maps_to_already_handled_text() -> None:
    exc = HttpError(
        409, '{"error": "CONFLICT", "message": "invitation already accepted"}',
    )
    assert "already been handled" in format_invite_error(exc, "accept")


def test_unknown_4xx_falls_back_to_generic_retry() -> None:
    exc = HttpError(422, '{"error": "WEIRD", "message": "what even"}')
    out = format_invite_error(exc, "accept")
    assert "please try again" in out
    assert "WEIRD" not in out
    assert "what even" not in out


def test_5xx_maps_to_server_issue_text() -> None:
    exc = HttpError(503, '{"error": "INTERNAL"}')
    out = format_invite_error(exc, "accept")
    assert "Puffo server hit an issue" in out
    assert "INTERNAL" not in out


def test_non_json_body_falls_back_to_status_class() -> None:
    # A proxy/CDN returning HTML on 502 shouldn't break the helper.
    exc = HttpError(502, "<html>Bad Gateway</html>")
    out = format_invite_error(exc, "accept")
    assert "Puffo server hit an issue" in out
    assert "<html>" not in out


def test_empty_body_on_4xx_still_friendly() -> None:
    exc = HttpError(400, "")
    out = format_invite_error(exc, "accept")
    assert "please try again" in out


def test_non_http_exception_falls_back_to_unexpected() -> None:
    out = format_invite_error(ValueError("boom"), "accept")
    assert "unexpected error" in out
    assert "boom" not in out


def test_timeout_error_falls_back_to_unexpected() -> None:
    # Production: the accept/reject path goes through ``http.post``
    # which can raise ``asyncio.TimeoutError`` on a slow round-trip.
    # Same fallback branch as ``ValueError`` -- pinning so the helper
    # never tries to ``isinstance(exc, HttpError)`` past these.
    out = format_invite_error(asyncio.TimeoutError(), "accept")
    assert "unexpected error" in out
    assert out.startswith("Couldn't accept invite:")


def test_returned_text_never_starts_with_HTTP_status_prefix() -> None:
    # Regression seal for the specific shape Sam saw -- the literal
    # "HTTP 400:" prefix from str(HttpError) must never leak through.
    for status in (400, 401, 403, 404, 409, 422, 500, 502, 503):
        exc = HttpError(status, '{"error": "X", "message": "y"}')
        assert "HTTP " not in format_invite_error(exc, "accept")
        assert "HTTP " not in format_invite_error(exc, "reject")


def test_returned_text_uses_ascii_colon_not_em_dash() -> None:
    # Operator PR-#43 review item 4: em-dash (U+2014) can render as
    # ``?`` or boxes on older clients / screen-readers / log
    # aggregators that mis-handle UTF-8. ASCII ``: `` everywhere.
    exc_4xx = HttpError(400, '{"message": "channel not found"}')
    exc_5xx = HttpError(503, '{}')
    for exc in (exc_4xx, exc_5xx):
        for verb in ("accept", "reject"):
            out = format_invite_error(exc, verb)
            assert "—" not in out, f"em-dash leaked for {verb}/{exc.status}: {out!r}"
            # And the prefix uses ``: `` not ``: ``-ish lookalikes.
            assert f"Couldn't {verb} invite:" in out
