"""HTTP handlers for the bridge.

All endpoints live under ``/v1/`` for forward versioning.

Path-traversal safety on file endpoints uses ``Path.resolve()`` +
``Path.is_relative_to(workspace)`` — catches ``../`` walks, absolute
paths, and escaping symlinks. Symlinks pointing inside the workspace
are allowed.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from ...agent.shared_content import rewrite_profile_name
from ...crypto.encoding import base64url_decode
from ..state import (
    AgentConfig,
    PuffoCoreConfig,
    RuntimeState,
    TriggerRules,
    agent_claude_user_dir,
    agent_dir,
    agent_yml_path,
    archive_flag_path,
    delete_flag_path,
    discover_agents,
    is_valid_agent_id,
    refresh_agent_flag_path,
    restart_flag_path,
)
from .audit_log import parse_log_query, read_audit_log
from .ownership import is_owner
from .pair_endpoint import pair as pair
from .pairing import clear_pairing, load_pairing, now_ms
from .profile_content import (
    MAX_PROFILE_SUMMARY_BYTES,
    MAX_ROLE_LEN,
    MAX_ROLE_SHORT_LEN,
)
from .profile_content import (
    derive_role_short as _derive_role_short,
)
from .profile_content import (
    profile_summary as _profile_summary,
)
from .profile_content import (
    update_profile_summary as _update_profile_summary,
)
from .provision_validation import (
    ProvisionError,
)
from .provision_validation import (
    verify_agent_bundle as _verify_agent_bundle,
)
from .runtime_patch import apply_runtime_patch, runtime_response

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


def _cli_tool_status(
    resolver: Callable[[], str | None],
    cred_check: Callable[[], bool],
) -> str:
    """Returns ``not_installed`` | ``need_login`` | ``ready``."""
    try:
        path = resolver()
    except Exception:
        path = None
    if not path:
        return "not_installed"
    try:
        return "ready" if cred_check() else "need_login"
    except Exception:
        return "need_login"


async def info(_request: web.Request) -> web.Response:
    """Public discovery endpoint. No auth."""
    pairing = load_pairing()
    try:
        from importlib.metadata import version

        daemon_version = version("puffo-agent")
    except Exception:
        daemon_version = "unknown"
    from ...agent.cli_bin import (
        claude_has_credentials,
        codex_has_credentials,
        resolve_claude_bin,
        resolve_codex_bin,
    )
    from ..control.store import current_machine_id

    return web.json_response(
        {
            "service": "puffo-agent-bridge",
            "version": "v1",
            "runtime": "puffo-agent",
            "daemon_version": daemon_version,
            "pid": os.getpid(),
            # Lets a web client that sees both the remote-linked machine and this
            # local bridge recognise they're the same host and drop the dup UI.
            "machine_id": current_machine_id(),
            "agent_count": len(discover_agents()),
            "paired": pairing is not None,
            "paired_slug": pairing.slug if pairing else None,
            "paired_device_id": pairing.device_id if pairing else None,
            "cli_tools": {
                "claude-code": _cli_tool_status(
                    resolve_claude_bin, claude_has_credentials
                ),
                "codex": _cli_tool_status(resolve_codex_bin, codex_has_credentials),
            },
        }
    )


async def list_providers(_request: web.Request) -> web.Response:
    """Public: live per-provider model catalogs. claude-code hits
    ``/v1/models``, codex reads its local cache. Models only — harness
    install/auth status lives on ``/v1/info``."""
    import asyncio

    from ...agent.model_catalog import KNOWN_HARNESSES, provider_models

    def _build() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for harness in KNOWN_HARNESSES:
            models = [
                {"id": o.id, "label": o.label, "alias": o.is_alias}
                for o in provider_models(harness, fetch=True)
                if o.id  # drop the daemon-default ("") sentinel
            ]
            out.append({"provider": harness, "models": models})
        return out

    return web.json_response({"providers": await asyncio.to_thread(_build)})


# ────────────────────────────────────────────────────────────────────
# /v1/pair
# ────────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────────
# /v1/agents and friends
# ────────────────────────────────────────────────────────────────────


def _operator_has_active_link(paired_root_pubkey: str) -> bool:
    """True if this operator already has a machine-link pairing."""
    if not paired_root_pubkey:
        return False
    from ..control.store import load_pairings

    return any(
        p.operator_root_pubkey == paired_root_pubkey for p in load_pairings().values()
    )


async def list_agents(request: web.Request) -> web.Response:
    paired_root = request["paired_root_pubkey"]
    # Linked operators see agents via the server's link channel;
    # bridge returns empty to avoid double-listing.
    if _operator_has_active_link(paired_root):
        return web.json_response({"agents": []})
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
        workspace = cfg.resolve_workspace_dir()
        restart_pending = (
            refresh_agent_flag_path(workspace).exists()
            or restart_flag_path(aid).exists()
        )
        rs_status = (
            "restarting" if restart_pending else (rs.status if rs else "unknown")
        )
        items.append(
            {
                "id": aid,
                "display_name": cfg.display_name,
                "avatar_url": cfg.avatar_url,
                "puffo_core_slug": cfg.puffo_core.slug,
                "space_id": cfg.puffo_core.space_id,
                "profile_summary": _profile_summary(cfg),
                "state": cfg.state,
                "runtime_kind": cfg.runtime.kind,
                "runtime_harness": cfg.runtime.harness,
                "runtime_model": cfg.runtime.model,
                "runtime_status": rs_status,
                "runtime_health": rs.health if rs else "unknown",
                "msg_count": rs.msg_count if rs else 0,
                "owned": is_owner(aid, paired_root),
                # Operator slug who created the agent. Empty string for
                # agent.yml files written before this field existed; UI
                # degrades to the ``owned`` boolean alone.
                "operator_slug": cfg.puffo_core.operator_slug or "",
            }
        )
    return web.json_response({"agents": items})


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
        "docker_image": cfg.runtime.docker_image,
        # Only owners see the actual key. Non-owners get a boolean
        # so the UI can still render "(set)" / "(inherit)".
        "api_key": cfg.runtime.api_key if owned else None,
        "api_key_set": bool(cfg.runtime.api_key),
    }
    return web.json_response(
        {
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
        }
    )


async def update_runtime(request: web.Request) -> web.Response:
    """Patch the agent's runtime block. Owner-only.

    Accepts any subset of: ``kind``, ``provider``, ``model``,
    ``harness``, ``api_key``, ``permission_mode``, ``sandbox``, and
    ``docker_image``. Missing fields
    are untouched. ``harness`` editing requires the corresponding CLI
    to already be installed + authenticated on the host
    (`claude login` / `codex login` / ...) — the worker will hit
    auth_failed on first turn otherwise. validate_triple below
    catches kind/provider/harness combo violations before save.

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

    error = apply_runtime_patch(rt, payload)
    if error is not None:
        return _bad(error)

    cfg.save()
    logger.info(
        "bridge: updated runtime for agent=%s kind=%s provider=%s model=%s",
        agent_id,
        rt.kind,
        rt.provider,
        rt.model or "(default)",
    )
    return web.json_response(
        {
            "agent_id": agent_id,
            "runtime": runtime_response(rt),
            "note": "daemon will restart this agent on the next reconcile tick (~2s)",
        }
    )


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
    context = await _profile_edit_context(request)
    if isinstance(context, web.Response):
        return context
    agent_id, cfg, payload = context
    error, avatar_bytes, summary = _validate_profile_edit(payload)
    if error:
        return _bad(error)
    patch, avatar_url, warning = await _prepare_profile_patch(
        agent_id, cfg, payload, avatar_bytes
    )
    old_display_name = cfg.display_name
    renamed = _persist_profile_edit(cfg, payload, avatar_url, summary)
    if summary is not None:
        patch["soul"] = summary
    _post_persist_profile_edit(cfg, old_display_name, renamed, payload, summary)
    warning = await _sync_profile_patch(cfg, patch, warning)
    return web.json_response(_profile_edit_response(agent_id, cfg, warning))


