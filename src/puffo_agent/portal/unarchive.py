"""Unarchive: bring an archived agent back on this machine, paused.

Archive (``daemon._archive_on_flag``) moves ``agents/<id>`` to
``archived/<id>-ws-<stamp>`` — keystore, root key and the very
``archive.flag`` that triggered it included — and revokes the agent's
device. When that revoke does not settle it leaves
``.puffo-agent/pending_revoke.json`` for the startup sweep.

Coming back cannot reuse the old device: it is revoked, or may be — a
revoke whose response was lost still leaves the marker, so the marker
cannot tell us either way. The path is therefore the same in every case:

1. mint a new device + subkey signed by the agent's root key, staged in the
   archive so a retry replays the *same* signed certs;
2. ask the server to install them (``restore-device``, machine-authed);
3. write the new identity, keeping the old one under ``keys/retired/`` so
   nothing that could decrypt past traffic is destroyed;
4. revoke the old device with a root-signed revocation sent through the new
   device (idempotent from our side: already-revoked is fine), or leave an
   import-style pending marker for ``revoke-pending``;
5. drop ``archive.flag``, set the agent paused, move the directory back and
   report ``paused``.

The archive stays untouched until step 3, so any earlier failure leaves it
exactly as it was, plus the reason in ``.puffo-agent/unarchive.json``.

An agent that is still on disk while the server says "archived" (an owner
force-archived it while this machine was offline) was never moved or
revoked; unarchive then only reports it paused (``mode: server_only``).

LingTai agents need nothing extra: archive never revokes the LingTai
runtime binding, so the ``--runtime-id`` in ``agent.yml`` still resolves.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import aiohttp

from ..crypto.certs import create_subkey_cert
from ..crypto.http_session import create_remote_http_session
from ..crypto.keystore import KeyStore, StoredIdentity, decode_secret, encode_secret
from ..crypto.primitives import Ed25519KeyPair, KemKeyPair
from .host_assets import _atomic_write_private, _ensure_private_directory
from .migration_certs import create_device_cert
from .state import agent_dir, agent_yml_path, archived_dir, is_valid_agent_id

logger = logging.getLogger(__name__)

_STAMP = r"\d{8}-\d{6}"
STATE_FILE = "unarchive.json"


class UnarchiveError(Exception):
    def __init__(self, code: str, message: str, **fields) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


def _archives(agent_id: str, kind: str) -> list[Path]:
    root = archived_dir()
    if not root.is_dir():
        return []
    pattern = re.compile(rf"^{re.escape(agent_id)}-{kind}-{_STAMP}$")
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and pattern.match(p.name)),
        key=lambda p: p.name,
    )


def _state_path(archive: Path) -> Path:
    return archive / ".puffo-agent" / STATE_FILE


def _load_state(archive: Path) -> dict:
    try:
        data = json.loads(_state_path(archive).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise UnarchiveError("archive_unreadable", f"unarchive state unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise UnarchiveError("archive_unreadable", "unarchive state is not an object")
    return data


def _save_state(archive: Path, state: dict) -> None:
    path = _state_path(archive)
    _ensure_private_directory(path.parent)
    _atomic_write_private(path, json.dumps(state, indent=2))


def _record_failure(archive: Path | None, exc: UnarchiveError) -> None:
    """Keep the last failure inside the archive so it survives restarts."""
    if archive is None or not archive.is_dir():
        return
    try:
        state = _load_state(archive)
    except UnarchiveError:
        state = {}
    state["last_error"] = {"code": exc.code, "message": str(exc)}
    try:
        _save_state(archive, state)
    except OSError as err:
        logger.warning("unarchive: could not record failure in %s: %s", archive, err)


def _select_archive(agent_id: str, archive_id: str | None) -> Path:
    candidates = _archives(agent_id, "ws")
    if archive_id is not None:
        match = [p for p in candidates if p.name == archive_id]
        if not match:
            raise UnarchiveError("archive_not_found", f"archive {archive_id!r} not found")
        return match[0]
    if not candidates:
        if _archives(agent_id, "del"):
            raise UnarchiveError(
                "archive_deleting",
                "only a pending deletion remains for this agent; it cannot be restored",
            )
        raise UnarchiveError("archive_not_found", f"no archive of {agent_id!r} on this machine")
    if len(candidates) > 1:
        raise UnarchiveError(
            "archive_ambiguous",
            f"{len(candidates)} archives of {agent_id!r}; choose one",
            archive_ids=[p.name for p in candidates],
        )
    return candidates[0]


def _load_archived_identity(archive: Path) -> tuple[dict, StoredIdentity]:
    import yaml

    try:
        raw = yaml.safe_load((archive / "agent.yml").read_text(encoding="utf-8")) or {}
        slug = (raw.get("puffo_core") or {}).get("slug")
        if not isinstance(slug, str) or not slug:
            raise ValueError("agent.yml has no puffo_core.slug")
        identity = KeyStore(archive / "keys").load_identity(slug)
        decode_secret(identity.root_secret_key)
    except UnarchiveError:
        raise
    except Exception as exc:  # noqa: BLE001 — anything here means "cannot read"
        raise UnarchiveError("archive_unreadable", f"archived agent unreadable: {exc}") from exc
    return raw, identity


def _staged_request(archive: Path, state: dict, identity: StoredIdentity) -> dict:
    """The signed restore request, minted once and replayed on every retry."""
    staged = state.get("staged")
    if isinstance(staged, dict) and {"device_cert", "subkey_cert", "secrets"} <= staged.keys():
        return staged
    root = Ed25519KeyPair.from_secret_bytes(decode_secret(identity.root_secret_key))
    device = Ed25519KeyPair.generate()
    kem = KemKeyPair.generate()
    subkey = Ed25519KeyPair.generate()
    device_cert = create_device_cert(root, device.public_key_bytes(), kem.public_key_bytes())
    subkey_cert = create_subkey_cert(
        device, device_cert["device_id"], subkey.public_key_bytes(),
    )
    staged = {
        "device_cert": device_cert,
        "subkey_cert": subkey_cert,
        "secrets": {
            "device_signing": encode_secret(device.secret_bytes()),
            "kem": encode_secret(kem.secret_bytes()),
            "subkey": encode_secret(subkey.secret_bytes()),
        },
    }
    state["staged"] = staged
    _save_state(archive, state)
    return staged


async def _post_restore(server_url: str, slug: str, staged: dict) -> dict:
    from .control import machine_auth
    from .control.store import load_or_create_machine

    machine = load_or_create_machine()
    path = f"/v2/machines/me/agents/{slug}/restore-device"
    body = json.dumps({
        "device_cert": staged["device_cert"],
        "subkey_cert": staged["subkey_cert"],
        "device_name": "restored",
    }).encode()
    headers = machine_auth.signed_headers(machine, "POST", path, body)
    headers["content-type"] = "application/json"
    base = server_url.rstrip("/")
    try:
        async with create_remote_http_session(base) as session:
            async with session.post(f"{base}{path}", data=body, headers=headers) as resp:
                text = await resp.text()
                status = resp.status
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise UnarchiveError("restore_pending", f"server unreachable: {exc}") from exc
    if status in (200, 201):
        try:
            return json.loads(text)
        except ValueError:
            return {}
    if status in (408, 429) or status >= 500:
        raise UnarchiveError("restore_pending", f"server busy (HTTP {status}); retry")
    if status == 409 and "not archived" in text:
        raise UnarchiveError("not_archived_on_server", "the server does not consider this agent archived")
    raise UnarchiveError("restore_rejected", f"server refused the restore (HTTP {status}): {text[:300]}")


def _install_identity(archive: Path, identity: StoredIdentity, staged: dict) -> str:
    """Make the new device the agent's identity; keep the old one retired."""
    keys = archive / "keys"
    new_device_id = staged["device_cert"]["device_id"]
    if identity.device_id != new_device_id:
        retired = keys / "retired"
        _ensure_private_directory(retired)
        _atomic_write_private(
            retired / f"{identity.device_id}.json",
            json.dumps(identity.to_dict(), indent=2),
        )
    secrets = staged["secrets"]
    KeyStore(keys).save_identity(StoredIdentity(
        slug=identity.slug,
        device_id=new_device_id,
        root_secret_key=identity.root_secret_key,
        device_signing_secret_key=secrets["device_signing"],
        kem_secret_key=secrets["kem"],
        server_url=identity.server_url,
        slug_binding_json=identity.slug_binding_json,
        identity_cert_json=identity.identity_cert_json,
        identity_profile_json=identity.identity_profile_json,
    ))
    subkey_cert = staged["subkey_cert"]
    _atomic_write_private(keys / f"{identity.slug}.session.json", json.dumps({
        "slug": identity.slug,
        "subkey_id": subkey_cert["subkey_id"],
        "subkey_secret_key": secrets["subkey"],
        "expires_at": subkey_cert["expires_at"],
    }, indent=2))
    return new_device_id


