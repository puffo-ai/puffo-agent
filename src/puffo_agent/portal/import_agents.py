"""Multi-agent import with enrollment-style device migration.

Each agent goes through three phases on the new daemon:

1. **stage** — decrypt + write files to ``agents/.import-staging/<id>/``
   + sanitize device-bound files.
2. **enrol + revoke** — talk to puffo-server (signed by the OLD
   device's subkey, derived from the imported bundle): submit an
   enrollment for a freshly-generated device key + KEM key, then
   revoke the OLD device id. Revoke is best-effort; on failure we
   leave a ``pending_revoke.json`` marker for ``revoke_pending``
   retry.
3. **commit** — atomic rename staging → ``agents/<id>/``.

Phase 2 is the commit point: once the server has registered the new
device, the daemon writes the new keys to staging. Phase 3 makes it
visible to the reconciler. Re-running ``import`` is idempotent — if
the agent dir already exists it's skipped; pending revokes are
handled by the separate ``revoke_pending`` helper.
"""

from __future__ import annotations

import enum
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from ..agent.event_kinds import EventKind
from ..agent.events import random_nonce, sign_event
from ..crypto.certs import create_subkey_cert
from ..crypto.encoding import base64url_encode
from ..crypto.http_auth import sign_request
from ..crypto.http_session import create_remote_http_session
from ..crypto.keystore import KeyStore, StoredIdentity, decode_secret, encode_secret
from ..crypto.primitives import Ed25519KeyPair, KemKeyPair
from .export import (
    UnpackedBundle,
    sanitize_staged_agent,
    unpack,
)
from .host_assets import _atomic_write_private, _ensure_private_directory
from .migration_certs import (
    build_root_key_envelope,
    create_device_cert,
    create_device_revocation,
)
from .state import agent_dir, agent_yml_path, agents_dir

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)


def _remote_http_session(server_url: str) -> aiohttp.ClientSession:
    return create_remote_http_session(server_url, timeout=HTTP_TIMEOUT)


class ImportError(Exception):
    pass


