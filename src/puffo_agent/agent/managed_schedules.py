"""Encrypted schedule management. Delivery uses the existing message transport."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from ..crypto.http_client import PuffoCoreHttpClient
from ..crypto.keystore import decode_secret
from ..crypto.message import EncryptInput, decrypt_message, encrypt_message
from ..crypto.primitives import Ed25519KeyPair, KemKeyPair
from .client_support import DeviceKeyCache
from .outbound_messages import fetch_device_keys
from .schedule_wire import CONTENT_TYPE, validate_plan, validate_schedule_text


class ScheduleUnavailable(ValueError):
    """This row cannot be verified with the current device's keys."""


class ScheduleAPI:
    def __init__(self, http: PuffoCoreHttpClient, slug: str):
        self.http = http
        self.slug = slug
        prefix = "/v2/cloud-agents" if http.keyless else "/v2"
        self.path = f"{prefix}/agents/{quote(slug, safe='')}/schedules"

    async def list(self) -> dict[str, Any]:
        data = await self._get(self.path)
        rows = []
        for row in data["schedules"]:
            try:
                rows.append(await self._open(row))
            except ScheduleUnavailable:
                # Metadata remains usable for deletion/replacement after key
                # rotation. Network/key-store failures are not row corruption.
                rows.append({**row, "opened": False})
        return {"schedules": rows}

    async def read(self, schedule_id: str) -> dict[str, Any]:
        return await self._open(await self._get(f"{self.path}/{UUID(schedule_id)}"))

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        sealed = await self._seal(str(uuid4()), body)
        post = self.http.post_unsigned if self.http.keyless else self.http.post
        return await self._open(await post(self.path, sealed))

    async def update(self, schedule_id: str, version: int, body: dict[str, Any]) -> dict[str, Any]:
        sealed = await self._seal(str(UUID(schedule_id)), body)
        put = self.http.put_unsigned if self.http.keyless else self.http.put
        return await self._open(await put(f"{self.path}/{UUID(schedule_id)}?version={int(version)}", sealed))

    async def delete(self, schedule_id: str, version: int) -> dict[str, bool]:
        delete = self.http.delete_unsigned if self.http.keyless else self.http.delete
        await delete(f"{self.path}/{UUID(schedule_id)}?version={int(version)}")
        return {"deleted": True}

    async def _get(self, path: str) -> Any:
        get = self.http.get_unsigned if self.http.keyless else self.http.get
        return await get(path)

    async def _seal(self, schedule_id: str, body: dict[str, Any]) -> dict[str, Any]:
        validate_schedule_text(body)
        draft = {**body, "id": schedule_id, "revision": str(uuid4()), "first_run_at": body["next_run_at"]}
        del draft["next_run_at"]
        if self.http.keyless:
            return draft  # The existing trusted KMS bridge seals before persistence.
        profiles = await self.http.get(f"/identities/profiles?slugs={quote(self.slug, safe='')}")
        owner = next(p["owner_slug"] for p in profiles["profiles"] if p["slug"] == self.slug)
        if not owner:
            raise ValueError("schedule agent has no owner")
        first = datetime.fromisoformat(draft["first_run_at"].replace("Z", "+00:00"))
        if first.tzinfo is None:
            raise ValueError("schedule timestamp requires a timezone")
        plan = {
            "version": 1, "schedule_id": schedule_id, "revision": draft["revision"],
            "agent_id": self.slug, "owner_slug": owner, "first_run_at": int(first.timestamp() * 1000),
            "interval_seconds": draft["interval_seconds"], "enabled": draft["enabled"],
            "name": draft["name"], "prompt": draft["prompt"],
        }
        await self.http._ensure_subkey()
        session = self.http.keystore.load_session(self.http.slug)
        devices = await fetch_device_keys(http=self.http, slugs=[self.slug, owner])
        active = await self.http.get(f"/certs/active?slugs={self.slug},{owner}")
        active_ids = {d["device_id"] for d in active["devices"]}
        envelope = encrypt_message(EncryptInput(
            envelope_kind="dm", sender_slug=self.http.slug, sender_subkey_id=session.subkey_id,
            recipient_slug=self.slug, content_type=CONTENT_TYPE, content=plan,
            recipients=[d for d in devices if d.device_id in active_ids], is_visible_to_human=False,
        ), Ed25519KeyPair.from_secret_bytes(decode_secret(session.subkey_secret_key)))
        return {"id": schedule_id, "revision": draft["revision"], "envelope": envelope,
                "signer_subkey_id": session.subkey_id, "first_run_at": draft["first_run_at"],
                "interval_seconds": draft["interval_seconds"], "enabled": draft["enabled"]}

    async def _open(self, row: dict[str, Any]) -> dict[str, Any]:
        if self.http.keyless:
            return row
        if not isinstance(row["envelope"], dict) or not isinstance(row["envelope"].get("sender_slug"), str):
            raise ScheduleUnavailable("invalid stored schedule envelope")
        identity = self.http.keystore.load_identity(self.http.slug)
        kem = KemKeyPair.from_secret_bytes(decode_secret(identity.kem_secret_key))
        keys = await DeviceKeyCache(self.http).get_signing_keys(row["envelope"]["sender_slug"])
        for key in keys:
            try:
                payload = decrypt_message(row["envelope"], identity.device_id, kem, key)
            except Exception:
                continue
            try:
                validate_plan(payload, {
                    "schedule_id": row["id"], "revision": row["revision"], "owner_slug": row["owner_slug"],
                    "first_run_at": int(datetime.fromisoformat(row["first_run_at"].replace("Z", "+00:00")).timestamp() * 1000),
                    "interval_seconds": row["interval_seconds"],
                }, agent=self.slug, sender=row["envelope"]["sender_slug"])
            except ValueError as error:
                raise ScheduleUnavailable(str(error)) from error
            return {**row, "opened": True, "name": payload.content["name"], "prompt": payload.content["prompt"]}
        raise ScheduleUnavailable("unable to decrypt schedule with this device")
