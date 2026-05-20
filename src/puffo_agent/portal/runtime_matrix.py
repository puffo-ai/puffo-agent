"""(runtime, provider, harness) validity matrix.

Single source of truth for which combinations are supported, used
both at agent-load time and at CLI flag-parse time. Some harnesses
are bound to one provider (``claude-code`` → anthropic, ``gemini-cli``
→ google); ``hermes`` is multi-provider.
"""

from __future__ import annotations

import logging
from typing import NamedTuple


logger = logging.getLogger(__name__)


# ── Enumerations ──────────────────────────────────────────────────────────────

RUNTIME_CHAT_LOCAL  = "chat-local"
RUNTIME_SDK_LOCAL   = "sdk-local"
RUNTIME_CLI_LOCAL   = "cli-local"
RUNTIME_CLI_DOCKER  = "cli-docker"
RUNTIME_CLI_SANDBOX = "cli-sandbox"  # reserved; not yet implemented

VALID_RUNTIMES: frozenset[str] = frozenset({
    RUNTIME_CHAT_LOCAL,
    RUNTIME_SDK_LOCAL,
    RUNTIME_CLI_LOCAL,
    RUNTIME_CLI_DOCKER,
})

RESERVED_RUNTIMES: frozenset[str] = frozenset({
    RUNTIME_CLI_SANDBOX,
})


PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI    = "openai"
PROVIDER_GOOGLE    = "google"

VALID_PROVIDERS: frozenset[str] = frozenset({
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    PROVIDER_GOOGLE,
})


HARNESS_CLAUDE_CODE = "claude-code"
HARNESS_HERMES      = "hermes"
HARNESS_GEMINI_CLI  = "gemini-cli"
HARNESS_CODEX       = "codex"

VALID_HARNESSES: frozenset[str] = frozenset({
    HARNESS_CLAUDE_CODE,
    HARNESS_HERMES,
    HARNESS_GEMINI_CLI,
    HARNESS_CODEX,
})


# ── Constraints ───────────────────────────────────────────────────────────────

# Harness → providers it supports. ``codex`` is OpenAI-only but is
# NOT the default for openai (see DEFAULT_HARNESS_FOR_PROVIDER below);
# operators opt in via ``runtime.harness: codex`` in agent.yml.
HARNESS_PROVIDERS: dict[str, frozenset[str]] = {
    HARNESS_CLAUDE_CODE: frozenset({PROVIDER_ANTHROPIC}),
    HARNESS_HERMES:      frozenset({PROVIDER_ANTHROPIC, PROVIDER_OPENAI}),
    HARNESS_GEMINI_CLI:  frozenset({PROVIDER_GOOGLE}),
    HARNESS_CODEX:       frozenset({PROVIDER_OPENAI}),
}


# Runtimes where ``harness`` is meaningful. For chat-local and
# sdk-local the agent engine is implicit and the field is ignored.
_HARNESS_BEARING_RUNTIMES: frozenset[str] = frozenset({
    RUNTIME_CLI_LOCAL,
    RUNTIME_CLI_DOCKER,
})


def harness_applies(runtime: str) -> bool:
    """True when the runtime takes a ``harness`` field."""
    return runtime in _HARNESS_BEARING_RUNTIMES


# ── Default provider / harness per runtime ───────────────────────────────────

# Default provider when agent.yml omits the field. Matches
# ``DaemonConfig.default_provider``.
DEFAULT_PROVIDER_FOR_RUNTIME: dict[str, str] = {
    RUNTIME_CHAT_LOCAL: PROVIDER_ANTHROPIC,
    RUNTIME_SDK_LOCAL:  PROVIDER_ANTHROPIC,
    RUNTIME_CLI_LOCAL:  PROVIDER_ANTHROPIC,
    RUNTIME_CLI_DOCKER: PROVIDER_ANTHROPIC,
}

DEFAULT_HARNESS_FOR_PROVIDER: dict[str, str] = {
    PROVIDER_ANTHROPIC: HARNESS_CLAUDE_CODE,
    PROVIDER_OPENAI:    HARNESS_HERMES,
    PROVIDER_GOOGLE:    HARNESS_GEMINI_CLI,
}


# ── Legacy-name migration ─────────────────────────────────────────────────────

