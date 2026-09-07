"""exa search MCP tool registration.

One generic tool lets an agent run a web search through exa with the server as
the spend-control middle layer:

* ``exa_search`` — PAID. Search the web; the server holds the exa key, checks
  the budget, pays exa at a fixed per-call price, and returns the results.

The agent never holds the exa key or money: it forwards to the server via the
native signed client, and the server calls exa and settles the charge. exa's
plain-search price is fixed and known before the call, so there is no separate
"prepare" step (unlike Monid). Native (key-holding) agents only: the keyless
bridge transport is unsigned and cannot reach the subkey-gated route.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..crypto.http_client import HttpError

logger = logging.getLogger(__name__)

# Comfortably covers a full search (up to 100 results costs ~97_000 micro-
# dollars); the operator's per-agent cap is the real limit, so the model does not
# have to reason about micro-dollars for an ordinary search.
_DEFAULT_MAX_COST_MICRO = 100_000

# Provenance is mandatory. A successful `exa_search` result is stamped as
# exa-sourced (paid, real web search) so the model can attribute it; on any
# failure we tell it that if it falls back to its own knowledge it MUST label
# that as non-exa — never pass it off as an exa search result.
_LABEL_NON_EXA = (
    "If you answer from your own knowledge instead, you MUST clearly label it as "
    "NOT an exa search result — never present non-exa data as an exa result."
)


def _exa_error_message(exc: HttpError) -> str:
    """Pull the server's human-readable ``message`` out of a failed exa response
    body (JSON ``{error, message}``), falling back to a terse ``HTTP <status>``.
    Keeps the tool's error clean instead of a raw blob.
    """
    try:
        parsed = json.loads(exc.body)
        if isinstance(parsed, dict) and parsed.get("message"):
            return str(parsed["message"])
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return f"HTTP {exc.status}"


def register_exa_tools(mcp: FastMCP, cfg: Any) -> None:
    # Native-only tool. A keyless (bridge) agent holds no subkey and cannot reach
    # the subkey-gated `/v2/exa/search` route, so the tool is simply not
    # registered for it — the same conditional-registration pattern the Monid
    # tools use.
    if getattr(cfg, "keyless", False):
        return
    _register_exa_search(mcp, cfg)


def _register_exa_search(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def exa_search(
        query: str,
        num_results: int = 10,
        type: str = "auto",
        max_cost_micro: int = _DEFAULT_MAX_COST_MICRO,
        idempotency_key: str = "",
    ) -> str:
        """Search the web through exa — PAID.

        Puffo is the middle layer: it holds the exa key, checks your spend
        budget, pays exa, and returns the results. You never see the key and
        never hold money. The charge comes out of the shared balance, and your
        operator must have enabled paid tools for you and set a cap first.

        This tool is the ONLY way to reach exa: never install or run an exa CLI,
        and never ask for or hold your own exa key (the server holds it).

        Args:
            query: What to search for, in natural language.
            num_results: How many results to return (1-100). More results cost
                slightly more; the price is fixed and known before the call.
            type: Search mode — one of "auto", "neural", "keyword", "fast".
                Leave as "auto" unless you have a reason to change it.
            max_cost_micro: Your hard ceiling for THIS one call, in micro-dollars
                (1_000_000 = $1). The default covers an ordinary search; if the
                fixed price is above it, the call is rejected before any spend.
            idempotency_key: Optional. Pass a stable value to make a retried call
                safe — it reports the earlier result instead of charging twice.

        Returns the search results and what the call cost, stamped
        `via exa · search · <cost>` — mark data you got this way as exa-sourced.
        Anything you instead answer from your own knowledge MUST be labeled as
        NOT an exa result; never present non-exa data as an exa result.
        """
        if not query.strip():
            raise RuntimeError("query is required")
        if max_cost_micro <= 0:
            raise RuntimeError(
                "max_cost_micro must be a positive integer in micro-dollars "
                "(1_000_000 = $1)"
            )

        body: dict[str, Any] = {
            "query": query,
            "num_results": num_results,
            "type": type,
            "max_cost_micro": max_cost_micro,
        }
        if idempotency_key:
            body["idempotency_key"] = idempotency_key

        try:
            data = await cfg.http_client.post("/v2/exa/search", body)
        except HttpError as exc:
            # A search failure (budget, rate limit, upstream error) means the data
            # was not reached through exa, so it must not be passed off as an exa
            # result. Answering from elsewhere is allowed if labeled non-exa.
            raise RuntimeError(
                f"exa search failed: {_exa_error_message(exc)}\n{_LABEL_NON_EXA}"
            ) from exc

        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected exa response: {data!r}")
        return _format_search_result(data)


def _format_search_result(data: dict[str, Any]) -> str:
    """Render a `/v2/exa/search` response for the model.

    An idempotent retry of an already-settled search returns the accounting with
    null results (results are never stored, so they cannot be replayed) — report
    that rather than pretending there are fresh results.
    """
    cost = data.get("cost_micro")
    status = data.get("provider_http_status")
    results = data.get("results")

    if data.get("already_settled") and results is None:
        return (
            f"via exa · search · cost {cost} micro-dollars (already settled)\n"
            "This search was already run and charged under this idempotency key; "
            "its results are not stored and cannot be replayed. Run a new search "
            "for fresh results."
        )

    # Provenance stamp: this is paid, real exa web-search data.
    header = f"via exa · search · cost {cost} micro-dollars"
    if status is not None:
        header += f", provider status {status}"
    return header + "\nresults:\n" + json.dumps(results, indent=2, ensure_ascii=False)
