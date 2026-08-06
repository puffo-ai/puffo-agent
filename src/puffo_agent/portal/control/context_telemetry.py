"""Best-effort Claude Code context-window telemetry.

Trigger tokens are estimates because Claude Code keeps version-specific
output and precompute reserves.
"""

from __future__ import annotations

import math
import os

DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000
DEFAULT_COMPACT_THRESHOLD_PCT = 95.0
MIN_CONTEXT_WINDOW_TOKENS = 100_000
MAX_CONTEXT_WINDOW_TOKENS = 1_000_000

_MODEL_WINDOWS: tuple[tuple[str, int], ...] = (
    ("[1m]", 1_000_000),
    ("-1m", 1_000_000),
    ("opus-5", 1_000_000),
    ("opus-4-8", 1_000_000),
    ("opus-4-7", 1_000_000),
    ("opus-4-6", 1_000_000),
    ("sonnet-5", 1_000_000),
    ("sonnet-4-6", 1_000_000),
    ("fable-5", 1_000_000),
    ("mythos-5", 1_000_000),
)


def resolve_context_window(model: str = "", env: dict[str, str] | None = None) -> int:
    """Resolve Claude Code's effective auto-compact window."""
    src = os.environ if env is None else env
    raw = (src.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or "").strip()
    if raw:
        try:
            parsed = float(raw)
            if (
                math.isfinite(parsed)
                and MIN_CONTEXT_WINDOW_TOKENS <= parsed <= MAX_CONTEXT_WINDOW_TOKENS
            ):
                return int(parsed)
        except (ValueError, OverflowError):
            pass
    low = (model or "").lower()
    for needle, window in _MODEL_WINDOWS:
        if needle in low:
            return window
    return DEFAULT_CONTEXT_WINDOW_TOKENS


def parse_threshold_pct(raw: object) -> float | None:
    """Return the effective override, or None when Claude Code ignores it."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        pct = float(text)
    except ValueError:
        return None
    if not (0 < pct <= 100):
        return None
    return pct


def estimate_compact_threshold_tokens(window: int, pct: float | None) -> int:
    """Approximate the trigger without claiming Claude Code internals."""
    effective_pct = min(
        pct or DEFAULT_COMPACT_THRESHOLD_PCT,
        DEFAULT_COMPACT_THRESHOLD_PCT,
    )
    return max(0, int(window * effective_pct / 100))


def build_context_telemetry(
    *,
    model: str = "",
    current_context_tokens: int = 0,
    env_overrides: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Build one agent's ``context_telemetry`` payload."""
    overrides = env_overrides or {}
    window = resolve_context_window(model, env)
    pct = parse_threshold_pct(overrides.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"))
    threshold = estimate_compact_threshold_tokens(window, pct)
    default_threshold = estimate_compact_threshold_tokens(window, None)
    current = max(0, int(current_context_tokens or 0))
    return {
        "max_context": window,
        "current_context": current,
        "auto_compact_threshold": threshold,
        "auto_compact_threshold_pct": pct,
        "threshold_is_default": threshold == default_threshold,
        "threshold_is_estimate": True,
        "used_pct": round(current / window * 100, 1) if window > 0 else 0.0,
    }