async def _profile_edit_context(
    request: web.Request,
) -> tuple[str, AgentConfig, dict] | web.Response:
    agent_id = request.match_info["id"]
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")
    if not is_owner(agent_id, request["paired_root_pubkey"]):
        return web.json_response(
            {"error": "only the agent's operator can edit profile"}, status=403
        )
    try:
        payload = await request.json()
    except Exception:
        return _bad("body must be JSON")
    if not isinstance(payload, dict):
        return _bad("body must be a JSON object")
    return agent_id, AgentConfig.load(agent_id), payload


def _validate_profile_edit(
    payload: dict,
) -> tuple[str | None, bytes | None, str | None]:
    import base64

    role, role_short = payload.get("role"), payload.get("role_short")
    if role is None and role_short is not None:
        return "role_short cannot be set without role", None, None
    if isinstance(role, str) and len(role) > MAX_ROLE_LEN:
        return f"role must be at most {MAX_ROLE_LEN} characters", None, None
    if isinstance(role_short, str) and len(role_short) > MAX_ROLE_SHORT_LEN:
        return f"role_short must be at most {MAX_ROLE_SHORT_LEN} characters", None, None
    avatar_b64 = payload.get("avatar_bytes_b64")
    if avatar_b64 is not None and not isinstance(avatar_b64, str):
        return "avatar_bytes_b64 must be a base64 string", None, None
    try:
        avatar = base64.b64decode(avatar_b64) if avatar_b64 is not None else None
    except Exception as exc:
        return f"avatar_bytes_b64 decode: {exc}", None, None
    if avatar is not None and len(avatar) > MAX_AVATAR_BYTES:
        return f"avatar exceeds {MAX_AVATAR_LABEL} cap", None, None
    summary = payload.get("profile_summary")
    if isinstance(summary, str):
        summary = summary.strip()
        size = len(summary.encode("utf-8"))
        if size > MAX_PROFILE_SUMMARY_BYTES:
            return (
                f"profile_summary is {size} bytes; cap is {MAX_PROFILE_SUMMARY_BYTES}",
                None,
                None,
            )
    return None, avatar, summary if isinstance(summary, str) else None


