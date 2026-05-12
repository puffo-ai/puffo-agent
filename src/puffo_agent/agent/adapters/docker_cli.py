"""Docker CLI adapter.

Runs the Claude Code CLI inside a per-agent Docker container. The
container is the sandbox; Claude Code runs with
``--dangerously-skip-permissions`` inside.

Auth: each agent gets its own isolated claude identity at
``~/.puffo-agent/agents/<id>/.claude/`` (sessions, history, cache,
settings — seeded once from the operator's real ``~/.claude``). The
``.credentials.json`` file alone is a single-file bind-mount of the
host's copy so every agent shares one rotating-refresh-token source
and avoids the race per-agent copies would hit.

A second bind-mount exposes ``~/.puffo-agent/shared/`` at
``/workspace/.shared`` so all agents on this host can cooperate at
the filesystem level.

Lifecycle:
  - container: one per agent (``puffo-<id>``), started lazily,
    ``docker stop`` on ``aclose()``.
  - claude: one long-lived stream-json subprocess inside the
    container, kept alive across turns by ``ClaudeSession``.
  - session id: persisted to ``cli_session.json`` so daemon /
    container restarts re-spawn with ``--resume <id>``.

Image: bundled inline as a Dockerfile string, built on first use.
Users can override via ``runtime.docker_image`` to skip the build.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path

from ...mcp.config import (
    write_cli_mcp_config,
)
from ...portal.state import (
    seed_claude_home,
    sync_host_gemini_mcp_servers,
    sync_host_gemini_skills,
    sync_host_mcp_servers,
    sync_host_skills,
)
from .base import Adapter, TurnContext, TurnResult, looks_like_auth_failure
from .cli_session import AuditLog, ClaudeSession


logger = logging.getLogger(__name__)


def _puffo_agent_pkg_dir() -> Path:
    """Host-side puffo_agent package import root, bind-mounted
    read-only into cli-docker containers at /opt/puffoagent-pkg so
    the in-container puffo-core MCP server can ``import puffo_agent.*``.
    """
    import puffo_agent
    return Path(puffo_agent.__file__).resolve().parent.parent


# Bump on Dockerfile changes so existing hosts rebuild without manual
# image-tag pruning. ``_ensure_image`` only builds when the tag is
# missing locally.
DEFAULT_IMAGE = "puffo/agent-runtime:v11"

# Pinned Claude Code CLI version baked into the image. Floating would
# let an upstream release shift the stream-json protocol or
# ``--permission-mode`` semantics under us; bump deliberately after
# verification.
CLAUDE_CODE_NPM_VERSION = "2.1.117"

# Pinned Gemini CLI version (same reproducibility rationale).
GEMINI_CLI_NPM_VERSION = "0.38.2"

# Refresh one-shot timeout. Cold claude + OAuth refresh + one-turn
# API call lands at 5-15s normally, can stretch past 30s on a busy
# host; 120s covers the chain without letting a wedged subprocess
# stall the tick.
REFRESH_ONESHOT_TIMEOUT_SECONDS = 120

# Kept minimal. The claude CLI refuses --dangerously-skip-permissions
# as root, so we create a non-root ``agent`` user. UID doesn't need
# to match the host: Docker Desktop's VFS maps bind-mount perms.
#
# PID 1 tails the host-written audit log (via the workspace bind-
# mount) so ``docker logs <container>`` streams turn events. Without
# it the container would be a black box since the claude subprocess
# is spawned via docker-exec and its stdout returns to the host
# adapter, not container PID 1.
DOCKERFILE = """\
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git curl ca-certificates jq ripgrep \\
        python3 python3-pip \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g \\
        @anthropic-ai/claude-code@__CLAUDE_CODE_VERSION__ \\
        @google/gemini-cli@__GEMINI_CLI_VERSION__

# Puffo MCP tools server deps. ``--break-system-packages`` is
# required on Debian bookworm (PEP 668); acceptable since the
# container is single-purpose and disposable. ``uv`` ships ``uvx``
# (Python counterpart of ``npx``) so agents can register stdio MCPs
# without per-server pip/npm install.
#
# hermes-agent: the alternative harness. Installed from git because
# upstream isn't on PyPI. Billing for OAuth-token usage routes to
# Anthropic's ``extra_usage`` pool — not the Claude subscription.
RUN pip3 install --break-system-packages --no-cache-dir \\
        "mcp>=1.0" "aiohttp>=3.9" "uv>=0.5" \\
        "cryptography>=43" "pyhpke>=0.6" "aiosqlite>=0.20" "pyyaml>=6.0" \\
     && pip3 install --break-system-packages --no-cache-dir \\
        "git+https://github.com/NousResearch/hermes-agent.git@main"

RUN useradd -m -u 2000 -s /bin/bash agent
USER agent
WORKDIR /workspace

