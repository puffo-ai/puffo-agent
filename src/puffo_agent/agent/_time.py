"""Small time-format helpers shared across the agent layer."""

from datetime import datetime, timezone


def ms_to_iso(ms: int) -> str:
    """Render a ms-since-epoch timestamp as ISO 8601 in UTC.
    Empty string when ``ms`` is 0 / missing — callers drop the
    field rather than emitting an empty timestamp.
    """
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(
            ms / 1000, tz=timezone.utc,
        ).isoformat(timespec="seconds")
    except (ValueError, OSError):
        return ""
