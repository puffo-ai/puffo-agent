"""Cloud-slim packaging guard: the keyless cloud-agent boot chain must
import and run with the chat-local LLM SDKs (anthropic / openai) absent —
and building a chat-local provider without its extra must fail with the
actionable "rebuild the template" hint, not a raw ImportError.

anthropic / openai moved out of ``[project].dependencies`` into the
``anthropic`` / ``openai`` extras (see pyproject), so the base install
(the cli-local cloud template) carries neither. Pillow deliberately STAYS
in the base install — the inbound-image dimension guard runs on the shared
delivery path for every runtime kind, including cli-local — so these tests
still block PIL via ``sys.modules`` to prove the guard degrades gracefully
if a template is ever built without it. worker.py lazy-imports the
providers only when it builds a chat-local legacy provider, and
puffo_core_client lazy-imports Pillow only on the attachment path — so
importing the daemon touches none of the three.

Deterministic in any environment: we block the three SDKs via
``sys.modules[...] = None`` (import raises ImportError even when the
package happens to be installed) and drop every cached ``puffo_agent``
module, so the whole boot chain re-executes under the block. That
asserts the real slim contract rather than "the test box didn't have
the SDK".
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


# The three cloud-slim SDKs and the submodules the code reaches for.
# Mapping a name to ``None`` in sys.modules makes ``import <name>`` raise
# ImportError even when the package is installed.
_BLOCKED_NAMES = [
    "anthropic",
    "openai",
    "PIL",
    "PIL.Image",
]


@pytest.fixture
def sdks_blocked(monkeypatch):
    """Block anthropic/openai/PIL and drop every cached ``puffo_agent``
    module so the boot chain re-imports from scratch under the block."""
    for name in _BLOCKED_NAMES:
        monkeypatch.setitem(sys.modules, name, None)
    # Drop cached puffo_agent modules AND any already-cached copy of the
    # blocked SDKs, so re-imports actually re-run under the block instead
    # of returning a warm module object.
    for name in list(sys.modules):
        if name == "puffo_agent" or name.startswith("puffo_agent."):
            monkeypatch.delitem(sys.modules, name, raising=False)
        elif name in ("anthropic", "openai", "PIL") or name.startswith(
            ("anthropic.", "openai.", "PIL.")
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)
    # Re-apply the block after the delete sweep above cleared it.
    for name in _BLOCKED_NAMES:
        monkeypatch.setitem(sys.modules, name, None)
    yield


def test_sdks_are_actually_blocked(sdks_blocked):
    """Sanity: the fixture makes the three imports raise, so the
    assertions below mean what they say."""
    for name in ("anthropic", "openai"):
        with pytest.raises(ImportError):
            importlib.import_module(name)
    with pytest.raises(ImportError):
        from PIL import Image  # noqa: F401


def test_daemon_boot_chain_imports_without_llm_sdks(sdks_blocked):
    """The keyless cloud-agent boot path (`puffo-agent start` ->
    run_daemon) must import cleanly with anthropic/openai/pillow absent,
    and must NOT pull any of them into sys.modules."""
    daemon = importlib.import_module("puffo_agent.portal.daemon")
    # run_daemon is the headless entry point cmd_start dispatches to.
    assert hasattr(daemon, "run_daemon")
    # Importing the daemon (and everything it eagerly loads) touched
    # none of the three SDKs — each is still the None block sentinel,
    # not a real module. If the boot chain had imported one, the import
    # above would have raised ImportError instead.
    assert sys.modules.get("anthropic") is None
    assert sys.modules.get("openai") is None
    assert sys.modules.get("PIL") is None


def _stub_cfgs(provider: str):
    """Minimal duck-typed daemon_cfg / runtime for
    ``_build_legacy_provider`` — the SDK import fires before any api_key
    field is read, so we only need provider + llm_base_url."""
    runtime = types.SimpleNamespace(provider=provider, llm_base_url=None)
    daemon_cfg = types.SimpleNamespace(default_provider=provider)
    return daemon_cfg, runtime


def test_chat_local_anthropic_without_extra_raises_actionable_hint(sdks_blocked):
    worker = importlib.import_module("puffo_agent.portal.worker")
    daemon_cfg, runtime = _stub_cfgs("anthropic")
    with pytest.raises(RuntimeError) as excinfo:
        worker._build_legacy_provider(daemon_cfg, runtime)
    msg = str(excinfo.value)
    assert "puffo-agent[anthropic]" in msg
    assert "Anthropic SDK" in msg


def test_chat_local_openai_without_extra_raises_actionable_hint(sdks_blocked):
    worker = importlib.import_module("puffo_agent.portal.worker")
    daemon_cfg, runtime = _stub_cfgs("openai")
    with pytest.raises(RuntimeError) as excinfo:
        worker._build_legacy_provider(daemon_cfg, runtime)
    msg = str(excinfo.value)
    assert "puffo-agent[openai]" in msg
    assert "OpenAI SDK" in msg


def test_attachment_guard_without_pillow_hints_extra_and_no_ops(sdks_blocked):
    """Missing Pillow must not crash the delivery path — the dimension
    guard no-ops (returns False) and logs the actionable extras hint."""
    pcc = importlib.import_module("puffo_agent.agent.puffo_core_client")
    with pytest.raises(ImportError):
        from PIL import Image  # noqa: F401  (Pillow really is blocked)
    # Best-effort guard: no exception, just a False no-op.
    assert pcc._downscale_oversized_image("/nonexistent/whatever.png") is False


def test_attachment_guard_logs_attachments_extra(sdks_blocked, caplog):
    import logging

    pcc = importlib.import_module("puffo_agent.agent.puffo_core_client")
    with caplog.at_level(logging.WARNING):
        pcc._downscale_oversized_image("/nonexistent/whatever.png")
    assert "puffo-agent[attachments]" in caplog.text


# --- positive paths + guard-precision (do NOT use sdks_blocked) ----------


def test_daemon_eagerly_imports_worker():
    """The boot-chain SDK-free guard above relies on importing the daemon
    transitively walking worker.py (daemon.py has a top-level
    ``from .worker import Worker``). Pin that eagerness so a future
    lazy-worker refactor can't silently narrow
    ``test_daemon_boot_chain_imports_without_llm_sdks`` to a path that no
    longer covers worker's provider imports."""
    importlib.import_module("puffo_agent.portal.daemon")
    assert "puffo_agent.portal.worker" in sys.modules