async def _revoke_old_device(identity: StoredIdentity, staged: dict, old_device_id: str) -> str:
    """Revoke the pre-archive device through the new one. Returns an error or ''."""
    from .import_agents import _revoke_old_device as revoke

    secrets = staged["secrets"]
    try:
        await revoke(
            server_url=identity.server_url,
            slug=identity.slug,
            new_device_id=staged["device_cert"]["device_id"],
            new_signing_key=Ed25519KeyPair.from_secret_bytes(decode_secret(secrets["device_signing"])),
            root_signing_key=Ed25519KeyPair.from_secret_bytes(decode_secret(identity.root_secret_key)),
            old_device_id=old_device_id,
            preregistered_subkey=(
                Ed25519KeyPair.from_secret_bytes(decode_secret(secrets["subkey"])),
                staged["subkey_cert"],
            ),
        )
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        # Already revoked is the outcome we want. The server answers a later
        # duplicate revocation with 409 NOT_IMPROVING (revocations.rs); a bare
        # 409 could also be a nonce replay, so match the code, not the status.
        if "NOT_IMPROVING" in text:
            return ""
        return text
    return ""


def _prepare_yml(archive: Path, new_device_id: str) -> None:
    import yaml

    path = archive / "agent.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw["state"] = "paused"
    pc = raw.get("puffo_core") or {}
    pc["device_id"] = new_device_id
    raw["puffo_core"] = pc
    _atomic_write_private(path, yaml.safe_dump(raw, sort_keys=False))


