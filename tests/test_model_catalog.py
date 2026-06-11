"""Provider model catalog: aliases + a live /v1/models refresh for
claude-code, static for other harnesses."""

from __future__ import annotations

import io
import json

import pytest

from puffo_agent.agent import model_catalog as mc
from puffo_agent.agent.model_catalog import ModelOption, provider_models


@pytest.fixture(autouse=True)
def _clear_cache():
    mc._cache.clear()
    yield
    mc._cache.clear()


def _ids(opts):
    return [o.id for o in opts]


def test_claude_code_default_and_aliases_offline(monkeypatch):
    monkeypatch.setattr(mc, "_fetch_anthropic_models", lambda: None)  # offline
    opts = provider_models("claude-code", fetch=True)
    ids = _ids(opts)
    assert ids[0] == ""  # daemon default first
    assert {"opus", "sonnet"} <= set(ids)  # aliases
    assert "haiku" not in ids and "opusplan" not in ids  # blocked aliases
    # static fallback = the curated 4 (no Fable 5 in the fallback)
    assert {"claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
            "claude-sonnet-4-6"} <= set(ids)
    assert "claude-fable-5" not in ids
    # general aliases sort to the end, after the concrete versions
    assert ids.index("opus") > ids.index("claude-opus-4-8")
    assert ids.index("sonnet") > ids.index("claude-sonnet-4-6")


def test_claude_code_prefers_live_models(monkeypatch):
    live = (
        ModelOption("claude-fable-5", "Claude Fable 5"),
        ModelOption("claude-zeta-9", "Claude Zeta 9"),  # a model static doesn't know
    )
    monkeypatch.setattr(mc, "_fetch_anthropic_models", lambda: live)
    ids = _ids(provider_models("claude-code", fetch=True))
    assert "opus" in ids  # aliases still prepended
    assert "claude-zeta-9" in ids  # surfaced from the live API
    assert "claude-opus-4-8" not in ids  # static list not used when live wins


def test_live_result_is_cached_within_ttl(monkeypatch):
    calls = {"n": 0}

    def _fetch():
        calls["n"] += 1
        return (ModelOption("claude-fable-5", "Claude Fable 5"),)

    monkeypatch.setattr(mc, "_fetch_anthropic_models", _fetch)
    provider_models("claude-code", fetch=True)
    provider_models("claude-code", fetch=True)
    assert calls["n"] == 1  # second call served from cache


def test_no_fetch_does_not_hit_the_api(monkeypatch):
    monkeypatch.setattr(
        mc, "_fetch_anthropic_models",
        lambda: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    ids = _ids(provider_models("claude-code"))  # fetch=False default
    assert "claude-opus-4-8" in ids  # served from static, no network


def test_codex_reads_local_cache(monkeypatch, tmp_path):
    cache = tmp_path / ".codex" / "models_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"models": [
        {"slug": "gpt-5.4", "display_name": "GPT-5.4", "visibility": "list", "priority": 16},
        {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list", "priority": 9},
        {"slug": "codex-auto-review", "display_name": "Codex Auto Review",
         "visibility": "hide", "priority": 43},
    ]}), encoding="utf-8")
    monkeypatch.setattr(mc.Path, "home", lambda: tmp_path)
    ids = _ids(provider_models("codex"))
    assert ids[0] == ""  # daemon default
    # visibility=hide excluded; ordered by priority (gpt-5.5 before gpt-5.4)
    assert ids[1:] == ["gpt-5.5", "gpt-5.4"]


def test_codex_fallback_when_cache_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(mc.Path, "home", lambda: tmp_path)  # no .codex dir
    assert _ids(provider_models("codex"))[1:] == ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]


def test_codex_fallback_on_bad_json(monkeypatch, tmp_path):
    cache = tmp_path / ".codex" / "models_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(mc.Path, "home", lambda: tmp_path)
    assert _ids(provider_models("codex"))[1:] == ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]


