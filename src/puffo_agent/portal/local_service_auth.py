"""Per-Agent authorization for daemon-owned local HTTP services."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from aiohttp import web

LOCAL_SERVICE_TOKEN_ENV = "PUFFO_LOCAL_SERVICE_TOKEN"
_SERVICE_SECRET = secrets.token_bytes(32)


def issue_local_service_token(agent_id: str) -> str:
    """Return a process-ephemeral token scoped to one Agent id."""
    normalized = agent_id.strip()
    if not normalized:
        raise ValueError("agent_id is required for local service authorization")
    return hmac.new(
        _SERVICE_SECRET,
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def read_local_service_token() -> str:
    """Read the token passed to one MCP subprocess."""
    return os.environ.get(LOCAL_SERVICE_TOKEN_ENV, "").strip()


def local_service_headers(token: str) -> dict[str, str]:
    """Build request headers without retaining an empty credential."""
    return {"Authorization": f"Bearer {token}"} if token else {}


@web.middleware
async def require_local_service_auth(
    request: web.Request,
    handler,
) -> web.StreamResponse:
    """Reject missing, malformed, or cross-Agent bearer credentials."""
    agent_id = request.match_info.get("agent_id", "")
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    valid = bool(
        agent_id
        and separator
        and scheme.lower() == "bearer"
        and token
        and hmac.compare_digest(token, issue_local_service_token(agent_id))
    )
    if not valid:
        return web.json_response(
            {"error": "local service authorization required"},
            status=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await handler(request)