async def _prepare_profile_patch(
    agent_id: str,
    cfg: AgentConfig,
    payload: dict,
    avatar: bytes | None,
) -> tuple[dict[str, Any], str | None, str | None]:
    patch: dict[str, Any] = {}
    name, role, role_short = (
        payload.get("display_name"),
        payload.get("role"),
        payload.get("role_short"),
    )
    if isinstance(name, str):
        patch["display_name"] = name.strip()
    if isinstance(role, str):
        patch["role"] = role
    if isinstance(role_short, str):
        patch["role_short"] = role_short
    if avatar is not None:
        try:
            url = await _upload_avatar_via_agent_keystore(cfg, avatar)
            patch["avatar_url"] = url
            return patch, url, None
        except Exception as exc:
            logger.warning(
                "bridge: avatar upload failed for agent=%s: %s", agent_id, exc
            )
            return patch, None, f"avatar upload failed: {exc}"
    if isinstance(payload.get("avatar_url"), str):
        url = payload["avatar_url"].strip()
        patch["avatar_url"] = url
        return patch, url, None
    return patch, None, None


def _persist_profile_edit(
    cfg: AgentConfig, payload: dict, avatar_url: str | None, summary: str | None
) -> bool:
    old_name, name, role, role_short = (
        cfg.display_name,
        payload.get("display_name"),
        payload.get("role"),
        payload.get("role_short"),
    )
    if isinstance(name, str):
        cfg.display_name = name.strip() or cfg.display_name
    if avatar_url is not None:
        cfg.avatar_url = avatar_url
    if isinstance(role, str):
        cfg.role = role
        if not isinstance(role_short, str):
            cfg.role_short = _derive_role_short(role)
    if isinstance(role_short, str):
        cfg.role_short = role_short
    cfg.save()
    if summary is not None:
        _update_profile_summary(cfg, summary)
    return bool(
        isinstance(name, str)
        and cfg.display_name != old_name
        and old_name
        and cfg.display_name
    )


