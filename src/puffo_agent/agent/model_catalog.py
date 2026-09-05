"""Per-provider model catalogs.

Each harness exposes selectable models = aliases (the CLI resolves
these to the latest model in the family at runtime, so they never go
stale) + concrete versions. claude-code refreshes its concrete list
from the live, account-authoritative ``/v1/models`` — so new models
appear without a code change; codex reads its local CLI cache; Pi and
OpenCode query their installed CLIs; the rest are static.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
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
    # None means the catalog makes no model-specific claim. An empty tuple is
    # an explicit claim that this model exposes no selectable inference level.
    supported_inference_levels: tuple[str, ...] | None = None


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

# codex reads its own local model cache (see _codex_models); these are
# the fallback when that cache is unreadable.
_CODEX_STATIC: tuple[ModelOption, ...] = (
    ModelOption("gpt-5.5", "GPT-5.5"),
    ModelOption("gpt-5.4", "GPT-5.4"),
    ModelOption("gpt-5.4-mini", "GPT-5.4-Mini"),
)

# hermes / gemini-cli are static for now.
# TODO: a dynamic source for gemini (Google API) like claude / codex.
_STATIC: dict[str, tuple[ModelOption, ...]] = {
    "hermes": (
        ModelOption("gpt-5.5", "GPT-5.5"),
        ModelOption("gpt-5.4", "GPT-5.4"),
        ModelOption("opus", "opus — latest Opus", is_alias=True),
        ModelOption("sonnet", "sonnet — latest Sonnet", is_alias=True),
    ),
    "gemini-cli": (
        ModelOption("gemini-2.5-pro", "Gemini 2.5 Pro"),
        ModelOption("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ),
}

# Harnesses the catalog can answer for (Claude, Pi, and OpenCode are dynamic).
KNOWN_HARNESSES: tuple[str, ...] = (
    "claude-code", "codex", "pi", "opencode", "gemini-cli", "hermes",
)

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_CACHE_TTL_S = 3600.0
_FETCH_TIMEOUT_S = 6.0
_DYNAMIC_CACHE_TTL_S = 20.0
_CATALOG_REMOVAL_MISSES = 3
_CATALOG_REMOVAL_GRACE_S = 15 * 60.0
MAX_CUSTOM_MODEL_ID_LENGTH = 128

# "claude-code" -> (fetched_at, concrete_models). Guarded by _lock.
_cache: dict[str, tuple[float, tuple[ModelOption, ...]]] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class _CatalogObservation:
    models: tuple[ModelOption, ...]
    revision: str


@dataclass(frozen=True)
class _MissingEvidence:
    model_id: str
    successful_misses: int
    first_missing_at: float


@dataclass(frozen=True)
class _CatalogState:
    models: tuple[ModelOption, ...]
    last_observation: str
    missing: tuple[_MissingEvidence, ...] = ()


@dataclass(frozen=True)
class CatalogStabilizer:
    """Persist and de-flake one account-scoped discovery catalog.

    Discovery is advisory: additions appear immediately, while removals need
    repeated, time-separated evidence. Failed and empty observations never
    erase the last-known-good list.
    """

    harness: str
    account_fingerprint: str
    fallback: tuple[ModelOption, ...]
    removal_misses: int = _CATALOG_REMOVAL_MISSES
    removal_grace_seconds: float = _CATALOG_REMOVAL_GRACE_S

    def stabilize(
        self, observation: _CatalogObservation | None,
    ) -> tuple[ModelOption, ...]:
        with _lock:
            state = self._load()
            if observation is None or not observation.models:
                return state.models if state is not None else self.fallback
            if state is not None and observation.revision == state.last_observation:
                return state.models
            updated = self._reconcile(state, observation, now=time.time())
            self._save(updated)
            return updated.models

    def _reconcile(
        self,
        state: _CatalogState | None,
        observation: _CatalogObservation,
        *,
        now: float,
    ) -> _CatalogState:
        if state is None:
            return _CatalogState(observation.models, observation.revision)
        observed_ids = {model.id for model in observation.models}
        prior_missing = {item.model_id: item for item in state.missing}
        retained: list[ModelOption] = []
        missing: list[_MissingEvidence] = []
        for model in state.models:
            if model.id in observed_ids:
                continue
            evidence = prior_missing.get(model.id)
            evidence = _MissingEvidence(
                model.id,
                (evidence.successful_misses + 1) if evidence else 1,
                evidence.first_missing_at if evidence else now,
            )
            old_enough = now - evidence.first_missing_at >= self.removal_grace_seconds
            if evidence.successful_misses >= self.removal_misses and old_enough:
                continue
            retained.append(model)
            missing.append(evidence)
        return _CatalogState(
            (*observation.models, *retained),
            observation.revision,
            tuple(missing),
        )

    def _path(self) -> Path:
        from ..portal.state import home_dir

        name = f"{self.harness}-{self.account_fingerprint}.json"
        return home_dir() / "cache" / "model-catalogs" / name

    def _load(self) -> _CatalogState | None:
        try:
            raw = json.loads(self._path().read_text(encoding="utf-8"))
            if raw.get("version") != 1:
                return None
            models = tuple(_model_option_from_json(item) for item in raw["models"])
            missing = tuple(
                _MissingEvidence(
                    str(item["model_id"]),
                    int(item["successful_misses"]),
                    float(item["first_missing_at"]),
                )
                for item in raw.get("missing", [])
            )
            return _CatalogState(models, str(raw["last_observation"]), missing)
        except (KeyError, OSError, TypeError, ValueError):
            return None

    def _save(self, state: _CatalogState) -> None:
        from ..portal.host_assets import _atomic_write_private

        body = {
            "version": 1,
            "harness": self.harness,
            "account_fingerprint": self.account_fingerprint,
            "last_observation": state.last_observation,
            "models": [_model_option_to_json(model) for model in state.models],
            "missing": [
                {
                    "model_id": item.model_id,
                    "successful_misses": item.successful_misses,
                    "first_missing_at": item.first_missing_at,
                }
                for item in state.missing
            ],
        }
        try:
            _atomic_write_private(self._path(), json.dumps(body, indent=2))
        except OSError as exc:
            logger.debug("could not persist %s model catalog: %s", self.harness, exc)


def _model_option_to_json(model: ModelOption) -> dict[str, object]:
    return {
        "id": model.id,
        "label": model.label,
        "is_alias": model.is_alias,
        "supported_inference_levels": (
            list(model.supported_inference_levels)
            if model.supported_inference_levels is not None
            else None
        ),
    }


def _model_option_from_json(raw: object) -> ModelOption:
    if not isinstance(raw, dict):
        raise ValueError("model option must be an object")
    levels = raw.get("supported_inference_levels")
    if levels is not None and not isinstance(levels, list):
        raise ValueError("supported_inference_levels must be a list or null")
    return ModelOption(
        id=str(raw["id"]),
        label=str(raw["label"]),
        is_alias=bool(raw.get("is_alias", False)),
        supported_inference_levels=(
            tuple(str(level) for level in levels) if levels is not None else None
        ),
    )


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


def _jwt_account_identity(token: object) -> str:
    if not isinstance(token, str) or token.count(".") < 2:
        return ""
    try:
        payload = token.split(".", 2)[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return ""
    provider_claims = claims.get("https://api.openai.com/auth") or {}
    if not isinstance(provider_claims, dict):
        provider_claims = {}
    return str(
        provider_claims.get("chatgpt_account_id")
        or provider_claims.get("account_id")
        or claims.get("sub")
        or ""
    )


def _codex_account_fingerprint() -> str:
    """Opaque scope for persisted discovery, never exposed or logged."""
    source_home = Path.home()
    path = source_home / ".codex" / "auth.json"
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        auth = {}
    tokens = auth.get("tokens") or {}
    if not isinstance(tokens, dict):
        tokens = {}
    identity = str(tokens.get("account_id") or "")
    if not identity:
        identity = _jwt_account_identity(tokens.get("id_token"))
    if not identity:
        identity = str(
            auth.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        )
    source = "\0".join((
        "puffo-model-catalog-v1",
        "codex",
        str(source_home.resolve()),
        str(auth.get("auth_mode") or "unknown"),
        identity or "unauthenticated",
    ))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def _codex_model_option(raw: dict) -> ModelOption:
    levels = raw.get("supported_reasoning_levels")
    supported: tuple[str, ...] | None = None
    if isinstance(levels, list):
        from ..mcp.config import REASONING_EFFORTS

        advertised = {
            str(item.get("effort"))
            for item in levels
            if isinstance(item, dict) and item.get("effort")
        }
        supported = tuple(
            level for level in REASONING_EFFORTS if level in advertised
        )
    return ModelOption(
        raw["slug"],
        raw.get("display_name") or raw["slug"],
        supported_inference_levels=supported,
    )


def _codex_observation() -> _CatalogObservation | None:
    """Parse one successful, non-empty Codex discovery snapshot."""
    path = Path.home() / ".codex" / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    listed = sorted(
        (
            m for m in data.get("models", [])
            if isinstance(m, dict)
            and m.get("slug")
            and m.get("visibility") == "list"
        ),
        key=lambda m: m.get("priority", 9999),
    )
    models = tuple(_codex_model_option(model) for model in listed)
    if not models:
        return None
    revision_body = {
        "etag": data.get("etag"),
        "fetched_at": data.get("fetched_at"),
        "models": [_model_option_to_json(model) for model in models],
    }
    revision = hashlib.sha256(
        json.dumps(revision_body, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return _CatalogObservation(models, revision)


def _codex_models() -> tuple[ModelOption, ...]:
    """Stable advisory view over the Codex CLI's volatile local cache."""
    return CatalogStabilizer(
        harness="codex",
        account_fingerprint=_codex_account_fingerprint(),
        fallback=_CODEX_STATIC,
    ).stabilize(_codex_observation())


