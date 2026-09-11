
from ._auth_markers import looks_like_auth_error
from ._logging import agent_logger
from ._time import ms_to_iso as _ms_to_iso
from ._usage_markers import looks_like_usage_limit
from .adapters import Adapter, TurnContext
from .adapters.base import STATUS_PREVIEW_CHARS, is_silent
from .errors import AgentAPIError
from .message_projection import model_attachment_path
from .memory import MemoryManager
from ..tasks import spawn

MAX_LOG_ENTRIES = 60


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


def _classify_api_error(joined: str) -> tuple[bool, bool, str]:
    """``(is_auth, is_drained, label)`` for an ``API Error`` body.
    Quota first: auth markers are substrings and match quota bodies."""
    if looks_like_usage_limit(joined):
        return False, True, "quota-drained"
    if looks_like_auth_error(joined):
        return True, False, "auth-failed"
    return False, False, "rate-limited"


def _user_message_preview(messages: list[dict]) -> str:
    """Body of the latest user message (the log entry is a metadata block)."""
    for m in reversed(messages):
        content = m.get("content")
        if (
            m.get("role") == "user"
            and isinstance(content, str)
            and "- message: " in content
        ):
            body = content.split("- message: ", 1)[1]
            return " ".join(body.split())[:STATUS_PREVIEW_CHARS]
    return ""