async def _report_paused(agent_id: str) -> bool:
    from .daemon import _report_lifecycle
    from .state import AgentConfig

    try:
        return await _report_lifecycle(AgentConfig.load(agent_id), "paused")
    except Exception as exc:  # noqa: BLE001 — the restore itself already succeeded
        logger.warning("unarchive %s: paused report failed: %s", agent_id, exc)
        return False


async def _server_only(agent_id: str) -> dict:
    """Owner archived it server-side while we were offline: nothing moved."""
    from .state import AgentConfig

    cfg = AgentConfig.load(agent_id)
    cfg.state = "paused"
    cfg.save()
    reported = await _report_paused(agent_id)
    return {
        "ok": True, "agent_slug": agent_id, "state": "paused", "mode": "server_only",
        "device_id": cfg.puffo_core.device_id, "status_reported": reported,
    }


def _discard_state(directory: Path) -> None:
    try:
        _state_path(directory).unlink()
    except FileNotFoundError:
        pass


def _move_back(agent_id: str, archive: Path) -> Path:
    target = agent_dir(agent_id)
    if target.exists():
        raise UnarchiveError("agent_exists", f"agent {agent_id!r} appeared during unarchive")
    _ensure_private_directory(target.parent)
    os.rename(archive, target)
    return target


async def _restore_unrevoked(
    agent_id: str, archive: Path, identity: StoredIdentity, refusal: UnarchiveError,
) -> dict:
    """The archive never finished: its device was never revoked.

    ``_archive_on_flag`` reports ``archived`` *before* revoking and gives up
    on the revoke when that report fails, leaving a marker with
    ``lifecycle_status: archived``. If the server still does not consider the
    agent archived, that report never landed, so the revoke was never sent
    and the old device is still valid — restore in place, no new device.
    Without such a marker the archive did complete (the device is revoked),
    and the server's view genuinely disagrees: refuse rather than guess.
    """
    marker = archive / ".puffo-agent" / "pending_revoke.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if not (
        isinstance(payload, dict)
        and payload.get("lifecycle_status") == "archived"
        and payload.get("device_id") == identity.device_id
    ):
        raise UnarchiveError("restore_rejected", str(refusal))
    dot = archive / ".puffo-agent"
    for leftover in ("archive.flag", "pending_revoke.json"):
        try:
            (dot / leftover).unlink()
        except FileNotFoundError:
            pass
    _prepare_yml(archive, identity.device_id)
    target = _move_back(agent_id, archive)
    _discard_state(target)
    reported = await _report_paused(agent_id)
    logger.info("unarchive %s: archive had not completed; restored in place", agent_id)
    return {
        "ok": True, "agent_slug": agent_id, "state": "paused", "mode": "restored",
        "device_id": identity.device_id, "restored_from": archive.name,
        "old_device_revoke_pending": False, "status_reported": reported,
    }


