"""Agent-scoped management of server-owned prompt schedules."""
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent.managed_schedules import ScheduleAPI


def register_schedule_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def create_schedule(
        name: str, prompt: str, next_run_at: str,
        interval_seconds: int | None = None, enabled: bool = True,
    ) -> dict[str, Any]:
        """Schedule a prompt on the server for this agent.

        next_run_at is a future RFC3339 timestamp with timezone. Omit
        interval_seconds for one run; recurring intervals are at least 60
        seconds. Prompts are signed and encrypted before storage; Puffo's
        trusted cloud-agent bridge handles crypto for keyless agents. Missed
        runs coalesce. WebSocket delivery enters the owner's DM Inbox as a
        system event. New recipient devices may require re-saving the task.
        """
        return await ScheduleAPI(cfg.http_client, cfg.slug).create({
            "name": name, "prompt": prompt, "next_run_at": next_run_at,
            "interval_seconds": interval_seconds, "enabled": enabled,
        })

    @mcp.tool()
    async def list_schedules() -> dict[str, Any]:
        """List this agent's server schedules, including paused and completed tasks."""
        return await ScheduleAPI(cfg.http_client, cfg.slug).list()

    @mcp.tool()
    async def get_schedule(schedule_id: str) -> dict[str, Any]:
        """Read one schedule and its current version before editing it."""
        return await ScheduleAPI(cfg.http_client, cfg.slug).read(schedule_id)

    @mcp.tool()
    async def update_schedule(
        schedule_id: str, version: int, name: str, prompt: str,
        next_run_at: str, interval_seconds: int | None = None, enabled: bool = True,
    ) -> dict[str, Any]:
        """Replace a schedule using its latest version; stale edits fail.

        Supply a future next_run_at, including when pausing. Changes cancel
        future triggers; already queued messages can still arrive.
        """
        return await ScheduleAPI(cfg.http_client, cfg.slug).update(schedule_id, version, {
            "name": name, "prompt": prompt, "next_run_at": next_run_at,
            "interval_seconds": interval_seconds, "enabled": enabled,
        })

    @mcp.tool()
    async def delete_schedule(schedule_id: str, version: int) -> dict[str, bool]:
        """Delete a schedule at its current version. Already queued messages may arrive."""
        return await ScheduleAPI(cfg.http_client, cfg.slug).delete(schedule_id, version)
