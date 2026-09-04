"""Pinned Pi RPC protocol surface, @earendil-works/pi-coding-agent@0.84.3.

Source of truth is the published tarball, not the docs page: every name below
was verified present as a string literal in the shipped bundle
(``dist/bundle/chunks/chunk-E5KXRMZK.js``) as well as in ``docs/rpc.md``. For
this package the two agreed exactly -- 32/32 commands, 21/21 events, 9/9
extension-UI kinds. That is worth pinning precisely because it is not
guaranteed to stay true across releases.

Re-verify on version bump; do not edit these lists to make a test pass.
"""

PINNED_VERSION = "0.84.3"

# Client -> agent, one JSON object per line on stdin.
COMMANDS = frozenset({
    "prompt", "steer", "follow_up", "abort",
    "new_session", "switch_session", "fork", "clone",
    "get_state", "get_messages", "get_entries", "get_tree",
    "get_fork_messages", "get_last_assistant_text", "get_session_stats",
    "set_session_name", "export_html",
    "set_model", "cycle_model", "get_available_models",
    "set_thinking_level", "cycle_thinking_level",
    "get_available_thinking_levels",
    "set_steering_mode", "set_follow_up_mode",
    "compact", "set_auto_compaction",
    "set_auto_retry", "abort_retry",
    "bash", "abort_bash",
    "get_commands",
})

# Agent -> client, streamed on stdout. A driver must branch on every one of
# these explicitly; anything reaching a fallback branch is a protocol change
# we have not read yet, not a thing to swallow.
EVENTS = frozenset({
    "agent_start", "agent_end", "agent_settled",
    "turn_start", "turn_end",
    "message_start", "message_update", "message_end",
    "tool_execution_start", "tool_execution_update", "tool_execution_end",
    "bash_execution_update",
    "queue_update",
    "compaction_start", "compaction_end",
    "auto_retry_start", "auto_retry_end",
    "summarization_retry_scheduled",
    "summarization_retry_attempt_start",
    "summarization_retry_finished",
    "extension_error",
})

# Agent -> client requests that expect a response written back on stdin.
# Puffo has no interactive operator at this layer, so a driver must answer
# or decline each one; leaving them unanswered stalls the agent.
EXTENSION_UI_REQUESTS = frozenset({
    "select", "confirm", "input", "editor",
    "notify", "setStatus", "setWidget", "setTitle", "set_editor_text",
})

# Capability values this harness will declare, from the command surface above.
# steer is GATED, not CURRENT_TURN: docs/rpc.md states a steering message is
# delivered "after the current assistant turn finishes executing its tool
# calls, before the next LLM call" -- queued to a boundary, not injected.
DECLARED_CAPABILITIES = {
    "lifecycle": "persistent_child",
    "busy_delivery": "steer",
    "steer": "gated",
    "cancel": "typed",            # `abort` is a command with a response
    "context_status": "pull",     # get_session_stats / get_state
    "compact": "typed",
    "session_resume": True,
    "permission_bridge": False,   # Pi ships no permission gate at all
}
