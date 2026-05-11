import os

from ._logging import agent_logger
from ._time import ms_to_iso as _ms_to_iso
from .adapters import Adapter, TurnContext
from .memory import MemoryManager

MAX_LOG_ENTRIES = 60


class AgentAPIError(Exception):
    """Raised when adapter output contains ``API Error``. Signals the
    consumer to suppress the reply, mark the turn errored, and
    re-enqueue the triggering message after a 15-45s backoff so
    transient provider failures recover without operator intervention.
    """
    pass


def _format_assistant_fallback(text_parts: list[str], joined_reply: str) -> str:
    """Assemble fallback reply (markdown bullets, one per non-empty
    assistant.text frame) for turns where the agent neither called
    ``send_message`` nor emitted ``[SILENT]``.
    """
    cleaned = [p.strip() for p in text_parts if p and p.strip()]
    if not cleaned:
        return joined_reply.strip()
    if len(cleaned) == 1:
        return cleaned[0]
    return "\n".join(f"- {p}" for p in cleaned)


class PuffoAgent:
    def __init__(
        self,
        adapter: Adapter,
        system_prompt: str,
        memory_dir: str,
        workspace_dir: str = "",
        claude_dir: str = "",
        agent_id: str = "",
    ):
        """Per-agent shell. Owns cross-cutting state (conversation
        log, memory manager) and delegates each turn to an ``Adapter``
        (see ``adapters/base.py``).

        ``system_prompt`` is pre-assembled and mirrors the content
        written to ``<workspace>/.claude/CLAUDE.md`` so CLI runtimes
        discover it via project-level file lookup while sdk/chat
        adapters consume it as a string.
        """
        self.adapter = adapter
        self.system_prompt = system_prompt
        self.workspace_dir = workspace_dir
        self.claude_dir = claude_dir
        self.agent_id = agent_id
        self.logger = agent_logger(__name__, agent_id)

        self.memory = MemoryManager(memory_dir)
        self.memory_dir = memory_dir

        # Conversation log shared across all channels.
        self.log: list[dict] = []

    # ── Message handling ──────────────────────────────────────────────────────

    async def handle_message(
        self,
        channel_id: str,
        channel_name: str,
        sender: str,
        sender_email: str,
        text: str,
        direct: bool = False,
        attachments: list[str] | None = None,
        sender_is_bot: bool = False,
        mentions: list[dict] | None = None,
        on_progress=None,
        post_id: str = "",
        root_id: str = "",
        create_at: int = 0,
        followups: list[dict] | None = None,
        space_id: str = "",
        space_name: str = "",
    ) -> str | None:
        self._append_user(
            channel_name, sender, sender_email, text,
            channel_id=channel_id,
            root_id=root_id,
            attachments=attachments,
            sender_is_bot=sender_is_bot,
            mentions=mentions,
            post_id=post_id,
            create_at=create_at,
            followups=followups,
            space_id=space_id,
            space_name=space_name,
        )
        return await self._run_turn_and_route(
            channel_name=channel_name,
            sender=sender,
            on_progress=on_progress,
        )

    async def handle_message_batch(
        self,
        root_id: str,
        batch: list[dict],
        channel_meta: dict,
        on_progress=None,
    ) -> str | None:
        """One adapter turn over a whole thread batch.

        Each entry in ``batch`` is the same decoded-message dict the
        listen handler used to enqueue (envelope_id, sender_slug,
        text, attachments, mentions, sent_at, sender_is_bot,
        is_dm…). The thread/channel context is constant across the
        batch and rides on ``channel_meta`` (channel_id,
        channel_name, space_id, space_name).

        The agent sees every message in order as separate ``user``
        turns in the shell log so the LLM can reason about who said
        what and decide on its own how many replies to issue. The
        ``followups`` field on the old single-message path is gone —
        every message is a real user turn now.
        """
        if not batch:
            return None
        for msg in batch:
            self._append_user(
                channel_meta.get("channel_name", ""),
                msg.get("sender_slug", ""),
                msg.get("sender_email", ""),
                msg.get("text", ""),
                channel_id=channel_meta.get("channel_id", ""),
                root_id=root_id,
                attachments=msg.get("attachments") or [],
                sender_is_bot=msg.get("sender_is_bot", False),
                mentions=msg.get("mentions") or [],
                post_id=msg.get("envelope_id", ""),
                create_at=msg.get("sent_at", 0),
                followups=None,
                space_id=channel_meta.get("space_id", ""),
                space_name=channel_meta.get("space_name", ""),
            )
        # Route logging uses the LAST sender in the batch as the
        # display "trigger" for log lines — purely cosmetic, the
        # agent itself decides who to reply to.
        last_msg = batch[-1]
        return await self._run_turn_and_route(
            channel_name=channel_meta.get("channel_name", ""),
            sender=last_msg.get("sender_slug", ""),
            on_progress=on_progress,
        )

    async def handle_api_error_retry(
        self,
        root_id: str,
        channel_meta: dict,
        fallback_batch: list[dict],
        on_progress=None,
    ) -> str | None:
        """Retry the most recently failed turn.

        Doesn't touch ``self.log`` — the original user input is
        already in there from the first attempt. The adapter sends
        a small kick ("session errored on rate limiting, please
        resume processing") when ``--resume`` is still live, or
        falls back to the original ``fallback_batch`` payload when
        the resumable session has been lost.

        Reply routing is the same as ``_run_turn_and_route`` (the
        ``AgentAPIError`` raise still happens here on consecutive
        failures, so the consumer keeps incrementing its retry
        counter).
        """
        kick_text = (
            "[system] session errored on rate limiting, "
            "please resume processing."
        )
        # Fallback is the same payload ``_append_user`` would have
        # produced. For multi-message batches we only have the
        # adapter API for a single user_message, so concatenate.
        fallback_chunks: list[str] = []
        for msg in fallback_batch:
            fallback_chunks.append(self._format_user_block(
                channel_name=channel_meta.get("channel_name", ""),
                sender=msg.get("sender_slug", ""),
                sender_email=msg.get("sender_email", ""),
                text=msg.get("text", ""),
                channel_id=channel_meta.get("channel_id", ""),
                root_id=root_id,
                attachments=msg.get("attachments") or [],
                sender_is_bot=msg.get("sender_is_bot", False),
                mentions=msg.get("mentions") or [],
                post_id=msg.get("envelope_id", ""),
                create_at=msg.get("sent_at", 0),
                space_id=channel_meta.get("space_id", ""),
                space_name=channel_meta.get("space_name", ""),
            ))
        fallback_text = "\n\n".join(fallback_chunks)

        ctx = TurnContext(
            system_prompt=self.system_prompt,
            messages=list(self.log),
            workspace_dir=self.workspace_dir,
            claude_dir=self.claude_dir,
            memory_dir=self.memory_dir,
            on_progress=on_progress,
        )
        result = await self.adapter.run_retry_turn(
            kick_text, fallback_text, ctx,
        )

        # Route reply the same way as a normal turn so the consumer
        # picks up AgentAPIError again on consecutive rate-limit
        # failures.
        send_message_called = bool(result.metadata.get("send_message_targets"))
        text_parts: list[str] = result.metadata.get("assistant_text_parts") or []
        if send_message_called:
            if result.reply:
                self._append_assistant(
                    channel_meta.get("channel_name", ""), result.reply,
                )
            return None
        joined = "\n".join(text_parts) if text_parts else (result.reply or "")
        if "[SILENT]" in joined:
            return None
        if "API Error" in joined:
            self.logger.warning(
                "[api-error-retry] adapter still rate-limited; "
                "raising for consumer-side backoff"
            )
            raise AgentAPIError(
                "agent adapter output contained 'API Error' on retry"
            )
        if not text_parts and not result.reply:
            return None
        fallback = _format_assistant_fallback(text_parts, result.reply)
        self._append_assistant(channel_meta.get("channel_name", ""), fallback)
        return fallback

    async def _run_turn_and_route(
        self,
        channel_name: str,
        sender: str,
        on_progress=None,
    ) -> str | None:
        """Shared tail for ``handle_message`` and ``handle_message_batch``.
        Runs one adapter turn against the current ``self.log`` and
        routes the reply per the rules below.
        """
        ctx = TurnContext(
            system_prompt=self.system_prompt,
            messages=list(self.log),
            workspace_dir=self.workspace_dir,
            claude_dir=self.claude_dir,
            memory_dir=self.memory_dir,
            on_progress=on_progress,
        )
        result = await self.adapter.run_turn(ctx)

        # Reply routing:
        #   a. send_message called → return None (MCP already posted).
        #   b. else if [SILENT] in assistant.text → silent.
        #   c. else if "API Error" in output → raise AgentAPIError.
        #   d. else → fallback: bullet list of assistant.text frames.
        send_message_called = bool(result.metadata.get("send_message_targets"))
        text_parts: list[str] = result.metadata.get("assistant_text_parts") or []

        if send_message_called:
            self.logger.debug(
                f"[mcp-only] [{channel_name}] @{sender}: send_message "
                "called; skipping shell auto-post"
            )
            if result.reply:
                self._append_assistant(channel_name, result.reply)
            return None

        # Substring match: marker position in the assistant text
        # doesn't matter. Real replies go via send_message, so a
        # prose-only turn mentioning the marker is correctly silent.
        joined = "\n".join(text_parts) if text_parts else (result.reply or "")
        if "[SILENT]" in joined:
            self.logger.debug(
                f"[silent] [{channel_name}] @{sender}: agent chose not to reply"
            )
            return None

        # API Error suppression. Provider error bodies often surface
        # as ``"API Error: 429 ..."`` in the assistant prose when the
        # adapter couldn't recover. Posting that would leak provider
        # internals and spam the thread on transient rate-limits.
        # Raise so the consumer can re-enqueue after a backoff.
        if "API Error" in joined:
            self.logger.warning(
                f"[api-error] [{channel_name}] @{sender}: adapter output "
                "contained 'API Error'; suppressing post, abandoning batch"
            )
            raise AgentAPIError(
                "agent adapter output contained 'API Error'"
            )

        if not text_parts and not result.reply:
            return None

        # Fallback: agent skipped send_message and [SILENT]; assemble
        # assistant.text frames into a bullet list so something lands.
        fallback = _format_assistant_fallback(text_parts, result.reply)
        self.logger.warning(
            f"[fallback] [{channel_name}] @{sender}: agent skipped both "
            f"send_message and [SILENT] markers; posting "
            f"{len(text_parts) or 1}-frame fallback"
        )
        self._append_assistant(channel_name, fallback)
        return fallback

    def _append_user(
        self,
        channel_name: str,
        sender: str,
        sender_email: str,
        text: str,
        attachments: list[str] | None,
        channel_id: str = "",
        root_id: str = "",
        sender_is_bot: bool = False,
        mentions: list[dict] | None = None,
        post_id: str = "",
        create_at: int = 0,
        followups: list[dict] | None = None,
        space_id: str = "",
        space_name: str = "",
    ):
        content = self._format_user_block(
            channel_name=channel_name,
            sender=sender,
            sender_email=sender_email,
            text=text,
            attachments=attachments,
            channel_id=channel_id,
            root_id=root_id,
            sender_is_bot=sender_is_bot,
            mentions=mentions,
            post_id=post_id,
            create_at=create_at,
            followups=followups,
            space_id=space_id,
            space_name=space_name,
        )
        self.log.append({"role": "user", "content": content})
        self._truncate_log()

    def _format_user_block(
        self,
        *,
        channel_name: str,
        sender: str,
        sender_email: str,
        text: str,
        attachments: list[str] | None,
        channel_id: str = "",
        root_id: str = "",
        sender_is_bot: bool = False,
        mentions: list[dict] | None = None,
        post_id: str = "",
        create_at: int = 0,
        followups: list[dict] | None = None,
        space_id: str = "",
        space_name: str = "",
    ) -> str:
        # Structured markdown block keeps context metadata distinct
        # from message content, preventing the LLM from echoing
        # "[#channel] @user:" style prefixes back into replies. Format
        # is documented to the model in DEFAULT_SHARED_CLAUDE_MD.
        lines: list[str] = []
        if space_name:
            lines.append("- space: " + space_name)
        if space_id:
            lines.append(f"- space_id: {space_id}")
        lines.append("- channel: " + (channel_name or channel_id))
        if channel_id:
            lines.append(f"- channel_id: {channel_id}")
        if post_id:
            lines.append(f"- post_id: {post_id}")
        # thread_root_id is the root post id to pass as send_message's
        # root_id. For a top-level post the root is the post itself.
        thread_root = root_id or post_id
        if thread_root:
            lines.append(f"- thread_root_id: {thread_root}")
        ts_iso = _ms_to_iso(create_at)
        if ts_iso:
            lines.append(f"- timestamp: {ts_iso}")
        lines.append(
            f"- sender: {sender}" + (f" ({sender_email})" if sender_email else "")
        )
        lines.append(f"- sender_type: {'bot' if sender_is_bot else 'human'}")
        if mentions:
            lines.append("- mentions:")
            for m in mentions:
                # ``(you)`` pairs with the ``@you(name)`` rewrite in
                # the message body — two independent signals so agents
                # that parse only one layer still spot a self-mention.
                if m.get("is_self"):
                    suffix = " (you)"
                else:
                    kind = "agent" if m.get("is_bot") else "human"
                    suffix = f" ({kind})"
                lines.append(f"  - {m['username']}{suffix}")
        if attachments:
            lines.append("- attachments:")
            for path in attachments:
                lines.append(f"  - {path}")
        lines.append("- message: " + text)
        if followups:
            # Messages that arrived in the same thread/channel AFTER
            # this one was queued. The agent should weigh them before
            # replying — the conversation may have moved on.
            lines.append("- followup_messages_since:")
            for f in followups:
                ts = f.get("timestamp", "") or _ms_to_iso(f.get("create_at", 0))
                fid = f.get("id", "")
                fsender = f.get("sender_username", "") or f.get("sender_id", "")
                ftext = f.get("text", "") or ""
                lines.append(
                    f"  - [{ts} post:{fid}] @{fsender}: {ftext}"
                )
        return "\n".join(lines)

    def _append_assistant(self, channel_name: str, reply: str):
        self.log.append({"role": "assistant", "content": reply})
        self._truncate_log()

    def _truncate_log(self):
        if len(self.log) > MAX_LOG_ENTRIES:
            self.log = self.log[-MAX_LOG_ENTRIES:]
