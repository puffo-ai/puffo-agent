"""Mock puffo-cloud-server for end-to-end smoke testing the
api-puffo runtime.

Run:
    PYTHONPATH=src python scripts/api_puffo_mock_cloud.py \\
        --port 9999 \\
        --agent-slug smoke-bot-test01 \\
        --recipient-device-id dev_smoke_cloud \\
        --recipient-kem-secret-key AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA

It:
  * Listens for a WS connect from the agent and accepts any bearer.
  * On '--inject-message <text>', encrypts a fake DM as a foreign
    sender and pushes it over the WS as an ``envelope`` frame.
  * Mocks the LLM by returning a fixed canned response that either
    speaks plain text or calls ``send_message`` once with the text.
  * Logs every tool POST to stdout so you can see the round-trip.

This is a *dev tool*, not production code — no auth, no rate
limits, no persistence. Lives under scripts/ so the package import
graph stays clean.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from aiohttp import WSMsgType, web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffo_agent.crypto.encoding import base64url_decode, base64url_encode
from puffo_agent.crypto.message import (
    EncryptInput,
    RecipientDevice,
    encrypt_message,
)
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] mock-cloud: %(message)s",
)
log = logging.getLogger("mock-cloud")


class MockCloudState:
    def __init__(self, agent_slug: str, recipient_device_id: str,
                 recipient_kem_pubkey: bytes) -> None:
        self.agent_slug = agent_slug
        self.recipient_device_id = recipient_device_id
        self.recipient_kem_pubkey = recipient_kem_pubkey
        # Foreign sender identity — generated once per process; we'll
        # use this to sign the canned inbound payload so the agent's
        # decrypt-and-verify path is exercised end-to-end.
        self.foreign_signing_key = Ed25519KeyPair.generate()
        self.foreign_slug = "smoker"
        self.foreign_subkey_id = "subkey_smoke_001"
        self.active_ws: web.WebSocketResponse | None = None
        self.recv_tool_calls: list[tuple[str, dict]] = []

    def sender_pubkey_b64(self) -> str:
        return base64url_encode(self.foreign_signing_key.public_key_bytes())

    def build_envelope_frame(self, text: str) -> dict:
        device = RecipientDevice(
            device_id=self.recipient_device_id,
            kem_public_key=self.recipient_kem_pubkey,
        )
        inp = EncryptInput(
            envelope_kind="dm",
            sender_slug=self.foreign_slug,
            sender_subkey_id=self.foreign_subkey_id,
            is_visible_to_human=True,
            recipient_slug=self.agent_slug,
            content_type="text/plain",
            content=text,
            recipients=[device],
        )
        envelope = encrypt_message(inp, self.foreign_signing_key)
        return {
            "type": "envelope",
            "envelope": envelope,
            "sender_signing_public_key": self.sender_pubkey_b64(),
        }


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    state: MockCloudState = request.app["state"]
    slug = request.match_info["slug"]
    token = request.headers.get("Authorization", "")
    log.info("WS connect: slug=%s auth=%s", slug, token[:24])

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    state.active_ws = ws
    log.info("WS ready; awaiting --inject calls")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                log.info("WS client→server: %s", msg.data[:120])
            elif msg.type == WSMsgType.ERROR:
                log.warning("WS error: %s", ws.exception())
                break
    finally:
        state.active_ws = None
        log.info("WS closed")
    return ws


async def llm_complete(request: web.Request) -> web.Response:
    body = await request.json()
    log.info(
        "LLM /v1/llm/complete: provider=%s model=%s messages=%d",
        body.get("provider"), body.get("model"), len(body.get("messages") or []),
    )
    msgs = body.get("messages") or []
    last = msgs[-1] if msgs else {}
    last_content = last.get("content", "")
    last_text = (
        last_content if isinstance(last_content, str)
        else " ".join(c.get("content", "") for c in last_content if isinstance(c, dict))
    )

    # If the last message is a tool_result we're in round 2+: emit a
    # final text reply.
    if isinstance(last_content, list) and any(
        isinstance(c, dict) and c.get("type") == "tool_result" for c in last_content
    ):
        return web.json_response({
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Done!"}],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        })

    # First round: call send_message tool with an echo reply.
    return web.json_response({
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "Replying via tool..."},
            {
                "type": "tool_use",
                "id": "tu_001",
                "name": "send_message",
                "input": {
                    "channel": "@smoker",
                    "text": f"echo: {last_text[:200]}",
                    "is_visible_to_human": True,
                },
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    })


async def tool_send_message(request: web.Request) -> web.Response:
    body = await request.json()
    state: MockCloudState = request.app["state"]
    state.recv_tool_calls.append(("send_message", body))
    log.info("TOOL send_message: %s", json.dumps(body)[:200])
    return web.json_response({"ok": True, "envelope_id": "msg_mock_xxxx"})


async def tool_get_channel_history(request: web.Request) -> web.Response:
    body = await request.json()
    log.info("TOOL get_channel_history: %s", body)
    return web.json_response({"text": "(no history in mock)"})


async def tool_get_thread_history(request: web.Request) -> web.Response:
    body = await request.json()
    log.info("TOOL get_thread_history: %s", body)
    return web.json_response({"text": "(no thread history in mock)"})


async def tool_whoami(request: web.Request) -> web.Response:
    state: MockCloudState = request.app["state"]
    return web.json_response({
        "text": f"slug: {state.agent_slug}\ndisplay_name: Smoke Bot",
    })


async def admin_inject(request: web.Request) -> web.Response:
    """POST /_admin/inject {"text": "..."} — pushes an encrypted DM
    to the connected agent as an inbound WS frame."""
    state: MockCloudState = request.app["state"]
    if state.active_ws is None or state.active_ws.closed:
        return web.json_response(
            {"ok": False, "error": "no agent WS connected"}, status=409,
        )
    body = await request.json()
    text = body.get("text", "hello from mock cloud")
    frame = state.build_envelope_frame(text)
    await state.active_ws.send_str(json.dumps(frame))
    log.info("INJECTED envelope to agent (text=%r)", text[:80])
    return web.json_response({"ok": True})


def build_app(state: MockCloudState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/v1/ws/{slug}", ws_handler)
    app.router.add_post("/v1/llm/complete", llm_complete)
    app.router.add_post("/v1/send_message", tool_send_message)
    app.router.add_post("/v1/get_channel_history", tool_get_channel_history)
    app.router.add_post("/v1/get_thread_history", tool_get_thread_history)
    app.router.add_post("/v1/whoami", tool_whoami)
    app.router.add_post("/_admin/inject", admin_inject)
    return app


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--agent-slug", required=True)
    p.add_argument("--recipient-device-id", required=True)
    p.add_argument("--recipient-kem-secret-key", required=True,
                   help="base64url-encoded 32 bytes — same as bundle's kem_secret_key")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    # We derive the recipient's KEM PUBLIC key from the SECRET so the
    # mock doesn't need a separate pubkey input — the agent's
    # decrypt with the matching secret will succeed.
    secret_bytes = base64url_decode(args.recipient_kem_secret_key)
    kp = KemKeyPair.from_secret_bytes(secret_bytes)
    state = MockCloudState(
        agent_slug=args.agent_slug,
        recipient_device_id=args.recipient_device_id,
        recipient_kem_pubkey=kp.public_key_bytes(),
    )
    app = build_app(state)
    log.info(
        "starting mock cloud at 0.0.0.0:%d (agent=%s)",
        args.port, args.agent_slug,
    )
    web.run_app(app, host="127.0.0.1", port=args.port, print=lambda *a: None)


if __name__ == "__main__":
    main()
