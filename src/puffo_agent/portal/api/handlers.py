"""HTTP handlers for the bridge.

All endpoints live under ``/v1/`` for forward versioning.

Path-traversal safety on file endpoints uses ``Path.resolve()`` +
``Path.is_relative_to(workspace)`` — catches ``../`` walks, absolute
paths, and escaping symlinks. Symlinks pointing inside the workspace
are allowed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from ...crypto.http_auth import VerifyError, is_timestamp_fresh, verify_request
from ...crypto.encoding import base64url_decode
from ..state import (
    AgentConfig,
    PuffoCoreConfig,
    RuntimeConfig,
    RuntimeState,
    TriggerRules,
    agent_claude_user_dir,
    agent_dir,
    agent_yml_path,
    delete_flag_path,
    discover_agents,
    is_valid_agent_id,
    restart_flag_path,
)
from .certs import (
    CertError, verify_device_cert, verify_identity_cert, verify_slug_binding,
)
from .ownership import is_owner
from .pairing import Pairing, clear_pairing, load_pairing, now_ms, save_pairing

logger = logging.getLogger(__name__)


# Hard cap on file-endpoint responses. Anything bigger is almost
# certainly a binary the user didn't mean to open via the bridge.
MAX_FILE_BYTES = 1 * 1024 * 1024
BINARY_PROBE_BYTES = 4096

# Mirrors ``MAX_AVATAR_BYTES`` in the web client's
# ``ui/services/profile-service.ts`` — keep both sides in lockstep
# or the UI lets a file through that the bridge then rejects.
MAX_AVATAR_BYTES = 4 * 1024 * 1024
MAX_AVATAR_LABEL = "4 MiB"


# ────────────────────────────────────────────────────────────────────
# /v1/info
# ────────────────────────────────────────────────────────────────────


async def info(_request: web.Request) -> web.Response:
    """Public discovery endpoint. No auth."""
    pairing = load_pairing()
    try:
        from importlib.metadata import version
        daemon_version = version("puffo-agent")
    except Exception:
        daemon_version = "unknown"
    return web.json_response({
        "service": "puffo-agent-bridge",
        "version": "v1",
        "daemon_version": daemon_version,
        "pid": os.getpid(),
        "agent_count": len(discover_agents()),
        "paired": pairing is not None,
        "paired_slug": pairing.slug if pairing else None,
        "paired_device_id": pairing.device_id if pairing else None,
    })


# ────────────────────────────────────────────────────────────────────
# /v1/pair
# ────────────────────────────────────────────────────────────────────


def _pair_reject(reason: str, **fields) -> web.Response:
    """Log + return a 400 with structured detail so operators can see
    which check failed in the daemon log."""
    extra = " ".join(f"{k}={v!r}" for k, v in fields.items())
    logger.warning("bridge: pair rejected: %s%s", reason, f" {extra}" if extra else "")
    return _bad(reason)


async def pair(request: web.Request) -> web.Response:
    """Pair this daemon to a (slug, device_id).

    Body: ``{ identity_cert, device_cert, slug_binding }``. Headers
    carry the standard ``x-puffo-*`` set; signature is over the body
    using ``device_cert.keys.signing.public_key``. Verification
    order:

      1. Both certs self-verify against ``root_public_key``.
      2. ``slug_binding.slug`` == ``x-puffo-slug``.
      3. ``identity_cert.identity_type`` == ``human`` (agents can't
         pair to drive themselves).
      4. ``device_cert.device_id`` == ``x-puffo-signer-id``.
      5. Request signature verifies against the device signing key.

    A successful POST overwrites any existing pairing — equivalent
    to ``puffo-agent pairing unpair`` followed by re-pair. Letting
    both paths win means the web client can take over a daemon
    without dropping the operator to a terminal.
    """
    body = await request.read()
    try:
        payload = await request.json()
    except Exception:
        return _pair_reject("body must be JSON")
    identity_cert = payload.get("identity_cert")
    device_cert = payload.get("device_cert")
    slug_binding = payload.get("slug_binding")
    if (
        not isinstance(identity_cert, dict)
        or not isinstance(device_cert, dict)
        or not isinstance(slug_binding, dict)
    ):
        return _pair_reject(
            "identity_cert, device_cert, and slug_binding all required",
            identity_cert_type=type(identity_cert).__name__,
            device_cert_type=type(device_cert).__name__,
            slug_binding_type=type(slug_binding).__name__,
        )

    try:
        root_pk = verify_identity_cert(identity_cert)
    except CertError as exc:
        return _pair_reject(
            f"identity_cert: {exc}",
            cert_keys=sorted(identity_cert.keys()) if isinstance(identity_cert, dict) else None,
        )
    try:
        device_signing_pk = verify_device_cert(device_cert, root_pk)
    except CertError as exc:
        return _pair_reject(
            f"device_cert: {exc}",
            cert_keys=sorted(device_cert.keys()) if isinstance(device_cert, dict) else None,
        )
    try:
        # Returns the disambiguated slug ("alice-a62c") — the form
        # used by the chat protocol, not the bare username.
        cert_slug = verify_slug_binding(slug_binding, root_pk)
    except CertError as exc:
        return _pair_reject(
            f"slug_binding: {exc}",
            binding_keys=sorted(slug_binding.keys()) if isinstance(slug_binding, dict) else None,
        )

    if identity_cert.get("identity_type") != "human":
        return _pair_reject(
            "identity_type must be 'human'",
            got=identity_cert.get("identity_type"),
        )

    cert_device_id = device_cert.get("device_id")
    hdr_slug = request.headers.get("x-puffo-slug", "")
    hdr_signer_id = request.headers.get("x-puffo-signer-id", "")
    hdr_ts = request.headers.get("x-puffo-timestamp", "")
    hdr_nonce = request.headers.get("x-puffo-nonce", "")
    hdr_sig = request.headers.get("x-puffo-signature", "")

    if cert_slug != hdr_slug:
        return _pair_reject(
            "slug_binding.slug does not match x-puffo-slug",
            cert_slug=cert_slug, hdr_slug=hdr_slug,
        )
    if cert_device_id != hdr_signer_id:
        return _pair_reject(
            "device_cert.device_id does not match x-puffo-signer-id",
            cert_device_id=cert_device_id, hdr_signer_id=hdr_signer_id,
        )
    if not is_timestamp_fresh(hdr_ts):
        return _pair_reject("stale x-puffo-timestamp", ts=hdr_ts)
    if not hdr_nonce or not hdr_sig:
        return _pair_reject("missing x-puffo-nonce or x-puffo-signature")

    try:
        verify_request(
            public_key=device_signing_pk,
            method=request.method,
            path=request.path_qs,
            timestamp=hdr_ts,
            nonce=hdr_nonce,
            body=body,
            signature_b64=hdr_sig,
        )
    except VerifyError as exc:
        return _pair_reject(
            f"signature: {exc}",
            method=request.method, path=request.path_qs,
        )

    existing = load_pairing()
    replaced_existing = (
        existing is not None
        and (existing.slug != cert_slug or existing.device_id != cert_device_id)
    )

    from ...crypto.encoding import base64url_encode
    pairing = Pairing(
        slug=cert_slug,
        device_id=cert_device_id,
        root_public_key=base64url_encode(root_pk),
        device_signing_public_key=base64url_encode(device_signing_pk),
        identity_cert=identity_cert,
        device_cert=device_cert,
        paired_at=now_ms(),
    )
    save_pairing(pairing)
    if replaced_existing:
        logger.info(
            "bridge: replaced pairing prev_slug=%s prev_device_id=%s -> slug=%s device_id=%s",
            existing.slug, existing.device_id, cert_slug, cert_device_id,
        )
    else:
        logger.info("bridge: paired with slug=%s device_id=%s", cert_slug, cert_device_id)
    return web.json_response({
        "paired_slug": pairing.slug,
        "paired_device_id": pairing.device_id,
        "paired_at": pairing.paired_at,
    })


# ────────────────────────────────────────────────────────────────────
# /v1/agents and friends
# ────────────────────────────────────────────────────────────────────


async def list_agents(request: web.Request) -> web.Response:
    paired_root = request["paired_root_pubkey"]
    items: list[dict] = []
    for aid in discover_agents():
        # Skip agents pending tear-down — the reconcile loop runs
        # filesystem cleanup ~2s later, but the UI's optimistic
        # delete + refresh would otherwise flicker the card back on.
        if delete_flag_path(aid).exists():
            continue
        try:
            cfg = AgentConfig.load(aid)
        except Exception as exc:
            items.append({"id": aid, "error": str(exc)})
            continue
        rs = RuntimeState.load(aid)
        # Override runtime_status to ``restarting`` while a restart
        # flag is pending so the UI shows a busy state immediately
        # instead of the pre-flag ``running`` snapshot.
        restart_pending = restart_flag_path(aid).exists()
        rs_status = "restarting" if restart_pending else (rs.status if rs else "unknown")
        items.append({
            "id": aid,
            "display_name": cfg.display_name,
            "avatar_url": cfg.avatar_url,
            "puffo_core_slug": cfg.puffo_core.slug,
            "space_id": cfg.puffo_core.space_id,
            "profile_summary": _profile_summary(cfg),
            "state": cfg.state,
            "runtime_kind": cfg.runtime.kind,
            "runtime_status": rs_status,
            "runtime_health": rs.health if rs else "unknown",
            "msg_count": rs.msg_count if rs else 0,
            "owned": is_owner(aid, paired_root),
            # Operator slug who created the agent. Empty string for
            # agent.yml files written before this field existed; UI
            # degrades to the ``owned`` boolean alone.
            "operator_slug": cfg.puffo_core.operator_slug or "",
        })
    return web.json_response({"agents": items})


def _profile_summary(cfg: AgentConfig) -> str:
    """Best-effort 1-line summary for list rows. Prefers the first
    non-heading line under a description-like section; falls back to
    the first non-heading line anywhere; returns empty string if the
    profile is unreadable."""
    try:
        text = cfg.resolve_profile_path().read_text(encoding="utf-8")
    except Exception:
        return ""

    lines = [line.strip() for line in text.splitlines()]
    description_heading = {"soul", "description", "about", "summary"}
    in_description = False
    fallback = ""

    for line in lines:
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            in_description = heading in description_heading
            continue
        if in_description:
            return line
        if not fallback:
            fallback = line

    return fallback


async def get_agent(request: web.Request) -> web.Response:
    agent_id = request.match_info["id"]
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")
    cfg = AgentConfig.load(agent_id)
    rs = RuntimeState.load(agent_id)
    paired_root = request["paired_root_pubkey"]
    owned = is_owner(agent_id, paired_root)
    runtime_dict: dict[str, Any] = {
        "kind": cfg.runtime.kind,
        "provider": cfg.runtime.provider,
        "model": cfg.runtime.model,
        "harness": cfg.runtime.harness,
        "permission_mode": cfg.runtime.permission_mode,
        "max_turns": cfg.runtime.max_turns,
        "allowed_tools": list(cfg.runtime.allowed_tools),
        "docker_image": cfg.runtime.docker_image,
        # Only owners see the actual key. Non-owners get a boolean
        # so the UI can still render "(set)" / "(inherit)".
        "api_key": cfg.runtime.api_key if owned else None,
        "api_key_set": bool(cfg.runtime.api_key),
    }
    return web.json_response({
        "id": cfg.id,
        "display_name": cfg.display_name,
        "avatar_url": cfg.avatar_url,
        "state": cfg.state,
        "owned": owned,
        "puffo_core": {
            "server_url": cfg.puffo_core.server_url,
            "slug": cfg.puffo_core.slug,
            "device_id": cfg.puffo_core.device_id,
            "space_id": cfg.puffo_core.space_id,
        },
        "runtime": runtime_dict,
        "triggers": {
            "on_mention": cfg.triggers.on_mention,
            "on_dm": cfg.triggers.on_dm,
        },
        "profile_path": str(cfg.resolve_profile_path()),
        "memory_dir": str(cfg.resolve_memory_dir()),
        "workspace_dir": str(cfg.resolve_workspace_dir()),
        "created_at": cfg.created_at,
        "runtime_state": _runtime_state_dict(rs),
    })


async def update_runtime(request: web.Request) -> web.Response:
    """Patch the agent's runtime block. Owner-only.

    Accepts any subset of: ``kind``, ``provider``, ``model``,
    ``api_key``, ``permission_mode``, ``allowed_tools``,
    ``docker_image``, ``max_turns``. Missing fields are untouched.
    ``harness`` is intentionally not editable here — switching
    harness usually requires host-level auth setup too.

    The reconcile loop notices ``runtime`` changed and respawns the
    worker on its next tick.
    """
    agent_id = request.match_info["id"]
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")

    paired_root = request["paired_root_pubkey"]
    if not is_owner(agent_id, paired_root):
        return web.json_response(
            {"error": "only the agent's operator can edit runtime"},
            status=403,
        )

    try:
        payload = await request.json()
    except Exception:
        return _bad("body must be JSON")
    if not isinstance(payload, dict):
        return _bad("body must be a JSON object")

    cfg = AgentConfig.load(agent_id)
    rt = cfg.runtime

    # ``harness`` is excluded by design.
    if "kind" in payload:
        rt.kind = str(payload["kind"])
    if "provider" in payload:
        rt.provider = str(payload["provider"])
    if "model" in payload:
        rt.model = str(payload["model"])
    if "api_key" in payload:
        rt.api_key = str(payload["api_key"])
    if "permission_mode" in payload:
        rt.permission_mode = str(payload["permission_mode"])
    if "allowed_tools" in payload:
        tools = payload["allowed_tools"]
        if not isinstance(tools, list):
            return _bad("allowed_tools must be a list of strings")
        rt.allowed_tools = [str(t) for t in tools]
    if "docker_image" in payload:
        rt.docker_image = str(payload["docker_image"])
    if "max_turns" in payload:
        try:
            rt.max_turns = int(payload["max_turns"])
        except (TypeError, ValueError):
            return _bad("max_turns must be an integer")

    # Catch invalid combos here so the worker doesn't crash on the
    # next reconcile tick.
    from ..runtime_matrix import validate_triple
    result = validate_triple(rt.kind, rt.provider, rt.harness)
    if not result.ok:
        return _bad(f"runtime: {result.error}")

    cfg.save()
    logger.info(
        "bridge: updated runtime for agent=%s kind=%s provider=%s model=%s",
        agent_id, rt.kind, rt.provider, rt.model or "(default)",
    )
    return web.json_response({
        "agent_id": agent_id,
        "runtime": {
            "kind": rt.kind,
            "provider": rt.provider,
            "model": rt.model,
            "api_key_set": bool(rt.api_key),
            "permission_mode": rt.permission_mode,
            "harness": rt.harness,
            "allowed_tools": list(rt.allowed_tools),
            "docker_image": rt.docker_image,
            "max_turns": rt.max_turns,
        },
        "note": "daemon will restart this agent on the next reconcile tick (~2s)",
    })


MAX_ROLE_LEN = 140
MAX_ROLE_SHORT_LEN = 32


async def update_profile(request: web.Request) -> web.Response:
    """Patch the agent's display_name + avatar_url + role. Owner-only.

    Body (all fields optional)::

        {
          "display_name": "Helper Bot",
          "avatar_bytes_b64": "<base64 of a PNG/JPG/GIF>",
          "avatar_content_type": "image/png",
          "role": "coder: main puffo-core coder",
          "role_short": "coder"
        }

    Updates ``agent.yml`` locally and best-effort syncs to puffo-server
    via ``/blobs/upload`` + ``PATCH /identities/self``. On sync
    failure the local agent.yml still gets written so the operator
    can retry without losing what they typed.

    ``role_short`` is normally derived server-side from ``role`` (the
    recommended ``<short>: <description>`` shape). Sending it
    explicitly overrides the derive. ``role_short`` without ``role``
    is a 400 — the server enforces the same rule but it's cheaper to
    catch it before the round-trip.
    """
    import base64

    agent_id = request.match_info["id"]
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")

    paired_root = request["paired_root_pubkey"]
    if not is_owner(agent_id, paired_root):
        return web.json_response(
            {"error": "only the agent's operator can edit profile"},
            status=403,
        )

    try:
        payload = await request.json()
    except Exception:
        return _bad("body must be JSON")
    if not isinstance(payload, dict):
        return _bad("body must be a JSON object")

    cfg = AgentConfig.load(agent_id)
    new_display_name = payload.get("display_name")
    avatar_b64 = payload.get("avatar_bytes_b64")
    new_role = payload.get("role")
    new_role_short = payload.get("role_short")

    # Mirror the server-side INVALID_ROLE_SHORT 400 — keeps clients
    # from sending an inconsistent patch that the server would reject.
    if new_role is None and new_role_short is not None:
        return _bad("role_short cannot be set without role")
    if isinstance(new_role, str) and len(new_role) > MAX_ROLE_LEN:
        return _bad(f"role must be at most {MAX_ROLE_LEN} characters")
    if isinstance(new_role_short, str) and len(new_role_short) > MAX_ROLE_SHORT_LEN:
        return _bad(
            f"role_short must be at most {MAX_ROLE_SHORT_LEN} characters",
        )

    avatar_bytes: bytes | None = None
    if avatar_b64 is not None:
        if not isinstance(avatar_b64, str):
            return _bad("avatar_bytes_b64 must be a base64 string")
        try:
            avatar_bytes = base64.b64decode(avatar_b64)
        except Exception as exc:
            return _bad(f"avatar_bytes_b64 decode: {exc}")
        if len(avatar_bytes) > MAX_AVATAR_BYTES:
            return _bad(f"avatar exceeds {MAX_AVATAR_LABEL} cap")

    new_avatar_url: str | None = None
    sync_warning: str | None = None
    profile_patch: dict[str, Any] = {}
    if isinstance(new_display_name, str):
        profile_patch["display_name"] = new_display_name.strip()
    if avatar_bytes is not None:
        try:
            new_avatar_url = await _upload_avatar_via_agent_keystore(
                cfg, avatar_bytes,
            )
            profile_patch["avatar_url"] = new_avatar_url
        except Exception as exc:
            # Recoverable: surface but don't block the local write.
            sync_warning = f"avatar upload failed: {exc}"
            logger.warning(
                "bridge: avatar upload failed for agent=%s: %s", agent_id, exc,
            )
    elif "avatar_url" in payload and isinstance(payload["avatar_url"], str):
        # Direct override (e.g. clearing). Synced to server too.
        new_avatar_url = payload["avatar_url"].strip()
        profile_patch["avatar_url"] = new_avatar_url
    if isinstance(new_role, str):
        profile_patch["role"] = new_role
    if isinstance(new_role_short, str):
        profile_patch["role_short"] = new_role_short

    # Best-effort server sync; failure logs but doesn't fail the
    # request since agent.yml is still updated locally.
    if profile_patch:
        try:
            await _sync_agent_profile(cfg, profile_patch)
        except Exception as exc:
            sync_warning = (
                sync_warning or f"profile sync failed: {exc}"
            )
            logger.warning(
                "bridge: profile sync failed for agent=%s: %s", agent_id, exc,
            )

    new_profile_summary = payload.get("profile_summary")
    if isinstance(new_profile_summary, str):
        _update_profile_summary(cfg, new_profile_summary.strip())

    # Write agent.yml last so local state reflects what we asked
    # the server for, even if the sync warned.
    if isinstance(new_display_name, str):
        cfg.display_name = new_display_name.strip() or cfg.display_name
    if new_avatar_url is not None:
        cfg.avatar_url = new_avatar_url
    if isinstance(new_role, str):
        cfg.role = new_role
        # If the caller didn't override role_short, mirror the
        # server-side derive locally so agent.yml stays consistent
        # with what the server stores. The server is still
        # authoritative — this is a best-effort local cache.
        if not isinstance(new_role_short, str):
            cfg.role_short = _derive_role_short(new_role)
    if isinstance(new_role_short, str):
        cfg.role_short = new_role_short
    cfg.save()
    logger.info(
        "bridge: updated profile for agent=%s display_name=%r avatar=%s role_short=%r",
        agent_id, cfg.display_name,
        "(set)" if cfg.avatar_url else "(empty)",
        cfg.role_short,
    )
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "display_name": cfg.display_name,
        "avatar_url": cfg.avatar_url,
        "role": cfg.role,
        "role_short": cfg.role_short,
        "profile_summary": _profile_summary(cfg),
    }
    if sync_warning:
        body["warning"] = sync_warning
    return web.json_response(body)


def _derive_role_short(role: str) -> str:
    """Local mirror of puffo-server's ``derive_role_short``: pull a
    short chip label out of a ``<short>: <description>``-shaped role
    string. Returns ``""`` for any shape the server would also
    reject (no colon, empty prefix, whitespace in prefix, empty
    suffix, prefix > ``MAX_ROLE_SHORT_LEN``). Kept in sync with
    ``profiles::derive_role_short`` in puffo-server."""
    if ":" not in role:
        return ""
    colon_pos = role.index(":")
    candidate = role[:colon_pos].strip()
    rest = role[colon_pos + 1:].strip()
    if not candidate or not rest:
        return ""
    if len(candidate) > MAX_ROLE_SHORT_LEN:
        return ""
    if any(ch.isspace() for ch in candidate):
        return ""
    return candidate


def _update_profile_summary(cfg: AgentConfig, new_summary: str) -> None:
    """Rewrite the description section of profile.md with
    ``new_summary``. Replaces the body of the first matching heading
    (soul / description / about / summary); appends a new ``# Soul``
    section when no such heading exists.
    """
    try:
        path = cfg.resolve_profile_path()
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)

        description_heading = {"soul", "description", "about", "summary"}
        new_lines: list[str] = []
        i = 0
        found = False

        while i < len(lines):
            raw = lines[i]
            stripped = raw.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip().lower()
                if heading in description_heading:
                    new_lines.append(raw)  # keep the heading line
                    i += 1
                    # Skip existing body lines until the next heading or EOF
                    while i < len(lines) and not lines[i].strip().startswith("#"):
                        i += 1
                    # Insert updated summary (single line)
                    new_lines.append(new_summary + "\n")
                    found = True
                    continue
            new_lines.append(raw)
            i += 1

        if not found:
            # No matching heading — append a new Soul section
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.extend(["\n# Soul\n", new_summary + "\n"])

        path.write_text("".join(new_lines), encoding="utf-8")
    except Exception as exc:
        logger.warning("bridge: failed to update profile summary for agent=%s: %s", cfg, exc)


async def _upload_avatar_via_agent_keystore(
    cfg: AgentConfig, avatar_bytes: bytes,
) -> str:
    """Upload bytes to ``/blobs/upload`` signed by the agent's
    subkey; returns the resulting blob URL."""
    from ...crypto.http_client import PuffoCoreHttpClient
    from ...crypto.keystore import KeyStore
    from ...crypto.encoding import base64url_encode

    pc = cfg.puffo_core
    ks = KeyStore.for_agent(cfg.id)
    http = PuffoCoreHttpClient(pc.server_url, ks, pc.slug)
    try:
        # Pre-rotate so a long-idle agent's first upload doesn't fail.
        await http._ensure_subkey()  # noqa: SLF001 — same intra-package use
        signing_key, signer_id = http._load_signing_key()  # noqa: SLF001
        from ...crypto.http_auth import sign_request
        auth = sign_request(
            signing_key, pc.slug, signer_id,
            "POST", "/blobs/upload", avatar_bytes,
        )
        headers = auth.to_dict()
        headers["content-type"] = "application/octet-stream"
        session = await http._get_session()  # noqa: SLF001
        async with session.post(
            f"{http.server_url}/blobs/upload",
            data=avatar_bytes,
            headers=headers,
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"upload HTTP {resp.status}: {await resp.text()}")
            data = await resp.json()
        blob_id = data["blob_id"]
        return f"{http.server_url.rstrip('/')}/blobs/{blob_id}"
    finally:
        await http.close()


async def _sync_agent_profile(cfg: AgentConfig, patch: dict[str, Any]) -> None:
    """PATCH /identities/self signed by the agent's keystore.
    Thin wrapper around ``portal.profile_sync.sync_agent_profile`` so
    the bridge and CLI share the same wire shape."""
    from ..profile_sync import sync_agent_profile
    await sync_agent_profile(cfg, patch)


async def get_runtime_state(request: web.Request) -> web.Response:
    agent_id = request.match_info["id"]
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")
    return web.json_response(_runtime_state_dict(RuntimeState.load(agent_id)))


def _runtime_state_dict(rs: RuntimeState | None) -> dict | None:
    if rs is None:
        return None
    return {
        "status": rs.status,
        "started_at": rs.started_at,
        "updated_at": rs.updated_at,
        "msg_count": rs.msg_count,
        "last_event_at": rs.last_event_at,
        "error": rs.error,
        "health": rs.health,
    }


# ────────────────────────────────────────────────────────────────────
# /v1/agents/{id}/restart (POST) — drops a ``restart.flag`` sentinel
# in the agent dir; the daemon reconciler picks it up on the next
# tick, stops the worker (which auto-respawns because desired_state
# stays ``running``), and removes the flag.
# ────────────────────────────────────────────────────────────────────


async def restart_agent(request: web.Request) -> web.Response:
    agent_id = request.match_info["id"]
    if not is_valid_agent_id(agent_id):
        return _bad("invalid agent id")
    if not agent_dir(agent_id).exists():
        return web.json_response({"error": "agent not found"}, status=404)
    paired_root = request["paired_root_pubkey"]
    if not is_owner(agent_id, paired_root):
        return web.json_response(
            {"error": "only the agent's operator can restart it"},
            status=403,
        )
    flag = restart_flag_path(agent_id)
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("requested", encoding="utf-8")
    except OSError as exc:
        return web.json_response(
            {"error": f"could not write restart flag: {exc}"},
            status=500,
        )
    logger.info("bridge: restart requested for agent=%s", agent_id)
    return web.json_response({
        "agent_id": agent_id,
        "ok": True,
        "note": "daemon will stop + respawn this agent on the next reconcile tick (~2s)",
    })


# ────────────────────────────────────────────────────────────────────
# /v1/agents/{id} (DELETE) — drops a ``delete.flag`` sentinel; the
# reconciler stops the worker and removes the agent dir entirely on
# the next tick. Destructive — no archived/ copy retained.
# ────────────────────────────────────────────────────────────────────


async def delete_agent(request: web.Request) -> web.Response:
    agent_id = request.match_info["id"]
    if not is_valid_agent_id(agent_id):
        return _bad("invalid agent id")
    if not agent_dir(agent_id).exists():
        return web.json_response({"error": "agent not found"}, status=404)
    paired_root = request["paired_root_pubkey"]
    if not is_owner(agent_id, paired_root):
        return web.json_response(
            {"error": "only the agent's operator can delete it"},
            status=403,
        )
    flag = delete_flag_path(agent_id)
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("requested", encoding="utf-8")
    except OSError as exc:
        return web.json_response(
            {"error": f"could not write delete flag: {exc}"},
            status=500,
        )
    logger.info("bridge: delete requested for agent=%s", agent_id)
    return web.json_response({
        "agent_id": agent_id,
        "ok": True,
        "note": "daemon will stop the worker + remove the agent dir on the next reconcile tick (~2s)",
    })


# ────────────────────────────────────────────────────────────────────
# /v1/agents/{id}/log
# ────────────────────────────────────────────────────────────────────


async def get_log(request: web.Request) -> web.Response:
    """Stub — per-agent log capture is not implemented yet. The
    daemon currently writes a single combined stderr stream.
    """
    agent_id = request.match_info["id"]
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")
    return web.json_response({
        "agent_id": agent_id,
        "lines": [],
        "note": (
            "per-agent log capture is not implemented yet — daemon "
            "currently writes a single combined stream. follow-up PR."
        ),
    })


# ────────────────────────────────────────────────────────────────────
# /v1/agents/{id}/files + /files/raw
# ────────────────────────────────────────────────────────────────────


async def list_files(request: web.Request) -> web.Response:
    agent_id = request.match_info["id"]
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")
    rel = request.query.get("path", "") or ""
    cfg = AgentConfig.load(agent_id)
    workspace = cfg.resolve_workspace_dir().resolve()
    target, err = _safe_join(workspace, rel)
    if err is not None:
        return _bad(err)
    if not target.exists():
        return _not_found("path not found")
    if not target.is_dir():
        return _bad("path is not a directory; use /files/raw to read a file")
    entries: list[dict] = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                st = child.stat()
            except OSError:
                continue
            entries.append({
                "name": child.name,
                "kind": "dir" if child.is_dir() else "file",
                "size": int(st.st_size) if child.is_file() else 0,
                "mtime": int(st.st_mtime),
            })
    except OSError as exc:
        return _bad(f"readdir failed: {exc}")
    return web.json_response({
        "agent_id": agent_id,
        "workspace": str(workspace),
        "path": rel,
        "entries": entries,
    })


async def read_file(request: web.Request) -> web.Response:
    agent_id = request.match_info["id"]
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")
    rel = request.query.get("path", "") or ""
    if not rel:
        return _bad("path query param required")
    cfg = AgentConfig.load(agent_id)
    workspace = cfg.resolve_workspace_dir().resolve()
    target, err = _safe_join(workspace, rel)
    if err is not None:
        return _bad(err)
    if not target.exists() or not target.is_file():
        return _not_found("file not found")
    try:
        size = target.stat().st_size
    except OSError as exc:
        return _bad(f"stat failed: {exc}")
    if size > MAX_FILE_BYTES:
        return web.Response(
            status=413,
            text=f"file is {size} bytes (cap {MAX_FILE_BYTES})",
        )
    try:
        with target.open("rb") as f:
            head = f.read(BINARY_PROBE_BYTES)
            if b"\x00" in head:
                return web.Response(status=415, text="binary file")
            rest = f.read()
        raw = head + rest
    except OSError as exc:
        return _bad(f"read failed: {exc}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return web.Response(status=415, text="not utf-8")
    return web.Response(
        text=text,
        content_type="text/plain",
        charset="utf-8",
    )


# ────────────────────────────────────────────────────────────────────
# /v1/pairing (DELETE) — disconnect
# ────────────────────────────────────────────────────────────────────


async def disconnect(request: web.Request) -> web.Response:
    """Clear the daemon's pairing.json. Auth middleware already
    enforced that the caller is the currently paired identity.
    """
    paired_slug = request.get("paired_slug", "?")
    clear_pairing()
    logger.info("bridge: disconnected slug=%s", paired_slug)
    return web.json_response({"disconnected": True})


# ────────────────────────────────────────────────────────────────────
# /v1/agents (POST) — provision a new agent
# ────────────────────────────────────────────────────────────────────


def _create_reject(reason: str, **fields) -> web.Response:
    extra = " ".join(f"{k}={v!r}" for k, v in fields.items())
    logger.warning("bridge: create-agent rejected: %s%s", reason, f" {extra}" if extra else "")
    return _bad(reason)


def _verify_attestation(att: dict, agent_root_pk: bytes, paired_root_pk: bytes) -> None:
    """Verify operator_attestation: signature valid (signed by the
    operator root key) and both pubkeys match the bound expectations.
    Raises ``CertError`` on mismatch.
    """
    from ...crypto.canonical import canonicalize_for_signing
    from ...crypto.primitives import ed25519_verify

    if not isinstance(att, dict):
        raise CertError("operator_attestation must be an object")
    if att.get("type") != "operator_attestation":
        raise CertError(f"unexpected attestation type {att.get('type')!r}")
    op_pk_b64 = att.get("operator_root_public_key")
    agent_pk_b64 = att.get("agent_root_public_key")
    sig_b64 = att.get("signature")
    if not isinstance(op_pk_b64, str) or not isinstance(agent_pk_b64, str) or not isinstance(sig_b64, str):
        raise CertError("operator_attestation missing required fields")

    try:
        op_pk = base64url_decode(op_pk_b64)
        agent_pk = base64url_decode(agent_pk_b64)
        sig = base64url_decode(sig_b64)
    except Exception as exc:
        raise CertError(f"attestation field decode: {exc}") from exc
    if op_pk != paired_root_pk:
        raise CertError("attestation.operator_root_public_key != paired user")
    if agent_pk != agent_root_pk:
        raise CertError("attestation.agent_root_public_key != agent identity_cert")
    canonical = canonicalize_for_signing({k: v for k, v in att.items() if k != "signature"})
    if not ed25519_verify(op_pk, canonical, sig):
        raise CertError("attestation signature verification failed")


async def create_agent(request: web.Request) -> web.Response:
    """Provision a new agent locally.

    The web client has already signed the operator attestation and
    registered the new identity with puffo-server; this handler
    only verifies cryptographic consistency and writes the agent
    dir on disk for the reconcile loop to pick up.

    Body shape::

        {
          "display_name": "...",
          "profile": "<full profile.md text>",
          "puffo_core": {server_url, slug, device_id, space_id},
          "runtime": {kind, provider, model, api_key, harness, permission_mode},
          "identity_bundle": {
            "identity_cert": <obj>,
            "device_cert": <obj>,
            "operator_attestation": <obj>,
            "slug_binding": <obj>,
            "root_secret_key": "<b64url>",
            "device_signing_secret_key": "<b64url>",
            "kem_secret_key": "<b64url>"
          }
        }
    """
    paired_root_pubkey_b64 = request["paired_root_pubkey"]
    try:
        paired_root_pk = base64url_decode(paired_root_pubkey_b64)
    except Exception as exc:
        return _create_reject(f"paired root pubkey decode: {exc}")

    try:
        payload = await request.json()
    except Exception:
        return _create_reject("body must be JSON")
    if not isinstance(payload, dict):
        return _create_reject("body must be a JSON object")

    bundle = payload.get("identity_bundle")
    pc = payload.get("puffo_core")
    rt = payload.get("runtime")
    if not isinstance(bundle, dict) or not isinstance(pc, dict) or not isinstance(rt, dict):
        return _create_reject(
            "identity_bundle, puffo_core, and runtime are required",
            bundle_type=type(bundle).__name__,
            pc_type=type(pc).__name__,
            rt_type=type(rt).__name__,
        )

    identity_cert = bundle.get("identity_cert")
    device_cert = bundle.get("device_cert")
    attestation = bundle.get("operator_attestation")
    slug_binding = bundle.get("slug_binding")
    if not (
        isinstance(identity_cert, dict)
        and isinstance(device_cert, dict)
        and isinstance(attestation, dict)
        and isinstance(slug_binding, dict)
    ):
        return _create_reject("identity_bundle missing one of identity_cert/device_cert/operator_attestation/slug_binding")

    # Surface cert errors verbatim so the web client can show the
    # specific cryptographic mismatch.
    try:
        agent_root_pk = verify_identity_cert(identity_cert)
    except CertError as exc:
        return _create_reject(f"identity_cert: {exc}")
    if identity_cert.get("identity_type") != "agent":
        return _create_reject(
            "identity_cert.identity_type must be 'agent'",
            got=identity_cert.get("identity_type"),
        )

    declared_op_pk_b64 = identity_cert.get("declared_operator_public_key")
    if not isinstance(declared_op_pk_b64, str) or not declared_op_pk_b64:
        return _create_reject("identity_cert.declared_operator_public_key required for agent identity")
    if declared_op_pk_b64 != paired_root_pubkey_b64:
        return _create_reject(
            "identity_cert.declared_operator_public_key does not match paired operator",
            cert=declared_op_pk_b64, paired=paired_root_pubkey_b64,
        )

    try:
        verify_device_cert(device_cert, agent_root_pk)
    except CertError as exc:
        return _create_reject(f"device_cert: {exc}")
    try:
        slug_from_binding = verify_slug_binding(slug_binding, agent_root_pk)
    except CertError as exc:
        return _create_reject(f"slug_binding: {exc}")
    try:
        _verify_attestation(attestation, agent_root_pk, paired_root_pk)
    except CertError as exc:
        return _create_reject(f"operator_attestation: {exc}")

    # Validate the slug + device_id match the cert bundle. A
    # mismatch means a doctored payload (or rare provisioning race).
    server_url = (pc.get("server_url") or "").strip()
    pc_slug = (pc.get("slug") or "").strip()
    pc_device_id = (pc.get("device_id") or "").strip()
    space_id = (pc.get("space_id") or "").strip()
    # Optional on the wire. Without it the worker can't DM the
    # operator about non-auto-acceptable invites (falls back to
    # logging).
    operator_slug = (pc.get("operator_slug") or "").strip()
    if not (server_url and pc_slug and pc_device_id and space_id):
        return _create_reject(
            "puffo_core block must include server_url, slug, device_id, space_id",
            keys_present=sorted(pc.keys()),
        )
    if pc_slug != slug_from_binding:
        return _create_reject(
            "puffo_core.slug != slug_binding.slug",
            puffo_core=pc_slug, slug_binding=slug_from_binding,
        )
    if pc_device_id != device_cert.get("device_id"):
        return _create_reject(
            "puffo_core.device_id != device_cert.device_id",
            puffo_core=pc_device_id, device_cert=device_cert.get("device_id"),
        )

    # The agent dir name is the full slug.
    agent_id = pc_slug
    if not is_valid_agent_id(agent_id):
        return _create_reject(f"slug {agent_id!r} is not a valid agent id")
    if agent_yml_path(agent_id).exists():
        return _create_reject(f"agent {agent_id!r} already exists on this daemon")

    # Defaults match the CLI's `agent create` behaviour.
    display_name = (payload.get("display_name") or agent_id).strip() or agent_id
    avatar_url = (payload.get("avatar_url") or "").strip()
    role = (payload.get("role") or "").strip()
    role_short_raw = payload.get("role_short")
    if role_short_raw is not None and not isinstance(role_short_raw, str):
        return _create_reject("role_short must be a string")
    if role and len(role) > MAX_ROLE_LEN:
        return _create_reject(
            f"role must be at most {MAX_ROLE_LEN} characters",
        )
    if isinstance(role_short_raw, str) and len(role_short_raw) > MAX_ROLE_SHORT_LEN:
        return _create_reject(
            f"role_short must be at most {MAX_ROLE_SHORT_LEN} characters",
        )
    if not role and role_short_raw:
        return _create_reject("role_short cannot be set without role")
    role_short = (
        role_short_raw.strip() if isinstance(role_short_raw, str)
        else _derive_role_short(role) if role
        else ""
    )
    profile_text = payload.get("profile")
    if not isinstance(profile_text, str) or not profile_text.strip():
        return _create_reject("profile (markdown body) is required")

    # Reject invalid runtime triples up front instead of letting
    # the worker crash on the next reconcile tick.
    runtime = RuntimeConfig(
        kind=str(rt.get("kind", "chat-local")),
        provider=str(rt.get("provider", "")),
        model=str(rt.get("model", "")),
        api_key=str(rt.get("api_key", "")),
        harness=str(rt.get("harness", "claude-code")),
        permission_mode=str(rt.get("permission_mode", "bypassPermissions")),
        max_turns=int(rt.get("max_turns", 10)),
    )
    from ..runtime_matrix import validate_triple
    validation = validate_triple(runtime.kind, runtime.provider, runtime.harness)
    if not validation.ok:
        return _create_reject(f"runtime: {validation.error}")

    # On any failure tear the half-built dir down so the reconcile
    # loop doesn't keep retrying a half-provisioned agent.
    target = agent_dir(agent_id)
    try:
        target.mkdir(parents=True, exist_ok=False)
        cfg = AgentConfig(
            id=agent_id,
            state="running",
            display_name=display_name,
            avatar_url=avatar_url,
            role=role,
            role_short=role_short,
            puffo_core=PuffoCoreConfig(
                server_url=server_url,
                slug=pc_slug,
                device_id=pc_device_id,
                space_id=space_id,
                operator_slug=operator_slug,
            ),
            runtime=runtime,
            profile="profile.md",
            memory_dir="memory",
            workspace_dir="workspace",
            triggers=TriggerRules(on_mention=True, on_dm=True),
            created_at=int(now_ms() / 1000),
        )
        cfg.save()
        (target / "memory").mkdir(exist_ok=True)
        (target / "profile.md").write_text(profile_text, encoding="utf-8")
        _write_keystore(agent_id, pc_slug, server_url, bundle, pc_device_id)
    except Exception as exc:
        # Best-effort cleanup; otherwise the reconcile loop would
        # log a partial-agent.yml warning every tick.
        import shutil
        shutil.rmtree(target, ignore_errors=True)
        logger.error("bridge: create-agent write failed: %s", exc, exc_info=True)
        return web.json_response({"error": f"write failed: {exc}"}, status=500)

    logger.info(
        "bridge: created agent slug=%s device_id=%s by operator=%s",
        agent_id, pc_device_id, request["paired_slug"],
    )

    # Best-effort: push role to the agent's server-side identity
    # profile. display_name + avatar_url already flow through the
    # pending_agents → identities materialisation in puffo-server
    # signup, so they're set at registration time; ``role`` was
    # added later (migration 019_identity_role) and doesn't have a
    # matching signup pathway yet, so the bridge has to sync it
    # post-create. Failure here is non-fatal — the operator can
    # retry via ``PATCH /v1/agents/{id}/profile`` later.
    if role:
        try:
            patch: dict[str, Any] = {"role": role}
            if role_short:
                patch["role_short"] = role_short
            await _sync_agent_profile(AgentConfig.load(agent_id), patch)
        except Exception as exc:
            logger.warning(
                "bridge: post-create role sync failed for agent=%s: %s",
                agent_id, exc,
            )

    return web.json_response({
        "agent_id": agent_id,
        "agent_dir": str(target),
    }, status=201)


def _write_keystore(
    agent_id: str, slug: str, server_url: str, bundle: dict, device_id: str,
) -> None:
    """Write the agent's StoredIdentity to ``keys/<slug>.json``;
    shape mirrors what ``puffo-cli agent register`` produces."""
    import json
    keys_dir = agent_dir(agent_id) / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    stored = {
        "slug": slug,
        "device_id": device_id,
        "root_secret_key": bundle["root_secret_key"],
        "device_signing_secret_key": bundle["device_signing_secret_key"],
        "kem_secret_key": bundle["kem_secret_key"],
        "server_url": server_url,
        "identity_cert_json": json.dumps(bundle["identity_cert"]),
        "slug_binding_json": json.dumps(bundle["slug_binding"]),
    }
    path = keys_dir / f"{slug}.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    import os
    os.replace(tmp, path)


async def get_claude_md(request: web.Request) -> web.Response:
    """Read the agent's generated CLAUDE.md.

    Lives at ``<agent_home>/.claude/CLAUDE.md`` (outside
    ``workspace_dir``), so the generic file endpoints can't reach
    it — hence this dedicated handler.
    """
    agent_id = request.match_info["id"]
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")
    target = agent_claude_user_dir(agent_id) / "CLAUDE.md"
    if not target.exists() or not target.is_file():
        return _not_found("CLAUDE.md not generated yet (agent never started)")
    try:
        size = target.stat().st_size
    except OSError as exc:
        return _bad(f"stat failed: {exc}")
    if size > MAX_FILE_BYTES:
        return web.Response(
            status=413,
            text=f"CLAUDE.md is {size} bytes (cap {MAX_FILE_BYTES})",
        )
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _bad(f"read failed: {exc}")
    return web.Response(text=text, content_type="text/plain", charset="utf-8")


def _safe_join(workspace: Path, rel: str) -> tuple[Path, str | None]:
    """Resolve ``workspace / rel`` and verify the result still lives
    under ``workspace`` after symlink resolution. Returns
    ``(path, None)`` on success or ``(workspace, error)`` on
    rejection. Absolute paths are rejected outright.
    """
    if rel == "":
        return workspace, None
    p = Path(rel)
    if p.is_absolute():
        return workspace, "absolute path not allowed"
    try:
        resolved = (workspace / p).resolve()
    except OSError as exc:
        return workspace, f"path resolve failed: {exc}"
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return workspace, "path escapes workspace"
    return resolved, None


# ────────────────────────────────────────────────────────────────────
# small helpers
# ────────────────────────────────────────────────────────────────────


def _bad(msg: str) -> web.Response:
    return web.json_response({"error": msg}, status=400)


def _not_found(msg: str) -> web.Response:
    return web.json_response({"error": msg}, status=404)
