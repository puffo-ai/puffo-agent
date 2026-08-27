"""(runtime, provider, harness) validity matrix.

Single source of truth for which combinations are supported, used
both at agent-load time and at CLI flag-parse time. Some harnesses
are bound to one provider (``claude-code`` → anthropic, ``gemini-cli``
→ google); ``hermes`` is multi-provider.

Supported today: ``cli-local`` with ``claude-code`` or ``codex``,
``cli-docker`` with ``claude-code`` or ``codex``, and ``ws-local``
(external tool, no internal engine). ``hermes`` and ``gemini-cli``
remain design-only — no runtime accepts them because their Driver and
operational contracts have not been implemented and verified. They stay
enumerated here so a persisted agent.yml carrying one fails with that explicit
diagnostic instead of a misleading "unknown harness".
"""

from __future__ import annotations

import logging
from typing import NamedTuple


logger = logging.getLogger(__name__)


# ── Enumerations ──────────────────────────────────────────────────────────────

RUNTIME_CLI_LOCAL   = "cli-local"
RUNTIME_CLI_DOCKER  = "cli-docker"
RUNTIME_WS_LOCAL    = "ws-local"  # external tool consumes over localhost WS
RUNTIME_CLI_SANDBOX = "cli-sandbox"  # reserved; not yet implemented

VALID_RUNTIMES: frozenset[str] = frozenset({
    RUNTIME_CLI_LOCAL,
    RUNTIME_CLI_DOCKER,
    RUNTIME_WS_LOCAL,
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

# Harness → providers it supports. ``resolve_effective_harness`` selects Codex
# for OpenAI on both harness-bearing CLI runtimes. The design-only
# Hermes/Gemini entries are kept so provider defaults still resolve to a
# named harness and ``validate_triple`` can reject it with the design-only
# diagnostic; no runtime admits them.
HARNESS_PROVIDERS: dict[str, frozenset[str]] = {
    HARNESS_CLAUDE_CODE: frozenset({PROVIDER_ANTHROPIC}),
    HARNESS_HERMES:      frozenset({PROVIDER_ANTHROPIC, PROVIDER_OPENAI}),
    HARNESS_GEMINI_CLI:  frozenset({PROVIDER_GOOGLE}),
    HARNESS_CODEX:       frozenset({PROVIDER_OPENAI}),
}


# Runtimes where ``harness`` is meaningful.
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
# ws-local has no internal engine — the external tool brings its own
# model — so provider/harness are inert; the entry keeps the map
# exhaustive over VALID_RUNTIMES.
DEFAULT_PROVIDER_FOR_RUNTIME: dict[str, str] = {
    RUNTIME_CLI_LOCAL:  PROVIDER_ANTHROPIC,
    RUNTIME_CLI_DOCKER: PROVIDER_ANTHROPIC,
    RUNTIME_WS_LOCAL:   PROVIDER_ANTHROPIC,
}

DEFAULT_HARNESS_FOR_PROVIDER: dict[str, str] = {
    PROVIDER_ANTHROPIC: HARNESS_CLAUDE_CODE,
    PROVIDER_OPENAI:    HARNESS_CODEX,
    PROVIDER_GOOGLE:    HARNESS_GEMINI_CLI,
}


# ── Legacy-name migration ─────────────────────────────────────────────────────

# Direct-provider runtimes were retired in favour of the host-local Driver
# runtime. Existing agent.yml files still load; AgentConfig normalizes their
# previously-inert harness field from the provider before validation.
_LEGACY_KIND_MIGRATIONS: dict[str, str] = {
    "chat-only": RUNTIME_CLI_LOCAL,
    "chat-local": RUNTIME_CLI_LOCAL,
    "sdk": RUNTIME_CLI_LOCAL,
    "sdk-local": RUNTIME_CLI_LOCAL,
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
            "auto-migrated to the CLI Driver runtime for this run; "
            "authenticate with `claude login` or `codex login`, then update "
            "agent.yml.",
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

    Empty ``provider`` / ``harness`` mean "use the default". Validation
    resolves those defaults so an unsupported inferred combination cannot
    pass here and fail later during Worker startup.
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
        harness = resolve_effective_harness(runtime, provider, harness)

    if harness not in VALID_HARNESSES:
        return ValidationResult(False, (
            f"unknown harness {harness!r} "
            f"(valid: {', '.join(sorted(VALID_HARNESSES))})"
        ))

    if runtime == RUNTIME_CLI_LOCAL and harness not in {
        HARNESS_CLAUDE_CODE,
        HARNESS_CODEX,
    }:
        return ValidationResult(False, (
            f"runtime {RUNTIME_CLI_LOCAL!r} supports only "
            f"{HARNESS_CLAUDE_CODE!r} and {HARNESS_CODEX!r}; "
            f"harness {harness!r} is not implemented by the Driver runtime"
        ))

    if runtime == RUNTIME_CLI_DOCKER and harness not in {
        HARNESS_CLAUDE_CODE,
        HARNESS_CODEX,
    }:
        return ValidationResult(False, (
            f"runtime {RUNTIME_CLI_DOCKER!r} supports only "
            f"{HARNESS_CLAUDE_CODE!r} and {HARNESS_CODEX!r}; "
            f"harness {harness!r} is design-only in this release — it "
            "cannot complete the metadata-notified Inbox contract. Keep "
            f"{RUNTIME_CLI_DOCKER!r} with harness {HARNESS_CLAUDE_CODE!r} "
            f"or {HARNESS_CODEX!r}, or set runtime.kind "
            f"{RUNTIME_CLI_LOCAL!r} with harness {HARNESS_CLAUDE_CODE!r} "
            f"or {HARNESS_CODEX!r}"
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
    set, or the provider-specific default. Both CLI runtimes default
    OpenAI to Codex and Anthropic to Claude Code.
    """
    if not harness_applies(runtime):
        return ""
    if harness:
        return harness
    provider = resolve_effective_provider(runtime, provider)
    return DEFAULT_HARNESS_FOR_PROVIDER.get(provider, HARNESS_CLAUDE_CODE)


def normalize_inference_level(
    runtime: str, provider: str, harness: str, inference_level: str,
) -> str:
    """Return ``inference_level`` if the effective harness supports it, else "".

    The single source of truth for the harness↔inference_level rule. Every
    writer of a runtime config calls this after applying its fields, so no
    writer can persist a level that ``AgentConfig.load`` then rejects.
    Value-based (rather than taking a ``RuntimeConfig``) to keep this module
    free of a ``state`` import cycle.
    """
    if not inference_level:
        return ""
    from ..mcp.config import supported_inference_levels

    effective = resolve_effective_harness(
        runtime, resolve_effective_provider(runtime, provider), harness
    )
    if inference_level in supported_inference_levels(effective):
        return inference_level
    return ""


__all__ = [
    # runtime constants
    "RUNTIME_CLI_LOCAL", "RUNTIME_CLI_DOCKER", "RUNTIME_WS_LOCAL",
    "RUNTIME_CLI_SANDBOX",
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
    "normalize_inference_level",
]
