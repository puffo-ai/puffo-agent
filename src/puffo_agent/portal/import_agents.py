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

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from ..crypto.certs import create_subkey_cert
from ..crypto.encoding import base64url_decode, base64url_encode
from ..crypto.http_auth import sign_request
from ..crypto.keystore import KeyStore, StoredIdentity, decode_secret, encode_secret
from ..crypto.primitives import Ed25519KeyPair, KemKeyPair
from .export import (
    ImportPackError,
    UnpackedBundle,
    sanitize_staged_agent,
    unpack,
    write_unpacked_to_dir,
)
from .migration_certs import (
    build_root_key_envelope,
    create_device_cert,
    create_device_revocation,
    create_slug_binding,
)
from .state import agent_dir, agent_yml_path, agents_dir

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)


class ImportError(Exception):
    pass


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
    write_unpacked_to_dir(files, stage_dir)
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
        # Best-effort: if the server rejects the subkey (chain
        # validation lag etc.), the worker rotates one on its first
        # request anyway. Skip persisting a session and let the
        # revoke step take its own retry.
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
    for path in list(keys_dir.iterdir()):
        if path.is_file() and path.name != "registered-agents.json":
            path.unlink()
    out_path = keys_dir / f"{old_identity.slug}.json"
    out_path.write_text(json.dumps(new_identity.to_dict(), indent=2), encoding="utf-8")

    if new_subkey is not None and new_subkey_cert is not None:
        session_path = keys_dir / f"{old_identity.slug}.session.json"
        session_path.write_text(json.dumps({
            "slug": old_identity.slug,
            "subkey_id": new_subkey_cert["subkey_id"],
            "subkey_secret_key": encode_secret(new_subkey.secret_bytes()),
            "expires_at": new_subkey_cert["expires_at"],
        }, indent=2), encoding="utf-8")

    _patch_agent_yml_device_id(stage_dir / "agent.yml", new_device_id)


async def _register_new_device_subkey(
    *,
    server_url: str,
    slug: str,
    new_device_id: str,
    new_signing_key: Ed25519KeyPair,
) -> tuple[Ed25519KeyPair, dict]:
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        return await _register_subkey_via_device(
            session,
            server_url=server_url,
            slug=slug,
            device_id=new_device_id,
            device_signing_key=new_signing_key,
        )


def _set_state_running(agent_id: str) -> None:
    import yaml

    yml_path = agent_yml_path(agent_id)
    raw = yaml.safe_load(yml_path.read_text(encoding="utf-8")) or {}
    raw["state"] = "running"
    yml_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _patch_agent_yml_device_id(yml_path: Path, new_device_id: str) -> None:
    import yaml

    raw = yaml.safe_load(yml_path.read_text(encoding="utf-8")) or {}
    pc = raw.get("puffo_core") or {}
    pc["device_id"] = new_device_id
    raw["puffo_core"] = pc
    yml_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _commit_staging(agent_id: str, stage_dir: Path) -> None:
    target = agent_dir(agent_id)
    if target.exists():
        raise ImportError(f"agent dir appeared during import: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
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
            raise ImportError(f"{path} {resp.status}: {text}")


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

    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
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
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
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
