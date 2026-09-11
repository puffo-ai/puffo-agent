"""Shared Docker image and subprocess support for Driver runtimes."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from ....tasks import spawn


logger = logging.getLogger(__name__)


def puffo_agent_pkg_dir() -> Path:
    """Host package root mounted read-only for the in-container MCP server."""
    import puffo_agent

    return Path(puffo_agent.__file__).resolve().parent.parent


# Bump both values when the bundled image or mount layout changes. The image
# tag makes first use build new contents; the layout marker recreates existing
# per-Agent containers that still point at an older image or mount set.
DEFAULT_IMAGE = "puffo/agent-runtime:v20"
CONTAINER_LAYOUT_VERSION = "23"

CLAUDE_CODE_NPM_VERSION = "2.1.224"
CODEX_NPM_VERSION = "0.147.0"

DOCKER_COMMAND_TIMEOUT_SECONDS = 60.0
DOCKER_BUILD_TIMEOUT_SECONDS = 900.0
PROBE_FALSE_EXIT = 42


DOCKERFILE = """\
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git curl ca-certificates jq ripgrep \\
        python3 python3-pip \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@__CLAUDE_CODE_VERSION__

RUN npm install -g @openai/codex@__CODEX_CODE_VERSION__

# Puffo MCP tools server deps. ``--break-system-packages`` is required on
# Debian bookworm (PEP 668); acceptable for this single-purpose image.
RUN pip3 install --break-system-packages --no-cache-dir \\
        "aiohttp>=3.9" "aiohttp-socks>=0.10" "aiosqlite>=0.20" \\
        "certifi>=2024.2.2" "cryptography>=43" "mcp>=1.0,<2" \\
        "pillow>=10.0" "pyhpke>=0.6" "psutil>=5.9" \\
        "python-socks>=2.4" "pyyaml>=6.0" "tzdata>=2024.1" \\
        "websockets>=12.0" "uv>=0.5"

RUN useradd -m -u 2000 -s /bin/bash agent
USER agent
WORKDIR /workspace

# Docker Desktop does not propagate host bind-mount inotify reliably. Poll the
# audit file so ``docker logs`` still exposes host-written runtime activity.
CMD ["sh", "-c", "set -eu; mkdir -p /workspace/.puffo-agent; touch /workspace/.puffo-agent/audit.log; echo \\"[$(date -u +%FT%TZ)] puffo agent=${PUFFO_AGENT_ID:-unknown} container starting; polling /workspace/.puffo-agent/audit.log every 1s\\"; last=$(stat -c%s /workspace/.puffo-agent/audit.log 2>/dev/null || echo 0); while :; do size=$(stat -c%s /workspace/.puffo-agent/audit.log 2>/dev/null || echo 0); if [ \\"$size\\" -gt \\"$last\\" ]; then tail -c +$((last + 1)) /workspace/.puffo-agent/audit.log; last=$size; elif [ \\"$size\\" -lt \\"$last\\" ]; then last=0; fi; sleep 1; done"]
""".replace(
    "__CLAUDE_CODE_VERSION__",
    CLAUDE_CODE_NPM_VERSION,
).replace(
    "__CODEX_CODE_VERSION__",
    CODEX_NPM_VERSION,
)


_BUILD_LOCK = asyncio.Lock()


async def container_state(docker_bin: str, container_name: str) -> str | None:
    """Return Docker state, ``""`` when absent, or ``None`` on probe failure."""
    rc, out, _ = await run_cmd(
        [
            docker_bin,
            "container",
            "ls",
            "--all",
            "--filter",
            f"name=^/{container_name}$",
            "--format",
            "{{.State}}",
        ],
        check=False,
    )
    if rc != 0:
        return None
    return out.decode("utf-8", errors="replace").strip()


async def ensure_docker_image(
    docker_bin: str,
    image: str,
    *,
    agent_id: str = "",
) -> None:
    """Build the bundled image once; custom images must already exist."""
    if await _image_exists_locally(docker_bin, image):
        return
    if image != DEFAULT_IMAGE:
        raise RuntimeError(
            f"docker image {image!r} not found locally. "
            f"pull it (`docker pull {image}`) or clear "
            "runtime.docker_image to use the bundled default."
        )
    async with _BUILD_LOCK:
        if await _image_exists_locally(docker_bin, image):
            logger.info(
                "agent %s: image %s was built by another worker during our "
                "wait; skipping rebuild",
                agent_id,
                image,
            )
            return
        logger.info(
            "agent %s: building docker image %s (first use may take minutes)",
            agent_id,
            image,
        )
        await _build_image(docker_bin, image, agent_id)


async def _build_image(docker_bin: str, image: str, agent_id: str) -> None:
    from ...._proc import no_window_kwargs

    proc = await asyncio.create_subprocess_exec(
        docker_bin,
        "build",
        "-t",
        image,
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **no_window_kwargs(),
    )
    stdout, _ = await communicate_with_timeout(
        proc,
        input_data=DOCKERFILE.encode(),
        timeout_seconds=DOCKER_BUILD_TIMEOUT_SECONDS,
        operation="docker build",
    )
    if proc.returncode != 0:
        tail = stdout.decode("utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"docker build failed:\n{tail}")
    logger.info("agent %s: docker image %s built", agent_id, image)


def probe_result(returncode: int) -> bool | None:
    if returncode == 0:
        return True
    if returncode == PROBE_FALSE_EXIT:
        return False
    return None


async def communicate_with_timeout(
    proc: asyncio.subprocess.Process,
    *,
    input_data: bytes | None = None,
    timeout_seconds: float,
    operation: str,
) -> tuple[bytes, bytes]:
    communicate_task = spawn(
        proc.communicate(input_data),
        name="proc.communicate",
    )
    try:
        return await asyncio.wait_for(
            asyncio.shield(communicate_task),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        await _kill_and_reap(proc, communicate_task)
        raise RuntimeError(
            f"{operation} timed out after {timeout_seconds:g}s; "
            "the child process was terminated"
        ) from exc
    except asyncio.CancelledError:
        await _kill_and_reap(proc, communicate_task)
        raise


async def _kill_and_reap(
    proc: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await communicate_task
    except (BrokenPipeError, ConnectionResetError):
        await proc.wait()


async def _image_exists_locally(docker_bin: str, tag: str) -> bool:
    rc, _, _ = await run_cmd(
        [docker_bin, "image", "inspect", tag],
        check=False,
    )
    return rc == 0


async def run_cmd(
    cmd: list[str],
    check: bool = True,
    *,
    timeout_seconds: float = DOCKER_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, bytes, bytes]:
    from ...._proc import no_window_kwargs

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **no_window_kwargs(),
    )
    stdout, stderr = await communicate_with_timeout(
        proc,
        timeout_seconds=timeout_seconds,
        operation=" ".join(cmd[:2]),
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {stderr.decode('utf-8', errors='replace').strip()[:500]}"
        )
    return proc.returncode, stdout, stderr


__all__ = [
    "CLAUDE_CODE_NPM_VERSION",
    "CODEX_NPM_VERSION",
    "CONTAINER_LAYOUT_VERSION",
    "DEFAULT_IMAGE",
    "DOCKERFILE",
    "PROBE_FALSE_EXIT",
    "communicate_with_timeout",
    "container_state",
    "ensure_docker_image",
    "probe_result",
    "puffo_agent_pkg_dir",
    "run_cmd",
]