def _post_persist_profile_edit(
    cfg: AgentConfig, old_name: str, renamed: bool, payload: dict, summary: str | None
) -> None:
    if renamed:
        rewrite_profile_name(cfg.resolve_profile_path(), old_name, cfg.display_name)
    logger.info(
        "bridge: updated profile for agent=%s display_name=%r avatar=%s role_short=%r",
        cfg.id,
        cfg.display_name,
        "(set)" if cfg.avatar_url else "(empty)",
        cfg.role_short,
    )
    if renamed or summary is not None or isinstance(payload.get("role"), str):
        from ..profile_sync import write_refresh_agent_flag

        write_refresh_agent_flag(cfg, reason="bridge profile edit")


async def _sync_profile_patch(
    cfg: AgentConfig, patch: dict[str, Any], warning: str | None
) -> str | None:
    if not patch:
        return warning
    try:
        await _sync_agent_profile(cfg, patch)
    except Exception as exc:
        logger.warning("bridge: profile sync failed for agent=%s: %s", cfg.id, exc)
        return warning or f"profile sync failed: {exc}"
    return warning


def _profile_edit_response(
    agent_id: str, cfg: AgentConfig, warning: str | None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "display_name": cfg.display_name,
        "avatar_url": cfg.avatar_url,
        "role": cfg.role,
        "role_short": cfg.role_short,
        "profile_summary": _profile_summary(cfg),
    }
    if warning:
        body["warning"] = warning
    return body


async def _upload_avatar_via_agent_keystore(
    cfg: AgentConfig,
    avatar_bytes: bytes,
) -> str:
    """Upload bytes to ``/blobs/upload`` signed by the agent's
    subkey; returns the resulting blob URL."""
    from ...crypto.http_client import PuffoCoreHttpClient
    from ...crypto.keystore import KeyStore

    pc = cfg.puffo_core
    ks = KeyStore.for_agent(cfg.id)
    http = PuffoCoreHttpClient(pc.server_url, ks, pc.slug)
    try:
        # Pre-rotate so a long-idle agent's first upload doesn't fail.
        await http._ensure_subkey()  # noqa: SLF001 — same intra-package use
        signing_key, signer_id = http._load_signing_key()  # noqa: SLF001
        from ...crypto.http_auth import sign_request

        auth = sign_request(
            signing_key,
            pc.slug,
            signer_id,
            "POST",
            "/blobs/upload",
            avatar_bytes,
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


# /v1/agents/{id}/restart drops refresh_agent.flag — the daemon-
# internal restart.flag is reserved for auth-recovery.


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
    try:
        cfg = AgentConfig.load(agent_id)
    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"error": f"could not load agent config: {exc}"},
            status=500,
        )
    flag = refresh_agent_flag_path(cfg.resolve_workspace_dir())
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(
            json.dumps({"requested_at": int(time.time())}),
            encoding="utf-8",
        )
    except OSError as exc:
        return web.json_response(
            {"error": f"could not write refresh_agent flag: {exc}"},
            status=500,
        )
    logger.info("bridge: restart (refresh_agent) requested for agent=%s", agent_id)
    return web.json_response(
        {
            "agent_id": agent_id,
            "ok": True,
            "note": "worker will rebuild CLAUDE.md + reload on its next turn",
        }
    )


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
    return web.json_response(
        {
            "agent_id": agent_id,
            "ok": True,
            "note": "daemon will stop the worker + remove the agent dir on the next reconcile tick (~2s)",
        }
    )


# ────────────────────────────────────────────────────────────────────
# /v1/agents/{id}/pause (POST) and /v1/agents/{id}/resume (POST) —
# flip ``agent.yml``'s ``state`` field; the daemon reconciler picks
# up the change on the next tick and stops / starts the worker.
# Same flow the CLI's ``puffo-agent agent pause`` / ``resume`` use
# via ``_set_agent_state`` in ``portal.cli``.
# ────────────────────────────────────────────────────────────────────


