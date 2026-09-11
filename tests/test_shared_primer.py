import tempfile
from pathlib import Path

from puffo_agent.agent.shared_content import (
    DEFAULT_SHARED_CLAUDE_MD,
    DEFAULT_SKILLS,
    HELD_SEND_RECONSIDERATION_GUIDANCE,
    INBOX_TURN_CUE,
    ensure_shared_primer,
    rebuild_agent_claude_md,
    rebuild_agent_codex_md,
)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _rebuild(
    root: Path, *, workspace_shared_status: str = "existing"
) -> tuple[str, str]:
    profile = root / "profile.md"
    profile.write_text("# Soul\nSOUL-MARKER-7b", encoding="utf-8")
    memory = root / "memory"
    memory.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    common = dict(
        shared_dir=root / "shared", profile_path=profile, memory_dir=memory,
        workspace_dir=workspace, agent_id="AGENT-ID-MARKER-8c",
        display_name="DISPLAY-NAME-MARKER-9d", role="LONG-ROLE-MARKER-ae",
        role_short="SHORT-ROLE-MARKER-bf",
        workspace_shared_status=workspace_shared_status,
    )
    claude = rebuild_agent_claude_md(
        **common, claude_user_dir=root / ".claude", gemini_user_dir=root / ".gemini",
    )
    (memory / "topic.md").write_text(
        "FLAT-TOPIC-MARKER-c0", encoding="utf-8",
    )
    claude = rebuild_agent_claude_md(
        **common, claude_user_dir=root / ".claude", gemini_user_dir=root / ".gemini",
    )
    codex = rebuild_agent_codex_md(**common, codex_user_dir=root / ".codex")
    return claude, codex


def test_standing_prompt_contains_runtime_identity_profile_and_flat_memory():
    claude, codex = _rebuild(_tmp())
    for text in (claude, codex):
        for marker in (
            "DISPLAY-NAME-MARKER-9d", "AGENT-ID-MARKER-8c", "LONG-ROLE-MARKER-ae",
            "SHORT-ROLE-MARKER-bf", "SOUL-MARKER-7b", "FLAT-TOPIC-MARKER-c0",
        ):
            assert text.count(marker) == 1
        assert "# Your role" in text
        assert "# Your memory" in text


def test_standing_prompt_owns_communication_policy_and_retains_contract():
    primer = " ".join(DEFAULT_SHARED_CLAUDE_MD.split())
    for phrase in (
        "<global_inbox_notice>", "content_included=false", "read_inbox",
        "read_history",
        "context_version=1", "target_ref", "sender_identity", "sender_type",
        "An `@slug` identity is unique", "send_message",
        "A metadata-only notice means unread content exists",
        "When you receive a concrete message",
        "visible acknowledgment", "For multi-step work",
        "report the outcome", "material ambiguity",
        "Agent messages may legitimately trigger further Agent work",
        "choose one unclaimed part that best fits your role",
        "A cover is your explicit declaration",
        "mark_covered",
        "uncovered_redelivery=true",
        "A claim becomes visible only after it is successfully sent",
        "ordinary assistant text is not delivered",
        "message comes from an existing thread",
        "message_id` as `root_id",
    ):
        assert phrase in primer
    for absent in ("decide-response", "[SILENT]", "Send, Clarify, Wait, or Silent"):
        assert absent not in primer
    assert "~/.puffo-agent/shared" not in primer
    assert "/workspace/.shared" not in primer
    for absent in (
        "prior_context", "visible_draft_basis", "new_channel_context",
        "context_ready", "same originating assignment", "send_anyway=True",
        "mcp__puffo__list_reminders", "mcp__puffo__cancel_reminder",
    ):
        assert absent not in DEFAULT_SHARED_CLAUDE_MD

    read = DEFAULT_SKILLS["read-messages"][1]
    post = DEFAULT_SKILLS["get-post"][1]
    send = DEFAULT_SKILLS["send-message"][1]
    for phrase in (
        "context_version", "## context", "target_ref", "seq", "sent_at",
        "message_id", "sender_identity", "sender_type", "self", "encrypted",
        "[event]", "body",
    ):
        assert phrase in read
    assert "read-messages" in post
    normalized_read = " ".join(read.split())
    assert "pending_messages" in read and "earlier_context" not in read
    assert "has_older" in read and "has_newer" in read and "has_next" in read
    assert "pinned Inbox snapshot" in read
    assert "pending" in read and "history" in read
    assert "decide-response" not in DEFAULT_SKILLS
    assert "originating request and conversation intent" not in DEFAULT_SHARED_CLAUDE_MD
    assert "canonical view of pending work" in " ".join(post.split())
    normalized_send = " ".join(send.lower().split())
    for phrase in (
        'state="held"', "unchanged `[draft]`", "participation context",
        "held_basis", "held_new_context", "context_ready",
        "held-reconsideration guidance", "included only when a draft is actually held",
        "sequence watermark alone is not semantic context",
        "preserve the inbox target by default",
        'omit it for `target_type="channel"`',
        'pass the supplied `thread_root_id` for `target_type="thread"`',
    ):
        assert phrase.lower() in normalized_send
    assert "common held-send procedure" in DEFAULT_SKILLS["send-message-with-attachments"][1]
    assert "A held draft was attempted but not sent" not in send

    for text in _rebuild(_tmp()):
        assert "`shared/` inside it is the host-wide collaboration directory" in text


