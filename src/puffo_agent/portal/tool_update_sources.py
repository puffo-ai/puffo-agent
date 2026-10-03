"""Read-only local CLI inventory and authoritative release lookups.

No package manager or updater is executed. Unknown versions/channels are skipped;
unknown installation methods get documentation, never a guessed shell command.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from email.parser import Parser
from itertools import islice
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import aiohttp
from packaging.version import Version

from ..agent.cli_bin import (
    normalize_launch_argv, resolve_claude_bin, resolve_codex_bin,
    resolve_opencode_bin, resolve_pi_bin,
)
from ..agent.harness.support.child_env import build_child_environment
from ..agent.harness.support.cleanup_errors import collect_cleanup_errors, raise_collected_errors
from ..agent.harness.support.subprocess_io import process_group_spawn_kwargs, shutdown_process_tree
from ..crypto.http_session import create_remote_http_session
from ..tasks import spawn

log = logging.getLogger(__name__)
_LIMIT = 1024 * 1024
_VERSION = r"\d+\.\d+\.\d+(?:[-.]?(?:alpha|beta|rc|a|b|dev)[.-]?\d+)?(?:\+[A-Za-z0-9.-]+)?"


@dataclass(frozen=True)
class Tool:
    key: str
    name: str
    resolve: Callable[[], str | None]
    package: str
    docs: str


def _lingtai_bin() -> str | None:
    from .control.lingtai_discovery import _executable_paths

    paths = _executable_paths([])
    # The product installer owns this private venv; it need not be on PATH.
    suffix = "Scripts/lingtai-agent.exe" if os.name == "nt" else "bin/lingtai-agent"
    product = Path.home() / ".lingtai-tui/runtime/venv" / suffix
    if not paths and product.is_file():
        return str(product)
    return paths[0] if paths else None


TOOLS = (
    Tool("codex", "Codex", resolve_codex_bin, "@openai/codex", "https://developers.openai.com/codex/cli"),
    Tool("claude", "Claude Code", resolve_claude_bin, "@anthropic-ai/claude-code", "https://code.claude.com/docs/en/setup"),
    Tool("pi", "Pi", resolve_pi_bin, "@earendil-works/pi-coding-agent", "https://pi.dev"),
    Tool("opencode", "OpenCode", resolve_opencode_bin, "opencode-ai", "https://opencode.ai/docs/cli/#upgrade"),
    Tool("lingtai", "LingTai kernel", _lingtai_bin, "", "https://github.com/Lingtai-AI/lingtai#installation"),
)


@dataclass(frozen=True)
class Update:
    tool: str
    name: str
    current: str
    latest: str
    channel: str
    source: str
    instructions: str


def _read_file(path: Path) -> bytes:
    # Do not let a special file (or a huge local settings file) hang inventory.
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("not a regular metadata file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular metadata file")
        data = stream.read(_LIMIT + 1)
    if len(data) > _LIMIT:
        raise ValueError("metadata too large")
    return data


def _read_json(path: Path) -> dict:
    result = json.loads(_read_file(path))
    if not isinstance(result, dict):
        raise ValueError("metadata must be an object")
    return result


def _version(text: str) -> str:
    match = re.fullmatch(
        rf"(?:codex-cli |claude |pi |opencode |lingtai-agent |lingtai, version |lingtai-agent, version )?v?({_VERSION})(?: \(Claude Code\))?",
        text.strip(), re.IGNORECASE,
    )
    if not match:
        raise ValueError("unrecognized CLI version")
    value = match[1]
    Version(value)
    return value


async def _capture(executable: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *normalize_launch_argv(executable), "--version",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=8192,
        cwd=str(Path.home()), env=build_child_environment(), **process_group_spawn_kwargs(),
    )
    waiter = spawn(proc.wait(), name="tool_version.wait")
    errors: list[BaseException] = []
    result = b""
    try:
        async with asyncio.timeout(10):
            while chunk := await proc.stdout.read(8193 - len(result)):
                result += chunk
                if len(result) > 8192:
                    raise ValueError("CLI version output too large")
            if await asyncio.shield(waiter):
                raise ValueError("CLI version command failed")
    except BaseException as exc:
        errors.append(exc)
    await collect_cleanup_errors(
        shutdown_process_tree(proc, waiter=waiter, timeout=3 if os.name == "nt" else 1,
                              task_name="tool_version.shutdown"), errors, timeout=10,
    )
    raise_collected_errors("CLI version probe failed", errors)
    return _version(result.decode("utf-8"))


async def _fetch_bytes(url: str, *, redirects: bool = False) -> bytes:
    async with create_remote_http_session(url, timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(url, allow_redirects=redirects) as response:
            response.raise_for_status()
            if response.status != 200:
                raise ValueError("release source did not return 200")
            data = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                data.extend(chunk)
                if len(data) > _LIMIT:
                    raise ValueError("release metadata too large")
    return bytes(data)


async def _fetch_json(url: str) -> dict:
    result = json.loads(await _fetch_bytes(url))
    if not isinstance(result, dict):
        raise ValueError("release metadata must be an object")
    return result


def _npm_package(tool: Tool, executable: str) -> str:
    """Recognize the old Pi package too; never suggest an implicit migration."""
    allowed = {tool.package}
    if tool.key == "pi":
        allowed.add("@mariozechner/pi-coding-agent")
    path = Path(executable).resolve()
    for parent in list(path.parents)[:7]:
        candidate = parent / "package.json"
        if candidate.is_file():
            package = _read_json(candidate).get("name")
            if package in allowed:
                return package
    return tool.package


def _channel(tool: Tool, current: str) -> str:
    parsed = Version(current)
    if parsed.local or parsed.is_devrelease:
        raise ValueError("development build has no inferred update channel")
    if parsed.pre:
        return {"a": "alpha", "b": "beta", "rc": "rc"}[parsed.pre[0]]
    if tool.key == "claude":
        settings = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "settings.json"
        data = _read_json(settings) if settings.exists() else {}
        channel = data.get("autoUpdatesChannel", "latest")
        if channel not in {"stable", "latest"}:
            raise ValueError("unknown Claude update channel")
        return channel
    return "latest"


def _brew_source(executable: str) -> tuple[str, str] | None:
    parts = Path(executable).resolve().parts
    for folder, kind in (("Caskroom", "cask"), ("Cellar", "formula")):
        if folder in parts:
            pos = parts.index(folder) + 1
            if pos < len(parts):
                return kind, parts[pos]
    return None


async def _check(tool: Tool, executable: str, current: str) -> Update | None:
    channel = _channel(tool, current)
    instructions = f"Follow the update instructions for your installation: {tool.docs}"
    brew = _brew_source(executable)
    if brew:
        kind, name = brew
        allowed = {"codex": {"codex"}, "claude": {"claude-code", "claude-code@latest"},
                   "opencode": {"opencode"}}.get(tool.key, set())
        if name not in allowed or Version(current).is_prerelease:
            raise ValueError("unsupported Homebrew tool/channel")
        source = f"https://formulae.brew.sh/api/{kind}/{quote(name, safe='')}.json"
        metadata = await _fetch_json(source)
        latest = metadata["version"] if kind == "cask" else metadata["versions"]["stable"]
        channel = name
        instructions = f"brew upgrade {'--cask ' if kind == 'cask' else ''}{name}"
    elif tool.key == "lingtai":
        if Version(current).is_prerelease:
            raise ValueError("LingTai development channel is not a stable release")
        source = "https://api.github.com/repos/Lingtai-AI/lingtai-kernel/releases/latest"
        latest = await _lingtai_latest()
    else:
        if ".app/Contents/" in Path(executable).resolve().as_posix():
            raise ValueError("app-bundled CLI must follow its parent application's releases")
        package = await asyncio.to_thread(_npm_package, tool, executable)
        source = f"https://registry.npmjs.org/-/package/{quote(package, safe='')}/dist-tags"
        tags = await _fetch_json(source)
        if tool.key == "pi" and "legacy-node20" in tags:
            # npm does not retain the original dist-tag. Overlapping versions
            # cannot establish whether this installation follows the legacy line.
            if Version(current) <= Version(_version(tags["legacy-node20"])):
                raise ValueError("Pi legacy/latest channel is ambiguous")
        latest = tags.get(channel)
        if not isinstance(latest, str):
            raise ValueError("update channel unavailable")
    latest = _version(latest)
    candidate = Version(latest)
    if candidate.local or candidate.is_devrelease:
        raise ValueError("development release is not an update candidate")
    if candidate <= Version(current):
        return None
    if candidate.is_prerelease and (not Version(current).pre or candidate.pre[0] != Version(current).pre[0]):
        raise ValueError("release channel points at a different prerelease")
    return Update(tool.key, tool.name, current, latest, channel, source, instructions)


async def check_updates() -> list[Update]:
    updates = []
    for tool in TOOLS:
        try:
            executable = await asyncio.to_thread(tool.resolve)
            if not executable:
                continue
            current = (await asyncio.to_thread(_lingtai_version, executable)
                       if tool.key == "lingtai" else await _capture(executable))
            update = await _check(tool, executable, current)
            if update:
                updates.append(update)
        except Exception as exc:  # Isolate malformed providers and failed child cleanup.
            log.debug("%s update check skipped: %s", tool.key, type(exc).__name__)
    return updates


def _lingtai_version(executable: str) -> str:
    """LingTai has no --version flag; read only its own installed distribution."""
    root = Path(executable).resolve().parent.parent
    sites = [root / "Lib/site-packages", *islice(root.glob("lib/python*/site-packages"), 16)]
    found = []
    for site in sites:
        for entry in islice(site.glob("lingtai-*.dist-info"), 16):
            direct = entry / "direct_url.json"
            if direct.exists():
                data = _read_json(direct)
                if data.get("dir_info") or data.get("vcs_info"):
                    raise ValueError("LingTai source install has no release channel")
            path = entry / "METADATA"
            if not path.is_file() or path.stat().st_size > _LIMIT:
                continue
            metadata = Parser().parsestr(_read_file(path).decode("utf-8"))
            if metadata.get("Name") == "lingtai":
                found.append(_version(metadata["Version"]))
    if len(found) != 1:
        raise ValueError("no unambiguous installed LingTai kernel metadata")
    return found[0]


async def _lingtai_latest() -> str:
    """Follow the kernel's manifest mirrors, not the independently released TUI."""
    mirrors = (
        ("https://api.github.com/repos/Lingtai-AI/lingtai-kernel/releases/latest",
         "assets", ("browser_download_url", "browserDownloadUrl")),
        ("https://gitee.com/api/v5/repos/huangzesen1997/lingtai-kernel/releases/latest",
         "attach_files", ("browserDownloadUrl", "browser_download_url", "download_url", "url")),
    )
    manifests = []
    for url, field, aliases in mirrors:
        try:
            release = await _fetch_json(url)
            assets = [a for a in release[field] if a.get("name") == "lingtai-kernel-release-manifest.json"]
            if len(assets) != 1 or release.get("prerelease") or release.get("draft"):
                raise ValueError("ambiguous/nonstable kernel manifest")
            urls = {assets[0][key] for key in aliases if key in assets[0]}
            if len(urls) != 1:
                raise ValueError("ambiguous manifest URLs")
            asset_url = urls.pop()
            if not asset_url.startswith(("https://github.com/Lingtai-AI/lingtai-kernel/releases/download/",
                                         "https://gitee.com/huangzesen1997/lingtai-kernel/")):
                raise ValueError("unexpected kernel manifest host")
            raw = await _fetch_bytes(asset_url, redirects=True)
            manifests.append((raw, _validated_lingtai_manifest(raw)))
        except (OSError, ValueError, KeyError, TypeError, TimeoutError, aiohttp.ClientError):
            continue
    if not manifests:
        # The GitHub release page is public even when its anonymous API quota
        # is exhausted. Its redirect identifies the published stable release.
        raw, tag = await _lingtai_public_latest()
        version = _validated_lingtai_manifest(raw)
        if tag != "v" + version:
            raise ValueError("kernel release redirect and manifest disagree")
        manifests.append((raw, version))
    if not manifests or any(item != manifests[0] for item in manifests[1:]):
        raise ValueError("kernel manifest mirrors unavailable or disagree")
    return manifests[0][1]


