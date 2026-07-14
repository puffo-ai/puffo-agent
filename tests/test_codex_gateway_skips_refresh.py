"""Gateway/VK agents must NOT go through the OAuth credential-refresh path.

Regression cover for the ~40s/turn detour: an agent authenticating to the
LiteLLM gateway with a static per-agent virtual key has no OAuth token, so
the daemon must neither (a) register it with the periodic refresher nor
(b) hand the worker a pre-delivery ``ensure_fresh`` gate. Before the fix,
both fired and probed ``api.openai.com`` directly, which 401s under VK auth
— deferring the first turn by ~40s and flipping ``refresh_broken``.

Native-auth (ChatGPT OAuth) codex agents must keep the refresh behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

from puffo_agent.portal.daemon import Daemon


class _StubRefresher:
    def __init__(self) -> None:
        self.registered: list = []
        self.success_callbacks: list = []

    def register_agent(self, home) -> None:
        self.registered.append(home)

    def register_on_refresh_success(self, cb) -> None:
        self.success_callbacks.append(cb)

    def ensure_fresh(self, *_a, **_k) -> bool:  # pragma: no cover - identity only
        return True


def _daemon() -> Daemon:
    """A Daemon with just the refresher wiring the guards touch."""
    d = Daemon.__new__(Daemon)
    d.codex_refresher = _StubRefresher()
    d.refresher = _StubRefresher()
    return d


def _cfg(*, llm_base_url: str = "", harness: str = "codex") -> SimpleNamespace:
    return SimpleNamespace(
        id="agent-1",
        runtime=SimpleNamespace(harness=harness, llm_base_url=llm_base_url),
    )


def _worker() -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(health="ok"),
        _auth_failed_notification_sent=False,
        _refresh_success_callback=None,
    )


# ── _ensure_fresh_for: the worker's pre-delivery gate ──

def test_ensure_fresh_is_none_in_gateway_mode():
    d = _daemon()
    cfg = _cfg(llm_base_url="https://litellm-staging.puffo.ai")
    # None => worker skips the gate entirely; no api.openai.com probe.
    assert d._ensure_fresh_for(cfg) is None


def test_ensure_fresh_is_wired_for_oauth_codex():
    d = _daemon()
    cfg = _cfg(llm_base_url="")
    assert d._ensure_fresh_for(cfg) == d.codex_refresher.ensure_fresh


def test_ensure_fresh_ignores_blank_llm_base_url():
    """A whitespace-only base_url is not gateway mode — don't strand OAuth."""
    d = _daemon()
    assert d._ensure_fresh_for(_cfg(llm_base_url="   ")) is not None


def test_runtime_without_llm_base_url_field_is_oauth_mode():
    """A runtime object lacking the field entirely must not raise.

    Worker startup runs through these guards; a missing optional attribute
    must degrade to native-auth mode, never crash the daemon.
    """
    d = _daemon()
    cfg = SimpleNamespace(id="agent-1", runtime=SimpleNamespace(harness="codex"))
    assert d._ensure_fresh_for(cfg) == d.codex_refresher.ensure_fresh
    d._register_with_refresher(cfg, _worker())
    assert len(d.codex_refresher.registered) == 1


# ── _register_with_refresher: the periodic background refresh loop ──

def test_gateway_agent_is_not_registered_with_refresher():
    d = _daemon()
    cfg = _cfg(llm_base_url="https://litellm-staging.puffo.ai")
    d._register_with_refresher(cfg, _worker())
    assert d.codex_refresher.registered == []
    assert d.refresher.registered == []


def test_oauth_codex_agent_is_registered_with_refresher():
    d = _daemon()
    d._register_with_refresher(_cfg(llm_base_url=""), _worker())
    assert len(d.codex_refresher.registered) == 1
    assert d.refresher.registered == []


def test_non_codex_agent_still_uses_claude_refresher():
    d = _daemon()
    d._register_with_refresher(
        _cfg(llm_base_url="", harness="claude-code"), _worker(),
    )
    assert len(d.refresher.registered) == 1
    assert d.codex_refresher.registered == []