def test_standing_prompt_reports_unavailable_shared_workspace_truthfully():
    conflict, _ = _rebuild(_tmp(), workspace_shared_status="conflict")
    unavailable, _ = _rebuild(_tmp(), workspace_shared_status="unavailable")

    assert "not connected to the host-wide collaboration directory" in conflict
    assert "host-wide collaboration directory is unavailable" in unavailable


def test_inbox_turn_cue_is_short_and_reinforces_the_standing_default():
    cue = " ".join(INBOX_TURN_CUE.split())
    assert "<puffo_runtime_instruction>" in cue
    assert "notice above contains metadata only" in cue.lower()
    assert "call `read_inbox` now" in cue.lower()
    assert "do not finish this turn from notice metadata alone" in cue.lower()
    assert "use `read_history` only if earlier context is needed" in cue.lower()
    assert "decide-response" not in cue
    assert "dispose of every human message" in cue.lower()
    assert "`covers`" in cue
    assert "`mark_covered`" in cue
    assert len(INBOX_TURN_CUE.encode()) < 640


def test_held_send_applies_the_shared_judgment_to_the_attempted_draft():
    held_method = " ".join(HELD_SEND_RECONSIDERATION_GUIDANCE.split()).lower()
    send = " ".join(DEFAULT_SKILLS["send-message"][1].split()).lower()
    assert held_method not in send
    for phrase in (
        "follow the returned held-reconsideration guidance",
        "included only when a draft is actually held",
    ):
        assert phrase in send
    for phrase in (
        "attempted but not sent", "reconsider the originating interaction",
        "send_anyway=true", "is rare",
        "if the draft was a claim", "it did not establish ownership",
        "make it durable with a suitable reminder",
        "if no visible response is useful, do not send one",
        "it neither creates nor settles a participation obligation",
        "decide those questions independently",
        "rather than treating overlapping peer content as your participation",
    ):
        assert phrase in held_method
    assert "Confidence is not evidence" not in held_method

    root = _tmp()
    _rebuild(root)
    for path in (
        root / "workspace" / ".claude" / "skills" / "send-message" / "SKILL.md",
        root / "workspace" / ".agents" / "skills" / "send-message" / "SKILL.md",
    ):
        skill = " ".join(path.read_text(encoding="utf-8").split()).lower()
        assert held_method not in skill
        assert "follow the returned held-reconsideration guidance" in skill

    other_prompt_surfaces = [" ".join(DEFAULT_SHARED_CLAUDE_MD.split())]
    other_prompt_surfaces.extend(
        " ".join(body.split())
        for skill_id, (_, body) in DEFAULT_SKILLS.items()
        if skill_id != "send-message"
    )
    held_opening = "A held draft was attempted but not sent"
    assert not any(held_opening in surface for surface in other_prompt_surfaces)
    assert "new successful visible contribution from you" not in send


def test_harnesses_discover_managed_skills_with_correct_tool_names():
    root = _tmp()
    claude, codex = _rebuild(root)
    assert "mcp__puffo__" in claude
    assert "mcp__puffo__" not in codex
    assert "send_message" in codex
    ensure_shared_primer(root / "shared")
    for skill_id in ("read-messages", "send-message"):
        claude_skill = root / "workspace" / ".claude" / "skills" / skill_id / "SKILL.md"
        codex_skill = root / "workspace" / ".agents" / "skills" / skill_id / "SKILL.md"
        for skill in (claude_skill, codex_skill):
            text = skill.read_text(encoding="utf-8")
            assert "name:" in text and "description:" in text
        assert "mcp__puffo__" in claude_skill.read_text(encoding="utf-8")
        assert "mcp__puffo__" not in codex_skill.read_text(encoding="utf-8")


def test_managed_refresh_rewrites_stale_skill():
    root = _tmp()
    shared = root / "shared"
    ensure_shared_primer(shared)
    skill = shared / "skills" / "read-messages" / "SKILL.md"
    skill.write_text("stale", encoding="utf-8")
    actions = dict(ensure_shared_primer(shared))
    assert actions["skills/read-messages/SKILL.md"] == "updated"
    assert "read_history" in skill.read_text(encoding="utf-8")

    removed = shared / "skills" / "decide-response"
    removed.mkdir()
    (removed / "SKILL.md").write_text("legacy", encoding="utf-8")
    (removed / ".puffo-managed").write_text("managed", encoding="utf-8")
    actions = dict(ensure_shared_primer(shared))
    assert actions["skills/decide-response"] == "pruned"
    assert not removed.exists()
