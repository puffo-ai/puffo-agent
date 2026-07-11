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
import sys
import time
from pathlib import Path

from ...macos.keychain import is_macos
from ...mcp.config import (
    default_python_executable,
    write_cli_mcp_config,
    write_codex_mcp_config,
)
from ...portal.state import (
    agent_codex_user_dir,
    home_dir,
    read_host_codex_mcp_servers,
    seed_claude_home,
    sync_host_codex_auth_view,
    sync_host_claude_code_auth_view,
    sync_host_enabled_plugins,
    sync_host_mcp_servers,
    sync_host_plugins,
    sync_host_skills,
)
from ..cli_bin import resolve_claude_bin, resolve_codex_bin, resolve_hermes_bin
from .base import Adapter, TurnContext, TurnResult
from .cli_session import AuditLog, ClaudeSession
from .codex_session import CodexSession
from .desired_install import run_spawn_install
from .hermes_helpers import (
    HERMES_NO_RESUME_SIGNATURE,
    hermes_model_id,
    parse_hermes_reply,
    run_cmd as hermes_run_cmd,
    stitch_hermes_prompt,
)

logger = logging.getLogger(__name__)


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


def _host_hermes_home() -> Path:
    """``$HERMES_HOME`` → ``%LOCALAPPDATA%\\hermes`` on Windows →
    ``~/.hermes`` elsewhere. Mirrors upstream ``get_hermes_home``."""
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            return Path(local_appdata) / "hermes"
    return Path.home() / ".hermes"


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


VALID_SANDBOX_MODES = frozenset({
    "read-only",
    "workspace-write",
    "danger-full-access",
})


