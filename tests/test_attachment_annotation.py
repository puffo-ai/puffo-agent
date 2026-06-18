"""Attachment-line rendering: a ``<stem>.compressed<ext>`` attachment is
annotated with a pointer to its full-res ``<stem>.origin<ext>`` sibling and
guidance to crop rather than open the whole image; plain attachments aren't.
"""

from __future__ import annotations

from puffo_agent.agent.core import PuffoAgent, _origin_for_compressed


def test_origin_for_compressed():
    assert _origin_for_compressed("/x/shot.compressed.png") == "/x/shot.origin.png"
    assert _origin_for_compressed("/x/a.b.compressed.jpg") == "/x/a.b.origin.jpg"
    assert _origin_for_compressed("/x/shot.png") is None
    assert _origin_for_compressed("/x/notes.txt") is None
    assert _origin_for_compressed("noext") is None


def _block(attachments):
    # _format_user_block uses only its args (no ``self``), so an unbound call
    # with a dummy first arg renders the block in isolation.
    return PuffoAgent._format_user_block(
        object(),
        channel_name="general",
        sender="alice",
        sender_email="",
        text="see attached",
        attachments=attachments,
    )


def test_compressed_attachment_gets_origin_annotation():
    block = _block(["/inbox/env/shot.compressed.png"])
    assert "/inbox/env/shot.compressed.png" in block
    assert "/inbox/env/shot.origin.png" in block
    assert "crop a region" in block


def test_plain_attachment_has_no_annotation():
    block = _block(["/inbox/env/thumb.png"])
    assert "/inbox/env/thumb.png" in block
    assert "origin" not in block
    assert "crop a region" not in block
