"""Top-level CLI for the puffo-agent portal.

All commands are file-driven; the daemon reconciles on-disk state.
Entry point: the ``puffo-agent`` console script, or
``python -m puffo_agent.portal.cli <subcommand>``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .cli_parser import build_parser as build_cli_parser
from .daemon import run_daemon
from .state import (
    AgentConfig,
    DaemonConfig,
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
    daemon_pid_path,
    daemon_yml_path,
    discover_agents,
    derive_role_short,
    docker_shared_dir,
    home_dir,
    is_daemon_alive,
    is_daemon_ready,
    is_pid_alive,
    is_valid_agent_id,
    read_daemon_pid,
    refresh_agent_flag_path,
    refresh_host_sync_flag_path,
    refresh_model_flag_path,
    refresh_runtime_flag_path,
    refresh_session_flag_path,
    shared_fs_dir,
    write_refresh_token_request,
    write_stop_request,
)
from .workspace_layout import (
    AVAILABLE_SHARED_WORKSPACE_STATES,
    prepare_workspace_shared_access,
)

DEFAULT_PROFILE = """# Agent Profile

## Identity
You are a helpful assistant.

## Soul
Be thoughtful, capable, and clear.
"""


def _prepare_cli_shared_workspace(cfg: AgentConfig) -> str:
    status = prepare_workspace_shared_access(
        cfg.resolve_workspace_dir(),
        shared_fs_dir(),
        mounted=(cfg.runtime.kind or "cli-local") == "cli-docker",
    )
    if status not in AVAILABLE_SHARED_WORKSPACE_STATES:
        print(
            f"warning: shared workspace is {status}; cross-Agent file "
            "handoffs through workspace/shared are unavailable",
            file=sys.stderr,
        )
    return status


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
        from importlib.metadata import version

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
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        ValueError,
        OSError,
    ):
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
    """Set the daemon-owned Claude API-key policy.

    Claude Code and Codex authenticate through their CLI login or a
    per-agent gateway configuration by default.
    """
    home_dir().mkdir(parents=True, exist_ok=True)
    cfg = DaemonConfig.load()

    anthropic_api_key = getattr(args, "anthropic_api_key", None)
    anthropic_cli_use_api_key = getattr(
        args, "anthropic_cli_use_api_key", None
    )
    if anthropic_api_key is not None or anthropic_cli_use_api_key is not None:
        if anthropic_api_key is not None:
            cfg.anthropic.api_key = anthropic_api_key
        if anthropic_cli_use_api_key is not None:
            cfg.anthropic.cli_use_api_key = (
                anthropic_cli_use_api_key == "true"
            )
        cfg.save()
        print(f"wrote {daemon_yml_path()}")
        return 0

    if daemon_yml_path().exists():
        print(f"updating daemon.yml at {daemon_yml_path()}")
    else:
        print("creating daemon.yml (optional — defaults only)")

    def prompt(label: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            val = input(f"{label}{hint}: ").strip()
        except EOFError:
            val = ""
        return val or default

    anth_key = prompt(
        "Default Anthropic API key (blank to skip)",
        cfg.anthropic.api_key,
    )
    if anth_key:
        cfg.anthropic.api_key = anth_key
        cfg.anthropic.model = (
            cfg.anthropic.model or "claude-sonnet-4-6"
        )
    cli_use_api_key = prompt(
        "Use the Anthropic API key for Claude Code (true|false)",
        "true" if cfg.anthropic.cli_use_api_key else "false",
    ).lower()
    if cli_use_api_key not in {"true", "false"}:
        print("error: Claude Code API-key mode must be true or false")
        return 2
    cfg.anthropic.cli_use_api_key = cli_use_api_key == "true"

    cfg.save()
    print(f"wrote {daemon_yml_path()}")
    print(f"agents dir: {agents_dir()}")
    print()
    print("agent runtime choices (per agent, set at create time):")
    print(
        "  cli-local    Claude/Codex CLI on the host (default; log in to the CLI first)"
    )
    print(
        "  cli-docker   claude CLI inside a per-agent container  [Docker + `claude login` on host]"
    )
    print()
    print("daemon settings saved.")
    return 0


# Shown when a GUI entry point (``start --ui`` / ``start --background``) is
# invoked but the desktop UI's ``[gui]`` extra (PySide6) isn't installed.
# The base ``pip install puffo-agent`` is deliberately Qt-free so headless
# / cloud daemons don't pull Qt; PySide6 lives in the ``gui`` extra.
_GUI_EXTRA_HINT = (
    "the desktop UI requires the [gui] extra (PySide6), which is not "
    "installed. install it with:\n\n    pip install 'puffo-agent[gui]'\n"
    "or, for a uv tool install:\n"
    "    uv tool install --force 'puffo-agent[gui]'\n\n"
    "(the headless daemon — `puffo-agent start` with no UI flag — runs "
    "without it.)"
)


def cmd_start(args: argparse.Namespace) -> int:
    # The PySide6 import inside run_tray/launch is deferred to call time,
    # so the ImportError surfaces from the call, not the ``from .ui...``
    # line — wrap both so a missing [gui] extra yields the actionable hint
    # instead of a raw ModuleNotFoundError traceback.
    if getattr(args, "tray_runner", False):
        try:
            from .ui.tray import run_tray

            return run_tray()
        except ImportError:
            print(_GUI_EXTRA_HINT, file=sys.stderr)
            return 1
    if getattr(args, "background", False):
        from .background import spawn_background

        return spawn_background()
    if getattr(args, "ui", False):
        try:
            from .ui.launcher import launch

            return launch()
        except ImportError:
            print(_GUI_EXTRA_HINT, file=sys.stderr)
            return 1
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
        clear_daemon_pid(expected_pid=pid)
        clear_stop_request(expected_pid=pid)
        return 0

    write_stop_request(pid)
    print(f"requested daemon shutdown (pid={pid}); waiting up to {args.timeout}s...")
    deadline = time.time() + max(1, args.timeout)
    while time.time() < deadline:
        if not is_pid_alive(pid):
            clear_stop_request(expected_pid=pid)
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
    return asyncio.run(
        run_attach(
            Path(args.bundle),
            args.passcode,
            daemon_url=args.daemon_url,
            session_dir=session_dir,
        )
    )


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
            health_suffix = (
                f"  health={health}" if health not in ("ok", "unknown") else ""
            )
            print(f"  - {aid}  state={ac.state}  runtime={status}{health_suffix}")
        except Exception as exc:
            print(f"  - {aid}  (error: {exc})")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# agent subcommands
# ─────────────────────────────────────────────────────────────────────────────


def _derive_role_short_cli(role: str) -> str:
    """Compatibility wrapper around the shared server contract."""
    return derive_role_short(role)


def cmd_agent_create(args: argparse.Namespace) -> int:
    agent_id = args.id
    if not is_valid_agent_id(agent_id):
        print(
            f"error: invalid agent id {agent_id!r} (alphanumerics, _ and -)",
            file=sys.stderr,
        )
        return 2
    target = agent_dir(agent_id)
    if target.exists():
        print(f"error: agent {agent_id!r} already exists at {target}", file=sys.stderr)
        return 2

    runtime_kind = args.runtime or "cli-local"
    provider = args.provider or ""
    from .runtime_matrix import resolve_effective_harness, validate_triple

    harness = resolve_effective_harness(runtime_kind, provider, "")
    validation = validate_triple(runtime_kind, provider, harness)
    if not validation.ok:
        print(f"error: {validation.error}", file=sys.stderr)
        return 2
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
    role_short = _derive_role_short_cli(role) if role else ""
    if role_short_raw and role_short_raw != role_short:
        print(
            f"warning: --role-short is deprecated (PUF-401); ignoring "
            f"{role_short_raw!r}, using role-derived {role_short!r}",
            file=sys.stderr,
        )

    target.mkdir(parents=True)

    cfg = AgentConfig(
        id=agent_id,
        state="running",
        display_name=args.display_name or agent_id,
        role=role,
        role_short=role_short,
        runtime=RuntimeConfig(
            kind=runtime_kind,
            provider=provider,
            api_key=args.api_key or "",
            model=args.model or "",
            harness=harness,
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
    _prepare_cli_shared_workspace(cfg)

    (target / "memory").mkdir()

    profile_path = target / "profile.md"
    if args.profile and Path(args.profile).exists():
        shutil.copy2(args.profile, profile_path)
    else:
        profile_path.write_text(DEFAULT_PROFILE, encoding="utf-8")

    _print_agent_create_result(agent_id, target)
    return 0


def _print_agent_create_result(agent_id: str, target: Path) -> None:
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


def cmd_agent_list(args: argparse.Namespace) -> int:
    agents = discover_agents()
    if not agents:
        print("(no agents registered)")
        return 0
    daemon_alive = is_daemon_alive()
    fmt = "{id:<24}  {name:<18}  {state:<8}  {runtime:<18}  {msgs:>6}  {uptime}"
    print(
        fmt.format(
            id="ID",
            name="DISPLAY",
            state="STATE",
            runtime="RUNTIME",
            msgs="MSGS",
            uptime="UPTIME",
        )
    )
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
            "auth_failed",
            "api_error_abandoned",
            "refresh_broken",
            "unhandled_error",
            "codex_thread_wedged",
        ):
            runtime = f"{runtime} [{rs.health}]"
        # Truncate display_name for table alignment.
        display = ac.display_name or aid
        if len(display) > 18:
            display = display[:17] + "…"
        print(
            fmt.format(
                id=aid,
                name=display,
                state=ac.state,
                runtime=runtime,
                msgs=msgs,
                uptime=uptime,
            )
        )
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
    print(
        f"triggers:        on_mention={ac.triggers.on_mention} on_dm={ac.triggers.on_dm}"
    )
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
    """Describe the credential file without reading secret-bearing content."""
    if not path.exists():
        return "not present"
    try:
        st = path.stat()
    except OSError as exc:
        return f"stat failed: {exc}"
    return f"size={st.st_size}B mtime={_format_ts(int(st.st_mtime))}"


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
    print(
        "refresh request written; daemon will pick it up on its "
        "next reconcile tick (typically <1s)."
    )
    print(
        f"after a few seconds, re-check {host_creds} mtime "
        "to confirm the refresh landed."
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

    from .profile_sync import sync_agent_profile, write_refresh_agent_flag

    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)

    role_arg = getattr(args, "role", None)
    role_short_arg = getattr(args, "role_short", None)
    display_name_arg = getattr(args, "display_name", None)

    no_edits = all(x is None for x in (display_name_arg, role_arg, role_short_arg))
    if no_edits:
        print(f"id:            {cfg.id}")
        print(f"slug:          {cfg.puffo_core.slug}")
        print(f"display_name:  {cfg.display_name!r}")
        print(f"avatar_url:    {cfg.avatar_url!r}")
        print(f"role:          {cfg.role!r}")
        print(f"role_short:    {cfg.role_short!r}")
        print(f"server_url:    {cfg.puffo_core.server_url}")
        return 0

    # Validation mirrors the control provision contract so the CLI fails
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
        cfg.role_short = _derive_role_short_cli(role_arg)
        patch["role_short"] = cfg.role_short
    if isinstance(role_short_arg, str):
        derived = _derive_role_short_cli(cfg.role)
        if role_short_arg.strip() and role_short_arg.strip() != derived:
            print(
                f"warning: --role-short is deprecated (PUF-401); ignoring "
                f"{role_short_arg!r}, using role-derived {derived!r}",
                file=sys.stderr,
            )
        cfg.role_short = derived
        patch["role_short"] = derived

    cfg.save()
    write_refresh_agent_flag(cfg, reason="cli agent profile")

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
        print(f"  role_short:   {cfg.role_short!r}  (server-derived)")
    return 0


def _apply_cli_inference_level(
    cfg: AgentConfig, args: argparse.Namespace,
) -> tuple[int, bool, bool]:
    """Resolve ``--inference-level`` against the (possibly new) harness.

    Returns ``(exit_code, touched, cleared)``. A non-zero exit code means an
    explicitly supplied level is unusable and the caller must abort; a level
    left over from a harness swap is cleared silently, matching every other
    runtime writer.
    """
    if args.inference_level is None and args.harness is None:
        return 0, False, False
    from ..mcp.config import supported_inference_levels
    from .runtime_matrix import normalize_inference_level

    def _normalize(level: str) -> str:
        return normalize_inference_level(
            cfg.runtime.kind, cfg.runtime.provider, cfg.runtime.harness, level,
        )

    if args.inference_level is not None:
        if args.inference_level and not _normalize(args.inference_level):
            levels = supported_inference_levels(cfg.runtime.harness)
            print(
                f"error: inference level {args.inference_level!r} is not "
                f"supported by {cfg.runtime.harness!r}; expected one of "
                f"{', '.join(levels)}",
                file=sys.stderr,
            )
            return 2, False, False
        cfg.runtime.inference_level = args.inference_level
        return 0, True, False

    normalized = _normalize(cfg.runtime.inference_level)
    if normalized == cfg.runtime.inference_level:
        return 0, False, False
    cfg.runtime.inference_level = normalized
    return 0, False, True


def _load_runtime_command_config(
    agent_id: str,
    args: argparse.Namespace,
) -> AgentConfig | None:
    update_requested = any(
        getattr(args, field) is not None
        for field in (
            "kind",
            "provider",
            "model",
            "inference_level",
            "api_key",
            "docker_image",
            "permission_mode",
            "sandbox",
            "harness",
        )
    )
    try:
        # An explicit edit must be able to repair a runtime combination that
        # a newer release stopped supporting. The final combination is still
        # validated by cmd_agent_runtime before any bytes are written.
        return AgentConfig.load(
            agent_id,
            allow_invalid_runtime=update_requested,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def cmd_agent_runtime(args: argparse.Namespace) -> int:
    """Show or update the runtime: block in agent.yml. Fields are
    optional; invoking with no flags just prints the current block."""
    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = _load_runtime_command_config(agent_id, args)
    if cfg is None:
        return 2

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
    if args.permission_mode is not None:
        cfg.runtime.permission_mode = args.permission_mode
        touched = True
    if args.sandbox is not None:
        cfg.runtime.sandbox = args.sandbox
        touched = True
    if args.harness is not None:
        cfg.runtime.harness = args.harness
        touched = True
    status, level_touched, inference_level_cleared = _apply_cli_inference_level(
        cfg, args
    )
    if status:
        return status
    touched = touched or level_touched
    if not touched:
        # No flags → print only. Matches ``agent show``'s runtime lines.
        print(f"id:              {cfg.id}")
        print("runtime:")
        print(f"  kind:             {cfg.runtime.kind}")
        print(f"  provider:         {cfg.runtime.provider or '(default)'}")
        print(
            f"  harness:          {cfg.runtime.harness}  (cli-local / cli-docker only)"
        )
        print(f"  model:            {cfg.runtime.model or '(default)'}")
        print(
            f"  inference_level:  {cfg.runtime.inference_level or '(harness default)'}"
        )
        print(f"  api_key:          {'(set)' if cfg.runtime.api_key else '(inherit)'}")
        print(f"  docker_image:     {cfg.runtime.docker_image or '(bundled default)'}")
        print(f"  permission_mode:  {cfg.runtime.permission_mode}  (cli-local only)")
        print(f"  sandbox:          {cfg.runtime.sandbox}  (codex only)")
        return 0

    # Validate the triple before writing — same check the daemon
    # runs at AgentConfig.load.
    from .runtime_matrix import validate_triple

    result = validate_triple(
        cfg.runtime.kind, cfg.runtime.provider, cfg.runtime.harness
    )
    if not result.ok:
        print(f"error: {result.error}", file=sys.stderr)
        return 2

    cfg.save()
    print(f"agent {agent_id!r} runtime updated:")
    print(f"  kind={cfg.runtime.kind} model={cfg.runtime.model or '(default)'}")
    if cfg.runtime.inference_level:
        print(f"  inference_level={cfg.runtime.inference_level}")
    elif inference_level_cleared:
        print("  inference_level=(harness default; incompatible prior value cleared)")
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
        print(
            f"error: editor {editor!r} not found. Set $EDITOR and retry.",
            file=sys.stderr,
        )
        return 2
    from .profile_sync import write_refresh_agent_flag

    write_refresh_agent_flag(cfg, reason="cli profile editor")
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    """Link this machine to an operator via the online agent portal."""
    from .control.link import DEFAULT_SERVER_URL, friendly_device_name, run_link

    # The daemon holds the control WS that serves the operator's commands
    # once approved — auto-start a detached headless daemon if it isn't
    # running, so `link` stays a one-step onboard without requiring Qt.
    if not is_daemon_ready():
        from .background import spawn_headless_background

        startup_rc = spawn_headless_background()
        if startup_rc != 0:
            return startup_rc

    name = args.name or friendly_device_name()
    server_url = args.server_url or DEFAULT_SERVER_URL
    try:
        return asyncio.run(
            run_link(
                server_url,
                name,
                open_browser=not args.not_open,
                code=getattr(args, "code", None),
            )
        )
    except KeyboardInterrupt:
        print("\nlink: cancelled.")
        return 1


def cmd_unlink(args: argparse.Namespace) -> int:
    """Remove an operator pairing and pause that operator's agents."""
    from .control.link import run_unlink

    return asyncio.run(run_unlink(args.operator, expected_server_url=args.server_url))


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
        print(
            f"error: {dest} already exists (pass --force to overwrite)", file=sys.stderr
        )
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

    agent_id, cfg = _load_refresh_config(args.id)
    if cfg is None:
        return 2

    model_swap = _parse_refresh_model(args.model)
    if args.model is not None and model_swap is None:
        return 2

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
            json.dumps(payload),
            encoding="utf-8",
        )
        touched.append(
            f"refresh_runtime.flag (kind={kind!r}"
            + (
                f" harness={model_swap[0]!r} model={model_swap[1]!r}"
                if model_swap
                else ""
            )
            + ")"
        )
    elif model_swap is not None:
        refresh_model_flag_path(workspace).write_text(
            json.dumps(
                {
                    "harness": model_swap[0],
                    "model": model_swap[1],
                    "requested_at": now,
                }
            ),
            encoding="utf-8",
        )
        touched.append(
            f"refresh_model.flag (harness={model_swap[0]!r} model={model_swap[1]!r})"
        )
    else:
        refresh_agent_flag_path(workspace).write_text(
            json.dumps({"requested_at": now}),
            encoding="utf-8",
        )
        touched.append("refresh_agent.flag")
        if args.host_sync:
            refresh_host_sync_flag_path(workspace).write_text(
                json.dumps({"requested_at": now}),
                encoding="utf-8",
            )
            touched.append("refresh_host_sync.flag")
        if args.session:
            refresh_session_flag_path(workspace).write_text(
                json.dumps({"requested_at": now}),
                encoding="utf-8",
            )
            touched.append("refresh_session.flag")

    print(f"agent {agent_id!r}: dropped " + ", ".join(touched))
    if is_daemon_alive():
        print("daemon + worker will pick up the flags on the next tick / turn.")
    return 0


