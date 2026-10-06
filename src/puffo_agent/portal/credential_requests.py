"""Agentic credential setup: an agent asks its owner for a credential over DM.

The flow, end to end (the message contract is pinned; the web renders it):

1. The agent calls the ``request_credential`` tool. The daemon posts a DM to
   the owner: one plain sentence, then a fenced ``puffo-credential-request``
   block carrying ``request_id`` (uuid4), ``type``, optional ``alias`` and a
   one-line ``reason``. The sentence is for clients that do not render cards.
2. The web shows a Secure Form. On submit the OWNER'S browser seals the value
   to the owner and to this agent and files it through ``/v2/credentials`` —
   never as a message. It then replies in the DM with a fenced
   ``puffo-credential-filed`` block: ``request_id``, ``type``, ``index``,
   ``version``. No secret in it.
3. The daemon sees that reply BEFORE the model does (``worker_run`` runs
   :func:`handle_filed_replies` on each admitted batch), fetches the wrap,
   opens it, and PLACES it. The model is told only ``placed <type> #<index>
   v<version>``. The value never enters a tool result, a message or a log.

Four properties, and who enforces each:

* **Fetch must place.** The daemon retrieves AND writes the value to its
  destination; nothing hands it back to a caller. Enforced here — the easy
  implementation (return it, let the tool write it) looks identical in tests
  and loses the guarantee, so :func:`place` is the only consumer of a value.
* **The filed reply is untrusted.** Acted on only when it is a DM from the
  OWNER, its ``request_id`` is one we issued and have not consumed, its
  ``type`` matches what we asked for, and the GET actually succeeds (the
  credential is distributed to us). Consumed after one use. Enforced here.
* **Nothing reaches the transcript.** Anything in a message is archived by a
  rebuild for days. No fetched value, or part of one, is ever echoed; logs
  carry a fingerprint at most. Enforced here. That the web files the value
  through the API rather than as a message is ASSUMED of the web.
* **The sealed value opens only for us.** The AAD binds the wrap to this
  recipient, type and version. Assumed of the server and the sealing client;
  this module only refuses what does not open.

Placement is SPAWN-TIME in both harnesses (``subscription_credentials``): a
claude-code plan token is an environment variable on the child, a Codex one is
a file the CLI reads at start. So "placed" means: written to the daemon-side
store ``subscription_token`` reads, then the provider runtime reloaded at the
next idle boundary via ``refresh_provider_auth.flag`` — the same path a refreshed
credential already takes. The agent's CLI restarts; its session survives.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# ── the pinned contract ───────────────────────────────────────────────────────

REQUEST_FENCE = "puffo-credential-request"
FILED_FENCE = "puffo-credential-filed"

TYPE_CHATGPT = "PUFFO_CHATGPT_CREDENTIAL_JSON_v1"
TYPE_CLAUDE_TOKEN = "PUFFO_CLAUDE_CODE_TOKEN_v1"
TYPE_CUSTOMIZED = "CUSTOMIZED"
REQUESTABLE_TYPES = frozenset({TYPE_CHATGPT, TYPE_CLAUDE_TOKEN, TYPE_CUSTOMIZED})

#: Which plan-credential type each harness consumes at spawn.
PLAN_TYPE_FOR_HARNESS = {"codex": TYPE_CHATGPT, "claude-code": TYPE_CLAUDE_TOKEN}

_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_FENCED = re.compile(r"```([A-Za-z0-9_-]+)[ \t]*\n(.*?)\n```", re.S)
_REASON_MAX = 200


def fingerprint(value: bytes) -> str:
    """Eight hex chars of SHA-256: enough to match a UI row, useless to an attacker."""
    return hashlib.sha256(value).hexdigest()[:8]


def build_request_message(
    *, request_id: str, credential_type: str, reason: str, alias: str = ""
) -> str:
    """The DM body: a sentence for plain clients, then the fenced block."""
    payload: dict[str, Any] = {
        "request_id": request_id,
        "type": credential_type,
        "reason": reason,
    }
    if alias:
        payload["alias"] = alias
    what = {
        TYPE_CHATGPT: "my ChatGPT plan credential",
        TYPE_CLAUDE_TOKEN: "my Claude Code plan token",
    }.get(credential_type, f"a credential ({alias or 'custom'})")
    sentence = (
        f"I need {what} to continue — {reason}. "
        "Please add it with the secure form below; it is encrypted to me and never sent as a message."
    )
    return f"{sentence}\n\n```{REQUEST_FENCE}\n{json.dumps(payload, sort_keys=True)}\n```"


def parse_filed_reply(text: str) -> dict[str, Any] | None:
    """The ``puffo-credential-filed`` block in ``text``, validated, or None.

    Strict on shape so a malformed or hostile block is simply ignored: a
    uuid4 ``request_id``, a known ``type``, non-negative int ``index`` and
    positive int ``version``. Anything else is None, never an exception.
    """
    for lang, body in _FENCED.findall(text or ""):
        if lang != FILED_FENCE:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        rid = data.get("request_id")
        ctype = data.get("type")
        index = data.get("index")
        version = data.get("version")
        if not (isinstance(rid, str) and _UUID4.fullmatch(rid)):
            continue
        if ctype not in REQUESTABLE_TYPES:
            continue
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            continue
        return {"request_id": rid, "type": ctype, "index": index, "version": version}
    return None


def message_text(content: Any) -> str:
    """A StoredMessage's content as text, whatever shape the store kept it in."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "content", "body"):
            v = content.get(key)
            if isinstance(v, str):
                return v
    return ""


