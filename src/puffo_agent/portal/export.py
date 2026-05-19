"""Multi-agent export: zip N agent dirs + manifest + scrypt+AES-GCM encrypt.

Wire layout::

    magic       16 bytes  b"PUFFO-AGENT-V1\x00\x00"
    kdf_salt    16 bytes
    aead_nonce  12 bytes
    ciphertext  rest      AES-256-GCM(scrypt(password, salt) || zip-bytes)

Inner zip::

    manifest.json
    agents/<id>/...  (full agent dir, *.tmp skipped)
"""

from __future__ import annotations

import io
import json
import os
import secrets
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .state import agent_dir, agent_yml_path

MAGIC = b"PUFFO-AGENT-V1\x00\x00"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1


class ExportError(Exception):
    pass


class ImportPackError(Exception):
    """Raised by ``unpack`` on bad password / corrupt header / bad zip."""


@dataclass(frozen=True)
class AgentManifestEntry:
    id: str
    slug: str
    display_name: str
    old_device_id: str


def derive_key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=KEY_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(
        password.encode("utf-8")
    )


def pack(agent_ids: Iterable[str], password: str, *, exported_by_slug: str = "") -> bytes:
    ids = list(agent_ids)
    if not ids:
        raise ExportError("at least one agent id required")
    if not password:
        raise ExportError("password is required")

    missing = [a for a in ids if not agent_yml_path(a).exists()]
    if missing:
        raise ExportError(f"agent(s) not found: {', '.join(missing)}")

    inner = io.BytesIO()
    manifest_entries: list[dict] = []
    with zipfile.ZipFile(inner, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for agent_id in ids:
            entry = _add_agent(zf, agent_id)
            manifest_entries.append(entry)
        manifest = {
            "format_version": 1,
            "exported_at": int(time.time() * 1000),
            "exported_by_slug": exported_by_slug,
            "agents": manifest_entries,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))

    zip_bytes = inner.getvalue()
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key = derive_key(password, salt)
    aad = MAGIC + salt
    ciphertext = AESGCM(key).encrypt(nonce, zip_bytes, aad)
    return MAGIC + salt + nonce + ciphertext


def _add_agent(zf: zipfile.ZipFile, agent_id: str) -> dict:
    src = agent_dir(agent_id)
    slug = ""
    display_name = ""
    old_device_id = ""
    try:
        from .state import AgentConfig

        cfg = AgentConfig.load(agent_id)
        slug = cfg.puffo_core.slug
        display_name = cfg.display_name
        old_device_id = cfg.puffo_core.device_id
    except Exception:
        pass
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".tmp":
            continue
        arcname = f"agents/{agent_id}/{path.relative_to(src).as_posix()}"
        zf.write(path, arcname=arcname)
    return {
        "id": agent_id,
        "slug": slug,
        "display_name": display_name,
        "old_device_id": old_device_id,
    }


@dataclass(frozen=True)
class UnpackedBundle:
    manifest: dict
    agents: dict[str, dict[str, bytes]]


def unpack(blob: bytes, password: str) -> UnpackedBundle:
    if len(blob) < len(MAGIC) + SALT_LEN + NONCE_LEN + 16:
        raise ImportPackError("archive too short to be a puffo-agent export")
    if blob[: len(MAGIC)] != MAGIC:
        raise ImportPackError("not a puffo-agent export (bad magic)")
    off = len(MAGIC)
    salt = blob[off : off + SALT_LEN]
    off += SALT_LEN
    nonce = blob[off : off + NONCE_LEN]
    off += NONCE_LEN
    ciphertext = blob[off:]
    if not password:
        raise ImportPackError("password is required")
    key = derive_key(password, salt)
    aad = MAGIC + salt
    try:
        zip_bytes = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception:
        raise ImportPackError("decryption failed (wrong password or corrupted archive)")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise ImportPackError(f"inner archive is not a valid zip: {exc}")

    names = set(zf.namelist())
    if "manifest.json" not in names:
        raise ImportPackError("archive missing manifest.json")
    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise ImportPackError("unsupported manifest format")
    declared = manifest.get("agents") or []
    if not isinstance(declared, list) or not declared:
        raise ImportPackError("manifest declares no agents")

    agents: dict[str, dict[str, bytes]] = {}
    for entry in declared:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise ImportPackError("manifest entry missing id")
        agent_id = entry["id"]
        prefix = f"agents/{agent_id}/"
        files: dict[str, bytes] = {}
        for name in names:
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            files[name[len(prefix) :]] = zf.read(name)
        if not files:
            raise ImportPackError(f"archive missing files for agent {agent_id!r}")
        if "agent.yml" not in files:
            raise ImportPackError(f"agent {agent_id!r} missing agent.yml")
        if "profile.md" not in files:
            raise ImportPackError(f"agent {agent_id!r} missing profile.md")
        agents[agent_id] = files

    return UnpackedBundle(manifest=manifest, agents=agents)


def write_unpacked_to_dir(files: dict[str, bytes], dest: Path) -> None:
    """Materialise an unpacked agent's files into ``dest`` (which must
    not exist). Used by import after decryption + validation."""
    if dest.exists():
        raise ExportError(f"destination already exists: {dest}")
    dest.mkdir(parents=True)
    for rel, data in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)


SANITIZE_PATHS = (
    "runtime.json",
    "cli_session.json",
    "messages.db",
    ".puffo-agent/reload.flag",
    ".puffo-agent/refresh.flag",
    ".puffo-agent/restart.flag",
    ".puffo-agent/archive.flag",
    ".puffo-agent/delete.flag",
    ".puffo-agent/current_turn.json",
    "workspace/.claude/.credentials.json",
)


def sanitize_staged_agent(staging_dir: Path) -> None:
    """Drop files that are device-bound to the source machine. Mutates
    the staging dir in place. Idempotent."""
    for rel in SANITIZE_PATHS:
        target = staging_dir / rel
        if target.exists() and target.is_file():
            try:
                os.unlink(target)
            except OSError:
                pass
