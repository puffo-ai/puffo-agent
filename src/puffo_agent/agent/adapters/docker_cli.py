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
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...mcp.config import (
    INFERENCE_LEVELS,
    write_cli_mcp_config,
)
from .._logging import safe_diagnostic_summary
from .hermes_helpers import (
    HERMES_NO_RESUME_SIGNATURE,
    hermes_model_id,
    parse_hermes_reply,
    stitch_hermes_prompt,
)
from ...portal.state import (
    seed_claude_home,
    sync_host_enabled_plugins,
    sync_host_gemini_mcp_servers,
    sync_host_gemini_skills,
    sync_host_mcp_servers,
    sync_host_skills,
)
from .base import Adapter, TurnContext, TurnResult
from ..context_controller import (
    ContextCapabilities,
    ContextSnapshot,
    ProviderAdmissionEvent,
    normalize_context_snapshot,
)
from .cli_session import AuditLog, ClaudeSession


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DockerCommandOutput:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float


async def _run_text_command(command: list[str]) -> _DockerCommandOutput:
    started = time.time()
    returncode, stdout, stderr = await _run_cmd(command, check=False)
    return _DockerCommandOutput(
        returncode=returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        elapsed=time.time() - started,
    )


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
    "__CLAUDE_CODE_VERSION__",
    CLAUDE_CODE_NPM_VERSION,
).replace(
    "__GEMINI_CLI_VERSION__",
    GEMINI_CLI_NPM_VERSION,
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
        inference_level: str = "",
        harness=None,
        google_api_key: str = "",
        memory_limit: str = "",
        memory_reservation: str = "",
        desired_skills: list[str] | None = None,
        puffo_core_server_url: str = "",
        puffo_core_slug: str = "",
        puffo_core_keys_dir: str = "",
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
        self.inference_level = inference_level
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
        # Installed into the bind-mounted .claude/skills/ on first
        # start (see _ensure_started). MCPs are rejected upstream.
        self.desired_skills = list(desired_skills or [])
        self.puffo_core_server_url = puffo_core_server_url
        self.puffo_core_slug = puffo_core_slug
        self.puffo_core_keys_dir = puffo_core_keys_dir
        self._desired_installed = False
        self._started_lock = asyncio.Lock()
        self._started = False
        self._session: ClaudeSession | None = None
        # Has the puffo MCP server been registered with the
        # in-container hermes config yet? Registration is idempotent
        # (remove + add) so a flag mismatch is safe. The gemini path
        # writes MCP config upfront via ``_ensure_started`` instead.
        self._hermes_mcp_registered = False
        self._one_shot_provider_session_id: str | None = None
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
                session_ref=ctx.session_ref,
                turn_ref=ctx.turn_ref,
                trusted_context_refs=ctx.trusted_context_refs,
            )
            return await self.run_turn(ctx_fallback)
        await self._ensure_started()
        session = self._ensure_session()
        return await session.run_retry_turn(
            kick_text,
            fallback_user_message,
            ctx.system_prompt,
        )

    async def _run_turn_hermes(
        self, user_message: str, system_prompt: str
    ) -> TurnResult:
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

        env_flags = self._hermes_mcp_env_flags()
        await _run_cmd(
            [
                "docker",
                "exec",
                self.container_name,
                "hermes",
                "mcp",
                "remove",
                "puffo",
            ],
            check=False,
        )
        await self._add_hermes_mcp(env_flags)

    def _hermes_mcp_env_flags(self) -> list[str]:
        assert self.puffo_core_mcp_env is not None
        env = dict(self.puffo_core_mcp_env)
        env.update(
            {
                "PUFFO_CORE_KEYSTORE_DIR": "/home/agent/.puffo-agent-state/keys",
                "PUFFO_WORKSPACE": "/workspace",
                "PUFFO_MEMORY_DIR": "/home/agent/.puffo-agent-state/memory",
                "PUFFO_RUNTIME_KIND": "cli-docker",
                "PUFFO_HARNESS": "hermes",
                "PYTHONPATH": "/opt/puffoagent-pkg",
            }
        )
        return [f"{key}={value}" for key, value in env.items()]

    async def _add_hermes_mcp(self, env_flags: list[str]) -> None:
        command = [
            "docker",
            "exec",
            "-i",
            self.container_name,
            "hermes",
            "mcp",
            "add",
            "puffo",
            "--command",
            "python3",
            "--args",
            "-m",
            "puffo_agent.mcp.puffo_core_server",
            "--env",
            *env_flags,
        ]
        try:
            from ..._proc import no_window_kwargs

            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **no_window_kwargs(),
            )
            stdout, stderr = await proc.communicate(b"y\n")
        except Exception as exc:
            logger.warning(
                "agent %s: couldn't register puffo MCP with hermes: %s "
                "(chat will work, tool calls won't)",
                self.agent_id,
                exc,
            )
            return
        if proc.returncode != 0:
            logger.warning(
                "agent %s: hermes mcp add puffo rc=%d | stdout: %s | stderr: %s "
                "(chat will work, tool calls won't)",
                self.agent_id,
                proc.returncode,
                safe_diagnostic_summary(stdout.decode("utf-8", errors="replace")),
                safe_diagnostic_summary(stderr.decode("utf-8", errors="replace")),
            )
            return
        logger.info(
            "agent %s: registered puffo MCP server with hermes "
            "(18 tools available via hermes chat)",
            self.agent_id,
        )
        self._hermes_mcp_registered = True

    async def _run_hermes_chat(
        self,
        user_message: str,
        system_prompt: str,
        *,
        _retried: bool = False,
    ) -> TurnResult:
        token = _read_claude_access_token()
        if not token:
            return self._missing_hermes_token_result()
        await self._ensure_hermes_mcp_registered()
        has_prior_session = self.session_file.exists()
        prompt = (
            user_message
            if has_prior_session
            else stitch_hermes_prompt(
                system_prompt,
                user_message,
            )
        )
        output = await _run_text_command(
            self._hermes_command(token, prompt, has_prior_session)
        )
        if (
            output.returncode != 0
            and HERMES_NO_RESUME_SIGNATURE in output.stdout
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
                user_message,
                system_prompt,
                _retried=True,
            )
        if output.returncode != 0:
            return self._failed_hermes_result(output)
        return await self._finish_hermes_turn(output, has_prior_session)

    def _missing_hermes_token_result(self) -> TurnResult:
        logger.error(
            "agent %s: cannot read Claude Code access token from "
            "%s — hermes turn would fail with no credentials. "
            "run `claude login` on the host to refresh.",
            self.agent_id,
            _HOST_CLAUDE_CREDENTIALS_PATH,
        )
        return TurnResult(
            reply="",
            metadata={"error": "no Claude Code access token available on host"},
        )

    def _hermes_command(
        self,
        token: str,
        prompt: str,
        has_prior_session: bool,
    ) -> list[str]:
        command = [
            "docker",
            "exec",
            "-i",
            "-e",
            f"ANTHROPIC_API_KEY={token}",
            self.container_name,
            "hermes",
            "chat",
            "--provider",
            "anthropic",
            "--quiet",
            "--source",
            f"puffoagent:{self.agent_id}",
            "--model",
            hermes_model_id(self.model),
        ]
        if has_prior_session:
            command.append("--continue")
        command.extend(["-q", prompt])
        return command

    def _failed_hermes_result(self, output: _DockerCommandOutput) -> TurnResult:
        logger.error(
            "agent %s: hermes turn rc=%d in %.1fs | stdout: %r | stderr: %s",
            self.agent_id,
            output.returncode,
            output.elapsed,
            safe_diagnostic_summary(output.stdout),
            safe_diagnostic_summary(output.stderr),
        )
        return TurnResult(
            reply="",
            metadata={
                "error": f"hermes exited rc={output.returncode}",
                "stdout_summary": safe_diagnostic_summary(output.stdout),
                "stderr_summary": safe_diagnostic_summary(output.stderr),
            },
        )

    async def _finish_hermes_turn(
        self,
        output: _DockerCommandOutput,
        has_prior_session: bool,
    ) -> TurnResult:
        reply, session_id, tool_calls = parse_hermes_reply(output.stdout)
        self._one_shot_provider_session_id = session_id or None
        if reply or session_id or tool_calls:
            await self._fire_admission_callback(
                ProviderAdmissionEvent(
                    planning_cycle_key=getattr(
                        self,
                        "_context_admission_planning_cycle_key",
                        "",
                    ),
                    provider_session_id=self.get_provider_session_id(),
                    admitted_at=datetime.now(timezone.utc),
                )
            )
        if tool_calls:
            logger.info(
                "agent %s: hermes turn invoked %d tool(s): %s",
                self.agent_id,
                len(tool_calls),
                ", ".join(tool_calls),
            )
        if not reply:
            logger.warning(
                "agent %s: hermes rc=0 but parser found no reply. stdout: %s",
                self.agent_id,
                safe_diagnostic_summary(output.stdout),
            )
        if not has_prior_session:
            self._write_one_shot_session("hermes", session_id)
        logger.info(
            "agent %s: hermes turn rc=0 in %.1fs, %d reply chars, "
            "session=%s, resume=%s",
            self.agent_id,
            output.elapsed,
            len(reply),
            session_id or "?",
            has_prior_session,
        )
        return TurnResult(
            reply="",
            tool_calls=len(tool_calls),
            metadata={
                "harness": "hermes",
                "session_id": session_id,
                "tools_invoked": tool_calls,
                "send_message_targets": [{"channel": "", "root_id": ""}],
                "hermes_assistant_text": reply,
            },
        )

    def _write_one_shot_session(self, harness: str, session_id: str) -> None:
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            self.session_file.write_text(
                json.dumps(
                    {
                        "harness": harness,
                        "session_id": session_id,
                        "first_turn_at": int(time.time()),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "agent %s: couldn't write %s session_file: %s "
                "(next turn will start a fresh session)",
                self.agent_id,
                harness.removesuffix("-cli"),
                exc,
            )

    # ── Gemini harness ────────────────────────────────────────────

    async def _run_turn_gemini(
        self,
        user_message: str,
        system_prompt: str,
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
        self,
        user_message: str,
        system_prompt: str,
        *,
        _retried: bool = False,
    ) -> TurnResult:
        if not self.google_api_key:
            return self._missing_gemini_key_result()
        has_prior_session = self.session_file.exists()
        cmd = _build_gemini_argv(
            container_name=self.container_name,
            api_key=self.google_api_key,
            model=self.model,
            has_prior_session=has_prior_session,
            user_message=user_message,
        )

        redacted = [
            "GEMINI_API_KEY=***" if a.startswith("GEMINI_API_KEY=") else a for a in cmd
        ]
        logger.info("agent %s: gemini argv: %s", self.agent_id, " ".join(redacted))
        output = await _run_text_command(cmd)
        if output.returncode != 0 and has_prior_session and not _retried:
            logger.info(
                "agent %s: gemini -r latest rc=%d; clearing sentinel "
                "and retrying with a fresh session. stderr: %s",
                self.agent_id,
                output.returncode,
                safe_diagnostic_summary(output.stderr),
            )
            try:
                self.session_file.unlink()
            except OSError:
                pass
            return await self._run_gemini_chat(
                user_message,
                system_prompt,
                _retried=True,
            )
        if output.returncode != 0:
            return self._failed_gemini_result(output)
        return await self._finish_gemini_turn(output, has_prior_session)

    def _missing_gemini_key_result(self) -> TurnResult:
        logger.error(
            "agent %s: gemini-cli turn requires a google api_key "
            "(passed as GEMINI_API_KEY into the container). Pass "
            "--api-key on `agent create`, set GEMINI_API_KEY in "
            "the environment, or run `puffo-agent config`.",
            self.agent_id,
        )
        return TurnResult(reply="", metadata={"error": "no google api_key configured"})

    def _failed_gemini_result(self, output: _DockerCommandOutput) -> TurnResult:
        logger.error(
            "agent %s: gemini turn rc=%d in %.1fs | stdout: %r | stderr: %s",
            self.agent_id,
            output.returncode,
            output.elapsed,
            safe_diagnostic_summary(output.stdout),
            safe_diagnostic_summary(output.stderr),
        )
        return TurnResult(
            reply="",
            metadata={
                "error": f"gemini exited rc={output.returncode}",
                "stdout_summary": safe_diagnostic_summary(output.stdout),
                "stderr_summary": safe_diagnostic_summary(output.stderr),
            },
        )

    async def _finish_gemini_turn(
        self,
        output: _DockerCommandOutput,
        has_prior_session: bool,
    ) -> TurnResult:
        reply, session_id, err = _parse_gemini_reply(output.stdout)
        self._one_shot_provider_session_id = session_id or None
        if reply or session_id or err:
            await self._fire_admission_callback(
                ProviderAdmissionEvent(
                    planning_cycle_key=getattr(
                        self,
                        "_context_admission_planning_cycle_key",
                        "",
                    ),
                    provider_session_id=self.get_provider_session_id(),
                    admitted_at=datetime.now(timezone.utc),
                )
            )
        if err:
            logger.warning(
                "agent %s: gemini rc=0 but returned JSON error: %s",
                self.agent_id,
                err,
            )
        if not reply:
            logger.warning(
                "agent %s: gemini rc=0 but parser found no reply. stdout: %s",
                self.agent_id,
                safe_diagnostic_summary(output.stdout),
            )
        if not has_prior_session:
            self._write_one_shot_session("gemini-cli", session_id)
        logger.info(
            "agent %s: gemini turn rc=0 in %.1fs, %d reply chars, "
            "session=%s, resume=%s%s",
            self.agent_id,
            output.elapsed,
            len(reply),
            session_id or "?",
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

    async def reload(
        self,
        new_system_prompt: str,
        *,
        with_session: bool = False,
    ) -> None:
        """Close the in-container claude subprocess so the next turn
        re-reads CLAUDE.md; container stays up. No-op for hermes.
        ``with_session=True`` also unlinks ``cli_session.json``."""
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if with_session:
            try:
                self.session_file.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "agent %s: couldn't unlink session file %s: %s",
                    self.agent_id,
                    self.session_file,
                    exc,
                )

    async def get_context_snapshot(self) -> ContextSnapshot:
        if self.harness.name() == "claude-code":
            return await self._ensure_session().get_context_snapshot()
        return normalize_context_snapshot(
            used_tokens=0,
            estimated_source=f"{self.harness.name()}_unsupported_fallback_200000",
        )

    def get_context_capabilities(self) -> ContextCapabilities:
        if self.harness.name() == "claude-code":
            return self._ensure_session().get_context_capabilities()
        return ContextCapabilities(
            diagnostic=f"one-shot {self.harness.name()} context control unsupported",
        )

    async def compact_context(self):
        if self.harness.name() == "claude-code":
            return await self._ensure_session().compact_context()
        return await super().compact_context()

    async def rollover_context(self):
        if self.harness.name() == "claude-code":
            return await self._ensure_session().rollover_context()
        return await super().rollover_context()

    def get_provider_session_id(self) -> str | None:
        if self.harness.name() == "claude-code":
            return self._ensure_session().get_provider_session_id()
        if self._one_shot_provider_session_id:
            return self._one_shot_provider_session_id
        try:
            persisted = json.loads(self.session_file.read_text(encoding="utf-8"))
            return str(persisted.get("session_id") or "") or None
        except (OSError, ValueError):
            return None

    def register_admission_callback(
        self,
        callback,
        planning_cycle_key: str = "",
    ) -> None:
        if self.harness.name() == "claude-code":
            self._ensure_session().register_admission_callback(
                callback,
                planning_cycle_key,
            )
        else:
            super().register_admission_callback(callback, planning_cycle_key)

    register_provider_admission_callback = register_admission_callback

    def register_continuation_callback(
        self,
        callback,
        planning_cycle_key: str = "",
        *,
        channel_id: str = "",
        tool_names: tuple[str, ...] = (),
        tool_arguments: dict[str, object] | None = None,
        correlation_receipt: str = "",
    ) -> None:
        if self.harness.name() == "claude-code":
            self._ensure_session().register_continuation_callback(
                callback,
                planning_cycle_key,
                channel_id=channel_id,
                tool_names=tool_names,
                tool_arguments=tool_arguments,
                correlation_receipt=correlation_receipt,
            )
        else:
            super().register_continuation_callback(
                callback,
                planning_cycle_key,
                channel_id=channel_id,
                tool_names=tool_names,
                tool_arguments=tool_arguments,
                correlation_receipt=correlation_receipt,
            )

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
            ["docker", "stop", "-t", "5", self.container_name],
            check=False,
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
            model=self.model,
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
        cmd.extend(
            [
                self.container_name,
                "claude",
                "--dangerously-skip-permissions",
            ]
        )
        if self.model:
            cmd.extend(["--model", self.model])
        if self.inference_level:
            if self.inference_level in INFERENCE_LEVELS:
                cmd.extend(["--effort", self.inference_level])
            else:
                logger.warning(
                    "agent %s: ignoring inference_level %r for claude-code "
                    "(expected one of %s)",
                    self.agent_id,
                    self.inference_level,
                    ", ".join(INFERENCE_LEVELS),
                )
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
            env["PUFFO_MEMORY_DIR"] = "/home/agent/.puffo-agent-state/memory"
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
            "send_message / list_channels_in_all_spaces / etc. show up under "
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
                "docker",
                "exec",
                self.container_name,
                "test",
                "-f",
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
                "docker",
                "inspect",
                "-f",
                "{{.State.Status}}",
                self.container_name,
            ],
            check=False,
        )
        if rc != 0:
            return ""
        return out.decode("utf-8", errors="replace").strip()

    async def _install_desired_skills(self) -> None:
        """Install desired skills into .claude/skills/, once per
        instance. MCPs are gated out upstream, so skills only."""
        if self._desired_installed or not self.desired_skills:
            return
        self._desired_installed = True
        from .desired_install import run_spawn_install

        await run_spawn_install(
            agent_id=self.agent_id,
            agent_home=self.agent_home_dir,
            workspace_dir=Path(self.workspace_dir),
            harness_name=self.harness.name(),
            desired_skills=self.desired_skills,
            desired_mcps=[],
            server_url=self.puffo_core_server_url,
            slug=self.puffo_core_slug,
            keys_dir=self.puffo_core_keys_dir,
        )

    async def _ensure_started(self) -> None:
        async with self._started_lock:
            if self._started:
                return
            self._require_docker()
            host_home = Path.home()
            await self._sync_claude_host_assets(host_home)
            self._sync_gemini_host_assets(host_home)
            self._warn_missing_claude_credentials(host_home)
            existed = await self._start_or_resume_container()
            await self._recreate_if_mount_stale(existed)
            self._started = True

    @staticmethod
    def _require_docker() -> None:
        if shutil.which("docker") is None:
            raise RuntimeError(
                "docker binary not found on PATH. install Docker Desktop "
                "(Windows/macOS) or docker-ce (Linux) to use runtime "
                "kind 'cli-docker'."
            )

    async def _sync_claude_host_assets(self, host_home: Path) -> None:
        if seed_claude_home(host_home, self.agent_home_dir):
            logger.info(
                "agent %s: seeded per-agent virtual $HOME at %s from %s",
                self.agent_id,
                self.agent_home_dir,
                host_home,
            )
        skill_count = sync_host_skills(host_home, self.agent_home_dir)
        if skill_count:
            logger.info(
                "agent %s: synced %d host skill(s) into %s",
                self.agent_id,
                skill_count,
                self.agent_home_dir / ".claude" / "skills",
            )
        await self._install_desired_skills()
        merged_mcp, unreachable = sync_host_mcp_servers(host_home, self.agent_home_dir)
        if merged_mcp:
            logger.info(
                "agent %s: merged %d host MCP server registration(s) "
                "into per-agent .claude.json",
                self.agent_id,
                merged_mcp,
            )
        for name, command in unreachable:
            logger.warning(
                "agent %s: host MCP %r has host-local path %r that won't "
                "resolve inside the container — SKIPPED (not injected). "
                "Install the binary in the image or bind-mount it, then "
                "re-sync, to make this MCP available.",
                self.agent_id,
                name,
                command,
            )
        enabled_count = sync_host_enabled_plugins(host_home, self.agent_home_dir)
        if enabled_count:
            logger.info(
                "agent %s: propagated %d enabledPlugins entry/entries "
                "from host settings.json",
                self.agent_id,
                enabled_count,
            )

    def _sync_gemini_host_assets(self, host_home: Path) -> None:
        project_dir = Path(self.workspace_dir)
        skill_count = sync_host_gemini_skills(host_home, project_dir)
        if skill_count:
            logger.info(
                "agent %s: synced %d host gemini skill(s) into %s",
                self.agent_id,
                skill_count,
                project_dir / ".gemini" / "skills",
            )
        puffo_entry = _puffo_gemini_mcp_entry(
            puffo_core_mcp_env=self.puffo_core_mcp_env,
        )
        merged, unreachable = sync_host_gemini_mcp_servers(
            host_home,
            project_dir,
            extra_servers={"puffo": puffo_entry} if puffo_entry else None,
        )
        if merged:
            logger.info(
                "agent %s: merged %d host gemini MCP server registration(s) "
                "into .gemini/settings.json",
                self.agent_id,
                merged,
            )
        for name, command in unreachable:
            logger.warning(
                "agent %s: host gemini MCP %r has host-local path %r "
                "that won't resolve inside the container — SKIPPED. "
                "Install the binary in the image or bind-mount it, then "
                "re-sync, to make this MCP available.",
                self.agent_id,
                name,
                command,
            )

    def _warn_missing_claude_credentials(self, host_home: Path) -> None:
        credentials = host_home / ".claude" / ".credentials.json"
        if not credentials.exists():
            logger.warning(
                "agent %s: host has no %s — run `claude login` on the "
                "host, then restart the agent. First turn will fail "
                "with an auth error otherwise.",
                self.agent_id,
                credentials,
            )

    async def _start_or_resume_container(self) -> bool:
        state = await self._container_state()
        if state == "running":
            logger.info(
                "agent %s: reusing running container %r",
                self.agent_id,
                self.container_name,
            )
        elif state in ("exited", "created", "dead"):
            logger.info(
                "agent %s: starting existing container %r (was %s)",
                self.agent_id,
                self.container_name,
                state,
            )
            await _run_cmd(["docker", "start", self.container_name])
        elif state == "paused":
            logger.info(
                "agent %s: unpausing container %r",
                self.agent_id,
                self.container_name,
            )
            await _run_cmd(["docker", "unpause", self.container_name])
        else:
            await self._ensure_image()
            await self._start_container()
        return state != ""

    async def _recreate_if_mount_stale(self, existed: bool) -> None:
        if not existed or await self._puffo_pkg_mount_is_current():
            return
        logger.warning(
            "agent %s: container %r has a stale /opt/puffoagent-pkg bind "
            "mount (the host path it was created with no longer contains "
            "puffo_agent). Recreating so claude-code's MCP subprocess can "
            "import the package again — typical cause is a pip reinstall "
            "from a different path.",
            self.agent_id,
            self.container_name,
        )
        await _run_cmd(["docker", "rm", "-f", self.container_name], check=False)
        await self._ensure_image()
        await self._start_container()

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
                    self.agent_id,
                    self.image,
                )
                return
            logger.info(
                "agent %s: building docker image %s (first use — this may take a few minutes)",
                self.agent_id,
                self.image,
            )
            await self._build_image()

    async def _build_image(self) -> None:
        from ..._proc import no_window_kwargs

        proc = await asyncio.create_subprocess_exec(
            "docker",
            "build",
            "-t",
            self.image,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **no_window_kwargs(),
        )
        stdout, _ = await proc.communicate(DOCKERFILE.encode())
        if proc.returncode != 0:
            tail = stdout.decode("utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"docker build failed:\n{tail}")
        logger.info("agent %s: docker image %s built", self.agent_id, self.image)

    async def _start_container(self) -> None:
        host_credentials, agent_claude_json = self._prepare_container_mounts()
        command = self._container_run_command(host_credentials, agent_claude_json)
        self._add_optional_container_args(command)
        command.append(self.image)
        rc, _, stderr = await _run_cmd(command, check=False)
        if rc != 0:
            raise RuntimeError(
                f"docker run failed for {self.container_name}: "
                f"{stderr.decode('utf-8', errors='replace').strip()[:500]}"
            )

    def _prepare_container_mounts(self) -> tuple[Path, Path]:
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
        self.agent_home_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_home_dir / ".claude").mkdir(parents=True, exist_ok=True)
        host_credentials = Path.home() / ".claude" / ".credentials.json"
        if not host_credentials.exists():
            host_credentials.parent.mkdir(parents=True, exist_ok=True)
            host_credentials.touch()
        agent_claude_json = self.agent_home_dir / ".claude.json"
        agent_claude_json.touch(exist_ok=True)
        (self.agent_home_dir / ".gemini").mkdir(parents=True, exist_ok=True)
        self.shared_fs_dir.mkdir(parents=True, exist_ok=True)
        return host_credentials, agent_claude_json

    def _container_run_command(
        self,
        host_credentials: Path,
        agent_claude_json: Path,
    ) -> list[str]:
        return [
            "docker",
            "run",
            "-d",
            "--name",
            self.container_name,
            "-e",
            f"PUFFO_AGENT_ID={self.agent_id}",
            "-v",
            f"{self.workspace_dir}:/workspace",
            "-v",
            f"{self.claude_home_src}:/home/agent/.claude",
            # .credentials.json mount MUST come after the .claude dir
            # mount for Docker to treat it as a file overlay rather
            # than a no-op.
            "-v",
            f"{host_credentials}:/home/agent/.claude/.credentials.json",
            # Sibling .claude.json — without this it lands on the
            # container's ephemeral fs and is lost on restart.
            "-v",
            f"{agent_claude_json}:/home/agent/.claude.json",
            # Always mounted (regardless of harness) so swapping to
            # gemini-cli doesn't need a rebuild.
            "-v",
            f"{self.agent_home_dir / '.gemini'}:/home/agent/.gemini",
            "-v",
            f"{self.shared_fs_dir}:/workspace/.shared",
            "-v",
            f"{_puffo_agent_pkg_dir()}:/opt/puffoagent-pkg:ro",
            # RW because subkey rotation rewrites <slug>.session.json.
            # Mounting :ro surfaced as [Errno 30] from MCP tool calls
            # past the subkey TTL. Whole agent_home_dir is mounted
            # rather than individual files because SQLite WAL files
            # (-wal, -shm) sit alongside the .db.
            "-v",
            f"{self.agent_home_dir}:/home/agent/.puffo-agent-state",
            "--init",
        ]

    def _add_optional_container_args(self, command: list[str]) -> None:
        host_plugins = Path.home() / ".claude" / "plugins"
        if host_plugins.is_dir():
            command.extend(
                [
                    "-v",
                    f"{host_plugins}:/home/agent/.claude/plugins:ro",
                ]
            )
        if self.memory_limit:
            command.extend(["--memory", self.memory_limit])
        if self.memory_reservation:
            command.extend(["--memory-reservation", self.memory_reservation])


