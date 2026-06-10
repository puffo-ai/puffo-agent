"""Subprocess spawn helpers."""

from __future__ import annotations

import os
import subprocess


def no_window_kwargs() -> dict:
    """Windows: spawn child console apps (claude/codex/docker) without a
    console window so a DETACHED ``start --background`` daemon — which has
    no console of its own — doesn't pop a window per subprocess. No-op on
    other platforms; stdio is piped either way."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