# Old ``runtime.kind`` values kept working with a one-time WARNING.
_LEGACY_KIND_MIGRATIONS: dict[str, str] = {
    "chat-only": RUNTIME_CHAT_LOCAL,
    "sdk":       RUNTIME_SDK_LOCAL,
}


def migrate_legacy_kind(raw_kind: str, agent_id: str = "") -> str:
    """Translate a legacy ``kind`` value to its current spelling.

    Returns the input unchanged when already current or unrecognised;
    downstream validation surfaces unknown values.
    """
    if raw_kind in _LEGACY_KIND_MIGRATIONS:
        new = _LEGACY_KIND_MIGRATIONS[raw_kind]
        logger.warning(
            "agent %s: runtime.kind %r is deprecated, use %r. "
            "auto-migrated for this run; please update agent.yml.",
            agent_id or "(?)", raw_kind, new,
        )
        return new
    return raw_kind


# ── Validation ────────────────────────────────────────────────────────────────


class ValidationResult(NamedTuple):
    ok: bool
    error: str  # empty when ok


def validate_triple(
    runtime: str, provider: str, harness: str,
) -> ValidationResult:
    """Check a (runtime, provider, harness) triple.

    Empty ``provider`` / ``harness`` mean "use the default" and are
    accepted; callers resolve defaults separately.
    """
    if runtime in RESERVED_RUNTIMES:
        return ValidationResult(False, (
            f"runtime kind {runtime!r} is reserved for a future release "
            "and not yet implemented"
        ))
    if runtime not in VALID_RUNTIMES:
        return ValidationResult(False, (
            f"unknown runtime kind {runtime!r} "
            f"(valid: {', '.join(sorted(VALID_RUNTIMES))})"
        ))

    if provider and provider not in VALID_PROVIDERS:
        return ValidationResult(False, (
            f"unknown provider {provider!r} "
            f"(valid: {', '.join(sorted(VALID_PROVIDERS))})"
        ))

    if not harness_applies(runtime):
        # Field is ignored for this runtime; accept any value.
        return ValidationResult(True, "")

    if not harness:
        # Empty means "use default" — resolved by caller.
        return ValidationResult(True, "")

    if harness not in VALID_HARNESSES:
        return ValidationResult(False, (
            f"unknown harness {harness!r} "
            f"(valid: {', '.join(sorted(VALID_HARNESSES))})"
        ))

    if provider:
        supported = HARNESS_PROVIDERS.get(harness, frozenset())
        if provider not in supported:
            return ValidationResult(False, (
                f"harness {harness!r} does not support provider "
                f"{provider!r} (supported: {', '.join(sorted(supported)) or '(none)'})"
            ))

    return ValidationResult(True, "")


def resolve_effective_provider(runtime: str, provider: str) -> str:
    """Return ``provider`` if set, else the runtime-specific default."""
    if provider:
        return provider
    return DEFAULT_PROVIDER_FOR_RUNTIME.get(runtime, PROVIDER_ANTHROPIC)


def resolve_effective_harness(runtime: str, provider: str, harness: str) -> str:
    """Return the effective harness for this runtime.

    Empty string when the field doesn't apply; otherwise the input if
    set, or the provider-specific default.
    """
    if not harness_applies(runtime):
        return ""
    if harness:
        return harness
    provider = resolve_effective_provider(runtime, provider)
    return DEFAULT_HARNESS_FOR_PROVIDER.get(provider, HARNESS_CLAUDE_CODE)


__all__ = [
    # runtime constants
    "RUNTIME_CHAT_LOCAL", "RUNTIME_SDK_LOCAL",
    "RUNTIME_CLI_LOCAL", "RUNTIME_CLI_DOCKER", "RUNTIME_CLI_SANDBOX",
    # provider constants
    "PROVIDER_ANTHROPIC", "PROVIDER_OPENAI", "PROVIDER_GOOGLE",
    # harness constants
    "HARNESS_CLAUDE_CODE", "HARNESS_HERMES", "HARNESS_GEMINI_CLI",
    "HARNESS_CODEX",
    # sets
    "VALID_RUNTIMES", "RESERVED_RUNTIMES",
    "VALID_PROVIDERS", "VALID_HARNESSES",
    "HARNESS_PROVIDERS",
    # helpers
    "harness_applies",
    "migrate_legacy_kind",
    "validate_triple",
    "ValidationResult",
    "resolve_effective_provider",
    "resolve_effective_harness",
]
