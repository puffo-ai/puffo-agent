"""Machine enrollment: `puffo-agent machine link` / `machine unlink` (v0.4)."""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from ..api.ownership import is_owner
from ..state import AgentConfig, discover_agents
from . import machine_auth
from .envelope import ControlError, verify_control_cert
from .store import (
    ControlPairing,
    current_machine_id,
    delete_pairing,
    get_pairing,
    load_or_create_machine,
    now_ms,
    save_pairing,
)

logger = logging.getLogger(__name__)

DEFAULT_SERVER_URL = "https://chat.puffo.ai/relay"
POLL_INTERVAL_SECONDS = 2.0
LINK_TIMEOUT_SECONDS = 300


def _web_url_from_server(server_url: str) -> str:
    """Derive the web-app base from the server URL: the public edge serves the
    relay at ``<web>/relay``, so drop that suffix. Self-hosted/dev setups whose
    web app lives elsewhere can ignore the printed link and enter the code."""
    base = server_url.rstrip("/")
    if base.endswith("/relay"):
        base = base[: -len("/relay")]
    return base


class LinkError(Exception):
    """Machine registration or code-minting failed (a non-approval error)."""


async def mint_link_code(server_url: str, hostname: str) -> tuple[str, str]:
    """Register this machine (idempotent) and mint a link code. Returns
    ``(code, base)``; the machine private key never leaves disk."""
    machine = load_or_create_machine()
    base = server_url.rstrip("/")
    async with aiohttp.ClientSession() as session:
        cert = machine_auth.machine_cert(machine, hostname)
        async with session.post(f"{base}/v2/machines", json={"machine_cert": cert}) as resp:
            if resp.status != 200:
                raise LinkError(f"registration rejected ({resp.status}): {await resp.text()}")
        headers = machine_auth.signed_headers(machine, "POST", "/v2/machines/links")
        async with session.post(f"{base}/v2/machines/links", headers=headers) as resp:
            if resp.status != 200:
                raise LinkError(f"could not create code ({resp.status}): {await resp.text()}")
            code = (await resp.json())["code"]
    return code, base


async def await_link_approval(
    base: str, code: str, hostname: str, timeout: float = LINK_TIMEOUT_SECONDS
) -> tuple[str, str | None]:
    """Poll until the code is approved/expired/times out. On approval, verify the
    operator's control cert and save the pairing. Returns ``(status, operator_slug)``
    where status is ``approved`` / ``expired`` / ``timeout``."""
    machine = load_or_create_machine()
    waited = 0.0
    async with aiohttp.ClientSession() as session:
        while waited < timeout:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            waited += POLL_INTERVAL_SECONDS
            async with session.get(f"{base}/v2/machines/links/{code}") as resp:
                if resp.status != 200:
                    continue
                poll = await resp.json()
            status = poll.get("status")
            if status == "expired":
                return "expired", None
            if status != "approved":
                continue
            cert = poll.get("operator_control_cert")
            operator_slug = poll.get("operator_slug")
            if not (isinstance(cert, dict) and operator_slug):
                raise LinkError("approval response incomplete")
            operator_root = verify_control_cert(cert, machine.machine_id, machine.control_pubkey)
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
            await migrate_owned_agents(operator_root)
            return "approved", operator_slug
    return "timeout", None


async def migrate_owned_agents(operator_root_pubkey: str) -> int:
    """Stamp this machine's ``machine_id`` onto every agent owned by the
    operator so puffo-server flips them from local to remote — a paused/running
    local agent becomes remotely manageable without re-creating it. Idempotent
    and best-effort per agent. Returns the number reported."""
    machine_id = current_machine_id()
    if not machine_id:
        return 0
    from ...crypto.http_client import HttpError, PuffoCoreHttpClient
    from ...crypto.keystore import KeyStore

    reported = 0
    for agent_id in discover_agents():
        if not is_owner(agent_id, operator_root_pubkey):
            continue
        try:
            cfg = AgentConfig.load(agent_id)
        except Exception:  # noqa: BLE001
            continue
        pc = cfg.puffo_core
        if not pc.is_configured():
            continue
        # Running workers self-report status; assert "idle" only to carry the
        # machine_id — the worker's next heartbeat refines the real state.
        status = "paused" if cfg.state == "paused" else "idle"
        http = PuffoCoreHttpClient(pc.server_url, KeyStore.for_agent(cfg.id), pc.slug)
        try:
            await http.post(
                "/agents/me/heartbeat", {"status": status, "machine_id": machine_id}
            )
            reported += 1
        except HttpError as exc:
            logger.warning(
                "migrate %s: machine_id stamp rejected (HTTP %s)", cfg.id, exc.status
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; other agents proceed
            logger.warning("migrate %s: machine_id stamp failed: %s", cfg.id, exc)
        finally:
            await http.close()
    return reported


async def run_link(server_url: str, hostname: str) -> int:
    """Register this machine, mint a link code, and wait for an operator to
    approve it (CLI entry point)."""
    try:
        code, base = await mint_link_code(server_url, hostname)
    except LinkError as exc:
        print(f"link: {exc}")
        return 1

    web = _web_url_from_server(server_url)
    print(f"\n  Link code:  {code}\n")
    print(f"  Open:  {web}/chat/agents?linkCode={code}")
    print("  (the link opens the puffo web app with the code pre-filled —")
    print(f"   or go to My Agents → Link machine and enter it to approve '{hostname}'.)\n")
    print("  Waiting for approval (Ctrl-C to cancel)...")

    try:
        status, operator_slug = await await_link_approval(base, code, hostname)
    except ControlError as exc:
        print(f"link: REJECTED — control cert failed verification: {exc}")
        return 1
    except LinkError as exc:
        print(f"link: {exc}; aborting.")
        return 1
    if status == "expired":
        print("link: code expired before approval. Run `puffo-agent machine link` again.")
        return 1
    if status == "timeout":
        print("link: timed out waiting for approval.")
        return 1
    print(f"\n  Linked to operator {operator_slug}.")
    print("  The daemon will serve this operator's commands over the control WS.\n")
    return 0


async def run_unlink(operator_slug: str, expected_server_url: str | None = None) -> int:
    """Revoke an operator pairing server-side + locally, and pause that
    operator's agents on this machine. ``expected_server_url`` is an optional
    guard: refuse if the pairing is on a different server."""
    pairing = get_pairing(operator_slug)
    if pairing is None:
        print(f"unlink: no pairing for operator {operator_slug!r}")
        return 2
    if expected_server_url and pairing.server_url.rstrip("/") != expected_server_url.rstrip("/"):
        print(
            f"unlink: operator {operator_slug!r} is paired on {pairing.server_url}, "
            f"not {expected_server_url}"
        )
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
