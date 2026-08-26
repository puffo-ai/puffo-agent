"""Usage-limit (quota) markers. Sibling of ``_auth_markers``.

Call order at every site: usage-limit BEFORE auth — auth markers are bare
substrings and a spent-quota body carries auth-adjacent wording.
Substring match: adapter error output only, not free-form agent prose.
"""

from __future__ import annotations

import re

# Shipped Claude Code / Codex plan-budget spellings — plan-scoped by
# construction. Stable cores, not full sentences: spelling drifts per release.
PLAN_LIMIT_MARKERS: tuple[str, ...] = (
    "usage limit reached",
    "hour limit reached",
    "weekly limit reached",
    "limit will reset at",
    "you've hit your usage limit",
    "you have hit your usage limit",
)

# Ambiguous on their own: also emitted for per-model / per-project ceilings.
GENERIC_QUOTA_MARKERS: tuple[str, ...] = (
    "quota exceeded",
    "insufficient_quota",
)

USAGE_LIMIT_MARKERS: tuple[str, ...] = (
    PLAN_LIMIT_MARKERS + GENERIC_QUOTA_MARKERS
)

# Plan scope must be grammatically bound to the marker — co-presence is not
# enough ("quota exceeded for model X; account quota remains available").
# Excludes "project": a per-project ceiling is not the provider plan.
_PLAN_SCOPE = r"(?:account|plan|subscription|organization)"
_PLAN_SCOPED_QUOTA_RES: tuple[re.Pattern[str], ...] = (
    # "quota exceeded for this account", "insufficient_quota on your plan"
    re.compile(
        rf"\b(?:quota exceeded|insufficient_quota)\b\s+"
        rf"(?:for|on|under)\s+(?:(?:this|the|your|our|my|an)\s+)?"
        rf"{_PLAN_SCOPE}\b",
    ),
    # "account quota exceeded", "organization's usage quota exceeded"
    re.compile(
        rf"\b{_PLAN_SCOPE}(?:'s)?\s+"
        rf"(?:(?:usage|spending|billing)\s+)?quota exceeded\b",
    ),
    # "account insufficient_quota", "subscription's insufficient_quota"
    re.compile(rf"\b{_PLAN_SCOPE}(?:'s)?\s+insufficient_quota\b"),
)


# one wording across worker + snapshot paths; says nothing about signing in
DRAINED_RUNTIME_ERROR = (
    "Usage limit reached — the plan's quota for this account is spent. "
    "Holding messages until the window resets. Not a sign-in problem."
)


def looks_like_usage_limit(text: str) -> bool:
    """True only for plan/account-level exhaustion, on positive evidence:
    a shipped plan spelling, or generic quota vocabulary explicitly
    scoped to the account/plan. A bare or model-/project-scoped
    ``quota exceeded`` stays ambiguous — draining the whole account on
    it would park an agent whose plan still has budget."""
    if not text:
        return False
    low = text.lower()
    if any(marker in low for marker in PLAN_LIMIT_MARKERS):
        return True
    if any(marker in low for marker in GENERIC_QUOTA_MARKERS):
        return any(pattern.search(low) for pattern in _PLAN_SCOPED_QUOTA_RES)
    return False


# only the `|<epoch>` spelling is unambiguous; prose forms are tz-ambiguous
_EPOCH_RE = re.compile(r"usage limit reached\|(\d{9,11})\b", re.IGNORECASE)


def parse_reset_epoch(text: str) -> int | None:
    """Unix epoch, or ``None``. Callers degrade rather than guess."""
    if not text:
        return None
    m = _EPOCH_RE.search(text)
    return int(m.group(1)) if m else None