async def _flip_agent_state(
    request: web.Request,
    *,
    target_state: str,
    action_label: str,
) -> web.Response:
    agent_id = request.match_info["id"]
    if not is_valid_agent_id(agent_id):
        return _bad("invalid agent id")
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")
    paired_root = request["paired_root_pubkey"]
    if not is_owner(agent_id, paired_root):
        return web.json_response(
            {"error": f"only the agent's operator can {action_label} it"},
            status=403,
        )
    cfg = AgentConfig.load(agent_id)
    if cfg.state == target_state:
        # Idempotent: silently succeed when already in the target
        # state. The CLI prints "already {state}"; the bridge just
        # returns 200 with the resolved state so the web client can
        # refresh agent list without special-casing.
        return web.json_response(
            {
                "agent_id": agent_id,
                "state": cfg.state,
                "ok": True,
                "note": f"already {target_state}",
            }
        )
    cfg.state = target_state
    cfg.save()
    logger.info(
        "bridge: %s requested for agent=%s (state -> %s)",
        action_label,
        agent_id,
        target_state,
    )
    return web.json_response(
        {
            "agent_id": agent_id,
            "state": cfg.state,
            "ok": True,
            "note": (
                "daemon will apply the state change on the next reconcile tick (~2s)"
            ),
        }
    )


async def pause_agent(request: web.Request) -> web.Response:
    return await _flip_agent_state(
        request,
        target_state="paused",
        action_label="pause",
    )


async def resume_agent(request: web.Request) -> web.Response:
    return await _flip_agent_state(
        request,
        target_state="running",
        action_label="resume",
    )


# ────────────────────────────────────────────────────────────────────
# /v1/agents/{id}/archive (POST) — pauses the worker + drops an
# ``archive.flag`` sentinel; the reconciler stops the worker and
# moves the agent dir to ``~/.puffo-agent/archived/<id>-<ts>``.
# Soft-destructive: the dir is preserved on disk and can be brought
# back by hand. Distinct from DELETE which removes the dir entirely.
# ────────────────────────────────────────────────────────────────────


async def archive_agent(request: web.Request) -> web.Response:
    agent_id = request.match_info["id"]
    if not is_valid_agent_id(agent_id):
        return _bad("invalid agent id")
    if not agent_dir(agent_id).exists():
        return _not_found("agent not found")
    paired_root = request["paired_root_pubkey"]
    if not is_owner(agent_id, paired_root):
        return web.json_response(
            {"error": "only the agent's operator can archive it"},
            status=403,
        )
    # Flip to paused FIRST so the worker exits cleanly before the
    # reconciler tries to move the dir. CLI's ``cmd_agent_archive``
    # does the same dance — pause, wait for the worker to release
    # file handles (sqlite WAL is the slow one), then move.
    cfg = AgentConfig.load(agent_id)
    if cfg.state != "paused":
        cfg.state = "paused"
        cfg.save()
    flag = archive_flag_path(agent_id)
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("requested", encoding="utf-8")
    except OSError as exc:
        return web.json_response(
            {"error": f"could not write archive flag: {exc}"},
            status=500,
        )
    logger.info("bridge: archive requested for agent=%s", agent_id)
    return web.json_response(
        {
            "agent_id": agent_id,
            "ok": True,
            "note": (
                "daemon will pause the worker + move the agent dir to "
                "~/.puffo-agent/archived/ on the next reconcile tick (~2s)"
            ),
        }
    )


# ────────────────────────────────────────────────────────────────────
# /v1/agents/{id}/log
# ────────────────────────────────────────────────────────────────────


async def get_log(request: web.Request) -> web.Response:
    """Return a bounded tail or byte-cursor delta from the agent audit log."""
    agent_id = request.match_info["id"]
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")

    try:
        tail, since = parse_log_query(
            request.query.get("tail"),
            request.query.get("since"),
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    cfg = AgentConfig.load(agent_id)
    log_path = cfg.resolve_workspace_dir() / ".puffo-agent" / "audit.log"
    body = {"agent_id": agent_id}
    body.update(read_audit_log(log_path, tail, since))
    return web.json_response(body)


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
        for child in sorted(
            target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        ):
            try:
                st = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "kind": "dir" if child.is_dir() else "file",
                    "size": int(st.st_size) if child.is_file() else 0,
                    "mtime": int(st.st_mtime),
                }
            )
    except OSError as exc:
        return _bad(f"readdir failed: {exc}")
    return web.json_response(
        {
            "agent_id": agent_id,
            "workspace": str(workspace),
            "path": rel,
            "entries": entries,
        }
    )


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
    logger.warning(
        "bridge: create-agent rejected: %s%s", reason, f" {extra}" if extra else ""
    )
    return _bad(reason)


