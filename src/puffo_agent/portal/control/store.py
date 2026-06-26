"""Local persistence for the machine control identity + operator pairings."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ...crypto.encoding import base64url_decode, base64url_encode
from ...crypto.primitives import Ed25519KeyPair, KemKeyPair, sha256
from ..state import home_dir


def derive_machine_id(signing_pubkey: bytes) -> str:
    """``mac_<base64url(sha256(signing_pubkey))>`` — a machine's stable id.
    Deliberately its own namespace, NOT the PKI ``dev_`` device-id."""
    return f"mac_{base64url_encode(sha256(signing_pubkey))}"


def control_dir() -> Path:
    d = home_dir() / "control"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _machine_path() -> Path:
    return control_dir() / "machine.json"


def _pairings_path() -> Path:
    return control_dir() / "pairings.json"


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class MachineControlIdentity:
    """The machine's own control keypair. One per machine, reused across all
    operator pairings (so a single ``machine_id`` identifies the machine)."""

    machine_id: str
    signing_secret: str  # b64url Ed25519 seed
    kem_secret: str  # b64url X25519 secret

    def signing_keypair(self) -> Ed25519KeyPair:
        return Ed25519KeyPair.from_secret_bytes(base64url_decode(self.signing_secret))

    def kem_keypair(self) -> KemKeyPair:
        return KemKeyPair.from_secret_bytes(base64url_decode(self.kem_secret))

    @property
    def control_pubkey(self) -> str:
        return base64url_encode(self.signing_keypair().public_key_bytes())

    @property
    def kem_pubkey(self) -> str:
        return base64url_encode(self.kem_keypair().public_key_bytes())


def load_or_create_machine() -> MachineControlIdentity:
    """Load the machine control identity, generating + persisting it on first
    use. The private keys never leave this file."""
    path = _machine_path()
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        return MachineControlIdentity(
            machine_id=raw["machine_id"],
            signing_secret=raw["signing_secret"],
            kem_secret=raw["kem_secret"],
        )
    signing = Ed25519KeyPair.generate()
    kem = KemKeyPair.generate()
    machine_id = derive_machine_id(signing.public_key_bytes())
    identity = MachineControlIdentity(
        machine_id=machine_id,
        signing_secret=base64url_encode(signing.secret_bytes()),
        kem_secret=base64url_encode(kem.secret_bytes()),
    )
    _atomic_write(
        path,
        json.dumps(
            {
                "machine_id": identity.machine_id,
                "signing_secret": identity.signing_secret,
                "kem_secret": identity.kem_secret,
            },
            indent=2,
        ),
    )
    return identity


@dataclass
class ControlPairing:
    """One operator's link to this machine: the pinned operator root + the
    operator-signed control cert."""

    operator_slug: str
    operator_root_pubkey: str  # pinned at link time
    control_cert: dict
    server_url: str
    name: str
    created_at: int

    def to_dict(self) -> dict:
        return {
            "operator_slug": self.operator_slug,
            "operator_root_pubkey": self.operator_root_pubkey,
            "control_cert": self.control_cert,
            "server_url": self.server_url,
            "name": self.name,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict) -> ControlPairing:
        return ControlPairing(
            operator_slug=d["operator_slug"],
            operator_root_pubkey=d["operator_root_pubkey"],
            control_cert=d["control_cert"],
            server_url=d["server_url"],
            name=d["name"],
            created_at=int(d.get("created_at", 0)),
        )


def load_pairings() -> dict[str, ControlPairing]:
    path = _pairings_path()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {slug: ControlPairing.from_dict(p) for slug, p in raw.items()}


def save_pairing(pairing: ControlPairing) -> None:
    pairings = load_pairings()
    pairings[pairing.operator_slug] = pairing
    _atomic_write(
        _pairings_path(),
        json.dumps({s: p.to_dict() for s, p in pairings.items()}, indent=2),
    )


def get_pairing(operator_slug: str) -> ControlPairing | None:
    return load_pairings().get(operator_slug)


def delete_pairing(operator_slug: str) -> bool:
    pairings = load_pairings()
    if operator_slug not in pairings:
        return False
    del pairings[operator_slug]
    _atomic_write(
        _pairings_path(),
        json.dumps({s: p.to_dict() for s, p in pairings.items()}, indent=2),
    )
    return True


def current_machine_id() -> str | None:
    """The machine_id if this host has been linked, else None (so an unlinked
    local-only agent reports no machine)."""
    path = _machine_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("machine_id")
    except Exception:  # noqa: BLE001
        return None


def now_ms() -> int:
    return int(time.time() * 1000)
