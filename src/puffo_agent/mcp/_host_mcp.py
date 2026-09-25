"""MCP-side client for the daemon's ``rpc_service``. Host writes go through
the daemon for single-writer semantics; cli-docker reaches the daemon via
``host.docker.internal``."""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any, NoReturn, Optional

import aiohttp

from ..portal.local_service_auth import local_service_headers

logger = logging.getLogger(__name__)

_RPC_FAILURE_DETAIL_MAX_CHARS = 500
# Parity with ``agent._logging._TOKENISH``: this tail rides into
# exceptions and logs, so secret-shaped values may not survive raw.
_TOKENISH = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:access|refresh|id)[_-]?token\s*[:=]\s*\S+|"
    r"sk-[a-z0-9_-]{12,}|eyJ[a-zA-Z0-9_-]{12,}\.[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)?)"
)
_SECRET_KEYS = re.compile(
    r"(?i)(?:^|[_-])(?:token|secret|password|passwd|authorization|cookie|"
    r"credential|api[_-]?key|verifier)s?(?:$|[_-])"
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# An OAuth authorization code or CSRF state is an opaque string that no
# shape heuristic can promise to catch (``private_session_nonce`` is a
# perfectly snake-shaped secret), so ``code``/``error_code``/``state``
# values pass through only when they are *known* diagnostic values;
# everything else is redacted. Extend these sets when the daemon starts
# returning new codes — an unlisted code costs one lookup in the daemon
# log, a leaked credential cannot be recalled.
# Every value below is a literal this repo actually returns; extend
# only from real interface contracts, never invented names.
_KNOWN_DIAGNOSTIC_CODES = frozenset({
    # provider failure taxonomy (agent/provider_failures.py)
    "authentication", "permission_denied", "model_not_found",
    "not_entitled", "budget_exceeded", "plan_drained",
    "extra_usage_required", "quota_exhausted", "rate_limit",
    "provider_unavailable", "provider_error", "runtime_exited",
    "resume_unconfirmed", "protocol_error", "cancel_failed", "unknown",
    # control / runtime / receipt codes emitted elsewhere in this repo
    "invalid_command", "agent_start_failed", "agent_start_timeout",
    "command_rejected", "command_failed", "harness_not_ready",
    "runtime_not_ready", "acp_prompt_failed", "cancelled",
    "command_lifecycle_protocol", "invalid_resume",
    "input_admission_ambiguous", "turn_timeout", "runtime_closed",
    "operator_recovery_required", "event_persistence_failed",
    "transport", "malformed_ack", "malformed_response", "capacity",
})
_KNOWN_DIAGNOSTIC_STATES = frozenset({
    # send / staging / reminder lifecycle states on this interface
    "sent", "held", "failed", "staged", "scheduled", "cancelled",
    "delivered", "claimed", "requeued",
})
_QUERY_SECRETS = re.compile(
    r"(?i)([?&#](?:code|state|access_token|refresh_token|id_token|token|"
    r"client_secret|code_verifier)=)[^&#\s\"']+"
)
# Serialization priority under the truncation budget: stable diagnostic
# fields first (in this order), everything else shortest-first.
_DIAGNOSTIC_PRIORITY = (
    "code", "error_code", "state", "reason", "category",
    "message", "hint", "request_id", "retryable",
)


def _sanitize_detail(value: Any, *, key: str = "") -> Any:
    # Normalize camelCase (accessToken) to snake so the separator-based
    # secret-key pattern sees the same shape either way; the whole value
    # is replaced before any recursion, so ``credentials: {...}`` cannot
    # leak through its nested fields.
    lowered = _CAMEL_BOUNDARY.sub("_", key).lower()
    if _SECRET_KEYS.search(lowered):
        return "[REDACTED]"
    if lowered in ("code", "error_code", "state"):
        known = (
            _KNOWN_DIAGNOSTIC_STATES if lowered == "state"
            else _KNOWN_DIAGNOSTIC_CODES
        )
        # These fields' contract is a string enum; any other type is an
        # unknown value, not a loophole.
        if not (isinstance(value, str) and value in known):
            return "[REDACTED]"
    if isinstance(value, dict):
        return {
            k: _sanitize_detail(v, key=str(k)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_detail(item) for item in value]
    return value


def _encode_detail_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


class PuffoRpcError(RuntimeError):
    """A non-2xx RPC response. ``status`` carries the real HTTP status
    and ``error`` the body's headline text, so compatibility probes read
    them structurally instead of scanning the diagnostic message for
    digits (a ``request_id`` like ``req-404-abc`` must not read as 404).
    """

    def __init__(
        self, message: str, *, status: int = 0, route: str = "",
        error: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.route = route
        self.error = error


def _raise_rpc_failure(route: str, status: int, data: Any) -> NoReturn:
    error = data.get("error") if isinstance(data, dict) else None
    raise PuffoRpcError(
        _rpc_failure_message(route, status, data),
        status=status,
        route=route,
        error=error if isinstance(error, str) else "",
    )


def _raise_non_json_failure(route: str, status: int, raw: str) -> NoReturn:
    """A non-JSON body still carries a real HTTP status — an old daemon
    with no such route answers with aiohttp's default text/plain 404, and
    the rolling-upgrade probes must still see ``status`` structurally."""
    message = f"rpc {route} returned non-JSON body (status {status})"
    if raw:
        message = f"{message}: {_redact_detail_text(raw)[:500]}"
    if status >= 400:
        raise PuffoRpcError(message, status=status, route=route)
    raise RuntimeError(message)


def _redact_detail_text(text: str) -> str:
    text = _TOKENISH.sub("[REDACTED]", text)
    return _QUERY_SECRETS.sub(r"\g<1>[REDACTED]", text)


def _rpc_failure_message(route: str, status: int, data: Any) -> str:
    """Carry the daemon's whole failure body into the raised error.

    A 4xx body is the diagnosis — re-auth needed vs cloud config vs an
    operator denial — so beyond the ``error`` headline every remaining
    field rides along as a bounded JSON tail instead of being discarded.
    Stable diagnostic fields serialize first so codes and reasons survive
    the truncation budget regardless of how many other fields the body
    carries; credential-named keys, secret-shaped values, opaque
    ``code``/``state`` values, and secret URL query params are redacted.
    """
    headline = f"rpc {route} failed with status {status}"
    error = data.get("error") if isinstance(data, dict) else None
    headlined_error = isinstance(error, str) and bool(error)
    if headlined_error:
        headline = f"{headline}: {_redact_detail_text(error)}"
    if isinstance(data, dict):
        residue: Any = {
            key: _sanitize_detail(value, key=str(key))
            for key, value in data.items()
            if (key != "error" or not headlined_error)
            and value not in (None, "")
        }
    else:
        residue = _sanitize_detail(data)
    if residue in (None, "", {}, []):
        return headline
    if isinstance(residue, dict):
        rank = {name: idx for idx, name in enumerate(_DIAGNOSTIC_PRIORITY)}
        fields = [
            (
                str(key).lower(),
                f"{_encode_detail_value(str(key))}:{_encode_detail_value(value)}",
            )
            for key, value in residue.items()
        ]
        fields.sort(
            key=lambda item: (rank.get(item[0], len(rank)), len(item[1])),
        )
        tail = "{" + ",".join(encoded for _, encoded in fields) + "}"
    else:
        tail = _encode_detail_value(residue)
    tail = _redact_detail_text(tail)
    return f"{headline} detail={tail[:_RPC_FAILURE_DETAIL_MAX_CHARS]}"


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

    async def hello(
        self, generation: str, *, beacon_interval: float | None = None,
    ) -> str:
        """Hello beacon: prove this subprocess can reach the daemon's
        RPC service, tagged with the mcp-config generation that spawned
        it. ``beacon_interval`` declares the re-hello cadence so the
        daemon may read sustained silence as a wedged transport."""
        body: dict[str, Any] = {"generation": generation}
        if beacon_interval is not None:
            body["beacon_interval"] = beacon_interval
        return await self._post("mcp-hello", body)

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
                    _raise_non_json_failure(
                        route, resp.status, await resp.text(),
                    )
                if resp.status >= 400:
                    _raise_rpc_failure(route, resp.status, data)
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
                    _raise_non_json_failure(
                        route, resp.status, await resp.text(),
                    )
                if resp.status >= 400:
                    _raise_rpc_failure(route, resp.status, data)
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
                except Exception:
                    _raise_non_json_failure(route, resp.status, "")
                if resp.status >= 400:
                    _raise_rpc_failure(route, resp.status, data)
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
                    _raise_non_json_failure(
                        "model-visible-read", resp.status, await resp.text(),
                    )
                if resp.status >= 400:
                    _raise_rpc_failure("model-visible-read", resp.status, data)
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
                except Exception:
                    _raise_non_json_failure("read-inbox", resp.status, "")
                if resp.status >= 400:
                    _raise_rpc_failure("read-inbox", resp.status, data)
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
        except PuffoRpcError as exc:
            # Rolling local upgrade: an older daemon rejects the covers key
            # wholesale. The deferral matters more than the declaration, so
            # retry without covers and report them as dropped. Probe the
            # body's own error text, never the full diagnostic message.
            if not covers or "accepts only" not in exc.error:
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
        except PuffoRpcError as exc:
            # Judge by the real HTTP status — a request_id like
            # "req-404-abc" in the diagnostic tail must not read as 404.
            if exc.status == 404:
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
