"""Claude Code context limits and auto-compact configuration."""

from __future__ import annotations

import logging
import math
import os

MIN_CONTEXT_WINDOW_TOKENS = 100_000
MAX_CONTEXT_WINDOW_TOKENS = 1_000_000

logger = logging.getLogger(__name__)

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
    agent_id: str = "",
) -> dict:
    """Build context configuration persisted with runtime metadata."""
    overrides = env_overrides or {}
    window = (
        max_context
        if isinstance(max_context, int)
        and not isinstance(max_context, bool)
        and max_context > 0
        else resolve_context_window(model, env)
    )
    configured_pct = parse_threshold_pct(
        overrides.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    )
    reported_pct = compact_threshold_pct(window, auto_compact_threshold)
    if (
        configured_pct is not None
        and reported_pct is not None
        and not math.isclose(configured_pct, reported_pct, abs_tol=0.001)
    ):
        logger.warning(
            "agent %s: Claude session compact threshold %g%% differs from "
            "configured override %g%%",
            agent_id or "<unknown>",
            reported_pct,
            configured_pct,
            extra={
                "agent_id": agent_id,
                "configured_threshold_pct": configured_pct,
                "session_threshold_pct": reported_pct,
            },
        )
    return {
        "max_context": window,
        "auto_compact_threshold_pct": (
            reported_pct if reported_pct is not None else configured_pct
        ),
    }