def _validated_lingtai_manifest(raw: bytes) -> str:
    data = json.loads(raw)
    version = _version(data["kernel_version"])
    if (data["schema"] != "lingtai.kernel.release/v1"
            or data["kernel_tag"] != "v" + version
            or Version(version).is_prerelease or not data["artifacts"]):
        raise ValueError("invalid kernel manifest")
    for artifact in data["artifacts"]:
        if (not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
                or not artifact["filename"].startswith("lingtai-" + version)):
            raise ValueError("invalid kernel artifact metadata")
    return version


async def _lingtai_public_latest() -> tuple[bytes, str]:
    url = "https://github.com/Lingtai-AI/lingtai-kernel/releases/latest"
    async with create_remote_http_session(url, timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.head(url, allow_redirects=False) as response:
            if response.status not in (301, 302, 303, 307, 308):
                raise ValueError("kernel release page did not redirect")
            location = response.headers.get("Location", "")
    match = re.fullmatch(
        rf"https://github\.com/Lingtai-AI/lingtai-kernel/releases/tag/(v{_VERSION})",
        location,
    )
    if not match or Version(_version(match[1])).is_prerelease:
        raise ValueError("unexpected kernel release redirect")
    tag = match[1]
    asset = ("https://github.com/Lingtai-AI/lingtai-kernel/releases/download/"
             f"{tag}/lingtai-kernel-release-manifest.json")
    return await _fetch_bytes(asset, redirects=True), tag
