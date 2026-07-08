"""Bridge-only sandbox-lifecycle MCP tools (T23).

Registered *only* for cloud (bridge-transport) agents — gated on
``cfg.bridge_client is not None`` at the ``register_core_tools`` call
site — and exposed over the ws-local dispatch allowlist
(``WS_LOCAL_ALLOWED_TOOLS``). A native/desktop agent (signed-crypto
transport, ``bridge_client is None``) never registers them, so this
whole surface is invisible to native agents.

Every tool is a thin, fail-soft wrapper over the ``CloudBridgeClient``
keyless ``x-sandbox-token`` lifecycle methods: a failing lifecycle call
returns a plain error string the model can reason about, never an
exception that would crash the turn. The server owns all wake / sleep
state — these tools are stateless request/response calls, no
persistence or background scheduling on the agent side.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent.bridge_client import BridgeClosed, BridgeError

logger = logging.getLogger(__name__)


def register_lifecycle_tools(mcp: FastMCP, cfg: Any) -> None:
    """Register the five cloud-lifecycle tools on ``mcp``. Call only
    when ``cfg.bridge_client is not None`` — each tool also defends the
    gate itself so a stray registration can't act on a native agent."""

    @mcp.tool()
    async def schedule_wake(
        after_seconds: int = 0,
        wake_at: str = "",
        reason: str = "",
    ) -> str:
        """Schedule a server-side wake so a long task self-resumes after
        your cloud sandbox auto-sleeps. Cloud (bridge) agents only.

        Your sandbox is put to sleep after an idle timeout; a scheduled
        wake tells the server to bring it back so an in-flight task keeps
        going instead of stalling until the next inbound message.

        Pass EXACTLY ONE of:
          - ``after_seconds`` — wake this many seconds from now (relative).
          - ``wake_at`` — an absolute ISO-8601 timestamp to wake at.
        ``reason`` is an optional short note stored with the wake.

        Returns the confirmed wake time.
        """
        if cfg.bridge_client is None:
            return "schedule_wake is only available to cloud (bridge) agents."
        # F7: a raw ws-local dispatch can hand us after_seconds as a string
        # (e.g. {"after_seconds": "600"}). Coerce to int before the > 0
        # compare — mirrors keep_alive — so a stringy value schedules
        # cleanly instead of raising TypeError deeper in the compare.
        if after_seconds in (None, "", 0, "0"):
            after_val = 0
        else:
            try:
                after_val = int(after_seconds)
            except (TypeError, ValueError):
                return (
                    "schedule_wake: after_seconds must be an integer "
                    "number of seconds."
                )
        has_after = after_val > 0
        has_at = bool(wake_at and wake_at.strip())
        if has_after and has_at:
            return (
                "schedule_wake: pass exactly one of after_seconds or "
                "wake_at, not both."
            )
        if not has_after and not has_at:
            return (
                "schedule_wake: pass one of after_seconds (>0) or wake_at "
                "(an ISO-8601 timestamp)."
            )
        try:
            result = await cfg.bridge_client.schedule_wake(
                after_seconds=after_val if has_after else None,
                wake_at=wake_at.strip() if has_at else None,
                reason=reason,
            )
        except (BridgeError, BridgeClosed, Exception) as exc:  # noqa: BLE001
            return (
                "schedule_wake failed (x-sandbox-token POST "
                f"/v2/cloud-agents/schedule-wake): {exc}"
            )
        result = result or {}
        confirmed = result.get("wake_at") or "?"
        note = result.get("reason") or reason
        tail = f" (reason: {note})" if note else ""
        return f"wake scheduled for {confirmed}{tail}"

    @mcp.tool()
    async def cancel_wake() -> str:
        """Cancel a previously scheduled wake for this cloud sandbox.
        Cloud (bridge) agents only. Safe to call when nothing is
        scheduled — the server treats that as a no-op."""
        if cfg.bridge_client is None:
            return "cancel_wake is only available to cloud (bridge) agents."
        try:
            await cfg.bridge_client.cancel_wake()
        except (BridgeError, BridgeClosed, Exception) as exc:  # noqa: BLE001
            return (
                "cancel_wake failed (x-sandbox-token DELETE "
                f"/v2/cloud-agents/scheduled-wake): {exc}"
            )
        return "scheduled wake cancelled."

    @mcp.tool()
    async def get_scheduled_wake() -> str:
        """Show the wake currently scheduled for this cloud sandbox, if
        any. Cloud (bridge) agents only."""
        if cfg.bridge_client is None:
            return (
                "get_scheduled_wake is only available to cloud (bridge) "
                "agents."
            )
        try:
            result = await cfg.bridge_client.get_scheduled_wake()
        except (BridgeError, BridgeClosed, Exception) as exc:  # noqa: BLE001
            return (
                "get_scheduled_wake failed (x-sandbox-token GET "
                f"/v2/cloud-agents/scheduled-wake): {exc}"
            )
        result = result or {}
        wake_at = result.get("wake_at")
        if not wake_at:
            return "no wake is currently scheduled."
        reason = result.get("reason") or ""
        tail = f" (reason: {reason})" if reason else ""
        return f"wake scheduled for {wake_at}{tail}"

    @mcp.tool()
    async def get_runtime_status() -> str:
        """Report this cloud sandbox's runtime state and how long until
        it auto-sleeps. Cloud (bridge) agents only.

        ``seconds_until_sleep`` is shown as ``unknown`` when the server
        can't compute it — never a fabricated number. Use ``keep_alive``
        to push the deadline back, or ``schedule_wake`` to self-resume
        after a sleep."""
        if cfg.bridge_client is None:
            return (
                "get_runtime_status is only available to cloud (bridge) "
                "agents."
            )
        try:
            result = await cfg.bridge_client.runtime_status()
        except (BridgeError, BridgeClosed, Exception) as exc:  # noqa: BLE001
            return (
                "get_runtime_status failed (x-sandbox-token GET "
                f"/v2/cloud-agents/runtime-status): {exc}"
            )
        result = result or {}
        state = result.get("state") or "unknown"
        sandbox_id = result.get("sandbox_id") or "?"
        timeout_at = result.get("timeout_at") or "unknown"
        secs = result.get("seconds_until_sleep")
        # ``null``/absent → the literal word "unknown"; a real number
        # (including 0) renders as itself. Never guess a value.
        secs_str = "unknown" if secs is None else str(secs)
        return (
            f"sandbox {sandbox_id}: state={state}, "
            f"seconds_until_sleep={secs_str}, timeout_at={timeout_at}"
        )

    @mcp.tool()
    async def keep_alive(seconds: int = 600) -> str:
        """Push back this cloud sandbox's auto-sleep deadline by roughly
        ``seconds``. Cloud (bridge) agents only.

        Use this to hold the sandbox awake while a long task runs. If the
        upstream deadline-refresh isn't available yet, this automatically
        falls back to scheduling a wake ~``seconds`` out, so the task
        still self-resumes even if the sandbox sleeps. Returns what
        happened either way."""
        if cfg.bridge_client is None:
            return "keep_alive is only available to cloud (bridge) agents."
        try:
            secs = int(seconds)
        except (TypeError, ValueError):
            return "keep_alive: seconds must be an integer number of seconds."
        if secs <= 0:
            return "keep_alive: seconds must be > 0."
        try:
            result = await cfg.bridge_client.keepalive(secs)
        except (BridgeError, BridgeClosed, Exception) as exc:  # noqa: BLE001
            return (
                "keep_alive failed (x-sandbox-token POST "
                f"/v2/cloud-agents/keepalive): {exc}"
            )
        result = result or {}
        if result.get("available"):
            timeout_at = result.get("timeout_at") or "?"
            secs_left = result.get("seconds_until_sleep")
            secs_str = "unknown" if secs_left is None else str(secs_left)
            return (
                f"keepalive ok — auto-sleep pushed to {timeout_at} "
                f"(seconds_until_sleep={secs_str})."
            )
        # Upstream deadline-refresh not landed yet — fall back to a
        # scheduled wake so a long task still self-resumes if the sandbox
        # sleeps before it finishes.
        detail = result.get("detail") or "keepalive unavailable upstream"
        try:
            wake = await cfg.bridge_client.schedule_wake(
                after_seconds=secs, reason="keepalive fallback",
            )
        except (BridgeError, BridgeClosed, Exception) as exc:  # noqa: BLE001
            return (
                f"keep_alive: upstream keepalive is unavailable ({detail}) "
                f"and the schedule_wake fallback also failed: {exc}. The "
                "sandbox may sleep without resuming — retry keep_alive or "
                "schedule_wake."
            )
        wake_at = (wake or {}).get("wake_at") or f"~{secs}s from now"
        return (
            f"keepalive is not available upstream yet ({detail}); scheduled "
            f"a wake at {wake_at} (~{secs}s) as a fallback so this task "
            "self-resumes if the sandbox sleeps."
        )