# Serialises concurrent ``docker build -t <tag>`` across workers
# (right after an image-tag bump every cli-docker worker would
# otherwise race BuildKit's exporter).
_BUILD_LOCK = asyncio.Lock()


async def _image_exists_locally(tag: str) -> bool:
    rc, _, _ = await _run_cmd(
        ["docker", "image", "inspect", tag],
        check=False,
    )
    return rc == 0


# Host-side Claude Code credentials path. Read on every hermes turn
# because hermes' own auto-discovery is unreliable inside the
# container even with the credentials file bind-mounted in.
_HOST_CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


def _read_claude_access_token() -> str:
    """Current Claude Code OAuth access token from the host's
    credentials file. Empty string on any failure (missing file,
    malformed JSON, missing key) — caller logs and surfaces a turn-
    level error rather than crashing the worker.
    """
    try:
        data = json.loads(_HOST_CLAUDE_CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return ((data.get("claudeAiOauth") or {}).get("accessToken") or "").strip()


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
    env["PUFFO_MEMORY_DIR"] = "/home/agent/.puffo-agent-state/memory"
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
        "docker",
        "exec",
        "-i",
        "-e",
        f"GEMINI_API_KEY={api_key}",
        container_name,
        "gemini",
    ]
    if model:
        cmd.extend(["--model", _gemini_model_id(model)])
    if has_prior_session:
        cmd.extend(["-r", "latest"])
    cmd.extend(
        [
            "--output-format",
            "json",
            f"--prompt={user_message}",
        ]
    )
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
            return (
                "",
                "",
                "gemini printed its --help banner instead of a reply; argv likely malformed",
            )
        return stdout_text, "", ""
    if not isinstance(obj, dict):
        return stdout_text, "", ""
    reply = str(obj.get("response", "") or "")
    session_id = str(obj.get("session_id", "") or "")
    err_raw = obj.get("error")
    if isinstance(err_raw, dict):
        err = str(
            err_raw.get("message", "") or err_raw.get("type", "") or "unknown error"
        )
    else:
        err = str(err_raw or "")
    return reply.strip(), session_id, err


async def _run_cmd(cmd: list[str], check: bool = True) -> tuple[int, bytes, bytes]:
    from ..._proc import no_window_kwargs

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **no_window_kwargs(),
    )
    stdout, stderr = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {stderr.decode('utf-8', errors='replace').strip()[:500]}"
        )
    return proc.returncode, stdout, stderr
