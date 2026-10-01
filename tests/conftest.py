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
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _tempdirs_under_pytest_basetemp(tmp_path_factory):
    base = str(tmp_path_factory.getbasetemp())
    saved = (tempfile.tempdir, os.environ.get("TMP"), os.environ.get("TEMP"), os.environ.get("TMPDIR"))
    tempfile.tempdir = base
    os.environ["TMP"] = os.environ["TEMP"] = os.environ["TMPDIR"] = base
    yield
    tempfile.tempdir = saved[0]
    for key, val in zip(("TMP", "TEMP", "TMPDIR"), saved[1:]):
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
