"""Top-level CLI for the puffo-agent portal.

All commands are file-driven; the daemon reconciles on-disk state.
Entry point: the ``puffo-agent`` console script, or
``python -m puffo_agent.portal.cli <subcommand>``.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .api.pairing import clear_pairing, load_pairing
from .daemon import run_daemon
from .state import (
    AgentConfig,
    DaemonConfig,
    ProviderConfig,
    RuntimeConfig,
    RuntimeState,
    TriggerRules,
    agent_claude_user_dir,
    agent_dir,
    agent_home_dir,
    agent_yml_path,
    agents_dir,
    archived_dir,
    clear_daemon_pid,
    clear_stop_request,
    daemon_yml_path,
    daemon_pid_path,
    discover_agents,
    docker_shared_dir,
    home_dir,
    is_daemon_alive,
    is_pid_alive,
    is_valid_agent_id,
    read_daemon_pid,
    refresh_agent_flag_path,
    refresh_host_sync_flag_path,
    refresh_model_flag_path,
    refresh_runtime_flag_path,
    refresh_session_flag_path,
    write_refresh_token_request,
    write_stop_request,
)

DEFAULT_PROFILE = """# Agent Profile

## Conversation Format
Every incoming user message is wrapped in a structured markdown block:

    - channel: <channel name>
    - sender: <username> (<email>)
    - message: <actual message text>

The first two fields are context metadata — use them to understand where
the message was posted and who sent it. Only the `message:` field
contains the actual text you are replying to.

IMPORTANT: Your reply must contain ONLY your response text. Do NOT
include the markdown block, field labels like `message:`, bracketed
prefixes like `[#channel]`, or self-identifiers. If you need to address
the sender, use `@username` inline.

## Identity
You are a helpful assistant.

## When to Reply
Use your judgement. Reply when someone directly addresses you or asks a
question that invites a response. Stay silent when the conversation is
between other people and you have nothing useful to add — output
exactly `[SILENT]` to stay silent.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Version + update helpers
# ─────────────────────────────────────────────────────────────────────────────

# GitHub Releases (not PyPI) — we want the repo's release tag, not
# whatever's currently on PyPI which can lag during a release window.
GITHUB_RELEASES_LATEST_URL = (
    "https://api.github.com/repos/puffo-ai/puffo-agent/releases/latest"
)


