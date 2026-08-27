"""Argument parser construction for the ``puffo-agent`` CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping

CommandHandler = Callable[[argparse.Namespace], int]
CommandHandlers = Mapping[str, CommandHandler]


def build_parser(handlers: CommandHandlers) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="puffo-agent",
        description="Multi-agent portal for Puffo.ai",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_core_commands(sub, handlers)
    agent_sub = _add_agent_create_commands(sub, handlers)
    _add_agent_runtime_commands(agent_sub, handlers)
    _add_agent_management_commands(agent_sub, handlers)
    _add_machine_commands(sub, handlers)
    _add_attach_command(sub, handlers)
    _add_diagnostic_commands(sub)
    return parser


def _add_core_commands(sub, handlers: CommandHandlers) -> None:
    config = sub.add_parser(
        "config",
        help=(
            "Optional: set daemon-wide model defaults. "
            "The daemon runs fine without this — agents can carry their own keys."
        ),
    )
    config.add_argument(
        "--anthropic-api-key",
        metavar="KEY",
        help="Set daemon.yml anthropic.api_key; pass an empty value to clear it",
    )
    config.add_argument(
        "--anthropic-cli-use-api-key",
        choices=("true", "false"),
        help="Allow Claude Code to use daemon.yml anthropic.api_key",
    )
    config.set_defaults(func=handlers["cmd_config"])
    start = sub.add_parser(
        "start",
        help=(
            "Run the daemon (foreground by default; --detach runs headless "
            "in the background, --background adds a status-bar icon, "
            "and --ui opens the desktop window)"
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
    start_mode.add_argument(
        "--detach",
        action="store_true",
        help="Detach the headless daemon without requiring the desktop GUI extra.",
    )
    # Internal: the detached child that --background spawns to host the
    # tray. Hidden from --help.
    start.add_argument("--tray-runner", action="store_true", help=argparse.SUPPRESS)
    start.set_defaults(func=handlers["cmd_start"])
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
    stop.set_defaults(func=handlers["cmd_stop"])
    sub.add_parser("status", help="Show daemon + agent status").set_defaults(
        func=handlers["cmd_status"]
    )
    sub.add_parser("version", help="Print installed puffo-agent version").set_defaults(
        func=handlers["cmd_version"]
    )
    sub.add_parser(
        "check-update",
        help="Compare installed version against latest GitHub release",
    ).set_defaults(func=handlers["cmd_check_update"])


def _add_agent_create_commands(sub, handlers: CommandHandlers):
    agent = sub.add_parser("agent", help="Manage individual agents")
    agent_sub = agent.add_subparsers(dest="agent_cmd", required=True)
    _add_agent_create(agent_sub, handlers)
    _add_agent_basic_commands(agent_sub, handlers)
    return agent_sub


def _add_agent_create(agent_sub, handlers: CommandHandlers) -> None:
    create = agent_sub.add_parser(
        "create",
        help=(
            "Register a new agent locally. Claude Code and Codex reuse their "
            "host CLI login; --api-key is only for an explicitly configured "
            "gateway. The puffo_core slug/device_id/space_id still need to be "
            "populated before the daemon starts the worker."
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
    create.add_argument(
        "--profile", help="Path to a profile.md to copy (default: built-in template)"
    )
    create.add_argument(
        "--runtime",
        choices=["cli-local", "cli-docker"],
        default="cli-local",
        help="Runtime adapter kind (default: cli-local)",
    )
    create.add_argument(
        "--provider",
        choices=["anthropic", "openai"],
        help="Model provider; selects the default harness",
    )
    create.add_argument(
        "--api-key",
        help=(
            "Gateway API key used with runtime.llm_base_url. Normal local "
            "Claude/Codex agents authenticate through their CLI login."
        ),
    )
    create.add_argument("--model", help="Model override")
    create.add_argument(
        "--no-mention", action="store_true", help="Don't reply on @mention"
    )
    create.add_argument("--no-dm", action="store_true", help="Don't reply on DM")
    create.set_defaults(func=handlers["cmd_agent_create"])


def _add_agent_basic_commands(agent_sub, handlers: CommandHandlers) -> None:
    lst = agent_sub.add_parser("list", help="List registered agents")
    lst.set_defaults(func=handlers["cmd_agent_list"])

    show = agent_sub.add_parser("show", help="Show details for one agent")
    show.add_argument("id")
    show.set_defaults(func=handlers["cmd_agent_show"])

    pause = agent_sub.add_parser(
        "pause", help="Pause a running agent (daemon will stop its worker)"
    )
    pause.add_argument("id")
    pause.set_defaults(func=handlers["cmd_agent_pause"])

    resume = agent_sub.add_parser("resume", help="Resume a paused agent")
    resume.add_argument("id")
    resume.set_defaults(func=handlers["cmd_agent_resume"])

    refresh_token = agent_sub.add_parser(
        "refresh-token",
        help=(
            "Ask the daemon to refresh Claude's OAuth token and "
            "distribute the new credentials to every agent. Single "
            "writer (daemon) — no per-agent race on the on-disk "
            "refresh token."
        ),
    )
    refresh_token.set_defaults(func=handlers["cmd_agent_refresh_token"])


def _add_agent_runtime_commands(agent_sub, handlers: CommandHandlers) -> None:
    _add_agent_runtime_parser(agent_sub, handlers)
    _add_agent_profile_parser(agent_sub, handlers)
    _add_agent_autoaccept_parser(agent_sub, handlers)


def _add_agent_runtime_parser(agent_sub, handlers: CommandHandlers) -> None:
    runtime = agent_sub.add_parser(
        "runtime",
        help="Show or edit the runtime: block in an agent's agent.yml",
    )
    runtime.add_argument("id")
    runtime.add_argument(
        "--kind",
        choices=["cli-local", "cli-docker"],
        help="Runtime adapter kind",
    )
    runtime.add_argument(
        "--provider",
        choices=["anthropic", "openai"],
        help=(
            "Model provider. anthropic (default) pairs with claude-code; "
            "openai pairs with codex on cli-local. Must match the selected "
            "harness. Hermes and Gemini remain design-only."
        ),
    )
    runtime.add_argument("--model", help="Model override (empty string clears)")
    runtime.add_argument(
        "--inference-level",
        default=None,
        help=(
            "Per-agent reasoning effort (values depend on harness; empty "
            "string clears and restores the harness default)"
        ),
    )
    runtime.add_argument(
        "--api-key",
        help="Gateway API key used with runtime.llm_base_url",
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
        "--harness",
        choices=["claude-code", "codex"],
        help=(
            "cli-local / cli-docker: which agent engine runs inside the "
            "runtime. Claude Code pairs with Anthropic and Codex with OpenAI. "
            "Both cli-local and cli-docker use the same long-lived Drivers; "
            "cli-docker places the selected CLI in a per-Agent container. "
            "Codex uses `codex app-server`; Claude Code uses stream-json. "
            "Auth comes from runtime.api_key or the matching operator-side "
            "CLI login. 'hermes' and 'gemini-cli' are "
            "design-only and rejected by config validation."
        ),
    )
    runtime.set_defaults(func=handlers["cmd_agent_runtime"])


def _add_agent_profile_parser(agent_sub, handlers: CommandHandlers) -> None:
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
    profile.set_defaults(func=handlers["cmd_agent_profile"])


def _add_agent_autoaccept_parser(agent_sub, handlers: CommandHandlers) -> None:
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
    autoaccept.add_argument(
        "--space", required=True, help="space_id (sp_<uuid>) to scope the toggle to"
    )
    autoaccept.add_argument(
        "--owner",
        choices=["on", "off"],
        required=True,
        help="Auto-accept channel invites from the space owner",
    )
    autoaccept.set_defaults(func=handlers["cmd_agent_autoaccept"])


def _add_agent_management_commands(agent_sub, handlers: CommandHandlers) -> None:
    _add_agent_archive_edit_rename(agent_sub, handlers)
    _add_agent_transfer_commands(agent_sub, handlers)
    _add_agent_maintenance_commands(agent_sub, handlers)


def _add_agent_archive_edit_rename(agent_sub, handlers: CommandHandlers) -> None:
    archive = agent_sub.add_parser(
        "archive", help="Stop and archive an agent to ~/.puffo-agent/archived/"
    )
    archive.add_argument("id")
    archive.set_defaults(func=handlers["cmd_agent_archive"])

    edit = agent_sub.add_parser("edit", help="Open the agent's profile.md in $EDITOR")
    edit.add_argument("id")
    edit.set_defaults(func=handlers["cmd_agent_edit"])

    rename = agent_sub.add_parser(
        "rename",
        help="Change the agent's display name (server-side + local)",
    )
    rename.add_argument("id")
    rename.add_argument(
        "display_name",
        help="New display name. UTF-8 / CJK / emoji are fine.",
    )
    rename.set_defaults(func=handlers["cmd_agent_rename"])


def _add_agent_transfer_commands(agent_sub, handlers: CommandHandlers) -> None:
    export = agent_sub.add_parser(
        "export",
        help="Encrypted export of N agents into a .puffoagent bundle",
    )
    export.add_argument(
        "ids", nargs="+", metavar="agent_id", help="agent id(s) to export"
    )
    export.add_argument("--dest", required=True, help="Destination .puffoagent file")
    export.add_argument(
        "--force", action="store_true", help="Overwrite dest if it exists"
    )
    export.set_defaults(func=handlers["cmd_agent_export"])

    imp = agent_sub.add_parser(
        "import",
        help="Restore agents from a .puffoagent bundle on this daemon",
    )
    imp.add_argument("src", help="Path to the .puffoagent file")
    imp.set_defaults(func=handlers["cmd_agent_import"])

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
    revoke_pending.set_defaults(func=handlers["cmd_agent_revoke_pending"])


def _add_agent_maintenance_commands(agent_sub, handlers: CommandHandlers) -> None:
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
    reset_primer.set_defaults(func=handlers["cmd_agent_reset_primer"])

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
    refresh.set_defaults(func=handlers["cmd_agent_refresh"])


def _add_machine_commands(sub, handlers: CommandHandlers) -> None:
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
        "--code",
        default=None,
        help="Link code generated in the Puffo web app",
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
    machine_link.set_defaults(func=handlers["cmd_link"])

    machine_unlink = machine_sub.add_parser(
        "unlink",
        help="Remove an operator pairing and pause that operator's agents",
    )
    machine_unlink.add_argument(
        "--operator",
        required=True,
        help="Operator slug to unlink",
    )
    machine_unlink.add_argument(
        "--server-url",
        default=None,
        help="Only unlink the pairing on this server URL (default: match by operator).",
    )
    machine_unlink.set_defaults(func=handlers["cmd_unlink"])


def _add_attach_command(sub, handlers: CommandHandlers) -> None:
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
        "--daemon-url",
        dest="daemon_url",
        default="http://127.0.0.1:63387",
        help="Daemon ws-local base URL (default: %(default)s).",
    )
    attach.add_argument(
        "--session-dir",
        default="",
        help=(
            "Pre-create the session work-dir at this path. "
            "Default: a random ``puffo-attach-XXXX`` under the system temp dir."
        ),
    )
    attach.set_defaults(func=handlers["cmd_attach"])


def _add_diagnostic_commands(sub) -> None:
    # macOS Keychain integration probes (no-op on Linux/Windows).
    from .diagnostic import register_test_subcommands

    register_test_subcommands(sub)
