"""OpenCode native model-access readiness checks.

OpenCode has no standalone ``auth check`` command.  Its native ``models``
command is nevertheless the right authority: it lists public models plus the
providers unlocked by the current credential store, and a provider-filtered
query fails when that provider is not configured.  Reuse that output for both
the picker and create-time preflight so discovery cannot claim more than the
runtime can actually launch.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .._proc import no_window_kwargs

from .cli_bin import normalize_launch_argv
from .harness.support.child_env import build_child_environment


class OpenCodeProbeError(RuntimeError):
    """OpenCode could not produce a trustworthy model-access verdict."""


OpenCodeModelStatus = Literal[
    "ready", "need_login", "model_not_available",
]


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True, slots=True)
class OpenCodeModel:
    """One model and the native variants advertised by OpenCode."""

    id: str
    variants: tuple[str, ...] = ()


def _run_opencode_models(
    executable: str,
    *,
    provider: str,
    verbose: bool,
    timeout_seconds: float,
) -> str:
    command = [*normalize_launch_argv(executable), "models"]
    if provider:
        command.append(provider)
    if verbose:
        command.append("--verbose")
    try:
        # OpenCode extracts native libraries on every process start. Own only
        # this invocation's temp files; subprocess.run kills/waits on timeout
        # before TemporaryDirectory removes them.
        with tempfile.TemporaryDirectory(prefix="puffo-opencode-probe-") as scratch:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env={
                    **build_child_environment(),
                    **{name: scratch for name in ("TMPDIR", "TMP", "TEMP")},
                },
                timeout=timeout_seconds,
                **no_window_kwargs(),
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpenCodeProbeError("OpenCode model check could not complete") from exc

    stdout = _ANSI_ESCAPE.sub("", completed.stdout)
    diagnostic = stdout + _ANSI_ESCAPE.sub("", completed.stderr)
    if completed.returncode != 0:
        if provider and "Provider not found:" in diagnostic:
            return ""
        raise OpenCodeProbeError("OpenCode model check failed")
    return stdout


def _is_model_id(value: str) -> bool:
    return (
        "/" in value
        and not any(char.isspace() or char in '{}[],"' for char in value)
    )


def _parse_verbose_models(output: str) -> tuple[OpenCodeModel, ...]:
    """Parse ``models --verbose``'s repeated ``id`` + JSON blocks.

    The model-id line is the launch authority. JSON is used only for optional
    variant metadata, so a malformed/missing block degrades to no variants
    without inventing support or dropping a runnable model.
    """
    decoder = json.JSONDecoder()
    models: list[OpenCodeModel] = []
    seen: set[str] = set()
    offset = 0
    length = len(output)
    while offset < length:
        line_end = output.find("\n", offset)
        if line_end < 0:
            line_end = length
        raw_line = output[offset:line_end]
        model_id = raw_line.strip()
        offset = line_end + 1
        if raw_line != model_id or not _is_model_id(model_id) or model_id in seen:
            continue

        metadata: object = {}
        json_offset = offset
        while json_offset < length and output[json_offset].isspace():
            json_offset += 1
        if json_offset < length and output[json_offset] == "{":
            try:
                metadata, offset = decoder.raw_decode(output, json_offset)
            except json.JSONDecodeError:
                metadata = {}
                # Continue line by line from the malformed block. Bare model-id
                # lines remain authoritative even when they have no JSON block;
                # punctuation and indentation keep JSON fragments from becoming
                # phantom models.

        variants = metadata.get("variants") if isinstance(metadata, dict) else {}
        variant_names = tuple(
            key for key in variants
            if isinstance(key, str) and key
        ) if isinstance(variants, dict) else ()
        seen.add(model_id)
        models.append(OpenCodeModel(model_id, variant_names))
    return tuple(models)


def list_opencode_models(
    executable: str,
    *,
    provider: str = "",
    timeout_seconds: float = 5.0,
) -> tuple[str, ...]:
    """Return model IDs visible to OpenCode's current native credential view."""
    stdout = _run_opencode_models(
        executable,
        provider=provider,
        verbose=False,
        timeout_seconds=timeout_seconds,
    )
    models: list[str] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        model = line.strip()
        if not _is_model_id(model):
            continue
        if model not in seen:
            seen.add(model)
            models.append(model)
    return tuple(models)


def list_opencode_model_catalog(
    executable: str,
    *,
    provider: str = "",
    timeout_seconds: float = 5.0,
) -> tuple[OpenCodeModel, ...]:
    """Return visible models with their model-specific native variants."""
    try:
        stdout = _run_opencode_models(
            executable,
            provider=provider,
            verbose=True,
            timeout_seconds=timeout_seconds,
        )
    except OpenCodeProbeError:
        return tuple(
            OpenCodeModel(model_id)
            for model_id in list_opencode_models(
                executable,
                provider=provider,
                timeout_seconds=timeout_seconds,
            )
        )
    return _parse_verbose_models(stdout)


