from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from .certs import create_subkey_cert, needs_rotation
from .encoding import base64url_encode
from .http_auth import sign_request
from .keystore import KeyStore, Session, decode_secret
from .primitives import Ed25519KeyPair

logger = logging.getLogger(__name__)


class HttpError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


class PuffoCoreHttpClient:
    def __init__(self, server_url: str, keystore: KeyStore, slug: str):
        self.server_url = server_url.rstrip("/")
        self.keystore = keystore
        self.slug = slug
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _load_signing_key(self) -> tuple[Ed25519KeyPair, str]:
        sess = self.keystore.load_session(self.slug)
        key = Ed25519KeyPair.from_secret_bytes(decode_secret(sess.subkey_secret_key))
        return key, sess.subkey_id

    async def _ensure_subkey(self) -> None:
        try:
            sess = self.keystore.load_session(self.slug)
            if not needs_rotation(sess.expires_at):
                return
        except FileNotFoundError:
            pass
        await self._rotate_subkey()

    async def _rotate_subkey(self) -> None:
        identity = self.keystore.load_identity(self.slug)
        device_key = Ed25519KeyPair.from_secret_bytes(
            decode_secret(identity.device_signing_secret_key)
        )
        subkey = Ed25519KeyPair.generate()
        cert = create_subkey_cert(device_key, identity.device_id, subkey.public_key_bytes())

        body = json.dumps({"subkey_cert": cert}).encode()
        auth = sign_request(
            device_key, self.slug, identity.device_id,
            "POST", "/devices/subkeys", body,
        )

        http = await self._get_session()
        async with http.post(
            f"{self.server_url}/devices/subkeys",
            data=body,
            headers=auth.to_dict(),
        ) as resp:
            resp_text = await resp.text()
            if resp.status >= 400:
                raise HttpError(resp.status, resp_text)

        session = Session(
            slug=self.slug,
            subkey_id=cert["subkey_id"],
            subkey_secret_key=base64url_encode(subkey.secret_bytes()),
            expires_at=cert["expires_at"],
        )
        self.keystore.save_session(session)
        logger.info("Rotated subkey → %s", cert["subkey_id"])

    async def _request(
        self, method: str, path: str, body: bytes = b"",
    ) -> tuple[int, Any]:
        await self._ensure_subkey()
        status, data = await self._do_request(method, path, body)
        if status == 401:
            logger.info("Got 401, rotating subkey and retrying")
            await self._rotate_subkey()
            status, data = await self._do_request(method, path, body)
        if status >= 400:
            raise HttpError(status, json.dumps(data) if isinstance(data, dict) else str(data))
        # Fail loud on a 2xx whose body wasn't JSON. ``_do_request``
        # falls back to the raw string when ``json.loads`` fails;
        # every caller of get()/post()/etc. expects a dict/list, so a
        # *non-empty* non-JSON 2xx body — an HTML error page, a
        # proxy/CDN interstitial, a plain-text gateway error — is a
        # broken response. Surfacing it here turns the otherwise
        # cryptic downstream ``'str' object has no attribute 'get'``
        # into a diagnosable HttpError carrying the actual body.
        # Empty bodies (204 No Content etc.) stay untouched.
        if isinstance(data, str) and data.strip():
            raise HttpError(
                status,
                f"non-JSON body on {status} response: {data[:500]}",
            )
        return status, data

    async def _do_request(
        self, method: str, path: str, body: bytes = b"",
    ) -> tuple[int, Any]:
        signing_key, signer_id = self._load_signing_key()
        auth = sign_request(signing_key, self.slug, signer_id, method, path, body)
        headers = auth.to_dict()
        url = f"{self.server_url}{path}"

        http = await self._get_session()
        async with http.request(method, url, data=body or None, headers=headers) as resp:
            text = await resp.text()
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                data = text
            return resp.status, data

    async def get(self, path: str) -> Any:
        _, data = await self._request("GET", path)
        return data

    async def get_bytes(self, path: str) -> bytes:
        """Signed GET returning raw bytes (e.g. /blobs/{id})."""
        await self._ensure_subkey()
        bytes_out = await self._do_request_bytes("GET", path)
        return bytes_out

    async def _do_request_bytes(self, method: str, path: str) -> bytes:
        signing_key, signer_id = self._load_signing_key()
        from .http_auth import sign_request
        auth = sign_request(signing_key, self.slug, signer_id, method, path, b"")
        headers = auth.to_dict()
        url = f"{self.server_url}{path}"
        http = await self._get_session()
        async with http.request(method, url, headers=headers) as resp:
            if resp.status == 401:
                # Caller retries after a rotation.
                raise HttpError(401, await resp.text())
            if resp.status >= 400:
                raise HttpError(resp.status, await resp.text())
            return await resp.read()

    async def post(self, path: str, body: dict | None = None) -> Any:
        raw = json.dumps(body).encode() if body else b""
        _, data = await self._request("POST", path, raw)
        return data

    async def post_bytes(self, path: str, body: bytes) -> Any:
        """POST raw bytes (e.g. blob ciphertext to /blobs/upload).
        Signature is over the raw body, not a JSON encoding."""
        _, data = await self._request("POST", path, body)
        return data

    async def put(self, path: str, body: dict | None = None) -> Any:
        raw = json.dumps(body).encode() if body else b""
        _, data = await self._request("PUT", path, raw)
        return data

    async def patch(self, path: str, body: dict | None = None) -> Any:
        raw = json.dumps(body).encode() if body else b""
        _, data = await self._request("PATCH", path, raw)
        return data

    async def delete(self, path: str) -> Any:
        _, data = await self._request("DELETE", path)
        return data

    async def post_unsigned(self, path: str, body: dict | None = None) -> Any:
        raw = json.dumps(body).encode() if body else b""
        http = await self._get_session()
        async with http.post(
            f"{self.server_url}{path}",
            data=raw,
            headers={"content-type": "application/json"},
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise HttpError(resp.status, text)
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return text

    async def get_unsigned(self, path: str) -> Any:
        http = await self._get_session()
        async with http.get(f"{self.server_url}{path}") as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise HttpError(resp.status, text)
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return text
