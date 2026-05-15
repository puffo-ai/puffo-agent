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
import zipfile
from pathlib import Path

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
    is_valid_agent_id,
    link_host_credentials,
    read_daemon_pid,
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


def upgrade_command_for_install_mode() -> str:
    """Suggested upgrade command for the current install mode."""
    if is_source_install():
        return (
            "pip install --upgrade --user "
            "'git+https://github.com/puffo-ai/puffo-agent.git'"
        )
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(run_daemon())


def cmd_stop(args: argparse.Namespace) -> int:
    """Request graceful daemon shutdown via the stop sentinel.

    The signal-file path is required on Windows where the proactor
    loop doesn't accept ``add_signal_handler(SIGTERM)``; without this
    only ``taskkill /F`` would work, leaving containers running.
    """
    pid = read_daemon_pid()
    if pid is None:
        print("daemon: not running")
        return 0
    if not is_daemon_alive():
        print(f"daemon: not running (stale pid file at {daemon_pid_path()})")
        clear_daemon_pid()
        return 0

    write_stop_request()
    print(f"requested daemon shutdown (pid={pid}); waiting up to {args.timeout}s...")
    deadline = time.time() + max(1, args.timeout)
    while time.time() < deadline:
        if not is_daemon_alive():
            clear_stop_request()
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
        "server_url defaults to https://api.puffo.ai)."
    )
    if not is_daemon_alive():
        print("daemon is not running — run `puffo-agent start` to activate.")
    else:
        print("daemon will pick it up on the next reconcile tick (a few seconds).")
    return 0


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
        # Surface auth_failed alongside lifecycle status so the
        # operator can see at a glance which agents need re-auth.
        if rs is not None and rs.health == "auth_failed":
            runtime = f"{runtime} [auth_failed]"
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


def cmd_agent_refresh_ping(args: argparse.Namespace) -> int:
    """Run the OAuth refresh one-shot against a cli-local agent and
    print everything observable (credentials before/after, full
    subprocess output) for reproducible diagnosis."""
    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    if cfg.runtime.kind != "cli-local":
        print(
            f"error: refresh-ping only supports cli-local "
            f"(agent {agent_id!r} is {cfg.runtime.kind!r}).",
            file=sys.stderr,
        )
        return 2

    home_override = agent_home_dir(agent_id)
    agent_creds = home_override / ".claude" / ".credentials.json"
    host_creds = Path.home() / ".claude" / ".credentials.json"
    workspace = Path(cfg.resolve_workspace_dir())

    print(f"agent: {agent_id}  runtime: {cfg.runtime.kind}  model: {cfg.runtime.model or '(default)'}")
    print(f"agent HOME override: {home_override}")
    print(f"workspace:           {workspace}")
    print()
    print("Before link:")
    print(f"  agent {agent_creds}")
    print(f"        {_summarise_credentials(agent_creds)}")
    print(f"  host  {host_creds}")
    print(f"        {_summarise_credentials(host_creds)}")
    print()

    # Mirror LocalCLIAdapter._verify() so the diagnostic matches
    # production starting conditions.
    link_mode = link_host_credentials(Path.home(), home_override)
    print(f"link_host_credentials -> {link_mode}")
    print("After link:")
    print(f"  agent {agent_creds}")
    print(f"        {_summarise_credentials(agent_creds)}")
    print()

    if shutil.which("claude") is None:
        print("error: claude binary not on PATH", file=sys.stderr)
        return 2

    cmd = [
        "claude", "--dangerously-skip-permissions",
        "--print", "--max-turns", "1",
        "--output-format", "stream-json", "--verbose",
    ]
    if cfg.runtime.model:
        cmd.extend(["--model", cfg.runtime.model])
    cmd.append("ok")

    env = {
        **os.environ,
        "HOME": str(home_override),
        "USERPROFILE": str(home_override),
    }

    print(f"running: {' '.join(cmd)}")
    print(f"  cwd={workspace}")
    print(f"  HOME={home_override}  USERPROFILE={home_override}")
    print()

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            env=env,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - started
        print(f"timed out after {elapsed:.1f}s", file=sys.stderr)
        if exc.stdout:
            print("--- stdout ---")
            print(exc.stdout.decode("utf-8", errors="replace"))
        if exc.stderr:
            print("--- stderr ---")
            print(exc.stderr.decode("utf-8", errors="replace"))
        return 3
    elapsed = time.time() - started

    print(f"rc={proc.returncode}  elapsed={elapsed:.1f}s")
    print()
    print("--- stdout ---")
    stdout = proc.stdout.decode("utf-8", errors="replace")
    print(stdout or "(empty)")
    print()
    print("--- stderr ---")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    print(stderr or "(empty)")
    print()
    print("After:")
    print(f"  agent {agent_creds}")
    print(f"        {_summarise_credentials(agent_creds)}")
    print(f"  host  {host_creds}")
    print(f"        {_summarise_credentials(host_creds)}")
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
    """Change the operator-facing display_name in agent.yml.

    The chat-visible identity profile lives on puffo-core under the
    agent's slug; manage that via ``puffo-cli``.
    """
    agent_id = args.id
    new_name = (args.display_name or "").strip()
    if not new_name:
        print("error: display_name cannot be empty", file=sys.stderr)
        return 2
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    cfg.display_name = new_name
    cfg.save()
    print(f"agent {agent_id!r} display_name set to {new_name!r}")
    print(
        "note: this updates the local agent.yml only. "
        "use puffo-cli to change the puffo-core identity profile."
    )
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
    # Retry briefly: aiosqlite WAL handles can take a moment to
    # release after client.stop() returns on Windows.
    last_err: OSError | None = None
    for attempt in range(8):
        try:
            shutil.move(str(src), str(dest))
            last_err = None
            break
        except (OSError, PermissionError) as exc:
            last_err = exc
            time.sleep(0.5)
    if last_err is not None:
        # Surface a clear error so the operator can clean up by hand.
        print(
            f"error: archive partially failed after retries: {last_err}\n"
            f"       source: {src}\n"
            f"       dest:   {dest}\n"
            "       inspect both dirs and remove the source by hand if dest looks complete.",
            file=sys.stderr,
        )
        return 1
    print(f"archived {agent_id!r} → {dest}")
    return 0


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
    agent_id = args.id
    src = agent_dir(agent_id)
    if not src.exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    dest = Path(args.dest)
    if dest.suffix.lower() != ".zip":
        dest = dest.with_suffix(".zip")
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in src.rglob("*"):
            if path.is_file():
                # Skip any tmp files mid-write.
                if path.suffix == ".tmp":
                    continue
                arcname = Path(agent_id) / path.relative_to(src)
                zf.write(path, arcname=str(arcname))
    print(f"exported {agent_id!r} → {dest}")
    return 0


