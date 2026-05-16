"""Local CLI adapter — host-level execution, no sandbox.

Spawns a long-lived ``claude`` subprocess on the host with stream-json
I/O. The agent has the same filesystem and network access as the user
running the daemon. Auth comes from ``~/.claude/.credentials.json``
(operator runs ``claude login`` once); session id is persisted to
``cli_session.json`` so daemon restarts re-spawn with
``--resume <id>``.

Use ``cli-docker`` instead for isolation. A loud WARNING is logged on
first turn so operators see the host-level access posture even if
they skipped the README.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from ...mcp.config import (
    default_python_executable,
    write_cli_mcp_config,
    write_codex_mcp_config,
)
from ...portal.state import (
    agent_codex_user_dir,
    link_host_codex_auth,
    link_host_credentials,
    seed_claude_home,
    sync_host_enabled_plugins,
    sync_host_mcp_servers,
    sync_host_plugins,
    sync_host_skills,
)
from .base import Adapter, TurnContext, TurnResult, looks_like_auth_failure
from .cli_session import AuditLog, ClaudeSession
from .codex_session import CodexSession

logger = logging.getLogger(__name__)


# See docker_cli for rationale.
REFRESH_ONESHOT_TIMEOUT_SECONDS = 120

# How long the permission proxy hook waits for an owner reply before
# denying. Exposed to the hook via PUFFO_PERMISSION_TIMEOUT.
PERMISSION_HOOK_TIMEOUT_SECONDS = 300

# Tools the PreToolUse hook intercepts in ``default`` mode. Reads
# (Read/Glob/Grep) and MCP tools deliberately pass through unsurveyed:
# reads auto-approve, and MCP tools are the agent's talking-to-the-
# user path so per-call DMs would be self-referential.
PERMISSION_HOOK_FULL_MATCHER = "Bash|Edit|Write|MultiEdit|NotebookEdit|WebFetch|WebSearch"

# ``acceptEdits`` mode: file-edit tools auto-approve, so the hook
# only proxies shell + network. Without this narrowing, setting
# acceptEdits would still DM on every Edit/Write.
PERMISSION_HOOK_NON_EDIT_MATCHER = "Bash|WebFetch|WebSearch"

# Marker we look for to identify hook entries this adapter wrote
# previously. Matching on the module path (not the matcher string)
# means mode switches don't leak stale full-matcher entries.
_HOOK_COMMAND_MARKER = "puffo_agent.hooks.permission"


# Claude Code accepts five permission modes; only ``bypassPermissions``
# currently ships working here — the others depend on a permission-
# proxy DM flow that still needs work. Anything else falls back to
# ``bypassPermissions`` with a WARNING.
VALID_PERMISSION_MODES = frozenset({
    "bypassPermissions",
})


def _is_puffo_agent_hook_entry(entry: object) -> bool:
    """True if ``entry`` is a PreToolUse hook this adapter wrote
    (identified by the ``_HOOK_COMMAND_MARKER`` in its command).
    """
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks") or []
    return any(
        isinstance(h, dict) and _HOOK_COMMAND_MARKER in (h.get("command") or "")
        for h in hooks
    )


def _sanitise_permission_mode(mode: str, agent_id: str) -> str:
    """Validate ``mode`` against the supported set; unsupported
    values fall back to ``bypassPermissions`` with a WARNING so a
    bad config doesn't look silently honoured.
    """
    if mode in VALID_PERMISSION_MODES:
        return mode
    if mode:
        logger.warning(
            "agent %s: permission_mode %r is not yet supported — "
            "falling back to 'bypassPermissions'. supported: %s",
            agent_id, mode, sorted(VALID_PERMISSION_MODES),
        )
    return "bypassPermissions"


class LocalCLIAdapter(Adapter):
    def __init__(
        self,
        agent_id: str,
        model: str,
        workspace_dir: str,
        claude_dir: str,
        session_file: str,
        mcp_config_file: str,
        agent_home_dir: str,
        owner_username: str = "",
        permission_mode: str = "default",
        harness=None,
    ):
        self.agent_id = agent_id
        self.model = model
        self.workspace_dir = workspace_dir
        self.claude_dir = claude_dir
        self.session_file = Path(session_file)
        self.mcp_config_file = Path(mcp_config_file)
        # Per-agent virtual $HOME. The claude subprocess's HOME /
        # USERPROFILE point here so its ~/.claude resolves to the
        # agent's isolated dir.
        self.agent_home_dir = Path(agent_home_dir)
        self.owner_username = owner_username
        self.permission_mode = _sanitise_permission_mode(permission_mode, agent_id)
        if harness is None:
            from ..harness import ClaudeCodeHarness
            harness = ClaudeCodeHarness()
        # cli-local supports claude-code (default) and codex (alpha).
        # hermes / gemini-cli remain cli-docker-only — replicating
        # their containerised setup on the operator's own host needs
        # its own design pass.
        if harness.name() in ("hermes", "gemini-cli"):
            raise RuntimeError(
                f"agent {agent_id!r}: runtime.harness={harness.name()!r} is "
                "not supported with runtime.kind=cli-local yet. Use "
                "runtime.kind=cli-docker, or switch runtime.harness "
                "back to claude-code."
            )
        self.harness = harness
        self.puffo_core_mcp_env: dict[str, str] | None = None
        # Caller injects the OpenAI key (from agent.yml or env) into
        # this field; codex reads it as ``OPENAI_API_KEY`` env. Unused
        # for the claude-code path.
        self.openai_api_key: str = ""
        self._verified = False
        # claude-code path uses ClaudeSession (long-lived stream-json);
        # codex path uses CodexSession (long-lived JSON-RPC). Only one
        # is non-None at a time — selected by ``harness.name()``.
        self._session: ClaudeSession | None = None
        self._codex_session: CodexSession | None = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        self._verify()
        user_message = ctx.messages[-1]["content"] if ctx.messages else ""
        if self.harness.name() == "codex":
            session = self._ensure_codex_session()
            return await session.run_turn(user_message, ctx.system_prompt)
        session = self._ensure_session()
        return await session.run_turn(user_message, ctx.system_prompt)

    async def run_retry_turn(
        self,
        kick_text: str,
        fallback_user_message: str,
        ctx: TurnContext,
    ) -> TurnResult:
        self._verify()
        if self.harness.name() == "codex":
            # codex has no equivalent of claude-code's cheap "resume
            # kick" — the App Server doesn't expose a resume-with-
            # tickle handle in v1. Re-send the full payload, same as
            # a fresh turn would.
            session = self._ensure_codex_session()
            return await session.run_turn(
                fallback_user_message, ctx.system_prompt,
            )
        session = self._ensure_session()
        return await session.run_retry_turn(
            kick_text, fallback_user_message, ctx.system_prompt,
        )

    async def warm(self, system_prompt: str) -> None:
        """Spawn the runtime subprocess eagerly when this agent has a
        persisted session; fresh agents wait for their first message
        to avoid paying for permanently-idle bots.
        """
        self._verify()
        if self.harness.name() == "codex":
            session = self._ensure_codex_session()
            if not session.has_persisted_session():
                logger.info(
                    "agent %s: no persisted codex conversation; deferring "
                    "spawn until first message", self.agent_id,
                )
                return
            await session.warm(system_prompt)
            return
        session = self._ensure_session()
        if not session.has_persisted_session():
            logger.info(
                "agent %s: no persisted session; deferring spawn until first message",
                self.agent_id,
            )
            return
        await session.warm(system_prompt)

    async def reload(self, new_system_prompt: str) -> None:
        """Drop cached runtime state so the next turn re-reads the
        on-disk instructions (CLAUDE.md for claude-code, AGENTS.md for
        codex). For claude-code we close the long-lived subprocess;
        for codex we update the in-memory ``current_instructions``
        which the next ``sendUserTurn`` carries through (and the
        rewritten AGENTS.md catches new conversations on resume).
        """
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if self._codex_session is not None:
            await self._codex_session.reload(new_system_prompt)

    def _credentials_expires_in_seconds(self) -> int | None:
        # Codex auth is a static OPENAI_API_KEY env var — no expiry,
        # no rotation, so we always report "fresh" by returning None
        # (the base refresh_ping short-circuits on None).
        if self.harness.name() == "codex":
            return None
        # All cli-local agents share the HOST's .credentials.json
        # (symlink where permitted, periodic copy elsewhere). Parse
        # ``expiresAt`` directly — mtime only advances on rewrite,
        # not while the token is still valid. The link call here
        # doubles as the periodic re-sync for copy-mode agents.
        link_host_credentials(Path.home(), self.agent_home_dir)
        host_credentials = Path.home() / ".claude" / ".credentials.json"
        try:
            data = json.loads(host_credentials.read_text(encoding="utf-8"))
            expires_ms = int(data["claudeAiOauth"]["expiresAt"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return int(expires_ms / 1000 - time.time())

    async def _run_refresh_oneshot(self) -> None:
        """Spawn ``claude --print ...`` with the per-agent HOME env.
        Same rationale as DockerCLIAdapter: only a process exit
        flushes the refreshed token to disk.

        codex: OPENAI_API_KEY is static, so refresh is a no-op.
        """
        if self.harness.name() == "codex":
            return
        self._verify()
        env = {
            **os.environ,
            "HOME": str(self.agent_home_dir),
            "USERPROFILE": str(self.agent_home_dir),
        }
        # --dangerously-skip-permissions is required: in --print mode
        # claude can't surface permission prompts, so without bypass
        # it exits before the API call and no refresh happens.
        cmd = [
            "claude", "--dangerously-skip-permissions",
            "--print", "--max-turns", "1",
            "--output-format", "stream-json", "--verbose",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.append("ok")
        started_at = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.workspace_dir,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "agent %s: refresh one-shot timed out after %ds",
                self.agent_id, REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
            return
        except FileNotFoundError:
            logger.warning(
                "agent %s: refresh one-shot: claude binary missing",
                self.agent_id,
            )
            return
        elapsed = time.time() - started_at
        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        # Doubles as an inference smoke test (auth status can
        # report OK while every API call returns 401). The worker
        # reads auth_healthy to suppress noisy replies while the
        # operator re-auths.
        if looks_like_auth_failure(out_text, err_text):
            logger.error(
                "agent %s: refresh one-shot hit an auth failure "
                "(rc=%d in %.1fs). operator re-auth likely required. "
                "stdout: %s | stderr: %s",
                self.agent_id, proc.returncode, elapsed,
                out_text.strip()[-400:], err_text.strip()[-400:],
            )
            self.auth_healthy = False
        elif proc.returncode != 0:
            logger.warning(
                "agent %s: refresh one-shot rc=%d in %.1fs | "
                "stdout: %s | stderr: %s",
                self.agent_id, proc.returncode, elapsed,
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
        if self._codex_session is not None:
            await self._codex_session.aclose()
            self._codex_session = None

    def _ensure_codex_session(self) -> CodexSession:
        if self._codex_session is not None:
            return self._codex_session

        codex_home = agent_codex_user_dir(self.agent_id)
        codex_home.mkdir(parents=True, exist_ok=True)
        # AGENTS.md investment goes here so codex picks it up on
        # ``newConversation``; the file body itself is written by
        # ``profile_sync.rebuild_agent_codex_md`` (worker startup +
        # reload_system_prompt). Writing the dir is just to make sure
        # codex has a HOME to read from.
        agents_md = codex_home / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text("", encoding="utf-8")

        # Per-server env injection for the puffo_core MCP server runs
        # through ``write_codex_mcp_config`` (TOML, not JSON).
        if self.puffo_core_mcp_env:
            write_codex_mcp_config(
                codex_home / "config.toml",
                command=default_python_executable(),
                args=["-m", "puffo_agent.mcp.puffo_core_server"],
                env=self.puffo_core_mcp_env,
            )
        else:
            logger.warning(
                "agent %s: codex MCP tools unavailable — puffo_core is "
                "not configured. populate `puffo_core:` in agent.yml to "
                "enable send_message / list_channels / etc.",
                self.agent_id,
            )

        env = {
            **os.environ,
            # Per-agent isolation for codex sessions/config/instructions.
            # Plan §2.D — Phase 0 #8 verifies the App Server actually
            # honours this.
            "CODEX_HOME": str(codex_home),
        }
        if self.openai_api_key:
            env["OPENAI_API_KEY"] = self.openai_api_key
        else:
            # OAuth fallback: share the operator's ``~/.codex/auth.json``
            # into this agent's $CODEX_HOME so codex picks up the
            # ``codex login`` session. Symlink-preferred (refresh rotations
            # propagate instantly across agents); copy fallback on
            # Windows non-dev-mode. Fail loud with a clear message if
            # neither auth path exists — there's no way the agent can
            # talk to OpenAI without one or the other.
            auth_mode = link_host_codex_auth(Path.home(), codex_home)
            if auth_mode == "no-host-file":
                raise RuntimeError(
                    f"agent {self.agent_id!r}: codex needs auth — either "
                    "set runtime.api_key in agent.yml / openai.api_key in "
                    "daemon.yml / export OPENAI_API_KEY, OR run "
                    "`codex login` in your own shell so ~/.codex/auth.json "
                    "exists."
                )
            logger.info(
                "agent %s: shared host codex auth (%s)",
                self.agent_id, auth_mode,
            )
        # Subprocess argv — ``codex app-server`` is the documented entry
        # point for embedding codex as a long-running agent. Resolve
        # the binary via shutil.which so npm-installed shims like
        # ``codex.cmd`` (Windows) work: asyncio.create_subprocess_exec
        # goes through CreateProcess and does NOT honour PATHEXT, so
        # the bare name ``codex`` fails to find ``codex.cmd``.
        codex_bin = shutil.which("codex") or "codex"
        argv = [codex_bin, "app-server"]

        self._codex_session = CodexSession(
            agent_id=self.agent_id,
            session_file=codex_home / "codex_session.json",
            argv=argv,
            cwd=self.workspace_dir,
            env=env,
            permission_mode=self.permission_mode,
        )
        return self._codex_session

    def _ensure_session(self) -> ClaudeSession:
        if self._session is not None:
            return self._session
        extra = self._prepare_mcp_args()
        # Register the PreToolUse permission hook before spawning;
        # settings.json is read fresh every spawn so this is
        # idempotent on every worker restart.
        self._write_permission_hook_settings()
        # Both HOME (POSIX) and USERPROFILE (Node on Windows) are
        # needed: Claude Code uses Node's os.homedir(). PUFFO_* are
        # consumed by the per-tool-call hook subprocess.
        env = {
            **os.environ,
            "HOME": str(self.agent_home_dir),
            "USERPROFILE": str(self.agent_home_dir),
            **self._permission_hook_env(),
        }
        self._session = ClaudeSession(
            agent_id=self.agent_id,
            session_file=self.session_file,
            build_command=self._build_command,
            cwd=self.workspace_dir,
            env=env,
            audit=AuditLog(
                Path(self.workspace_dir) / ".puffo-agent" / "audit.log",
                self.agent_id,
            ),
            extra_args=extra,
        )
        return self._session

    def _permission_hook_env(self) -> dict[str, str]:
        """Env vars the PreToolUse hook script reads. Claude inherits
        the parent's env and passes it to hook subprocesses, so
        setting them on the claude spawn reaches the hook.
        """
        env: dict[str, str] = {
            "PUFFO_OPERATOR_USERNAME": self.owner_username,
            "PUFFO_AGENT_ID": self.agent_id,
            "PUFFO_PERMISSION_TIMEOUT": str(PERMISSION_HOOK_TIMEOUT_SECONDS),
        }
        if self.puffo_core_mcp_env is not None:
            env.update(self.puffo_core_mcp_env)
        return env

    def _hook_matcher_for_mode(self) -> str | None:
        """Return the PreToolUse hook matcher for this agent's mode,
        or ``None`` if the mode opts out of proxying.

        Claude Code runs PreToolUse hooks regardless of
        ``--permission-mode``, so we must vary the matcher (not just
        the flag) for the mode setting to actually take effect:
          - default        → full matcher
          - acceptEdits    → shell + network only
          - auto/dontAsk/bypassPermissions → no hook
        """
        mode = self.permission_mode
        if mode == "default":
            return PERMISSION_HOOK_FULL_MATCHER
        if mode == "acceptEdits":
            return PERMISSION_HOOK_NON_EDIT_MATCHER
        return None

    def _write_permission_hook_settings(self) -> None:
        """Reconcile the project-level ``settings.json`` so the
        puffo PreToolUse hook matches this agent's
        ``permission_mode``. Non-puffo hooks are preserved. Hook
        runs under ``default_python_executable()`` so it shares the
        interpreter that has puffoagent installed.
        """
        settings_path = Path(self.claude_dir) / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        # Merge into existing content so hand-edits and agent-added
        # hooks survive.
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (FileNotFoundError, ValueError, OSError):
            existing = {}

        hooks_cfg = existing.get("hooks") or {}
        pretool = hooks_cfg.get("PreToolUse") or []
        # Drop previous puffo entries by command signature so we
        # catch them across matcher changes.
        pretool = [
            entry for entry in pretool
            if not _is_puffo_agent_hook_entry(entry)
        ]

        matcher = self._hook_matcher_for_mode()
        if matcher is not None:
            pretool.append({
                "matcher": matcher,
                "hooks": [{
                    "type": "command",
                    "command": (
                        f'"{default_python_executable()}" '
                        f"-m puffo_agent.hooks.permission"
                    ),
                    "timeout": PERMISSION_HOOK_TIMEOUT_SECONDS + 60,
                }],
            })

        if pretool:
            hooks_cfg["PreToolUse"] = pretool
        elif "PreToolUse" in hooks_cfg:
            del hooks_cfg["PreToolUse"]
        if hooks_cfg:
            existing["hooks"] = hooks_cfg
        elif "hooks" in existing:
            del existing["hooks"]

        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        tmp.replace(settings_path)

    def _build_command(
        self,
        extra_args: list[str],
        env_overrides: dict[str, str] | None = None,
    ) -> list[str]:
        # ``env_overrides`` is merged into the subprocess env on the
        # host by ClaudeSession._spawn; the kwarg here is just for
        # symmetry with the docker adapter.
        del env_overrides
        cmd = ["claude"]
        # ``--dangerously-skip-permissions`` bypasses BOTH the per-tool
        # approval prompt AND the per-project trust dialog. Claude
        # code's stream-json mode has no UI surface to accept the
        # trust dialog, so anything short of this flag leaves the
        # cwd un-trusted, which silently drops MCP servers supplied
        # via ``--mcp-config`` — exactly the "agent can't see the
        # puffo MCP" symptom on cli-local. ``--permission-mode
        # bypassPermissions`` (the previous flag here) only handles
        # the per-tool prompt path, not the trust dialog, so the MCP
        # was getting dropped before its tools could even register.
        # When the bypass mode is anything other than
        # ``bypassPermissions`` (e.g. the future ``default`` /
        # ``acceptEdits`` modes that route through the PreToolUse
        # hook), fall back to ``--permission-mode <mode>`` so the
        # hook controls each tool category — those modes implicitly
        # require an operator-supervised setup where the trust
        # dialog has already been accepted in a real claude session.
        if self.permission_mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd.extend(["--permission-mode", self.permission_mode])
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.extend(extra_args)
        return cmd

    def _prepare_mcp_args(self) -> list[str]:
        """Write per-agent MCP config and return the claude-CLI flag
        registering it. Permission proxying lives in a PreToolUse
        hook (not the MCP ``--permission-prompt-tool`` flag, which is
        documented as non-interactive-mode-only and silently ignored
        in the stream-json mode cli-local uses).
        """
        if self.puffo_core_mcp_env:
            write_cli_mcp_config(
                self.mcp_config_file,
                command=default_python_executable(),
                args=["-m", "puffo_agent.mcp.puffo_core_server"],
                env=self.puffo_core_mcp_env,
            )
            return ["--mcp-config", str(self.mcp_config_file)]
        logger.warning(
            "agent %s: cli-local MCP tools unavailable — puffo_core is "
            "not configured. populate `puffo_core:` in agent.yml to "
            "enable send_message / list_channels / etc.",
            self.agent_id,
        )
        return []

    def _verify(self) -> None:
        if self._verified:
            return
        if self.harness.name() == "codex":
            # codex has its own binary check (see CodexSession._spawn).
            # We deliberately skip the claude-seed / link-credentials
            # bookkeeping below — none of it applies to codex, and
            # touching ~/.claude/* for a codex agent would be confusing.
            self._verified = True
            return
        if shutil.which("claude") is None:
            raise RuntimeError(
                "claude binary not found on PATH. install the Claude Code CLI "
                "(`npm install -g @anthropic-ai/claude-code`) to use runtime "
                "kind 'cli-local'."
            )
        # Seed the per-agent virtual $HOME on first use (settings,
        # .claude.json). Credentials are handled separately via
        # link_host_credentials so every agent tracks the operator's
        # live OAuth state.
        host_home = Path.home()
        self.agent_home_dir.mkdir(parents=True, exist_ok=True)
        seeded = seed_claude_home(host_home, self.agent_home_dir)
        if seeded:
            logger.info(
                "agent %s: seeded per-agent virtual $HOME at %s from %s",
                self.agent_id, self.agent_home_dir, host_home,
            )
        # Symlink the agent's .credentials.json to the host's so
        # every refresh is visible across agents. Falls back to copy
        # on systems where symlinks aren't permitted; the copy is
        # re-synced on every refresh_ping tick.
        mode = link_host_credentials(host_home, self.agent_home_dir)
        logger.info(
            "agent %s: shared host credentials (%s)",
            self.agent_id, mode,
        )
        # One-way sync of host skills + MCP registrations. Runs
        # every start so host edits propagate. The unreachable-
        # command list is ignored: cli-local runs on the host, so
        # absolute host paths in MCP commands resolve naturally.
        skill_count = sync_host_skills(host_home, self.agent_home_dir)
        if skill_count:
            logger.info(
                "agent %s: synced %d host skill(s) into %s",
                self.agent_id, skill_count,
                self.agent_home_dir / ".claude" / "skills",
            )
        merged_mcp, _ = sync_host_mcp_servers(host_home, self.agent_home_dir)
        if merged_mcp:
            logger.info(
                "agent %s: merged %d host MCP server registration(s) "
                "into per-agent .claude.json", self.agent_id, merged_mcp,
            )
        # Plugins layer — pairs the actual plugin code tree with the
        # ``enabledPlugins`` array Claude reads from settings.json.
        # Without both, plugin-provided MCP servers (imessage,
        # chrome-devtools-mcp, etc.) silently never register.
        plugins_mode = sync_host_plugins(host_home, self.agent_home_dir)
        if plugins_mode not in ("no-host-dir",):
            logger.info(
                "agent %s: shared host ~/.claude/plugins/ (%s)",
                self.agent_id, plugins_mode,
            )
        enabled_count = sync_host_enabled_plugins(host_home, self.agent_home_dir)
        if enabled_count:
            logger.info(
                "agent %s: propagated %d enabledPlugins entry/entries "
                "from host settings.json", self.agent_id, enabled_count,
            )
        agent_claude = self.agent_home_dir / ".claude"
        if not (agent_claude / ".credentials.json").exists():
            logger.warning(
                "agent %s: no .credentials.json in %s (and none at %s). "
                "run `claude login` on the host — first turn will fail "
                "with an auth error otherwise.",
                self.agent_id, agent_claude, host_home / ".claude",
            )
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
        Path(self.claude_dir).mkdir(parents=True, exist_ok=True)

        self._log_host_runtime_banner()
        self._verified = True

    def _log_host_runtime_banner(self) -> None:
        """One-time startup banner. INFO when tool calls proxy to
        the owner; WARNING when the mode auto-approves everything.
        """
        mode = self.permission_mode
        if mode == "default":
            logger.info(
                "agent %s: cli-local runs on the host; all non-read tool "
                "calls DM the operator for approval via the PreToolUse "
                "hook. switch to 'cli-docker' for sandboxed execution if "
                "the operator can't be the gate.",
                self.agent_id,
            )
            return
        if mode == "acceptEdits":
            logger.info(
                "agent %s: cli-local (permission_mode=acceptEdits): file "
                "edits auto-approve; shell + network still DM the "
                "operator via the PreToolUse hook. switch to 'cli-docker' "
                "for full sandboxed execution.",
                self.agent_id,
            )
            return
        # auto / dontAsk / bypassPermissions: no proxy hook, no prompts.
        logger.warning(
            "agent %s: cli-local (permission_mode=%s) auto-approves all "
            "tool calls — the agent has your filesystem + network access "
            "with no approval prompts. switch to 'cli-docker' for "
            "sandboxed execution.",
            self.agent_id, mode,
        )