def get_local_version() -> str:
    """Installed puffo-agent version, or "unknown" if metadata is
    missing (e.g. raw checkout)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("puffo-agent")
    except (ImportError, Exception):
        return "unknown"


def is_source_install() -> bool:
    """True when installed from a local path or VCS (PEP 610
    ``direct_url.json``) rather than PyPI. Outdated-version warnings
    are skipped for source installs since they may be ahead of main.
    """
    try:
        from importlib.metadata import files

        for f in files("puffo-agent") or []:
            if f.name == "direct_url.json":
                return True
    except Exception:
        pass
    return False


def fetch_latest_release_tag(timeout: float = 5.0) -> str | None:
    """Fetch the latest GitHub release tag, leading ``v`` stripped.
    Returns None on any failure so callers can fail-soft."""
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        GITHUB_RELEASES_LATEST_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "puffo-agent-cli",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        tag = (data.get("tag_name") or "").strip()
        return tag.lstrip("v") or None
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ValueError, OSError):
        return None


def is_outdated(local: str, remote: str) -> bool:
    """``remote > local`` for dotted versions, tolerating pre-release
    suffixes (``0.4.0rc1`` → ``0.4.0``). Falls back to False on
    parse errors — better to under-warn than to warn on noise."""
    def parse(v: str) -> tuple[int, ...]:
        out: list[int] = []
        for part in v.split("."):
            digits = ""
            for ch in part:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            out.append(int(digits) if digits else 0)
        return tuple(out)

    if local in ("", "unknown") or not remote:
        return False
    try:
        return parse(remote) > parse(local)
    except Exception:
        return False


def _is_uv_tool_install() -> bool:
    """True when puffo-agent was installed via ``uv tool install``,
    detected by ``sys.prefix`` landing under uv's tool store
    (``.../uv/tools/puffo-agent/``). Those users hit PEP 668
    ``externally-managed-environment`` on ``pip install``, so their
    upgrade command is ``uv tool install puffo-agent --force``.
    """
    prefix = sys.prefix.replace("\\", "/")
    return "/uv/tools/" in prefix


def upgrade_command_for_install_mode() -> str:
    """Suggested upgrade command for the current install mode."""
    if is_source_install():
        return (
            "pip install --upgrade --user "
            "'git+https://github.com/puffo-ai/puffo-agent.git'"
        )
    if _is_uv_tool_install():
        return "uv tool install puffo-agent --force"
    return "pip install --upgrade puffo-agent"


# ─────────────────────────────────────────────────────────────────────────────
# init / start / status
# ─────────────────────────────────────────────────────────────────────────────


def cmd_config(args: argparse.Namespace) -> int:
    """Set daemon-wide defaults (provider, models, API keys).
    Optional — agents can carry their own keys or read them from env."""
    home_dir().mkdir(parents=True, exist_ok=True)
    cfg = DaemonConfig.load()

    if daemon_yml_path().exists():
        print(f"updating daemon.yml at {daemon_yml_path()}")
    else:
        print("creating daemon.yml (optional — defaults only)")

    env_anthropic = os.environ.get("ANTHROPIC_API_KEY", "")
    env_openai = os.environ.get("OPENAI_API_KEY", "")
    env_google = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    )

    def prompt(label: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            val = input(f"{label}{hint}: ").strip()
        except EOFError:
            val = ""
        return val or default

    cfg.default_provider = prompt("Default AI provider (anthropic|openai|google)", cfg.default_provider or "anthropic")

    anth_key = cfg.anthropic.api_key or env_anthropic
    anth_key = prompt("Default Anthropic API key (blank to skip)", anth_key)
    if anth_key:
        cfg.anthropic = ProviderConfig(api_key=anth_key, model=cfg.anthropic.model or "claude-sonnet-4-6")

    oai_key = cfg.openai.api_key or env_openai
    oai_key = prompt("Default OpenAI API key (blank to skip)", oai_key)
    if oai_key:
        cfg.openai = ProviderConfig(api_key=oai_key, model=cfg.openai.model or "gpt-4o")

    goog_key = cfg.google.api_key or env_google
    goog_key = prompt("Default Google API key (blank to skip; needed for cli-docker + gemini-cli)", goog_key)
    if goog_key:
        cfg.google = ProviderConfig(api_key=goog_key, model=cfg.google.model or "gemini-2.5-pro")

    cfg.save()
    print(f"wrote {daemon_yml_path()}")
    print(f"agents dir: {agents_dir()}")
    print()
    print("agent runtime choices (per agent, set at create time):")
    print("  chat-local   conversational LLM, no tools (default, uses the keys above)")
    print("  sdk-local    in-process agent SDK w/ tools  [pip install puffo-agent[sdk]]")
    print("  cli-local    claude CLI on the host, permission-proxy DMs operator [run `claude login` first]")
    print("  cli-docker   claude CLI inside a per-agent container  [Docker + `claude login` on host]")
    print()
    print("defaults saved — `puffo-agent agent create` will use these unless overridden.")
    return 0



def cmd_start(args: argparse.Namespace) -> int:
    with_local_bridge = getattr(args, "with_local_bridge", False)
    if getattr(args, "tray_runner", False):
        from .ui.tray import run_tray
        return run_tray(with_local_bridge=with_local_bridge)
    if getattr(args, "background", False):
        from .background import spawn_background
        return spawn_background(with_local_bridge=with_local_bridge)
    if getattr(args, "ui", False):
        from .ui.launcher import launch
        return launch(with_local_bridge=with_local_bridge)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(run_daemon(with_local_bridge=with_local_bridge))


def cmd_stop(args: argparse.Namespace) -> int:
    """Request graceful daemon shutdown via the stop sentinel.

    The signal-file path is required on Windows where the proactor
    loop doesn't accept ``add_signal_handler(SIGTERM)``; without this
    only ``taskkill /F`` would work, leaving containers running.

    Polls the specific pid we asked to stop (not the pid file, which a
    new daemon can overwrite mid-upgrade), so a daemon-swap is reported
    as such instead of as "still running".
    """
    pid = read_daemon_pid()
    if pid is None:
        print("daemon: not running")
        return 0
    if not is_pid_alive(pid):
        print(f"daemon: not running (stale pid file at {daemon_pid_path()})")
        clear_daemon_pid()
        return 0

    write_stop_request()
    print(f"requested daemon shutdown (pid={pid}); waiting up to {args.timeout}s...")
    deadline = time.time() + max(1, args.timeout)
    while time.time() < deadline:
        if not is_pid_alive(pid):
            clear_stop_request()
            # A new daemon may have taken the pid file mid-poll — say so,
            # rather than a bare "stopped".
            new_pid = read_daemon_pid()
            if new_pid is not None and new_pid != pid and is_pid_alive(new_pid):
                print(
                    f"daemon stopped (pid={pid}); a new daemon has since "
                    f"started (pid={new_pid})"
                )
            else:
                print("daemon stopped")
            return 0
        time.sleep(1)

    print(
        f"warning: daemon still running after {args.timeout}s (pid={pid}); "
        "the sentinel is still in place — it will fire on the next reconcile "
        "tick. Run `puffo-agent status` to recheck, or "
        f"`taskkill /PID {pid} /F` (Windows) / `kill -9 {pid}` (POSIX) to "
        "force-kill (note: force-killing leaves cli-docker containers "
        "running, since aclose never gets to run).",
        file=sys.stderr,
    )
    return 1


def cmd_version(args: argparse.Namespace) -> int:
    """Print installed version + install mode."""
    local = get_local_version()
    src = "source install" if is_source_install() else "release install"
    print(f"puffo-agent {local}  ({src})")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    """Run the reference ws-local attach client."""
    import asyncio
    from pathlib import Path
    from .ws_local.ws_local_client import run_attach

    session_dir = Path(args.session_dir) if args.session_dir else None
    return asyncio.run(run_attach(
        Path(args.bundle),
        args.passcode,
        bridge_url=args.bridge_url,
        session_dir=session_dir,
    ))


def cmd_check_update(args: argparse.Namespace) -> int:
    """Compare installed version against the latest GitHub release.
    Never runs pip — Windows locks the running ``.exe``, and the
    correct pip invocation depends on install mode."""
    local = get_local_version()
    src = "source install" if is_source_install() else "release install"
    print(f"installed: puffo-agent {local}  ({src})")
    remote = fetch_latest_release_tag()
    if remote is None:
        print("latest:    (could not reach github.com — check your network)")
        return 0
    print(f"latest:    {remote}")
    if is_outdated(local, remote):
        print()
        print("an update is available. to upgrade:")
        print(f"  {upgrade_command_for_install_mode()}")
        if is_source_install():
            print("  (or re-run pip install against your local clone)")
        print()
        print("note: if the daemon is currently running, stop it first —")
        print("on windows the puffo-agent.exe file is locked while in use.")
        return 0
    print()
    print("you're up to date.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    pid = read_daemon_pid()
    alive = is_daemon_alive()
    if alive and pid is not None:
        print(f"daemon: running (pid={pid})")
    elif pid is not None:
        print(f"daemon: not running (stale pid file at {daemon_pid_path()}; pid={pid})")
    else:
        print("daemon: not running")
    agents = discover_agents()
    print(f"home: {home_dir()}")
    print(f"agents registered: {len(agents)}")
    for aid in agents:
        try:
            ac = AgentConfig.load(aid)
            rs = RuntimeState.load(aid)
            status = rs.status if rs else "unknown"
            health = rs.health if rs else "unknown"
            # Only surface non-ok health to keep the listing tight.
            health_suffix = f"  health={health}" if health not in ("ok", "unknown") else ""
            print(f"  - {aid}  state={ac.state}  runtime={status}{health_suffix}")
        except Exception as exc:
            print(f"  - {aid}  (error: {exc})")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# agent subcommands
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_api_key_for_create(
    provider: str,
    flag_value: str,
    runtime_kind: str,
) -> str:
    """Pick an API key for ``agent create``. Resolution order:
    --api-key, env var, daemon.yml default, interactive prompt. CLI
    runtimes return early — they auth via ~/.claude/.credentials."""
    if runtime_kind in ("cli-local", "cli-docker"):
        return flag_value
    if flag_value:
        return flag_value
    env_var = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GEMINI_API_KEY",
    }.get(provider)
    env_val = os.environ.get(env_var, "") if env_var else ""
    # Daemon defaults cover the operator who ran ``puffo-agent config``.
    daemon = DaemonConfig.load()
    daemon_default = {
        "anthropic": daemon.anthropic.api_key,
        "openai": daemon.openai.api_key,
        "google": daemon.google.api_key,
    }.get(provider, "")
    if daemon_default:
        return ""  # empty runtime.api_key inherits from daemon.yml
    if env_val:
        return env_val
    label = {
        "anthropic": "Anthropic API key",
        "openai": "OpenAI API key",
        "google": "Google API key",
    }.get(provider, f"{provider} API key")
    try:
        val = input(f"{label} (paste from your provider dashboard): ").strip()
    except EOFError:
        val = ""
    return val


def _derive_role_short_cli(role: str) -> str:
    """Local mirror of ``profiles::derive_role_short`` (server) +
    ``_derive_role_short`` (bridge). Keeps the chip label in
    agent.yml consistent with what the server stores when the
    daemon syncs on first connect. See server-side validators for
    the canonical contract."""
    if ":" not in role:
        return ""
    head, tail = role.split(":", 1)
    candidate = head.strip()
    rest = tail.strip()
    if not candidate or not rest or len(candidate) > 32:
        return ""
    if any(ch.isspace() for ch in candidate):
        return ""
    return candidate


def cmd_agent_create(args: argparse.Namespace) -> int:
    agent_id = args.id
    if not is_valid_agent_id(agent_id):
        print(f"error: invalid agent id {agent_id!r} (alphanumerics, _ and -)", file=sys.stderr)
        return 2
    target = agent_dir(agent_id)
    if target.exists():
        print(f"error: agent {agent_id!r} already exists at {target}", file=sys.stderr)
        return 2

    runtime_kind = args.runtime or "chat-local"
    provider = args.provider or "anthropic"
    api_key = _resolve_api_key_for_create(
        provider=provider,
        flag_value=args.api_key or "",
        runtime_kind=runtime_kind,
    )

    role = (args.role or "").strip()
    role_short_raw = getattr(args, "role_short", None)
    role_short_raw = role_short_raw.strip() if role_short_raw else ""
    if role_short_raw and not role:
        print(
            "error: --role-short cannot be set without --role",
            file=sys.stderr,
        )
        return 2
    if role and len(role) > 140:
        print("error: --role must be at most 140 characters", file=sys.stderr)
        return 2
    if role_short_raw and len(role_short_raw) > 32:
        print("error: --role-short must be at most 32 characters", file=sys.stderr)
        return 2
    role_short = role_short_raw or (_derive_role_short_cli(role) if role else "")

    target.mkdir(parents=True)

    cfg = AgentConfig(
        id=agent_id,
        state="running",
        display_name=args.display_name or agent_id,
        role=role,
        role_short=role_short,
        runtime=RuntimeConfig(
            kind=runtime_kind,
            provider=args.provider or "",
            api_key=api_key,
            model=args.model or "",
        ),
        profile="profile.md",
        memory_dir="memory",
        workspace_dir="workspace",
        triggers=TriggerRules(
            on_mention=not args.no_mention,
            on_dm=not args.no_dm,
        ),
        created_at=int(time.time()),
    )
    cfg.save()

    (target / "memory").mkdir(exist_ok=True)

    profile_path = target / "profile.md"
    if args.profile and Path(args.profile).exists():
        shutil.copy2(args.profile, profile_path)
    else:
        profile_path.write_text(DEFAULT_PROFILE, encoding="utf-8")

    print(f"created agent {agent_id!r} at {target}")
    print(
        "next: register a puffo-core identity for this agent with "
        "`puffo-cli agent register`, then fill the puffo_core: block in "
        f"{agent_yml_path(agent_id)} (slug, device_id, space_id; "
        "server_url defaults to https://chat.puffo.ai/relay)."
    )
    if not is_daemon_alive():
        print("daemon is not running — run `puffo-agent start` to activate.")
    else:
        print("daemon will pick it up on the next reconcile tick (a few seconds).")
    return 0


def _bridge_wait_until_command(base: str, command_id: str, timeout: float) -> int:
    """GET the bridge's wait-until-command and print its result JSON to stdout."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    url = (
        f"{base}/v1/machine/wait-until-command?"
        f"id={urllib.parse.quote(command_id)}&timeout={int(timeout)}"
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout + 10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 504:
            print(f"pending: operator hasn't approved yet ({detail})", file=sys.stderr)
        else:
            print(f"error: wait failed (HTTP {exc.code}): {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"error: cannot reach the daemon bridge ({exc.reason})", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


def cmd_agent_create_ws_local(args: argparse.Namespace) -> int:
    """Request a ws-local agent via operator approval. Non-blocking: prints the
    ``request_id`` and returns. Poll completion with
    ``machine wait-until-command --id <request_id>`` (or pass ``--wait`` to block
    here). Requires the daemon running with the bridge."""
    import json
    import urllib.error
    import urllib.request

    base = args.bridge_url.rstrip("/")
    body = json.dumps(
        {
            "operator": args.operator,
            "passcode": args.passcode,
            "display_name": getattr(args, "display_name", "") or "",
            "message": getattr(args, "message", "") or "",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/v1/agents/create-ws-local",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            started = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"error: request failed (HTTP {exc.code}): {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(
            f"error: cannot reach the daemon bridge at {args.bridge_url} ({exc.reason}). "
            "Is the daemon running with --with-local-bridge?",
            file=sys.stderr,
        )
        return 1

    if not getattr(args, "wait", False):
        print(json.dumps(started))
        return 0
    return _bridge_wait_until_command(base, str(started.get("request_id") or ""), args.wait_timeout)


def cmd_machine_wait_until_command(args: argparse.Namespace) -> int:
    """Block until the command with ``--id`` has been processed, print its result."""
    return _bridge_wait_until_command(args.bridge_url.rstrip("/"), args.id, args.timeout)


def cmd_agent_list(args: argparse.Namespace) -> int:
    agents = discover_agents()
    if not agents:
        print("(no agents registered)")
        return 0
    daemon_alive = is_daemon_alive()
    fmt = "{id:<24}  {name:<18}  {state:<8}  {runtime:<18}  {msgs:>6}  {uptime}"
    print(fmt.format(
        id="ID", name="DISPLAY", state="STATE",
        runtime="RUNTIME", msgs="MSGS", uptime="UPTIME",
    ))
    print("-" * 100)
    for aid in agents:
        try:
            ac = AgentConfig.load(aid)
        except Exception as exc:
            print(f"{aid:<24}  (error: {exc})")
            continue
        rs = RuntimeState.load(aid)
        if rs is None:
            runtime = "no data"
            msgs = 0
            uptime = "—"
        else:
            staleness = int(time.time()) - rs.updated_at
            if daemon_alive and staleness < 30:
                runtime = rs.status
            elif rs.status == "stopped":
                runtime = "stopped"
            else:
                runtime = "stale"
            msgs = rs.msg_count
            if rs.started_at:
                uptime = _format_duration(int(time.time()) - rs.started_at)
            else:
                uptime = "—"
        # Surface non-ok health alongside lifecycle status so the
        # operator can see at a glance which agents need attention.
        if rs is not None and rs.health in (
            "in_progress",
            "auth_failed", "api_error_abandoned", "refresh_broken",
            "unhandled_error", "codex_thread_wedged",
        ):
            runtime = f"{runtime} [{rs.health}]"
        # Truncate display_name for table alignment.
        display = (ac.display_name or aid)
        if len(display) > 18:
            display = display[:17] + "…"
        print(fmt.format(
            id=aid, name=display, state=ac.state,
            runtime=runtime, msgs=msgs, uptime=uptime,
        ))
    return 0


def cmd_agent_show(args: argparse.Namespace) -> int:
    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    ac = AgentConfig.load(agent_id)
    rs = RuntimeState.load(agent_id)
    print(f"id:              {ac.id}")
    print(f"display_name:    {ac.display_name}")
    print(f"state:           {ac.state}")
    print(f"directory:       {agent_dir(agent_id)}")
    print(f"profile:         {ac.resolve_profile_path()}")
    print(f"memory_dir:      {ac.resolve_memory_dir()}")
    print(f"workspace_dir:   {ac.resolve_workspace_dir()}")
    print("puffo_core:")
    print(f"  server_url:    {ac.puffo_core.server_url or '(not set)'}")
    print(f"  slug:          {ac.puffo_core.slug or '(not set)'}")
    print(f"  device_id:     {ac.puffo_core.device_id or '(not set)'}")
    print(f"  space_id:      {ac.puffo_core.space_id or '(not set)'}")
    print(f"claude_dir:      {ac.resolve_claude_dir()}  (derived)")
    print("runtime:")
    print(f"  kind:          {ac.runtime.kind}")
    print(f"  provider:      {ac.runtime.provider or '(default)'}")
    print(f"  model:         {ac.runtime.model or '(default)'}")
    print(f"  api_key:       {'(set)' if ac.runtime.api_key else '(inherit)'}")
    print(f"triggers:        on_mention={ac.triggers.on_mention} on_dm={ac.triggers.on_dm}")
    if rs is not None:
        print("status:")
        print(f"  status:        {rs.status}")
        print(f"  health:        {rs.health}")
        print(f"  msg_count:     {rs.msg_count}")
        print(f"  last_event_at: {_format_ts(rs.last_event_at)}")
        print(f"  updated_at:    {_format_ts(rs.updated_at)}")
        if rs.error:
            print(f"  error:         {rs.error}")
    return 0


def cmd_agent_pause(args: argparse.Namespace) -> int:
    return _set_agent_state(args.id, "paused")


def cmd_agent_resume(args: argparse.Namespace) -> int:
    return _set_agent_state(args.id, "running")


def _summarise_credentials(path: Path) -> str:
    """One-line description of a ``.credentials.json`` file (mtime,
    expiry, token presence, scopes) for the refresh-ping diagnostic."""
    import json as _json
    if not path.exists():
        return "not present"
    try:
        st = path.stat()
    except OSError as exc:
        return f"stat failed: {exc}"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"size={st.st_size}B mtime={_format_ts(int(st.st_mtime))} parse-error: {exc}"
    oauth = data.get("claudeAiOauth") or {}
    expires_ms = oauth.get("expiresAt")
    if isinstance(expires_ms, (int, float)):
        expires_in = int(expires_ms / 1000 - time.time())
        expires_at = _format_ts(int(expires_ms / 1000))
        expiry_info = f"expiresAt={expires_at} ({expires_in:+d}s from now)"
    else:
        expiry_info = "expiresAt=(missing)"
    has_access = bool(oauth.get("accessToken"))
    has_refresh = bool(oauth.get("refreshToken"))
    scopes = oauth.get("scopes") or []
    return (
        f"mtime={_format_ts(int(st.st_mtime))} {expiry_info} "
        f"accessToken={'yes' if has_access else 'no'} "
        f"refreshToken={'yes' if has_refresh else 'no'} "
        f"scopes={scopes!r}"
    )


def cmd_agent_refresh_token(args: argparse.Namespace) -> int:
    """PUF-221: ask the daemon to refresh Claude's OAuth token and
    distribute the new credentials to every agent.

    Writes the ``refresh-token`` flag file; the daemon picks it up
    on its next reconcile tick, wakes the credential refresher, runs
    one ``claude --print "ok"`` against the host credentials, and
    fans ``sync_host_claude_code_auth_view`` to every registered agent home.
    Single writer (daemon) = no multi-process race on Anthropic's
    single-use refresh tokens.
    """
    if not is_daemon_alive():
        print(
            "error: puffo-agent daemon is not running. start it with "
            "`puffo-agent start`.",
            file=sys.stderr,
        )
        return 2
    host_creds = Path.home() / ".claude" / ".credentials.json"
    print("host credentials:")
    print(f"  {host_creds}")
    print(f"  {_summarise_credentials(host_creds)}")
    print()
    write_refresh_token_request()
    print("refresh request written; daemon will pick it up on its "
          "next reconcile tick (typically <1s).")
    print(
        f"after a few seconds, re-check {host_creds} mtime + "
        "expiresAt to confirm the refresh landed."
    )
    return 0


def _set_agent_state(agent_id: str, new_state: str) -> int:
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    if cfg.state == new_state:
        print(f"agent {agent_id!r} already {new_state}")
        return 0
    cfg.state = new_state
    cfg.save()
    print(f"agent {agent_id!r} state set to {new_state}")
    if is_daemon_alive():
        print("daemon will apply the change on the next reconcile tick.")
    return 0


def cmd_agent_rename(args: argparse.Namespace) -> int:
    """Change display_name on disk, in profile.md heading, on the
    server identity, and drop refresh_agent.flag (mirrors bridge edit)."""
    import asyncio
    from ..agent.shared_content import rewrite_profile_name
    from .profile_sync import sync_agent_profile, write_refresh_agent_flag

    agent_id = args.id
    new_name = (args.display_name or "").strip()
    if not new_name:
        print("error: display_name cannot be empty", file=sys.stderr)
        return 2
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    old_name = cfg.display_name
    if new_name == old_name:
        print(f"agent {agent_id!r} display_name already {new_name!r}")
        return 0
    cfg.display_name = new_name
    cfg.save()
    if old_name:
        try:
            rewrite_profile_name(cfg.resolve_profile_path(), old_name, new_name)
        except Exception as exc:
            print(
                f"warning: profile.md heading rewrite failed: {exc}",
                file=sys.stderr,
            )
    write_refresh_agent_flag(cfg, reason="cli agent rename")
    try:
        asyncio.run(sync_agent_profile(cfg, {"display_name": new_name}))
    except Exception as exc:
        print(
            f"warning: server profile sync failed: {exc} "
            f"(local agent.yml is updated; retry via the UI / linked "
            f"operator if you need the puffo-core identity to match)",
            file=sys.stderr,
        )
    print(f"agent {agent_id!r} display_name {old_name!r} → {new_name!r}")
    return 0


def cmd_agent_autoaccept(args: argparse.Namespace) -> int:
    """Flip the agent's per-space ``auto_accept_owner_invite`` flag
    via the server's PATCH endpoint. Signed by the agent's own subkey
    (mirrors the ``profile`` CLI's auth model — the operator
    controls the local keystore, so a CLI invocation IS an operator
    decision).

    The member-invite flag is intentionally not exposed: the server
    rejects PATCH-with-member-flag from agent identities (403), so
    surfacing it as a CLI flag would just produce a confusing
    server error. If/when the policy changes, add ``--member``
    here in lockstep with relaxing the server-side gate.
    """
    import asyncio

    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    space_id = args.space
    owner_on = args.owner == "on"

    async def patch_settings() -> dict:
        from ..crypto.http_client import PuffoCoreHttpClient
        from ..crypto.keystore import KeyStore

        pc = cfg.puffo_core
        ks = KeyStore.for_agent(cfg.id)
        http = PuffoCoreHttpClient(pc.server_url, ks, pc.slug)
        try:
            return await http.patch(
                f"/spaces/{space_id}/members/me/settings",
                {"auto_accept_owner_invite": owner_on},
            )
        finally:
            await http.close()

    try:
        resp = asyncio.run(patch_settings())
    except Exception as exc:
        print(f"error: server PATCH failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"agent {agent_id!r} in space {space_id}: "
        f"auto_accept_owner_invite = {resp.get('auto_accept_owner_invite')!r}"
    )
    print(
        f"  auto_accept_member_invite (unchanged, locked for agents): "
        f"{resp.get('auto_accept_member_invite')!r}"
    )
    return 0


def cmd_agent_profile(args: argparse.Namespace) -> int:
    """Show or update the identity-profile fields (display_name, role,
    role_short) and best-effort sync them to puffo-server signed by
    the agent's own keystore.

    Mirrors the bridge ``PATCH /v1/agents/{id}/profile`` endpoint
    one-for-one — same validation, same wire shape, same server
    update — so anything the operator can do from the local-bridge
    UI is reachable from the CLI too. No flags ⇒ show current
    values. With flags ⇒ update agent.yml, then sync to server."""
    import asyncio

    from .profile_sync import sync_agent_profile

    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)

    role_arg = getattr(args, "role", None)
    role_short_arg = getattr(args, "role_short", None)
    display_name_arg = getattr(args, "display_name", None)

    no_edits = all(
        x is None for x in (display_name_arg, role_arg, role_short_arg)
    )
    if no_edits:
        print(f"id:            {cfg.id}")
        print(f"slug:          {cfg.puffo_core.slug}")
        print(f"display_name:  {cfg.display_name!r}")
        print(f"avatar_url:    {cfg.avatar_url!r}")
        print(f"role:          {cfg.role!r}")
        print(f"role_short:    {cfg.role_short!r}")
        print(f"server_url:    {cfg.puffo_core.server_url}")
        return 0

    # Validation mirrors handlers.update_profile so the CLI fails
    # locally before bothering the server.
    if role_short_arg is not None and role_arg is None and not cfg.role:
        print(
            "error: --role-short cannot be set without --role "
            "(no existing role on file)",
            file=sys.stderr,
        )
        return 2
    if role_arg is not None and len(role_arg) > 140:
        print("error: --role must be at most 140 characters", file=sys.stderr)
        return 2
    if role_short_arg is not None and len(role_short_arg) > 32:
        print("error: --role-short must be at most 32 characters", file=sys.stderr)
        return 2

    # Build the wire patch + apply locally in lock-step. agent.yml
    # writes happen first so a server-side hiccup doesn't lose what
    # the operator typed; the sync warning surfaces after.
    patch: dict[str, Any] = {}
    if isinstance(display_name_arg, str):
        new_name = display_name_arg.strip() or cfg.display_name
        cfg.display_name = new_name
        patch["display_name"] = new_name
    if isinstance(role_arg, str):
        cfg.role = role_arg
        patch["role"] = role_arg
        # Mirror the server-side derive locally so agent.yml stays
        # in sync with what the server stores unless the caller
        # explicitly overrides ``role_short`` below.
        if not isinstance(role_short_arg, str):
            cfg.role_short = _derive_role_short_cli(role_arg)
    if isinstance(role_short_arg, str):
        cfg.role_short = role_short_arg
        patch["role_short"] = role_short_arg

    cfg.save()

    try:
        asyncio.run(sync_agent_profile(cfg, patch))
    except Exception as exc:
        print(f"warning: server sync failed: {exc}", file=sys.stderr)
        print(
            "agent.yml is updated locally. Rerun this command after "
            "fixing connectivity to retry the push.",
            file=sys.stderr,
        )
        return 0

    print(f"agent {agent_id!r} profile updated + synced:")
    if "display_name" in patch:
        print(f"  display_name: {cfg.display_name!r}")
    if "role" in patch:
        print(f"  role:         {cfg.role!r}")
    if "role_short" in patch:
        print(f"  role_short:   {cfg.role_short!r}  (explicit)")
    elif "role" in patch:
        print(f"  role_short:   {cfg.role_short!r}  (server-derived)")
    return 0


def cmd_agent_runtime(args: argparse.Namespace) -> int:
    """Show or update the runtime: block in agent.yml. Fields are
    optional; invoking with no flags just prints the current block."""
    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)

    touched = False
    if args.kind is not None:
        cfg.runtime.kind = args.kind
        touched = True
    if args.provider is not None:
        cfg.runtime.provider = args.provider
        touched = True
    if args.model is not None:
        cfg.runtime.model = args.model
        touched = True
    if args.api_key is not None:
        cfg.runtime.api_key = args.api_key
        touched = True
    if args.docker_image is not None:
        cfg.runtime.docker_image = args.docker_image
        touched = True
    if args.allowed_tools is not None:
        raw = args.allowed_tools.strip()
        cfg.runtime.allowed_tools = (
            [] if not raw else [t.strip() for t in raw.split(",") if t.strip()]
        )
        touched = True
    if args.permission_mode is not None:
        cfg.runtime.permission_mode = args.permission_mode
        touched = True
    if args.sandbox is not None:
        cfg.runtime.sandbox = args.sandbox
        touched = True
    if args.harness is not None:
        cfg.runtime.harness = args.harness
        touched = True
    if args.max_turns is not None:
        if args.max_turns < 1:
            print("error: --max-turns must be >= 1", file=sys.stderr)
            return 2
        cfg.runtime.max_turns = args.max_turns
        touched = True

    if not touched:
        # No flags → print only. Matches ``agent show``'s runtime lines.
        print(f"id:              {cfg.id}")
        print("runtime:")
        print(f"  kind:             {cfg.runtime.kind}")
        print(f"  provider:         {cfg.runtime.provider or '(default)'}")
        print(f"  harness:          {cfg.runtime.harness}  (cli-local / cli-docker only)")
        print(f"  model:            {cfg.runtime.model or '(default)'}")
        print(f"  api_key:          {'(set)' if cfg.runtime.api_key else '(inherit)'}")
        print(f"  allowed_tools:    {cfg.runtime.allowed_tools or '[]'}")
        print(f"  docker_image:     {cfg.runtime.docker_image or '(bundled default)'}")
        print(f"  permission_mode:  {cfg.runtime.permission_mode}  (cli-local only)")
        print(f"  sandbox:          {cfg.runtime.sandbox}  (codex only)")
        print(f"  max_turns:        {cfg.runtime.max_turns}  (sdk-local only)")
        return 0

    # Validate the triple before writing — same check the daemon
    # runs at AgentConfig.load.
    from .runtime_matrix import validate_triple
    result = validate_triple(cfg.runtime.kind, cfg.runtime.provider, cfg.runtime.harness)
    if not result.ok:
        print(f"error: {result.error}", file=sys.stderr)
        return 2

    cfg.save()
    print(f"agent {agent_id!r} runtime updated:")
    print(f"  kind={cfg.runtime.kind} model={cfg.runtime.model or '(default)'}")
    if cfg.runtime.allowed_tools:
        print(f"  allowed_tools={cfg.runtime.allowed_tools}")
    if cfg.runtime.docker_image:
        print(f"  docker_image={cfg.runtime.docker_image}")
    if is_daemon_alive():
        print("daemon will restart the worker on the next reconcile tick.")
    return 0


def cmd_agent_archive(args: argparse.Namespace) -> int:
    agent_id = args.id
    src = agent_dir(agent_id)
    if not src.exists():
        from .control.client import _is_already_archived
        if _is_already_archived(agent_id):
            print(f"{agent_id!r} is already archived")
            return 0
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    # Pause first so the worker exits cleanly before we move the dir.
    cfg = AgentConfig.load(agent_id)
    if cfg.state != "paused":
        cfg.state = "paused"
        cfg.save()
        print(f"flipped {agent_id!r} to paused; waiting for daemon to release it...")
        for _ in range(10):
            rs = RuntimeState.load(agent_id)
            if rs is None or rs.status in ("stopped", "paused"):
                break
            time.sleep(1)

    archived_dir().mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = archived_dir() / f"{agent_id}-{stamp}"
    from .daemon import _retry_move
    from .import_agents import (
        revoke_archived_device,
        write_archived_pending_revoke,
    )

    async def _archive_async() -> int:
        move_err = await _retry_move(src, dest)
        if move_err is not None:
            print(
                f"error: archive move failed after retries: {move_err}\n"
                f"       source: {src}\n"
                f"       dest:   {dest}",
                file=sys.stderr,
            )
            return 1
        if cfg.puffo_core.is_configured():
            try:
                await revoke_archived_device(dest, slug=cfg.puffo_core.slug)
                print(f"revoked {agent_id!r} device server-side")
            except Exception as exc:  # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
                print(
                    f"warning: device revoke failed ({reason}); pending "
                    "marker left for the daemon's next startup sweep",
                    file=sys.stderr,
                )
                try:
                    from ..crypto.keystore import KeyStore
                    identity = KeyStore(dest / "keys").load_identity(
                        cfg.puffo_core.slug
                    )
                    write_archived_pending_revoke(
                        dest,
                        server_url=identity.server_url,
                        slug=identity.slug,
                        device_id=identity.device_id,
                        last_error=reason,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"warning: failed to write pending_revoke marker "
                        f"into {dest}: {exc}",
                        file=sys.stderr,
                    )
        print(f"archived {agent_id!r} → {dest}")
        return 0

    return asyncio.run(_archive_async())


def cmd_agent_edit(args: argparse.Namespace) -> int:
    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    profile = cfg.resolve_profile_path()
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    try:
        subprocess.call([editor, str(profile)])
    except FileNotFoundError:
        print(f"error: editor {editor!r} not found. Set $EDITOR and retry.", file=sys.stderr)
        return 2
    return 0


def cmd_pairing_show(args: argparse.Namespace) -> int:
    """Print the current bridge pairing, or ``(not paired)``.
    Reads ``pairing.json`` directly — works whether the daemon is
    running or not."""
    pairing = load_pairing()
    if pairing is None:
        print("bridge: not paired")
        return 0
    print(f"slug:        {pairing.slug}")
    print(f"device_id:   {pairing.device_id}")
    print(f"paired_at:   {_format_ts(int(pairing.paired_at / 1000))}")
    print(f"root_pubkey: {pairing.root_public_key}")
    return 0


def cmd_pairing_unpair(args: argparse.Namespace) -> int:
    """Remove the bridge pairing so a different identity can pair
    next. The daemon re-reads ``pairing.json`` on every request — no
    restart needed."""
    pairing = load_pairing()
    if pairing is None:
        print("bridge: nothing to unpair (not paired)")
        return 0
    clear_pairing()
    print(f"bridge: unpaired (was slug={pairing.slug} device_id={pairing.device_id})")
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    """Link this machine to an operator via the online agent portal."""
    from .control.link import DEFAULT_SERVER_URL, friendly_device_name, run_link

    # The daemon holds the control WS that serves the operator's commands
    # once approved — auto-start it (without the local bridge) if it isn't
    # running, so `link` is a one-step onboard.
    if not is_daemon_alive():
        from .background import spawn_background
        spawn_background()

    name = args.name or friendly_device_name()
    server_url = args.server_url or DEFAULT_SERVER_URL
    try:
        return asyncio.run(run_link(server_url, name, open_browser=not args.not_open))
    except KeyboardInterrupt:
        print("\nlink: cancelled.")
        return 1


def cmd_unlink(args: argparse.Namespace) -> int:
    """Remove an operator pairing and pause that operator's agents."""
    from .control.link import run_unlink

    return asyncio.run(run_unlink(args.operator, expected_server_url=args.server_url))


def cmd_api_status(args: argparse.Namespace) -> int:
    """Print bridge configuration + pairing status."""
    cfg = DaemonConfig.load()
    b = cfg.bridge
    pairing = load_pairing()
    print(f"enabled:         {b.enabled}")
    print(f"bind:            http://{b.bind_host}:{b.port}")
    print(f"allowed_origins: {b.allowed_origins}")
    if pairing is None:
        print("paired:          (none)")
    else:
        print(f"paired:          slug={pairing.slug} device_id={pairing.device_id}")
        print(f"paired_at:       {_format_ts(int(pairing.paired_at / 1000))}")
    if not is_daemon_alive():
        print("daemon:          not running (bridge is offline until you `puffo-agent start`)")
    return 0


def cmd_agent_export(args: argparse.Namespace) -> int:
    from . import export as exp

    agent_ids: list[str] = args.ids
    missing = [a for a in agent_ids if not agent_dir(a).exists()]
    if missing:
        print(f"error: agent(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    dest = Path(args.dest)
    if dest.suffix.lower() != ".puffoagent":
        dest = dest.with_suffix(".puffoagent")
    if dest.exists() and not args.force:
        print(f"error: {dest} already exists (pass --force to overwrite)", file=sys.stderr)
        return 2

    password = _prompt_password_twice("Set export password: ")
    if password is None:
        return 130

    try:
        blob = exp.pack(agent_ids, password)
    except exp.ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    dest.write_bytes(blob)
    print(f"exported {len(agent_ids)} agent(s) → {dest} ({len(blob):,} bytes)")
    return 0


def cmd_agent_import(args: argparse.Namespace) -> int:
    from . import export as exp
    from . import import_agents

    src = Path(args.src)
    if not src.is_file():
        print(f"error: {src} not found", file=sys.stderr)
        return 2
    try:
        blob = src.read_bytes()
    except OSError as e:
        print(f"error: cannot read {src}: {e}", file=sys.stderr)
        return 2

    password = _prompt_password_once("Import password: ")
    if password is None:
        return 130

    try:
        report = asyncio.run(import_agents.import_bundle(blob, password))
    except exp.ImportPackError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    width = max((len(r.agent_id) for r in report.results), default=10)
    for r in report.results:
        tag = {
            "imported": "OK     ",
            "imported_pending_revoke": "PARTIAL",
            "skipped": "SKIP   ",
            "failed": "FAIL   ",
        }.get(r.status, r.status)
        line = f"  [{tag}] {r.agent_id.ljust(width)}"
        if r.detail:
            line += f"  — {r.detail}"
        print(line)

    print(
        f"\nsummary: {report.imported} imported "
        f"({report.pending_revokes} pending revoke), "
        f"{report.skipped} skipped, {report.failed} failed"
    )
    return 0 if report.failed == 0 else 1


def cmd_agent_revoke_pending(args: argparse.Namespace) -> int:
    from . import import_agents

    if args.id:
        result = asyncio.run(import_agents.revoke_pending(args.id))
        if result.status == "imported":
            print(f"OK: revoked {result.old_device_id} for agent {result.agent_id}")
            return 0
        if result.status == "skipped":
            print(f"skip: {result.detail}")
            return 0
        print(f"FAIL: {result.detail}", file=sys.stderr)
        return 1

    pending = import_agents.list_pending_revokes()
    if not pending:
        print("no pending revokes")
        return 0
    print(f"{len(pending)} pending revoke(s):")
    for agent_id, old_device_id in pending:
        result = asyncio.run(import_agents.revoke_pending(agent_id))
        tag = "OK  " if result.status == "imported" else "FAIL"
        print(f"  [{tag}] {agent_id}  old={old_device_id}  {result.detail}")
    return 0


def _prompt_password_once(prompt: str) -> str | None:
    import getpass

    try:
        pw = getpass.getpass(prompt)
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        return None
    if not pw:
        print("error: empty password", file=sys.stderr)
        return None
    return pw


def _prompt_password_twice(prompt: str) -> str | None:
    import getpass

    try:
        pw = getpass.getpass(prompt)
        confirm = getpass.getpass("Confirm password:    ")
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        return None
    if not pw:
        print("error: empty password", file=sys.stderr)
        return None
    if pw != confirm:
        print("error: passwords do not match", file=sys.stderr)
        return None
    return pw


def cmd_agent_refresh(args: argparse.Namespace) -> int:
    """CLI mirror of the MCP ``refresh()`` tool plus the CLI-only
    ``--kind`` axis."""
    import json

    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    try:
        cfg = AgentConfig.load(agent_id)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    model_swap: tuple[str, str] | None = None
    if args.model is not None:
        raw = args.model.strip()
        if ":" not in raw:
            print("error: --model must be harness:model (e.g. codex:gpt-5)", file=sys.stderr)
            return 2
        harness, model_id = raw.split(":", 1)
        harness = harness.strip()
        model_id = model_id.strip()
        if not harness or not model_id:
            print("error: --model must be non-empty harness:model", file=sys.stderr)
            return 2
        model_swap = (harness, model_id)

    kind = args.kind.strip() if args.kind is not None else None
    swap_requested = bool(model_swap or kind)
    if swap_requested and (args.host_sync or args.session):
        print(
            "error: --host-sync / --session are worker-scope; they're "
            "subsumed by the full respawn from --model / --kind. Drop "
            "them or drop the swap flag.",
            file=sys.stderr,
        )
        return 2
    if (
        cfg.runtime.kind == "cli-docker"
        and args.host_sync
        and not args.session
        and not swap_requested
    ):
        print(
            "error: --host-sync on cli-docker requires --session (the "
            "container has to restart to pick up new host skills/MCP).",
            file=sys.stderr,
        )
        return 2

    workspace = cfg.resolve_workspace_dir()
    (workspace / ".puffo-agent").mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    touched: list[str] = []

    if kind is not None:
        payload: dict[str, str | int] = {"kind": kind, "requested_at": now}
        if model_swap is not None:
            payload["harness"], payload["model"] = model_swap
        refresh_runtime_flag_path(workspace).write_text(
            json.dumps(payload), encoding="utf-8",
        )
        touched.append(
            f"refresh_runtime.flag (kind={kind!r}"
            + (
                f" harness={model_swap[0]!r} model={model_swap[1]!r}"
                if model_swap else ""
            )
            + ")"
        )
    elif model_swap is not None:
        refresh_model_flag_path(workspace).write_text(
            json.dumps({
                "harness": model_swap[0],
                "model": model_swap[1],
                "requested_at": now,
            }),
            encoding="utf-8",
        )
        touched.append(
            f"refresh_model.flag (harness={model_swap[0]!r} "
            f"model={model_swap[1]!r})"
        )
    else:
        refresh_agent_flag_path(workspace).write_text(
            json.dumps({"requested_at": now}), encoding="utf-8",
        )
        touched.append("refresh_agent.flag")
        if args.host_sync:
            refresh_host_sync_flag_path(workspace).write_text(
                json.dumps({"requested_at": now}), encoding="utf-8",
            )
            touched.append("refresh_host_sync.flag")
        if args.session:
            refresh_session_flag_path(workspace).write_text(
                json.dumps({"requested_at": now}), encoding="utf-8",
            )
            touched.append("refresh_session.flag")

    print(f"agent {agent_id!r}: dropped " + ", ".join(touched))
    if is_daemon_alive():
        print(
            "daemon + worker will pick up the flags on the next tick / turn."
        )
    return 0


def cmd_agent_reset_primer(args: argparse.Namespace) -> int:
    """Re-sync the shared primer + rebuild the listed agents' CLAUDE.md.
    ensure_shared_primer runs on every worker startup, so this is only
    needed to force a rebuild without waiting for a message."""
    from ..agent.shared_content import (
        ensure_shared_primer,
        rebuild_agent_claude_md,
    )

    shared_dir = docker_shared_dir()
    actions = ensure_shared_primer(shared_dir)
    print(f"shared primer ({shared_dir}):")
    for rel, action in actions:
        print(f"  {rel}: {action}")

    rc = 0
    rebuilt: list[str] = []
    for agent_id in args.ids:
        if not agent_yml_path(agent_id).exists():
            print(f"error: agent {agent_id!r} not found", file=sys.stderr)
            rc = 2
            continue
        try:
            cfg = AgentConfig.load(agent_id)
        except Exception as exc:
            print(f"error: agent {agent_id!r}: {exc}", file=sys.stderr)
            rc = 2
            continue
        rebuild_agent_claude_md(
            shared_dir=shared_dir,
            profile_path=cfg.resolve_profile_path(),
            memory_dir=cfg.resolve_memory_dir(),
            workspace_dir=cfg.resolve_workspace_dir(),
            claude_user_dir=agent_claude_user_dir(agent_id),
            gemini_user_dir=agent_home_dir(agent_id) / ".gemini",
        )
        rebuilt.append(agent_id)
        print(f"rebuilt CLAUDE.md for {agent_id!r}")

    if rebuilt:
        if is_daemon_alive():
            print(
                "note: a running worker keeps its already-loaded prompt — "
                "the rebuilt CLAUDE.md takes effect when the agent's worker "
                "next restarts (or it calls refresh())."
            )
        else:
            print(
                "note: agents will pick up the rebuilt CLAUDE.md on the "
                "next `puffo-agent start`."
            )
    return rc


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    hours, rem = divmod(seconds, 3600)
    return f"{hours}h{rem // 60}m"


def _format_ts(ts: int) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


# ─────────────────────────────────────────────────────────────────────────────
# argparse glue
# ─────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="puffo-agent",
        description="Multi-agent portal for Puffo.ai",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "config",
        help=(
            "Optional: set daemon-wide defaults (provider, models, API keys). "
            "The daemon runs fine without this — agents can carry their own keys."
        ),
    ).set_defaults(func=cmd_config)
    start = sub.add_parser(
        "start",
        help=(
            "Run the daemon (foreground by default; --background detaches "
            "with a status-bar icon, --ui opens the desktop window)"
        ),
    )
    start_mode = start.add_mutually_exclusive_group()
    start_mode.add_argument(
        "--ui",
        action="store_true",
        help="Launch the PySide6 desktop window alongside the daemon.",
    )
    start_mode.add_argument(
        "--background",
        action="store_true",
        help=(
            "Detach the daemon into the background with a status-bar (tray) "
            "icon; it survives the terminal closing. Quit from the icon or "
            "run `puffo-agent stop`."
        ),
    )
    start.add_argument(
        "--with-local-bridge",
        action="store_true",
        help=(
            "Also serve the local bridge HTTP API (off by default; the MCP "
            "data + rpc ports are always served)."
        ),
    )
    # Internal: the detached child that --background spawns to host the
    # tray. Hidden from --help.
    start.add_argument("--tray-runner", action="store_true", help=argparse.SUPPRESS)
    start.set_defaults(func=cmd_start)
    stop = sub.add_parser(
        "stop",
        help=(
            "Signal the running daemon to shut down gracefully — "
            "stops cli-docker containers but keeps them around for "
            "next start (claude sessions resume from where they left "
            "off)."
        ),
    )
    stop.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Seconds to wait for the daemon to exit before giving up (default: 60)",
    )
    stop.set_defaults(func=cmd_stop)
    sub.add_parser("status", help="Show daemon + agent status").set_defaults(func=cmd_status)
    sub.add_parser("version", help="Print installed puffo-agent version").set_defaults(func=cmd_version)
    sub.add_parser(
        "check-update",
        help="Compare installed version against latest GitHub release",
    ).set_defaults(func=cmd_check_update)

    agent = sub.add_parser("agent", help="Manage individual agents")
    agent_sub = agent.add_subparsers(dest="agent_cmd", required=True)

    create = agent_sub.add_parser(
        "create",
        help=(
            "Register a new agent locally. Prompts for an LLM API key "
            "if --api-key isn't given and no env var / daemon default "
            "is set. The puffo_core slug/device_id/space_id still need "
            "to be populated (via puffo-cli or by hand) before the "
            "daemon will start the agent's worker."
        ),
    )
    create.add_argument("--id", required=True)
    create.add_argument("--display-name", help="Friendly name for the agent")
    create.add_argument(
        "--role",
        help=(
            "Short 'what does this agent do' string (<=140 chars). "
            "Recommended shape '<short>: <description>'; the server "
            "derives a chip label from the prefix (so "
            "'coder: main puffo-core coder' surfaces 'coder' in "
            "member lists)."
        ),
    )
    create.add_argument(
        "--role-short",
        help=(
            "Optional explicit override for the chip label "
            "(<=32 chars). When omitted and --role is set, the "
            "server derives it. Cannot be passed without --role."
        ),
    )
    create.add_argument("--profile", help="Path to a profile.md to copy (default: built-in template)")
    create.add_argument(
        "--runtime",
        choices=["chat-local", "sdk-local", "cli-local", "cli-docker"],
        default="chat-local",
        help="Runtime adapter kind (default: chat-local)",
    )
    create.add_argument(
        "--provider",
        choices=["anthropic", "openai", "google"],
        help="Model provider (default: anthropic; ignored for cli-local/cli-docker)",
    )
    create.add_argument(
        "--api-key",
        help=(
            "Provider API key. If omitted, falls back to "
            "ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY env "
            "var, then daemon.yml default, then an interactive prompt."
        ),
    )
    create.add_argument("--model", help="Model override")
    create.add_argument("--no-mention", action="store_true", help="Don't reply on @mention")
    create.add_argument("--no-dm", action="store_true", help="Don't reply on DM")
    create.set_defaults(func=cmd_agent_create)

    create_wsl = agent_sub.add_parser(
        "create-ws-local",
        help="Create a ws-local agent via operator approval over the machine channel.",
    )
    create_wsl.add_argument(
        "--operator", required=True, help="Linked operator slug to request approval from"
    )
    create_wsl.add_argument(
        "--passcode", required=True, help="Passcode for the .puffoagent bundle + ws-local attach"
    )
    create_wsl.add_argument("--display-name", default="", help="Suggested name for the new agent")
    create_wsl.add_argument(
        "--message",
        default="",
        help="Free-text note shown to the operator for context (why this agent is needed).",
    )
    create_wsl.add_argument(
        "--wait",
        action="store_true",
        help="Block until the operator approves and print the final result (slug/bundle/passcode).",
    )
    create_wsl.add_argument(
        "--wait-timeout", type=float, default=600.0, help="Seconds to wait with --wait (default 600)."
    )
    create_wsl.add_argument(
        "--bridge-url",
        default="http://127.0.0.1:63387",
        help="Bridge HTTP base URL (default: %(default)s).",
    )
    create_wsl.set_defaults(func=cmd_agent_create_ws_local)

    lst = agent_sub.add_parser("list", help="List registered agents")
    lst.set_defaults(func=cmd_agent_list)

    show = agent_sub.add_parser("show", help="Show details for one agent")
    show.add_argument("id")
    show.set_defaults(func=cmd_agent_show)

    pause = agent_sub.add_parser("pause", help="Pause a running agent (daemon will stop its worker)")
    pause.add_argument("id")
    pause.set_defaults(func=cmd_agent_pause)

    resume = agent_sub.add_parser("resume", help="Resume a paused agent")
    resume.add_argument("id")
    resume.set_defaults(func=cmd_agent_resume)

    refresh_token = agent_sub.add_parser(
        "refresh-token",
        help=(
            "Ask the daemon to refresh Claude's OAuth token and "
            "distribute the new credentials to every agent. Single "
            "writer (daemon) — no per-agent race on the on-disk "
            "refresh token."
        ),
    )
    refresh_token.set_defaults(func=cmd_agent_refresh_token)

    runtime = agent_sub.add_parser(
        "runtime",
        help="Show or edit the runtime: block in an agent's agent.yml",
    )
    runtime.add_argument("id")
    runtime.add_argument(
        "--kind",
        choices=["chat-local", "sdk-local", "cli-local", "cli-docker"],
        help="Runtime adapter kind",
    )
    runtime.add_argument(
        "--provider",
        choices=["anthropic", "openai", "google"],
        help=(
            "Model provider. anthropic (default) pairs with claude-code; "
            "openai pairs with hermes; google reserved for gemini-cli. "
            "Must match harness if harness is claude-code / gemini-cli."
        ),
    )
    runtime.add_argument("--model", help="Model override (empty string clears)")
    runtime.add_argument("--api-key", help="Runtime API key (sdk-local / chat-local)")
    runtime.add_argument(
        "--allowed-tools",
        help="SDK: comma-separated tool allowlist patterns, e.g. Read,Edit,\"Bash(git *)\" — empty clears",
    )
    runtime.add_argument("--docker-image", help="cli-docker: override image tag")
    runtime.add_argument(
        "--permission-mode",
        choices=["bypassPermissions"],
        help=(
            "cli-local: Claude Code permission mode. Only "
            "'bypassPermissions' is supported today — the proxy "
            "modes (default / acceptEdits / auto / dontAsk) need "
            "more work on the permission DM flow."
        ),
    )
    runtime.add_argument(
        "--sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help=(
            "codex (cli-local): file-system policy. Note "
            "``workspace-write`` is silently downgraded to read-only "
            "on Windows; use ``danger-full-access`` there."
        ),
    )
    runtime.add_argument(
        "--max-turns",
        type=int,
        help=(
            "sdk only: max agentic-loop iterations per conversation "
            "turn (tool calls + final reply). Default 10; raise for "
            "complex multi-step tasks."
        ),
    )
    runtime.add_argument(
        "--harness",
        choices=["claude-code", "hermes", "gemini-cli", "codex"],
        help=(
            "cli-local / cli-docker: which agent engine runs inside the "
            "runtime. 'claude-code' (default, anthropic only) spawns the "
            "claude CLI with our stream-json session protocol. 'hermes' "
            "(anthropic + openai) spawns `hermes chat` one-shot per turn. "
            "'gemini-cli' (google, reserved — not yet implemented) targets "
            "Google's gemini CLI. 'codex' (openai, cli-local alpha — opt-"
            "in, not the default for openai) spawns `codex app-server` as "
            "a long-lived JSON-RPC subprocess; auth via runtime.api_key or "
            "operator-side `codex login`. Hermes OAuth routes to "
            "Anthropic's extra_usage pool, NOT your Claude subscription — "
            "see NousResearch/hermes-agent#12905."
        ),
    )
    runtime.set_defaults(func=cmd_agent_runtime)

    profile = agent_sub.add_parser(
        "profile",
        help=(
            "Show or edit identity-profile fields (display_name, role, "
            "role_short). No flags ⇒ show. With flags ⇒ update "
            "agent.yml AND sync to puffo-server, signed by the agent's "
            "own keystore. Mirrors the local-bridge PATCH endpoint."
        ),
    )
    profile.add_argument("id")
    profile.add_argument(
        "--display-name",
        help="New friendly name for the agent (≤60 chars per server validation)",
    )
    profile.add_argument(
        "--role",
        help=(
            "Long-form 'what does this agent do' string (≤140 chars). "
            "Recommended shape '<short>: <description>' — the server "
            "auto-derives the chip label from the prefix."
        ),
    )
    profile.add_argument(
        "--role-short",
        help=(
            "Explicit chip-label override (≤32 chars). When omitted, "
            "the server derives this from --role. Cannot be passed "
            "alone unless agent.yml already has a role."
        ),
    )
    profile.set_defaults(func=cmd_agent_profile)

    autoaccept = agent_sub.add_parser(
        "autoaccept",
        help=(
            "Toggle this agent's auto-accept-channel-invite preference "
            "in a given space. With --owner on, the agent silently "
            "joins any channel its space-owner invites it to; with off, "
            "the agent goes through the normal DM-operator confirmation "
            "path. The member-invite flag is locked off for agents in "
            "this build (server returns 403)."
        ),
    )
    autoaccept.add_argument("id")
    autoaccept.add_argument("--space", required=True, help="space_id (sp_<uuid>) to scope the toggle to")
    autoaccept.add_argument(
        "--owner",
        choices=["on", "off"],
        required=True,
        help="Auto-accept channel invites from the space owner",
    )
    autoaccept.set_defaults(func=cmd_agent_autoaccept)

    archive = agent_sub.add_parser("archive", help="Stop and archive an agent to ~/.puffo-agent/archived/")
    archive.add_argument("id")
    archive.set_defaults(func=cmd_agent_archive)

    edit = agent_sub.add_parser("edit", help="Open the agent's profile.md in $EDITOR")
    edit.add_argument("id")
    edit.set_defaults(func=cmd_agent_edit)

    rename = agent_sub.add_parser(
        "rename",
        help="Change the agent's display name (server-side + local)",
    )
    rename.add_argument("id")
    rename.add_argument(
        "display_name",
        help="New display name. UTF-8 / CJK / emoji are fine.",
    )
    rename.set_defaults(func=cmd_agent_rename)

    export = agent_sub.add_parser(
        "export",
        help="Encrypted export of N agents into a .puffoagent bundle",
    )
    export.add_argument("ids", nargs="+", metavar="agent_id", help="agent id(s) to export")
    export.add_argument("--dest", required=True, help="Destination .puffoagent file")
    export.add_argument("--force", action="store_true", help="Overwrite dest if it exists")
    export.set_defaults(func=cmd_agent_export)

    imp = agent_sub.add_parser(
        "import",
        help="Restore agents from a .puffoagent bundle on this daemon",
    )
    imp.add_argument("src", help="Path to the .puffoagent file")
    imp.set_defaults(func=cmd_agent_import)

    revoke_pending = agent_sub.add_parser(
        "revoke-pending",
        help="Retry the post-import revocation of an old device",
    )
    revoke_pending.add_argument(
        "id",
        nargs="?",
        default=None,
        help="agent id to retry (omit to retry all pending)",
    )
    revoke_pending.set_defaults(func=cmd_agent_revoke_pending)

    reset_primer = agent_sub.add_parser(
        "reset-primer",
        help=(
            "Re-seed the shared platform primer to this install's version "
            "and rebuild the listed agents' CLAUDE.md from it"
        ),
    )
    reset_primer.add_argument(
        "ids",
        nargs="+",
        metavar="agent_id",
        help="agent id(s) whose CLAUDE.md to rebuild",
    )
    reset_primer.set_defaults(func=cmd_agent_reset_primer)

    refresh = agent_sub.add_parser(
        "refresh",
        help=(
            "Drop one or more refresh flags. No flags = rebuild CLAUDE.md "
            "+ re-sync shared skills."
        ),
    )
    refresh.add_argument("id", help="agent id")
    refresh.add_argument(
        "--host-sync",
        action="store_true",
        help="also re-sync ~/.claude/skills + host MCP registrations",
    )
    refresh.add_argument(
        "--session",
        action="store_true",
        help="also drop the CLI session token (fresh conversation on next spawn)",
    )
    refresh.add_argument(
        "--model",
        default=None,
        metavar="HARNESS:MODEL",
        help="swap (harness, model); e.g. codex:gpt-5, claude-code:sonnet-4-5",
    )
    refresh.add_argument(
        "--kind",
        default=None,
        help="swap runtime kind; CLI-only (MCP + web app cannot change this)",
    )
    refresh.set_defaults(func=cmd_agent_refresh)

    # Bridge / local HTTP API admin.
    pairing = sub.add_parser(
        "pairing",
        help="Inspect or reset the local bridge pairing (which user can drive this daemon)",
    )
    pairing_sub = pairing.add_subparsers(dest="pairing_cmd", required=True)
    pairing_sub.add_parser(
        "show",
        help="Print the currently paired (slug, device_id), or '(not paired)'",
    ).set_defaults(func=cmd_pairing_show)
    pairing_sub.add_parser(
        "unpair",
        help="Delete pairing.json so a different identity can pair next",
    ).set_defaults(func=cmd_pairing_unpair)

    machine = sub.add_parser(
        "machine",
        help="Link / unlink this machine to puffo operators via the agent portal",
    )
    machine_sub = machine.add_subparsers(dest="machine_cmd", required=True)

    machine_link = machine_sub.add_parser(
        "link",
        help="Link this machine to a puffo operator via the online agent portal",
    )
    machine_link.add_argument(
        "--server-url",
        default=None,
        help="puffo-server base URL (default: the production relay).",
    )
    machine_link.add_argument(
        "--name",
        default=None,
        help="Name for this machine in the portal (default: hostname).",
    )
    machine_link.add_argument(
        "--not-open",
        action="store_true",
        help="Don't auto-open the link page in your browser.",
    )
    machine_link.set_defaults(func=cmd_link)

    machine_unlink = machine_sub.add_parser(
        "unlink",
        help="Remove an operator pairing and pause that operator's agents",
    )
    machine_unlink.add_argument(
        "--operator", required=True, help="Operator slug to unlink",
    )
    machine_unlink.add_argument(
        "--server-url",
        default=None,
        help="Only unlink the pairing on this server URL (default: match by operator).",
    )
    machine_unlink.set_defaults(func=cmd_unlink)

    machine_wait = machine_sub.add_parser(
        "wait-until-command",
        help="Block until a machine command (by id) is processed; print its result.",
    )
    machine_wait.add_argument("--id", required=True, help="Command id to wait for (e.g. a create request_id).")
    machine_wait.add_argument(
        "--timeout", type=float, default=600.0, help="Seconds to wait (default 600)."
    )
    machine_wait.add_argument(
        "--bridge-url",
        default="http://127.0.0.1:63387",
        help="Bridge HTTP base URL (default: %(default)s).",
    )
    machine_wait.set_defaults(func=cmd_machine_wait_until_command)

    api = sub.add_parser(
        "api",
        help="Inspect the local bridge HTTP API config",
    )
    api_sub = api.add_subparsers(dest="api_cmd", required=True)
    api_sub.add_parser(
        "status",
        help="Print bind address, allowed origins, and pairing status",
    ).set_defaults(func=cmd_api_status)

    attach = sub.add_parser(
        "ws-local",
        help=(
            "Reference ws-local client: hold a WebSocket session to the "
            "daemon on behalf of a .puffoagent bundle so an external AI "
            "tool can drive the agent through files on disk."
        ),
    )
    attach.add_argument("bundle", help="Path to the .puffoagent export blob")
    attach.add_argument(
        "--passcode",
        required=True,
        help="Passcode that decrypts the bundle (matches the create-agent UI).",
    )
    attach.add_argument(
        "--bridge-url",
        default="http://127.0.0.1:63387",
        help="Bridge HTTP base URL (default: %(default)s).",
    )
    attach.add_argument(
        "--session-dir",
        default="",
        help=(
            "Pre-create the session work-dir at this path. "
            "Default: a random ``puffo-attach-XXXX`` under the system temp dir."
        ),
    )
    attach.set_defaults(func=cmd_attach)

    # macOS Keychain integration probes (no-op on Linux/Windows).
    from .diagnostic import register_test_subcommands
    register_test_subcommands(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdio for non-ASCII names/messages on Windows
    # consoles (cp1252/cp936). Best-effort.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
