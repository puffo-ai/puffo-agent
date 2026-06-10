"""``no_window_kwargs`` — windowless child spawns so a detached
``start --background`` daemon doesn't pop a console per claude/codex."""

from __future__ import annotations

import subprocess

import pytest

from puffo_agent import _proc


@pytest.mark.skipif(
    not hasattr(subprocess, "CREATE_NO_WINDOW"),
    reason="CREATE_NO_WINDOW is Windows-only",
)
def test_no_window_kwargs_on_windows(monkeypatch):
    monkeypatch.setattr(_proc.os, "name", "nt")
    assert _proc.no_window_kwargs() == {
        "creationflags": subprocess.CREATE_NO_WINDOW
    }


def test_no_window_kwargs_off_windows(monkeypatch):
    monkeypatch.setattr(_proc.os, "name", "posix")
    assert _proc.no_window_kwargs() == {}
