"""Real OperatorsView widget behaviour on the offscreen Qt platform:
display-name fallback, the Disconnect confirm gate, and unlink marshaling."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import types

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from puffo_agent.portal.ui.widgets import operators_view as ov


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _SyncThread:
    """threading.Thread stand-in: runs target() inline on start()."""

    def __init__(self, target=None, daemon=None, **_):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _pairing(slug="alice-1", name="MacBook", url="https://x.example"):
    return types.SimpleNamespace(operator_slug=slug, server_url=url, name=name)


def _view(monkeypatch):
    # __init__ calls poll() → load_pairings; keep it empty so no fetch threads.
    monkeypatch.setattr(ov, "load_pairings", lambda: {})
    return ov.OperatorsView()


def _labels(card):
    return [w.text() for w in card.findChildren(QLabel)]


def test_card_secondary_label_falls_back_to_slug(qapp, monkeypatch):
    v = _view(monkeypatch)
    card = v._make_card(_pairing(slug="alice-1", name="MacBook"))
    texts = _labels(card)
    assert "MacBook" in texts   # primary = machine name
    assert "alice-1" in texts   # secondary unresolved → slug


def test_card_secondary_label_shows_resolved_name(qapp, monkeypatch):
    v = _view(monkeypatch)
    v._names.resolved("alice-1", "Alice Doe")
    card = v._make_card(_pairing(slug="alice-1"))
    assert "Alice Doe" in _labels(card)


def test_card_has_disconnect_button(qapp, monkeypatch):
    v = _view(monkeypatch)
    card = v._make_card(_pairing())
    assert "Disconnect" in [b.text() for b in card.findChildren(QPushButton)]


def test_on_name_resolved_updates_label(qapp, monkeypatch):
    v = _view(monkeypatch)
    v._on_name_resolved("alice-1", "Alice")
    assert v._names.label("alice-1") == "Alice"


def test_disconnect_confirm_no_skips_unlink(qapp, monkeypatch):
    v = _view(monkeypatch)
    calls = []

    async def fake_unlink(slug, expected_server_url=None):
        calls.append(slug)
        return 0

    monkeypatch.setattr(ov.QMessageBox, "question", lambda *a, **k: ov.QMessageBox.No)
    monkeypatch.setattr(ov, "run_unlink", fake_unlink)
    monkeypatch.setattr(ov.threading, "Thread", _SyncThread)
    v._on_disconnect(_pairing(), QPushButton("Disconnect"))
    assert calls == []


def test_disconnect_confirm_yes_runs_unlink(qapp, monkeypatch):
    v = _view(monkeypatch)
    rec = {}

    async def fake_unlink(slug, expected_server_url=None):
        rec["slug"] = slug
        rec["url"] = expected_server_url
        return 0

    monkeypatch.setattr(ov.QMessageBox, "question", lambda *a, **k: ov.QMessageBox.Yes)
    monkeypatch.setattr(ov, "run_unlink", fake_unlink)
    monkeypatch.setattr(ov.threading, "Thread", _SyncThread)
    v._on_disconnect(_pairing(slug="bob-2", url="https://y"), QPushButton("Disconnect"))
    assert rec == {"slug": "bob-2", "url": "https://y"}


def test_on_unlink_done_failure_shows_warning(qapp, monkeypatch):
    v = _view(monkeypatch)
    warned = []
    monkeypatch.setattr(ov.QMessageBox, "warning", lambda *a, **k: warned.append(a))
    v._on_unlink_done("alice-1", False, "boom")
    assert warned


def test_resolve_names_fetches_and_caches(qapp, monkeypatch):
    v = _view(monkeypatch)

    async def fake_fetch(server_url, slug):
        return "Resolved Name"

    monkeypatch.setattr(ov, "fetch_operator_display_name", fake_fetch)
    monkeypatch.setattr(ov.threading, "Thread", _SyncThread)
    v._resolve_names([_pairing(slug="carol-3", url="https://z")])
    assert v._names.label("carol-3") == "Resolved Name"


def test_resolve_names_swallows_fetch_error(qapp, monkeypatch):
    v = _view(monkeypatch)

    async def boom(server_url, slug):
        raise RuntimeError("net down")

    monkeypatch.setattr(ov, "fetch_operator_display_name", boom)
    monkeypatch.setattr(ov.threading, "Thread", _SyncThread)
    v._resolve_names([_pairing(slug="dave-4")])
    assert v._names.label("dave-4") == "dave-4"   # error → cached "" → slug


def test_rebuild_renders_a_card_per_pairing(qapp, monkeypatch):
    v = _view(monkeypatch)
    v._rebuild([_pairing(slug="e-5", name="Zed"), _pairing(slug="f-6", name="Amy")])
    btns = [b for b in v._list_host.findChildren(QPushButton) if b.text() == "Disconnect"]
    assert len(btns) == 2


def test_disconnect_worker_error_surfaces_warning(qapp, monkeypatch):
    v = _view(monkeypatch)
    warned = []

    async def boom(slug, expected_server_url=None):
        raise RuntimeError("relay 500")

    monkeypatch.setattr(ov.QMessageBox, "question", lambda *a, **k: ov.QMessageBox.Yes)
    monkeypatch.setattr(ov.QMessageBox, "warning", lambda *a, **k: warned.append(a))
    monkeypatch.setattr(ov, "run_unlink", boom)
    monkeypatch.setattr(ov.threading, "Thread", _SyncThread)
    v._on_disconnect(_pairing(), QPushButton("Disconnect"))
    assert warned   # worker exception → _unlink_done(False) → warning


def test_poll_survives_corrupt_pairings(qapp, monkeypatch):
    def boom():
        raise ValueError("corrupt file")

    monkeypatch.setattr(ov, "load_pairings", boom)
    v = ov.OperatorsView()   # __init__ → poll() must swallow the error
    assert not [b for b in v._list_host.findChildren(QPushButton) if b.text() == "Disconnect"]