class HttpStatusError(ImportError):
    """Carries the response status so callers can tell a retryable blip
    from a permanent rejection."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class AgentImportResult:
    agent_id: str
    status: str  # "imported" | "skipped" | "failed" | "imported_pending_revoke"
    detail: str = ""
    new_device_id: str = ""
    old_device_id: str = ""


@dataclass
class ImportReport:
    results: list[AgentImportResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def imported(self) -> int:
        return sum(1 for r in self.results if r.status.startswith("imported"))

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")

    @property
    def pending_revokes(self) -> int:
        return sum(1 for r in self.results if r.status == "imported_pending_revoke")


def staging_dir(agent_id: str) -> Path:
    return agents_dir() / ".import-staging" / agent_id


def pending_revoke_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / ".puffo-agent" / "pending_revoke.json"


async def import_bundle(blob: bytes, password: str) -> ImportReport:
    bundle: UnpackedBundle = unpack(blob, password)
    results: list[AgentImportResult] = []
    for agent_id, files in bundle.agents.items():
        try:
            results.append(await _import_one(agent_id, files))
        except Exception as exc:
            logger.exception("import: agent=%s unexpected error", agent_id)
            _cleanup_staging(agent_id)
            results.append(
                AgentImportResult(agent_id=agent_id, status="failed", detail=str(exc))
            )
    return ImportReport(results=results)


async def _import_one(agent_id: str, files: dict[str, bytes]) -> AgentImportResult:
    if agent_yml_path(agent_id).exists():
        detail = "agent already exists on this daemon"
        if pending_revoke_path(agent_id).exists():
            detail += " — pending revoke; run `puffo-agent agent revoke-pending`"
        return AgentImportResult(agent_id=agent_id, status="skipped", detail=detail)

    _cleanup_staging(agent_id)
    stage_dir = staging_dir(agent_id)
    _write_unpacked_private(files, stage_dir)
    sanitize_staged_agent(stage_dir)

    old_identity = _load_old_identity(stage_dir)
    server_url = old_identity.server_url
    if not server_url:
        _cleanup_staging(agent_id)
        return AgentImportResult(
            agent_id=agent_id, status="failed", detail="bundle missing server_url",
        )

    new_signing = Ed25519KeyPair.generate()
    new_kem = KemKeyPair.generate()

    try:
        await _enroll_new_device(server_url, old_identity, new_signing, new_kem)
    except Exception as exc:
        _cleanup_staging(agent_id)
        return AgentImportResult(
            agent_id=agent_id, status="failed", detail=f"enrollment failed: {exc}",
        )

    new_device_id = _device_id_from_pk(new_signing.public_key_bytes())

    new_subkey: Ed25519KeyPair | None = None
    new_subkey_cert: dict | None = None
    try:
        new_subkey, new_subkey_cert = await _register_new_device_subkey(
            server_url=server_url,
            slug=old_identity.slug,
            new_device_id=new_device_id,
            new_signing_key=new_signing,
        )
    except Exception as exc:
        # Best-effort: worker self-rotates a subkey on first request if none was persisted.
        logger.warning("import: agent=%s new subkey registration failed: %s", agent_id, exc)

    _write_new_identity(
        stage_dir, old_identity, new_signing, new_kem, new_device_id,
        new_subkey=new_subkey, new_subkey_cert=new_subkey_cert,
    )
    _commit_staging(agent_id, stage_dir)

    preregistered = (new_subkey, new_subkey_cert) if new_subkey and new_subkey_cert else None
    revoke_ok = False
    revoke_err = ""
    try:
        await _revoke_old_device(
            server_url=server_url,
            slug=old_identity.slug,
            new_device_id=new_device_id,
            new_signing_key=new_signing,
            root_signing_key=Ed25519KeyPair.from_secret_bytes(
                decode_secret(old_identity.root_secret_key)
            ),
            old_device_id=old_identity.device_id,
            preregistered_subkey=preregistered,
        )
        revoke_ok = True
    except Exception as exc:
        revoke_err = str(exc)
        _write_pending_revoke(agent_id, old_identity.device_id, revoke_err)
        logger.warning(
            "import: agent=%s old device revoke failed: %s (left pending_revoke.json)",
            agent_id, revoke_err,
        )

    try:
        _set_state_running(agent_id)
    except Exception as exc:
        logger.warning("import: agent=%s could not flip state to running: %s", agent_id, exc)

    return AgentImportResult(
        agent_id=agent_id,
        status="imported" if revoke_ok else "imported_pending_revoke",
        new_device_id=new_device_id,
        old_device_id=old_identity.device_id,
        detail="" if revoke_ok else f"new device active; old revoke failed: {revoke_err}",
    )


def _write_unpacked_private(files: dict[str, bytes], dest: Path) -> None:
    """Materialize a decrypted bundle under private staging permissions."""
    if dest.exists():
        raise ImportError(f"staging destination already exists: {dest}")
    _ensure_private_directory(dest.parent)
    _ensure_private_directory(dest)
    for rel, data in files.items():
        target = dest / rel
        _ensure_private_directory(target.parent)
        _atomic_write_private(target, data)


def _load_old_identity(stage_dir: Path) -> StoredIdentity:
    keys_dir = stage_dir / "keys"
    if not keys_dir.is_dir():
        raise ImportError("bundle missing keys/ directory")
    json_files = [p for p in keys_dir.iterdir() if p.suffix == ".json" and ".session" not in p.name]
    if len(json_files) != 1:
        raise ImportError(f"expected exactly one identity JSON in keys/, found {len(json_files)}")
    raw = json.loads(json_files[0].read_text(encoding="utf-8"))
    return StoredIdentity(
        slug=raw["slug"],
        device_id=raw["device_id"],
        root_secret_key=raw["root_secret_key"],
        device_signing_secret_key=raw["device_signing_secret_key"],
        kem_secret_key=raw["kem_secret_key"],
        server_url=raw["server_url"],
        slug_binding_json=raw.get("slug_binding_json"),
        identity_cert_json=raw.get("identity_cert_json"),
        identity_profile_json=raw.get("identity_profile_json"),
    )


def _device_id_from_pk(signing_pk: bytes) -> str:
    from ..crypto.certs import derive_public_key_id

    return derive_public_key_id("dev", signing_pk)


def _write_new_identity(
    stage_dir: Path,
    old_identity: StoredIdentity,
    new_signing: Ed25519KeyPair,
    new_kem: KemKeyPair,
    new_device_id: str,
    *,
    new_subkey: Ed25519KeyPair | None = None,
    new_subkey_cert: dict | None = None,
) -> None:
    new_identity = StoredIdentity(
        slug=old_identity.slug,
        device_id=new_device_id,
        root_secret_key=old_identity.root_secret_key,
        device_signing_secret_key=encode_secret(new_signing.secret_bytes()),
        kem_secret_key=encode_secret(new_kem.secret_bytes()),
        server_url=old_identity.server_url,
        slug_binding_json=old_identity.slug_binding_json,
        identity_cert_json=old_identity.identity_cert_json,
        identity_profile_json=old_identity.identity_profile_json,
    )
    keys_dir = stage_dir / "keys"
    backup_dek_name = f"{old_identity.slug}.message-backup-dek-v1"
    backup_dek_path = keys_dir / backup_dek_name
    if backup_dek_path.exists():
        if backup_dek_path.is_symlink() or not backup_dek_path.is_file():
            raise ImportError("bundle message backup key is not a regular file")
        if backup_dek_path.stat().st_size != 32:
            raise ImportError("bundle message backup key has invalid length")
        backup_dek_path.chmod(0o600)
    for path in list(keys_dir.iterdir()):
        if path.is_file() and path.name not in {
            "registered-agents.json",
            backup_dek_name,
        }:
            path.unlink()
    out_path = keys_dir / f"{old_identity.slug}.json"
    _atomic_write_private(out_path, json.dumps(new_identity.to_dict(), indent=2))

    if new_subkey is not None and new_subkey_cert is not None:
        session_path = keys_dir / f"{old_identity.slug}.session.json"
        _atomic_write_private(session_path, json.dumps({
            "slug": old_identity.slug,
            "subkey_id": new_subkey_cert["subkey_id"],
            "subkey_secret_key": encode_secret(new_subkey.secret_bytes()),
            "expires_at": new_subkey_cert["expires_at"],
        }, indent=2))

    _patch_agent_yml_device_id(stage_dir / "agent.yml", new_device_id)


async def _register_new_device_subkey(
    *,
    server_url: str,
    slug: str,
    new_device_id: str,
    new_signing_key: Ed25519KeyPair,
) -> tuple[Ed25519KeyPair, dict]:
    async with _remote_http_session(server_url) as session:
        return await _register_subkey_via_device(
            session,
            server_url=server_url,
            slug=slug,
            device_id=new_device_id,
            device_signing_key=new_signing_key,
        )


def _set_state_running(agent_id: str) -> None:
    import yaml

    path = agent_yml_path(agent_id)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw["state"] = "running"
    _atomic_write_private(path, yaml.safe_dump(raw, sort_keys=False))


def _patch_agent_yml_device_id(yml_path: Path, new_device_id: str) -> None:
    import yaml

    raw = yaml.safe_load(yml_path.read_text(encoding="utf-8")) or {}
    pc = raw.get("puffo_core") or {}
    pc["device_id"] = new_device_id
    raw["puffo_core"] = pc
    _atomic_write_private(yml_path, yaml.safe_dump(raw, sort_keys=False))


def _commit_staging(agent_id: str, stage_dir: Path) -> None:
    target = agent_dir(agent_id)
    if target.exists():
        raise ImportError(f"agent dir appeared during import: {target}")
    _ensure_private_directory(target.parent)
    shutil.move(str(stage_dir), str(target))


def _cleanup_staging(agent_id: str) -> None:
    stage = staging_dir(agent_id)
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)


def _write_pending_revoke(agent_id: str, old_device_id: str, last_error: str) -> None:
    path = pending_revoke_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "old_device_id": old_device_id,
                "last_error": last_error,
                "attempted_at": int(time.time() * 1000),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


async def _signed_post(
    session: aiohttp.ClientSession,
    *,
    server_url: str,
    path: str,
    signer_key: Ed25519KeyPair,
    signer_id: str,
    slug: str,
    body_dict: dict,
) -> None:
    body_bytes = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")
    headers = sign_request(
        signing_key=signer_key,
        slug=slug,
        signer_id=signer_id,
        method="POST",
        path=path,
        body=body_bytes,
    ).to_dict()
    async with session.post(
        f"{server_url.rstrip('/')}{path}",
        data=body_bytes,
        headers=headers,
    ) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise HttpStatusError(f"{path} {resp.status}: {text}", resp.status)


async def _signed_get(
    session: aiohttp.ClientSession,
    *,
    server_url: str,
    path: str,
    signer_key: Ed25519KeyPair,
    signer_id: str,
    slug: str,
) -> dict:
    headers = sign_request(
        signing_key=signer_key,
        slug=slug,
        signer_id=signer_id,
        method="GET",
        path=path,
        body=b"",
    ).to_dict()
    async with session.get(f"{server_url.rstrip('/')}{path}", headers=headers) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise HttpStatusError(f"{path} {resp.status}: {text}", resp.status)
        return await resp.json()


async def _register_subkey_via_device(
    session: aiohttp.ClientSession,
    *,
    server_url: str,
    slug: str,
    device_id: str,
    device_signing_key: Ed25519KeyPair,
) -> tuple[Ed25519KeyPair, dict]:
    subkey = Ed25519KeyPair.generate()
    cert = create_subkey_cert(device_signing_key, device_id, subkey.public_key_bytes())
    await _signed_post(
        session,
        server_url=server_url,
        path="/devices/subkeys",
        signer_key=device_signing_key,
        signer_id=device_id,
        slug=slug,
        body_dict={"subkey_cert": cert},
    )
    return subkey, cert


async def _enroll_new_device(
    server_url: str,
    old_identity: StoredIdentity,
    new_signing: Ed25519KeyPair,
    new_kem: KemKeyPair,
) -> None:
    import secrets

    if not old_identity.identity_cert_json or not old_identity.slug_binding_json:
        raise ImportError("bundle missing identity_cert or slug_binding")

    root_signing = Ed25519KeyPair.from_secret_bytes(decode_secret(old_identity.root_secret_key))
    old_device_signing = Ed25519KeyPair.from_secret_bytes(
        decode_secret(old_identity.device_signing_secret_key)
    )
    new_signing_pk = new_signing.public_key_bytes()
    new_kem_pk = new_kem.public_key_bytes()
    signing_pk_b64 = base64url_encode(new_signing_pk)
    kem_pk_b64 = base64url_encode(new_kem_pk)
    nonce = base64url_encode(secrets.token_bytes(32))

    async with _remote_http_session(server_url) as session:
        old_subkey, old_subkey_cert = await _register_subkey_via_device(
            session,
            server_url=server_url,
            slug=old_identity.slug,
            device_id=old_identity.device_id,
            device_signing_key=old_device_signing,
        )

        async with session.post(
            f"{server_url.rstrip('/')}/devices/enroll/init",
            json={
                "nonce": nonce,
                "device_signing_public_key": signing_pk_b64,
                "device_kem_public_key": kem_pk_b64,
                "fingerprint": f"{signing_pk_b64[:8]}..{kem_pk_b64[:8]}",
            },
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise ImportError(f"enroll/init {resp.status}: {text}")

        device_cert = create_device_cert(root_signing, new_signing_pk, new_kem_pk)
        root_envelope = build_root_key_envelope(
            decode_secret(old_identity.root_secret_key), nonce, new_kem_pk,
        )
        body_dict = {
            "device_cert": device_cert,
            "root_key_envelope": root_envelope,
            "slug_binding": json.loads(old_identity.slug_binding_json),
            "identity_cert": json.loads(old_identity.identity_cert_json),
            "identity_profile": (
                json.loads(old_identity.identity_profile_json)
                if old_identity.identity_profile_json
                else None
            ),
        }
        await _signed_post(
            session,
            server_url=server_url,
            path=f"/devices/enroll/{nonce}/complete",
            signer_key=old_subkey,
            signer_id=old_subkey_cert["subkey_id"],
            slug=old_identity.slug,
            body_dict=body_dict,
        )


async def _revoke_old_device(
    *,
    server_url: str,
    slug: str,
    new_device_id: str,
    new_signing_key: Ed25519KeyPair,
    root_signing_key: Ed25519KeyPair,
    old_device_id: str,
    preregistered_subkey: tuple[Ed25519KeyPair, dict] | None = None,
) -> None:
    revocation = create_device_revocation(root_signing_key, old_device_id)
    async with _remote_http_session(server_url) as session:
        if preregistered_subkey is not None:
            new_subkey, new_subkey_cert = preregistered_subkey
        else:
            new_subkey, new_subkey_cert = await _register_subkey_via_device(
                session,
                server_url=server_url,
                slug=slug,
                device_id=new_device_id,
                device_signing_key=new_signing_key,
            )
        await _signed_post(
            session,
            server_url=server_url,
            path=f"/devices/{old_device_id}/revoke",
            signer_key=new_subkey,
            signer_id=new_subkey_cert["subkey_id"],
            slug=slug,
            body_dict=revocation,
        )


async def revoke_pending(agent_id: str) -> AgentImportResult:
    if not agent_yml_path(agent_id).exists():
        return AgentImportResult(
            agent_id=agent_id, status="failed", detail="agent not found",
        )
    path = pending_revoke_path(agent_id)
    if not path.exists():
        return AgentImportResult(
            agent_id=agent_id, status="skipped", detail="no pending revoke",
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    old_device_id = payload["old_device_id"]

    from .state import AgentConfig

    cfg = AgentConfig.load(agent_id)
    identity = KeyStore.for_agent(agent_id).load_identity(cfg.puffo_core.slug)
    root_signing = Ed25519KeyPair.from_secret_bytes(decode_secret(identity.root_secret_key))
    new_signing = Ed25519KeyPair.from_secret_bytes(
        decode_secret(identity.device_signing_secret_key)
    )

    try:
        await _revoke_old_device(
            server_url=identity.server_url,
            slug=identity.slug,
            new_device_id=identity.device_id,
            new_signing_key=new_signing,
            root_signing_key=root_signing,
            old_device_id=old_device_id,
        )
    except Exception as exc:
        _write_pending_revoke(agent_id, old_device_id, str(exc))
        return AgentImportResult(
            agent_id=agent_id,
            status="failed",
            detail=f"revoke retry failed: {exc}",
            old_device_id=old_device_id,
        )
    try:
        path.unlink()
    except OSError:
        pass
    return AgentImportResult(
        agent_id=agent_id, status="imported", old_device_id=old_device_id,
    )


def list_pending_revokes() -> list[tuple[str, str]]:
    """Scan all agent dirs for pending_revoke.json. Returns
    [(agent_id, old_device_id), ...]."""
    out: list[tuple[str, str]] = []
    root = agents_dir()
    if not root.exists():
        return out
    for child in root.iterdir():
        if not child.is_dir() or child.name == ".import-staging":
            continue
        marker = pending_revoke_path(child.name)
        if marker.exists():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                out.append((child.name, payload.get("old_device_id", "")))
            except Exception:
                pass
    return out


def cleanup_staging_dir() -> None:
    """Sweep any leftover ``.import-staging/`` entries from a previous
    crashed import. Safe to call at daemon startup."""
    root = agents_dir() / ".import-staging"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────
# Self-revoke for archive / delete
# ─────────────────────────────────────────────────────────────────────


async def self_revoke_device(
    *,
    server_url: str,
    slug: str,
    device_id: str,
    device_signing_key: Ed25519KeyPair,
    root_signing_key: Ed25519KeyPair,
    preregistered_subkey: tuple[Ed25519KeyPair, dict] | None = None,
) -> None:
    # POST signed by a subkey of THIS device — valid at request-time
    # even though the revoke is about to apply.
    revocation = create_device_revocation(root_signing_key, device_id)
    async with _remote_http_session(server_url) as session:
        if preregistered_subkey is not None:
            subkey, cert = preregistered_subkey
        else:
            subkey, cert = await _register_subkey_via_device(
                session,
                server_url=server_url,
                slug=slug,
                device_id=device_id,
                device_signing_key=device_signing_key,
            )
        await _signed_post(
            session,
            server_url=server_url,
            path=f"/devices/{device_id}/revoke",
            signer_key=subkey,
            signer_id=cert["subkey_id"],
            slug=slug,
            body_dict=revocation,
        )


def _fresh_session_subkey(
    keystore: KeyStore, slug: str
) -> tuple[Ed25519KeyPair, dict] | None:
    """Reuse a still-valid session subkey from disk to skip a redundant
    ``/devices/subkeys`` POST. ``None`` when absent or due for rotation."""
    try:
        from ..crypto.certs import needs_rotation
        sess = keystore.load_session(slug)
    except FileNotFoundError:
        return None
    if needs_rotation(sess.expires_at):
        return None
    return (
        Ed25519KeyPair.from_secret_bytes(decode_secret(sess.subkey_secret_key)),
        {"subkey_id": sess.subkey_id},
    )


async def revoke_archived_device(archived_dir: Path, *, slug: str) -> None:
    keystore = KeyStore(archived_dir / "keys")
    identity = keystore.load_identity(slug)
    root_signing = Ed25519KeyPair.from_secret_bytes(
        decode_secret(identity.root_secret_key)
    )
    device_signing = Ed25519KeyPair.from_secret_bytes(
        decode_secret(identity.device_signing_secret_key)
    )
    preregistered = _fresh_session_subkey(keystore, slug)
    await self_revoke_device(
        server_url=identity.server_url,
        slug=identity.slug,
        device_id=identity.device_id,
        device_signing_key=device_signing,
        root_signing_key=root_signing,
        preregistered_subkey=preregistered,
    )


def archived_pending_revoke_path(archived_agent_dir: Path) -> Path:
    return archived_agent_dir / ".puffo-agent" / "pending_revoke.json"


def write_archived_pending_revoke(
    archived_agent_dir: Path,
    *,
    server_url: str,
    slug: str,
    device_id: str,
    last_error: str,
) -> None:
    path = archived_pending_revoke_path(archived_agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "kind": "archive_self_revoke",
                "server_url": server_url,
                "slug": slug,
                "device_id": device_id,
                "last_error": last_error,
                "attempted_at": int(time.time() * 1000),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


OWNER_LEAVE_REJECTION = "owner must transfer ownership before leaving"


@dataclass
class LeaveSpacesResult:
    """Outcome of the on-archive space-leave fanout. ``failed`` is the
    only retryable bucket; both skip buckets are permanent and must never
    hold up the revoke, since retrying them would never converge.

    ``skipped_owner`` needs a human to transfer the space first.
    ``skipped_permanent`` is a non-403 4xx — a rejection the server will
    repeat identically on every retry."""

    left: list[str]
    skipped_owner: list[str]
    failed: list[str]
    skipped_permanent: list[str] = field(default_factory=list)
    last_error: str = ""


def _is_permanent_leave_rejection(exc: Exception) -> bool:
    """Per AC #4: a 4xx other than 403 is the server telling us this
    request is wrong, not that it's busy — retrying can't fix it. 403 is
    excluded because a non-owner 403 can be a transient auth blip, and
    429 is a rate-limit, i.e. explicitly retryable."""
    status = getattr(exc, "status", 0)
    return 400 <= status < 500 and status not in (403, 429)


def archived_pending_leave_path(archived_agent_dir: Path) -> Path:
    return archived_agent_dir / ".puffo-agent" / "pending_leave_memberships.json"


def write_archived_pending_leave(
    archived_agent_dir: Path,
    *,
    slug: str,
    space_ids: list[str],
    last_error: str,
) -> None:
    """Marker for the startup sweep. ``space_ids`` is what failed on this
    pass — informational only; the retry re-queries ``GET /spaces`` so it
    converges on server-authoritative state rather than a stale list."""
    path = archived_pending_leave_path(archived_agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "kind": "archive_leave_spaces",
                "slug": slug,
                "space_ids": space_ids,
                "last_error": last_error,
                "attempted_at": int(time.time() * 1000),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


async def leave_all_spaces(archived_dir: Path, *, slug: str) -> LeaveSpacesResult:
    """Sign a ``LeaveSpace`` for every space the agent still belongs to,
    so an archived agent stops showing up in channel rosters and the
    @-mention picker (PUF-430).

    MUST run before ``revoke_archived_device`` — once the device is
    revoked its subkeys are 401'd and nothing can be signed. Spaces are
    enumerated from ``GET /spaces`` (server-authoritative; local state
    can be stale) and the server cascades each ``LeaveSpace`` to every
    channel in that space, so per-space is enough.

    Raises on pre-flight failure (keystore, subkey, or the enumerate
    GET); per-space failures are collected into the result instead.
    """
    keystore = KeyStore(archived_dir / "keys")
    identity = keystore.load_identity(slug)
    device_signing = Ed25519KeyPair.from_secret_bytes(
        decode_secret(identity.device_signing_secret_key)
    )
    server_url = identity.server_url
    result = LeaveSpacesResult(left=[], skipped_owner=[], failed=[])
    async with _remote_http_session(server_url) as session:
        preregistered = _fresh_session_subkey(keystore, slug)
        if preregistered is not None:
            subkey, cert = preregistered
        else:
            subkey, cert = await _register_subkey_via_device(
                session,
                server_url=server_url,
                slug=slug,
                device_id=identity.device_id,
                device_signing_key=device_signing,
            )
        subkey_id = cert["subkey_id"]
        data = await _signed_get(
            session,
            server_url=server_url,
            path="/spaces",
            signer_key=subkey,
            signer_id=subkey_id,
            slug=slug,
        )
        for entry in data.get("spaces") or []:
            space_id = (entry.get("space_id") or "").strip()
            if not space_id:
                continue
            # The server rejects an owner's LeaveSpace outright; skip it
            # here so we don't spend a round-trip to be told so.
            if (entry.get("role") or "") == "owner":
                result.skipped_owner.append(space_id)
                continue
            event = sign_event(
                kind=EventKind.LEAVE_SPACE,
                payload={
                    "space_id": space_id,
                    "effective_from": int(time.time() * 1000),
                    "nonce": random_nonce(),
                },
                signer_slug=slug,
                signer_device_id=identity.device_id,
                signer_subkey_id=subkey_id,
                signing_key=subkey,
            )
            try:
                await _signed_post(
                    session,
                    server_url=server_url,
                    path="/spaces/events",
                    signer_key=subkey,
                    signer_id=subkey_id,
                    slug=slug,
                    body_dict={"space_id": space_id, "events": [event]},
                )
            except Exception as exc:  # noqa: BLE001
                if OWNER_LEAVE_REJECTION in str(exc):
                    result.skipped_owner.append(space_id)
                    continue
                if _is_permanent_leave_rejection(exc):
                    result.skipped_permanent.append(space_id)
                    logger.warning(
                        "archive leave: space %s permanently rejected for %s "
                        "(%s); not retrying",
                        space_id, slug, exc,
                    )
                    continue
                result.failed.append(space_id)
                result.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "archive leave: space %s failed for %s: %s",
                    space_id, slug, exc,
                )
            else:
                result.left.append(space_id)
    return result


class _RetryOutcome(enum.Enum):
    SUCCEEDED = "succeeded"          # revoke posted; marker removed
    TRANSIENT = "transient"          # server/network blip; retry next sweep
    UNRETRYABLE = "unretryable"      # bad schema / missing keys; renamed to .broken


async def _retry_archived_pending_revoke(
    archived_path: Path,
) -> _RetryOutcome:
    marker = archived_pending_revoke_path(archived_path)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        server_url = payload["server_url"]
        slug = payload["slug"]
        device_id = payload["device_id"]
    except (OSError, ValueError, KeyError) as exc:
        logger.warning(
            "pending revoke at %s is unparseable (%s); renaming to .broken",
            marker, exc,
        )
        _mark_pending_revoke_broken(marker, str(exc))
        return _RetryOutcome.UNRETRYABLE
    keystore = KeyStore(archived_path / "keys")
    try:
        identity = keystore.load_identity(slug)
    except FileNotFoundError as exc:
        logger.warning(
            "pending revoke at %s: keystore missing (%s); renaming to .broken",
            marker, exc,
        )
        _mark_pending_revoke_broken(marker, f"keystore missing: {exc}")
        return _RetryOutcome.UNRETRYABLE
    root_signing = Ed25519KeyPair.from_secret_bytes(
        decode_secret(identity.root_secret_key)
    )
    device_signing = Ed25519KeyPair.from_secret_bytes(
        decode_secret(identity.device_signing_secret_key)
    )
    try:
        await self_revoke_device(
            server_url=server_url,
            slug=slug,
            device_id=device_id,
            device_signing_key=device_signing,
            root_signing_key=root_signing,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pending revoke retry for %s failed: %s; will try again",
            archived_path.name, exc,
        )
        return _RetryOutcome.TRANSIENT
    try:
        marker.unlink()
    except OSError:
        pass
    logger.info("pending revoke retry for %s ok", archived_path.name)
    return _RetryOutcome.SUCCEEDED


async def _retry_archived_pending_leave(archived_path: Path) -> bool:
    """Retry the space-leave fanout for one archived dir. Returns whether
    the revoke may proceed this pass — ``False`` only for a transient
    leave failure, because revoking now would 401 every future retry."""
    marker = archived_pending_leave_path(archived_path)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        slug = payload["slug"]
    except (OSError, ValueError, KeyError) as exc:
        logger.warning(
            "pending leave at %s is unparseable (%s); renaming to .broken",
            marker, exc,
        )
        _mark_pending_revoke_broken(marker, str(exc))
        return True
    try:
        result = await leave_all_spaces(archived_path, slug=slug)
    except FileNotFoundError as exc:
        logger.warning(
            "pending leave at %s: keystore missing (%s); renaming to .broken",
            marker, exc,
        )
        _mark_pending_revoke_broken(marker, f"keystore missing: {exc}")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pending leave retry for %s failed: %s; will try again",
            archived_path.name, exc,
        )
        return False
    if result.failed:
        write_archived_pending_leave(
            archived_path,
            slug=slug,
            space_ids=result.failed,
            last_error=result.last_error,
        )
        return False
    try:
        marker.unlink()
    except OSError:
        pass
    logger.info(
        "pending leave retry for %s ok (left %d, owner-skipped %d)",
        archived_path.name, len(result.left), len(result.skipped_owner),
    )
    return True


def _mark_pending_revoke_broken(marker: Path, reason: str) -> None:
    # Rename so the next sweep doesn't keep warning; left on disk for
    # operator inspection.
    broken = marker.with_suffix(marker.suffix + ".broken")
    try:
        marker.replace(broken)
    except OSError as exc:
        logger.warning(
            "could not rename %s to .broken: %s; leaving in place "
            "(will warn again next sweep)",
            marker, exc,
        )


async def sweep_archived_pending_revokes() -> int:
    # Returns count of markers actually retried successfully.
    from .state import archived_dir

    root = archived_dir()
    if not root.exists():
        return 0
    retried = 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        has_leave = archived_pending_leave_path(entry).exists()
        has_revoke = archived_pending_revoke_path(entry).exists()
        if not (has_leave or has_revoke):
            continue
        # Leave first, always: the revoke kills the credential that signs
        # the leave, so a revoke that lands early strands the memberships
        # forever (PUF-430).
        if has_leave and not await _retry_archived_pending_leave(entry):
            continue
        if not has_revoke:
            continue
        outcome = await _retry_archived_pending_revoke(entry)
        if outcome is _RetryOutcome.SUCCEEDED:
            retried += 1
    return retried