def _sanitise_sandbox(mode: str, agent_id: str) -> str:
    """Validate ``mode`` against codex's sandbox set; unknown values
    fall back to ``danger-full-access`` (the prior hardcoded default)
    with a WARNING."""
    if mode in VALID_SANDBOX_MODES:
        return mode
    if mode:
        logger.warning(
            "agent %s: sandbox %r is not a valid codex sandbox — "
            "falling back to 'danger-full-access'. valid: %s",
            agent_id, mode, sorted(VALID_SANDBOX_MODES),
        )
    return "danger-full-access"


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
        sandbox: str = "danger-full-access",
        harness=None,
        desired_skills: list[str] | None = None,
        desired_mcps: list[str] | None = None,
        puffo_core_server_url: str = "",
        puffo_core_slug: str = "",
        puffo_core_keys_dir: str = "",
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
        self.sandbox = _sanitise_sandbox(sandbox, agent_id)
        self.desired_skills = list(desired_skills or [])
        self.desired_mcps = list(desired_mcps or [])
        self.puffo_core_server_url = puffo_core_server_url
        self.puffo_core_slug = puffo_core_slug
        self.puffo_core_keys_dir = puffo_core_keys_dir
        self._desired_codex_extras: dict[str, dict] = {}
        self._desired_installed = False
        if harness is None:
            from ..harness import ClaudeCodeHarness
            harness = ClaudeCodeHarness()
        # cli-local supports claude-code (default), codex (alpha),
        # and hermes (alpha — one-shot CLI per turn, no long-lived
        # session). gemini-cli remains cli-docker-only.
        if harness.name() == "gemini-cli":
            raise RuntimeError(
                f"agent {agent_id!r}: runtime.harness={harness.name()!r} is "
                "not supported with runtime.kind=cli-local yet. Use "
                "runtime.kind=cli-docker, or switch runtime.harness "
                "back to claude-code."
            )
        self.harness = harness
        self.puffo_core_mcp_env: dict[str, str] | None = None
        self._verified = False
        # claude-code path uses ClaudeSession (long-lived stream-json);
        # codex path uses CodexSession (long-lived JSON-RPC). hermes
        # has no long-lived session — every turn is a fresh
        # ``hermes chat`` subprocess, continuity comes from hermes'
        # own state.db indexed by per-agent HERMES_HOME + sentinel.
        self._session: ClaudeSession | None = None
        self._codex_session: CodexSession | None = None
        self._hermes_bin: str | None = None
        self._hermes_home: Path | None = None
        self._hermes_mcp_registered = False
        self._hermes_audit: AuditLog | None = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        self._verify()
        await self._install_desired()
        user_message = ctx.messages[-1]["content"] if ctx.messages else ""
        if self.harness.name() == "codex":
            session = self._ensure_codex_session()
            return await session.run_turn(user_message, ctx.system_prompt)
        if self.harness.name() == "hermes":
            return await self._run_hermes_turn(user_message, ctx.system_prompt)
        session = self._ensure_session()
        return await session.run_turn(user_message, ctx.system_prompt)

    async def run_retry_turn(
        self,
        kick_text: str,
        fallback_user_message: str,
        ctx: TurnContext,
    ) -> TurnResult:
        self._verify()
        await self._install_desired()
        if self.harness.name() == "codex":
            # codex has no equivalent of claude-code's cheap "resume
            # kick" — the App Server doesn't expose a resume-with-
            # tickle handle in v1. Re-send the full payload, same as
            # a fresh turn would.
            session = self._ensure_codex_session()
            return await session.run_turn(
                fallback_user_message, ctx.system_prompt,
            )
        if self.harness.name() == "hermes":
            # hermes always runs one-shot — the retry is just a
            # normal turn against the fallback payload.
            return await self._run_hermes_turn(
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
        await self._install_desired()
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
        if self.harness.name() == "hermes":
            # hermes is one-shot per turn; no subprocess to keep
            # warm. ``_verify`` already validated the binary +
            # seeded HERMES_HOME, so there's nothing left to do
            # until the first message arrives.
            return
        session = self._ensure_session()
        if not session.has_persisted_session():
            logger.info(
                "agent %s: no persisted session; deferring spawn until first message",
                self.agent_id,
            )
            return
        await session.warm(system_prompt)

    async def reload(
        self, new_system_prompt: str, *, with_session: bool = False,
    ) -> None:
        """Drop the cached subprocess so the next turn re-reads
        instructions, skills, config. ``with_session=True`` also
        unlinks the session sentinel."""
        codex_session_file = (
            self._codex_session.session_file if self._codex_session is not None else None
        )
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if self._codex_session is not None:
            await self._codex_session.aclose()
            self._codex_session = None
        if with_session:
            for path in (self.session_file, codex_session_file):
                if path is None:
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning(
                        "agent %s: couldn't unlink session file %s: %s",
                        self.agent_id, path, exc,
                    )

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if self._codex_session is not None:
            await self._codex_session.aclose()
            self._codex_session = None

    async def health_probe(self) -> bool:
        """Delegate to the Codex session probe when one exists; other
        harnesses (claude-code, hermes, gemini-cli) inherit the True
        default — their next inbound message surfaces a real auth
        failure via the worker leak-filter as before."""
        if self._codex_session is not None:
            return await self._codex_session.health_probe()
        return True

    def _ensure_codex_session(self) -> CodexSession:
        if self._codex_session is not None:
            return self._codex_session

        codex_home = agent_codex_user_dir(self.agent_id)
        codex_home.mkdir(parents=True, exist_ok=True)
        # AGENTS.md investment goes here so codex picks it up on
        # ``newConversation``; the file body itself is written by
        # ``profile_sync.rebuild_agent_codex_md`` (worker startup +
        # refresh). Writing the dir is just to make sure codex has a
        # HOME to read from.
        agents_md = codex_home / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text("", encoding="utf-8")

        # Pin ``cli_auth_credentials_store=file`` so codex reads
        # ``$CODEX_HOME/auth.json`` (not macOS Keychain) — required for
        # our view/refresh model. Host's own ``~/.codex/config.toml``
        # MCP entries are merged in so the agent inherits the
        # operator's catalog (puffo entry below shadows same-name).
        host_mcps = read_host_codex_mcp_servers(Path.home())
        # Host wins on collision so the operator's local override
        # beats the catalog default — same precedence as claude's
        # sync_host_mcp_servers.
        merged_extras: dict[str, dict] = dict(self._desired_codex_extras)
        merged_extras.update(host_mcps)
        if self.puffo_core_mcp_env:
            write_codex_mcp_config(
                codex_home / "config.toml",
                command=default_python_executable(),
                args=["-m", "puffo_agent.mcp.puffo_core_server"],
                env=self.puffo_core_mcp_env,
                extra_servers=merged_extras,
            )
        else:
            write_codex_mcp_config(
                codex_home / "config.toml",
                extra_servers=merged_extras,
            )
            logger.warning(
                "agent %s: codex MCP tools unavailable — puffo_core is "
                "not configured. populate `puffo_core:` in agent.yml to "
                "enable send_message / list_channels / etc.",
                self.agent_id,
            )
        if host_mcps:
            logger.info(
                "agent %s: merged %d host MCP server(s) into codex config: %s",
                self.agent_id, len(host_mcps), sorted(host_mcps),
            )

        env = {
            **os.environ,
            "CODEX_HOME": str(codex_home),
        }
        auth_mode = sync_host_codex_auth_view(Path.home(), codex_home)
        if auth_mode == "no-host-file":
            raise RuntimeError(
                f"agent {self.agent_id!r}: codex needs auth — run "
                "`codex login` in your own shell so ~/.codex/auth.json "
                "exists; cli-local + cli-docker only support codex's "
                "OAuth (ChatGPT account) credentials, not raw API keys."
            )
        logger.info(
            "agent %s: shared host codex auth (%s)",
            self.agent_id, auth_mode,
        )
        # Subprocess argv — ``codex app-server`` is the documented entry
        # point for embedding codex as a long-running agent. Resolve
        # via the shared resolver so PATH + ``PUFFO_CODEX_BIN`` env
        # override + macOS / Windows / Linux .app bundle paths all
        # work. LaunchAgent on macOS has a narrow PATH that misses
        # both ``/opt/homebrew/bin`` and the Codex.app bundle, so the
        # plain ``shutil.which`` would fail even when the operator
        # has Codex installed via the desktop app.
        codex_bin = resolve_codex_bin()
        if codex_bin is None:
            raise RuntimeError(
                "codex binary not found. Tried $PUFFO_CODEX_BIN, "
                "$PATH, and the Codex.app / Windows / Linux bundle "
                "paths. Install the Codex CLI (`npm install -g "
                "@openai/codex`), Codex.app, or set "
                "``PUFFO_CODEX_BIN=/abs/path/to/codex``."
            )
        # codex's macOS fs-sandbox helper self-invokes the CLI at the
        # hardcoded path ~/.local/bin/codex; a PATH tweak can't fix an
        # execvp of an absolute path, so point that path at the resolved
        # binary. A real file (or a live symlink) there is never touched;
        # a dangling symlink from a moved install is re-pointed.
        if is_macos():
            hardcoded = Path.home() / ".local" / "bin" / "codex"
            if not hardcoded.exists():
                try:
                    hardcoded.parent.mkdir(parents=True, exist_ok=True)
                    if hardcoded.is_symlink():
                        hardcoded.unlink()
                    hardcoded.symlink_to(codex_bin)
                    logger.info(
                        "agent %s: symlinked %s -> %s for codex fs-sandbox "
                        "self-invoke", self.agent_id, hardcoded, codex_bin,
                    )
                except OSError as exc:
                    logger.warning(
                        "agent %s: could not create ~/.local/bin/codex "
                        "symlink (%s); codex view_image may fail", self.agent_id, exc,
                    )

        # Belt-and-suspenders for name-based re-invokes: the resolved
        # binary's dir goes on the subprocess PATH.
        codex_bin_dir = str(Path(codex_bin).parent)
        existing_path = env.get("PATH", "")
        existing_dirs = {
            os.path.normcase(os.path.normpath(p))
            for p in existing_path.split(os.pathsep)
            if p
        }
        if (
            codex_bin_dir
            and os.path.normcase(os.path.normpath(codex_bin_dir)) not in existing_dirs
        ):
            env["PATH"] = (
                codex_bin_dir + os.pathsep + existing_path
                if existing_path
                else codex_bin_dir
            )
        argv = [codex_bin, "app-server"]

        codex_audit = AuditLog(
            Path(self.workspace_dir) / ".puffo-agent" / "audit.log",
            self.agent_id,
        )
        self._codex_session = CodexSession(
            agent_id=self.agent_id,
            session_file=codex_home / "codex_session.json",
            argv=argv,
            cwd=self.workspace_dir,
            env=env,
            permission_mode=self.permission_mode,
            sandbox=self.sandbox,
            model=self.model,
            audit=codex_audit,
        )
        return self._codex_session

    # ── hermes (cli-local) ────────────────────────────────────────────
    # hermes is one-shot per turn: every turn spawns
    # ``hermes chat --quiet -q <prompt>``. No long-lived session
    # process to keep warm. State lives in the per-agent
    # ``HERMES_HOME=<agent_home_dir>/.hermes`` directory which we
    # seed from the operator's ``~/.hermes`` on first verify so the
    # agent inherits the operator's provider keys + ``hermes setup``
    # choices without sharing the operator's chat history.

    def _verify_hermes(self) -> None:
        """Resolve hermes binary + seed per-agent HERMES_HOME from
        the host's template + pin model/provider from agent.yml."""
        bin_path = resolve_hermes_bin()
        if bin_path is None:
            raise RuntimeError(
                f"agent {self.agent_id!r}: hermes binary not found. "
                "Tried $PUFFO_HERMES_BIN, $PATH, and known installer "
                "paths. Install: POSIX ``curl -fsSL https://"
                "raw.githubusercontent.com/NousResearch/hermes-agent/"
                "main/scripts/install.sh | bash``, or Windows ``iex "
                "(irm https://raw.githubusercontent.com/NousResearch/"
                "hermes-agent/main/scripts/install.ps1)``. Restart the "
                "daemon shell, or set ``PUFFO_HERMES_BIN``."
            )
        self._hermes_bin = bin_path

        host_hermes_home = _host_hermes_home()
        host_config = host_hermes_home / "config.yaml"
        if not host_config.is_file():
            raise RuntimeError(
                f"agent {self.agent_id!r}: ``{host_config}`` missing — "
                "hermes installer should have created it. Re-run the "
                "install one-liner from the README."
            )

        self._hermes_home = self.agent_home_dir / ".hermes"
        self._seed_hermes_home(host_hermes_home, self._hermes_home)
        self._pin_hermes_model(self._hermes_home / "config.yaml")
        self._hermes_audit = AuditLog(
            Path(self.workspace_dir) / ".puffo-agent" / "audit.log",
            self.agent_id,
        )
        self._log_host_runtime_banner()

    def _seed_hermes_home(self, host_dir: Path, agent_dir: Path) -> None:
        """Idempotent copy of ``config.yaml`` + ``.env`` from the
        host's HERMES_HOME. ``state.db`` is deliberately not copied
        so each agent gets fresh session/memory state."""
        agent_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("config.yaml", ".env"):
            src = host_dir / filename
            dst = agent_dir / filename
            if dst.exists() or not src.is_file():
                continue
            try:
                dst.write_bytes(src.read_bytes())
                logger.info(
                    "agent %s: seeded per-agent HERMES_HOME with %s",
                    self.agent_id, filename,
                )
            except OSError as exc:
                logger.warning(
                    "agent %s: couldn't seed %s: %s",
                    self.agent_id, src, exc,
                )

    def _pin_hermes_model(self, config_path: Path) -> None:
        """Rewrite ``model.default`` + ``model.provider`` from
        agent.yml's runtime config every verify. Lets puffo-agent
        own the model/provider choice without operators needing to
        run ``hermes setup``."""
        provider, default = self._hermes_provider_and_model()
        import yaml
        try:
            with config_path.open("r", encoding="utf-8") as fh:
                config = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "agent %s: couldn't read %s to pin model: %s",
                self.agent_id, config_path, exc,
            )
            return
        model = config.setdefault("model", {})
        if model.get("default") == default and model.get("provider") == provider:
            return
        model["default"] = default
        model["provider"] = provider
        try:
            with config_path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(config, fh, sort_keys=False)
            logger.info(
                "agent %s: pinned hermes model to %s/%s",
                self.agent_id, provider, default,
            )
        except OSError as exc:
            logger.warning(
                "agent %s: couldn't write %s: %s",
                self.agent_id, config_path, exc,
            )

    def _hermes_provider_and_model(self) -> tuple[str, str]:
        """Split ``hermes_model_id(self.model)`` into (provider, model)
        — the shape hermes' ``config.yaml`` expects."""
        spec = hermes_model_id(self.model)
        if "/" in spec:
            provider, _, default = spec.partition("/")
            return provider, default
        return "anthropic", spec

    async def _run_hermes_turn(
        self,
        user_message: str,
        system_prompt: str,
        *,
        _retried: bool = False,
    ) -> TurnResult:
        """One-shot ``hermes chat -q`` per turn. Continuity rides on
        the per-agent ``HERMES_HOME``'s state.db + sentinel."""
        if self._hermes_bin is None or self._hermes_home is None:
            return TurnResult(reply="", metadata={
                "error": "hermes not verified before run_turn",
            })
        await self._ensure_hermes_mcp_registered_local()

        if self._hermes_audit is not None and not _retried:
            self._hermes_audit.write("turn.input", content=user_message)

        has_prior_session = self.session_file.exists()
        prompt = user_message if has_prior_session else stitch_hermes_prompt(
            system_prompt, user_message,
        )
        env = {
            **os.environ,
            "HERMES_HOME": str(self._hermes_home),
        }
        cmd = [
            self._hermes_bin, "chat",
            "--quiet",
            "--source", f"puffoagent:{self.agent_id}",
            "--model", hermes_model_id(self.model),
        ]
        if has_prior_session:
            cmd.append("--continue")
        cmd.extend(["-q", prompt])

        started = time.time()
        rc, stdout, stderr = await hermes_run_cmd(
            cmd, env=env, cwd=self.workspace_dir, check=False,
        )
        elapsed = time.time() - started
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        # Stale sentinel — clear + retry once without --continue.
        if (
            rc != 0
            and HERMES_NO_RESUME_SIGNATURE in stdout_text
            and not _retried
        ):
            logger.info(
                "agent %s: hermes rejected --continue; clearing sentinel + retry",
                self.agent_id,
            )
            try:
                self.session_file.unlink()
            except OSError:
                pass
            return await self._run_hermes_turn(
                user_message, system_prompt, _retried=True,
            )

        if rc != 0:
            logger.error(
                "agent %s: hermes turn rc=%d in %.1fs | stdout: %r | stderr: %s",
                self.agent_id, rc, elapsed,
                stdout_text.strip()[:400],
                stderr_text.strip()[-400:] or "(empty)",
            )
            if self._hermes_audit is not None:
                self._hermes_audit.write(
                    "turn.error", rc=rc,
                    stdout_snippet=stdout_text[:400],
                    stderr_tail=stderr_text[-400:],
                )
            return TurnResult(reply="", metadata={
                "error": f"hermes exited rc={rc}",
                "stdout_snippet": stdout_text[:400],
                "stderr_tail": stderr_text[-400:],
            })

        reply, session_id, tool_calls = parse_hermes_reply(stdout_text)
        if tool_calls:
            logger.info(
                "agent %s: hermes turn invoked %d tool(s): %s",
                self.agent_id, len(tool_calls), ", ".join(tool_calls),
            )
        if not reply:
            logger.warning(
                "agent %s: hermes rc=0 but parser found no reply. "
                "stdout: %r", self.agent_id, stdout_text[:400],
            )
        if self._hermes_audit is not None:
            for name in tool_calls:
                self._hermes_audit.write("tool", name=name)
            if reply:
                self._hermes_audit.write("assistant.text", text=reply)
            self._hermes_audit.write(
                "turn.result",
                session_id=session_id, elapsed_seconds=round(elapsed, 2),
                tool_count=len(tool_calls),
                stdout_raw=stdout_text[:2000],
            )

        # Sentinel for ``--continue`` on subsequent turns.
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
                    "agent %s: couldn't write hermes session_file: %s",
                    self.agent_id, exc,
                )

        # Hermes turns are always silent — ``--quiet`` stdout doesn't
        # surface MCP calls, so we can't tell whether send_message
        # fired. Skip the fallback regardless; reply text kept in
        # metadata for debug.
        return TurnResult(
            reply="",
            tool_calls=len(tool_calls),
            metadata={
                "harness": "hermes",
                "session_id": session_id,
                "elapsed_seconds": round(elapsed, 2),
                "tools_invoked": tool_calls,
                "send_message_targets": [{"channel": "", "root_id": ""}],
                "hermes_assistant_text": reply,
            },
        )

    async def _ensure_hermes_mcp_registered_local(self) -> None:
        """Register the puffo MCP server in hermes' per-agent
        ``config.yaml``.

        Direct YAML write — ``hermes mcp add`` is unusable because
        its argparse ``--args nargs='*'`` chokes on ``-m`` (parses
        it as the top-level ``-m MODEL`` flag).
        """
        if self._hermes_mcp_registered:
            return
        if self.puffo_core_mcp_env is None:
            logger.warning(
                "agent %s: hermes MCP registration skipped — puffo_core "
                "is not configured", self.agent_id,
            )
            return
        if self._hermes_home is None:
            return

        config_path = self._hermes_home / "config.yaml"
        if not config_path.is_file():
            logger.warning(
                "agent %s: hermes config.yaml missing at %s",
                self.agent_id, config_path,
            )
            return

        mcp_env = dict(self.puffo_core_mcp_env)
        mcp_env["PUFFO_WORKSPACE"] = self.workspace_dir
        mcp_env["PUFFO_RUNTIME_KIND"] = "cli-local"
        mcp_env["PUFFO_HARNESS"] = "hermes"

        import yaml
        try:
            with config_path.open("r", encoding="utf-8") as fh:
                config = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "agent %s: couldn't read %s: %s",
                self.agent_id, config_path, exc,
            )
            return

        servers = config.setdefault("mcp_servers", {})
        # No ``tools:`` field — hermes' interactive add saves
        # ``tools: {include: [...]}`` only when the operator filters.
        # Omitting it leaves all discovered tools enabled. A bare-list
        # ``tools:`` is interpreted as a filter and silently drops
        # everything.
        servers["puffo"] = {
            "command": default_python_executable(),
            "args": ["-m", "puffo_agent.mcp.puffo_core_server"],
            "env": mcp_env,
            "enabled": True,
        }
        try:
            with config_path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(config, fh, sort_keys=False)
        except OSError as exc:
            logger.warning(
                "agent %s: couldn't write hermes config.yaml at %s: %s "
                "(chat will work, tool calls won't)",
                self.agent_id, config_path, exc,
            )
            return
        logger.info(
            "agent %s: registered puffo MCP server in %s",
            self.agent_id, config_path,
        )
        self._hermes_mcp_registered = True

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
            **self._macos_credential_env(),
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

    def _macos_credential_env(self) -> dict[str, str]:
        """macOS-only env hardening:

        - ``CLAUDE_CONFIG_DIR`` points at the per-agent .claude so the
          agent's claude reads from ``<agent_home>/.claude/.credentials.json``
          (which the daemon's ``KeychainBackend.sync_to_agent`` keeps
          fresh) rather than racing the operator's main CLI for the
          Keychain entry.

        Deliberately does NOT set ``CLAUDE_CODE_OAUTH_TOKEN`` — that
        env var triggers the fallback-combiner cleanup path from Claude
        Code issue #37512 that deletes the Keychain entry. We let
        claude read its token from the per-agent ``.credentials.json``
        like normal.

        Returns ``{}`` on non-macOS so the Linux/Windows spawn env is
        unchanged.
        """
        if not is_macos():
            return {}
        return {
            "CLAUDE_CONFIG_DIR": str(Path(self.agent_home_dir) / ".claude"),
        }

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
            "enable send_message / list_channels_in_all_spaces / etc.",
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
        if self.harness.name() == "hermes":
            self._verify_hermes()
            self._verified = True
            return
        if resolve_claude_bin() is None:
            raise RuntimeError(
                "claude binary not found. Tried $PUFFO_CLAUDE_BIN, "
                "$PATH, and known bundle paths. Install the Claude "
                "Code CLI (`npm install -g @anthropic-ai/claude-code`) "
                "or set ``PUFFO_CLAUDE_BIN=/abs/path/to/claude``."
            )
        # Seed the per-agent virtual $HOME on first use (settings,
        # .claude.json). Credentials handled separately below.
        host_home = Path.home()
        self.agent_home_dir.mkdir(parents=True, exist_ok=True)
        seeded = seed_claude_home(host_home, self.agent_home_dir)
        if seeded:
            logger.info(
                "agent %s: seeded per-agent virtual $HOME at %s from %s",
                self.agent_id, self.agent_home_dir, host_home,
            )
        # Refresh-token-free view; the daemon's refresher is the sole
        # rotator. Post-tick view-sync keeps this file fresh.
        mode = sync_host_claude_code_auth_view(host_home, self.agent_home_dir)
        logger.info(
            "agent %s: wrote host credential view (%s)",
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

    async def _install_desired(self) -> None:
        """Spawn-time install of operator-picked skill + MCP templates.
        Runs once per adapter instance after ``_verify`` so host-sync
        wins on collisions. Fetch errors are logged + tolerated."""
        if self._desired_installed:
            return
        self._desired_installed = True
        codex_extras = await run_spawn_install(
            agent_id=self.agent_id,
            agent_home=self.agent_home_dir,
            workspace_dir=Path(self.workspace_dir),
            harness_name=self.harness.name(),
            desired_skills=self.desired_skills,
            desired_mcps=self.desired_mcps,
            server_url=self.puffo_core_server_url,
            slug=self.puffo_core_slug,
            keys_dir=self.puffo_core_keys_dir,
        )
        if codex_extras:
            self._desired_codex_extras = codex_extras

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