def _write_agent_from_context(ctx: dict) -> dict:
    """Write the agent dir + keystore from a verified context. Raises
    ``ProvisionError`` on slug collision; tears down a half-built dir on
    any write failure and re-raises. Returns ``{agent_id, agent_dir}``."""
    agent_id = ctx["agent_id"]
    if agent_yml_path(agent_id).exists():
        raise ProvisionError(f"agent {agent_id!r} already exists on this daemon")

    target = agent_dir(agent_id)
    try:
        target.mkdir(parents=True, exist_ok=False)
        cfg = AgentConfig(
            id=agent_id,
            state="running",
            display_name=ctx["display_name"],
            avatar_url=ctx["avatar_url"],
            role=ctx["role"],
            role_short=ctx["role_short"],
            puffo_core=PuffoCoreConfig(
                server_url=ctx["server_url"],
                slug=ctx["slug"],
                device_id=ctx["device_id"],
                space_id=ctx["space_id"],
                operator_slug=ctx["operator_slug"],
            ),
            runtime=ctx["runtime"],
            profile="profile.md",
            memory_dir="memory",
            workspace_dir="workspace",
            triggers=TriggerRules(on_mention=True, on_dm=True),
            desired_skills=list(ctx["desired_skills"]),
            desired_mcps=list(ctx["desired_mcps"]),
            created_at=int(now_ms() / 1000),
        )
        cfg.save()
        (target / "memory").mkdir(exist_ok=True)
        (target / "profile.md").write_text(ctx["profile_text"], encoding="utf-8")
        _write_keystore(
            agent_id, ctx["slug"], ctx["server_url"], ctx["bundle"], ctx["device_id"]
        )
    except ProvisionError:
        raise
    except Exception:
        # Best-effort cleanup so the reconcile loop doesn't keep
        # retrying a half-provisioned agent.
        import shutil

        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"agent_id": agent_id, "agent_dir": str(target)}


async def provision_agent_from_bundle(
    payload: dict,
    paired_root_pubkey_b64: str,
    *,
    materialize=None,
) -> dict:
    """Verify a web-signed create-agent bundle, optionally run an async
    ``materialize`` hook (Agent Portal remote create: finalize the pending
    identity with puffo-server *before* the dir lands, so the reconcile loop
    never spawns an unmaterialised agent), then write the agent dir.

    Returns ``{agent_id, agent_dir, device_id, role, role_short, runtime}``.
    The HTTP bridge passes no hook — its identity is materialised by the
    browser before the call.
    """
    ctx = _verify_agent_bundle(payload, paired_root_pubkey_b64)
    if materialize is not None:
        await materialize(ctx)
    result = _write_agent_from_context(ctx)
    result.update(
        device_id=ctx["device_id"],
        role=ctx["role"],
        role_short=ctx["role_short"],
        runtime=ctx["runtime"],
    )
    return result


