"""Compact, best-effort channel audience context for Inbox notices."""

from __future__ import annotations

import asyncio
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

AUDIENCE_READ_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class ChannelAudience:
    agent_count: int
    human_count: int
    online_agent_count: int | None

    def projection(self) -> dict[str, int]:
        result = {"agents": self.agent_count}
        if self.online_agent_count is not None:
            result["online_agents"] = self.online_agent_count
        result["humans"] = self.human_count
        return result


ChannelAudienceLoader = Callable[[str, str], Awaitable[ChannelAudience | None]]


async def load_channel_audiences(
    loader: ChannelAudienceLoader | None,
    channels: Iterable[tuple[str, str]],
    *,
    log: Any,
) -> dict[tuple[str, str], ChannelAudience]:
    if loader is None:
        return {}
    unique_channels = tuple(dict.fromkeys(channels))
    results = await asyncio.gather(
        *(
            asyncio.wait_for(
                loader(space_id, channel_id),
                timeout=AUDIENCE_READ_TIMEOUT_SECONDS,
            )
            for space_id, channel_id in unique_channels
        ),
        return_exceptions=True,
    )
    audiences: dict[tuple[str, str], ChannelAudience] = {}
    for channel, result in zip(unique_channels, results, strict=True):
        if isinstance(result, ChannelAudience):
            audiences[channel] = result
        elif isinstance(result, BaseException):
            log.debug(
                "channel audience loader failed for %s/%s: %s",
                channel[0],
                channel[1],
                type(result).__name__,
            )
    return audiences


def project_notice_targets(
    counts: dict[str, int],
    target_channels: dict[str, tuple[str, str]],
    audiences: dict[tuple[str, str], ChannelAudience],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target, count in counts.items():
        row: dict[str, Any] = {"target": target, "count": count}
        channel = target_channels.get(target)
        audience = audiences.get(channel) if channel is not None else None
        if audience is not None:
            row["audience"] = audience.projection()
        rows.append(row)
    return rows


async def read_channel_audience(
    space_id: str,
    channel_id: str,
    *,
    http: Any,
    log: Any,
) -> ChannelAudience | None:
    """Read one authorized roster and reduce it before provider exposure."""
    quoted_space = urllib.parse.quote(space_id, safe="")
    quoted_channel = urllib.parse.quote(channel_id, safe="")
    path = f"/spaces/{quoted_space}/channels/{quoted_channel}/members"
    try:
        if bool(getattr(http, "keyless", False)):
            data = await http.get_unsigned(f"/v2/cloud-agents{path}")
        else:
            data = await http.get(path)
    except Exception as exc:  # noqa: BLE001 - optional context must not block Inbox
        log.debug(
            "channel audience unavailable for %s/%s: %s",
            space_id,
            channel_id,
            type(exc).__name__,
        )
        return None
    if not isinstance(data, dict) or not isinstance(data.get("members"), list):
        return None
    members = [member for member in data["members"] if isinstance(member, dict)]
    agents = [member for member in members if member.get("identity_type") == "agent"]
    humans = [member for member in members if member.get("identity_type") == "human"]
    online_values = [member.get("online") for member in agents]
    online_count = (
        sum(value is True for value in online_values)
        if all(isinstance(value, bool) for value in online_values)
        else None
    )
    return ChannelAudience(
        agent_count=len(agents),
        human_count=len(humans),
        online_agent_count=online_count,
    )