def cmd_agent_reset_primer(args: argparse.Namespace) -> int:
    """Re-seed the shared platform primer to this install's version,
    then rebuild the listed agents' managed CLAUDE.md from it.

    The shared primer is a single file shared by every agent, so the
    re-seed is global — the agent id list only scopes which agents'
    CLAUDE.md gets rebuilt. Running workers keep their already-loaded
    prompt; the rebuilt file takes effect on their next restart.
    """
    from ..agent.shared_content import (
        rebuild_agent_claude_md,
        reseed_shared_primer,
    )

    shared_dir = docker_shared_dir()
    actions = reseed_shared_primer(shared_dir)
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
                "next restarts (or it calls reload_system_prompt)."
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
    sub.add_parser("start", help="Run the daemon in the foreground").set_defaults(func=cmd_start)
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

    refresh_ping = agent_sub.add_parser(
        "refresh-ping",
        help=(
            "Diagnostic: run the OAuth refresh one-shot against a "
            "cli-local agent and print credentials before/after + "
            "full subprocess output."
        ),
    )
    refresh_ping.add_argument("id")
    refresh_ping.set_defaults(func=cmd_agent_refresh_ping)

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
        choices=["claude-code", "hermes", "gemini-cli"],
        help=(
            "cli-local / cli-docker: which agent engine runs inside the "
            "runtime. 'claude-code' (default, anthropic only) spawns the "
            "claude CLI with our stream-json session protocol. 'hermes' "
            "(anthropic + openai) spawns `hermes chat` one-shot per turn. "
            "'gemini-cli' (google, reserved — not yet implemented) targets "
            "Google's gemini CLI. Hermes OAuth routes to Anthropic's "
            "extra_usage pool, NOT your Claude subscription — see "
            "NousResearch/hermes-agent#12905."
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

    export = agent_sub.add_parser("export", help="Export agent profile + memory + config as a zip")
    export.add_argument("id")
    export.add_argument("dest", help="Destination .zip file")
    export.set_defaults(func=cmd_agent_export)

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

    api = sub.add_parser(
        "api",
        help="Inspect the local bridge HTTP API config",
    )
    api_sub = api.add_subparsers(dest="api_cmd", required=True)
    api_sub.add_parser(
        "status",
        help="Print bind address, allowed origins, and pairing status",
    ).set_defaults(func=cmd_api_status)

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
