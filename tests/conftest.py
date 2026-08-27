"""Make pytest import the in-tree source instead of any installed
``puffo-agent``. Lets the test suite run against source without
requiring ``pip install -e .``.
"""

import sys
from pathlib import Path

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
