"""Machine enrollment: `puffo-agent link` / `unlink` (v0.4)."""

from __future__ import annotations

import asyncio
import json

import aiohttp

from ..api.ownership import is_owner
from ..state import AgentConfig, discover_agents
from . import machine_auth
from .envelope import ControlError, verify_control_cert
from .store import (
    ControlPairing,
    delete_pairing,
    get_pairing,
    load_or_create_machine,
    now_ms,
    save_pairing,
)

DEFAULT_SERVER_URL = "https://chat.puffo.ai/relay"
POLL_INTERVAL_SECONDS = 2.0
LINK_TIMEOUT_SECONDS = 300


async def run_link(server_url: str, hostname: str) -> int:
    """Register this machine, mint a link code, and wait for an operator to
    approve it. The machine's private key never leaves disk."""
    machine = load_or_create_machine()
    base = server_url.rstrip("/")

    async with aiohttp.ClientSession() as session:
        # 1. self-register (idempotent).
        cert = machine_auth.machine_cert(machine, hostname)
        async with session.post(f"{base}/v2/machines", json={"machine_cert": cert}) as resp:
            if resp.status != 200:
                print(f"link: registration rejected ({resp.status}): {await resp.text()}")
                return 1

        # 2. create a link code (machine-authed).
        headers = machine_auth.signed_headers(machine, "POST", "/v2/machines/links")
        async with session.post(f"{base}/v2/machines/links", headers=headers) as resp:
            if resp.status != 200:
                print(f"link: could not create code ({resp.status}): {await resp.text()}")
                return 1
            code = (await resp.json())["code"]

        print(f"\n  Link code:  {code}\n")
        print("  Open the puffo web app, go to Devices → Link a machine,")
        print(f"  and enter this code to approve '{hostname}'.\n")
        print("  Waiting for approval (Ctrl-C to cancel)...")

        waited = 0.0
        while waited < LINK_TIMEOUT_SECONDS:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            waited += POLL_INTERVAL_SECONDS
            async with session.get(f"{base}/v2/machines/links/{code}") as resp:
                if resp.status != 200:
                    continue
                poll = await resp.json()
            status = poll.get("status")
            if status == "expired":
                print("link: code expired before approval. Run `puffo-agent link` again.")
                return 1
            if status != "approved":
                continue

            cert = poll.get("operator_control_cert")
            operator_slug = poll.get("operator_slug")
            if not (isinstance(cert, dict) and operator_slug):
                print("link: approval response incomplete; aborting.")
                return 1
            try:
                operator_root = verify_control_cert(
                    cert, machine.machine_id, machine.control_pubkey
                )
            except ControlError as exc:
                print(f"link: REJECTED — control cert failed verification: {exc}")
                return 1

            save_pairing(
                ControlPairing(
                    operator_slug=operator_slug,
                    operator_root_pubkey=operator_root,
                    control_cert=cert,
                    server_url=base,
                    name=hostname,
                    created_at=now_ms(),
                )
            )
            print(f"\n  Linked to operator {operator_slug}.")
            print("  The daemon will serve this operator's commands over the control WS.\n")
            return 0

        print("link: timed out waiting for approval.")
        return 1


async def run_unlink(operator_slug: str) -> int:
    """Revoke an operator pairing server-side + locally, and pause that
    operator's agents on this machine."""
    pairing = get_pairing(operator_slug)
    if pairing is None:
        print(f"unlink: no pairing for operator {operator_slug!r}")
        return 2
    machine = load_or_create_machine()
    base = pairing.server_url.rstrip("/")

    body = json.dumps(
        {"machine_id": machine.machine_id, "operator_slug": operator_slug}
    ).encode()
    headers = machine_auth.signed_headers(machine, "POST", "/v2/machines/links/unlink", body)
    headers["content-type"] = "application/json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/v2/machines/links/unlink", data=body, headers=headers
            ) as resp:
                if resp.status not in (200, 404):
                    print(f"unlink: server returned {resp.status} (continuing locally)")
    except Exception as exc:  # noqa: BLE001 — local teardown proceeds regardless
        print(f"unlink: server unreachable ({exc}); removing local pairing anyway")

    paused = 0
    for agent_id in discover_agents():
        if is_owner(agent_id, pairing.operator_root_pubkey):
            try:
                cfg = AgentConfig.load(agent_id)
            except Exception:  # noqa: BLE001
                continue
            if cfg.state != "paused":
                cfg.state = "paused"
                cfg.save()
                paused += 1

    delete_pairing(operator_slug)
    print(f"unlink: removed pairing {operator_slug}; paused {paused} agent(s).")
    return 0
