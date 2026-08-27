"""Shared message-size limits imported by the message runtime."""

# Messages above this size are represented by a placeholder in prompts.
MAX_INLINE_MESSAGE_CHARS = 16000

# Page size used when reading a redacted message body.
MESSAGE_SEGMENT_CHARS = 8000

# Older catch-up messages are stored but skip the model.
DEFAULT_CATCHUP_STALE_HOURS = 48.0

# Attachment limits apply at both native-E2EE and keyless ingress boundaries.
MAX_INBOUND_ATTACHMENTS = 10
MAX_INBOUND_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_INBOUND_ATTACHMENT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_INBOUND_IMAGE_PIXELS = 64_000_000
