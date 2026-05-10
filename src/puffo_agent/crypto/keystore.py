from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
        home = os.environ.get("PUFFO_HOME", os.path.expanduser("~/.puffo-agent"))
        return KeyStore(Path(home) / "agents" / agent_id / "keys")

    def _identity_path(self, slug: str) -> Path:
        return self.base_dir / f"{slug}.json"

    def _session_path(self, slug: str) -> Path:
        return self.base_dir / f"{slug}.session.json"

    def save_identity(self, identity: StoredIdentity) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self._identity_path(identity.slug)
        path.write_text(json.dumps(identity.to_dict(), indent=2))

    def load_identity(self, slug: str) -> StoredIdentity:
        path = self._identity_path(slug)
        if not path.exists():
            raise FileNotFoundError(f"identity not found: {slug}")
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
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self._session_path(session.slug)
        path.write_text(json.dumps({
            "slug": session.slug,
            "subkey_id": session.subkey_id,
            "subkey_secret_key": session.subkey_secret_key,
            "expires_at": session.expires_at,
        }, indent=2))

    def load_session(self, slug: str) -> Session:
        path = self._session_path(slug)
        if not path.exists():
            raise FileNotFoundError(f"session not found: {slug}")
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