# GNU ``tail -F`` relies on inotify, and inotify doesn't propagate
# through Docker Desktop's host bind-mount on Windows / macOS.
# Instead we poll file size each second and emit newly-appended
# bytes to stdout for ``docker logs``. Start from EOF so we don't
# re-dump history on every restart.
CMD ["sh", "-c", "set -eu; mkdir -p /workspace/.puffo-agent; touch /workspace/.puffo-agent/audit.log; echo \\"[$(date -u +%FT%TZ)] puffo agent=${PUFFO_AGENT_ID:-unknown} container starting; polling /workspace/.puffo-agent/audit.log every 1s\\"; last=$(stat -c%s /workspace/.puffo-agent/audit.log 2>/dev/null || echo 0); while :; do size=$(stat -c%s /workspace/.puffo-agent/audit.log 2>/dev/null || echo 0); if [ \\"$size\\" -gt \\"$last\\" ]; then tail -c +$((last + 1)) /workspace/.puffo-agent/audit.log; last=$size; elif [ \\"$size\\" -lt \\"$last\\" ]; then last=0; fi; sleep 1; done"]
""".replace(
    "__CLAUDE_CODE_VERSION__", CLAUDE_CODE_NPM_VERSION,
).replace(
    "__GEMINI_CLI_VERSION__", GEMINI_CLI_NPM_VERSION,
)


class DockerCLIAdapter(Adapter):
    def __init__(
        self,
        agent_id: str,
        model: str,
        image: str,
        workspace_dir: str,
        claude_dir: str,
        session_file: str,
        agent_home_dir: str,
        shared_fs_dir: str,
        owner_username: str = "",
        harness=None,
        google_api_key: str = "",
        memory_limit: str = "",
        memory_reservation: str = "",
    ):
        self.agent_id = agent_id
        self.model = model
        self.image = image or DEFAULT_IMAGE
        self.workspace_dir = workspace_dir
        self.claude_dir = claude_dir
        self.session_file = Path(session_file)
        self.container_name = f"puffo-{agent_id}"
        # Agent's virtual $HOME; only .claude (and .gemini, .claude.json)
        # are bind-mounted in, not the whole home, so the container's
        # default home skeleton stays intact.
        self.agent_home_dir = Path(agent_home_dir)
        self.claude_home_src = self.agent_home_dir / ".claude"
        # Cross-agent cooperation dir; same mount in every container
        # on this host — intentional escape hatch from per-agent
        # isolation.
        self.shared_fs_dir = Path(shared_fs_dir)
        self.owner_username = owner_username
        # Only used when harness is gemini-cli (passed via
        # ``docker exec -e GEMINI_API_KEY=...``).
        self.google_api_key = google_api_key
        # Optional cgroup caps. ``--memory`` is a hard ceiling that
        # OOM-kills processes in this container only; ``--memory-
        # reservation`` is a soft floor. Bound a runaway claude so it
        # doesn't drain the VM and trigger ENOMEM on neighbours' small
        # reads. Empty = no flag = Docker default unbounded.
        self.memory_limit = memory_limit
        self.memory_reservation = memory_reservation
        # Which agent engine runs inside the container.
        if harness is None:
            from ..harness import ClaudeCodeHarness
            harness = ClaudeCodeHarness()
        self.harness = harness
        self._started_lock = asyncio.Lock()
        self._started = False
        self._session: ClaudeSession | None = None
        # Has the puffo MCP server been registered with the
        # in-container hermes config yet? Registration is idempotent
        # (remove + add) so a flag mismatch is safe. The gemini path
        # writes MCP config upfront via ``_ensure_started`` instead.
        self._hermes_mcp_registered = False
        # Set post-construction by worker.py. When non-None, claude-
        # code is routed at ``puffo_core_server``. Values must be
        # CONTAINER-local paths since the MCP subprocess runs inside
        # the container.
        self.puffo_core_mcp_env: dict[str, str] | None = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        await self._ensure_started()
        user_message = ctx.messages[-1]["content"] if ctx.messages else ""
        if self.harness.name() == "hermes":
            return await self._run_turn_hermes(user_message, ctx.system_prompt)
        if self.harness.name() == "gemini-cli":
            return await self._run_turn_gemini(user_message, ctx.system_prompt)
        session = self._ensure_session()
        return await session.run_turn(user_message, ctx.system_prompt)

    async def run_retry_turn(
        self,
        kick_text: str,
        fallback_user_message: str,
        ctx: TurnContext,
    ) -> TurnResult:
        # claude-code only — hermes / gemini-cli always run one-shot
        # without --resume, so a retry is just a normal turn against
        # the fallback payload.
        if self.harness.name() != "claude-code":
            ctx_fallback = TurnContext(
                system_prompt=ctx.system_prompt,
                messages=[{"role": "user", "content": fallback_user_message}],
                workspace_dir=ctx.workspace_dir,
                claude_dir=ctx.claude_dir,
                memory_dir=ctx.memory_dir,
                on_progress=ctx.on_progress,
            )
            return await self.run_turn(ctx_fallback)
        await self._ensure_started()
        session = self._ensure_session()
        return await session.run_retry_turn(
            kick_text, fallback_user_message, ctx.system_prompt,
        )

    async def _run_turn_hermes(self, user_message: str, system_prompt: str) -> TurnResult:
        """One-shot hermes turn via ``hermes chat --provider anthropic
        --quiet [--continue] -q <prompt>``.

        Hermes has no stream-json line protocol; interactive mode
        requires a TTY and treats piped EOF as "user quit". Cold
        start per turn is ~3-7s.

        Auth: hermes auto-discovers the bind-mounted
        ``~/.claude/.credentials.json``; no hermes-side state.

        Continuity: ``cli_session.json`` is a "have we done at least
        one turn" sentinel. First turn inlines the system prompt (no
        ``--system`` flag in hermes); subsequent turns pass
        ``--continue``. Stale sentinel triggers a one-shot retry
        without ``--continue``.
        """
        return await self._run_hermes_chat(user_message, system_prompt)

    async def _ensure_hermes_mcp_registered(self) -> None:
        """Register the puffo MCP server with the in-container hermes
        config so chat turns can call puffo tools.

        Hermes uses its own ``hermes mcp add`` registry at
        ``/home/agent/.hermes/config.yaml``. Re-registered on every
        adapter start so config-shape changes are picked up
        automatically. ``hermes mcp add`` prompts "Enable all N tools?
        [Y/n/select]" before writing config — we pipe ``y\\n`` to
        accept. Failure logs but doesn't hard-fail the turn (chat
        still works, just without tools).
        """
        if self._hermes_mcp_registered:
            return
        if self.puffo_core_mcp_env is None:
            logger.warning(
                "agent %s: hermes MCP registration skipped — puffo_core "
                "is not configured. Populate `puffo_core:` in agent.yml "
                "to enable tool calls under hermes.",
                self.agent_id,
            )
            return

        env = dict(self.puffo_core_mcp_env)
        env["PUFFO_CORE_KEYSTORE_DIR"] = "/home/agent/.puffo-agent-state/keys"
        # No PUFFO_CORE_DB_PATH — MCP routes data reads through the
        # daemon's data service at PUFFO_DATA_SERVICE_URL.
        env["PUFFO_WORKSPACE"] = "/workspace"
        env["PUFFO_RUNTIME_KIND"] = "cli-docker"
        env["PUFFO_HARNESS"] = "hermes"
        env["PYTHONPATH"] = "/opt/puffoagent-pkg"
        env_flags: list[str] = [f"{k}={v}" for k, v in env.items()]

        # Remove any stale puffo registration first so the add below
        # overwrites cleanly. rc!=0 is fine — means there wasn't one.
        await _run_cmd(
            [
                "docker", "exec", self.container_name,
                "hermes", "mcp", "remove", "puffo",
            ],
            check=False,
        )

        cmd = [
            "docker", "exec", "-i", self.container_name,
            "hermes", "mcp", "add", "puffo",
            "--command", "python3",
            "--args", "-m", "puffo_agent.mcp.puffo_core_server",
            "--env", *env_flags,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(b"y\n")
        except Exception as exc:
            logger.warning(
                "agent %s: couldn't register puffo MCP with hermes: %s "
                "(chat will work, tool calls won't)",
                self.agent_id, exc,
            )
            return
        if proc.returncode != 0:
            logger.warning(
                "agent %s: hermes mcp add puffo rc=%d | stdout: %s | stderr: %s "
                "(chat will work, tool calls won't)",
                self.agent_id, proc.returncode,
                stdout.decode("utf-8", errors="replace").strip()[-400:],
                stderr.decode("utf-8", errors="replace").strip()[-400:],
            )
            return
        logger.info(
            "agent %s: registered puffo MCP server with hermes "
            "(18 tools available via hermes chat)",
            self.agent_id,
        )
        self._hermes_mcp_registered = True

    async def _run_hermes_chat(
        self, user_message: str, system_prompt: str, *, _retried: bool = False,
    ) -> TurnResult:
        # Hermes doesn't actually auto-discover Claude Code's
        # credentials despite upstream docs claiming it does. We read
        # the access token from the bind-mounted credentials file and
        # pass it via ``ANTHROPIC_API_KEY`` on the ``docker exec``
        # command. ``sk-ant-oat01-*`` tokens are API-compatible with
        # regular ``sk-ant-api03-*`` keys; billing routes to
        # Anthropic's ``extra_usage`` pool, not the Claude
        # subscription.
        token = _read_claude_access_token()
        if not token:
            logger.error(
                "agent %s: cannot read Claude Code access token from "
                "%s — hermes turn would fail with no credentials. "
                "run `claude login` on the host to refresh.",
                self.agent_id, _HOST_CLAUDE_CREDENTIALS_PATH,
            )
            return TurnResult(reply="", metadata={
                "error": "no Claude Code access token available on host",
            })

        # Idempotent — skipped after first success per adapter
        # instance.
        await self._ensure_hermes_mcp_registered()

        has_prior_session = self.session_file.exists()
        prompt = user_message if has_prior_session else _stitch_hermes_prompt(
            system_prompt, user_message,
        )
        cmd = [
            "docker", "exec", "-i",
            # Token in argv-space is acceptable for a single-user
            # host; switch to --env-file + tmpfile if running on a
            # shared host.
            "-e", f"ANTHROPIC_API_KEY={token}",
            self.container_name,
            "hermes", "chat",
            "--provider", "anthropic",
            "--quiet",
            "--source", f"puffoagent:{self.agent_id}",
            "--model", _hermes_model_id(self.model),
        ]
        if has_prior_session:
            cmd.append("--continue")
        cmd.extend(["-q", prompt])

        started = time.time()
        rc, stdout, stderr = await _run_cmd(cmd, check=False)
        elapsed = time.time() - started
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        # Stale sentinel: hermes has no session matching ours.
        # Clear + retry once without --continue.
        if (
            rc != 0
            and _HERMES_NO_RESUME_SIGNATURE in stdout_text
            and not _retried
        ):
            logger.info(
                "agent %s: hermes rejected --continue; clearing sentinel and retrying fresh",
                self.agent_id,
            )
            try:
                self.session_file.unlink()
            except OSError:
                pass
            return await self._run_hermes_chat(
                user_message, system_prompt, _retried=True,
            )

        if rc != 0:
            logger.error(
                "agent %s: hermes turn rc=%d in %.1fs | stdout: %r | stderr: %s",
                self.agent_id, rc, elapsed,
                stdout_text.strip()[:400],
                stderr_text.strip()[-400:] or "(empty)",
            )
            return TurnResult(reply="", metadata={
                "error": f"hermes exited rc={rc}",
                "stdout_snippet": stdout_text[:400],
                "stderr_tail": stderr_text[-400:],
            })

        reply, session_id = _parse_hermes_reply(stdout_text)
        if not reply:
            logger.warning(
                "agent %s: hermes rc=0 but parser found no reply. "
                "stdout: %r", self.agent_id, stdout_text[:400],
            )

        # First-ever success: write the sentinel so subsequent turns
        # pass --continue. session_id is captured for debug.
        if not has_prior_session:
            try:
                self.session_file.parent.mkdir(parents=True, exist_ok=True)
                self.session_file.write_text(
                    json.dumps({
                        "harness": "hermes",
                        "session_id": session_id,
                        "first_turn_at": int(time.time()),
                    }) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning(
                    "agent %s: couldn't write hermes session_file: %s "
                    "(next turn will start a fresh session)",
                    self.agent_id, exc,
                )

        logger.info(
            "agent %s: hermes turn rc=0 in %.1fs, %d reply chars, "
            "session=%s, resume=%s",
            self.agent_id, elapsed, len(reply), session_id or "?",
            has_prior_session,
        )
        return TurnResult(reply=reply, metadata={
            "harness": "hermes",
            "session_id": session_id,
        })

    # ── Gemini harness ────────────────────────────────────────────

    async def _run_turn_gemini(
        self, user_message: str, system_prompt: str,
    ) -> TurnResult:
        """One-shot gemini-cli turn via ``gemini -p <prompt>
        --output-format json [-r latest]``.

        Auth: ``GEMINI_API_KEY`` from daemon.yml passed via
        ``docker exec -e``.

        Continuity: ``cli_session.json`` sentinel gates ``-r latest``;
        stale sentinel falls back to a fresh session.

        Persona + memory: ``<agent_home>/.gemini/GEMINI.md`` is
        rewritten on every start; gemini auto-discovers it.

        MCP tools: registered in PROJECT-scope ``<workspace>/.gemini/
        settings.json`` (gemini's MCP resolver defaults to cwd, not
        $HOME). Same file merges in host user-level MCPs.
        """
        return await self._run_gemini_chat(user_message, system_prompt)

    async def _run_gemini_chat(
        self, user_message: str, system_prompt: str, *, _retried: bool = False,
    ) -> TurnResult:
        if not self.google_api_key:
            logger.error(
                "agent %s: gemini-cli turn requires a google api_key "
                "(passed as GEMINI_API_KEY into the container). Pass "
                "--api-key on `agent create`, set GEMINI_API_KEY in "
                "the environment, or run `puffo-agent config`.",
                self.agent_id,
            )
            return TurnResult(reply="", metadata={
                "error": "no google api_key configured",
            })

        # Persona + memory + MCP entries are written upfront by
        # ``_ensure_started``, so just send the user message.
        has_prior_session = self.session_file.exists()
        cmd = _build_gemini_argv(
            container_name=self.container_name,
            api_key=self.google_api_key,
            model=self.model,
            has_prior_session=has_prior_session,
            user_message=user_message,
        )

        # Log the redacted argv so a failed turn is reproducible
        # from the daemon log.
        redacted = [
            "GEMINI_API_KEY=***" if a.startswith("GEMINI_API_KEY=") else a
            for a in cmd
        ]
        logger.info("agent %s: gemini argv: %s", self.agent_id, " ".join(redacted))

        started = time.time()
        rc, stdout, stderr = await _run_cmd(cmd, check=False)
        elapsed = time.time() - started
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        # Stale sentinel recovery — any error with ``-r`` in play
        # triggers one retry from a fresh session. Permissive because
        # upstream doesn't pin an error string to match against.
        if rc != 0 and has_prior_session and not _retried:
            logger.info(
                "agent %s: gemini -r latest rc=%d; clearing sentinel "
                "and retrying with a fresh session. stderr: %s",
                self.agent_id, rc, stderr_text.strip()[-200:] or "(empty)",
            )
            try:
                self.session_file.unlink()
            except OSError:
                pass
            return await self._run_gemini_chat(
                user_message, system_prompt, _retried=True,
            )

        if rc != 0:
            logger.error(
                "agent %s: gemini turn rc=%d in %.1fs | stdout: %r | stderr: %s",
                self.agent_id, rc, elapsed,
                stdout_text.strip()[:400],
                stderr_text.strip()[-400:] or "(empty)",
            )
            return TurnResult(reply="", metadata={
                "error": f"gemini exited rc={rc}",
                "stdout_snippet": stdout_text[:400],
                "stderr_tail": stderr_text[-400:],
            })

        reply, session_id, err = _parse_gemini_reply(stdout_text)
        if err:
            logger.warning(
                "agent %s: gemini rc=0 but returned JSON error: %s",
                self.agent_id, err,
            )
        if not reply:
            logger.warning(
                "agent %s: gemini rc=0 but parser found no reply. "
                "stdout: %r", self.agent_id, stdout_text[:400],
            )

        if not has_prior_session:
            try:
                self.session_file.parent.mkdir(parents=True, exist_ok=True)
                self.session_file.write_text(
                    json.dumps({
                        "harness": "gemini-cli",
                        "session_id": session_id,
                        "first_turn_at": int(time.time()),
                    }) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning(
                    "agent %s: couldn't write gemini session_file: %s "
                    "(next turn will start a fresh session)",
                    self.agent_id, exc,
                )

        logger.info(
            "agent %s: gemini turn rc=0 in %.1fs, %d reply chars, "
            "session=%s, resume=%s%s",
            self.agent_id, elapsed, len(reply), session_id or "?",
            has_prior_session,
            f", err={err!r}" if err else "",
        )
        metadata: dict = {
            "harness": "gemini-cli",
            "session_id": session_id,
        }
        if err:
            metadata["error"] = err
        return TurnResult(reply=reply, metadata=metadata)

    async def warm(self, system_prompt: str) -> None:
        """Start the container eagerly; spawn the claude subprocess
        only when this agent has a persisted session (fresh agents
        wait for their first message). Container always starts so
        ``docker logs`` tailing is useful even when idle.
        """
        await self._ensure_started()
        if self.harness.name() == "hermes":
            # Hermes is one-shot per turn — no persistent subprocess.
            return
        session = self._ensure_session()
        if not session.has_persisted_session():
            logger.info(
                "agent %s: no persisted session; deferring claude spawn until first message",
                self.agent_id,
            )
            return
        await session.warm(system_prompt)

    async def reload(self, new_system_prompt: str) -> None:
        """Close the in-container claude subprocess so the next turn
        spawns one that re-reads CLAUDE.md. Container stays up.
        No-op for hermes (each turn is already fresh).
        """
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    def _credentials_expires_in_seconds(self) -> int | None:
        # Every cli-docker agent's container reads the host's
        # credentials file via bind-mount, so the host copy is the
        # source of truth for expiry.
        host_credentials = Path.home() / ".claude" / ".credentials.json"
        try:
            data = json.loads(host_credentials.read_text(encoding="utf-8"))
            expires_ms = int(data["claudeAiOauth"]["expiresAt"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return int(expires_ms / 1000 - time.time())

    async def _run_refresh_oneshot(self) -> None:
        """Spawn a short-lived ``docker exec <container> claude
        --print ...`` alongside the long-lived stream-json session.
        The long-lived session refreshes tokens in memory but only
        rewrites ``.credentials.json`` on exit; the one-shot exit
        forces that write so sibling agents see the new token on
        their next read. ``--max-turns 1`` bounds the loop; stream-
        json output avoids buffered-text wedges in docker-exec.
        """
        await self._ensure_started()
        cmd = [
            "docker", "exec", self.container_name,
            "claude", "--dangerously-skip-permissions",
            "--print", "--max-turns", "1",
            "--output-format", "stream-json", "--verbose",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        # Any prompt drives the API hit; reply is discarded.
        cmd.append("ok")
        started_at = time.time()
        try:
            rc, stdout, stderr = await asyncio.wait_for(
                _run_cmd(cmd, check=False),
                timeout=REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "agent %s: refresh one-shot timed out after %ds",
                self.agent_id, REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
            return
        elapsed = time.time() - started_at
        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        # Doubles as an inference smoke test (claude auth status can
        # report OK while every API call returns 401).
        if looks_like_auth_failure(out_text, err_text):
            logger.error(
                "agent %s: refresh one-shot hit an auth failure "
                "(rc=%d in %.1fs). operator re-auth likely required. "
                "stdout: %s | stderr: %s",
                self.agent_id, rc, elapsed,
                out_text.strip()[-400:], err_text.strip()[-400:],
            )
            self.auth_healthy = False
        elif rc != 0:
            logger.warning(
                "agent %s: refresh one-shot rc=%d in %.1fs | "
                "stdout: %s | stderr: %s",
                self.agent_id, rc, elapsed,
                out_text.strip()[-400:], err_text.strip()[-400:],
            )
        else:
            logger.debug(
                "agent %s: refresh one-shot rc=0 in %.1fs",
                self.agent_id, elapsed,
            )
            self.auth_healthy = True

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if not self._started:
            return
        # ``docker stop`` (not ``rm -f``) preserves the container's
        # fs — workspace, claude session files, MCP config — so the
        # next start resumes ``--resume <session_id>`` cleanly.
        # ``-t 5`` shortens docker's 10s SIGTERM grace; stays within
        # Worker.stop's 30s asyncio.wait_for even on slow Windows.
        await _run_cmd(
            ["docker", "stop", "-t", "5", self.container_name], check=False,
        )
        self._started = False

    def _ensure_session(self) -> ClaudeSession:
        if self._session is not None:
            return self._session
        extra = self._prepare_mcp_args()
        self._session = ClaudeSession(
            agent_id=self.agent_id,
            session_file=self.session_file,
            build_command=self._build_command,
            # cwd is WORKDIR /workspace inside the container.
            cwd=None,
            # Host-side write; the workspace bind-mount delivers it
            # to the container's tail loop and ``docker logs``.
            audit=AuditLog(
                Path(self.workspace_dir) / ".puffo-agent" / "audit.log",
                self.agent_id,
            ),
            extra_args=extra,
        )
        return self._session

    def _build_command(
        self,
        extra_args: list[str],
        env_overrides: dict[str, str] | None = None,
    ) -> list[str]:
        cmd: list[str] = ["docker", "exec", "-i"]
        # ``env_overrides`` flows in before the container name so
        # docker treats each ``-e KEY=VALUE`` as an exec flag.
        for key, value in (env_overrides or {}).items():
            cmd.extend(["-e", f"{key}={value}"])
        cmd.extend([
            self.container_name,
            "claude", "--dangerously-skip-permissions",
        ])
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.extend(extra_args)
        return cmd

    def _prepare_mcp_args(self) -> list[str]:
        """Write the per-agent MCP config into the workspace and
        return the corresponding claude CLI flags. No
        ``--permission-prompt-tool`` — the container is the sandbox.
        """
        config_host = Path(self.workspace_dir) / ".puffo-agent" / "mcp-config.json"

        # Path values must be CONTAINER-local — override whatever the
        # worker put in the env dict from the host side.
        if self.puffo_core_mcp_env is not None:
            env = dict(self.puffo_core_mcp_env)
            env["PUFFO_CORE_KEYSTORE_DIR"] = "/home/agent/.puffo-agent-state/keys"
            # No PUFFO_CORE_DB_PATH — SQLite reads route via the
            # daemon's data service.
            env["PUFFO_WORKSPACE"] = "/workspace"
            env["PUFFO_RUNTIME_KIND"] = "cli-docker"
            env["PUFFO_HARNESS"] = self.harness.name()
            env["PYTHONPATH"] = "/opt/puffoagent-pkg"
            write_cli_mcp_config(
                config_host,
                command="python3",
                args=["-m", "puffo_agent.mcp.puffo_core_server"],
                env=env,
            )
            return ["--mcp-config", "/workspace/.puffo-agent/mcp-config.json"]

        logger.warning(
            "agent %s: cli-docker MCP tools unavailable — puffo_core is "
            "not configured. populate `puffo_core:` in agent.yml so "
            "send_message / list_channels / etc. show up under "
            "claude-code's tool surface.",
            self.agent_id,
        )
        return []

    async def _puffo_pkg_mount_is_current(self) -> bool:
        """``True`` iff the existing container's
        ``/opt/puffoagent-pkg`` bind mount still resolves to a
        directory containing the ``puffo_agent`` package.

        Implemented as a ``docker exec test -f`` rather than
        comparing ``docker inspect``'s Mount.Source against
        ``_puffo_agent_pkg_dir()`` because Docker Desktop on Windows
        rewrites the source path (``/run/desktop/mnt/host/c/...``)
        and a literal string compare wouldn't survive that. The
        in-container probe is authoritative: if claude-code's MCP
        subprocess can ``import puffo_agent`` from the bind mount,
        ``__init__.py`` must be visible — and if it isn't, the
        subprocess will crash and every puffo MCP tool will surface
        as "No such tool available".
        """
        rc, _, _ = await _run_cmd(
            [
                "docker", "exec", self.container_name,
                "test", "-f",
                "/opt/puffoagent-pkg/puffo_agent/__init__.py",
            ],
            check=False,
        )
        return rc == 0

    async def _container_state(self) -> str:
        """Docker-reported container State.Status (``running``,
        ``exited``, ``paused``, ``created``, ``dead``), or ``""``
        when the container doesn't exist.
        """
        rc, out, _ = await _run_cmd(
            [
                "docker", "inspect",
                "-f", "{{.State.Status}}",
                self.container_name,
            ],
            check=False,
        )
        if rc != 0:
            return ""
        return out.decode("utf-8", errors="replace").strip()

    async def _ensure_started(self) -> None:
        async with self._started_lock:
            if self._started:
                return
            if shutil.which("docker") is None:
                raise RuntimeError(
                    "docker binary not found on PATH. install Docker Desktop "
                    "(Windows/macOS) or docker-ce (Linux) to use runtime "
                    "kind 'cli-docker'."
                )
            # Seed the per-agent virtual $HOME from the operator's
            # real $HOME on first use (settings, .claude.json).
            # .credentials.json is also seeded but the docker mount
            # overlays it with the host file so refreshes propagate.
            host_home = Path.home()
            seeded = seed_claude_home(host_home, self.agent_home_dir)
            if seeded:
                logger.info(
                    "agent %s: seeded per-agent virtual $HOME at %s from %s",
                    self.agent_id, self.agent_home_dir, host_home,
                )
            # One-way sync of host skills + MCP registrations into
            # the per-agent home. Runs every start so host edits
            # propagate without daemon restart.
            skill_count = sync_host_skills(host_home, self.agent_home_dir)
            if skill_count:
                logger.info(
                    "agent %s: synced %d host skill(s) into %s",
                    self.agent_id, skill_count,
                    self.agent_home_dir / ".claude" / "skills",
                )
            merged_mcp, unreachable = sync_host_mcp_servers(
                host_home, self.agent_home_dir,
            )
            if merged_mcp:
                logger.info(
                    "agent %s: merged %d host MCP server registration(s) "
                    "into per-agent .claude.json", self.agent_id, merged_mcp,
                )
            for name, cmd in unreachable:
                logger.warning(
                    "agent %s: host MCP %r command %r looks host-local and "
                    "won't resolve inside the container. Install the "
                    "binary in the image or bind-mount it explicitly, "
                    "otherwise this MCP will fail on first use.",
                    self.agent_id, name, cmd,
                )

            # Gemini host sync — always runs (cheap when there's no
            # ~/.gemini/) so harness swap doesn't require a rebuild.
            # Target is PROJECT-SCOPE (<workspace>/.gemini/), not
            # user-scope: gemini's MCP resolver from cwd ignores
            # $HOME/.gemini/settings.json.
            gemini_project_dir = Path(self.workspace_dir)
            gemini_skill_count = sync_host_gemini_skills(
                host_home, gemini_project_dir,
            )
            if gemini_skill_count:
                logger.info(
                    "agent %s: synced %d host gemini skill(s) into %s",
                    self.agent_id, gemini_skill_count,
                    gemini_project_dir / ".gemini" / "skills",
                )
            # Inject the puffo MCP entry in the same write — no
            # separate ``gemini mcp add`` subprocess to race.
            puffo_entry = _puffo_gemini_mcp_entry(
                puffo_core_mcp_env=self.puffo_core_mcp_env,
            )
            merged_gemini_mcp, gemini_unreachable = sync_host_gemini_mcp_servers(
                host_home, gemini_project_dir,
                extra_servers={"puffo": puffo_entry} if puffo_entry else None,
            )
            if merged_gemini_mcp:
                logger.info(
                    "agent %s: merged %d host gemini MCP server "
                    "registration(s) into .gemini/settings.json",
                    self.agent_id, merged_gemini_mcp,
                )
            for name, cmd in gemini_unreachable:
                logger.warning(
                    "agent %s: host gemini MCP %r command %r looks "
                    "host-local and won't resolve inside the container. "
                    "Install the binary in the image or bind-mount it, "
                    "otherwise the MCP will fail on first use.",
                    self.agent_id, name, cmd,
                )

            if not (host_home / ".claude" / ".credentials.json").exists():
                logger.warning(
                    "agent %s: host has no %s — run `claude login` on the "
                    "host, then restart the agent. First turn will fail "
                    "with an auth error otherwise.",
                    self.agent_id, host_home / ".claude" / ".credentials.json",
                )
            # Reuse the container left behind by a prior daemon run
            # (``aclose`` does ``docker stop``, not ``rm``) so
            # ``--resume <session_id>`` reattaches cleanly on the
            # next turn instead of paying container boot + image
            # pull every restart.
            state = await self._container_state()
            existed = state != ""
            if state == "running":
                logger.info(
                    "agent %s: reusing running container %r",
                    self.agent_id, self.container_name,
                )
            elif state in ("exited", "created", "dead"):
                logger.info(
                    "agent %s: starting existing container %r (was %s)",
                    self.agent_id, self.container_name, state,
                )
                await _run_cmd(["docker", "start", self.container_name])
            elif state == "paused":
                logger.info(
                    "agent %s: unpausing container %r",
                    self.agent_id, self.container_name,
                )
                await _run_cmd(["docker", "unpause", self.container_name])
            else:
                # state == "" — no container with this name.
                await self._ensure_image()
                await self._start_container()

            # Validate the puffo_agent bind mount on REUSED
            # containers. The /opt/puffoagent-pkg bind mount source
            # is baked in at ``docker run`` time and immutable until
            # the container is recreated. If the operator pip-
            # reinstalled puffo-agent from a different host path
            # (e.g. uninstalled the editable install from
            # puffo-core-han-group/agent and re-installed from
            # puffo-ai/puffo-agent), the container is still bound to
            # the old — now non-existent — path. ``python3 -m
            # puffo_agent.mcp.puffo_core_server`` inside the
            # container then fails with ModuleNotFoundError, and
            # claude-code reports every puffo MCP tool as
            # "No such tool available". Detect that case here and
            # recreate.
            if existed and not await self._puffo_pkg_mount_is_current():
                logger.warning(
                    "agent %s: container %r has a stale "
                    "/opt/puffoagent-pkg bind mount (the host path it "
                    "was created with no longer contains puffo_agent). "
                    "Recreating so claude-code's MCP subprocess can "
                    "import the package again — typical cause is a "
                    "pip reinstall from a different path.",
                    self.agent_id, self.container_name,
                )
                await _run_cmd(
                    ["docker", "rm", "-f", self.container_name],
                    check=False,
                )
                await self._ensure_image()
                await self._start_container()
            self._started = True

    async def _ensure_image(self) -> None:
        if await _image_exists_locally(self.image):
            return
        if self.image != DEFAULT_IMAGE:
            raise RuntimeError(
                f"docker image {self.image!r} not found locally. "
                f"pull it (`docker pull {self.image}`) or clear "
                "runtime.docker_image to use the bundled default."
            )
        # Daemon-wide lock — concurrent ``docker build -t <tag>``
        # races in BuildKit's exporter and the loser crashes with
        # "image already exists". First wins; others wait and re-check.
        async with _BUILD_LOCK:
            if await _image_exists_locally(self.image):
                logger.info(
                    "agent %s: image %s was built by another worker "
                    "during our wait — skipping rebuild",
                    self.agent_id, self.image,
                )
                return
            logger.info(
                "agent %s: building docker image %s (first use — this may take a few minutes)",
                self.agent_id, self.image,
            )
            await self._build_image()

    async def _build_image(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", self.image, "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate(DOCKERFILE.encode())
        if proc.returncode != 0:
            tail = stdout.decode("utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"docker build failed:\n{tail}")
        logger.info("agent %s: docker image %s built", self.agent_id, self.image)

    async def _start_container(self) -> None:
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
        # Pre-create every bind-mount source as a real dir/file so
        # Docker doesn't auto-create one owned by root that the
        # non-root container user can't write to.
        self.agent_home_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_home_dir / ".claude").mkdir(parents=True, exist_ok=True)
        host_credentials = Path.home() / ".claude" / ".credentials.json"
        if not host_credentials.exists():
            host_credentials.parent.mkdir(parents=True, exist_ok=True)
            host_credentials.touch()
        # .claude.json is a FILE (not a dir) — touch so the
        # bind-mount target is a file, not a dir.
        agent_claude_json = self.agent_home_dir / ".claude.json"
        agent_claude_json.touch(exist_ok=True)
        (self.agent_home_dir / ".gemini").mkdir(parents=True, exist_ok=True)
        self.shared_fs_dir.mkdir(parents=True, exist_ok=True)

        # Bind-mounts per agent:
        #   1. workspace            — project root + cwd
        #   2. .claude dir          — per-agent identity
        #   3. .credentials.json    — SHARED single-file overlay
        #   4. .claude.json         — per-agent CLI config
        #   5. .gemini dir          — per-agent gemini identity
        #   6. shared_fs            — cross-agent cooperation
        #   7. puffoagent pkg       — host package for in-container imports
        #   8. .puffo-agent-state   — keystore + message DB
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "-e", f"PUFFO_AGENT_ID={self.agent_id}",
            "-v", f"{self.workspace_dir}:/workspace",
            "-v", f"{self.claude_home_src}:/home/agent/.claude",
            # .credentials.json mount MUST come after the .claude dir
            # mount for Docker to treat it as a file overlay rather
            # than a no-op.
            "-v", f"{host_credentials}:/home/agent/.claude/.credentials.json",
            # Sibling .claude.json — without this it lands on the
            # container's ephemeral fs and is lost on restart.
            "-v", f"{agent_claude_json}:/home/agent/.claude.json",
            # Always mounted (regardless of harness) so swapping to
            # gemini-cli doesn't need a rebuild.
            "-v", f"{self.agent_home_dir / '.gemini'}:/home/agent/.gemini",
            "-v", f"{self.shared_fs_dir}:/workspace/.shared",
            "-v", f"{_puffo_agent_pkg_dir()}:/opt/puffoagent-pkg:ro",
            # RW because subkey rotation rewrites <slug>.session.json.
            # Mounting :ro surfaced as [Errno 30] from MCP tool calls
            # past the subkey TTL. Whole agent_home_dir is mounted
            # rather than individual files because SQLite WAL files
            # (-wal, -shm) sit alongside the .db.
            "-v", f"{self.agent_home_dir}:/home/agent/.puffo-agent-state",
            "--init",  # reap zombies from claude's child processes
        ]
        # ``--memory`` is a hard cgroup ceiling; ``--memory-reservation``
        # is a soft floor. Either may be empty (operator opt-out).
        if self.memory_limit:
            cmd.extend(["--memory", self.memory_limit])
        if self.memory_reservation:
            cmd.extend(["--memory-reservation", self.memory_reservation])
        cmd.extend([
            self.image,
            # No command override — the image's CMD tails the audit
            # log so ``docker logs`` streams turn events.
        ])
        rc, _, stderr = await _run_cmd(cmd, check=False)
        if rc != 0:
            raise RuntimeError(
                f"docker run failed for {self.container_name}: "
                f"{stderr.decode('utf-8', errors='replace').strip()[:500]}"
            )


# Serialises concurrent ``docker build -t <tag>`` across workers
# (right after an image-tag bump every cli-docker worker would
# otherwise race BuildKit's exporter).
_BUILD_LOCK = asyncio.Lock()


async def _image_exists_locally(tag: str) -> bool:
    rc, _, _ = await _run_cmd(
        ["docker", "image", "inspect", tag], check=False,
    )
    return rc == 0


# Exact stdout line hermes emits when ``--continue`` is passed but
# its session store has nothing to resume.
_HERMES_NO_RESUME_SIGNATURE = "No previous CLI session found to continue"

# Banner / metadata lines from ``hermes --quiet``. Skip-matching
# these isolates the actual response text. Session id arrives on
# either the "Resumed session" line (with ``--continue``) or a
# standalone ``session_id:`` line; sometimes absent on fresh
# sessions (we tolerate that).
_HERMES_SESSION_ID_RE = re.compile(r"^session_id:\s*(\S+)\s*$")
_HERMES_RESUMED_SESSION_RE = re.compile(
    r"^↻\s*Resumed session\s+(\S+).*$"
)
_HERMES_MODEL_NORMALISED_RE = re.compile(
    r"^⚠️\s+Normalized model .*$"
)
# Continuation line of the "Normalized model" banner. Match a bare
# provider name followed by a period so we don't eat reply text
# that happens to start with one.
_HERMES_MODEL_NORMALISED_TAIL_RE = re.compile(r"^[a-z0-9\-]+\.$")


# Host-side Claude Code credentials path. Read on every hermes turn
# because hermes' own auto-discovery is unreliable.
_HOST_CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


def _read_claude_access_token() -> str:
    """Current Claude Code OAuth access token from the host's
    credentials file. Empty string on any failure (missing file,
    malformed JSON, missing key) — caller logs and surfaces a turn-
    level error rather than crashing the worker.
    """
    try:
        data = json.loads(
            _HOST_CLAUDE_CREDENTIALS_PATH.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ""
    return ((data.get("claudeAiOauth") or {}).get("accessToken") or "").strip()


def _hermes_model_id(model: str) -> str:
    """Translate ``runtime.model`` into ``<provider>/<model>`` form.
    Strips Claude-Code suffixes hermes rejects (e.g. ``[1m]``);
    prepends ``anthropic/`` if absent; empty → default.
    """
    base = (model or "").split("[", 1)[0].strip()
    if not base:
        return "anthropic/claude-opus-4-6"
    return base if "/" in base else f"anthropic/{base}"


def _stitch_hermes_prompt(system_prompt: str, user_message: str) -> str:
    """First-turn system-prompt inlining for hermes (which has no
    ``--system`` flag). Subsequent turns rely on ``--continue`` and
    skip this entirely."""
    if not system_prompt:
        return user_message
    return f"{system_prompt}\n\n---\n\n{user_message}"


def _parse_hermes_reply(stdout_text: str) -> tuple[str, str]:
    """Pull (reply, session_id) out of ``hermes chat --quiet`` stdout.
    Filter known banner / metadata lines and capture session_id from
    whichever marker emits it (may be absent on fresh sessions).
    """
    session_id = ""
    content: list[str] = []
    for line in stdout_text.splitlines():
        m = _HERMES_SESSION_ID_RE.match(line)
        if m:
            session_id = m.group(1)
            continue
        m = _HERMES_RESUMED_SESSION_RE.match(line)
        if m:
            session_id = session_id or m.group(1)
            continue
        if _HERMES_MODEL_NORMALISED_RE.match(line):
            continue
        if _HERMES_MODEL_NORMALISED_TAIL_RE.match(line):
            continue
        content.append(line)
    reply = "\n".join(content).strip()
    return reply, session_id


def _puffo_gemini_mcp_entry(
    *,
    puffo_core_mcp_env: dict[str, str] | None,
) -> dict | None:
    """Build gemini's ``mcpServers`` entry (command + args + env)
    for the puffo MCP server. ``None`` when puffo_core isn't
    configured.
    """
    if puffo_core_mcp_env is None:
        return None
    env = dict(puffo_core_mcp_env)
    env["PUFFO_CORE_KEYSTORE_DIR"] = "/home/agent/.puffo-agent-state/keys"
    # No PUFFO_CORE_DB_PATH — see mcp/data_client.py.
    env["PUFFO_WORKSPACE"] = "/workspace"
    env["PUFFO_RUNTIME_KIND"] = "cli-docker"
    env["PUFFO_HARNESS"] = "gemini-cli"
    env["PYTHONPATH"] = "/opt/puffoagent-pkg"
    return {
        "command": "python3",
        "args": ["-m", "puffo_agent.mcp.puffo_core_server"],
        "env": env,
    }


def _build_gemini_argv(
    *,
    container_name: str,
    api_key: str,
    model: str,
    has_prior_session: bool,
    user_message: str,
) -> list[str]:
    """Assemble the ``docker exec ... gemini ...`` argv for one turn.

    Uses ``--prompt=<value>`` (not ``-p <value>``) so yargs reads
    the whole prompt as a single token even when it starts with
    ``-`` (e.g. markdown list syntax in preambles).
    """
    cmd = [
        "docker", "exec", "-i",
        "-e", f"GEMINI_API_KEY={api_key}",
        container_name,
        "gemini",
    ]
    if model:
        cmd.extend(["--model", _gemini_model_id(model)])
    if has_prior_session:
        cmd.extend(["-r", "latest"])
    cmd.extend([
        "--output-format", "json",
        f"--prompt={user_message}",
    ])
    return cmd


def _gemini_model_id(model: str) -> str:
    """Translate ``runtime.model`` into the form ``gemini --model``
    expects. Strips Claude-style ``[1m]`` suffixes; empty → default.
    """
    base = (model or "").split("[", 1)[0].strip()
    if not base:
        return "gemini-2.5-pro"
    return base


def _parse_gemini_reply(stdout_text: str) -> tuple[str, str, str]:
    """Pull (reply, session_id, error) from ``gemini -p ...
    --output-format json`` stdout. Falls back to raw text when JSON
    parse fails (some upstream failure modes ignore the format
    flag). Returns an explicit error when stdout is gemini's --help
    banner instead of a reply (signals malformed argv).
    """
    stdout_text = stdout_text.strip()
    if not stdout_text:
        return "", "", ""
    try:
        obj = json.loads(stdout_text)
    except (json.JSONDecodeError, ValueError):
        if stdout_text.startswith("Usage: gemini"):
            return "", "", "gemini printed its --help banner instead of a reply; argv likely malformed"
        return stdout_text, "", ""
    if not isinstance(obj, dict):
        return stdout_text, "", ""
    reply = str(obj.get("response", "") or "")
    session_id = str(obj.get("session_id", "") or "")
    err_raw = obj.get("error")
    if isinstance(err_raw, dict):
        err = str(err_raw.get("message", "") or err_raw.get("type", "") or "unknown error")
    else:
        err = str(err_raw or "")
    return reply.strip(), session_id, err


async def _run_cmd(cmd: list[str], check: bool = True) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {stderr.decode('utf-8', errors='replace').strip()[:500]}"
        )
    return proc.returncode, stdout, stderr