def test_build_legacy_provider_anthropic_happy_path():
    """Positive path: with the anthropic SDK present, _build_legacy_provider
    actually constructs an AnthropicProvider — so a regression that broke
    the happy path (e.g. the try/except shadowing the real provider, or a
    wrong constructor call) is caught here instead of only at agent spawn."""
    pytest.importorskip("anthropic")
    worker = importlib.import_module("puffo_agent.portal.worker")
    from puffo_agent.agent.providers.anthropic_provider import AnthropicProvider

    runtime = types.SimpleNamespace(
        provider="anthropic", llm_base_url=None, api_key="sk-test", model=None
    )
    daemon_cfg = types.SimpleNamespace(
        default_provider="anthropic",
        anthropic=types.SimpleNamespace(api_key="", model=""),
    )
    provider = worker._build_legacy_provider(daemon_cfg, runtime)
    assert isinstance(provider, AnthropicProvider)


def test_provider_import_failure_is_not_misattributed_to_missing_extra():
    """Guard precision: an ImportError raised for a reason OTHER than the
    anthropic package being absent (e.g. a broken transitive dep, a partial
    wheel) must propagate as an ImportError — NOT be re-attributed to the
    'rebuild with puffo-agent[anthropic]' hint, which would misdirect the
    operator to reinstall an already-present package.

    We simulate this by planting a stub ``anthropic_provider`` module that
    lacks ``AnthropicProvider``: the ``from ... import AnthropicProvider``
    then raises an ImportError whose ``.name`` is the provider module, not
    ``anthropic`` — exactly the shape of a non-missing-SDK failure."""
    worker = importlib.import_module("puffo_agent.portal.worker")
    modname = "puffo_agent.agent.providers.anthropic_provider"
    stub = types.ModuleType(modname)  # deliberately no AnthropicProvider attr
    runtime = types.SimpleNamespace(provider="anthropic", llm_base_url=None)
    daemon_cfg = types.SimpleNamespace(default_provider="anthropic")
    saved = sys.modules.get(modname)
    sys.modules[modname] = stub
    try:
        with pytest.raises(ImportError) as excinfo:
            worker._build_legacy_provider(daemon_cfg, runtime)
        # The actionable-extra RuntimeError must NOT have swallowed it.
        assert not isinstance(excinfo.value, RuntimeError)
        assert "puffo-agent[anthropic]" not in str(excinfo.value)
    finally:
        if saved is not None:
            sys.modules[modname] = saved
        else:
            sys.modules.pop(modname, None)
