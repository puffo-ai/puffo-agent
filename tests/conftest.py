"""Make pytest import the in-tree source instead of any installed
``puffo-agent``. Lets the test suite run against source without
requiring ``pip install -e .``.
"""

import sys
from pathlib import Path

import pytest
import pytest_asyncio

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Allow test files to import sibling helpers like ``_portal_support``.
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))


@pytest_asyncio.fixture(autouse=True)
async def _close_message_stores(monkeypatch):
    """Return directly constructed stores before pytest closes their loop."""
    from puffo_agent.agent.message_store import MessageStore

    stores: list[MessageStore] = []
    original_init = MessageStore.__init__

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        stores.append(self)

    monkeypatch.setattr(MessageStore, "__init__", tracked_init)
    yield
    for store in reversed(stores):
        await store.close()


@pytest.fixture(autouse=True)
def _no_real_autostart(monkeypatch):
    """Keep tests from registering a real login item.

    A successful ``machine link`` enables autostart, and
    ``_enable_autostart_after_link`` turns any error into a warning. So a
    link test that forgets to stub it writes the developer's real
    ``~/Library/LaunchAgents/ai.puffo.agent.plist`` (pointing at the test's
    temporary home) and bootstraps a daemon from it, and still passes.
    The platform functions under test (``enable_macos`` etc.) take a fake
    runner and are not affected; only the public dispatchers are blocked.
    """
    from puffo_agent.portal import autostart

    reached: list[str] = []

    def _blocked(name):
        def call(*_args, **_kwargs):
            reached.append(name)
            raise RuntimeError(f"autostart.{name}() is blocked in tests")

        return call

    for name in ("enable", "disable", "status"):
        monkeypatch.setattr(autostart, name, _blocked(name))
    yield
    assert not reached, (
        f"test reached the real autostart.{reached[0]}(); stub it or pass "
        "no_autostart=True"
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Give every test its own HOME and Puffo home.

    Without this the suite wrote into whatever home it ran under: agent
    folders under ``~/.puffo-agent/agents`` (``a-1`` was then loaded by a
    real daemon and failed on every start), ``runtime.json`` rewrites into
    any existing folder with a fixture id, and the opencode/pi state the
    spawned binaries create. Host overrides that point harness config at a
    real location are dropped too; tests that need them set their own.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(home / ".puffo-agent"))
    for name in (
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    return home
