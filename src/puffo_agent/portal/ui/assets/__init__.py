"""Bundled UI assets (logo etc.)."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def logo_path() -> Path:
    return Path(str(files("puffo_agent.portal.ui.assets").joinpath("puffo-logo.png")))