async def create_agent(request: web.Request) -> web.Response:
    """Provision a new agent locally from a web-signed bundle.

    The web client has already signed the operator attestation and
    registered + materialised the identity with puffo-server; this handler
    verifies cryptographic consistency and writes the agent dir on disk for
    the reconcile loop to pick up.

    Body shape::

        {
          "display_name": "...",
          "profile": "<full profile.md text>",
          "puffo_core": {server_url, slug, device_id, space_id},
          "runtime": {kind, provider, model, api_key, harness, permission_mode},
          "identity_bundle": {identity_cert, device_cert, operator_attestation,
            slug_binding, root_secret_key, device_signing_secret_key,
            kem_secret_key}
        }
    """
    paired_root_pubkey_b64 = request["paired_root_pubkey"]
    try:
        payload = await request.json()
    except Exception:
        return _create_reject("body must be JSON")

    try:
        result = await provision_agent_from_bundle(payload, paired_root_pubkey_b64)
    except ProvisionError as exc:
        return _create_reject(exc.reason, **exc.fields)
    except Exception as exc:
        logger.error("bridge: create-agent write failed: %s", exc, exc_info=True)
        return web.json_response({"error": f"write failed: {exc}"}, status=500)

    agent_id = result["agent_id"]
    logger.info(
        "bridge: created agent slug=%s device_id=%s by operator=%s",
        agent_id,
        result["device_id"],
        request["paired_slug"],
    )

    # role has no signup pathway, so sync it to the server profile post-create
    # (display_name + avatar_url already land at registration).
    if result["role"]:
        try:
            patch: dict[str, Any] = {"role": result["role"]}
            if result["role_short"]:
                patch["role_short"] = result["role_short"]
            await _sync_agent_profile(AgentConfig.load(agent_id), patch)
        except Exception as exc:
            logger.warning(
                "bridge: post-create role sync failed for agent=%s: %s", agent_id, exc
            )

    response_body: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_dir": result["agent_dir"],
    }

    # ws-local: passcode doubles as the ``.puffoagent`` export password so the
    # web can round-trip create + export in one call.
    from ..runtime_matrix import RUNTIME_WS_LOCAL

    passcode = (payload.get("passcode") or "").strip()
    if result["runtime"].kind == RUNTIME_WS_LOCAL and passcode:
        try:
            from .. import export as exp

            bundle_bytes = exp.pack(
                [agent_id],
                passcode,
                exported_by_slug=request.get("paired_slug", ""),
            )
            response_body["bundle_base64"] = base64.b64encode(bundle_bytes).decode(
                "ascii"
            )
        except Exception as exc:
            logger.warning(
                "bridge: ws-local bundle pack failed for agent=%s: %s", agent_id, exc
            )
            response_body["bundle_error"] = str(exc)

    return web.json_response(response_body, status=201)


def _write_keystore(
    agent_id: str,
    slug: str,
    server_url: str,
    bundle: dict,
    device_id: str,
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


def _conflict(msg: str) -> web.Response:
    return web.json_response({"error": msg}, status=409)


# ────────────────────────────────────────────────────────────────────
# /v1/agents/export, /v1/agents/import, /v1/agents/{id}/revoke-pending
# Multi-agent migration. See ``portal/export.py`` and
# ``portal/import_agents.py`` for the on-disk + server-side flow.
# ────────────────────────────────────────────────────────────────────


async def agents_export(request: web.Request) -> web.Response:
    from .. import export as exp

    try:
        payload = await request.json()
    except Exception:
        return _bad("body must be JSON")
    if not isinstance(payload, dict):
        return _bad("body must be a JSON object")

    raw_ids = payload.get("agent_ids")
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or not all(isinstance(a, str) for a in raw_ids)
    ):
        return _bad("agent_ids must be a non-empty list of strings")
    password = payload.get("password")
    if not isinstance(password, str) or not password:
        return _bad("password must be a non-empty string")
    for aid in raw_ids:
        if not is_valid_agent_id(aid):
            return _bad(f"invalid agent id: {aid!r}")

    # Paused-only — running agents may be mid-write (cli_session, memory).
    # Small TOCTOU between this guard and exp.pack is accepted; see CHANGELOG.
    for aid in raw_ids:
        try:
            cfg = AgentConfig.load(aid)
        except FileNotFoundError:
            return _not_found(f"agent not found: {aid}")
        if cfg.state != "paused":
            return _conflict(f"agent {aid} is {cfg.state!r}; pause it before exporting")

    try:
        blob = exp.pack(
            raw_ids, password, exported_by_slug=request.get("paired_slug", "")
        )
    except exp.ExportError as exc:
        return _bad(str(exc))

    logger.info("bridge: export packed agents=%s bytes=%d", raw_ids, len(blob))
    return web.Response(
        body=blob,
        content_type="application/octet-stream",
        headers={"content-disposition": 'attachment; filename="agents.puffoagent"'},
    )


