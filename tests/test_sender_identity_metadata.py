"""Inbound-metadata sender-identity enrichment: ``sender_owner_slug``
fires for agent senders, ``is_from_operator`` for the agent's own
operator. ``_format_user_block`` uses only its args, so an unbound
call renders the block in isolation.
"""

from __future__ import annotations

from puffo_agent.agent.core import PuffoAgent


def _block(**over) -> str:
    args = dict(
        channel_name="general",
        sender="Nova",
        sender_email="",
        text="hi",
        attachments=None,
        sender_slug="nova-agent-1234",
    )
    args.update(over)
    # sender_slug isn't a _format_user_block param; the block uses ``sender``
    # for the slug line. Pass it through ``sender`` for the assertions.
    args.pop("sender_slug", None)
    return PuffoAgent._format_user_block(object(), **args)


def test_agent_sender_gets_owner_slug():
    block = _block(sender="nova-agent-1234", sender_owner_slug="nova-op-9999")
    assert "- sender_owner_slug: nova-op-9999" in block
    assert "- is_from_operator:" not in block
    # owner_slug is agent-only, so it flips the displayed type even
    # while the upstream sender_is_agent flag is still hardcoded False.
    assert "- sender_type: agent" in block


def test_message_from_own_operator_is_flagged():
    block = _block(sender="mingvase-8795", is_from_operator=True)
    assert "- is_from_operator: true" in block
    # A human operator has no owner_slug of their own.
    assert "- sender_owner_slug:" not in block
    assert "- sender_type: human" in block


def test_human_non_operator_gets_neither_field():
    block = _block(sender="random-human-0001")
    assert "- sender_owner_slug:" not in block
    assert "- is_from_operator:" not in block
    assert "- sender_type: human" in block


def test_agent_owned_by_current_operator_gets_both():
    # An agent whose sender IS also somehow the operator is unusual, but the
    # two annotations are independent — both fire when both conditions hold.
    block = _block(
        sender="agt-of-op-0001",
        sender_owner_slug="mingvase-8795",
        is_from_operator=True,
    )
    assert "- sender_owner_slug: mingvase-8795" in block
    assert "- is_from_operator: true" in block


def test_fields_sit_between_sender_type_and_visibility():
    # Ordering guard so the block stays stable for readers/tests.
    block = _block(sender="nova-agent-1234", sender_owner_slug="nova-op-9999")
    lines = block.splitlines()
    st = next(i for i, ln in enumerate(lines) if ln.startswith("- sender_type:"))
    ow = next(i for i, ln in enumerate(lines) if ln.startswith("- sender_owner_slug:"))
    vis = next(i for i, ln in enumerate(lines) if ln.startswith("- is_visible_to_human:"))
    assert st < ow < vis


def test_mention_suffixes_render_from_is_agent():
    """Mention entries carry ``is_agent`` (renamed from ``is_bot``);
    the block renders (you) / (agent) / (human) suffixes from it."""
    block = _block(mentions=[
        {"username": "me-0001", "is_agent": True, "is_self": True},
        {"username": "nova-bot-1234", "is_agent": True, "is_self": False},
        {"username": "alice-1234", "is_agent": False, "is_self": False},
    ])
    assert "  - me-0001 (you)" in block
    assert "  - nova-bot-1234 (agent)" in block
    assert "  - alice-1234 (human)" in block


def test_is_encrypted_defaults_true_in_block():
    block = _block()  # no is_encrypted passed → legacy/default
    assert "- is_encrypted: true" in block


def test_plaintext_message_block_flags_is_encrypted_false():
    block = _block(is_encrypted=False)
    assert "- is_encrypted: false" in block


def test_encrypted_message_block_flags_is_encrypted_true():
    block = _block(is_encrypted=True)
    assert "- is_encrypted: true" in block
