"""Usage-limit (quota) markers. Sibling of ``_auth_markers``.

Call order at every site: usage-limit BEFORE auth — auth markers are bare
substrings and a spent-quota body carries auth-adjacent wording.
Substring match: adapter error output only, not free-form agent prose.
"""

from __future__ import annotations

import re

# stable cores, not full sentences: spelling drifts per release.
# Plan-scoped by construction: these are the spellings Claude Code /
# Codex ship for the account/plan budget, so they mean drained even if
# surrounding text happens to mention a model.
PLAN_LIMIT_MARKERS: tuple[str, ...] = (
    "usage limit reached",
    "hour limit reached",
    "weekly limit reached",
    "limit will reset at",
    "you've hit your usage limit",
    "you have hit your usage limit",
)

# Generic quota vocabulary: also emitted for per-model / per-project
# ceilings, so it means plan-drained only when the body does not scope
# the limit to a model.
GENERIC_QUOTA_MARKERS: tuple[str, ...] = (
    "quota exceeded",
    "insufficient_quota",
)

USAGE_LIMIT_MARKERS: tuple[str, ...] = (
    PLAN_LIMIT_MARKERS + GENERIC_QUOTA_MARKERS
)

_MODEL_SCOPED_RE = re.compile(r"\bmodels?\b")


# one wording across worker + snapshot paths; says nothing about signing in
DRAINED_RUNTIME_ERROR = (
    "Usage limit reached — the plan's quota for this account is spent. "
    "Holding messages until the window resets. Not a sign-in problem."
)


def looks_like_usage_limit(text: str) -> bool:
    """True only for plan/account-level exhaustion. A generic quota
    marker in a body that scopes the limit to a model (\"quota exceeded
    for model X; other models remain available\") is a per-model
    ceiling, not a drained account."""
    if not text:
        return False
    low = text.lower()
    if any(marker in low for marker in PLAN_LIMIT_MARKERS):
        return True
    if any(marker in low for marker in GENERIC_QUOTA_MARKERS):
        return _MODEL_SCOPED_RE.search(low) is None
    return False


# only the `|<epoch>` spelling is unambiguous; prose forms are tz-ambiguous
_EPOCH_RE = re.compile(r"usage limit reached\|(\d{9,11})\b", re.IGNORECASE)


def parse_reset_epoch(text: str) -> int | None:
    """Unix epoch, or ``None``. Callers degrade rather than guess."""
    if not text:
        return None
    m = _EPOCH_RE.search(text)
    return int(m.group(1)) if m else None
