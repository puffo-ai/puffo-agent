"""Single builder for harness child-process environments.

Two shipped drivers had drifted apart here. The Claude path deliberately
stripped ``ANTHROPIC_API_KEY`` twice -- once before applying config overrides
and once after, so an override could not smuggle the ambient key back in --
then injected only a controlled key. The Codex path did
``{**os.environ, **overrides, ...}`` with no strip at all, so an ambient
``OPENAI_API_KEY`` reached the child.

Rather than copy the better deny-list to the second site, this builds the
environment from an allowlist. A deny-list only stops the names someone
thought to write down; the failure it cannot see is a new secret-bearing
variable nobody has named yet.

Layering: this constructs what a child *may* see. Which controlled credential
a runtime then injects stays with that runtime.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

# What a process needs to run at all: locate binaries, find its home, resolve
# names, speak the right locale, write temp files, trust the right CAs.
# Deliberately not "everything that is not a secret".
_BASE_ALLOWLIST = frozenset({
    # POSIX process essentials
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TZ", "TERM",
    "TMPDIR", "TMP", "TEMP",
    "LANG", "LANGUAGE",
    # Windows process essentials
    "USERPROFILE", "SystemRoot", "SystemDrive", "windir", "WINDIR",
    "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles",
    "ComSpec", "PATHEXT", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "COMPUTERNAME", "USERDOMAIN",
    # Network egress + TLS trust. Omitting these silently breaks agents behind
    # a corporate proxy or a custom CA bundle, which is a support nightmare
    # that looks like "the harness is broken".
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE",
})

# Prefixes kept wholesale: locale categories and XDG base directories are
# open-ended by specification, so enumerating them is not possible.
_ALLOWED_PREFIXES = ("LC_", "XDG_")

# Names a child must never inherit from the ambient environment, and which an
# override is never allowed to reintroduce. A runtime that legitimately needs
# one passes it through ``controlled``.
PROVIDER_CREDENTIAL_ENV_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY", "OPENAI_API_BASE",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "XAI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY",
    "OPENROUTER_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY",
    "PERPLEXITY_API_KEY", "COHERE_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "GH_TOKEN", "GITHUB_TOKEN",
})


def _allowed(name: str) -> bool:
    return name in _BASE_ALLOWLIST or name.startswith(_ALLOWED_PREFIXES)


def build_child_environment(
    *,
    overrides: Mapping[str, str] | None = None,
    controlled: Mapping[str, str] | None = None,
    extra_allowed: Iterable[str] = (),
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment from an allowlist.

    ``overrides``  operator-configured, explicitly non-credential.
    ``controlled`` the runtime's own credential/config injection. Applied
                   last and exempt from the strip -- this is the one path by
                   which a provider key may legitimately reach a child.
    ``extra_allowed`` names a specific runtime needs (e.g. ``CODEX_HOME``).

    Order matters: strip after merging overrides, not only before, so an
    override cannot reintroduce an ambient secret. That ordering is the one
    thing the Claude path already got right and is preserved here.
    """
    ambient = os.environ if source is None else source
    allowed_extra = frozenset(extra_allowed)

    env = {
        name: value
        for name, value in ambient.items()
        if _allowed(name) or name in allowed_extra
    }

    if overrides:
        env.update(overrides)

    # Post-merge strip: covers both the ambient inheritance and any override
    # that tried to set one of these.
    for name in PROVIDER_CREDENTIAL_ENV_NAMES:
        env.pop(name, None)

    if controlled:
        env.update(controlled)

    return env
