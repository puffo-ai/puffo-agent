"""Channel format policy: channels.is_encrypted drives the outbound format.

Fail-safe: an unknown or missing policy answers encrypted.
"""

from __future__ import annotations

import json

from ..crypto.http_client import HttpError

CHANNEL_FORMAT_MISMATCH = "CHANNEL_FORMAT_MISMATCH"


def is_channel_format_mismatch(exc: Exception) -> bool:
    if not isinstance(exc, HttpError) or exc.status != 400:
        return False
    try:
        body = json.loads(exc.body or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(body, dict) and body.get("error") == CHANNEL_FORMAT_MISMATCH