def validate_model_id(model: str) -> str:
    """Validate only transport-safe model syntax; discovery is advisory."""
    if not isinstance(model, str):
        raise ValueError("model must be a string")
    if not model or model != model.strip():
        raise ValueError("model must be a non-empty trimmed string")
    if len(model) > MAX_CUSTOM_MODEL_ID_LENGTH:
        raise ValueError(
            f"model must be at most {MAX_CUSTOM_MODEL_ID_LENGTH} characters"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in model):
        raise ValueError("model must not contain control characters")
    return model


def _cached_models(
    harness: str, *, ttl_seconds: float,
) -> tuple[ModelOption, ...] | None:
    now = time.time()
    with _lock:
        cached = _cache.get(harness)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1]
    return None


def _stale_models(harness: str) -> tuple[ModelOption, ...]:
    with _lock:
        cached = _cache.get(harness)
    return cached[1] if cached else ()


def _store_models(
    harness: str, models: tuple[ModelOption, ...],
) -> tuple[ModelOption, ...]:
    with _lock:
        _cache[harness] = (time.time(), models)
    return models


def _opencode_models(*, fetch: bool) -> tuple[ModelOption, ...]:
    cached = _cached_models("opencode", ttl_seconds=_DYNAMIC_CACHE_TTL_S)
    if cached is not None or not fetch:
        return cached if cached is not None else _stale_models("opencode")
    from .cli_bin import resolve_opencode_bin
    from .opencode_auth import OpenCodeProbeError, list_opencode_model_catalog

    executable = resolve_opencode_bin()
    if not executable:
        return _store_models("opencode", ())
    try:
        models = list_opencode_model_catalog(executable)
    except OpenCodeProbeError:
        return _stale_models("opencode")
    from ..mcp.config import OPENCODE_INFERENCE_LEVELS

    options = tuple(
        ModelOption(
            model.id,
            model.id,
            supported_inference_levels=tuple(
                level for level in OPENCODE_INFERENCE_LEVELS
                if (
                    level in model.variants
                    or (level == "off" and "none" in model.variants)
                )
            ),
        )
        for model in models
    )
    return _store_models("opencode", options)


