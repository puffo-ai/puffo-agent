"""PUF-393: OperatorNameCache scheduling/fallback logic + source-pins for the
Qt Operators-tab wiring (PySide6 isn't importable in this env, so the widget is
pinned by reading its source rather than instantiating it)."""
from __future__ import annotations

from pathlib import Path

import puffo_agent
from puffo_agent.portal.control.operator_names import OperatorNameCache


def test_label_falls_back_to_slug_when_unresolved():
    c = OperatorNameCache()
    assert c.label("alice-1") == "alice-1"


def test_label_uses_resolved_name():
    c = OperatorNameCache(clock=lambda: 0.0)
    c.resolved("alice-1", "Alice")
    assert c.label("alice-1") == "Alice"


def test_label_empty_resolution_falls_back_to_slug():
    c = OperatorNameCache(clock=lambda: 0.0)
    c.resolved("alice-1", "")
    assert c.label("alice-1") == "alice-1"


def test_slugs_to_fetch_returns_unresolved():
    c = OperatorNameCache(clock=lambda: 0.0)
    assert c.slugs_to_fetch(["a", "b"]) == ["a", "b"]


def test_slugs_to_fetch_skips_pending():
    c = OperatorNameCache(clock=lambda: 0.0)
    c.mark_pending("a")
    assert c.slugs_to_fetch(["a", "b"]) == ["b"]


def test_slugs_to_fetch_skips_fresh_resolved():
    now = [100.0]
    c = OperatorNameCache(refresh_after=300.0, clock=lambda: now[0])
    c.resolved("a", "Alice")
    now[0] = 200.0  # 100s later — within refresh window
    assert c.slugs_to_fetch(["a"]) == []


def test_slugs_to_fetch_refetches_when_stale():
    now = [100.0]
    c = OperatorNameCache(refresh_after=300.0, clock=lambda: now[0])
    c.resolved("a", "Alice")
    now[0] = 500.0  # 400s later — past refresh window (picks up renames)
    assert c.slugs_to_fetch(["a"]) == ["a"]


def test_concurrent_polls_dont_double_fetch_same_slug():
    # Two poll cycles can fire before the first fetch returns; once a slug is
    # marked pending, the next poll must not schedule a duplicate fetch.
    c = OperatorNameCache(clock=lambda: 0.0)
    first = c.slugs_to_fetch(["a"])
    assert first == ["a"]
    for s in first:
        c.mark_pending(s)
    assert c.slugs_to_fetch(["a"]) == []


def test_empty_result_is_cached_no_refetch_storm():
    # A failed fetch ("") must be cached so the 500ms poll doesn't re-fire it
    # every cycle; it still re-tries after the refresh window (server ships late).
    now = [0.0]
    c = OperatorNameCache(refresh_after=300.0, clock=lambda: now[0])
    c.resolved("a", "")
    now[0] = 10.0
    assert c.slugs_to_fetch(["a"]) == []


# ── Source-pins: Operators-tab Qt wiring (can't import PySide6 here) ──────────


def _operators_view_src() -> str:
    root = Path(puffo_agent.__file__).parent
    return (root / "portal" / "ui" / "widgets" / "operators_view.py").read_text(
        encoding="utf-8"
    )


def test_disconnect_button_wired_to_run_unlink_with_confirm():
    src = _operators_view_src()
    assert '"Disconnect"' in src
    assert "run_unlink" in src
    assert "expected_server_url=server_url" in src
    assert "QMessageBox.question" in src  # destructive → confirm first


def test_card_renders_resolved_display_name_with_fallback():
    src = _operators_view_src()
    assert "fetch_operator_display_name" in src
    assert "self._names.label(p.operator_slug)" in src
