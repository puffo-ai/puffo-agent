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
    # Python's Windows os.environ uppercases keys. Node needs SYSTEMROOT
    # for its crypto initialization before the Codex launcher can run.
    "USERPROFILE", "SystemRoot", "SYSTEMROOT", "SystemDrive", "windir", "WINDIR",
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

# Windows environment variable names are case-insensitive: ``PATH`` and
# ``Path`` are one variable, and Python's Windows ``os.environ`` upper-cases
# every key it reports. POSIX names are case-sensitive and stay that way.
#
# Matching the allowlist with ``in`` therefore silently dropped every entry
# written in mixed case -- ``SystemDrive``, ``ProgramData``, ``ProgramFiles``,
# ``ComSpec`` never matched the ``SYSTEMDRIVE`` / ``PROGRAMDATA`` /
# ``PROGRAMFILES`` / ``COMSPEC`` the OS actually hands us, so children lost
# them and failed to start. Adding an upper-case twin per name is the same
# mistake this module was written to avoid: it only fixes the names someone
# remembered to spell twice. Fold the key instead.
_BASE_ALLOWLIST_FOLDED = frozenset(name.upper() for name in _BASE_ALLOWLIST)

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
    # Subscription-plan credentials. Listed here for the same reason as the
    # API keys above: ambient inheritance and env_overrides must not be able
    # to smuggle one in, so only ``controlled`` can set them.
    "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_SUBSCRIPTION_AUTH_JSON",
})


def _allowed(name: str, *, fold: bool) -> bool:
    if fold:
        key = name.upper()
        return key in _BASE_ALLOWLIST_FOLDED or key.startswith(_ALLOWED_PREFIXES)
    return name in _BASE_ALLOWLIST or name.startswith(_ALLOWED_PREFIXES)


class _EnvMap:
    """Insertion-ordered env map whose keys may be case-insensitive.

    Plain ``dict.update`` is wrong on Windows: merging an override spelled
    ``Path`` into an ambient ``PATH`` yields *two* keys for one variable, and
    which one the child then sees is not ours to decide. Setting an existing
    variable therefore replaces its value in place and keeps the casing of
    whoever introduced it, so the result has exactly one entry per variable
    and nothing that already worked changes spelling.
    """

    def __init__(self, *, fold: bool) -> None:
        self._fold = fold
        self._names: dict[str, str] = {}   # folded key -> name as stored
        self._values: dict[str, str] = {}  # name as stored -> value

    def _key(self, name: str) -> str:
        return name.upper() if self._fold else name

    def set(self, name: str, value: str) -> None:
        stored = self._names.get(self._key(name))
        if stored is None:
            self._names[self._key(name)] = name
            self._values[name] = value
        else:
            self._values[stored] = value

    def discard(self, name: str) -> None:
        stored = self._names.pop(self._key(name), None)
        if stored is not None:
            self._values.pop(stored, None)

    def as_dict(self) -> dict[str, str]:
        return dict(self._values)


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

    Whether names fold is read from the host and is deliberately not a
    parameter. On Windows the fold is what makes the credential strip cover
    every spelling of a name, so a caller able to switch it off would be a
    caller able to turn that strip back into the leak it was. Tests reach
    ``_build_child_environment`` directly to exercise both platforms.
    """
    return _build_child_environment(
        overrides=overrides,
        controlled=controlled,
        extra_allowed=extra_allowed,
        source=source,
        fold=os.name == "nt",
    )


def _build_child_environment(
    *,
    overrides: Mapping[str, str] | None,
    controlled: Mapping[str, str] | None,
    extra_allowed: Iterable[str],
    source: Mapping[str, str] | None,
    fold: bool,
) -> dict[str, str]:
    """Implementation with the platform decision passed in explicitly."""
    ambient = os.environ if source is None else source
    allowed_extra = frozenset(
        name.upper() if fold else name for name in extra_allowed
    )

    env = _EnvMap(fold=fold)
    for name, value in ambient.items():
        key = name.upper() if fold else name
        if _allowed(name, fold=fold) or key in allowed_extra:
            env.set(name, value)

    if overrides:
        for name, value in overrides.items():
            env.set(name, value)

    # Post-merge strip: covers both the ambient inheritance and any override
    # that tried to set one of these. ``discard`` folds the name, so on
    # Windows an override spelled ``Anthropic_Api_Key`` cannot slip past and
    # reach the child -- that spelling is the same variable there, and a
    # case-sensitive ``pop`` would have defeated this ordering for it.
    for name in PROVIDER_CREDENTIAL_ENV_NAMES:
        env.discard(name)

    if controlled:
        for name, value in controlled.items():
            env.set(name, value)

    return env.as_dict()
