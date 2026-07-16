"""Shared message-size limits — single definition, imported everywhere."""

# A single inbound message's text above this many chars is redacted from
# the prompt to a placeholder; the full body pages back on demand via
# ``get_post_segment``.
MAX_INLINE_MESSAGE_CHARS = 16000

# Page size (chars) that ``get_post_segment`` returns redacted bodies in;
# the redaction placeholder cites it so paging stays aligned.
MESSAGE_SEGMENT_CHARS = 8000

# PUF-384: on catch-up (reconnect/restart/resume), inbound messages older
# than this many hours are stored to chat history but skip the LLM
# pipeline. <= 0 disables the gate.
DEFAULT_CATCHUP_STALE_HOURS = 48.0
