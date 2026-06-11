"""Per-provider model catalogs.

Mirrors the reference agent's ``runtime-catalog.ts``: each harness
exposes selectable models = aliases (the CLI resolves these to the
latest model in the family at runtime, so they never go stale) +
concrete versions. The claude-code catalog refreshes its concrete
list from the live, account-authoritative ``/v1/models`` — so new
models (Fable 5, and whatever ships next) appear without a code change.
Other harnesses are static for now.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelOption:
    id: str  # the ``--model`` value; "" means the daemon default
    label: str  # combo-box display text
    is_alias: bool = False


_DAEMON_DEFAULT = ModelOption("", "(daemon default)")

# CLI aliases — claude-code resolves these to the latest model in the
# family at call time, so they track new releases with no edits here.
_CLAUDE_ALIASES: tuple[ModelOption, ...] = (
    ModelOption("opus", "opus — latest Opus", is_alias=True),
    ModelOption("sonnet", "sonnet — latest Sonnet", is_alias=True),
)

# Models filtered out of the live ``/v1/models`` result — old dated
# point-releases + the haiku tier — to keep the picker to opus/sonnet.
_BLOCKED_MODELS: frozenset[str] = frozenset({
    "claude-opus-4-5-20251101",
    "claude-opus-4-1-20250805",
    "claude-opus-4-20250514",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
    "claude-haiku-4-5-20251001",
})

# Offline fallback for claude-code — only consulted when ``/v1/models``
# is unreachable (the aliases + the live refresh otherwise keep it
# current).
_CLAUDE_STATIC: tuple[ModelOption, ...] = (
    ModelOption("claude-opus-4-8", "Claude Opus 4.8"),
    ModelOption("claude-opus-4-7", "Claude Opus 4.7"),
    ModelOption("claude-opus-4-6", "Claude Opus 4.6"),
    ModelOption("claude-sonnet-4-6", "Claude Sonnet 4.6"),
)

# Other harnesses: static for now.
# TODO: same /v1/models refresh against the OpenAI / Google endpoints.
_STATIC: dict[str, tuple[ModelOption, ...]] = {
    "codex": (
        ModelOption("gpt-5.5", "GPT-5.5"),
        ModelOption("gpt-5.4", "GPT-5.4"),
        ModelOption("gpt-5.4-mini", "GPT-5.4 Mini"),
        ModelOption("gpt-5.3-codex", "GPT-5.3 Codex"),
        ModelOption("gpt-5.2", "GPT-5.2"),
    ),
    "hermes": (
        ModelOption("opus", "opus — latest Opus", is_alias=True),
        ModelOption("sonnet", "sonnet — latest Sonnet", is_alias=True),
        ModelOption("gpt-5.5", "GPT-5.5"),
        ModelOption("gpt-5.4", "GPT-5.4"),
    ),
    "gemini-cli": (
        ModelOption("gemini-2.5-pro", "Gemini 2.5 Pro"),
        ModelOption("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ),
}

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_CACHE_TTL_S = 3600.0
_FETCH_TIMEOUT_S = 6.0

# "claude-code" -> (fetched_at, concrete_models). Guarded by _lock.
_cache: dict[str, tuple[float, tuple[ModelOption, ...]]] = {}
_lock = threading.Lock()


def _anthropic_oauth_token() -> str | None:
    """The operator's claude-code OAuth access token, or None."""
    path = Path.home() / ".claude" / ".credentials.json"
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (creds.get("claudeAiOauth") or {}).get("accessToken")


def _fetch_anthropic_models() -> tuple[ModelOption, ...] | None:
    """Account-authoritative model list from ``/v1/models``. Returns
    None on any failure (no creds, network, auth) so callers fall back.
    """
    token = _anthropic_oauth_token()
    if not token:
        return None
    req = urllib.request.Request(
        _ANTHROPIC_MODELS_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("anthropic /v1/models fetch failed: %s", exc)
        return None
    out = [
        ModelOption(m["id"], m.get("display_name") or m["id"])
        for m in data.get("data", [])
        if m.get("id") and m["id"] not in _BLOCKED_MODELS
    ]
    return tuple(out) or None


def _claude_concrete(*, fetch: bool) -> tuple[ModelOption, ...]:
    now = time.time()
    with _lock:
        cached = _cache.get("claude-code")
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]
    if fetch:
        live = _fetch_anthropic_models()
        if live is not None:
            with _lock:
                _cache["claude-code"] = (now, live)
            return live
    # Serve the last-known list even if stale; else the static fallback.
    return cached[1] if cached else _CLAUDE_STATIC


def provider_models(harness: str, *, fetch: bool = False) -> list[ModelOption]:
    """Selectable models for ``harness``: daemon-default + aliases +
    concrete versions.

    ``fetch`` only affects claude-code: when True it may hit
    ``/v1/models`` synchronously (use off the UI thread — see
    ``prefetch``); when False it serves the cache or the static
    fallback without blocking.
    """
    if harness == "claude-code":
        return [_DAEMON_DEFAULT, *_CLAUDE_ALIASES, *_claude_concrete(fetch=fetch)]
    return [_DAEMON_DEFAULT, *_STATIC.get(harness, ())]


def prefetch() -> threading.Thread:
    """Warm the claude-code live list in a background thread (call once
    at UI/daemon start so later ``provider_models`` reads hit cache).
    Returns the thread; callers may ignore it."""
    t = threading.Thread(
        target=lambda: provider_models("claude-code", fetch=True),
        daemon=True,
    )
    t.start()
    return t
