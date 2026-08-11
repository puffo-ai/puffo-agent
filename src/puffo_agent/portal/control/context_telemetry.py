"""Harness context limits and auto-compact configuration."""

from __future__ import annotations

import logging
import math
import os

MIN_CONTEXT_WINDOW_TOKENS = 100_000
MAX_CONTEXT_WINDOW_TOKENS = 1_000_000
MIN_CLAUDE_AUTOCOMPACT_TOKENS = 100_000

logger = logging.getLogger(__name__)

CLAUDE_COMPACT_PCT_KEY = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
CODEX_COMPACT_PCT_KEY = "CODEX_AUTOCOMPACT_PCT_OVERRIDE"


def compact_pct_key(harness: str) -> str:
    return CODEX_COMPACT_PCT_KEY if harness == "codex" else CLAUDE_COMPACT_PCT_KEY


def configured_compact_pct(
    harness: str, env_overrides: dict[str, str] | None,
) -> float | None:
    return parse_threshold_pct((env_overrides or {}).get(compact_pct_key(harness)))

_MODEL_WINDOWS: tuple[tuple[str, int], ...] = (
    # Legacy explicit extended-window aliases.
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
    ("haiku-4-5", 200_000),
)

def resolve_context_window(
    model: str = "", env: dict[str, str] | None = None
) -> int | None:
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
    logger.warning("Claude context window is unknown for model %r", model)
    return None


def parse_threshold_pct(raw: object) -> float | None:
    """Parse a syntactically valid Claude auto-compact override."""
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


def estimate_compact_threshold_tokens(
    window: int | None, pct: float | None
) -> int | None:
    if window is None or pct is None:
        return None
    return max(0, int(window * pct / 100))


def claude_autocompact_tokens(
    *,
    model: str = "",
    pct: float | None,
    max_context: int | None = None,
    env: dict[str, str] | None = None,
) -> int | None:
    """Translate the percentage control into Claude Code's token argument."""
    if pct is None:
        return None
    window = (
        max_context
        if isinstance(max_context, int)
        and not isinstance(max_context, bool)
        and max_context > 0
        else resolve_context_window(model, env)
    )
    threshold = estimate_compact_threshold_tokens(window, pct)
    if threshold is None:
        return None
    return max(MIN_CLAUDE_AUTOCOMPACT_TOKENS, threshold)


def compact_threshold_pct(
    window: int | None, threshold: int | None
) -> float | None:
    if (
        window is None
        or isinstance(threshold, bool)
        or not isinstance(threshold, int)
        or threshold <= 0
        or threshold > window
    ):
        return None
    return round(threshold / window * 100, 3)


def build_context_runtime(
    *,
    model: str = "",
    max_context: int | None = None,
    auto_compact_threshold: int | None = None,
    env_overrides: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Build context configuration persisted with runtime metadata."""
    overrides = env_overrides or {}
    configured_pct = parse_threshold_pct(overrides.get(CLAUDE_COMPACT_PCT_KEY))
    if configured_pct is not None:
        window = resolve_context_window(model, env)
        configured_threshold = (
            claude_autocompact_tokens(
                pct=configured_pct,
                max_context=window,
                env=env,
            )
            if window is not None
            else None
        )
        return {
            "max_context": window,
            "auto_compact_threshold_pct": compact_threshold_pct(
                window, configured_threshold
            ),
        }
    window = (
        max_context
        if isinstance(max_context, int)
        and not isinstance(max_context, bool)
        and max_context > 0
        else resolve_context_window(model, env)
    )
    reported_pct = compact_threshold_pct(window, auto_compact_threshold)
    return {
        "max_context": window,
        "auto_compact_threshold_pct": reported_pct,
    }