def _pi_models(*, fetch: bool) -> tuple[ModelOption, ...]:
    cached = _cached_models("pi", ttl_seconds=_DYNAMIC_CACHE_TTL_S)
    if cached is not None or not fetch:
        return cached if cached is not None else _stale_models("pi")
    from .cli_bin import resolve_pi_bin
    from .pi_auth import PiAuthProbeError, list_pi_models

    executable = resolve_pi_bin()
    if not executable:
        return _store_models("pi", ())
    try:
        models = list_pi_models(
            executable,
            config_dir=Path.home() / ".pi" / "agent",
        )
    except PiAuthProbeError:
        return _stale_models("pi")
    from ..mcp.config import PI_INFERENCE_LEVELS

    return _store_models("pi", tuple(
        ModelOption(
            model_id,
            label,
            supported_inference_levels=(
                PI_INFERENCE_LEVELS if supports_thinking else ("off",)
            ),
        )
        for model_id, label, supports_thinking in models
    ))


def provider_models(harness: str, *, fetch: bool = False) -> list[ModelOption]:
    """Selectable models for ``harness``: daemon-default + aliases +
    concrete versions.

    ``fetch`` only affects claude-code: when True it may hit
    ``/v1/models`` synchronously (use off the UI thread — see
    ``prefetch``); when False it serves the cache or the static
    fallback without blocking. codex reads its local cache; the rest
    are static.
    """
    if harness == "claude-code":
        # General aliases (opus/sonnet) sort after the concrete versions.
        return [_DAEMON_DEFAULT, *_claude_concrete(fetch=fetch), *_CLAUDE_ALIASES]
    if harness == "codex":
        return [_DAEMON_DEFAULT, *_codex_models()]
    if harness == "pi":
        return [_DAEMON_DEFAULT, *_pi_models(fetch=fetch)]
    if harness == "opencode":
        return [_DAEMON_DEFAULT, *_opencode_models(fetch=fetch)]
    return [_DAEMON_DEFAULT, *_STATIC.get(harness, ())]


def prefetch() -> threading.Thread:
    """Warm dynamic model lists in a background thread (call once
    at UI/daemon start so later ``provider_models`` reads hit cache).
    Returns the thread; callers may ignore it."""
    t = threading.Thread(
        target=lambda: [
            provider_models(harness, fetch=True)
            for harness in ("claude-code", "pi", "opencode")
        ],
        daemon=True,
    )
    t.start()
    return t
