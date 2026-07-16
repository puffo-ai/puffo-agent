"""Shared message-size limits — single definition, imported everywhere."""

# A single inbound message's text above this many chars is redacted from
# the prompt to a placeholder; the full body pages back on demand via
# ``get_post_segment``.
MAX_INLINE_MESSAGE_CHARS = 16000

# Page size (chars) that ``get_post_segment`` returns redacted bodies in;
# the redaction placeholder cites it so paging stays aligned.
MESSAGE_SEGMENT_CHARS = 8000

# Catch-up older than this is stored but skips the LLM; <= 0 disables.
DEFAULT_CATCHUP_STALE_HOURS = 48.0