def _origin_for_compressed(path: str) -> str | None:
    """If ``path`` is a downscaled ``<stem>.compressed<ext>`` attachment,
    return its full-resolution ``<stem>.origin<ext>`` sibling, else None.
    String-only so it's separator-agnostic across hosts."""
    dot = path.rfind(".")
    if dot <= 0:
        return None
    base, ext = path[:dot], path[dot:]
    marker = ".compressed"
    if not base.endswith(marker):
        return None
    return base[: -len(marker)] + ".origin" + ext


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

        self.memory = MemoryManager(memory_dir, workspace_dir=workspace_dir)
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
        sender_is_agent: bool = False,
        mentions: list[dict] | None = None,
        on_progress=None,
        post_id: str = "",
        root_id: str = "",
        create_at: int = 0,
        space_id: str = "",
        space_name: str = "",
        sender_owner_slug: str = "",
        is_from_operator: bool = False,
    ) -> str | None:
        self._append_user(
            channel_name,
            sender,
            sender_email,
            text,
            channel_id=channel_id,
            root_id=root_id,
            attachments=attachments,
            sender_is_agent=sender_is_agent,
            mentions=mentions,
            post_id=post_id,
            create_at=create_at,
            space_id=space_id,
            space_name=space_name,
            sender_owner_slug=sender_owner_slug,
            is_from_operator=is_from_operator,
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
        text, attachments, mentions, sent_at, sender_is_agent,
        is_dm…). The thread/channel context is constant across the
        batch and rides on ``channel_meta`` (channel_id,
        channel_name, space_id, space_name).

        The whole batch is appended as ONE ``user`` log entry (blocks
        joined with a blank line, same shape as the api-error-retry
        fallback). CLI adapters transmit only ``ctx.messages[-1]`` per
        turn — the resume-based session already holds the earlier
        history — so per-message entries would silently drop all but
        the last message of the batch.
        """
        if not batch:
            return None
        blocks = [
            self._format_user_block(
                channel_name=channel_meta.get("channel_name", ""),
                sender=msg.get("sender_slug", ""),
                sender_email=msg.get("sender_email", ""),
                text=msg.get("text", ""),
                channel_id=channel_meta.get("channel_id", ""),
                root_id=root_id,
                attachments=msg.get("attachments") or [],
                sender_is_agent=msg.get("sender_is_agent", False),
                mentions=msg.get("mentions") or [],
                post_id=msg.get("envelope_id", ""),
                create_at=msg.get("sent_at", 0),
                space_id=channel_meta.get("space_id", ""),
                space_name=channel_meta.get("space_name", ""),
                sender_display_name=msg.get("sender_display_name", ""),
                is_visible_to_human=msg.get("is_visible_to_human", True),
                sender_owner_slug=msg.get("sender_owner_slug", ""),
                sender_type=msg.get("sender_type"),
                is_from_operator=msg.get("is_from_operator", False),
                is_encrypted=msg.get("is_encrypted", True),
            )
            for msg in batch
        ]
        self.log.append({"role": "user", "content": "\n\n".join(blocks)})
        self._truncate_log()
        # Route logging uses the LAST sender in the batch as the
        # display "trigger" for log lines — purely cosmetic, the
        # agent itself decides who to reply to.
        last_msg = batch[-1]
        return await self._run_turn_and_route(
            channel_name=channel_meta.get("channel_name", ""),
            sender=last_msg.get("sender_slug", ""),
            on_progress=on_progress,
        )

    async def handle_global_inbox_turn(
        self,
        planned,
        on_progress=None,
    ) -> str | None:
        """Run one metadata-only Inbox notice without retaining it in history."""
        # A notice wakes the current provider turn but is not conversation
        # content. Resumable Drivers retain their own history, while
        # stateless adapters receive this ephemeral final user entry alongside
        # the ordinary Puffo log for this one call.
        messages = [
            *self.log,
            {"role": "user", "content": planned.provider_input},
        ]
        return await self._run_turn_and_route(
            channel_name="global inbox",
            sender="multiple" if len(planned.targets) > 1 else "",
            on_progress=on_progress,
            allow_plain_fallback=False,
            messages=messages,
        )

    async def handle_global_inbox_retry(
        self,
        planned,
        on_progress=None,
    ) -> str | None:
        """Resume an admitted global turn without appending its input again."""
        kick_text = (
            "[puffo-agent system message] session errored on rate "
            "limiting, please resume processing."
        )
        ctx = TurnContext(
            system_prompt=self.system_prompt,
            messages=list(self.log),
            workspace_dir=self.workspace_dir,
            claude_dir=self.claude_dir,
            memory_dir=self.memory_dir,
            on_progress=on_progress,
        )
        result = await self.adapter.run_retry_turn(
            kick_text,
            planned.provider_input,
            ctx,
        )
        send_message_called = bool(result.metadata.get("send_message_targets"))
        text_parts: list[str] = result.metadata.get("assistant_text_parts") or []
        if send_message_called:
            if result.reply:
                self._append_assistant("global inbox", result.reply)
            return None
        joined = "\n".join(text_parts) if text_parts else (result.reply or "")
        if is_silent(joined):
            return None
        if "API Error" in joined:
            is_auth, is_drained, _label = _classify_api_error(joined)
            raise AgentAPIError(
                "agent adapter output contained 'API Error' on global retry",
                is_auth=is_auth,
                is_drained=is_drained,
            )
        if not text_parts and not result.reply:
            return None
        self.logger.info(
            "[no-send] [global inbox retry]: ignoring plain assistant output; "
            "outbound chat requires send_message"
        )
        return None

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
            "[puffo-agent system message] session errored on rate "
            "limiting, please resume processing."
        )
        # Fallback is the same payload ``_append_user`` would have
        # produced. For multi-message batches we only have the
        # adapter API for a single user_message, so concatenate.
        fallback_chunks: list[str] = []
        for msg in fallback_batch:
            fallback_chunks.append(
                self._format_user_block(
                    channel_name=channel_meta.get("channel_name", ""),
                    sender=msg.get("sender_slug", ""),
                    sender_email=msg.get("sender_email", ""),
                    text=msg.get("text", ""),
                    channel_id=channel_meta.get("channel_id", ""),
                    root_id=root_id,
                    attachments=msg.get("attachments") or [],
                    sender_is_agent=msg.get("sender_is_agent", False),
                    mentions=msg.get("mentions") or [],
                    post_id=msg.get("envelope_id", ""),
                    create_at=msg.get("sent_at", 0),
                    space_id=channel_meta.get("space_id", ""),
                    space_name=channel_meta.get("space_name", ""),
                    sender_display_name=msg.get("sender_display_name", ""),
                    sender_owner_slug=msg.get("sender_owner_slug", ""),
                    is_from_operator=msg.get("is_from_operator", False),
                    is_encrypted=msg.get("is_encrypted", True),
                )
            )
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
            kick_text,
            fallback_text,
            ctx,
        )

        # Route reply the same way as a normal turn so the consumer
        # picks up AgentAPIError again on consecutive rate-limit
        # failures.
        send_message_called = bool(result.metadata.get("send_message_targets"))
        text_parts: list[str] = result.metadata.get("assistant_text_parts") or []
        if send_message_called:
            if result.reply:
                self._append_assistant(
                    channel_meta.get("channel_name", ""),
                    result.reply,
                )
            return None
        joined = "\n".join(text_parts) if text_parts else (result.reply or "")
        if is_silent(joined):
            return None
        if "API Error" in joined:
            is_auth, is_drained, label = _classify_api_error(joined)
            self.logger.warning(
                "[api-error-retry] adapter still %s; raising for "
                "consumer-side handling",
                label,
            )
            raise AgentAPIError(
                "agent adapter output contained 'API Error' on retry",
                is_auth=is_auth,
                is_drained=is_drained,
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
        allow_plain_fallback: bool = True,
        messages: list[dict] | None = None,
    ) -> str | None:
        """Shared tail for ``handle_message`` and ``handle_message_batch``.
        Runs one adapter turn against the current ``self.log`` and
        routes the reply per the rules below.
        """
        ctx = TurnContext(
            system_prompt=self.system_prompt,
            messages=list(self.log if messages is None else messages),
            workspace_dir=self.workspace_dir,
            claude_dir=self.claude_dir,
            memory_dir=self.memory_dir,
            on_progress=on_progress,
        )
        # Reverse channel: turn_start → adapter streams assistant_text/tool_use
        # → turn_complete (tokens). Best-effort; no-ops if the owner isn't linked.
        from ..portal.control.reporter import get_reporter

        spawn(
            get_reporter().emit(
                self.agent_id,
                "turn_start",
                {"message": _user_message_preview(ctx.messages)},
            ),
            name="reporter.emit:turn_start",
        )
        result = await self.adapter.run_turn(ctx)

        turn_complete_payload = {
            "tokens": {
                "input": result.input_tokens,
                "output": result.output_tokens,
            }
        }
        context_tokens = result.metadata.get("context_tokens")
        if (
            isinstance(context_tokens, int)
            and not isinstance(context_tokens, bool)
            and context_tokens > 0
        ):
            turn_complete_payload["current_context"] = context_tokens
        context_window = result.metadata.get("context_window")
        if (
            isinstance(context_window, int)
            and not isinstance(context_window, bool)
            and context_window > 0
        ):
            turn_complete_payload["context_window"] = context_window
        context_measured_at = result.metadata.get("context_measured_at")
        if isinstance(context_measured_at, str) and context_measured_at:
            turn_complete_payload["context_measured_at"] = context_measured_at
        spawn(
            get_reporter().emit(
                self.agent_id,
                "turn_complete",
                turn_complete_payload,
            ),
            name="reporter.emit:turn_complete",
        )

        return self._route_turn_result(
            result,
            channel_name,
            sender,
            allow_plain_fallback=allow_plain_fallback,
        )

    def _route_turn_result(
        self,
        result,
        channel_name: str,
        sender: str,
        *,
        allow_plain_fallback: bool,
    ) -> str | None:
        """Apply the shared MCP/silent/error/plain-text reply policy."""
        from ..portal.control.reporter import get_reporter

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

        joined = "\n".join(text_parts) if text_parts else (result.reply or "")
        if is_silent(joined):
            self.logger.debug(
                f"[silent] [{channel_name}] @{sender}: agent chose not to reply"
            )
            return None

        if "API Error" in joined:
            is_auth, is_drained, label = _classify_api_error(joined)
            self.logger.warning(
                f"[api-error] [{channel_name}] @{sender}: adapter output "
                "contained 'API Error' (%s); suppressing post",
                label,
            )
            raise AgentAPIError(
                "agent adapter output contained 'API Error'",
                is_auth=is_auth,
                is_drained=is_drained,
            )

        if not text_parts and not result.reply:
            return None

        if not allow_plain_fallback:
            self.logger.info(
                f"[no-send] [{channel_name}] @{sender}: ignoring plain "
                "assistant output; outbound chat requires send_message"
            )
            return None

        fallback = _format_assistant_fallback(text_parts, result.reply)
        self.logger.warning(
            f"[fallback] [{channel_name}] @{sender}: agent skipped both "
            f"send_message and [SILENT] markers; posting "
            f"{len(text_parts) or 1}-frame fallback"
        )
        spawn(
            get_reporter().emit(
                self.agent_id,
                "tool_use",
                {"tool": "fallback", "content": fallback[:STATUS_PREVIEW_CHARS]},
            ),
            name="reporter.emit:tool_use",
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
        sender_is_agent: bool = False,
        mentions: list[dict] | None = None,
        post_id: str = "",
        create_at: int = 0,
        space_id: str = "",
        space_name: str = "",
        sender_display_name: str = "",
        is_visible_to_human: bool = True,
        sender_owner_slug: str = "",
        sender_type: str | None = None,
        is_from_operator: bool = False,
        is_encrypted: bool = True,
    ):
        content = self._format_user_block(
            channel_name=channel_name,
            sender=sender,
            sender_email=sender_email,
            text=text,
            attachments=attachments,
            channel_id=channel_id,
            root_id=root_id,
            sender_is_agent=sender_is_agent,
            mentions=mentions,
            post_id=post_id,
            create_at=create_at,
            space_id=space_id,
            space_name=space_name,
            sender_display_name=sender_display_name,
            is_visible_to_human=is_visible_to_human,
            sender_owner_slug=sender_owner_slug,
            sender_type=sender_type,
            is_from_operator=is_from_operator,
            is_encrypted=is_encrypted,
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
        sender_is_agent: bool = False,
        mentions: list[dict] | None = None,
        post_id: str = "",
        create_at: int = 0,
        space_id: str = "",
        space_name: str = "",
        sender_display_name: str = "",
        is_visible_to_human: bool = True,
        sender_owner_slug: str = "",
        sender_type: str | None = None,
        is_from_operator: bool = False,
        is_encrypted: bool = True,
    ) -> str:
        lines = _user_metadata_lines(
            channel_name=channel_name,
            channel_id=channel_id,
            root_id=root_id,
            post_id=post_id,
            space_id=space_id,
            space_name=space_name,
            create_at=create_at,
            sender=sender,
            sender_display_name=sender_display_name,
            sender_is_agent=sender_is_agent,
            sender_owner_slug=sender_owner_slug,
            sender_type=sender_type,
            is_from_operator=is_from_operator,
            is_encrypted=is_encrypted,
        )
        lines.append(
            f"- is_visible_to_human: {'true' if is_visible_to_human else 'false'}"
        )
        if mentions:
            lines.append("- mentions:")
            for m in mentions:
                # ``(you)`` pairs with the ``@you(name)`` rewrite in
                # the message body — two independent signals so agents
                # that parse only one layer still spot a self-mention.
                if m.get("is_self"):
                    suffix = " (you)"
                else:
                    kind = "agent" if m.get("is_agent") else "human"
                    suffix = f" ({kind})"
                lines.append(f"  - {m['username']}{suffix}")
        if attachments:
            lines.append("- attachments:")
            for raw_path in attachments:
                path = model_attachment_path(raw_path)
                lines.append(f"  - {path}")
                origin = _origin_for_compressed(path)
                if origin:
                    lines.append(
                        f"    (downscaled to fit the model; full-resolution "
                        f"original at {origin} — to read fine detail, crop a "
                        f"region of the original rather than opening the whole "
                        f"image)"
                    )
        lines.append("- message: " + text)
        return "\n".join(lines)

    def _append_assistant(self, channel_name: str, reply: str):
        self.log.append({"role": "assistant", "content": reply})
        self._truncate_log()

    def _truncate_log(self):
        if len(self.log) > MAX_LOG_ENTRIES:
            self.log = self.log[-MAX_LOG_ENTRIES:]


def _user_metadata_lines(
    *,
    channel_name: str,
    channel_id: str,
    root_id: str,
    post_id: str,
    space_id: str,
    space_name: str,
    create_at: int,
    sender: str,
    sender_display_name: str,
    sender_is_agent: bool,
    sender_owner_slug: str,
    sender_type: str | None,
    is_from_operator: bool,
    is_encrypted: bool,
) -> list[str]:
    """Render stable user-message metadata before optional projections."""
    lines: list[str] = []
    if post_id:
        lines.append(f"- message_id: {post_id}")
    if space_name:
        lines.append("- space: " + space_name)
    if space_id:
        lines.append(f"- space_id: {space_id}")
    lines.append("- channel: " + (channel_name or channel_id))
    if channel_id:
        lines.append(f"- channel_id: {channel_id}")
    thread_root = root_id or post_id
    if thread_root:
        lines.append(f"- thread_root_id: {thread_root}")
    lines.append(f"- is_encrypted: {str(is_encrypted).lower()}")
    timestamp = _ms_to_iso(create_at)
    if timestamp:
        lines.append(f"- timestamp: {timestamp}")
    lines.append(f"- sender: {sender_display_name or sender}")
    lines.append(f"- sender_slug: {sender}")
    if sender_type in {"human", "agent", "system"}:
        projected = sender_type
    elif sender.lstrip("@").lower() == "system":
        projected = "system"
    elif sender_is_agent or sender_owner_slug:
        projected = "agent"
    elif is_from_operator:
        projected = "human"
    else:
        projected = "unknown"
    lines.append(f"- sender_type: {projected}")
    if sender_owner_slug:
        lines.append(f"- sender_owner_slug: {sender_owner_slug}")
    if is_from_operator:
        lines.append("- is_from_operator: true")
    return lines
