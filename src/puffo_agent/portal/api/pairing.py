"""Single-pairing persistence for the local bridge API.

A daemon accepts requests from one ``(slug, device_id)`` at a time.
The first successful ``POST /v1/pair`` writes ``pairing.json`` in
the puffo-agent home dir; subsequent pair attempts replace it.

The stored shape caches the device signing pubkey so per-request
signature verification doesn't re-run cert chain validation on the
hot path.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

from ..state import pairing_path


@dataclass
class Pairing:
    slug: str
    device_id: str
    # base64url, 32 bytes — root pubkey lifted from identity_cert so
    # ownership comparisons skip re-parsing the cert.
    root_public_key: str
    # base64url, 32 bytes — device signing pubkey from device_cert,
    # used to verify x-puffo-signature on every request.
    device_signing_public_key: str
    # Raw certs retained for `pairing show` auditing and any future
    # re-verify path that doesn't need a fresh pair.
    identity_cert: dict
    device_cert: dict
    paired_at: int  # ms since epoch


def load_pairing() -> Optional[Pairing]:
    path = pairing_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return Pairing(
            slug=raw["slug"],
            device_id=raw["device_id"],
            root_public_key=raw["root_public_key"],
            device_signing_public_key=raw["device_signing_public_key"],
            identity_cert=raw["identity_cert"],
            device_cert=raw["device_cert"],
            paired_at=int(raw["paired_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def save_pairing(p: Pairing) -> None:
    path = pairing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(p), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def clear_pairing() -> None:
    path = pairing_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def now_ms() -> int:
    return int(time.time() * 1000)
