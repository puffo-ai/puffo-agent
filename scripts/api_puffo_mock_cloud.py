"""Mock puffo-cloud-server for end-to-end smoke testing the
api-puffo runtime against the real bridge wire protocol
(BRIDGE-WIRE-PROTOCOL.md).

Run:
    PYTHONPATH=src python scripts/api_puffo_mock_cloud.py --port 9999

  * Listens for a WS connect at ``/v2/cloud-agents/subscribe`` and
    accepts any ``x-sandbox-token`` header.
  * Sends ``connected`` immediately on upgrade.
  * Responds to ``heartbeat`` (silently — no client-facing ack),
    ``fetch_pending`` (returns 0 unless --inject is used),
    ``ack`` (returns ack_result), ``list_spaces`` (returns a fixed
    fixture), ``send`` (returns ack).
  * ``POST /_admin/inject {"text": "..."}`` pushes a fake DM as a
    ``message`` frame to the currently connected agent.
  * Mocks LLM at ``POST /v1/llm/complete`` — returns a canned
    response that either calls ``send_message`` once with an echo
    or end-turns with plain text.

Dev tool, not production: no auth on the inject endpoint, no
persistence, no rate limits."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path

from aiohttp import WSMsgType, web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] mock-cloud: %(message)s",
)
log = logging.getLogger("mock-cloud")


class MockCloudState:
    def __init__(self) -> None:
        self.active_ws: web.WebSocketResponse | None = None
        self.recv_send_frames: list[dict] = []
        self.recv_ack_frames: list[dict] = []
        self.pending_messages: list[dict] = []
        self.spaces_fixture: list[dict] = [
            {
                "space_id": "sp_mock_001",
                "name": "Engineering",
                "channels": [
                    {"channel_id": "ch_mock_001", "name": "general"},
                    {"channel_id": "ch_mock_002", "name": "random"},
                ],
            },
        ]


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    state: MockCloudState = request.app["state"]
    token = request.headers.get("x-sandbox-token", "")
    log.info("WS connect (x-sandbox-token=%s…)", (token or "(missing)")[:24])

    ws = web.WebSocketResponse(heartbeat=None)
    await ws.prepare(request)
    state.active_ws = ws

    # First frame after upgrade.
    await ws.send_json({"type": "connected"})
    log.info("sent: connected")

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                    log.info("WS closing (%s)", msg.type)
                    break
                continue
            try:
                frame = json.loads(msg.data)
            except json.JSONDecodeError as exc:
                log.warning("dropped non-JSON frame: %s", exc)
                continue
            await _handle_client_frame(ws, state, frame)
    finally:
        state.active_ws = None
        log.info("WS closed")
    return ws


async def _handle_client_frame(
    ws: web.WebSocketResponse, state: MockCloudState, frame: dict,
) -> None:
    kind = frame.get("type", "")
    if kind == "heartbeat":
        # Silent — server's recv-timeout just gets re-armed.
        return
    if kind == "send":
        state.recv_send_frames.append(frame)
        log.info("recv: send (%s)", json.dumps(frame)[:160])
        await ws.send_json({
            "type": "ack",
            "client_ref": frame.get("client_ref"),
            "envelope_id": f"msg_mock_{uuid.uuid4().hex[:12]}",
            "devices_queued": 2,
        })
        return
    if kind == "fetch_pending":
        log.info("recv: fetch_pending limit=%s", frame.get("limit"))
        count = 0
        # Drain whatever was queued via --admin-inject.
        while state.pending_messages:
            await ws.send_json(state.pending_messages.pop(0))
            count += 1
        await ws.send_json({
            "type": "pending_delivered", "count": count, "more": False,
        })
        log.info("sent: pending_delivered count=%d more=False", count)
        return
    if kind == "ack":
        state.recv_ack_frames.append(frame)
        ids = frame.get("envelope_ids") or []
        log.info("recv: ack count=%d", len(ids))
        await ws.send_json({"type": "ack_result", "acked": len(ids)})
        return
    if kind == "list_spaces":
        log.info("recv: list_spaces")
        await ws.send_json({"type": "spaces", "spaces": state.spaces_fixture})
        return
    log.warning("unhandled client frame type=%r", kind)
    await ws.send_json({
        "type": "error",
        "code": "BAD_FRAME",
        "message": f"unknown frame type {kind!r}",
    })


async def llm_complete(request: web.Request) -> web.Response:
    body = await request.json()
    log.info(
        "LLM /v1/llm/complete: provider=%s model=%s messages=%d",
        body.get("provider"), body.get("model"), len(body.get("messages") or []),
    )
    msgs = body.get("messages") or []
    last = msgs[-1] if msgs else {}
    last_content = last.get("content", "")

    # If the last message is a tool_result, we're in round 2+: emit a
    # final text reply.
    if isinstance(last_content, list) and any(
        isinstance(c, dict) and c.get("type") == "tool_result"
        for c in last_content
    ):
        return web.json_response({
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Done."}],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        })

    last_text = (
        last_content if isinstance(last_content, str)
        else " ".join(
            c.get("content", "") for c in last_content if isinstance(c, dict)
        )
    )
    # First round: ask to call send_message back to the user.
    return web.json_response({
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "Replying via tool..."},
            {
                "type": "tool_use",
                "id": "tu_001",
                "name": "send_message",
                "input": {
                    "plaintext": f"echo: {last_text[:200]}",
                    "recipient_slug": "smoker",
                },
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    })


async def admin_inject(request: web.Request) -> web.Response:
    """POST /_admin/inject {"text": "..."} — enqueues an inbound
    message frame. Pushes immediately if the agent is connected;
    otherwise it surfaces on the next ``fetch_pending``."""
    state: MockCloudState = request.app["state"]
    body = await request.json()
    text = body.get("text", "hello from mock cloud")
    frame = {
        "type": "message",
        "envelope_id": f"msg_inject_{uuid.uuid4().hex[:12]}",
        "sender_slug": body.get("sender_slug", "smoker"),
        "recipient_slug": body.get("recipient_slug", "smoke-bot"),
        "sent_at": 0,
        "plaintext": text,
    }
    if state.active_ws is not None and not state.active_ws.closed:
        await state.active_ws.send_str(json.dumps(frame))
        log.info("INJECTED live message → connected agent (text=%r)", text[:80])
        return web.json_response({"ok": True, "mode": "live"})
    state.pending_messages.append(frame)
    log.info("QUEUED message (no live WS); will deliver on next fetch_pending")
    return web.json_response({"ok": True, "mode": "queued"})


def build_app(state: MockCloudState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/v2/cloud-agents/subscribe", ws_handler)
    app.router.add_post("/v1/llm/complete", llm_complete)
    app.router.add_post("/_admin/inject", admin_inject)
    return app


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9999)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    state = MockCloudState()
    app = build_app(state)
    log.info("starting mock cloud at 127.0.0.1:%d", args.port)
    web.run_app(app, host="127.0.0.1", port=args.port, print=lambda *a: None)


if __name__ == "__main__":
    main()