# ── the ledger of requests we issued ─────────────────────────────────────────


@dataclass
class CredentialRequest:
    request_id: str
    type: str
    reason: str
    alias: str = ""
    issued_at: int = 0
    #: ``pending`` → ``in_flight`` → ``placed`` | ``failed``. ``in_flight`` is a
    #: CLAIM taken synchronously before the first await, so two coroutines that
    #: see the same reply (arrival hook, boot sweep, turn scan) cannot both
    #: place it. A request never returns to ``pending``; ``failed`` is terminal.
    state: str = "pending"
    index: int | None = None
    version: int | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "type": self.type,
            "reason": self.reason,
            "alias": self.alias,
            "issued_at": self.issued_at,
            "state": self.state,
            "index": self.index,
            "version": self.version,
            "detail": self.detail,
        }


class RequestLedger:
    """Requests this agent issued, persisted under ``agent_dir`` at 0600.

    Persisted because the owner answers on their own time — a daemon restart
    between the ask and the reply must not orphan the reply. Holds NO secret;
    it is the request_id → type binding the untrusted reply is checked against.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._items: dict[str, CredentialRequest] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in raw.get("requests", []) if isinstance(raw, dict) else []:
            try:
                req = CredentialRequest(**{k: item[k] for k in CredentialRequest.__dataclass_fields__ if k in item})
            except (TypeError, KeyError):
                continue
            self._items[req.request_id] = req

    def _save(self) -> None:
        payload = json.dumps(
            {"version": 1, "requests": [r.to_dict() for r in self._items.values()]},
            indent=2,
        )
        _write_private(self._path, payload)

    def issue(self, *, credential_type: str, reason: str, alias: str = "") -> CredentialRequest:
        req = CredentialRequest(
            request_id=str(uuid.uuid4()),
            type=credential_type,
            reason=reason,
            alias=alias,
            issued_at=int(time.time()),
        )
        self._items[req.request_id] = req
        self._save()
        return req

    def get(self, request_id: str) -> CredentialRequest | None:
        return self._items.get(request_id)

    def pending(self) -> list[CredentialRequest]:
        return [r for r in self._items.values() if r.state == "pending"]

    def claim(self, request_id: str) -> bool:
        """Atomically take a pending request for processing (pending → in_flight).

        Synchronous on purpose: on single-threaded asyncio nothing can interleave
        between the read and the write, so of several coroutines racing on the
        same reply exactly one gets ``True``. A check-then-await would let all
        of them through.
        """
        req = self._items.get(request_id)
        if req is None or req.state != "pending":
            return False
        req.state = "in_flight"
        self._save()
        return True

    def release(self, request_id: str) -> None:
        """Give a claim back (in_flight → pending). The ONE transition back, for a
        reply the server is not ready to honour yet (version behind): the request
        stays open so the next sighting of the reply retries. ``failed`` never
        comes back through here."""
        req = self._items.get(request_id)
        if req is not None and req.state == "in_flight":
            req.state = "pending"
            self._save()

    def settle(self, request_id: str, *, state: str, index: int | None = None,
               version: int | None = None, detail: str = "") -> None:
        """Finish a claimed request as ``placed`` or ``failed``. Never back to pending."""
        assert state in ("placed", "failed"), state
        req = self._items.get(request_id)
        if req is None or req.state not in ("pending", "in_flight"):
            return
        req.state, req.index, req.version, req.detail = state, index, version, detail
        self._save()


# ── the daemon-side store subscription_token() reads ─────────────────────────

STORE_FILENAME = "plan_credentials.json"


def store_path(agent_dir: Path) -> Path:
    """Under agent_dir, OUTSIDE workspace/: no rebuild archive carries it
    (#247's allowlists are ``workspace/`` and ``cli_session.json``), which is
    deliberate — boot reconcile re-derives it from the server instead."""
    return agent_dir / STORE_FILENAME


def read_store(agent_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(store_path(agent_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = raw.get("credentials") if isinstance(raw, dict) else None
    return items if isinstance(items, dict) else {}


def stored_value(agent_dir: Path, key: str) -> str:
    """The stored value for ``key`` (a type, or ``alias:<name>``), or ""."""
    item = read_store(agent_dir).get(key)
    if not isinstance(item, dict):
        return ""
    v = item.get("value")
    return v if isinstance(v, str) else ""


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def place(
    *,
    agent_dir: Path,
    credential_type: str,
    index: int,
    version: int,
    value: bytes,
    alias: str = "",
) -> str:
    """Write the value where the daemon's spawn path reads it. Returns the
    store key. The ONLY function that touches a plaintext value.

    Plan types key by type (one active plan per harness); CUSTOMIZED keys by
    ``alias:<name>`` so several can coexist and the child sees each as the
    environment variable named by its alias.
    """
    key = f"alias:{alias}" if credential_type == TYPE_CUSTOMIZED else credential_type
    if credential_type == TYPE_CUSTOMIZED and not _ALIAS.fullmatch(alias or ""):
        raise ValueError("a CUSTOMIZED credential needs an alias usable as an env var name")
    items = read_store(agent_dir)
    items[key] = {
        "type": credential_type,
        "index": index,
        "version": version,
        "value": value.decode("utf-8"),
        "fingerprint": fingerprint(value),
        "placed_at": int(time.time()),
    }
    _write_private(store_path(agent_dir), json.dumps({"version": 1, "credentials": items}, indent=2))
    return key


def customized_env(agent_dir: Path) -> dict[str, str]:
    """``{ALIAS: value}`` for every CUSTOMIZED credential placed. Reaches the
    child through ``controlled`` only, the one sanctioned channel."""
    out: dict[str, str] = {}
    for key, item in read_store(agent_dir).items():
        if key.startswith("alias:") and isinstance(item, dict) and isinstance(item.get("value"), str):
            out[key[len("alias:"):]] = item["value"]
    return out


def request_provider_reload(workspace: Path, *, reason: str) -> None:
    """Ask the worker to reload the provider runtime at its next idle boundary.

    The same flag a refreshed credential writes (``daemon.py``): the CLI child
    is respawned with the new credential, the Puffo session is kept.
    """
    from .state import refresh_provider_auth_flag_path

    flag = refresh_provider_auth_flag_path(workspace)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(
        json.dumps({"version": 1, "requested_at": int(time.time()), "reason": reason}) + "\n",
        encoding="utf-8",
    )


# ── acting on a filed reply ──────────────────────────────────────────────────


@dataclass
class FiledOutcome:
    request_id: str
    state: str  # placed | failed | ignored
    detail: str = ""


async def handle_filed_replies(
    items: Sequence[Any],
    *,
    owner_slug: str,
    ledger: RequestLedger,
    credentials: Any,
    agent_dir: Path,
    workspace: Path,
    harness: str,
    agent_id: str = "",
) -> list[FiledOutcome]:
    """Scan admitted messages for the owner's filed replies and act on each.

    Runs in the daemon before the model sees the batch. Never raises: a
    failure settles the request as ``failed`` with a reason the agent can
    read through ``credential_status``, and the message still reaches the
    model (it carries no secret).

    **Why ``sender_slug`` can carry the owner check**, per wire:

    * *Cloud (keyless bridge)*: puffo-server attests the sender at ingest —
      ``messages.rs:519`` rejects an envelope whose declared sender does not
      match the authenticated one — and delivers over this agent's
      sandbox-token WebSocket. The keyless agent does NO local signature
      check, so this is ENFORCED by puffo-server and ASSUMED here.
    * *Native*: verified locally against the cert cache before the message is
      stored (``inbound_receipts.py:238-253``). Enforced on this side.

    The checks run in this order and the order is load-bearing: the cheap,
    local refusals (wire, sender, shape, ledger, type) come before anything
    that talks to the server, and the server is asked to CONFIRM (list) before
    it is asked to HAND OVER (get).
    """
    del harness  # reserved: placement is type-keyed today
    outcomes: list[FiledOutcome] = []
    for item in items:
        # Three paths feed this (turn scan, arrival hook, boot sweep) and all
        # must hand over the store's own row type. A different class would make
        # every getattr below answer "" and skip silently — the #247 inert-
        # feature failure — so an unexpected type is logged, not ignored.
        if not _is_stored_row(item):
            logger.warning("credential replies: unexpected row type %s; skipping", type(item).__name__)
            continue
        if getattr(item, "envelope_kind", "") != "dm":
            continue
        if getattr(item, "sender_slug", "") != owner_slug:
            continue  # only the owner files credentials
        filed = parse_filed_reply(message_text(getattr(item, "content", "")))
        if filed is None:
            continue
        rid = filed["request_id"]
        req = ledger.get(rid)
        # CLAIM before the first await: the loser of a race sees in_flight.
        if req is None or not ledger.claim(rid):
            outcomes.append(FiledOutcome(rid, "ignored", "unknown or already consumed request_id"))
            continue
        if req.type != filed["type"]:
            ledger.settle(rid, state="failed", detail="filed type differs from the requested type")
            outcomes.append(FiledOutcome(rid, "failed", "type mismatch"))
            continue
        if credentials is None:
            ledger.settle(rid, state="failed", detail="this agent cannot read credentials")
            outcomes.append(FiledOutcome(rid, "failed", "no credential reader"))
            continue
        got = await _confirm_and_fetch(filed, credentials)
        if isinstance(got, FiledOutcome):
            if got.state == "failed":
                ledger.settle(rid, state="failed", detail=got.detail)
            else:
                ledger.release(rid)  # transient (server behind): keep it open for a retry
            outcomes.append(FiledOutcome(rid, got.state, got.detail))
            continue
        outcomes.append(
            _place_and_settle(got, req, ledger=ledger, agent_dir=agent_dir, workspace=workspace, agent_id=agent_id)
        )
    return outcomes


async def _confirm_and_fetch(filed: dict[str, Any], credentials: Any) -> Any:
    """CONFIRM against the server's list, then FETCH — in that order.

    Returns the ``HeldCredential``, or a ``FiledOutcome`` (``failed`` /
    ``ignored``) explaining why not. Never raises; never names a value.
    """
    rid, ctype, index = filed["request_id"], filed["type"], filed["index"]
    # The reply says a wrap was filed for us; the list says what we actually
    # hold. A reply naming an index the server does not list is not acted on.
    try:
        listed = await credentials.held(ctype)
    except Exception as exc:  # noqa: BLE001 — never let a reply break the turn
        return FiledOutcome(rid, "failed", f"list failed: {type(exc).__name__}")
    if index not in {i for i, _alias in listed}:
        return FiledOutcome(rid, "failed", "not listed for this agent by the server")
    # A filed reply is a one-off event about a NEW value: never serve it from
    # memory, or a re-filed newer version reads as "server behind".
    _forget(credentials, ctype, index)
    try:
        held = await credentials.get(ctype, index)
    except Exception as exc:  # noqa: BLE001
        return FiledOutcome(rid, "failed", f"fetch failed: {type(exc).__name__}")
    if held is None:
        return FiledOutcome(rid, "failed", "not distributed to this agent")
    if held.version < filed["version"]:
        # The server is behind the reply; ask again next time rather than
        # placing an older version under a newer label.
        return FiledOutcome(rid, "ignored", "server version behind the reply")
    return held


def _place_and_settle(
    held: Any,
    req: CredentialRequest,
    *,
    ledger: RequestLedger,
    agent_dir: Path,
    workspace: Path,
    agent_id: str,
) -> FiledOutcome:
    """PLACE the value, consume the request, ask for the provider reload.
    The only caller of :func:`place` on this path."""
    rid = req.request_id
    try:
        place(
            agent_dir=agent_dir,
            credential_type=held.type,
            index=held.index,
            version=held.version,
            value=held.value,
            alias=req.alias,
        )
    except Exception as exc:  # noqa: BLE001
        ledger.settle(rid, state="failed", detail=f"placement failed: {type(exc).__name__}")
        return FiledOutcome(rid, "failed", f"placement failed: {type(exc).__name__}")
    ledger.settle(rid, state="placed", index=held.index, version=held.version)
    logger.info(
        "agent %s: placed %s #%s v%s (fp %s); provider reload requested",
        agent_id, held.type, held.index, held.version, fingerprint(held.value),
    )
    try:
        request_provider_reload(workspace, reason=f"credential placed: {held.type}")
    except OSError as exc:
        logger.warning("agent %s: could not request provider reload: %s", agent_id, exc)
    return FiledOutcome(rid, "placed")

async def sweep_stored_replies(
    store: Any,
    *,
    owner_slug: str,
    ledger: RequestLedger,
    credentials: Any,
    agent_dir: Path,
    workspace: Path,
    harness: str,
    agent_id: str = "",
    limit: int = 200,
) -> list[FiledOutcome]:
    """At boot: act on owner replies that were STORED before this process
    existed — a reply that landed while the agent was paused, or one an
    older build admitted to a turn without acting on it.

    Bounded (the newest ``limit`` DMs with the owner) and idempotent (the
    ledger consumes a request once, so a second boot is a no-op). Funnels
    into :func:`handle_filed_replies`, so the owner / DM / list-confirm /
    version-floor chain is the same one seam. Skipped entirely when nothing is
    pending, so a settled agent costs no query.
    """
    pending = ledger.pending()
    if not pending:
        return []
    # Bounded in TIME as well as count: only DMs that arrived after the oldest
    # pending request was issued can answer it (a minute of clock slack).
    floor_ms = (min(r.issued_at for r in pending) - 60) * 1000
    try:
        rows = [r for r in await store.get_dm_history(owner_slug, limit=limit)
                if int(getattr(r, "received_at", 0) or 0) >= floor_ms]
    except Exception as exc:  # noqa: BLE001 — fail open, like every boot step
        logger.info("agent %s: credential reply sweep skipped: %s", agent_id, type(exc).__name__)
        return []
    outcomes = await handle_filed_replies(
        rows, owner_slug=owner_slug, ledger=ledger, credentials=credentials,
        agent_dir=agent_dir, workspace=workspace, harness=harness, agent_id=agent_id,
    )
    if outcomes:
        logger.info("agent %s: credential reply sweep: %s", agent_id,
                    ", ".join(f"{o.request_id[:8]}={o.state}" for o in outcomes))
    return outcomes


def _forget(credentials: Any, credential_type: str, index: int) -> None:
    """Bypass the reader's cache for one credential, whichever reader it is.

    ``CloudAgentCredentials`` exposes ``forget``; ``AgentCredentials`` has the
    generation-fenced ``_invalidate``. Neither raising matters here.
    """
    forget = getattr(credentials, "forget", None)
    if callable(forget):
        forget(credential_type, index)
        return
    invalidate = getattr(credentials, "_invalidate", None)
    if callable(invalidate):
        invalidate(lambda c: c.type == credential_type and c.index == index)


def _is_stored_row(item: Any) -> bool:
    """The store's row type, or a duck close enough to carry the three fields
    the chain reads. Tests use plain namespaces; production passes StoredMessage."""
    try:
        from ..agent.message_store_models import StoredMessage

        if isinstance(item, StoredMessage):
            return True
    except Exception:  # noqa: BLE001
        pass
    return all(hasattr(item, a) for a in ("envelope_kind", "sender_slug", "content"))


def status_line(req: CredentialRequest | None) -> str:
    """What the model is told. Never the value."""
    if req is None:
        return "unknown request_id"
    if req.state == "in_flight":
        return f"pending: placing {req.type} now"
    if req.state == "placed":
        return (
            f"placed {req.type} #{req.index} v{req.version}; "
            "restarting my CLI to pick it up"
        )
    if req.state == "failed":
        return f"failed: {req.detail}"
    return f"pending: waiting for the owner to file {req.type}"


# ── boot reconcile ───────────────────────────────────────────────────────────


async def reconcile_at_boot(
    *,
    credentials: Any,
    agent_dir: Path,
    harness: str,
    agent_id: str = "",
) -> bool:
    """Before the first spawn: if the server holds a plan credential for this
    harness, fetch and place it. One bounded attempt, fail-open.

    Why: a rebuild or resume-recreate re-injects the OLD vault value into the
    environment and the store is not carried. Re-deriving from the server
    makes a filed credential survive without carrying a secret file around.
    Returns whether something was placed.
    """
    ctype = PLAN_TYPE_FOR_HARNESS.get(harness)
    if ctype is None or credentials is None:
        return False
    # FIRST make sure the server holds a key for us (cloud readers only; the
    # native reader registers via keep_registering). Without this, an agent
    # claimed before puffo-server #437 can never be filed to — see
    # CloudAgentCredentials.ensure_registered. Fail-open like every branch.
    register = getattr(credentials, "ensure_registered", None)
    if callable(register):
        try:
            version = await register()
            logger.info("agent %s: boot credential reconcile: credential key registered (v%s)", agent_id, version)
        except Exception as exc:  # noqa: BLE001
            logger.info("agent %s: boot credential reconcile: key registration unavailable (%s); continuing",
                        agent_id, type(exc).__name__)
    try:
        held_list = await credentials.held(ctype)
    except Exception as exc:  # noqa: BLE001 — fail open to the env var
        logger.info("agent %s: boot credential list unavailable (%s); using the environment",
                    agent_id, type(exc).__name__)
        return False
    if not held_list:
        # The common case on a fresh agent, and the one an operator reading the
        # log after a rebuild needs to see: the server was asked and lists
        # nothing, so the spawn falls through to the vault env var.
        logger.info("agent %s: boot credential reconcile: server lists no %s for this agent; "
                    "using the environment", agent_id, ctype)
        return False
    # The lowest index. Fine while a plan type is one-per-harness; a second
    # plan credential of the same type would need an explicit choice here.
    index = held_list[0][0]
    try:
        held = await credentials.get(ctype, index)
    except Exception as exc:  # noqa: BLE001
        logger.info("agent %s: boot credential fetch failed (%s); using the environment",
                    agent_id, type(exc).__name__)
        return False
    if held is None:
        return False
    current = read_store(agent_dir).get(ctype) or {}
    if current.get("version") == held.version and current.get("fingerprint") == fingerprint(held.value):
        logger.info("agent %s: boot credential reconcile: %s #%s v%s already placed (fp %s)",
                    agent_id, ctype, index, held.version, fingerprint(held.value))
        return False
    try:
        place(agent_dir=agent_dir, credential_type=held.type, index=held.index,
              version=held.version, value=held.value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent %s: boot placement failed: %s", agent_id, type(exc).__name__)
        return False
    logger.info("agent %s: boot reconcile placed %s #%s v%s (fp %s)",
                agent_id, held.type, held.index, held.version, fingerprint(held.value))
    return True