def _load_refresh_config(agent_id: str) -> tuple[str, AgentConfig | None]:
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return agent_id, None
    try:
        return agent_id, AgentConfig.load(agent_id)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return agent_id, None


def _parse_refresh_model(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    raw = value.strip()
    if ":" not in raw:
        print(
            "error: --model must be harness:model (e.g. codex:gpt-5)", file=sys.stderr
        )
        return None
    harness, model_id = (item.strip() for item in raw.split(":", 1))
    if not harness or not model_id:
        print("error: --model must be non-empty harness:model", file=sys.stderr)
        return None
    return harness, model_id


def cmd_agent_reset_primer(args: argparse.Namespace) -> int:
    """Re-sync the shared primer + rebuild the listed agents' CLAUDE.md.
    ensure_shared_primer runs on every worker startup, so this is only
    needed to force a rebuild without waiting for a message."""
    from ..agent.memory_errors import BriefingCompileError
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
        try:
            workspace_shared_status = _prepare_cli_shared_workspace(cfg)
            rebuild_agent_claude_md(
                shared_dir=shared_dir,
                profile_path=cfg.resolve_profile_path(),
                memory_dir=cfg.resolve_memory_dir(),
                workspace_dir=cfg.resolve_workspace_dir(),
                claude_user_dir=agent_claude_user_dir(agent_id),
                gemini_user_dir=agent_home_dir(agent_id) / ".gemini",
                agent_id=agent_id,
                display_name=cfg.display_name,
                role=cfg.role,
                role_short=cfg.role_short,
                puffo_handle=cfg.puffo_core.slug,
                workspace_shared_status=workspace_shared_status,
            )
        except BriefingCompileError as exc:
            print(f"error: agent {agent_id!r}: {exc}", file=sys.stderr)
            rc = 2
            continue
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
    """Build the public CLI while keeping command behavior in this module."""
    handlers = {
        name: value
        for name, value in globals().items()
        if name.startswith("cmd_") and callable(value)
    }
    return build_cli_parser(handlers)


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