def test_unknown_harness_is_just_default():
    assert provider_models("nope") == [mc._DAEMON_DEFAULT]


def test_fetch_returns_none_without_token(monkeypatch):
    monkeypatch.setattr(mc, "_anthropic_oauth_token", lambda: None)
    assert mc._fetch_anthropic_models() is None


def test_fetch_parses_id_and_display_name(monkeypatch):
    monkeypatch.setattr(mc, "_anthropic_oauth_token", lambda: "tok")
    payload = {"data": [
        {"id": "claude-fable-5", "display_name": "Claude Fable 5"},
        {"id": "claude-opus-4-8"},  # no display_name -> label falls back to id
    ]}
    monkeypatch.setattr(
        mc.urllib.request, "urlopen",
        lambda req, timeout=None: io.BytesIO(json.dumps(payload).encode()),
    )
    out = mc._fetch_anthropic_models()
    assert _ids(out) == ["claude-fable-5", "claude-opus-4-8"]
    assert out[0].label == "Claude Fable 5"
    assert out[1].label == "claude-opus-4-8"


def test_fetch_drops_blocked_models(monkeypatch):
    monkeypatch.setattr(mc, "_anthropic_oauth_token", lambda: "tok")
    payload = {"data": [
        {"id": "claude-opus-4-8"},
        {"id": "claude-opus-4-20250514"},  # blocked
        {"id": "claude-sonnet-4-5-20250929"},  # blocked
        {"id": "claude-fable-5"},
    ]}
    monkeypatch.setattr(
        mc.urllib.request, "urlopen",
        lambda req, timeout=None: io.BytesIO(json.dumps(payload).encode()),
    )
    assert _ids(mc._fetch_anthropic_models()) == ["claude-opus-4-8", "claude-fable-5"]


def test_fetch_returns_none_on_network_error(monkeypatch):
    monkeypatch.setattr(mc, "_anthropic_oauth_token", lambda: "tok")

    def _boom(req, timeout=None):
        raise mc.urllib.error.URLError("no network")

    monkeypatch.setattr(mc.urllib.request, "urlopen", _boom)
    assert mc._fetch_anthropic_models() is None


def test_oauth_token_read_from_creds(monkeypatch, tmp_path):
    cred = tmp_path / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True)
    cred.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sk-tok"}}), encoding="utf-8"
    )
    monkeypatch.setattr(mc.Path, "home", lambda: tmp_path)
    assert mc._anthropic_oauth_token() == "sk-tok"


def test_oauth_token_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(mc.Path, "home", lambda: tmp_path)  # no .claude dir
    assert mc._anthropic_oauth_token() is None


def test_oauth_token_none_on_bad_json(monkeypatch, tmp_path):
    cred = tmp_path / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True)
    cred.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(mc.Path, "home", lambda: tmp_path)
    assert mc._anthropic_oauth_token() is None


def test_prefetch_warms_cache(monkeypatch):
    live = (ModelOption("claude-fable-5", "Claude Fable 5"),)
    monkeypatch.setattr(mc, "_fetch_anthropic_models", lambda: live)
    mc.prefetch().join(timeout=5)
    assert mc._cache.get("claude-code") is not None
    assert mc._cache["claude-code"][1] == live


def test_stale_cache_served_when_refetch_fails(monkeypatch):
    monkeypatch.setattr(
        mc, "_fetch_anthropic_models", lambda: (ModelOption("claude-x", "X"),)
    )
    provider_models("claude-code", fetch=True)  # warm
    ts, models = mc._cache["claude-code"]
    mc._cache["claude-code"] = (ts - mc._CACHE_TTL_S - 1, models)  # force stale
    monkeypatch.setattr(mc, "_fetch_anthropic_models", lambda: None)  # refetch fails
    ids = _ids(provider_models("claude-code", fetch=True))
    assert "claude-x" in ids  # stale cache served, not the static fallback
