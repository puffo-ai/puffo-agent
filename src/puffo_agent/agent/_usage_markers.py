"""Shared usage-limit (quota-exhausted) detection for provider output.

Sibling of :mod:`._auth_markers`. Exists for the same reason: the quota
classification has to be identical everywhere it's made, or a response
gets read as auth-expired in one place and quota-drained in another —
which is exactly the JYP mis-surface this module fixes.

**Order matters at every call site: check this BEFORE the auth markers.**
Claude Code's usage-limit responses sometimes carry auth-adjacent wording,
and ``looks_like_auth_error`` matches on bare substrings, so an auth-first
check swallows quota exhaustion and tells the operator to re-login — which
does nothing for a spent quota.

Substring match, same contract as ``_auth_markers``: safe on adapter error
output, NOT on free-form agent prose (the worker uses anchored patterns).
"""

from __future__ import annotations

# Claude Code has shipped several spellings of the same event across
# versions and surfaces ("Claude usage limit reached. Your limit will
# reset at 5pm", "Claude AI usage limit reached|1749924000",
# "5-hour limit reached ∙ resets 3pm", "Usage limit reached · resets at
# …"). Anchoring on one version's exact sentence would silently stop
# matching on the next release, so these key on the stable core of each
# spelling rather than the full phrase.
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


# ``runtime.error`` copy for a drained agent — shared by the worker and
# the usage-snapshot path so the operator sees one wording wherever the
# state was detected. Deliberately says nothing about signing in.
DRAINED_RUNTIME_ERROR = (
    "Usage limit reached — the plan's quota for this account is spent. "
    "Holding messages until the window resets. Not a sign-in problem."
)


def looks_like_usage_limit(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in USAGE_LIMIT_MARKERS)
