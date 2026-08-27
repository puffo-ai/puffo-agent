from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..portal.host_assets import (
    _atomic_write_private,
    _ensure_private_directory,
    _set_private_file_mode,
)
from ..portal.state import home_dir
from .encoding import base64url_decode, base64url_encode


@dataclass
class StoredIdentity:
    slug: str
    device_id: str
    root_secret_key: str
    device_signing_secret_key: str
    kem_secret_key: str
    server_url: str
    slug_binding_json: Optional[str] = None
    identity_cert_json: Optional[str] = None
    identity_profile_json: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "slug": self.slug,
            "device_id": self.device_id,
            "root_secret_key": self.root_secret_key,
            "device_signing_secret_key": self.device_signing_secret_key,
            "kem_secret_key": self.kem_secret_key,
            "server_url": self.server_url,
        }
        if self.slug_binding_json is not None:
            d["slug_binding_json"] = self.slug_binding_json
        if self.identity_cert_json is not None:
            d["identity_cert_json"] = self.identity_cert_json
        if self.identity_profile_json is not None:
            d["identity_profile_json"] = self.identity_profile_json
        return d

    @staticmethod
    def from_dict(d: dict) -> StoredIdentity:
        return StoredIdentity(
            slug=d["slug"],
            device_id=d["device_id"],
            root_secret_key=d["root_secret_key"],
            device_signing_secret_key=d["device_signing_secret_key"],
            kem_secret_key=d["kem_secret_key"],
            server_url=d["server_url"],
            slug_binding_json=d.get("slug_binding_json"),
            identity_cert_json=d.get("identity_cert_json"),
            identity_profile_json=d.get("identity_profile_json"),
        )


@dataclass
class Session:
    slug: str
    subkey_id: str
    subkey_secret_key: str
    expires_at: int  # milliseconds since epoch


def _now_ms() -> int:
    return int(time.time() * 1000)


def encode_secret(key_bytes: bytes) -> str:
    return base64url_encode(key_bytes)


def decode_secret(encoded: str) -> bytes:
    b = base64url_decode(encoded)
    if len(b) != 32:
        raise ValueError(f"secret key must be 32 bytes, got {len(b)}")
    return b


class KeyStore:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)

    @staticmethod
    def for_agent(agent_id: str) -> KeyStore:
        return KeyStore(home_dir() / "agents" / agent_id / "keys")

    def _identity_path(self, slug: str) -> Path:
        return self.base_dir / f"{slug}.json"

    def _session_path(self, slug: str) -> Path:
        return self.base_dir / f"{slug}.session.json"

    def _message_backup_dek_path(self, slug: str) -> Path:
        """Private v1 custody path for an Agent-owned remote-data DEK."""
        if (
            not isinstance(slug, str)
            or not slug
            or "/" in slug
            or "\\" in slug
            or slug in {".", ".."}
        ):
            raise ValueError("invalid key owner")
        return self.base_dir / f"{slug}.message-backup-dek-v1"

    @staticmethod
    def _read_private_32_byte_key(path: Path) -> bytes:
        """Read one regular 0600-ish key file without following links."""
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            raise
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("message backup key is not a regular file")
        if os.name != "nt" and before.st_mode & 0o077:
            raise ValueError("message backup key file permissions are unsafe")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            current = os.fstat(fd)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_ino != before.st_ino
                or current.st_dev != before.st_dev
                or (os.name != "nt" and current.st_mode & 0o077)
            ):
                raise ValueError("message backup key file changed unsafely")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 64)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(part) for part in chunks) > 32:
                    break
            key = b"".join(chunks)
        finally:
            os.close(fd)
        if len(key) != 32:
            raise ValueError("message backup key has invalid length")
        return key

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Best-effort durability for an atomic key publication on POSIX."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def load_or_create_message_backup_dek(self, slug: str) -> bytes:
        """Return one stable random 32-byte MessageBackupDEK for this Agent.

        It lives beside the Agent's existing private key state, never in
        ``messages.db`` or an identity/session JSON file.  A prepared 0600
        temporary file is hard-linked into place, so competing first opens
        either publish exactly one complete key or load that complete key.
        """
        path = self._message_backup_dek_path(slug)
        _ensure_private_directory(self.base_dir)
        try:
            return self._read_private_32_byte_key(path)
        except FileNotFoundError:
            pass

        temporary = self.base_dir / (
            f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
        )
        fd: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temporary, flags, 0o600)
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            key = os.urandom(32)
            written = 0
            while written < len(key):
                count = os.write(fd, key[written:])
                if count <= 0:
                    raise OSError("failed to write message backup key")
                written += count
            os.fsync(fd)
            os.close(fd)
            fd = None
            try:
                # link(2) creates the destination atomically and refuses to
                # replace a concurrent creator's file.
                os.link(temporary, path)
            except FileExistsError:
                return self._read_private_32_byte_key(path)
            self._fsync_directory(self.base_dir)
            return key
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def save_identity(self, identity: StoredIdentity) -> None:
        _ensure_private_directory(self.base_dir)
        path = self._identity_path(identity.slug)
        _atomic_write_private(path, json.dumps(identity.to_dict(), indent=2))

    def load_identity(self, slug: str) -> StoredIdentity:
        path = self._identity_path(slug)
        if not path.exists():
            raise FileNotFoundError(f"identity not found: {slug}")
        _ensure_private_directory(self.base_dir)
        _set_private_file_mode(path)
        return StoredIdentity.from_dict(json.loads(path.read_text()))

    def list_identities(self) -> list[str]:
        if not self.base_dir.exists():
            return []
        slugs = []
        for f in self.base_dir.iterdir():
            name = f.name
            if name.endswith(".session.json") or name.startswith("pending-") or name == "registered-agents.json":
                continue
            if name.endswith(".json"):
                slugs.append(name[: -len(".json")])
        slugs.sort()
        return slugs

    def default_identity(self) -> Optional[str]:
        ids = self.list_identities()
        return ids[0] if ids else None

    def delete_identity(self, slug: str) -> None:
        path = self._identity_path(slug)
        if not path.exists():
            raise FileNotFoundError(f"identity not found: {slug}")
        path.unlink()

    def save_session(self, session: Session) -> None:
        _ensure_private_directory(self.base_dir)
        path = self._session_path(session.slug)
        _atomic_write_private(path, json.dumps({
            "slug": session.slug,
            "subkey_id": session.subkey_id,
            "subkey_secret_key": session.subkey_secret_key,
            "expires_at": session.expires_at,
        }, indent=2))

    def load_session(self, slug: str) -> Session:
        path = self._session_path(slug)
        if not path.exists():
            raise FileNotFoundError(f"session not found: {slug}")
        _ensure_private_directory(self.base_dir)
        _set_private_file_mode(path)
        d = json.loads(path.read_text())
        session = Session(
            slug=d["slug"],
            subkey_id=d["subkey_id"],
            subkey_secret_key=d["subkey_secret_key"],
            expires_at=d["expires_at"],
        )
        if session.expires_at <= _now_ms():
            path.unlink(missing_ok=True)
            raise FileNotFoundError(f"{slug} (session expired)")
        return session

    def delete_session(self, slug: str) -> None:
        self._session_path(slug).unlink(missing_ok=True)
