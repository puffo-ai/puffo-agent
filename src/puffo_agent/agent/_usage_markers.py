"""Usage-limit (quota) markers. Sibling of ``_auth_markers``.

Call order at every site: usage-limit BEFORE auth — auth markers are bare
substrings and a spent-quota body carries auth-adjacent wording.
Substring match: adapter error output only, not free-form agent prose.
"""

from __future__ import annotations

import re

# stable cores, not full sentences: spelling drifts per release
USAGE_LIMIT_MARKERS: tuple[str, ...] = (
    "usage limit reached",
    "hour limit reached",
    "weekly limit reached",
    "limit will reset at",
    "you've hit your usage limit",
    "you have hit your usage limit",
    "quota exceeded",
    "insufficient_quota",
)


# one wording across worker + snapshot paths; says nothing about signing in
DRAINED_RUNTIME_ERROR = (
    "Usage limit reached — the plan's quota for this account is spent. "
    "Holding messages until the window resets. Not a sign-in problem."
)


def looks_like_usage_limit(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in USAGE_LIMIT_MARKERS)


# only the `|<epoch>` spelling is unambiguous; prose forms are tz-ambiguous
_EPOCH_RE = re.compile(r"usage limit reached\|(\d{9,11})\b", re.IGNORECASE)


def parse_reset_epoch(text: str) -> int | None:
    """Unix epoch, or ``None``. Callers degrade rather than guess."""
    if not text:
        return None
    m = _EPOCH_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:  # pragma: no cover — \d+ can't fail int()
        return None
