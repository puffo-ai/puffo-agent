"""Subscription-plan credentials for a CLI harness child process.

An agent reaches its model one of two ways (``runtime.auth_mode``):

``api-gateway``   a metered LLM proxy -- LiteLLM virtual key plus a base URL.
                  This is the default and is owned by the runtime itself; this
                  module does not touch it. Displayed as "API gateway".
``subscription``  the operator's own Claude/ChatGPT plan, billed to that plan
                  rather than per token. That is what this module resolves.

The two harnesses are **not symmetric**, which is the reason this returns a
struct rather than a plain env mapping:

* **claude-code** takes an environment variable -- a long-lived token from
  ``claude setup-token``.
* **codex** has no equivalent variable; its plan auth lives in an
  ``auth.json`` under ``CODEX_HOME``, so the credential must be *written* and
  the child must be told where to look.

The secret arrives as an argument, never from ``agent.yml`` and never read
here from the ambient environment. ``agent.yml`` is on disk, synced, and backed
up, so a token in it would outlive every place we can revoke it; and the
harness boundary is forbidden from re-reading ``os.environ`` (see
``tests/test_child_env_allowlist.py``). The daemon resolves the token once at
startup and passes it down, exactly as it does the gateway API key.

``build_child_environment`` drops anything outside its allowlist, so the only
way these reach the child is the ``controlled`` mapping -- which is exactly the
channel documented as "the one path by which a provider key may legitimately
reach a child".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

AUTH_MODE_API_GATEWAY = "api-gateway"
AUTH_MODE_SUBSCRIPTION = "subscription"
AUTH_MODES = frozenset({AUTH_MODE_API_GATEWAY, AUTH_MODE_SUBSCRIPTION})

#: Parent-environment variable holding a ``claude setup-token`` OAuth token.
CLAUDE_SUBSCRIPTION_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
#: Parent-environment variable holding a Codex ``auth.json`` document.
CODEX_SUBSCRIPTION_ENV = "CODEX_SUBSCRIPTION_AUTH_JSON"
#: Every name above, for the never-inherit set in ``child_env``.
SUBSCRIPTION_ENV_NAMES = frozenset({CLAUDE_SUBSCRIPTION_ENV, CODEX_SUBSCRIPTION_ENV})


class SubscriptionCredentialMissing(RuntimeError):
    """``auth_mode`` is ``subscription`` but no credential was supplied.

    Raised rather than falling back to the gateway. A silent fallback would
    keep working, bill the metered account, and look like success -- the one
    failure mode that hides itself.
    """


@dataclass(frozen=True)
class ChildCredentials:
    """What a harness child needs in order to authenticate.

    ``files`` maps an absolute path to its content; the caller writes each at
    mode ``0600`` before spawning. ``extra_allowed`` names variables the child
    may additionally inherit (``CODEX_HOME`` points at a directory, not a
    secret, so it travels as an allowance rather than a controlled value).
    """

    env: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[Path, str] = field(default_factory=dict)
    extra_allowed: tuple[str, ...] = ()


def resolve_subscription_credentials(
    harness: str,
    *,
    agent_home: Path,
    token: str,
) -> ChildCredentials:
    """Credentials for one subscription-mode agent, or raise.

    Call only when ``runtime.auth_mode == "subscription"``; the api-gateway
    path stays with the runtime that owns it. ``token`` is the daemon-resolved
    secret -- an OAuth token for claude-code, an ``auth.json`` document for
    codex.
    """
    if harness == "codex":
        return _codex(token, agent_home)
    return _claude_code(token)


def _claude_code(token: str) -> ChildCredentials:
    token = (token or "").strip()
    if not token:
        raise SubscriptionCredentialMissing(
            f"runtime.auth_mode is {AUTH_MODE_SUBSCRIPTION!r} for a claude-code "
            f"agent but no subscription token was supplied (daemon config / "
            f"{CLAUDE_SUBSCRIPTION_ENV}). Generate one with `claude setup-token`."
        )
    return ChildCredentials(env={CLAUDE_SUBSCRIPTION_ENV: token})


def _codex(token: str, agent_home: Path) -> ChildCredentials:
    blob = (token or "").strip()
    if not blob:
        raise SubscriptionCredentialMissing(
            f"runtime.auth_mode is {AUTH_MODE_SUBSCRIPTION!r} for a codex agent "
            f"but no subscription credential was supplied (daemon config / "
            f"{CODEX_SUBSCRIPTION_ENV}). It holds a Codex `auth.json` document."
        )
    codex_home = agent_home / ".codex"
    return ChildCredentials(
        env={"CODEX_HOME": str(codex_home)},
        files={codex_home / "auth.json": blob},
        extra_allowed=("CODEX_HOME",),
    )
