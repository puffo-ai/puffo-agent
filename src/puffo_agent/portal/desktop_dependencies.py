"""Prepare desktop dependencies only for an explicitly requested GUI mode."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import shutil
import site
import subprocess
import sys
from pathlib import Path

from packaging.requirements import Requirement

from puffo_agent._proc import no_window_kwargs

_GUI_REQUIREMENT = "pyside6>=6.7"


def prepare_desktop() -> None:
    """Install missing Qt into this interpreter, then check its shared libraries.

    Headless entry points never call this. Never reinstall puffo-agent: doing so
    could change its version or source, particularly for a TestPyPI installation.
    Existing but broken Qt installations are reported without another download.
    """
    if _needs_desktop_install():
        _install_desktop()
        importlib.invalidate_caches()
    importlib.import_module("PySide6.QtWidgets")


def _needs_desktop_install() -> bool:
    if importlib.util.find_spec("PySide6") is None:
        return True
    try:
        version = importlib.metadata.version("PySide6")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ImportError(
            "PySide6 is present but its installation metadata is missing."
        ) from exc
    return version not in Requirement(_GUI_REQUIREMENT).specifier


def _install_desktop() -> None:
    # uv tool environments need not contain pip. An explicit interpreter also
    # works with custom UV_TOOL_DIR paths and avoids changing another install.
    if _is_user_install():
        if importlib.util.find_spec("pip") is None:
            raise ImportError(
                "Automatic desktop setup for a user installation requires pip in this interpreter."
            )
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--user",
            _GUI_REQUIREMENT,
        ]
    elif uv := shutil.which("uv"):
        command = [uv, "pip", "install", "--python", sys.executable, _GUI_REQUIREMENT]
    elif importlib.util.find_spec("pip") is not None:
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            _GUI_REQUIREMENT,
        ]
    else:
        raise ImportError(
            "Automatic desktop setup needs uv on PATH or pip in this Python environment."
        )

    print("Preparing desktop support for this Python environment…", file=sys.stderr)
    try:
        result = subprocess.run(command, check=False, timeout=600, **no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImportError(
            "Automatic desktop setup could not finish; retry the same command."
        ) from exc
    if result.returncode:
        raise ImportError(
            f"Automatic desktop setup failed (installer exit {result.returncode}). Check the installer error above "
            "and retry the same command after resolving it."
        )


def _is_user_install() -> bool:
    """A pip --user install must not try to write to the system interpreter."""
    if not site.ENABLE_USER_SITE:
        return False
    try:
        installed = importlib.metadata.distribution("puffo-agent").locate_file("")
    except importlib.metadata.PackageNotFoundError:
        return False  # source checkout, rather than an installed distribution
    return (
        Path(installed)
        .resolve()
        .is_relative_to(Path(site.getusersitepackages()).resolve())
    )


def desktop_error_message(exc: ImportError) -> str:
    return (
        f"Desktop support could not start: {exc}\n\n"
        "A missing system shared library (.so) is not fixed by reinstalling Python packages.\n"
        "On Debian/Ubuntu, the Qt runtime libraries can be installed with:\n"
        "    sudo apt-get install libgl1 libegl1 libxkbcommon0 libdbus-1-3 libfontconfig1\n"
        "Alpine/musl has no compatible PySide6 wheel.\n"
        "The requested desktop mode has not been replaced with a headless daemon."
    )