async def agents_import(request: web.Request) -> web.Response:
    from .. import export as exp
    from .. import import_agents as imp

    try:
        payload = await request.json()
    except Exception:
        return _bad("body must be JSON")
    if not isinstance(payload, dict):
        return _bad("body must be a JSON object")

    bundle_b64 = payload.get("bundle_b64")
    password = payload.get("password")
    if not isinstance(bundle_b64, str) or not bundle_b64:
        return _bad("bundle_b64 must be a non-empty base64url string")
    if not isinstance(password, str) or not password:
        return _bad("password must be a non-empty string")

    try:
        blob = base64url_decode(bundle_b64)
    except Exception as exc:
        return _bad(f"bundle_b64 decode failed: {exc}")

    try:
        report = await imp.import_bundle(blob, password)
    except exp.ImportPackError as exc:
        return _bad(str(exc))

    body = {
        "total": report.total,
        "imported": report.imported,
        "skipped": report.skipped,
        "failed": report.failed,
        "pending_revokes": report.pending_revokes,
        "results": [
            {
                "agent_id": r.agent_id,
                "status": r.status,
                "detail": r.detail,
                "new_device_id": r.new_device_id,
                "old_device_id": r.old_device_id,
            }
            for r in report.results
        ],
    }
    logger.info(
        "bridge: import done total=%d imported=%d skipped=%d failed=%d pending_revokes=%d",
        report.total,
        report.imported,
        report.skipped,
        report.failed,
        report.pending_revokes,
    )
    return web.json_response(body)


async def agent_revoke_pending(request: web.Request) -> web.Response:
    from .. import import_agents as imp

    agent_id = request.match_info["id"]
    if not is_valid_agent_id(agent_id):
        return _bad("invalid agent id")
    if not agent_yml_path(agent_id).exists():
        return _not_found("agent not found")
    paired_root = request["paired_root_pubkey"]
    if not is_owner(agent_id, paired_root):
        return web.json_response(
            {"error": "only the agent's operator can retry its revoke"},
            status=403,
        )
    result = await imp.revoke_pending(agent_id)
    body = {
        "agent_id": result.agent_id,
        "status": result.status,
        "detail": result.detail,
        "old_device_id": result.old_device_id,
    }
    status = 200 if result.status in ("imported", "skipped") else 502
    return web.json_response(body, status=status)


async def create_ws_local_agent(request: web.Request) -> web.Response:
    """Bridge entry for ``puffo-agent agent create-ws-local``: send the operator
    the approval request and return immediately with the ``request_id`` (the flow
    is non-blocking — the operator approves whenever). Poll completion with
    ``wait-until-command --id <request_id>``."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid JSON body"}, status=400)
    operator = body.get("operator")
    passcode = body.get("passcode")
    if not isinstance(operator, str) or not operator:
        return web.json_response({"error": "operator required"}, status=400)
    if not isinstance(passcode, str) or not passcode:
        return web.json_response({"error": "passcode required"}, status=400)
    from ..control.agent_create import start_create

    try:
        result = await start_create(
            operator,
            passcode,
            username=str(body.get("display_name") or ""),
            message=str(body.get("message") or ""),
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response(result, status=202)


async def wait_until_command(request: web.Request) -> web.Response:
    """Bridge entry for ``puffo-agent machine wait-until-command --id X``: block
    until the command with id X has been processed, return its result. For a
    ws-local create this is the operator's approval → {agent_slug, bundle_path,
    passcode}."""
    command_id = request.query.get("id", "")
    if not command_id:
        return web.json_response({"error": "id required"}, status=400)
    try:
        timeout = float(request.query.get("timeout", "600"))
    except ValueError:
        timeout = 600.0
    from ..control.agent_create import get_registry

    try:
        result = await get_registry().wait_result(command_id, timeout)
    except TimeoutError:
        return web.json_response({"error": "timeout", "pending": True}, status=504)
    status = 200 if result.get("ok", True) else 502
    return web.json_response(result, status=status)