async def _restore(agent_id: str, archive: Path) -> dict:
    raw, identity = _load_archived_identity(archive)
    server_url = identity.server_url or (raw.get("puffo_core") or {}).get("server_url")
    if not server_url:
        raise UnarchiveError("archive_unreadable", "archived agent has no server_url")
    state = _load_state(archive)
    staged = _staged_request(archive, state, identity)
    try:
        await _post_restore(server_url, identity.slug, staged)
    except UnarchiveError as exc:
        if exc.code != "not_archived_on_server":
            raise
        return await _restore_unrevoked(agent_id, archive, identity, exc)

    # Server accepted the new device — from here the archive is rewritten.
    old_device_id = state.get("old_device_id") or identity.device_id
    if "old_device_id" not in state:
        state["old_device_id"] = old_device_id
        _save_state(archive, state)
    new_device_id = _install_identity(archive, identity, staged)
    revoke_error = ""
    if old_device_id != new_device_id:
        revoke_error = await _revoke_old_device(identity, staged, old_device_id)

    dot = archive / ".puffo-agent"
    for leftover in ("archive.flag", "pending_revoke.json"):
        try:
            (dot / leftover).unlink()
        except FileNotFoundError:
            pass
    _prepare_yml(archive, new_device_id)

    target = _move_back(agent_id, archive)
    # Only now: until the move, a retry must find the same staged certs and
    # the recorded old device, or it would mint yet another device.
    _discard_state(target)
    if revoke_error:
        from .import_agents import _write_pending_revoke

        _write_pending_revoke(agent_id, old_device_id, revoke_error)
        logger.warning(
            "unarchive %s: old device %s not yet revoked (%s); left for revoke-pending",
            agent_id, old_device_id, revoke_error,
        )
    reported = await _report_paused(agent_id)
    logger.info("unarchive %s: restored from %s as device %s", agent_id, archive.name, new_device_id)
    return {
        "ok": True, "agent_slug": agent_id, "state": "paused", "mode": "restored",
        "device_id": new_device_id, "restored_from": archive.name,
        "old_device_revoke_pending": bool(revoke_error), "status_reported": reported,
    }


_locks: dict[str, asyncio.Lock] = {}


async def unarchive_agent(agent_id: str, archive_id: str | None = None) -> dict:
    if not isinstance(agent_id, str) or not is_valid_agent_id(agent_id):
        return {"ok": False, "error_code": "archive_not_found", "error": "invalid agent id"}
    if archive_id is not None and not isinstance(archive_id, str):
        return {"ok": False, "error_code": "archive_not_found", "error": "invalid archive id"}
    # Runs as a background op, so two commands for one agent can overlap;
    # the second must see the first's outcome, not race it on the same dir.
    lock = _locks.setdefault(agent_id, asyncio.Lock())
    async with lock:
        return await _unarchive_locked(agent_id, archive_id)


async def _unarchive_locked(agent_id: str, archive_id: str | None) -> dict:
    archive: Path | None = None
    try:
        if agent_yml_path(agent_id).exists():
            if archive_id is not None:
                raise UnarchiveError(
                    "agent_exists", f"agent {agent_id!r} is on this machine; not restoring an archive over it",
                )
            return await _server_only(agent_id)
        archive = _select_archive(agent_id, archive_id)
        return await _restore(agent_id, archive)
    except UnarchiveError as exc:
        _record_failure(archive, exc)
        return {"ok": False, "error_code": exc.code, "error": str(exc), **exc.fields}
