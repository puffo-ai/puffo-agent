"""PUF-409: per-agent context-window telemetry.

Mirrors claude-code's own auto-compact arithmetic so the operator-facing
numbers match what the CLI will actually do. Verified against claude-code
v2.1.199, whose threshold function is:

    threshold(window, pct) =
        min(floor(window * pct/100), window - COMPACT_HEADROOM_TOKENS)
        when 0 < pct <= 100, else window - COMPACT_HEADROOM_TOKENS

Two consequences worth keeping in mind when rendering this:

* The default is an absolute *offset* (``window - 13000``), not a fixed
  percentage — 93.5% of a 200K window but 98.7% of a 1M one.
* Because of the ``min``, a pct of 100 collapses onto the default. It does
  NOT disable auto-compact; nothing in this env var can.

``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` is an internal claude-code test knob, so
this module is a best-effort read: if a future CLI stops honouring it the
numbers here become advisory. Telemetry is ephemeral and never gates a turn.
"""

from __future__ import annotations

import os

# claude-code reserves this much of the window before auto-compacting.
COMPACT_HEADROOM_TOKENS = 13_000

# Fallback when the model's window can't be resolved.
DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000

# Substring → window. Matched against the lowercased model id, longest
# first, so "sonnet-4-5-1m" beats a bare "sonnet" entry.
_MODEL_WINDOWS: tuple[tuple[str, int], ...] = (
    ("[1m]", 1_000_000),
    ("-1m", 1_000_000),
)


def resolve_context_window(model: str = "", env: dict[str, str] | None = None) -> int:
    """Context window in tokens for ``model``.

    ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` wins when set (that's the same
    override claude-code itself reads); otherwise a 1M-context model id is
    detected by suffix and everything else falls back to 200K.
    """
    src = os.environ if env is None else env
    raw = (src.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or "").strip()
    if raw:
        try:
            explicit = int(float(raw))
            if explicit > 0:
                return explicit
        except ValueError:
            pass
    low = (model or "").lower()
    for needle, window in _MODEL_WINDOWS:
        if needle in low:
            return window
    return DEFAULT_CONTEXT_WINDOW_TOKENS


def parse_threshold_pct(raw: object) -> float | None:
    """The effective pct override, or None when claude-code would ignore it.

    claude-code applies the override only for ``0 < pct <= 100``; anything
    else silently falls back to the default, so it's reported as None rather
    than pretending the operator's value took effect.
    """
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


def compact_threshold_tokens(window: int, pct: float | None) -> int:
    """Token count at which claude-code triggers auto-compact."""
    default = max(0, window - COMPACT_HEADROOM_TOKENS)
    if pct is None:
        return default
    return min(int(window * pct / 100), default)


def build_context_telemetry(
    *,
    model: str = "",
    current_context_tokens: int = 0,
    env_overrides: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """The ``context_telemetry`` payload for one agent.

    ``threshold_is_default`` lets the UI say "Default" instead of showing a
    percentage the operator picked but claude-code collapsed onto the default
    (which is exactly what a 100 selection does).
    """
    overrides = env_overrides or {}
    window = resolve_context_window(model, env)
    pct = parse_threshold_pct(overrides.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"))
    threshold = compact_threshold_tokens(window, pct)
    default_threshold = compact_threshold_tokens(window, None)
    current = max(0, int(current_context_tokens or 0))
    return {
        "max_context": window,
        "current_context": current,
        "auto_compact_threshold": threshold,
        # Percent the operator selected, echoed back only when claude-code
        # would actually honour it.
        "auto_compact_threshold_pct": pct,
        "threshold_is_default": threshold == default_threshold,
        "used_pct": round(current / window * 100, 1) if window > 0 else 0.0,
    }
