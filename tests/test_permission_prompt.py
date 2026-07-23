"""format_permission_prompt + the prompts built through it."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from _bridge_support import isolated_home
from puffo_agent.agent.permission_prompt import format_permission_prompt


@pytest.mark.asyncio
async def test_real_init_survives_permission_reply():
    """Guard: a y/n reply must not AttributeError on a real-__init__
    client — the __new__-built test clients stub these attributes, so
    only real construction catches a missing init."""
    isolated_home()
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient

    client = PuffoCoreMessageClient(
        slug="init-smoke",
        device_id="dev_x",
        space_id="sp_x",
        keystore=None,  # type: ignore[arg-type]
        http_client=None,  # type: ignore[arg-type]
        message_store=None,  # type: ignore[arg-type]
        operator_slug="op-1",
    )
    assert await client._maybe_handle_permission_reply(
        thread_root_id="msg_unknown", text="y",
    ) is False
    assert await client._maybe_handle_dm_approval_reply(
        thread_root_id="msg_unknown", text="y",
    ) is False


def test_basic_prompt_has_prefix_and_instruction():
    text = format_permission_prompt("Do the thing?")
    assert text.startswith("/permission Do the thing? ")
    assert text.endswith("Tap Yes/No, or reply `y`/`n` in this thread.")
    assert "\n" not in text


def test_detail_renders_as_quote_block():
    text = format_permission_prompt("Run it?", detail="line one\nline two")
    head, _, tail = text.partition("\n\n")
    assert head.startswith("/permission Run it?")
    assert tail == "> line one\n> line two"


def test_reply_note_extends_instruction_line():
    text = format_permission_prompt("Accept?", reply_note="a direct `y` answers all")
    assert text.endswith(
        "Tap Yes/No, or reply `y`/`n` in this thread — a direct `y` answers all."
    )


def test_whitespace_only_detail_omitted():
    text = format_permission_prompt("Go?", detail="   \n  ")
    assert "\n" not in text


# ─── the client prompts route through the helper ──────────────────────


def _make_client(*, operator_slug: str = "op-1"):
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient

    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.operator_slug = operator_slug
    client._pending_invite_dms = {}
    client._pending_leave_dms = {}
    client._pending_command_permissions = {}
    client._timed_out_command_permissions = {}
    client._gate_left_spaces = set()
    client._last_dm_sender = ""
    client._log = logging.getLogger("permission-prompt-test")

    sent_dms: list[dict[str, Any]] = []

    async def _stub_send_dm(slug, text, root_id=""):
        env_id = f"env_{len(sent_dms) + 1}"
        sent_dms.append({"to": slug, "text": text, "root_id": root_id, "env_id": env_id})
        return {"envelope_id": env_id}

    async def _stub_fetch_display_name(slug):
        return slug.split("-")[0].title()

    async def _resolve_space_name(space_id):
        return "Team"

    client._send_dm = _stub_send_dm  # type: ignore[assignment]
    client._fetch_display_name = _stub_fetch_display_name  # type: ignore[assignment]
    client._resolve_space_name = _resolve_space_name  # type: ignore[assignment]
    client._sent_dms = sent_dms  # type: ignore[attr-defined]
    return client


@pytest.mark.asyncio
async def test_invite_prompt_is_permission_card():
    from puffo_agent.agent.event_kinds import EventKind

    client = _make_client()
    await client._notify_operator_of_invite(
        kind=EventKind.INVITE_TO_SPACE,
        invitation_event_id="evt_1",
        inviter_slug="mallory-9",
        space_id="sp_1",
        space_name="Team",
        channel_id="",
        channel_name=None,
    )
    dm = client._sent_dms[0]
    assert dm["to"] == "op-1"
    assert dm["root_id"] == ""  # card renders on root-level DMs only
    assert dm["text"].startswith("/permission ")
    assert "**Team**" in dm["text"]
    assert "pending invites" in dm["text"]
    # Prompt registered for the threaded y/n intercept.
    assert dm["env_id"] in client._pending_invite_dms


@pytest.mark.asyncio
async def test_leave_prompt_is_permission_card_with_reason_quote():
    client = _make_client()
    out = await client.request_leave_approval(
        kind="leave_space", space_id="sp_1", channel_id="", reason="too noisy",
    )
    dm = client._sent_dms[0]
    assert dm["root_id"] == ""
    assert dm["text"].startswith("/permission ")
    assert "> Reason: too noisy" in dm["text"]
    assert dm["env_id"] in client._pending_leave_dms
    assert "Asked your operator" in out


@pytest.mark.asyncio
async def test_leave_prompt_omits_empty_reason():
    client = _make_client()
    await client.request_leave_approval(
        kind="leave_space", space_id="sp_1", channel_id="", reason="  ",
    )
    text = client._sent_dms[0]["text"]
    assert "Reason:" not in text
    assert "\n" not in text


# ─── cli-local command permission ─────────────────────────────────────


@pytest.mark.asyncio
async def test_command_permission_allow_roundtrip():
    client = _make_client()
    task = asyncio.ensure_future(
        client.request_command_permission(
            tool_name="Bash", summary="- command: rm -rf ./build", timeout_s=5,
        )
    )
    await asyncio.sleep(0)  # let the prompt DM go out
    prompt = client._sent_dms[0]
    assert prompt["text"].startswith("/permission ")
    assert "**Bash**" in prompt["text"]
    assert "> - command: rm -rf ./build" in prompt["text"]
    assert prompt["root_id"] == ""

    handled = await client._maybe_handle_permission_reply(
        thread_root_id=prompt["env_id"], text="y",
    )
    assert handled is True
    assert await task == "allow"
    # In-thread confirmation.
    confirm = client._sent_dms[1]
    assert confirm["root_id"] == prompt["env_id"]
    assert "Approved" in confirm["text"]
    assert client._pending_command_permissions == {}


@pytest.mark.asyncio
async def test_command_permission_deny():
    client = _make_client()
    task = asyncio.ensure_future(
        client.request_command_permission(
            tool_name="WebFetch", summary="", timeout_s=5,
        )
    )
    await asyncio.sleep(0)
    prompt = client._sent_dms[0]
    assert await client._maybe_handle_permission_reply(
        thread_root_id=prompt["env_id"], text="No",
    )
    assert await task == "deny"
    assert "Denied" in client._sent_dms[1]["text"]


@pytest.mark.asyncio
async def test_command_permission_timeout_notifies_thread():
    client = _make_client()
    result = await client.request_command_permission(
        tool_name="Bash", summary="", timeout_s=0,
    )
    assert result == "timeout"
    prompt, notice = client._sent_dms
    assert notice["root_id"] == prompt["env_id"]
    assert "Timed out" in notice["text"]
    assert client._pending_command_permissions == {}


@pytest.mark.asyncio
async def test_command_permission_ignores_non_yn_reply():
    client = _make_client()
    task = asyncio.ensure_future(
        client.request_command_permission(
            tool_name="Bash", summary="", timeout_s=5,
        )
    )
    await asyncio.sleep(0)
    prompt = client._sent_dms[0]
    handled = await client._maybe_handle_permission_reply(
        thread_root_id=prompt["env_id"], text="why do you need this?",
    )
    assert handled is False
    assert not task.done()
    # Then approve so the task finishes cleanly.
    await client._maybe_handle_permission_reply(
        thread_root_id=prompt["env_id"], text="yes",
    )
    assert await task == "allow"


@pytest.mark.asyncio
async def test_late_reply_in_timeout_window_gets_stale_note():
    """A `y` racing the timeout-notice send must get the stale note,
    never "Approved — running it" for a tool that never ran."""
    client = _make_client()
    orig_send = client._send_dm
    late: dict = {}

    async def _send_with_interleaved_reply(slug, text, root_id=""):
        if "Timed out" in text and "handled" not in late:
            late["handled"] = await client._maybe_handle_permission_reply(
                thread_root_id=root_id, text="y",
            )
        return await orig_send(slug, text, root_id)

    client._send_dm = _send_with_interleaved_reply  # type: ignore[assignment]
    result = await client.request_command_permission(
        tool_name="Bash", summary="", timeout_s=0,
    )
    assert result == "timeout"
    assert late["handled"] is True  # consumed — not fed to the LLM
    assert not any("Approved" in d["text"] for d in client._sent_dms)
    assert any("already timed out" in d["text"] for d in client._sent_dms)


@pytest.mark.asyncio
async def test_late_reply_after_timeout_gets_stale_note():
    client = _make_client()
    result = await client.request_command_permission(
        tool_name="Bash", summary="", timeout_s=0,
    )
    assert result == "timeout"
    prompt_env = client._sent_dms[0]["env_id"]
    # Minutes later the operator replies in the dead prompt's thread.
    handled = await client._maybe_handle_permission_reply(
        thread_root_id=prompt_env, text="y",
    )
    assert handled is True
    note = client._sent_dms[-1]
    assert note["root_id"] == prompt_env
    assert "already timed out" in note["text"]
    # Non-y/n chatter in that thread still falls through to the LLM.
    assert (
        await client._maybe_handle_permission_reply(
            thread_root_id=prompt_env, text="what was this about?",
        )
        is False
    )


@pytest.mark.asyncio
async def test_timed_out_registry_is_capped():
    client = _make_client()
    for i in range(70):
        client._timed_out_command_permissions[f"env_old_{i}"] = float(i)
    await client.request_command_permission(
        tool_name="Bash", summary="", timeout_s=0,
    )
    assert len(client._timed_out_command_permissions) <= 65
    # Oldest evicted first; the fresh timeout is retained.
    assert "env_old_0" not in client._timed_out_command_permissions


@pytest.mark.asyncio
async def test_command_permission_requires_operator():
    client = _make_client(operator_slug="")
    with pytest.raises(RuntimeError):
        await client.request_command_permission(
            tool_name="Bash", summary="", timeout_s=5,
        )


@pytest.mark.asyncio
async def test_command_permission_raises_when_dm_undeliverable():
    client = _make_client()

    async def _no_envelope(slug, text, root_id=""):
        return None  # recipient has no resolvable devices

    client._send_dm = _no_envelope  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await client.request_command_permission(
            tool_name="Bash", summary="", timeout_s=5,
        )
    assert client._pending_command_permissions == {}


@pytest.mark.asyncio
async def test_unknown_thread_reply_not_consumed():
    client = _make_client()
    assert (
        await client._maybe_handle_permission_reply(
            thread_root_id="env_nope", text="y",
        )
        is False
    )