def opencode_model_status(executable: str, model: str) -> OpenCodeModelStatus:
    """Classify provider access separately from an unavailable model ID."""
    selected = model.strip()
    provider = selected.split("/", 1)[0] if "/" in selected else ""
    models = list_opencode_models(executable, provider=provider)
    if not models:
        return "need_login"
    if not selected:
        return "ready"
    if "/" in selected:
        available = selected in models
    else:
        available = any(
            candidate.rsplit("/", 1)[-1] == selected for candidate in models
        )
    return "ready" if available else "model_not_available"


def opencode_model_is_available(executable: str, model: str) -> bool:
    """Compatibility bool view of :func:`opencode_model_status`."""
    return opencode_model_status(executable, model) == "ready"


@dataclass(frozen=True)
class _Discovery:
    expires: float
    models: tuple[OpenCodeModel, ...] = ()
    error: str = ""
    files: tuple = ()


_discovery_lock = threading.Lock()
_discovery_cache: dict[tuple, _Discovery] = {}


def _discovery_files(environment: dict[str, str], executable: str) -> tuple:
    """Notice native login/logout and ordinary config edits without reading secrets."""
    home = Path(environment.get("HOME") or environment.get("USERPROFILE") or Path.home())
    data = Path(environment.get("XDG_DATA_HOME") or home / ".local/share")
    config = Path(environment.get("XDG_CONFIG_HOME") or home / ".config")
    global_config = config / "opencode"
    paths = [Path(executable), data / "opencode/auth.json",
             global_config / "config.json", global_config / "config"]
    roots = [global_config, Path(environment.get("OPENCODE_TEST_HOME") or home) / ".opencode"]
    for root in (*Path.cwd().parents, Path.cwd()):
        roots.extend((root, root / ".opencode"))
    # Native v1.3.17 config/config.ts and config/paths.ts. Only forwarded
    # child variables participate; parent-only overrides have no effect.
    if environment.get("OPENCODE_CONFIG"):
        paths.append(Path(environment["OPENCODE_CONFIG"]))
    if environment.get("OPENCODE_CONFIG_DIR"):
        roots.append(Path(environment["OPENCODE_CONFIG_DIR"]))
    if sys.platform == "darwin":
        import pwd

        managed = Path("/Library/Application Support/opencode")
        preferences = Path("/Library/Managed Preferences")
        username = pwd.getpwuid(os.getuid()).pw_name
        paths.extend(root / "ai.opencode.managed.plist"
                     for root in (preferences, preferences / username))
    elif sys.platform == "win32":
        managed = Path(environment.get("ProgramData") or "C:\\ProgramData") / "opencode"
    else:
        managed = Path("/etc/opencode")
    roots.append(Path(environment.get("OPENCODE_TEST_MANAGED_CONFIG_DIR") or managed))
    for root in roots:
        paths.extend(root / name for name in ("opencode.json", "opencode.jsonc"))
    stamps = []
    for path in paths:
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        except OSError:
            stamp = None
        stamps.append((str(path), stamp))
    return tuple(stamps)


def discover_opencode_models(executable: str) -> tuple[OpenCodeModel, ...]:
    """Bound expensive advisory discovery; admission probes remain uncached.

    Readiness and the picker share a result for five minutes. Failed discovery
    retries after 30 seconds. Environment/cwd changes select a separate view;
    native auth/config edits invalidate the view. Admission always probes live.
    """
    environment = build_child_environment()
    key = (executable, os.getcwd(), tuple(sorted(environment.items())))
    # ponytail: serialize these short probes, including cache misses, so a
    # heartbeat and UI refresh cannot launch duplicate CLI processes.
    with _discovery_lock:
        files = _discovery_files(environment, executable)
        now = time.monotonic()
        cached = _discovery_cache.get(key)
        if cached is None or cached.expires <= now or cached.files != files:
            try:
                models = list_opencode_model_catalog(executable)
                cached = _Discovery(time.monotonic() + 300, models, files=files)
            except OpenCodeProbeError as exc:
                cached = _Discovery(time.monotonic() + 30, error=str(exc), files=files)
            # Bound retained environment views in a long-running daemon.
            if key not in _discovery_cache and len(_discovery_cache) >= 16:
                del _discovery_cache[next(iter(_discovery_cache))]
            _discovery_cache[key] = cached
        if cached.error:
            raise OpenCodeProbeError(cached.error)
        return cached.models
