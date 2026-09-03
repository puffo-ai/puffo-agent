"""MCP-side client for the daemon's ``rpc_service``. Host writes go through
the daemon for single-writer semantics; cli-docker reaches the daemon via
``host.docker.internal``."""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Optional

import aiohttp

from ..portal.local_service_auth import local_service_headers

logger = logging.getLogger(__name__)


class PuffoRpcClient:
    """Async client for the daemon's loopback RPC service.
    Transport failures + non-2xx responses raise ``RuntimeError``."""

    def __init__(
        self,
        base_url: str,
        agent_id: str,
        local_service_token: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._headers = local_service_headers(local_service_token)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
            # Match the bare-address repr aiohttp gc-emits on a leak.
            logger.info(
                "aiohttp ClientSession created (class=PuffoRpcClient "
                "base_url=%s agent_id=%s)",
                self.base_url, self.agent_id,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def hello(self, generation: str) -> str:
        """Startup handshake: prove this subprocess can reach the daemon's
        RPC service, tagged with the mcp-config generation that spawned it."""
        return await self._post("mcp-hello", {"generation": generation})

    async def _post(self, route: str, body: dict[str, Any]) -> str:
        """POST + return the ``message`` field. Raises on transport or non-2xx."""
        path = (
            f"/v1/rpc/{urllib.parse.quote(self.agent_id, safe='')}/"
            f"{route.lstrip('/')}"
        )
        url = f"{self.base_url}{path}"
        session = await self._get_session()
        try:
            async with session.post(url, json=body) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    raise RuntimeError(
                        f"rpc {route} returned non-JSON body "
                        f"(status {resp.status}): {text[:500]}"
                    )
                if resp.status >= 400:
                    err = (
                        data.get("error")
                        if isinstance(data, dict) else None
                    )
                    raise RuntimeError(
                        err or f"rpc {route} failed with status {resp.status}"
                    )
                msg = (
                    data.get("message") if isinstance(data, dict) else None
                )
                if not isinstance(msg, str):
                    raise RuntimeError(
                        f"rpc {route} returned a JSON object without a "
                        f"`message` string field"
                    )
                return msg
        except aiohttp.ClientError as exc:
            raise RuntimeError(
                f"rpc {route} transport error: {exc}"
            ) from exc

    async def _post_structured(
        self, route: str, body: dict[str, Any],
    ) -> dict[str, Any]:
        """POST an RPC whose successful response is a structured object.

        This is intentionally separate from ``_post`` so the established
        install/sync/leave/permission ``{"message": str}`` contract cannot
        accidentally change.
        """
        path = (
            f"/v1/rpc/{urllib.parse.quote(self.agent_id, safe='')}/"
            f"{route.lstrip('/')}"
        )
        session = await self._get_session()
        try:
            async with session.post(f"{self.base_url}{path}", json=body) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    raw = await resp.text()
                    raise RuntimeError(
                        f"rpc {route} returned non-JSON body "
                        f"(status {resp.status}): {raw[:500]}"
                    )
                if resp.status >= 400:
                    error = data.get("error") if isinstance(data, dict) else None
                    raise RuntimeError(
                        str(error or f"rpc {route} failed with status {resp.status}")
                    )
                if not isinstance(data, dict):
                    raise RuntimeError(f"rpc {route} returned a non-object result")
                if data.get("state") not in ("sent", "held", "failed"):
                    raise RuntimeError(f"rpc {route} returned an invalid send state")
                if data.get("attempted") is not True:
                    raise RuntimeError(f"rpc {route} omitted attempted=true")
                return data
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"rpc {route} transport error: {exc}") from exc

    async def _post_object(
        self, route: str, body: dict[str, Any],
    ) -> dict[str, Any]:
        """POST a strict object result without inheriting send semantics."""
        path = (
            f"/v1/rpc/{urllib.parse.quote(self.agent_id, safe='')}/"
            f"{route.lstrip('/')}"
        )
        session = await self._get_session()
        try:
            async with session.post(f"{self.base_url}{path}", json=body) as resp:
                try:
                    data = await resp.json()
                except Exception as exc:
                    raise RuntimeError(
                        f"rpc {route} returned non-JSON (status {resp.status})"
                    ) from exc
                if resp.status >= 400:
                    error = data.get("error") if isinstance(data, dict) else None
                    raise RuntimeError(
                        str(error or f"rpc {route} failed ({resp.status})")
                    )
                if not isinstance(data, dict):
                    raise RuntimeError(f"rpc {route} returned a non-object")
                return data
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"rpc {route} transport error: {exc}") from exc

    async def send_message(
        self,
        *,
        channel: str,
        text: str = "",
        paths: Optional[list[str]] = None,
        caption: str = "",
        root_id: str = "",
        visibility_level: str = "default",
        send_anyway: bool = False,
        covers: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "channel": channel,
            "root_id": root_id,
            "visibility_level": visibility_level,
            "send_anyway": send_anyway,
        }
        if paths:
            body.update(paths=paths, caption=caption)
        else:
            body["text"] = text
        if covers:
            body["covers"] = covers
        return await self._post_structured("send-message", body)

    async def stage_model_visible_read(
        self,
        *,
        tool_name: str,
        tool_arguments: dict[str, object],
        visible_message_ids: list[str] | None = None,
        space_id: str | None = None,
        channel_id: str | None = None,
        through_seq: int | None = None,
        through_envelope_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "tool_name": tool_name,
            "tool_arguments": tool_arguments,
        }
        boundary = (space_id, channel_id, through_seq, through_envelope_id)
        has_boundary = all(value not in (None, "") for value in boundary)
        if any(value not in (None, "") for value in boundary) and not has_boundary:
            raise RuntimeError("model-visible channel watermark is incomplete")
        if has_boundary:
            body.update(
                space_id=space_id,
                channel_id=channel_id,
                through_seq=through_seq,
                through_envelope_id=through_envelope_id,
            )
        if visible_message_ids is not None:
            body["visible_message_ids"] = visible_message_ids
        path = (
            f"/v1/rpc/{urllib.parse.quote(self.agent_id, safe='')}/"
            "model-visible-read"
        )
        session = await self._get_session()
        try:
            async with session.post(f"{self.base_url}{path}", json=body) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    raw = await resp.text()
                    raise RuntimeError(
                        "rpc model-visible-read returned non-JSON body "
                        f"(status {resp.status}): {raw[:500]}"
                    )
                if resp.status >= 400:
                    error = data.get("error") if isinstance(data, dict) else None
                    raise RuntimeError(
                        str(
                            error
                            or "rpc model-visible-read failed with "
                            f"status {resp.status}"
                        )
                    )
                if not isinstance(data, dict) or data.get("state") != "staged":
                    raise RuntimeError(
                        "rpc model-visible-read returned an invalid result"
                    )
                return data
        except aiohttp.ClientError as exc:
            raise RuntimeError(
                f"rpc model-visible-read transport error: {exc}"
            ) from exc

    async def read_inbox(
        self, *, target: str = "", cursor: str = "", limit: int = 50
    ) -> dict[str, Any]:
        path = (
            f"/v1/rpc/{urllib.parse.quote(self.agent_id, safe='')}/read-inbox"
        )
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.base_url}{path}",
                json={"target": target, "cursor": cursor, "limit": limit},
            ) as resp:
                try:
                    data = await resp.json()
                except Exception as exc:
                    raise RuntimeError(
                        f"rpc read-inbox returned non-JSON (status {resp.status})"
                    ) from exc
                if resp.status >= 400:
                    raise RuntimeError(
                        str(data.get("error") or f"rpc read-inbox failed ({resp.status})")
                    )
                if not isinstance(data, dict):
                    raise RuntimeError("rpc read-inbox returned a non-object")
                return data
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"rpc read-inbox transport error: {exc}") from exc

    @staticmethod
    def _validate_reminder_object(
        data: dict[str, Any], *, allow_covers: bool = False
    ) -> dict[str, Any]:
        # During a rolling local upgrade, a new MCP subprocess can briefly
        # talk to an older daemon that still exposed this internal state.
        if data.get("state") == "claimed":
            data = {**data, "state": "scheduled", "actual_fire_at": None}
        required = {
            "reminder_id", "occurrence_id", "state", "target", "content",
            "intended_at", "actual_fire_at", "created_at", "cancelled_at",
            "delivered_at",
        }
        # Cover outcomes ride only on the create response; every other
        # reminder surface keeps the strict exact-shape contract.
        optional = {"covers_recorded", "covers_unknown"} if allow_covers else set()
        covers_ok = all(
            isinstance(data[key], list)
            and all(isinstance(item, str) for item in data[key])
            for key in optional & set(data)
        )
        if set(data) - optional != required or not covers_ok or data.get(
            "state"
        ) not in {
            "scheduled", "cancelled", "delivered",
        } or not all(
            isinstance(data.get(key), str)
            for key in (
                "reminder_id", "occurrence_id", "state", "target", "content",
                "intended_at", "created_at",
            )
        ):
            raise RuntimeError("rpc reminder returned an invalid structured result")
        return data

    async def create_reminder(
        self,
        *,
        content: str,
        target: str,
        intended_at: str,
        covers: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "content": content, "target": target, "intended_at": intended_at,
        }
        if covers:
            body["covers"] = covers
        try:
            data = await self._post_object("create-reminder", body)
        except RuntimeError as exc:
            # Rolling local upgrade: an older daemon rejects the covers key
            # wholesale. The deferral matters more than the declaration, so
            # retry without covers and report them as dropped.
            if not covers or "accepts only" not in str(exc):
                raise
            data = await self._post_object(
                "create-reminder",
                {"content": content, "target": target, "intended_at": intended_at},
            )
            data = {**data, "covers_recorded": [], "covers_dropped": list(covers)}
        dropped = data.pop("covers_dropped", None)
        validated = self._validate_reminder_object(data, allow_covers=True)
        if dropped is not None:
            validated = {**validated, "covers_dropped": dropped}
        return validated

    async def mark_covered(
        self,
        *,
        covers: list[str],
        by_message_id: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"covers": covers}
        if by_message_id:
            body["by_message_id"] = by_message_id
        if note:
            body["note"] = note
        try:
            return await self._post_object("mark-covered", body)
        except RuntimeError as exc:
            if "404" in str(exc) or "failed (404)" in str(exc):
                raise RuntimeError(
                    "mark_covered is not available on this daemon yet "
                    "(rolling upgrade in progress); declare covers on the "
                    "send or reminder instead"
                ) from exc
            raise

    async def list_reminders(
        self, *, state: str = "", limit: int = 50,
    ) -> dict[str, Any]:
        data = await self._post_object(
            "list-reminders", {"state": state, "limit": limit},
        )
        reminders = data.get("reminders")
        if set(data) != {"reminders"} or not isinstance(reminders, list):
            raise RuntimeError("rpc list reminders returned an invalid structured result")
        validated: list[dict[str, Any]] = []
        for item in reminders:
            if not isinstance(item, dict):
                raise RuntimeError("rpc list reminders returned an invalid item")
            validated.append(self._validate_reminder_object(item))
        return {
            "reminders": validated,
        }

    async def cancel_reminder(self, *, reminder_id: str) -> dict[str, Any]:
        return self._validate_reminder_object(await self._post_object(
            "cancel-reminder", {"reminder_id": reminder_id},
        ))

    async def replace_reminder(
        self,
        *,
        reminder_id: str,
        content: str = "",
        target: str = "",
        intended_at: str = "",
    ) -> dict[str, Any]:
        data = await self._post_object(
            "replace-reminder",
            {
                "reminder_id": reminder_id,
                "content": content,
                "target": target,
                "intended_at": intended_at,
            },
        )
        if set(data) != {"cancelled", "replacement"}:
            raise RuntimeError("rpc replace reminder returned an invalid result")
        cancelled = data["cancelled"]
        replacement = data["replacement"]
        if not isinstance(cancelled, dict) or not isinstance(replacement, dict):
            raise RuntimeError("rpc replace reminder returned an invalid result")
        return {
            "cancelled": self._validate_reminder_object(cancelled),
            "replacement": self._validate_reminder_object(replacement),
        }

    async def install_mcp(
        self,
        *,
        name: str,
        template_id: str = "",
        spec: Optional[dict[str, Any]] = None,
    ) -> str:
        return await self._post(
            "install-mcp",
            {"name": name, "template_id": template_id, "spec": spec},
        )

    async def sync_mcp(self, *, template_id: str) -> str:
        return await self._post(
            "sync-mcp", {"template_id": template_id},
        )

    async def request_leave(
        self,
        *,
        kind: str,
        space_id: str,
        channel_id: str = "",
        reason: str = "",
    ) -> str:
        return await self._post(
            "leave-request",
            {
                "kind": kind,
                "space_id": space_id,
                "channel_id": channel_id,
                "reason": reason,
            },
        )
